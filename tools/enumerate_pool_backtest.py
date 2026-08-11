#!/usr/bin/env python3
"""
ETF 动量轮动 — 标的池枚举回测

对候选 ETF 池的所有组合（3/4/5 只）逐一跑回测，
输出按年化收益排名的最佳组合。

用法:
    python3 tools/enumerate_pool_backtest.py                    # 全部组合
    python3 tools/enumerate_pool_backtest.py --min 3 --max 5   # 3~5只组合
    python3 tools/enumerate_pool_backtest.py --top 20           # 只输出前20
"""

import argparse, itertools, json, os, sys, time
from datetime import datetime

# 复用现有回测引擎
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from momentum_etf_backtest import run_backtest, ETF_POOL

# 候选 ETF - 全量池（排除银华日利 511880）
CANDIDATE_CODES = [
    "518880",  # 黄金ETF (2013~)
    "513100",  # 纳指ETF (2013~)
    "159915",  # 创业板ETF (2011~)
    "510300",  # 沪深300ETF (2012~)
    "588000",  # 科创50ETF (2020~)
    "512880",  # 证券ETF (2016~)
    "512690",  # 酒ETF (2014~)
    "512010",  # 医药ETF (2013~)
    "513180",  # 恒生科技ETF (2021~)
    "510500",  # 中证500ETF (2013~)
    "159920",  # 恒生ETF (2012~)
    "510050",  # 上证50ETF (2005~)
]

# 长历史池: 2016年前成立的 ETF，可做 10年+ 回测
VETERAN_CODES = [
    "518880",  # 黄金ETF (2013)
    "513100",  # 纳指ETF (2013)
    "159915",  # 创业板ETF (2011)
    "510300",  # 沪深300ETF (2012)
    "512880",  # 证券ETF (2016)
    "512690",  # 酒ETF (2014)
    "512010",  # 医药ETF (2013)
    "510500",  # 中证500ETF (2013)
    "159920",  # 恒生ETF (2012)
    "510050",  # 上证50ETF (2005)
]

MOMENTUM_PERIODS = [20, 40]  # 短周期 + 中周期
FREQ = "biweekly"


def main():
    parser = argparse.ArgumentParser(description="ETF池枚举回测")
    parser.add_argument("--min", type=int, default=3, help="最少 ETF 数量（默认3）")
    parser.add_argument("--max", type=int, default=5, help="最多 ETF 数量（默认5）")
    parser.add_argument("--top", type=int, default=30, help="输出前 N 名（默认30）")
    parser.add_argument("--momentum", default="20,40", help="动量周期（默认20,40）")
    parser.add_argument("--json", action="store_true", help="JSON输出")
    parser.add_argument("--universe", default="full", choices=["full", "veteran"],
                        help="预设候选池名称 (full=全12只, veteran=精选10只)")
    parser.add_argument("--pool", dest="universe", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)  # 已弃用，用 --universe 代替
    parser.add_argument("--start", default=None, help="回测起始日期（默认自动适配）")
    parser.add_argument("--switch-buffer", type=float, default=1.0,
                        help="换仓迟滞系数（默认 1.0=无迟滞，如 1.25 表示挑战者需超持仓25%%才换仓）")
    args = parser.parse_args()

    momentums = [int(m.strip()) for m in args.momentum.split(",")]

    # 选择候选池
    codes = VETERAN_CODES if args.universe == "veteran" else CANDIDATE_CODES
    start_date = args.start or ("2016-01-01" if args.universe == "veteran" else "2019-01-01")

    # 枚举所有组合
    all_combos = []
    for n in range(args.min, args.max + 1):
        all_combos.extend(itertools.combinations(codes, n))

    total = len(all_combos) * len(momentums)
    print(f"{'='*80}")
    print(f"  ETF 池枚举回测")
    print(f"{'='*80}")
    print(f"  候选 ETF: {len(codes)} 只")
    print(f"  组合数: {len(all_combos)} (C({len(codes)},{args.min})~C({len(codes)},{args.max}))")
    print(f"  动量周期: {momentums}")
    print(f"  总回测次数: {total}")
    print(f"  预计耗时: ~{total * 2} 秒")
    print()

    results = []
    done = 0
    start_time = time.time()

    for combo in all_combos:
        pool = {c: ETF_POOL.get(c, c) for c in combo}
        combo_name = "+".join(combo)  # 如 "518880+513100+159915"
        combo_label = "+".join(ETF_POOL.get(c, c) for c in combo)

        for mp in momentums:
            done += 1
            elapsed = time.time() - start_time
            eta = (elapsed / done) * (total - done) if done > 0 else 0
            print(f"  [{done}/{total}] {combo_label} × {mp}日 ...", end=" ", flush=True)

            try:
                r = run_backtest(
                    pool=pool, start_date=start_date, end_date=None,
                    freq=FREQ, momentum_period=mp,
                    include_bench=True, quiet=True,
                    switch_buffer=args.switch_buffer,
                )
                if r:
                    p = r["performance"]
                    period = r["period"]
                    sells = [t for t in r["trades"] if "卖出" in t["action"]]
                    wins = len([t for t in sells if t.get("pnl", 0) > 0])
                    total_sells = len(sells)
                    wr = wins / total_sells * 100 if total_sells > 0 else 0

                    # 从回测结果直接读取风险指标
                    ann_ret = p["annual_return_pct"]
                    sharpe = p.get("sharpe", 0)
                    sortino = p.get("sortino", 0)
                    calmar = p.get("calmar", 0)
                    max_dd = p.get("max_dd_pct", 0)
                    ann_vol = p.get("annual_vol_pct", 0)
                    dd_days = p.get("max_dd_days", 0)

                    ret_str = f"年化{ann_ret:+.1f}% 总{p['total_return_pct']:+.1f}%"
                    risk_str = f"MaxDD{max_dd:.1f}%/{dd_days}d Vol{ann_vol:.1f}%"
                    ratio_str = f"Sharpe{sharpe:.2f} Sortino{sortino:.2f} Calmar{calmar:.2f}"
                    print(f"{ret_str} | {risk_str} | {ratio_str} | 胜率{wr:.0f}%")

                    results.append({
                        "combo": combo_name,
                        "label": combo_label,
                        "n_etf": len(combo),
                        "momentum": mp,
                        "freq": FREQ,
                        # 收益
                        "annual_pct": round(ann_ret, 2),
                        "total_pct": round(p["total_return_pct"], 2),
                        "excess_pct": round(p["excess_return_pct"], 2),
                        "benchmark_pct": round(p["benchmark_equal_weight_pct"], 2),
                        # 风险
                        "max_dd_pct": round(max_dd, 1),
                        "max_dd_days": dd_days,
                        "annual_vol_pct": round(ann_vol, 1),
                        "sharpe": round(sharpe, 2),
                        "sortino": round(sortino, 2),
                        "calmar": round(calmar, 2),
                        # 交易
                        "num_trades": p["num_trades"],
                        "win_rate": round(wr, 1),
                        "nav_final": round(p["final_nav"], 2),
                        "period_years": round(period["years"], 2),
                        # 窗口
                        "window_start": period["start"],
                        "window_truncated": period.get("window_truncated", False),
                    })
                else:
                    print("❌ 数据不足")
            except Exception as e:
                print(f"❌ {str(e)[:50]}")

    if not results:
        print("\n无有效回测结果")
        return

    # 按年化收益排序
    results.sort(key=lambda r: r["annual_pct"], reverse=True)

    # 输出
    if args.json:
        print(json.dumps(results[:args.top], indent=2, ensure_ascii=False))
        return

    # 检测窗口不一致
    windows = {(r["window_start"], r["period_years"]) for r in results}
    if len(windows) > 1:
        print(f"\n  ⚠️  各组合回测窗口不一致（{len(windows)} 种窗口），年化不可直接比较")

    print(f"\n{'='*110}")
    print(f"  🏆 标的池排名 TOP {args.top}（按年化收益）")
    print(f"{'='*110}")
    header = (f"  {'排名':<4s} {'组合':<30s} {'n':>2s} {'年化':>8s} {'总收益':>8s} "
              f"{'MaxDD':>7s} {'回撤天':>6s} {'Vol':>6s} "
              f"{'Sharpe':>6s} {'Sortino':>7s} {'Calmar':>6s} {'胜率':>5s} {'交易':>4s} {'窗口':>8s}")
    print(header)
    print("  " + "-" * (len(header) - 2))

    for rank, r in enumerate(results[:args.top], 1):
        ann = f"{r['annual_pct']:+.1f}%"
        tot = f"{r['total_pct']:+.1f}%"
        dd = f"{r['max_dd_pct']:.1f}%"
        dd_days = f"{r['max_dd_days']}d"
        vol = f"{r['annual_vol_pct']:.1f}%"
        sh = f"{r['sharpe']:.2f}"
        so = f"{r['sortino']:.2f}"
        ca = f"{r['calmar']:.2f}"
        wr = f"{r['win_rate']:.0f}%"
        win = r['window_start'][:7] if r.get('window_start') else '?'

        flag = "✅" if r["excess_pct"] > 0 else "🔴"
        trunc = " ⚠️" if r.get("window_truncated") else ""
        print(f"  {rank:>3d}. {r['label']:<30s} {r['n_etf']:>2d} {ann:>8s} {tot:>8s} "
              f"{dd:>7s} {dd_days:>6s} {vol:>6s} "
              f"{sh:>6s} {so:>7s} {ca:>6s} {wr:>5s} {r['num_trades']:>4d} {win:>8s}{trunc}")

    # ── 统计摘要 ──
    top_n = min(20, len(results))
    top_returns = [r["annual_pct"] for r in results[:top_n]]
    top_sharpe = [r["sharpe"] for r in results[:top_n]]
    top_sortino = [r["sortino"] for r in results[:top_n]]
    all_returns = [r["annual_pct"] for r in results]
    all_sharpe = [r["sharpe"] for r in results]

    print(f"\n{'='*110}")
    print(f"  📊 统计摘要")
    print(f"{'='*110}")
    print(f"  总组合数: {len(results)} 个有效回测")
    print(f"  TOP{top_n} 年化均值: {sum(top_returns)/len(top_returns):+.1f}%")
    print(f"  TOP{top_n} Sharpe 均值: {sum(top_sharpe)/len(top_sharpe):.2f}")
    print(f"  TOP{top_n} Sortino 均值: {sum(top_sortino)/len(top_sortino):.2f}")
    print(f"  全样本年化均值: {sum(all_returns)/len(all_returns):+.1f}%")
    print(f"  全样本 Sharpe 均值: {sum(all_sharpe)/len(all_sharpe):.2f}")
    print(f"  超额>0 比例: {sum(1 for r in results if r['excess_pct'] > 0)}/{len(results)} "
          f"({sum(1 for r in results if r['excess_pct'] > 0)/len(results)*100:.0f}%)")

    # ── 最佳组合详情 ──
    best = results[0]
    print(f"\n  🥇 最佳组合: {best['label']} × {best['momentum']}日")
    print(f"     年化 {best['annual_pct']:+.1f}%  超额 {best['excess_pct']:+.1f}%  "
          f"Sharpe {best['sharpe']:.2f}  Sortino {best['sortino']:.2f}  Calmar {best['calmar']:.2f}")
    print(f"     MaxDD {best['max_dd_pct']:.1f}% (持续 {best['max_dd_days']}天)  "
          f"年化波动 {best['annual_vol_pct']:.1f}%  胜率 {best['win_rate']:.0f}%")

    # 按 Sharpe 排名最佳
    best_sharpe = max(results, key=lambda r: r["sharpe"])
    print(f"\n  🏆 最佳 Sharpe: {best_sharpe['label']} × {best_sharpe['momentum']}日")
    print(f"     Sharpe {best_sharpe['sharpe']:.2f}  Sortino {best_sharpe['sortino']:.2f}  "
          f"Calmar {best_sharpe['calmar']:.2f}  年化 {best_sharpe['annual_pct']:+.1f}%  MaxDD {best_sharpe['max_dd_pct']:.1f}%")

    # ── 按 ETF 数量分组统计 ──
    print(f"\n  ── 按池子大小分组（均值）──")
    for n in range(args.min, args.max + 1):
        group = [r for r in results if r["n_etf"] == n]
        if group:
            avg_ann = sum(r["annual_pct"] for r in group) / len(group)
            avg_sh = sum(r["sharpe"] for r in group) / len(group)
            best_g = max(group, key=lambda r: r["annual_pct"])
            print(f"  {n}只ETF ({len(group)}组合): 年化均值{avg_ann:+.1f}%  Sharpe均值{avg_sh:.2f}  "
                  f"最佳: {best_g['label']} ({best_g['annual_pct']:+.1f}%)")

    print()


if __name__ == "__main__":
    main()
