#!/usr/bin/env python3
"""
RSRS 动量策略 — 量化审计补充工具
补齐审计报告指出的三个短板: IC/IR 因子验证、定量压力测试、日频风险指标

用法:
    python3 tools/strategy_audit.py
    python3 tools/strategy_audit.py --json
"""
import json, math, os, sys, time
from datetime import datetime, timedelta
from collections import defaultdict

try:
    from tools.momentum_etf_backtest import calc_rsrs, run_backtest
except ModuleNotFoundError:  # 支持直接执行 tools/strategy_audit.py
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from momentum_etf_backtest import calc_rsrs, run_backtest

POOL = {"518880": "黄金ETF", "513100": "纳指ETF", "159915": "创业板ETF", "159920": "恒生ETF"}
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "cache")


def spearman_rank_ic(scores, returns, min_samples=10):
    """计算 Spearman rank IC。并列值使用平均秩（average rank）。

    返回 None 如果样本不足或秩无方差。
    """
    n = len(scores)
    if n < min_samples:
        return None

    # 为数据分配平均秩
    def _rank_data(data):
        """1-indexed 平均秩分配。并列值取平均秩。"""
        indexed = sorted(enumerate(data), key=lambda x: x[1])
        ranks = [0.0] * len(data)
        i = 0
        while i < len(indexed):
            j = i
            while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
                j += 1
            avg_rank = (i + 1 + j + 1) / 2.0  # 1-indexed 平均秩
            for k in range(i, j + 1):
                ranks[indexed[k][0]] = avg_rank
            i = j + 1
        return ranks

    s_ranks = _rank_data(scores)
    r_ranks = _rank_data(returns)

    mean_s = sum(s_ranks) / n
    mean_r = sum(r_ranks) / n

    cov = sum((s_ranks[i] - mean_s) * (r_ranks[i] - mean_r) for i in range(n)) / n
    std_s = (sum((x - mean_s) ** 2 for x in s_ranks) / n) ** 0.5
    std_r = (sum((x - mean_r) ** 2 for x in r_ranks) / n) ** 0.5

    if std_s == 0 or std_r == 0:
        return None
    return cov / (std_s * std_r)


def compute_ir(ic_series, min_periods=3):
    """IR = mean(IC_t) / sample_std(IC_t)。需要至少 min_periods 期。"""
    n = len(ic_series)
    if n < min_periods:
        return None
    mean_ic = sum(ic_series) / n
    var_ic = sum((ic - mean_ic) ** 2 for ic in ic_series) / (n - 1)
    std_ic = var_ic ** 0.5
    if std_ic == 0:
        return None
    return mean_ic / std_ic


def compute_daily_metrics(navs):
    """从日频 NAV 计算全部风险指标。"""
    if len(navs) < 5:
        return {}

    values = [n[1] for n in navs]
    returns = []
    for i in range(1, len(values)):
        if values[i-1] > 0:
            returns.append((values[i] - values[i-1]) / values[i-1])

    n = len(returns)
    if n < 3:
        return {}

    # 基础统计
    mean_r = sum(returns) / n
    var_r = sum((r - mean_r)**2 for r in returns) / (n - 1)
    std_r = var_r ** 0.5
    annual_vol = std_r * math.sqrt(252) * 100
    annual_ret = ((values[-1] / values[0]) ** (252 / n) - 1) * 100

    # Sharpe (日频, 假设无风险利率 2.5%)
    rf_daily = 0.025 / 252
    sharpe_daily = (mean_r - rf_daily) / std_r * math.sqrt(252) if std_r > 0 else 0

    # MaxDD
    peak = values[0]
    max_dd = 0
    max_dd_start = navs[0][0]
    max_dd_end = navs[0][0]
    dd_start = navs[0][0]
    for i, v in enumerate(values):
        if v > peak:
            peak = v
            dd_start = navs[i][0]
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd
            max_dd_start = dd_start
            max_dd_end = navs[i][0]

    # VaR / CVaR
    sorted_rets = sorted(returns)
    var_95_idx = int(n * 0.05)
    var_95 = sorted_rets[var_95_idx] * 100 if var_95_idx < n else 0
    var_99_idx = int(n * 0.01)
    var_99 = sorted_rets[var_99_idx] * 100 if var_99_idx < n else 0
    cvar_95 = sum(r for r in sorted_rets[:var_95_idx]) / var_95_idx * 100 if var_95_idx > 0 else 0

    # 偏度 / 峰度
    if std_r > 0:
        skew = (sum((r - mean_r)**3 for r in returns) / n) / (std_r ** 3)
        kurt = (sum((r - mean_r)**4 for r in returns) / n) / (std_r ** 4) - 3
    else:
        skew = kurt = 0

    # Calmar
    calmar = annual_ret / max_dd if max_dd > 0 else 0

    # Sortino (下行偏差)
    downside = [r for r in returns if r < 0]
    if len(downside) > 2:
        down_std = (sum((r - mean_r)**2 for r in downside) / (len(downside) - 1)) ** 0.5
        sortino = (annual_ret - 2.5) / (down_std * math.sqrt(252) * 100) if down_std > 0 else 0
    else:
        sortino = 0
        down_std = 0

    # 盈亏比
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    avg_win = sum(wins) / len(wins) * 100 if wins else 0
    avg_loss = sum(losses) / len(losses) * 100 if losses else 0
    win_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    win_rate = len(wins) / n * 100

    return {
        "count": n,
        "total_return_pct": round((values[-1] / values[0] - 1) * 100, 2),
        "annual_return_pct": round(annual_ret, 2),
        "annual_vol_pct": round(annual_vol, 2),
        "sharpe": round(sharpe_daily, 2),
        "sortino": round(sortino, 2),
        "calmar": round(calmar, 2),
        "max_dd_pct": round(max_dd, 2),
        "max_dd_start": max_dd_start,
        "max_dd_end": max_dd_end,
        "var_95_daily_pct": round(var_95, 2),
        "var_99_daily_pct": round(var_99, 2),
        "cvar_95_daily_pct": round(cvar_95, 2),
        "skewness": round(skew, 3),
        "kurtosis": round(kurt, 3),
        "win_rate_pct": round(win_rate, 1),
        "win_loss_ratio": round(win_loss_ratio, 2),
        "avg_daily_ret_pct": round(mean_r * 100, 4),
    }


def compute_ic_ir(klines, pool, period=20):
    """在统一共同交易日历上计算非重叠横截面 IC 时间序列。"""
    horizons = (10, 20, 40)
    min_assets = 3
    eligible = {
        code: bars for code, bars in klines.items()
        if len(bars) >= period + max(horizons) + 1
    }
    results = {}
    if len(eligible) < min_assets:
        common_dates = []
    else:
        common_dates = sorted(set.intersection(*(
            {bar["date"] for bar in bars} for bars in eligible.values()
        )))
    indices = {
        code: {bar["date"]: index for index, bar in enumerate(bars)}
        for code, bars in eligible.items()
    }

    for horizon in horizons:
        ic_series = []
        n_assets_list = []
        # 每段为共同日历上的 [signal, signal+horizon]，天然互不重叠。
        for common_index in range(period + 1, len(common_dates) - horizon, horizon):
            signal_date = common_dates[common_index]
            target_date = common_dates[common_index + horizon]
            scores = []
            returns = []
            for code in sorted(eligible):
                bars = eligible[code]
                signal_index = indices[code][signal_date]
                target_index = indices[code][target_date]
                if signal_index < period or target_index <= signal_index:
                    continue
                _score, slope_pct, r_squared = calc_rsrs(
                    bars, signal_index, period
                )
                current_close = float(bars[signal_index]["close"])
                target_close = float(bars[target_index]["close"])
                if current_close <= 0:
                    continue
                scores.append(slope_pct * r_squared)
                returns.append(target_close / current_close - 1)
            if len(scores) < min_assets:
                continue
            ic = spearman_rank_ic(scores, returns, min_samples=min_assets)
            if ic is not None:
                ic_series.append(ic)
                n_assets_list.append(len(scores))

        count = len(ic_series)
        if count >= 3:
            ic_mean = sum(ic_series) / count
            ic_std = (
                sum((ic - ic_mean) ** 2 for ic in ic_series) / (count - 1)
            ) ** 0.5
            sorted_ics = sorted(ic_series)
            midpoint = count // 2
            ic_median = (
                sorted_ics[midpoint]
                if count % 2
                else (sorted_ics[midpoint - 1] + sorted_ics[midpoint]) / 2
            )
            positive_ratio = sum(ic > 0 for ic in ic_series) / count
            ir = compute_ir(ic_series)
            sorted_assets = sorted(n_assets_list)
            asset_midpoint = len(sorted_assets) // 2
            median_assets = sorted_assets[asset_midpoint]
        else:
            ic_mean = ic_std = ic_median = positive_ratio = 0.0
            ir = None
            median_assets = 0

        results[f"ic_{horizon}d"] = round(ic_mean, 4)
        results[f"ir_{horizon}d"] = round(ir, 2) if ir is not None else None
        results[f"ic_std_{horizon}d"] = round(ic_std, 4)
        results[f"ic_median_{horizon}d"] = round(ic_median, 4)
        results[f"ic_pos_ratio_{horizon}d"] = round(positive_ratio, 4)
        results[f"n_dates_{horizon}d"] = count
        results[f"median_assets_{horizon}d"] = median_assets

    results["ic_mean"] = results.get("ic_20d", 0.0)
    results["ic_std"] = results.get("ic_std_20d", 0.0)
    results["ic_median"] = results.get("ic_median_20d", 0.0)
    results["ic_positive_ratio"] = results.get("ic_pos_ratio_20d", 0.0)
    results["n_dates"] = results.get("n_dates_20d", 0)
    results["median_assets"] = results.get("median_assets_20d", 0)
    return results


def stress_scenarios(daily_nav, market_data):
    """历史情景压力测试。

    Args:
        daily_nav: 回测日频 NAV 序列 [(date, nav), ...]
        market_data: 回测使用的市场数据 {code: [bars], ...}（已通过数据契约验证）
    """
    if len(daily_nav) < 5:
        return []

    values = [n[1] for n in daily_nav]
    dates = [n[0] for n in daily_nav]
    returns = []
    for i in range(1, len(values)):
        if values[i-1] > 0:
            returns.append((values[i] - values[i-1]) / values[i-1])

    # VaR/CVaR via historical simulation（历史样本损失分位阈值，非最大亏损上限）
    n = len(returns)
    sorted_rets = sorted(returns)
    var_95_idx = int(n * 0.05)
    var_95 = sorted_rets[var_95_idx] if var_95_idx < n else 0
    var_99_idx = int(n * 0.01)
    var_99 = sorted_rets[var_99_idx] if var_99_idx < n else 0
    cvar_95 = sum(sorted_rets[:var_95_idx]) / var_95_idx if var_95_idx > 0 else 0

    # 历史情景: 找到指数回撤最大的期间，计算策略在该期间的损失
    scenarios = []
    # 找出每只ETF的极端下跌区间
    for code, bars in market_data.items():
        name = POOL.get(code, code)
        closes = [b["close"] for b in bars]
        peak_i = 0
        worst_dd = 0
        worst_start = ""
        worst_end = ""
        for i, c in enumerate(closes):
            if c > closes[peak_i]:
                peak_i = i
            dd = (closes[peak_i] - c) / closes[peak_i]
            if dd > worst_dd:
                worst_dd = dd
                worst_start = bars[peak_i]["date"]
                worst_end = bars[i]["date"]

        if worst_dd > 0.15:  # 只保留 > 15% 回撤的情景
            # 计算策略在该区间的收益
            strat_ret = None
            start_val = end_val = None
            for d, v in daily_nav:
                if d == worst_start:
                    start_val = v
                if d == worst_end:
                    end_val = v
            if start_val and end_val:
                strat_ret = (end_val - start_val) / start_val * 100

            scenarios.append({
                "scenario": f"{name} 极端回撤",
                "period": f"{worst_start} ~ {worst_end}",
                "asset_dd_pct": round(worst_dd * 100, 1),
                "strategy_return_pct": round(strat_ret, 1) if strat_ret is not None else None,
            })

    scenarios.sort(key=lambda s: s["asset_dd_pct"])

    # 取 top 5 最严重的情景
    top5 = scenarios[-5:] if len(scenarios) >= 5 else scenarios
    top5.reverse()

    # 当前组合 VaR（历史样本损失分位阈值，非最大亏损上限）
    current_value = values[-1] if values else 100000
    var_95_amount = current_value * abs(var_95)
    var_99_amount = current_value * abs(var_99)
    cvar_95_amount = current_value * abs(cvar_95)

    return {
        "scenarios": top5,
        "var_95_pct": round(abs(var_95) * 100, 2),
        "var_99_pct": round(abs(var_99) * 100, 2),
        "cvar_95_pct": round(abs(cvar_95) * 100, 2),
        "var_95_amount": round(var_95_amount, 0),
        "var_99_amount": round(var_99_amount, 0),
        "cvar_95_amount": round(cvar_95_amount, 0),
    }


def audit_backtest_result(result, pool):
    """只消费一次回测的结构化结果，不重新取数或重放交易。"""
    market_data = result.get("market_data", {})
    klines = {
        code: list(series.bars) if hasattr(series, "bars") else list(series)
        for code, series in market_data.items()
    }
    daily_nav = result.get("daily_nav", [])
    return {
        "period": dict(result.get("period", {})),
        "daily_metrics": compute_daily_metrics(daily_nav),
        "ic_ir": compute_ic_ir(klines, pool) if klines else {},
        "stress_test": stress_scenarios(daily_nav, klines) if klines else {},
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--end", default=None)
    args = parser.parse_args()

    print("=" * 70)
    print("  RSRS v2.1 策略量化审计 — 补齐 IC/IR + 压力测试 + 日频指标")
    print("=" * 70)
    print()

    # 只运行一次回测；审计直接消费其冻结行情、NAV、交易和 period。
    print("[1/4] 运行回测并冻结数据快照...")
    result = run_backtest(
        pool=POOL, start_date="2016-01-01", end_date=args.end,
        freq="biweekly", momentum_period=20,
        include_bench=False, quiet=True,
    )
    audit = audit_backtest_result(result, POOL)
    trades = result.get("trades", [])
    print(f"  交易记录: {len(trades)} 笔")

    print("\n[2/4] 消费回测日频 NAV...")
    daily_navs = result.get("daily_nav", [])
    daily_metrics = audit["daily_metrics"]
    print(f"  日频 NAV: {len(daily_navs)} 个交易日")

    print("\n[3/4] IC/IR 因子验证...")
    ic_ir = audit["ic_ir"]
    print("\n[4/4] 压力测试...")
    stress = audit["stress_test"]

    # ── 输出 ──
    print("\n" + "=" * 70)
    print("  📊 审计补充报告")
    print("=" * 70)

    # 日频 vs 双周对比
    print(f"\n  ┌─ 日频风险指标 (vs 双周采样) ─────────────────────────────┐")
    biweekly = result.get("performance", {})
    print(f"  │  {'指标':<24s} {'日频':>12s} {'双周(原回测)':>16s} │")
    print(f"  │  {'─'*52} │")
    print(f"  │  {'年化收益':<24s} {daily_metrics['annual_return_pct']:>+11.1f}% {biweekly.get('annual_return_pct', 0):>+15.1f}% │")
    print(f"  │  {'年化波动率':<24s} {daily_metrics['annual_vol_pct']:>11.1f}% {biweekly.get('annual_vol_pct', 0):>15.1f}% │")
    print(f"  │  {'Sharpe Ratio':<24s} {daily_metrics['sharpe']:>12.2f} {biweekly.get('sharpe', 0):>16.2f} │")
    print(f"  │  {'Sortino Ratio':<24s} {daily_metrics['sortino']:>12.2f} {'—':>16s} │")
    print(f"  │  {'Calmar Ratio':<24s} {daily_metrics['calmar']:>12.2f} {'—':>16s} │")
    print(f"  │  {'最大回撤':<24s} {daily_metrics['max_dd_pct']:>11.1f}% {'—':>16s} │")
    print(f"  │   回撤区间: {daily_metrics['max_dd_start']} ~ {daily_metrics['max_dd_end']:<10s} │")
    print(f"  │  {'VaR (95% 日)':<24s} {daily_metrics['var_95_daily_pct']:>11.2f}% {'—':>16s} │")
    print(f"  │  {'VaR (99% 日)':<24s} {daily_metrics['var_99_daily_pct']:>11.2f}% {'—':>16s} │")
    print(f"  │  {'CVaR (95% 日)':<24s} {daily_metrics['cvar_95_daily_pct']:>11.2f}% {'—':>16s} │")
    print(f"  │  {'偏度':<24s} {daily_metrics['skewness']:>12.3f} {'—':>16s} │")
    print(f"  │  {'超额峰度':<24s} {daily_metrics['kurtosis']:>12.3f} {'—':>16s} │")
    print(f"  │  {'日均收益':<24s} {daily_metrics['avg_daily_ret_pct']:>+11.4f}% {'—':>16s} │")
    print(f"  │  {'日胜率':<24s} {daily_metrics['win_rate_pct']:>11.1f}% {'—':>16s} │")
    print(f"  │  {'盈亏比':<24s} {daily_metrics['win_loss_ratio']:>12.2f} {'—':>16s} │")
    print(f"  └{'─'*53}┘")

    # IC/IR — 横截面 IC（按日期计算跨资产 rank correlation）
    n_dates_20 = ic_ir.get("n_dates_20d", 0)
    print(f"\n  ┌─ RSRS 因子 IC/IR 验证 (横截面 IC, 跨资产 rank corr, n={n_dates_20} 期) ─┐")
    print(f"  │  {'前向窗口':<16s} {'IC mean':>10s} {'IC std':>10s} {'IR':>10s} {'判定':>10s} │")
    print(f"  │  {'─'*58} │")
    for fwd in [10, 20, 40]:
        ic = ic_ir.get(f"ic_{fwd}d", 0)
        ic_std = ic_ir.get(f"ic_std_{fwd}d", 0)
        ir = ic_ir.get(f"ir_{fwd}d")
        if ir is None:
            verdict = "样本不足"
            ir_text = "N/A"
        else:
            verdict = "✅ 有效" if ir > 0.5 else ("⚠️ 偏弱" if ir > 0.3 else "❌ 不足")
            ir_text = f"{ir:.2f}"
        print(f"  │  {f'{fwd} 日':<16s} {ic:>10.4f} {ic_std:>10.4f} {ir_text:>10s} {verdict:>10s} │")
    print(f"  ├{'─'*59}┤")
    print(f"  │  汇总统计 (20 日窗口):                                       │")
    print(f"  │    IC均值={ic_ir.get('ic_mean', 0):.4f}  中位数={ic_ir.get('ic_median', 0):.4f}  正比率={ic_ir.get('ic_positive_ratio', 0):.1%} │")
    print(f"  │    n_dates={ic_ir.get('n_dates', 0)}  median_assets={ic_ir.get('median_assets', 0)}                                            │")
    print(f"  └{'─'*59}┘")

    # 压力测试
    print(f"\n  ┌─ 历史情景压力测试 ───────────────────────────────────────┐")
    print(f"  │  {'情景':<20s} {'期间':<22s} {'资产回撤':>8s} {'策略收益':>10s} │")
    print(f"  │  {'─'*62} │")
    for s in stress["scenarios"]:
        sr = f"{s['strategy_return_pct']:+.1f}%" if s['strategy_return_pct'] is not None else "N/A"
        print(f"  │  {s['scenario']:<20s} {s['period']:<22s} {s['asset_dd_pct']:>7.1f}% {sr:>10s} │")

    # 当前组合 VaR — 用 ¥20,000 动量子账户规模
    momentum_size = 20000
    print(f"  │  {'─'*62} │")
    print(f"  │  VaR 解读 (动量子账户 ¥20,000, 历史样本损失分位阈值):     │")
    print(f"  │    VaR 95%: 历史损失分位 ¥{momentum_size * stress['var_95_pct'] / 100:,.0f} ({stress['var_95_pct']}%)          │")
    print(f"  │    VaR 99%: 历史损失分位 ¥{momentum_size * stress['var_99_pct'] / 100:,.0f} ({stress['var_99_pct']}%)          │")
    print(f"  │    CVaR 95%: 尾部平均损失 ¥{momentum_size * stress['cvar_95_pct'] / 100:,.0f} ({stress['cvar_95_pct']}%)         │")
    print(f"  └{'─'*63}┘")

    # 汇总输出
    output = {
        "daily_metrics": daily_metrics,
        "ic_ir": ic_ir,
        "stress_test": stress,
    }
    print()

    # JSON
    if args.json:
        print(json.dumps(output, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
