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

import argparse, json, math, os, subprocess, sys, time
from datetime import datetime

try:
    from tools.etf_market_data import load_etf_series
    from tools.momentum_core import (
        MomentumConfig,
        evaluate_momentum_signal,
        rank_momentum_signals,
    )
except ModuleNotFoundError:  # 支持直接执行 tools/momentum_signal.py
    from etf_market_data import load_etf_series
    from momentum_core import MomentumConfig, evaluate_momentum_signal, rank_momentum_signals

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

# 止损参数
STOP_LOSS_PCT = 0.08  # 绝对止损 8%

# 成交量异动阈值
VOLUME_SURGE_RATIO = 2.5


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


def calc_ma(closes, period):
    if len(closes) < period: return None
    return sum(closes[-period:]) / period


def calc_rsrs(klines, period=20):
    """
    RSRS 动量: 年化斜率 × R²（拟合优度）

    对近 period 日的 log(收盘价) 做加权线性回归:
      - 权重: 近期更高（1 → 2 线性递增）
      - 斜率: 年化处理 (×252)
      - R²: 衡量价格沿趋势线的稳定程度

    返回: (rsrs_score, slope_annualized, r_squared)
    """
    if len(klines) < period + 1: return (0, 0, 0)

    window = klines[-(period + 1):]
    log_prices = [math.log(k["close"]) for k in window if k["close"] > 0]
    if len(log_prices) < period // 2: return (0, 0, 0)

    n = len(log_prices)
    x = list(range(n))
    # 线性递增权重: 1 → 2
    weights = [1 + i / (n - 1) for i in range(n)] if n > 1 else [1] * n

    wx = sum(w * xi for w, xi in zip(weights, x))
    wy = sum(w * yi for w, yi in zip(weights, log_prices))
    wxx = sum(w * xi * xi for w, xi in zip(weights, x))
    wxy = sum(w * xi * yi for w, xi, yi in zip(weights, x, log_prices))
    w_sum = sum(weights)
    wyy = sum(w * yi * yi for w, yi in zip(weights, log_prices))

    denominator = w_sum * wxx - wx * wx
    if abs(denominator) < 1e-10: return (0, 0, 0)

    slope = (w_sum * wxy - wx * wy) / denominator

    # R²
    y_mean = wy / w_sum
    ss_total = wyy - 2 * y_mean * wy + w_sum * y_mean * y_mean
    ss_error = 0
    intercept = (wy - slope * wx) / w_sum
    for xi, yi, wi in zip(x, log_prices, weights):
        pred = slope * xi + intercept
        ss_error += wi * (yi - pred) ** 2
    r_squared = max(0, 1 - ss_error / ss_total) if ss_total > 1e-10 else 0

    # 年化斜率（交易日 ×252）
    slope_annual = slope * 252

    # RSRS 得分 = 年化斜率 × R²（截断下限=0 防负分，无上限截断，保留连续排名能力）
    # 审查修正: 原 cap=5 导致 91% 买入信号得分并列 5.0，排名退化为字典序
    # 提高上限至 50，确保不同 ETF 之间有足够的区分度
    rsrs = max(0, min(50, slope_annual * r_squared * 100))

    return (round(rsrs, 2), round(slope_annual * 100, 2), round(r_squared, 4))


def calc_atr(klines, period=14):
    """ATR 波动率（百分比）"""
    if len(klines) < period + 1: return 0
    window = klines[-(period + 1):]
    trs = []
    for i in range(1, len(window)):
        h, l, pc = window[i]["high"], window[i]["low"], window[i-1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if not trs: return 0
    atr = sum(trs) / len(trs)
    current = window[-1]["close"]
    return atr / current if current > 0 else 0


def estimate_today_volume(klines, today_minutes_passed=240):
    """
    估算今日全天成交量。
    基于交易时间进度推估: 预估量 = 当前量 × (240 / 已过分钟)
    """
    if len(klines) < 2: return None
    today = klines[-1]
    vol = today.get("volume", 0)
    if vol <= 0: return None

    # 如果已经是全天数据（前一日），直接返回
    yesterday = klines[-2]
    if today.get("date") == yesterday.get("date"): return vol

    progress = max(0.1, min(1.0, today_minutes_passed / 240))
    return vol / progress


def calc_volume_ratio(klines):
    """当日预估量 / 5日均量"""
    if len(klines) < 7: return 1.0
    # 近5个完整交易日（排除今天）
    vols = [k["volume"] for k in klines[-6:-1]]
    avg_vol = sum(vols) / 5 if vols else 1
    # 今天已成交量
    today_vol = klines[-1].get("volume", 0)
    # 简化处理: 用 14:00 左右的数据，假设已完成 90% 交易
    estimated = today_vol / 0.9 if today_vol > 0 else None
    if estimated and avg_vol > 0:
        return estimated / avg_vol
    return 1.0


def scan(pool, momentum_period=20, *, config=None, market_closed=True):
    """扫描全部 ETF；未完成的盘中 bar 只返回 provisional 结果。"""
    config = config or MomentumConfig(rsrs_period=momentum_period)
    results = []
    snapshots = []
    for code, name in pool.items():
        if code == DEFENSIVE_CODE:
            continue
        try:
            series = load_etf_series(code, count=max(300, config.warmup_days + 1))
            bars = tuple(series.bars)
        except Exception as exc:
            results.append({"code": code, "name": name, "error": str(exc), "pass": False,
                            "formal": False, "provisional": not market_closed})
            continue
        if not bars:
            results.append({"code": code, "name": name, "error": "数据不足", "pass": False,
                            "formal": False, "provisional": not market_closed})
            continue
        snapshot = evaluate_momentum_signal(code, bars, len(bars) - 1, config)
        snapshots.append(snapshot)
        metrics = dict(snapshot.metrics)
        formal = bool(metrics["formal"] and market_closed)
        passed = bool(snapshot.passed and market_closed)
        closes = [float(bar["close"]) for bar in bars]
        results.append({
            "code": code, "name": name, "date": snapshot.date,
            "close": round(metrics["close"], 4),
            "raw_rsrs_score": snapshot.raw_rsrs_score,
            "rsrs_score": metrics["display_rsrs_score"],
            "slope_annual_pct": snapshot.slope_annual_pct,
            "r_squared": snapshot.r_squared,
            "ma20": round(metrics["ma20"], 4) if metrics["ma20"] is not None else None,
            "ma60": round(metrics["ma60"], 4) if metrics["ma60"] is not None else None,
            "above_ma20": metrics["above_ma"],
            "above_ma60": metrics["ma60"] is not None and metrics["close"] > metrics["ma60"],
            "golden_cross": metrics["golden_cross"],
            "vol_20d": round(metrics["current_volatility"] * 100, 2),
            "vol_median": round(metrics["historical_volatility_median"] * 100, 2),
            "vol_ok": metrics["volatility_ok"],
            "vol_ratio": round(metrics["volume_ratio"], 2),
            "volume_surge": not metrics["volume_ok"],
            "rsi": round(metrics["rsi"], 1),
            "rsi_overbought": not metrics["rsi_ok"],
            "pct_5d": round((closes[-1] / closes[-6] - 1) * 100, 2) if len(closes) >= 6 else 0,
            "pct_20d": round((closes[-1] / closes[-21] - 1) * 100, 2) if len(closes) >= 21 else 0,
            "metrics": metrics,
            "pass": passed,
            "formal": formal,
            "provisional": not market_closed,
            "signal_strength": snapshot.signal_strength if market_closed else "none",
        })
    results.sort(key=lambda item: (-item.get("raw_rsrs_score", float("-inf")),
                                   -item.get("r_squared", float("-inf")), item["code"]))
    return results


def check_stop_loss(entry_code, entry_price, current_price=None):
    """检查持仓是否触发止损线。"""
    if current_price is None:
        klines = fetch_kline(entry_code, count=5)
        if not klines: return None
        current_price = klines[-1]["close"]

    loss_pct = (current_price - entry_price) / entry_price
    triggered = loss_pct <= -STOP_LOSS_PCT
    return {
        "entry_price": entry_price,
        "current_price": current_price,
        "loss_pct": round(loss_pct * 100, 2),
        "stop_loss_line": round(entry_price * (1 - STOP_LOSS_PCT), 4),
        "triggered": triggered,
    }


def main():
    parser = argparse.ArgumentParser(description="ETF动量轮动信号扫描 v2.0")
    parser.add_argument("--pool", default="518880,513100,159915,510300,512880,512690,512010,588000,510500,513180",
                        help="ETF池（逗号分隔）")
    parser.add_argument("--momentum", type=int, default=20, help="RSRS 计算周期（默认20日）")
    parser.add_argument("--json", action="store_true", help="JSON输出")
    parser.add_argument("--entry", nargs=2, metavar=("CODE", "PRICE"),
                        help="检查止损: --entry 159915 3.45")
    args = parser.parse_args()

    # 止损检查模式
    if args.entry:
        code, price = args.entry[0], float(args.entry[1])
        name = POOL.get(code, code)
        result = check_stop_loss(code, price)
        if result:
            print(f"  {name} ({code})")
            print(f"  入场价: {result['entry_price']:.4f}")
            print(f"  当前价: {result['current_price']:.4f}")
            print(f"  浮动盈亏: {result['loss_pct']:+.2f}%")
            print(f"  止损线 (8%): {result['stop_loss_line']:.4f}")
            status = "🔴 触发止损！" if result['triggered'] else "🟢 未触发"
            print(f"  状态: {status}")
        return

    # 信号扫描模式
    codes = [c.strip() for c in args.pool.split(",") if c.strip()]
    pool = {c: POOL.get(c, c) for c in codes}

    results = scan(pool, args.momentum)

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
        return

    # 格式化输出
    mp = args.momentum
    date_str = results[0]["date"] if results else datetime.now().strftime("%Y-%m-%d")
    print("=" * 75)
    print(f"  ETF 动量轮动 · RSRS 信号扫描 v2.0")
    print(f"  扫描日期: {date_str}  |  候选池: {len(results)} 只 ETF")
    print(f"  核心指标: RSRS = 年化斜率 × R²（趋势强度 × 趋势质量）")
    print(f"  过滤: 收盘>MA20 | 波动<历史中位×1.5 | 成交量无异常 | RSI<80")
    print("=" * 75)

    # 信号分级统计
    strong = [r for r in results if r.get("signal_strength") == "strong"]
    medium = [r for r in results if r.get("signal_strength") == "medium"]
    no_signal = [r for r in results if r.get("signal_strength") == "none"]

    # 详细输出
    for rank, r in enumerate(results, 1):
        if r.get("error"):
            print(f"\n  {rank:>2}. ❌ {r['code']} {r['name']}  ⚠️ {r['error']}")
            continue

        strength_icon = {
            "strong": "🟢", "medium": "🟡", "none": "⚪"
        }.get(r["signal_strength"], "⚪")

        print(f"\n  {rank:>2}. {strength_icon} {r['code']} {r['name']}")

        # RSRS 核心行
        print(f"     RSRS: {r['rsrs_score']:.2f}  "
              f"(斜率 {r['slope_annual_pct']:+.1f}% × R² {r['r_squared']:.3f})  |  "
              f"20日涨幅 {r['pct_20d']:+.2f}%  |  5日 {r['pct_5d']:+.2f}%")

        # 过滤条件
        ma_status = "✅" if r["above_ma20"] else "❌"
        cross = " 双多头" if r.get("golden_cross") else ""
        print(f"     MA20: {r['ma20']} {ma_status}{cross}  |  "
              f"MA60: {r['ma60']} {'✅' if r.get('above_ma60') else '—'}")

        vol_status = "✅" if r["vol_ok"] else "❌ 过热"
        print(f"     波动率: {r['vol_20d']}%  |  历史中位: {r['vol_median']}%  |  {vol_status}")

        surge_warn = f" 🔴 放量{r['vol_ratio']:.1f}倍" if r["volume_surge"] else ""
        rsi_warn = f" 🔴 RSI={r['rsi']}" if r["rsi_overbought"] else ""
        extra = (surge_warn + rsi_warn).strip()
        if extra:
            print(f"     异动: {extra}")

        if not r["pass"]:
            reasons = []
            if not r.get("above_ma20"): reasons.append("收盘<MA20")
            if not r.get("vol_ok"): reasons.append("波动率过热")
            if r.get("volume_surge"): reasons.append("成交量异常放量")
            if r.get("rsi_overbought"): reasons.append("RSI超买")
            if r.get("rsrs_score", 0) <= 0: reasons.append("RSRS动量≤0")
            print(f"     🔴 不通过: {', '.join(reasons)}")

    # ===== 操作建议 =====
    print(f"\n{'='*75}")
    print(f"  📋 操作建议")
    print(f"{'='*75}")

    pass_list = [r for r in results if r["pass"]]
    best = pass_list[0] if pass_list else None

    if best:
        name = best["name"]
        code = best["code"]
        strength = best["signal_strength"]

        action = "全仓买入"
        detail = ("双均线多头 + RSRS 高分" if strength == "strong"
                  else "站上 MA20 且满足全部五项条件")

        print(f"  🟢 买入信号: {code} {name}")
        print(f"     RSRS 得分: {best['rsrs_score']:.2f}/5  |  信号强度: {strength}")
        print(f"     当前价: {best['close']}  |  止损线: {round(best['close'] * (1 - STOP_LOSS_PCT), 4)}")
        print(f"     操作: {action}")
        print(f"     理由: {detail}")

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
                if r.get("above_ma20") and r.get("vol_ok") and r.get("rsrs_score", 0) > 0
                and (r.get("volume_surge") or r.get("rsi_overbought"))]
        near2 = [r for r in results
                 if r.get("above_ma20") and not r.get("vol_ok") and r.get("rsrs_score", 0) > 0]
        near3 = [r for r in results
                 if r.get("vol_ok") and r.get("rsrs_score", 0) > 0 and not r.get("above_ma20")]

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
