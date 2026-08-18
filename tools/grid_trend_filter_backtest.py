#!/usr/bin/env python3
"""
网格趋势过滤回测 — 验证「趋势评分作开关」是否有效。

把网格回测的趋势过滤作为可回测参数（trend_pause_threshold），
对比「无脑网格（阈值=None）」vs「带趋势过滤的网格（评分<阈值暂停）」：
年化收益 / 最大回撤 / 相对买入持有的超额 α / 暂停天数。

趋势评分与 analyze_trend 同逻辑：正=震荡有利网格，负=趋势不利网格。
阈值含义（对应 analyze_trend 的 verdict 分档）:
  阈值 -3 → 只在「强趋势」暂停（评分 < -3）
  阈值 -1 → 「趋势 + 强趋势」都暂停（评分 < -1）
  阈值  1 → 非「偏震荡以上」都暂停（评分 < 1，即只保留震荡市跑网格）

用法:
    python3 tools/grid_trend_filter_backtest.py
    python3 tools/grid_trend_filter_backtest.py --codes 510300,512880 --thresholds -3,-1,1
    python3 tools/grid_trend_filter_backtest.py --json
"""

import argparse
import json
import sys
from datetime import date

try:
    from tools.grid_trading import run_grid_backtest, _fetch_ohlc_data, _is_t0
    from tools.trading_ledger import ExecutionConfig
except ModuleNotFoundError:
    from grid_trading import run_grid_backtest, _fetch_ohlc_data, _is_t0
    from trading_ledger import ExecutionConfig


DEFAULT_CODES = [
    "510300",  # 沪深300
    "512880",  # 证券
    "159915",  # 创业板
    "512010",  # 医药
    "159920",  # 恒生
    "512690",  # 酒
]


def _names():
    try:
        import os
        meta_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "etf_meta.json")
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        return {
            str(e.get("code")): e.get("name") or e.get("code")
            for e in (meta.get("etfs") or [])
        }
    except Exception:
        return {}


def _backtest(code, threshold):
    """单次网格回测。返回绩效摘要 dict，数据不足/失败返回 None。"""
    closes, dates, opens, highs, lows = _fetch_ohlc_data(code, 1500, as_of=date.today().isoformat())
    if len(closes) < 60:
        return None
    t_plus = 0 if _is_t0(code) else 1
    result = run_grid_backtest(
        closes, dates,
        opens=opens, highs=highs, lows=lows,
        spacing_up_pct=3.0,
        spacing_down_pct=3.0,
        levels_above=5,
        levels_below=5,
        shares_per_grid=1000,
        total_capital=100000.0,
        execution=ExecutionConfig(),
        t_plus=t_plus,
        stop_loss_ratio=0,  # 关闭止损，隔离趋势过滤变量，用完整窗口比较
        trend_pause_threshold=threshold,
    )
    return {
        "annual_pct": round(result["grid_annual_pct"], 2),
        "total_pct": round(result["grid_return_pct"], 2),
        "max_dd_pct": round(result["max_dd"] * 100, 2),
        "alpha_pct": round(result["alpha_pct"], 2),
        "sharpe": round(result["sharpe"], 2),
        "paused_days": result["paused_days"],
        "total_days": result["total_trading_days"],
        "trades": result["triggered_buy"] + result["triggered_sell"],
        "bh_annual_pct": round(result["bh_annual_pct"], 2),
    }


def main():
    parser = argparse.ArgumentParser(description="网格趋势过滤回测验证")
    parser.add_argument("--codes", default=",".join(DEFAULT_CODES),
                        help="逗号分隔 ETF 代码（默认沪深300/证券/创业板/医药/恒生/酒）")
    parser.add_argument("--thresholds", default="-3,-1,1",
                        help="趋势暂停阈值，逗号分隔（默认 -3,-1,1）")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    thresholds = [float(t) for t in args.thresholds.split(",") if t.strip()]
    names = _names()

    all_rows = []
    for code in codes:
        baseline = _backtest(code, None)
        if baseline is None:
            print(f"  {code} 数据不足，跳过")
            continue
        rows = [{"threshold": None, "label": "无脑网格", **baseline}]
        for th in thresholds:
            r = _backtest(code, th)
            if r is None:
                continue
            rows.append({
                "threshold": th,
                "label": f"评分<{th:g}暂停",
                **r,
            })
        all_rows.append({"code": code, "name": names.get(code, code), "rows": rows})

    if args.json:
        print(json.dumps(all_rows, ensure_ascii=False, indent=2))
        return

    # 文本报告
    for entry in all_rows:
        code = entry["code"]
        name = entry["name"]
        print(f"\n{'='*100}")
        print(f"  {name} ({code}) — 趋势过滤对比")
        print(f"{'='*100}")
        print(f"  {'策略':<14s} {'年化':>8s} {'总收益':>8s} {'MaxDD':>7s} "
              f"{'α超额':>8s} {'Sharpe':>7s} {'暂停天':>6s} {'交易':>5s} {'基准年化':>9s}")
        print("  " + "-" * 88)
        for r in entry["rows"]:
            dd = f"{r['max_dd_pct']:.1f}%"
            print(f"  {r['label']:<14s} {r['annual_pct']:>+7.1f}% {r['total_pct']:>+7.1f}% "
                  f"{dd:>7s} {r['alpha_pct']:>+7.1f}% {r['sharpe']:>7.2f} "
                  f"{r['paused_days']:>5d}/{r['total_days']} {r['trades']:>5d} "
                  f"{r['bh_annual_pct']:>+8.1f}%")

    print(f"\n{'='*100}")
    print("  结论提示：α超额 = 网格年化 − 买入持有年化。若「带过滤」的 MaxDD 显著下降而 α 不明显下降，")
    print("          则趋势过滤有效（用更小回撤换相近收益）；若 α 反而下降，则过滤在抹掉网格收益。")
    print(f"{'='*100}")


if __name__ == "__main__":
    main()
