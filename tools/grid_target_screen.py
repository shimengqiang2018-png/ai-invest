#!/usr/bin/env python3
"""
网格标的科学筛选器 — 从全市场 ETF 宇宙选出「网格该配哪些标的」。

背景：网格跑输买入持有是结构性的（现金拖累 + 过早卖赢家），只在「结构性均值回归」
的震荡标的上才有正 alpha。本工具不硬编码标的，而是从数据库/全宇宙枚举候选，用
三步科学方法筛选出「网格稳健跑赢买入持有」的标的。

方法论（每个标的一视同仁，三步）：
1. 样本内回测：固定网格参数跑全历史，算网格 vs 买入持有的 α / MaxDD / Sharpe。
2. 分段稳健性（防后视）：按年切成 K 段，每段独立跑网格 vs 买入持有，
   报「网格赢的段数占比」+「分段 α 均值/波动」。只有多数段都赢才算真震荡型，
   而不是靠某一段行情撞出来的 α（样本外验证的结论：ER 等滞后信号预测不了 regime 切换）。
3. 多重比较校正：从 N 只候选里选标的，「选到运气好的标的」是真实风险。对
   「网格 vs 买入持有」的日 α 序列套 DSR（n_trials=N），输出显著性概率。

分类规则（区分「真 alpha」与「崩盘少亏」——两者都表现为 α>0，但性质不同）：
- 适合：网格正收益 + 跑赢 B&H + 多数段（>=60%）网格赢。真震荡收割。
- 边缘：网格正收益 + 跑赢 B&H，但赢面不连续（可能靠某段行情）。
- 缓冲：网格仍亏（grid_annual <= 0），只是比 B&H 少亏。下行缓冲价值，非正 alpha。
- 不适合：网格跑输 B&H（α < 0，趋势型标的，网格必输）。

输出：排名表 + data/grid_target_screen.json。

用法:
    python3 tools/grid_target_screen.py --only-cached          # 只用离线缓存（快，无网络）
    python3 tools/grid_target_screen.py --limit 50             # 只扫前 50 只
    python3 tools/grid_target_screen.py --codes 512880,512660  # 指定候选
    python3 tools/grid_target_screen.py --resume               # 断点续跑（跳过已算的）
    python3 tools/grid_target_screen.py --optimize             # 对入选标的做参数寻优
"""

import argparse
import json
import math
import os
import sys
import time
from datetime import date

try:
    from tools.etf_market_data import load_etf_series
    from tools.grid_trading import run_grid_backtest, _is_t0
    from tools.trading_ledger import ExecutionConfig
    from tools.multiple_testing import deflated_sharpe_ratio, significance_label, return_stats
except ModuleNotFoundError:
    from etf_market_data import load_etf_series
    from grid_trading import run_grid_backtest, _is_t0
    from trading_ledger import ExecutionConfig
    from multiple_testing import deflated_sharpe_ratio, significance_label, return_stats


DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
META_FILE = os.path.join(DATA_DIR, "etf_meta.json")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
OUTPUT_FILE = os.path.join(DATA_DIR, "grid_target_screen.json")

# 网格不适合做趋势/现金/杠杆类标的：货币基金无波动，债券低波动，杠杆有漂移+损耗
EXCLUDE_CATEGORIES = {"货币基金", "债券ETF", "杠杆ETF"}
MIN_LISTING_BARS = 500     # 至少约 2 年交易数据，够跑分段回测
MIN_FUND_SIZE = 1e8        # 至少 1 亿规模（元），保证流动性
SEGMENT_BARS = 252         # 分段稳健性：按约 1 年（252 交易日）切段
MIN_SEGMENT_BARS = 60      # 单段过短则丢弃

# 固定网格参数（全候选一致，避免「参数寻优」本身成为又一重多重比较）
DEFAULT_PARAMS = dict(
    spacing_up_pct=3.0,
    spacing_down_pct=3.0,
    levels_above=5,
    levels_below=5,
    shares_per_grid=1000,
    total_capital=100000.0,
    stop_loss_ratio=0,      # 关止损，隔离变量、用完整窗口
)


def _load_meta():
    with open(META_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _filter_candidates(meta, codes_set, min_size):
    """按分类/规模/指定池过滤候选。返回 [etf, ...] 与跳过统计。"""
    candidates = []
    skip: dict[str, int] = {}
    for etf in meta.get("etfs", []):
        code = str(etf.get("code", ""))
        if not code:
            continue
        if codes_set is not None and code not in codes_set:
            continue
        cat = etf.get("category", "?")
        if cat in EXCLUDE_CATEGORIES:
            skip[cat] = skip.get(cat, 0) + 1
            continue
        if etf.get("fund_size") and etf["fund_size"] < min_size:
            skip["too_small"] = skip.get("too_small", 0) + 1
            continue
        candidates.append(etf)
    return candidates, skip


def _to_ohlc_lists(bars):
    """series.bars -> (closes, dates, opens, highs, lows)。"""
    closes = [b["close"] for b in bars]
    dates = [b["date"] for b in bars]
    opens = [b["open"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    return closes, dates, opens, highs, lows


def _run_backtest(code, closes, dates, opens, highs, lows, params):
    """固定参数跑一次网格回测，返回 run_grid_backtest 结果 dict。"""
    t_plus = 0 if _is_t0(code) else 1
    kwargs = dict(params)
    kwargs.update(execution=ExecutionConfig(), t_plus=t_plus)
    return run_grid_backtest(
        closes, dates, opens=opens, highs=highs, lows=lows, **kwargs
    )


def _daily_alpha_series(result):
    """全程「网格日收益 − 买入持有日收益」序列，用于 DSR 多重比较。"""
    eq = result["equity_curve"]
    bh_shares = result["bh_shares"]
    bh_cash = result["bh_remaining_cash"]
    alpha = []
    for i in range(1, len(eq)):
        g_prev, g_cur = eq[i - 1]["equity"], eq[i]["equity"]
        b_prev = bh_shares * eq[i - 1]["close"] + bh_cash
        b_cur = bh_shares * eq[i]["close"] + bh_cash
        if g_prev > 0 and b_prev > 0:
            alpha.append((g_cur / g_prev - 1) - (b_cur / b_prev - 1))
    return alpha


def _segmented_alpha(code, closes, dates, opens, highs, lows, params):
    """按年切段，每段独立跑网格 vs 买入持有，返回 (win_rate, seg_alpha, mean, std)。"""
    n = len(closes)
    seg_alpha = []
    start = 0
    while start < n:
        end = min(start + SEGMENT_BARS, n)
        if end - start < MIN_SEGMENT_BARS:
            break
        seg_c = closes[start:end]
        seg_d = dates[start:end]
        seg_o = opens[start:end]
        seg_h = highs[start:end]
        seg_l = lows[start:end]
        try:
            r = _run_backtest(code, seg_c, seg_d, seg_o, seg_h, seg_l, params)
        except Exception:
            start = end
            continue
        seg_alpha.append(r["grid_return_pct"] - r["bh_return_pct"])
        start = end

    if not seg_alpha:
        return 0.0, [], 0.0, 0.0
    wins = sum(1 for a in seg_alpha if a > 0)
    mean = sum(seg_alpha) / len(seg_alpha)
    var = sum((a - mean) ** 2 for a in seg_alpha) / max(len(seg_alpha) - 1, 1)
    return wins / len(seg_alpha), seg_alpha, mean, math.sqrt(var)


def _classify(alpha_pct, grid_annual_pct, win_rate):
    """区分真 alpha / 崩盘少亏 / 跑输。"""
    if alpha_pct < 0:
        return "不适合"
    if grid_annual_pct <= 0:
        return "缓冲"      # 网格仍亏，只是比 B&H 少亏
    if win_rate >= 0.6:
        return "适合"
    return "边缘"


def _evaluate_target(code, name, category, bars, params, n_trials):
    """单个标的：全历史回测 + 分段稳健性 + DSR，返回结果 dict 或 None。"""
    closes, dates, opens, highs, lows = _to_ohlc_lists(bars)
    full = _run_backtest(code, closes, dates, opens, highs, lows, params)

    win_rate, seg_alpha, seg_mean, seg_std = _segmented_alpha(
        code, closes, dates, opens, highs, lows, params
    )

    # DSR：对日 α 序列做多重比较校正
    alpha_series = _daily_alpha_series(full)
    dsr_prob = None
    sig_label = "数据不足"
    if len(alpha_series) >= 4:
        stats = return_stats(alpha_series)
        if stats["sr"] is not None:
            dsr = deflated_sharpe_ratio(
                stats["sr"], n_trials, stats["skew"], stats["kurt"], stats["n"]
            )
            dsr_prob = round(dsr["prob"], 3)
            sig_label = significance_label(dsr_prob)

    alpha_pct = full["alpha_pct"]
    return {
        "code": code,
        "name": name,
        "category": category,
        "bars_count": len(bars),
        "grid_annual_pct": round(full["grid_annual_pct"], 2),
        "bh_annual_pct": round(full["bh_annual_pct"], 2),
        "alpha_pct": round(alpha_pct, 2),
        "grid_total_pct": round(full["grid_return_pct"], 2),
        "max_dd_pct": round(full["max_dd"] * 100, 2),
        "sharpe": round(full["sharpe"], 2),
        "n_segments": len(seg_alpha),
        "win_rate": round(win_rate, 3),
        "seg_alpha_mean": round(seg_mean, 2),
        "seg_alpha_std": round(seg_std, 2),
        "dsr_prob": dsr_prob,
        "significance": sig_label,
        "classification": _classify(alpha_pct, full["grid_annual_pct"], win_rate),
    }


def _print_table(results, top_n=30):
    header = (
        f"{'排名':<5} {'代码':<8} {'名称':<14} {'分类':<6} "
        f"{'网格年化':>9} {'B&H年化':>9} {'α':>8} {'MaxDD':>7} "
        f"{'胜段':>6} {'DSR':>6} {'判定':<6}"
    )
    print(f"\n{'─' * len(header)}")
    print(header)
    print(f"{'─' * len(header)}")
    for rank, r in enumerate(results[:top_n], 1):
        win = f"{r['win_rate']*100:.0f}%" if r["win_rate"] else "—"
        dsr = f"{r['dsr_prob']:.2f}" if r["dsr_prob"] is not None else "—"
        print(
            f"{rank:<5} {r['code']:<8} {(r['name'] or '')[:14]:<14} "
            f"{(r['category'] or '')[:6]:<6} "
            f"{r['grid_annual_pct']:>+8.1f}% {r['bh_annual_pct']:>+8.1f}% "
            f"{r['alpha_pct']:>+7.1f}% {r['max_dd_pct']:>6.1f}% "
            f"{win:>6} {dsr:>6} {r['classification']:<6}"
        )
    print(f"{'─' * len(header)}")


def main():
    parser = argparse.ArgumentParser(description="网格标的科学筛选器")
    parser.add_argument("--codes", help="逗号分隔 ETF 代码（限定扫描范围）")
    parser.add_argument("--min-bars", type=int, default=MIN_LISTING_BARS,
                        help=f"最少 K 线天数（默认 {MIN_LISTING_BARS}）")
    parser.add_argument("--min-size", type=float, default=MIN_FUND_SIZE,
                        help=f"最小基金规模（元，默认 {MIN_FUND_SIZE:.0e} = 1亿）")
    parser.add_argument("--top", type=int, default=30, help="展示前 N 名")
    parser.add_argument("--limit", type=int, default=0, help="只扫前 N 只候选（0=全部）")
    parser.add_argument("--only-cached", action="store_true", help="只用离线缓存，不联网")
    parser.add_argument("--resume", action="store_true", help="从已有结果断点续跑")
    parser.add_argument("--output", default=OUTPUT_FILE, help="输出 JSON 路径")
    args = parser.parse_args()

    if args.only_cached:
        os.environ["ETF_DATA_OFFLINE"] = "1"

    codes_set = None
    if args.codes:
        codes_set = {c.strip() for c in args.codes.replace("，", ",").split(",") if c.strip()}

    if not os.path.exists(META_FILE):
        print(f"❌ 未找到 {META_FILE}，请先运行 tools/fetch_etf_list.py")
        sys.exit(1)

    meta = _load_meta()
    candidates, skip = _filter_candidates(meta, codes_set, args.min_size)
    if args.limit > 0:
        candidates = candidates[: args.limit]

    print("=" * 60)
    print("网格标的科学筛选器")
    print("=" * 60)
    print(f"📋 元信息: {meta.get('total')} 只 ETF，过滤后候选 {len(candidates)} 只")
    if skip:
        print(f"  跳过: {skip}")

    # 断点续跑
    existing: dict[str, dict] = {}
    if args.resume and os.path.exists(args.output):
        try:
            with open(args.output, "r", encoding="utf-8") as f:
                prev = json.load(f)
            for r in prev.get("results", []):
                existing[r["code"]] = r
            print(f"📂 已有结果: {len(existing)} 只")
        except (json.JSONDecodeError, KeyError):
            pass

    results: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    tested = 0
    t0 = time.time()

    for i, etf in enumerate(candidates):
        code = str(etf.get("code", ""))
        name = etf.get("name", "")
        cat = etf.get("category", "?")

        if code in existing:
            results.append(existing[code])
            continue

        print(f"\r[{i + 1}/{len(candidates)}] {code} {(name or '')[:16]:<16} ...",
              end="", flush=True)

        try:
            series = load_etf_series(code, count=2000, adjustment="qfq", cache_dir=CACHE_DIR)
            bars = list(series.bars)
        except Exception as e:
            skipped.append({"code": code, "name": name, "reason": f"no_data: {str(e)[:60]}"})
            continue

        if len(bars) < args.min_bars:
            skipped.append({"code": code, "name": name, "reason": f"too_few_bars({len(bars)})"})
            continue

        try:
            r = _evaluate_target(code, name, cat, bars, DEFAULT_PARAMS, n_trials=max(1, len(candidates)))
        except Exception as e:
            errors.append({"code": code, "name": name, "error": str(e)[:120]})
            continue

        results.append(r)
        tested += 1

    print(f"\r{' ' * 60}")  # 清进度行
    elapsed = time.time() - t0

    # 按 α 降序排名
    results.sort(key=lambda r: r["alpha_pct"], reverse=True)

    _print_table(results, args.top)

    summary = {
        "generated_at": date.today().isoformat(),
        "n_candidates": len(candidates),
        "n_tested": len(results),
        "n_skipped": len(skipped),
        "n_errors": len(errors),
        "skip_reasons": {k: v for k, v in skip.items()} or None,
        "params": DEFAULT_PARAMS,
        "elapsed_sec": round(elapsed, 1),
        "results": results,
        "skipped": skipped,
        "errors": errors,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    suitable = [r for r in results if r["classification"] == "适合"]
    buffer = [r for r in results if r["classification"] == "缓冲"]
    print(f"\n✅ 共测试 {len(results)} 只（用时 {elapsed:.0f}s），"
          f"适合 {len(suitable)} / 缓冲 {len(buffer)} / 跳过 {len(skipped)} / 错误 {len(errors)}")
    if suitable:
        print("   适合（真震荡收割）:", ", ".join(f"{r['name']}({r['code']})" for r in suitable))
    if buffer:
        print("   缓冲（崩盘减伤）:", ", ".join(f"{r['name']}({r['code']})" for r in buffer))
    print(f"📄 结果已写入 {args.output}")


if __name__ == "__main__":
    main()
