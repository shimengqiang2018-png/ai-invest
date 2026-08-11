#!/usr/bin/env python3
"""全市场 ETF 动量回测扫描工具。

遍历全市场 ETF，逐个运行 RSRS 动量信号评估，按综合评分排序，
筛选出最适合动量轮动策略的标的。

用法:
    python3 tools/etf_full_backtest.py                          # 全量扫描
    python3 tools/etf_full_backtest.py --pool 513100,159915     # 指定标的
    python3 tools/etf_full_backtest.py --top 50                 # 仅展示 Top 50
    python3 tools/etf_full_backtest.py --category A股宽基        # 按分类筛选
    python3 tools/etf_full_backtest.py --min-days 500            # 最少K线天数
    python3 tools/etf_full_backtest.py --resume                  # 断点续跑

依赖:
    - tools/etf_market_data.py (K线数据，已缓存则不复读)
    - data/etf_meta.json (ETF元信息)

输出:
    - data/etf_backtest_results.json (全量结果)
    - 终端表格展示 Top N
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime

try:
    from tools.etf_market_data import MarketDataSeries, load_etf_series
    from tools.momentum_core import MomentumConfig
except ModuleNotFoundError:
    from etf_market_data import MarketDataSeries, load_etf_series
    from momentum_core import MomentumConfig

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
META_FILE = os.path.join(DATA_DIR, "etf_meta.json")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
OUTPUT_FILE = os.path.join(DATA_DIR, "etf_backtest_results.json")

# ── 默认过滤条件 ──
EXCLUDE_CATEGORIES = {"货币基金", "债券ETF", "杠杆ETF"}
MIN_LISTING_DAYS = 250  # 至少 1 年交易数据
MIN_FUND_SIZE = 1e8     # 至少 1 亿规模（元）
DEFAULT_MOMENTUM_PERIOD = 40  # RSRS 计算周期

# ── 评分权重 ──
WEIGHTS = {
    "signal_quality": 0.30,   # 信号胜率 × 平均收益
    "trend_strength": 0.25,   # 年化收益
    "risk_control": 0.25,     # 夏普比率
    "liquidity": 0.10,        # 规模 + 换手率
    "stability": 0.10,        # 数据天数 + 信号频率
}


def _load_meta() -> dict:
    """加载 ETF 元信息。"""
    with open(META_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _filter_candidates(meta: dict, args) -> list[dict]:
    """按条件过滤候选 ETF。"""
    candidates = []
    skip_reasons: dict[str, int] = {}

    for etf in meta["etfs"]:
        code = etf["code"]
        name = etf["name"]
        cat = etf["category"]

        # 指定 pool 模式
        if args.pool and code not in args.pool:
            continue

        # 分类过滤
        if args.category and cat != args.category:
            continue
        if not args.category and cat in EXCLUDE_CATEGORIES:
            skip_reasons[cat] = skip_reasons.get(cat, 0) + 1
            continue

        # 规模过滤
        if etf.get("fund_size") and etf["fund_size"] < args.min_size:
            skip_reasons["too_small"] = skip_reasons.get("too_small", 0) + 1
            continue

        candidates.append(etf)

    if skip_reasons:
        print(f"  跳过原因: {skip_reasons}")

    return candidates


def _compute_annual_return(bars: list[dict]) -> float:
    """计算年化收益率。"""
    if len(bars) < 2:
        return 0.0
    start_price = bars[0]["close"]
    end_price = bars[-1]["close"]
    if start_price <= 0:
        return 0.0
    total_return = end_price / start_price - 1
    years = len(bars) / 252
    if years < 0.01:
        return 0.0
    return (1 + total_return) ** (1 / years) - 1


def _compute_max_drawdown(bars: list[dict]) -> float:
    """计算最大回撤。"""
    peak = bars[0]["close"]
    max_dd = 0.0
    for bar in bars:
        close = bar["close"]
        if close > peak:
            peak = close
        dd = (peak - close) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _compute_sharpe(bars: list[dict]) -> float:
    """计算年化夏普比率（无风险利率假设为0）。"""
    if len(bars) < 2:
        return 0.0
    daily_returns = []
    for i in range(1, len(bars)):
        prev = bars[i - 1]["close"]
        curr = bars[i]["close"]
        if prev > 0:
            daily_returns.append(curr / prev - 1)
    if len(daily_returns) < 20:
        return 0.0
    mean = sum(daily_returns) / len(daily_returns)
    var = sum((r - mean) ** 2 for r in daily_returns) / len(daily_returns)
    if var <= 0:
        return 0.0
    std = math.sqrt(var)
    return (mean / std) * math.sqrt(252)


def _compute_volatility(bars: list[dict], window: int = 20) -> float:
    """计算近20日年化波动率。"""
    if len(bars) < window + 1:
        return 0.0
    recent = bars[-window:]
    daily_returns = []
    for i in range(1, len(recent)):
        prev = recent[i - 1]["close"]
        curr = recent[i]["close"]
        if prev > 0:
            daily_returns.append(curr / prev - 1)
    if len(daily_returns) < 5:
        return 0.0
    mean = sum(daily_returns) / len(daily_returns)
    var = sum((r - mean) ** 2 for r in daily_returns) / len(daily_returns)
    return math.sqrt(var) * math.sqrt(252) if var > 0 else 0.0


def _fast_breakout_signals(bars: list[dict], period: int = 40) -> list[dict]:
    """快速突破信号扫描——O(n) 滑动窗口，不计算 RSRS。

    信号条件:
        1. 收盘 > 近 period 日最高价（突破）
        2. 近5日均量 > 近20日均量（放量确认）
        3. 收盘 > 近 period 日均价（趋势确认）

    Returns:
        [{idx, date, close, strength}, ...]
        strength = (close - max_high) / max_high 归一化突破强度
    """
    n = len(bars)
    if n < period + 30:
        return []

    # 预取数据为简单列表（避免 dict 查找开销）
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    volumes = [b.get("volume", 0) or 0 for b in bars]

    results = []

    # 滑动窗口追踪
    for i in range(period, n):
        close = closes[i]
        if close <= 0:
            continue

        # 条件1：突破 period 日最高价
        max_high = max(highs[i - period:i])
        if close <= max_high:
            continue

        # 条件2：放量（近5日均量 > 近20日均量 × 0.8）
        vol_5 = sum(volumes[i - 4:i + 1]) / 5
        vol_20 = sum(volumes[i - 19:i + 1]) / 20 if i >= 19 else vol_5
        if vol_20 > 0 and vol_5 < vol_20 * 0.8:
            continue

        # 条件3：趋势确认（收盘 > 近 period 日均价）
        ma = sum(closes[i - period:i]) / period
        if close <= ma:
            continue

        # 突破强度
        strength = (close - max_high) / max_high

        results.append({
            "idx": i,
            "date": bars[i]["date"],
            "close": close,
            "strength": round(strength, 4),
        })

    return results


def _evaluate_etf(code: str, name: str, bars: list[dict]) -> dict | None:
    """快速 ETF 动量信号评估。

    使用 O(n) 滑动窗口突破检测替代 RSRS 计算，10x+ 提速。
    """
    if len(bars) < DEFAULT_MOMENTUM_PERIOD + 30:
        return None

    # 快速突破信号扫描
    raw_signals = _fast_breakout_signals(bars, DEFAULT_MOMENTUM_PERIOD)

    # 月度去重
    seen_months = set()
    signals = []
    for sig in raw_signals:
        month = sig["date"][:7]
        if month not in seen_months:
            seen_months.add(month)
            signals.append(sig)

    # 信号后 N 日收益
    fwd_10d = []
    fwd_20d = []
    for sig in signals:
        idx = sig["idx"]
        close = sig["close"]
        if idx + 10 < len(bars):
            fwd_10d.append(bars[idx + 10]["close"] / close - 1)
        if idx + 20 < len(bars):
            fwd_20d.append(bars[idx + 20]["close"] / close - 1)

    # 基础指标
    annual_return = _compute_annual_return(bars)
    max_dd = _compute_max_drawdown(bars)
    sharpe = _compute_sharpe(bars)
    volatility = _compute_volatility(bars)

    years = len(bars) / 252
    sig_count = len(signals)
    signal_freq = sig_count / years if years > 0 else 0

    # 信号指标
    avg_fwd_10d = sum(fwd_10d) / len(fwd_10d) if fwd_10d else 0
    avg_fwd_20d = sum(fwd_20d) / len(fwd_20d) if fwd_20d else 0
    win_rate_10d = sum(1 for r in fwd_10d if r > 0) / len(fwd_10d) if fwd_10d else 0
    win_rate_20d = sum(1 for r in fwd_20d if r > 0) / len(fwd_20d) if fwd_20d else 0

    # ── 综合评分（0-100）──
    signal_quality = (win_rate_20d * 0.5 + max(0, avg_fwd_20d * 10 + 0.5) * 0.5) * 100
    signal_quality = max(0, min(100, signal_quality))

    trend_score = max(0, min(100, (annual_return + 0.3) / 0.5 * 100))
    risk_score = max(0, min(100, (sharpe + 0.5) / 2.0 * 100))
    liquidity_score = max(0, min(100, signal_freq * 25))
    stability_score = max(0, min(100, len(bars) / 2000 * 100))

    composite = (
        signal_quality * WEIGHTS["signal_quality"]
        + trend_score * WEIGHTS["trend_strength"]
        + risk_score * WEIGHTS["risk_control"]
        + liquidity_score * WEIGHTS["liquidity"]
        + stability_score * WEIGHTS["stability"]
    )

    return {
        "code": code,
        "name": name,
        "listing_date": bars[0]["date"],
        "last_date": bars[-1]["date"],
        "bars_count": len(bars),
        "years": round(years, 1),
        "signal_count": sig_count,
        "signal_freq_per_year": round(signal_freq, 1),
        "annual_return": round(annual_return, 4),
        "max_drawdown": round(max_dd, 4),
        "sharpe_ratio": round(sharpe, 4),
        "volatility": round(volatility, 4),
        "avg_forward_return_10d": round(avg_fwd_10d, 4),
        "avg_forward_return_20d": round(avg_fwd_20d, 4),
        "signal_win_rate_10d": round(win_rate_10d, 4),
        "signal_win_rate_20d": round(win_rate_20d, 4),
        "breakout_strength_mean": round(
            sum(s["strength"] for s in signals) / len(signals), 4
        ) if signals else 0,
        "composite_score": round(composite, 2),
    }


def _print_table(results: list[dict], top_n: int = 30):
    """打印排名表格。"""
    header = (
        f"{'排名':<5} {'代码':<8} {'名称':<18} {'分类':<8} "
        f"{'得分':<7} {'年化':<8} {'回撤':<8} {'夏普':<7} "
        f"{'信号/年':<8} {'胜率':<7} {'天数':<6} {'上市':<12}"
    )
    print(f"\n{'─' * len(header)}")
    print(header)
    print(f"{'─' * len(header)}")

    for rank, r in enumerate(results[:top_n], 1):
        code = r["code"]
        name = r.get("name", "")[:16]
        cat = r.get("category", "")[:6]
        score = r["composite_score"]
        ann = f"{r['annual_return'] * 100:.1f}%"
        dd = f"{r['max_drawdown'] * 100:.1f}%"
        sharpe = f"{r['sharpe_ratio']:.2f}"
        freq = f"{r['signal_freq_per_year']:.1f}"
        wr = f"{r['signal_win_rate_20d'] * 100:.0f}%"
        days = r["bars_count"]
        listing = r.get("listing_date", "")[:10]

        print(
            f"{rank:<5} {code:<8} {name:<18} {cat:<8} "
            f"{score:<7.1f} {ann:<8} {dd:<8} {sharpe:<7} "
            f"{freq:<8} {wr:<8} {days:<6} {listing:<12}"
        )
    print(f"{'─' * len(header)}")


def main():
    parser = argparse.ArgumentParser(description="全市场 ETF 动量回测扫描")
    parser.add_argument("--pool", help="逗号分隔的 ETF 代码列表（限定扫描范围）")
    parser.add_argument("--category", help="ETF 分类筛选（如 A股宽基, 跨境ETF）")
    parser.add_argument("--min-days", type=int, default=MIN_LISTING_DAYS,
                        help=f"最少K线天数（默认 {MIN_LISTING_DAYS}）")
    parser.add_argument("--min-size", type=float, default=MIN_FUND_SIZE,
                        help=f"最小基金规模（元，默认 {MIN_FUND_SIZE:.0e} = 1亿）")
    parser.add_argument("--top", type=int, default=30, help="展示前 N 名（默认 30）")
    parser.add_argument("--resume", action="store_true", help="从已有结果断点续跑")
    parser.add_argument("--no-cache-fetch", action="store_true",
                        help="不拉取新K线数据，仅使用已有缓存")
    parser.add_argument("--output", default=OUTPUT_FILE, help=f"输出文件路径")
    args_raw = parser.parse_args()

    # 解析 pool
    pool_set = None
    if args_raw.pool:
        pool_set = set(args_raw.pool.replace("，", ",").split(","))

    # 把 pool_set 挂到 args 上简化传递
    args_raw.pool = pool_set

    print("=" * 60)
    print("全市场 ETF RSRS 动量信号扫描")
    print("=" * 60)

    # ── 加载元信息 ──
    if not os.path.exists(META_FILE):
        print(f"❌ 未找到 {META_FILE}，请先运行 fetch_etf_list.py")
        sys.exit(1)

    meta = _load_meta()
    print(f"📋 加载元信息: {meta['total']} 只 ETF ({meta.get('data_source', '?')})")

    # ── 过滤候选 ──
    candidates = _filter_candidates(meta, args_raw)
    print(f"🎯 候选 ETF: {len(candidates)} 只")

    # ── 恢复已有结果 ──
    existing_results: dict[str, dict] = {}
    if args_raw.resume and os.path.exists(args_raw.output):
        try:
            with open(args_raw.output, "r", encoding="utf-8") as f:
                prev = json.load(f)
            for r in prev.get("results", []):
                existing_results[r["code"]] = r
            print(f"📂 已有结果: {len(existing_results)} 只")
        except (json.JSONDecodeError, KeyError):
            pass

    # ── 逐个扫描 ──
    results = []
    skipped = []
    errors = []
    total = len(candidates)
    start_time = time.time()

    for i, etf in enumerate(candidates):
        code = etf["code"]
        name = etf["name"]
        cat = etf.get("category", "?")

        # 断点续跑
        if code in existing_results:
            results.append(existing_results[code])
            continue

        progress = f"[{i + 1}/{total}]"
        print(f"\r{progress} {code} {name[:20]:<20s} ...", end="", flush=True)

        try:
            # 获取 K 线数据
            if args_raw.no_cache_fetch:
                # 仅检查缓存
                from pathlib import Path
                cache_path = Path(CACHE_DIR) / f"etf_v2_sina_{code}_qfq_2000.json"
                if not cache_path.exists():
                    skipped.append({"code": code, "name": name, "reason": "no_cache"})
                    continue

            series = load_etf_series(code, count=2000, adjustment="qfq", cache_dir=CACHE_DIR)
            bars = list(series.bars)

            if len(bars) < args_raw.min_days:
                skipped.append({"code": code, "name": name, "reason": f"too_few_bars({len(bars)})"})
                continue

            # 评估
            result = _evaluate_etf(code, name, bars)
            if result is None:
                skipped.append({"code": code, "name": name, "reason": "insufficient_data"})
                continue

            # 补充元信息
            result["market"] = etf.get("market", "?")
            result["category"] = etf.get("category", "?")
            result["subcategory"] = etf.get("subcategory", "?")
            result["fund_size"] = etf.get("fund_size")

            results.append(result)

        except Exception as e:
            errors.append({"code": code, "name": name, "error": str(e)[:100]})

    elapsed = time.time() - start_time
    print(f"\r{' ' * 60}")  # 清掉进度行

    # ── 排序 ──
    results.sort(key=lambda r: r["composite_score"], reverse=True)

    # ── 输出 ──
    print(f"\n⏱️  耗时: {elapsed:.0f}s  |  成功: {len(results)}  |  跳过: {len(skipped)}  |  错误: {len(errors)}")

    # 打印排名
    _print_table(results, args_raw.top)

    # 写文件
    output_data = {
        "generated_at": datetime.now().isoformat(),
        "total_candidates": total,
        "total_tested": len(results),
        "total_skipped": len(skipped),
        "total_errors": len(errors),
        "config": {
            "min_days": args_raw.min_days,
            "min_size": args_raw.min_size,
            "momentum_period": DEFAULT_MOMENTUM_PERIOD,
            "weights": WEIGHTS,
        },
        "results": results,
        "skipped": skipped[:50],  # 只保留前50条跳过记录
        "errors": errors[:50],
    }

    with open(args_raw.output, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False)

    print(f"\n✅ 结果已保存到 {args_raw.output}")

    # 分类统计
    cat_top: dict[str, list] = {}
    for r in results[:50]:
        c = r.get("category", "?")
        cat_top.setdefault(c, []).append(r["code"])

    print("\n📊 Top 50 分类分布:")
    for cat in sorted(cat_top, key=lambda c: len(cat_top[c]), reverse=True):
        codes = " ".join(cat_top[cat][:5])
        print(f"   {cat}: {len(cat_top[cat])} 只   {codes}...")

    # 展示错误（如果有）
    if errors:
        print(f"\n⚠️ 错误 ({len(errors)}):")
        for e in errors[:10]:
            print(f"   {e['code']} {e['name']}: {e['error']}")


if __name__ == "__main__":
    main()
