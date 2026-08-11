#!/usr/bin/env python3
"""
ETF 动量轮动 — 信号扫描工具 v2.0

改进点（基于知乎社区实证研究）:
  1. RSRS 动量: 年化斜率 × R² 替代简单涨幅，过滤"涨得疯但不稳"的标的
  2. 跨资产候选池: 10 只 ETF 覆盖宽基/行业/跨境/防御
  3. 成交量异动过滤: 放量 > 5日均量×2.5 剔除
  4. 防御资产切换: 无信号时切换 511880 银华日利
  5. 绝对止损线: 持仓亏损 8% 清仓
  6. 信号强弱分级: 强/中/弱三级，弱信号可减半仓

用法:
    python3 tools/momentum_signal.py                     # 默认池扫描
    python3 tools/momentum_signal.py --json              # JSON 输出
    python3 tools/momentum_signal.py --pool 518880,513100,159915,510300
    python3 tools/momentum_signal.py --entry 159915 3.45 # 检查止损线
"""

import argparse, json, os, subprocess, sys, time
from datetime import datetime, time as datetime_time
from zoneinfo import ZoneInfo

try:
    from tools.etf_market_data import load_etf_series
    from tools.momentum_core import (
        MomentumConfig,
        STOP_LOSS_PCT,
        evaluate_momentum_signal,
        rank_momentum_signals,
        select_rotation_target,
    )
    from tools.strategy_models import RunStatus, StrategyError, strict_json_dumps
except ModuleNotFoundError:  # 支持直接执行 tools/momentum_signal.py
    from etf_market_data import load_etf_series
    from momentum_core import MomentumConfig, STOP_LOSS_PCT, evaluate_momentum_signal, rank_momentum_signals, select_rotation_target
    from strategy_models import RunStatus, StrategyError, strict_json_dumps

_TIMEOUT = 15
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "cache")

# 跨资产候选池: 宽基 + 行业 + 跨境 + 防御
POOL = {
    # A股宽基
    "510300": "沪深300ETF",
    "510500": "中证500ETF",
    "159915": "创业板ETF",
    "588000": "科创50ETF",
    # 行业
    "512880": "证券ETF",
    "512690": "酒ETF",
    "512010": "医药ETF",
    # 跨境
    "513100": "纳指ETF",
    "513180": "恒生科技ETF",
    "159920": "恒生ETF",
    # 防御/商品
    "518880": "黄金ETF",
    "511880": "银华日利",  # 现金替代
}

# 防御资产: 所有标的都不通过时切换至此
DEFENSIVE_CODE = "511880"

SHANGHAI = ZoneInfo("Asia/Shanghai")


def determine_market_closed(last_bar_date: str, *, now: datetime | None = None) -> bool:
    """Return whether a Shanghai daily bar is finalized for formal signals."""
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    current_date = current.date().isoformat()
    if last_bar_date < current_date:
        return True
    if last_bar_date > current_date:
        return False
    return current.weekday() < 5 and current.time() >= datetime_time(15, 5)


def _qq_code(code):
    code = code.strip().replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    if code.startswith(("6", "9", "5")): return "sh" + code
    elif code.startswith(("0", "3", "2", "1")): return "sz" + code
    elif code.startswith(("4", "8")): return "bj" + code
    return "sh" + code


def _curl_json(url):
    result = subprocess.run(
        ["/usr/bin/curl", "-s", "--noproxy", "*",
         "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", url],
        capture_output=True, timeout=_TIMEOUT)
    raw = result.stdout
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return json.loads(raw.decode("gbk"))


def fetch_kline(code, count=300):
    """获取最近N个交易日K线，1小时缓存。"""
    cache_path = os.path.join(_CACHE_DIR, f"etf_signal_{code}_qfq_{count}.json")
    if os.path.exists(cache_path):
        age_hours = (time.time() - os.path.getmtime(cache_path)) / 3600
        if age_hours < 1:
            try:
                with open(cache_path) as f:
                    data = json.load(f).get("data", [])
                    if data: return data
            except: pass

    qq = _qq_code(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={qq},day,,,{count},qfq"
    resp = _curl_json(url)
    data = resp.get("data", {}).get(qq, {})
    raw = data.get("qfqday") or data.get("day") or []

    result = []
    for row in raw:
        if len(row) >= 6:
            result.append({"date": str(row[0]), "open": float(row[1]), "close": float(row[2]),
                           "high": float(row[3]), "low": float(row[4]), "volume": float(row[5])})
    if result:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        try:
            with open(cache_path, "w") as f:
                json.dump({"code": code, "data": result}, f, ensure_ascii=False)
        except: pass
    return result


def scan(
    pool,
    momentum_period=25,
    *,
    ma_period=20,
    market_closed=None,
    now=None,
    series_by_code=None,
    config=None,
    holding=None,
    switch_buffer=1.0,
):
    """Scan one complete ETF cross-section and return a structured envelope."""
    config = config or MomentumConfig(
        rsrs_period=momentum_period, ma_period=ma_period
    )
    candidates = {
        code: name for code, name in pool.items() if code != DEFENSIVE_CODE
    }
    loaded = {}
    errors = []

    for code in candidates:
        try:
            if series_by_code is not None:
                series = series_by_code[code]
            else:
                series = load_etf_series(
                    code, count=max(300, config.warmup_days + 1)
                )
            if not tuple(series.bars):
                raise ValueError("数据不足")
            loaded[code] = series
        except Exception as exc:
            errors.append(StrategyError(
                code=code,
                stage="load",
                source=None,
                message=str(exc) or "数据加载失败",
            ))

    end_dates = {
        series.manifest.end_date for series in loaded.values()
    }
    same_end_date = len(end_dates) <= 1
    as_of = next(iter(end_dates)) if len(end_dates) == 1 else None
    if not same_end_date:
        errors.append(StrategyError(
            code=None,
            stage="cross_section",
            source=None,
            message="候选池 manifest.end_date 不一致",
        ))

    pool_complete = len(loaded) == len(candidates) and same_end_date
    closed = (
        bool(market_closed)
        if market_closed is not None
        else (
            determine_market_closed(as_of, now=now)
            if as_of is not None
            else None
        )
    )
    formal_scan = pool_complete and closed is True
    snapshots = []
    items = []

    for code, name in candidates.items():
        series = loaded.get(code)
        if series is None:
            error = next(error for error in errors if error.code == code)
            items.append({
                "code": code,
                "name": name,
                "error": error.message,
                "pass": False,
                "formal": False,
                "provisional": None if closed is None else not closed,
                "signal_strength": "none",
            })
            continue

        bars = tuple(series.bars)
        item_closed = (
            bool(market_closed)
            if market_closed is not None
            else determine_market_closed(series.manifest.end_date, now=now)
        )
        try:
            snapshot = evaluate_momentum_signal(
                code, bars, len(bars) - 1, config
            )
        except Exception as exc:
            errors.append(StrategyError(
                code=code,
                stage="evaluate",
                source=getattr(series.manifest, "source", None),
                message=str(exc) or "信号计算失败",
            ))
            pool_complete = False
            formal_scan = False
            items.append({
                "code": code,
                "name": name,
                "date": getattr(series.manifest, "end_date", None),
                "error": str(exc),
                "pass": False,
                "formal": False,
                "provisional": not item_closed,
                "signal_strength": "none",
            })
            continue

        snapshots.append(snapshot)
        metrics = dict(snapshot.metrics)
        if not metrics["formal"]:
            errors.append(StrategyError(
                code=code,
                stage="history",
                source=getattr(series.manifest, "source", None),
                message=(
                    f"严格历史交易日不足: {metrics['strict_history_days']}"
                    f" < {config.warmup_days}"
                ),
            ))
            pool_complete = False
            formal_scan = False
        item_formal = bool(metrics["formal"] and formal_scan)
        passed = bool(snapshot.passed and formal_scan)
        closes = [float(bar["close"]) for bar in bars]
        volume_ratio = metrics["volume_ratio"]
        items.append({
            "code": code,
            "name": name,
            "date": snapshot.date,
            "close": round(metrics["close"], 4),
            "raw_rsrs_score": snapshot.raw_rsrs_score,
            "rsrs_score": metrics["display_rsrs_score"],
            "slope_annual_pct": snapshot.slope_annual_pct,
            "r_squared": snapshot.r_squared,
            "ma": round(metrics["ma"], 4) if metrics["ma"] is not None else None,
            "ma_period": config.ma_period,
            "ma60": round(metrics["ma60"], 4) if metrics["ma60"] is not None else None,
            "above_ma": metrics["above_ma"],
            "above_ma60": metrics["ma60"] is not None and metrics["close"] > metrics["ma60"],
            "golden_cross": bool(metrics["golden_cross"] and formal_scan),
            "vol_20d": round(metrics["current_volatility"] * 100, 2),
            "vol_median": round(metrics["historical_volatility_median"] * 100, 2),
            "vol_ok": metrics["volatility_ok"],
            "vol_ratio": round(volume_ratio, 2) if volume_ratio is not None else None,
            "volume_surge": not metrics["volume_ok"],
            "day_down": metrics.get("day_down", False),
            "rsi": round(metrics["rsi"], 1),
            "rsi_overbought": not metrics["rsi_ok"],
            "pct_5d": round((closes[-1] / closes[-6] - 1) * 100, 2) if len(closes) >= 6 else 0,
            "pct_20d": round((closes[-1] / closes[-21] - 1) * 100, 2) if len(closes) >= 21 else 0,
            "metrics": metrics,
            "pass": passed,
            "formal": item_formal,
            "provisional": not item_closed,
            "signal_strength": snapshot.signal_strength if formal_scan else "none",
        })

    selected = None
    if pool_complete and formal_scan:
        items.sort(key=lambda item: (
            -item.get("raw_rsrs_score", float("-inf")),
            -item.get("r_squared", float("-inf")),
            item["code"],
        ))
        ranked = rank_momentum_signals(snapshots)
        if ranked is not None:
            selected = next(item for item in items if item["code"] == ranked.code)
    elif not formal_scan:
        for item in items:
            item["pass"] = False
            item["formal"] = False
            item["signal_strength"] = "none"
            if "golden_cross" in item:
                item["golden_cross"] = False

    # 迟滞轮动决策（仅在正式扫描时计算）
    rotation = None
    if pool_complete and formal_scan:
        rotation = select_rotation_target(holding, snapshots, switch_buffer)
    elif not formal_scan:
        rotation = None  # 非正式扫描不产生操作建议

    if not pool_complete:
        status = RunStatus.UNKNOWN
        selected = None
    elif closed is not True:
        status = RunStatus.PROVISIONAL
    elif selected is None:
        status = RunStatus.NO_SIGNAL
    else:
        status = RunStatus.OK

    return {
        "status": status,
        "as_of": as_of,
        "items": items,
        "errors": errors,
        "selected": selected,
        "rotation": rotation,
        "pool_complete": pool_complete,
    }


def check_stop_loss(entry_code, entry_price, current_price=None):
    """检查持仓是否触发止损线。

    优先使用 load_etf_series（东方财富+腾讯交叉验证），不可用时降级到
    腾讯 API (fetch_kline)。同时检查最后一条 bar 是否为盘中未完成日 K，
    若是则标注 (盘中实时价，非正式信号)。
    """
    last_bar_date = None
    if current_price is None:
        # 优先使用 load_etf_series（东方财富+腾讯交叉验证）
        try:
            series = load_etf_series(entry_code, count=5)
            if series and series.bars:
                current_price = series.bars[-1]["close"]
                last_bar_date = series.manifest.end_date
        except Exception:
            # 降级到腾讯 API (fetch_kline)
            klines = fetch_kline(entry_code, count=5)
            if not klines:
                return None
            current_price = klines[-1]["close"]
            last_bar_date = klines[-1]["date"]

    loss_pct = (current_price - entry_price) / entry_price
    triggered = loss_pct <= -STOP_LOSS_PCT
    result = {
        "entry_price": entry_price,
        "current_price": current_price,
        "loss_pct": round(loss_pct * 100, 2),
        "stop_loss_line": round(entry_price * (1 - STOP_LOSS_PCT), 4),
        "triggered": triggered,
    }

    # 盘中检查: 如果最后 bar 日期为当日且尚未收盘，标注盘中价格
    if last_bar_date is not None:
        market_closed = determine_market_closed(last_bar_date)
        if not market_closed:
            result["intraday"] = True
            result["intraday_note"] = "(盘中实时价，非正式信号)"

    return result


def main():
    parser = argparse.ArgumentParser(description="ETF动量轮动信号扫描 v2.0")
    parser.add_argument("--pool", default="518880,513100,159915,510300,512880,512690,512010,588000,510500,513180",
                        help="ETF池（逗号分隔）")
    parser.add_argument("--momentum", type=int, default=25, help="RSRS 计算周期（默认25日，v3.0 规范）")
    parser.add_argument("--ma-period", type=int, default=20, help="均线周期（默认20日）")
    parser.add_argument("--json", action="store_true", help="JSON输出")
    parser.add_argument("--entry", nargs=2, metavar=("CODE", "PRICE"),
                        help="检查止损: --entry 159915 3.45")
    parser.add_argument("--holding", default=None,
                        help="当前持仓代码（启用迟滞决策）")
    parser.add_argument("--switch-buffer", type=float, default=1.0,
                        help="换仓迟滞系数（默认 1.0=无迟滞，≥1.0）")
    args = parser.parse_args()

    # 止损检查模式
    if args.entry:
        code, price = args.entry[0], float(args.entry[1])
        name = POOL.get(code, code)
        result = check_stop_loss(code, price)
        if args.json:
            print(strict_json_dumps(result))
            return
        if result:
            print(f"  {name} ({code})")
            print(f"  入场价: {result['entry_price']:.4f}")
            print(f"  当前价: {result['current_price']:.4f}")
            if result.get("intraday_note"):
                print(f"  ⚠️  {result['intraday_note']}")
            print(f"  浮动盈亏: {result['loss_pct']:+.2f}%")
            print(f"  止损线 (8%): {result['stop_loss_line']:.4f}")
            status = "🔴 触发止损！" if result['triggered'] else "🟢 未触发"
            if result.get("intraday"):
                status += " (盘中非正式)"
            print(f"  状态: {status}")
        return

    # 信号扫描模式
    codes = [c.strip() for c in args.pool.split(",") if c.strip()]
    pool = {c: POOL.get(c, c) for c in codes}

    result = scan(pool, args.momentum, ma_period=args.ma_period,
                  holding=args.holding, switch_buffer=args.switch_buffer)

    if args.json:
        print(strict_json_dumps(result))
        return

    # 格式化输出
    results = result["items"]
    ma_label = f"MA{args.ma_period}"
    ranked_output = result["status"] in (RunStatus.OK, RunStatus.NO_SIGNAL)
    date_str = result["as_of"] or datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    print("=" * 75)
    print(f"  ETF 动量轮动 · RSRS 信号扫描 v2.0")
    print(f"  扫描日期: {date_str}  |  候选池: {len(results)} 只 ETF")
    print(f"  核心指标: RSRS = 年化斜率 × R²（趋势强度 × 趋势质量）")
    print(f"  过滤: 收盘>{ma_label} | 波动<历史中位×1.5 | 下行放量(distribution) | RSI<80")
    print("=" * 75)

    # 信号分级统计
    strong = [r for r in results if r.get("signal_strength") == "strong"]
    medium = [r for r in results if r.get("signal_strength") == "medium"]
    no_signal = [r for r in results if r.get("signal_strength") == "none"]

    # 详细输出
    for rank, r in enumerate(results, 1):
        item_prefix = f"{rank:>2}." if ranked_output else " - "
        if r.get("error"):
            print(f"\n  {item_prefix} ❌ {r['code']} {r['name']}  ⚠️ {r['error']}")
            continue

        strength_icon = {
            "strong": "🟢", "medium": "🟡", "none": "⚪"
        }.get(r["signal_strength"], "⚪")

        print(f"\n  {item_prefix} {strength_icon} {r['code']} {r['name']}")

        # RSRS 核心行
        print(f"     RSRS: {r['rsrs_score']:.2f}  "
              f"(斜率 {r['slope_annual_pct']:+.1f}% × R² {r['r_squared']:.3f})  |  "
              f"20日涨幅 {r['pct_20d']:+.2f}%  |  5日 {r['pct_5d']:+.2f}%")

        # 过滤条件
        ma_status = "✅" if r["above_ma"] else "❌"
        cross = " 双多头" if r.get("golden_cross") else ""
        print(f"     {ma_label}: {r['ma']} {ma_status}{cross}  |  "
              f"MA60: {r['ma60']} {'✅' if r.get('above_ma60') else '—'}")

        vol_status = "✅" if r["vol_ok"] else "❌ 过热"
        print(f"     波动率: {r['vol_20d']}%  |  历史中位: {r['vol_median']}%  |  {vol_status}")

        surge_warn = ""
        if r["volume_surge"]:
            if r.get("day_down"):
                surge_warn = (
                    f" 🔴 放量{r['vol_ratio']:.1f}倍+下行(distribution)"
                    if r["vol_ratio"] is not None
                    else " 🔴 放量+下行(distribution)"
                )
            else:
                surge_warn = (
                    f" 🟡 放量{r['vol_ratio']:.1f}倍(上行,不作为过滤条件)"
                    if r["vol_ratio"] is not None
                    else " 🟡 放量(上行)"
                )
        rsi_warn = f" 🔴 RSI={r['rsi']}" if r["rsi_overbought"] else ""
        extra = (surge_warn + rsi_warn).strip()
        if extra:
            print(f"     异动: {extra}")

        if not r["pass"]:
            reasons = []
            if not r.get("above_ma"): reasons.append(f"收盘<{ma_label}")
            if not r.get("vol_ok"): reasons.append("波动率过热")
            if r.get("volume_surge") and r.get("day_down"): reasons.append("下行放量(distribution)")
            if r.get("rsi_overbought"): reasons.append("RSI超买")
            if r.get("rsrs_score", 0) <= 0: reasons.append("RSRS动量≤0")
            if r.get("error"):
                reasons.append(r["error"])
            if not reasons:
                if r.get("provisional"):
                    reasons.append("盘中非正式扫描")
                else:
                    reasons.append("候选池数据不完整或历史不足")
            print(f"     🔴 不通过: {', '.join(reasons)}")

    # ===== 操作建议 =====
    print(f"\n{'='*75}")
    print(f"  📋 操作建议")
    print(f"{'='*75}")

    pass_list = [r for r in results if r["pass"]]
    best = result["selected"]

    if result["status"] in (RunStatus.UNKNOWN, RunStatus.PROVISIONAL):
        status_label = (
            "候选池数据不完整"
            if result["status"] == RunStatus.UNKNOWN
            else "当日行情尚未正式收盘"
        )
        print(f"  ⚪ 无法形成正式结论: {status_label}")
    elif best:
        name = best["name"]
        code = best["code"]
        strength = best["signal_strength"]

        action = "全仓买入"
        detail = ("双均线多头 + RSRS 高分" if strength == "strong"
                  else f"站上 {ma_label} 且满足全部五项条件")

        print(f"  🟢 买入信号: {code} {name}")
        print(f"     RSRS 得分: {best['rsrs_score']:.2f}/5  |  信号强度: {strength}")
        print(f"     当前价: {best['close']}  |  止损线: {round(best['close'] * (1 - STOP_LOSS_PCT), 4)}")
        print(f"     操作: {action}")
        print(f"     理由: {detail}")

        # 迟滞决策
        rotation = result.get("rotation")
        if rotation:
            rot_label = {
                "buy": "买入", "switch": "换仓", "hold": "持有不动",
                "liquidate": "清仓", "none": "无操作",
            }.get(rotation["action"], rotation["action"])
            print(f"     迟滞决策: {rot_label} — {rotation['reason']}")

        # 展示第二名对比
        second = pass_list[1] if len(pass_list) > 1 else None
        if second:
            gap = best["rsrs_score"] - second["rsrs_score"]
            print(f"     排名第2: {second['code']} {second['name']} (RSRS {second['rsrs_score']:.2f}, 差距 {gap:.2f})")
    else:
        print(f"  🔴 无买入信号")
        print(f"     池内 {len(results)} 只 ETF 均不满足全部条件")

        # 最接近通过的标的
        near = [r for r in results
                if r.get("above_ma") and r.get("vol_ok") and r.get("rsrs_score", 0) > 0
                and (r.get("volume_surge") or r.get("rsi_overbought"))]
        near2 = [r for r in results
                 if r.get("above_ma") and not r.get("vol_ok") and r.get("rsrs_score", 0) > 0]
        near3 = [r for r in results
                 if r.get("vol_ok") and r.get("rsrs_score", 0) > 0 and not r.get("above_ma")]

        if near:
            codes = ", ".join(f"{r['code']}" for r in near)
            print(f"     接近通过(成交/RSI问题): {codes}")
        if near2:
            codes = ", ".join(f"{r['code']}(波动高)" for r in near2)
            print(f"     接近通过(波动率高): {codes}")
        if near3:
            codes = ", ".join(f"{r['code']}(均线下)" for r in near3)
            print(f"     均线未站上: {codes}")

        print(f"     操作: 切换至防御资产 {DEFENSIVE_CODE} 银华日利，或持币等待")
        print(f"     当前现金不应投入任何股票型 ETF")

    # 信号分布统计
    print(f"\n  ── 信号分布 ──")
    print(f"  🟢 强信号: {len(strong)}  |  🟡 中等: {len(medium)}  |  "
          f"⚪ 无信号: {len(no_signal)}")
    print()


if __name__ == "__main__":
    main()
