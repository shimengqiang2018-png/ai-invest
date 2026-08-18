#!/usr/bin/env python3
"""Walk-forward 样本外验证。

背景：组合枚举/参数寻优的「选参区间 = 验证区间 = 全部历史」，导致
「最优组合/参数」是全区间过拟合产物，不可信。walk-forward 把历史切成
多个 train/test 折：train 段只用来选参，test 段才是样本外，两者永不重叠。

本脚本对「全区间枚举的 top-K 组合」做样本外验证，输出：
  1. 每个候选的样本外绩效（几何总收益 / 年化 / Sharpe / 排名）；
  2. 「全区间排名 vs 样本外排名」对照表 —— 名次崩得越厉害，过拟合越严重；
  3. 跟随策略（每折 train 选最优 → 下一折 test 实盘）的样本外绩效，衡量选参
     流程本身是否稳健。

用法:
    # 先跑全区间枚举得到结果 JSON
    python3 tools/enumerate_pool_backtest.py --universe veteran --json > /tmp/enum.json
    # 对 top-20 组合做样本外验证
    python3 tools/walk_forward.py --enum /tmp/enum.json --top 20
    python3 tools/walk_forward.py --enum data/enum_backtest_veteran_c3_25d.json --top 10
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from momentum_etf_backtest import ETF_POOL, _add_months, fetch_kline, run_backtest
from multiple_testing import return_stats, returns_from_nav

__all__ = ["generate_folds", "run_walk_forward", "concat_fold_returns"]


def generate_folds(
    common_start: str,
    common_end: str,
    train_months: int = 24,
    test_months: int = 12,
    step_months: int = 12,
    warmup_days: int = 365,
) -> list[tuple[str, str, str]]:
    """生成不重叠的 train/test 窗口。

    每个窗口 = (train_start, train_end, test_end)，test_start = train_end。
    warmup_days：数据起点需要预留的预热期（252 个交易日 ≈ 1 年），确保 train
    段开跑时各标的都有足够历史算 RSRS 指标。

    返回 [(train_start, train_end, test_end), ...]，日期为 "YYYY-MM-DD"。
    """
    earliest = datetime.strptime(common_start, "%Y-%m-%d") + timedelta(days=warmup_days)
    latest = datetime.strptime(common_end, "%Y-%m-%d")

    folds = []
    fold_start = earliest
    while True:
        train_end = _add_months(fold_start, train_months)
        test_end = _add_months(train_end, test_months)
        if test_end > latest:
            break
        folds.append((
            fold_start.strftime("%Y-%m-%d"),
            train_end.strftime("%Y-%m-%d"),
            test_end.strftime("%Y-%m-%d"),
        ))
        fold_start = _add_months(fold_start, step_months)
    return folds


def concat_fold_returns(fold_daily_navs: list[list[tuple[str, float]]]) -> list[float]:
    """把各折 test 段的日 NAV 转为日收益并拼接（折间不产生收益）。"""
    rets: list[float] = []
    for nav in fold_daily_navs:
        rets.extend(returns_from_nav(nav))
    return rets


def _geom_annual(total_pct: float, total_days: float) -> float:
    """几何总收益 + 自然日数 → 年化百分比。"""
    total = 1 + total_pct / 100
    if total <= 0 or total_days <= 0:
        return float("nan")
    return (total ** (365.25 / total_days) - 1) * 100


def run_walk_forward(
    candidates: list[dict],
    train_months: int = 24,
    test_months: int = 12,
    step_months: int = 12,
    freq: str = "biweekly",
    select_metric: str = "sharpe",
    switch_buffer: float = 1.0,
    market_data: dict | None = None,
    run_fn=run_backtest,
    quiet: bool = True,
) -> dict:
    """对候选组合做 walk-forward 样本外验证。

    candidates: [{"codes": [...], "momentum": int, "label": str}, ...]
        （通常来自全区间枚举结果的 top-K，label 用于展示）
    select_metric: train 段选参指标，默认 "sharpe"（比年化更抗高波动孤峰）
    market_data: 预取的 {code: bars}，None 时自动 fetch（复用给所有回测）
    run_fn: 回测函数（可注入 mock 便于测试）

    返回 dict（见函数末尾）。
    """
    all_codes = sorted({c for cand in candidates for c in cand["codes"]})
    if market_data is None:
        market_data = {code: fetch_kline(code, count=2000) for code in all_codes}

    # 确定公共日期范围
    common_start = max(k[0]["date"] for k in market_data.values())
    common_end = min(k[-1]["date"] for k in market_data.values())
    folds = generate_folds(common_start, common_end, train_months, test_months, step_months)
    if len(folds) < 2:
        return {"folds": [], "error": "数据不足以生成至少 2 个 walk-forward 折"}

    def _run(cand, start, end, include_bench=False):
        pool = {c: ETF_POOL.get(c, c) for c in cand["codes"]}
        return run_fn(
            pool=pool,
            start_date=start,
            end_date=end,
            freq=freq,
            momentum_period=cand["momentum"],
            include_bench=include_bench,
            quiet=quiet,
            market_data=market_data,
            switch_buffer=switch_buffer,
        )

    # 候选样本外记录
    oos: dict[str, dict] = {
        cand["label"]: {
            "cand": cand,
            "fold_returns_pct": [],
            "fold_daily_navs": [],
            "fold_days": 0,
            "selected_count": 0,
        }
        for cand in candidates
    }
    follow_returns_pct = []
    follow_bench_pct = []
    follow_daily_navs = []
    fold_records = []

    for i, (train_start, train_end, test_end) in enumerate(folds):
        # ── train 段：全部候选跑一遍，选 select_metric 最优 ──
        best_label, best_score = None, -math.inf
        for cand in candidates:
            r = _run(cand, train_start, train_end)
            perf = (r or {}).get("performance") or {}
            score = perf.get(select_metric, -math.inf)
            if score is not None and score > best_score:
                best_score, best_label = score, cand["label"]

        # ── test 段：每个候选都验证一次（样本外），跟随策略用 best 那一个 ──
        follow_r = None
        for cand in candidates:
            r = _run(cand, train_end, test_end, include_bench=True)
            if not r or "performance" not in r:
                continue
            perf = r["performance"]
            rec = oos[cand["label"]]
            rec["fold_returns_pct"].append(perf.get("total_return_pct", 0.0))
            nav = r.get("daily_nav") or []
            rec["fold_daily_navs"].append(nav)
            if nav:
                rec["fold_days"] += (len(nav) - 1)
            if cand["label"] == best_label:
                rec["selected_count"] += 1
                follow_r = r

        if follow_r is not None:
            p = follow_r["performance"]
            follow_returns_pct.append(p.get("total_return_pct", 0.0))
            follow_bench_pct.append(p.get("benchmark_equal_weight_pct", 0.0))
            follow_daily_navs.append(follow_r.get("daily_nav") or [])

        fold_records.append({
            "train": f"{train_start}~{train_end}",
            "test": f"{train_end}~{test_end}",
            "selected": best_label,
            "selected_score": round(best_score, 3),
        })

    # ── 汇总每个候选的样本外绩效 ──
    for label, rec in oos.items():
        fr = rec["fold_returns_pct"]
        n = len(fr)
        total_pct = 0.0
        if n:
            prod = 1.0
            for r in fr:
                prod *= (1 + r / 100)
            total_pct = (prod - 1) * 100
        rec["oos_total_pct"] = total_pct
        rec["oos_annual_pct"] = _geom_annual(total_pct, rec["fold_days"])
        rets = concat_fold_returns(rec["fold_daily_navs"])
        stats = return_stats(rets)
        rec["oos_sharpe"] = (stats["sr"] * math.sqrt(252)) if stats["sr"] is not None else None
        rec["n_folds"] = n
        rec["n_obs"] = stats["n"]

    # 样本外排名（按样本外年化降序）
    ranked = sorted(oos.items(), key=lambda kv: kv[1]["oos_total_pct"], reverse=True)
    for rank, (label, rec) in enumerate(ranked, 1):
        rec["oos_rank"] = rank

    # 跟随策略样本外绩效
    follow_total = 0.0
    follow_bench_total = 0.0
    if follow_returns_pct:
        prod = prod_b = 1.0
        for r, b in zip(follow_returns_pct, follow_bench_pct):
            prod *= (1 + r / 100)
            prod_b *= (1 + b / 100)
        follow_total = (prod - 1) * 100
        follow_bench_total = (prod_b - 1) * 100
    follow_rets = concat_fold_returns(follow_daily_navs)
    follow_stats = return_stats(follow_rets)

    return {
        "folds": fold_records,
        "n_folds": len(folds),
        "candidates": [
            {
                "label": label,
                "codes": rec["cand"]["codes"],
                "momentum": rec["cand"]["momentum"],
                "oos_total_pct": round(rec["oos_total_pct"], 2),
                "oos_annual_pct": round(rec["oos_annual_pct"], 2) if not math.isnan(rec["oos_annual_pct"]) else None,
                "oos_sharpe": round(rec["oos_sharpe"], 2) if rec["oos_sharpe"] is not None else None,
                "oos_rank": rec["oos_rank"],
                "n_folds": rec["n_folds"],
                "selected_count": rec["selected_count"],
            }
            for label, rec in sorted(oos.items(), key=lambda kv: kv[1]["oos_rank"])
        ],
        "follow_strategy": {
            "oos_total_pct": round(follow_total, 2),
            "benchmark_total_pct": round(follow_bench_total, 2),
            "excess_pct": round(follow_total - follow_bench_total, 2),
            "oos_sharpe": round(follow_stats["sr"] * math.sqrt(252), 2) if follow_stats["sr"] is not None else None,
        },
    }


def _candidates_from_enum(results: list[dict], top_k: int) -> list[dict]:
    """从全区间枚举结果 JSON 的 results 里解析 top-K 候选。

    期望字段: combo（"518880+513100+..."）, momentum（int）。
    """
    candidates = []
    for r in results[:top_k]:
        combo = r.get("combo", "")
        codes = [c for c in combo.split("+") if c]
        momentum = int(r.get("momentum", 0) or 0)
        if not codes or momentum <= 0:
            continue
        candidates.append({
            "codes": codes,
            "momentum": momentum,
            "label": f"{r.get('label') or combo}×{momentum}日",
        })
    return candidates


def main():
    parser = argparse.ArgumentParser(description="Walk-forward 样本外验证")
    parser.add_argument("--enum", required=True, help="全区间枚举结果 JSON 路径")
    parser.add_argument("--top", type=int, default=20, help="取全区间排名前 N（默认20）")
    parser.add_argument("--train-months", type=int, default=24, help="训练段月数（默认24）")
    parser.add_argument("--test-months", type=int, default=12, help="测试段月数（默认12）")
    parser.add_argument("--step-months", type=int, default=12, help="滚动步长月数（默认12）")
    parser.add_argument("--metric", default="sharpe", help="train 选参指标（默认 sharpe）")
    args = parser.parse_args()

    with open(args.enum, encoding="utf-8") as f:
        payload = json.load(f)
    results = payload.get("results") or payload if isinstance(payload, list) else []
    if isinstance(payload, dict):
        results = payload.get("results") or []
    candidates = _candidates_from_enum(results, args.top)
    if not candidates:
        print("❌ 无法从枚举结果解析候选组合（缺少 combo/momentum 字段）")
        return

    print(f"\n{'='*96}")
    print(f"  Walk-forward 样本外验证  |  {args.enum}")
    print(f"{'='*96}")
    print(f"  候选: top-{len(candidates)}  |  train {args.train_months}月 / test {args.test_months}月 / 步长 {args.step_months}月  |  选参指标: {args.metric}")

    result = run_walk_forward(
        candidates,
        train_months=args.train_months,
        test_months=args.test_months,
        step_months=args.step_months,
        select_metric=args.metric,
    )
    if result.get("error"):
        print(f"  ❌ {result['error']}")
        return

    # ── 每折选了什么 ──
    print(f"\n  ── {result['n_folds']} 折选参记录 ──")
    for fr in result["folds"]:
        print(f"    train {fr['train']} → test {fr['test']}  选: {fr['selected']}")

    # ── 样本外排名 vs 全区间排名 ──
    print(f"\n  ── 样本外排名（按样本外总收益）──")
    hdr = f"  {'样本外#':<6} {'组合':<40} {'样本外总收益':>12} {'样本外年化':>11} {'样本外Sharpe':>12} {'被选中':>5}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for c in result["candidates"]:
        ann = f"{c['oos_annual_pct']:+.1f}%" if c["oos_annual_pct"] is not None else "  —"
        sh = f"{c['oos_sharpe']:.2f}" if c["oos_sharpe"] is not None else "—"
        print(f"  {c['oos_rank']:<6} {c['label']:<40} {c['oos_total_pct']:+11.1f}% {ann:>11} {sh:>12} {c['selected_count']:>4}/{c['n_folds']}")

    fs = result["follow_strategy"]
    print(f"\n  ── 跟随策略（每折选最优 → 下一折实盘）──")
    print(f"    样本外总收益 {fs['oos_total_pct']:+.1f}%  vs 等权基准 {fs['benchmark_total_pct']:+.1f}%  "
          f"超额 {fs['excess_pct']:+.1f}%  Sharpe {fs['oos_sharpe']}")
    print()


if __name__ == "__main__":
    main()
