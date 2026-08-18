#!/usr/bin/env python3
"""
网格 ER 降底仓回测 — 验证「效率比率(ER)趋势信号 + 降底仓」是否有效。

A3 结论：MA 趋势评分作「暂停网格订单」的开关无效（暂停=停止摊薄+底仓照亏）。
本脚本换两个维度：信号换成 Kaufman ER（度量单边单调性），干预换成「降底仓」
（ER 高=单边趋势时把底仓降至 0，ER 低=震荡时恢复），治本而非停订单。

对比「无脑网格」vs「ER 降底仓」：年化 / 最大回撤 / 相对买入持有的 α / 降底仓天数。

用法:
    python3 tools/grid_er_reduce_backtest.py
    python3 tools/grid_er_reduce_backtest.py --codes 510300,512880 --thresholds 0.3,0.4,0.5
    python3 tools/grid_er_reduce_backtest.py --json
"""

import argparse
import json
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


def _backtest(code, threshold, lookback=20, reduce_ratio=1.0):
    closes, dates, opens, highs, lows = _fetch_ohlc_data(code, 1500, as_of=date.today().isoformat())
    if len(closes) < 60:
        return None
    t_plus = 0 if _is_t0(code) else 1
    kwargs = dict(
        spacing_up_pct=3.0,
        spacing_down_pct=3.0,
        levels_above=5,
        levels_below=5,
        shares_per_grid=1000,
        total_capital=100000.0,
        execution=ExecutionConfig(),
        t_plus=t_plus,
        stop_loss_ratio=0,  # 关闭止损，隔离变量
    )
    if threshold is None:
        result = run_grid_backtest(closes, dates, opens=opens, highs=highs, lows=lows, **kwargs)
    else:
        result = run_grid_backtest(
            closes, dates, opens=opens, highs=highs, lows=lows,
            er_reduce_threshold=threshold, er_lookback=lookback,
            base_reduce_ratio=reduce_ratio, **kwargs,
        )
    return {
        "annual_pct": round(result["grid_annual_pct"], 2),
        "total_pct": round(result["grid_return_pct"], 2),
        "max_dd_pct": round(result["max_dd"] * 100, 2),
        "alpha_pct": round(result["alpha_pct"], 2),
        "sharpe": round(result["sharpe"], 2),
        "reduce_days": result["base_reduce_days"],
        "total_days": result["total_trading_days"],
        "trades": result["triggered_buy"] + result["triggered_sell"],
        "final_base": result["final_base_held"],
        "bh_annual_pct": round(result["bh_annual_pct"], 2),
    }


def main():
    parser = argparse.ArgumentParser(description="网格 ER 降底仓回测验证")
    parser.add_argument("--codes", default=",".join(DEFAULT_CODES),
                        help="逗号分隔 ETF 代码")
    parser.add_argument("--thresholds", default="0.3,0.4,0.5",
                        help="ER 阈值，逗号分隔（ER>=阈值即降底仓）")
    parser.add_argument("--lookback", type=int, default=20, help="ER 回看窗（交易日）")
    parser.add_argument("--reduce-ratio", type=float, default=1.0, help="降底仓比例（1=清空）")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    thresholds = [float(t) for t in args.thresholds.split(",") if t.strip()]
    names = _names()

    all_rows = []
    for code in codes:
        baseline = _backtest(code, None, args.lookback, args.reduce_ratio)
        if baseline is None:
            print(f"  {code} 数据不足，跳过")
            continue
        rows = [{"threshold": None, "label": "无脑网格", **baseline}]
        for th in thresholds:
            r = _backtest(code, th, args.lookback, args.reduce_ratio)
            if r is None:
                continue
            rows.append({
                "threshold": th,
                "label": f"ER>={th:g}降底仓",
                **r,
            })
        all_rows.append({"code": code, "name": names.get(code, code), "rows": rows})

    if args.json:
        print(json.dumps(all_rows, ensure_ascii=False, indent=2))
        return

    for entry in all_rows:
        code = entry["code"]
        name = entry["name"]
        print(f"\n{'='*104}")
        print(f"  {name} ({code}) — ER 降底仓对比 (lookback={args.lookback}, 削减{args.reduce_ratio:g})")
        print(f"{'='*104}")
        print(f"  {'策略':<14s} {'年化':>8s} {'总收益':>8s} {'MaxDD':>7s} "
              f"{'α超额':>8s} {'Sharpe':>7s} {'降底仓天':>8s} {'交易':>5s} {'基准年化':>9s}")
        print("  " + "-" * 92)
        for r in entry["rows"]:
            dd = f"{r['max_dd_pct']:.1f}%"
            print(f"  {r['label']:<14s} {r['annual_pct']:>+7.1f}% {r['total_pct']:>+7.1f}% "
                  f"{dd:>7s} {r['alpha_pct']:>+7.1f}% {r['sharpe']:>7.2f} "
                  f"{r['reduce_days']:>5d}/{r['total_days']} {r['trades']:>5d} "
                  f"{r['bh_annual_pct']:>+8.1f}%")

    print(f"\n{'='*104}")
    print("  结论提示：α超额 = 网格年化 − 买入持有年化。若「ER 降底仓」MaxDD 显著下降而 α 不明显下降，")
    print("          则降底仓有效（用更小回撤换相近收益）；若 α 反而下降或 MaxDD 上升，则降底仓有害。")
    print(f"{'='*104}")


if __name__ == "__main__":
    main()
