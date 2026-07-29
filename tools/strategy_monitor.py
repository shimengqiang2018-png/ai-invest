#!/usr/bin/env python3
"""
动量轮动(RSRS v2.1) + 网格 双策略综合监测

用法:
    python3 tools/strategy_monitor.py                          # 全量扫描
    python3 tools/strategy_monitor.py --momentum-only          # 仅动量
    python3 tools/strategy_monitor.py --grid-only              # 仅网格
    python3 tools/strategy_monitor.py --json                   # JSON 输出
    python3 tools/strategy_monitor.py --audit                  # 量化审计 (IC/IR+压力测试+日频指标)

输出:
    动量侧: RSRS v2.1 信号排名 + 推荐池状态 + 操作建议
    网格侧: 持仓 ETF 趋势评分 + 买卖方向 + 风险提示
    --audit: 日频 MaxDD/Sharpe/VaR + RSRS 因子 IC/IR + 历史情景压力测试
"""

import json, os, re, subprocess, sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.join(SCRIPT_DIR, "..")

# 动量推荐池 — 基于10年回测 Sharpe 最优
MOMENTUM_POOL = {
    "518880": "黄金ETF",
    "513100": "纳指ETF",
    "159915": "创业板ETF",
    "159920": "恒生ETF",
}


def run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, cwd=PROJECT_DIR)
        return r.stdout
    except Exception as e:
        return f"ERROR: {e}"


def load_grid_etfs():
    f = os.path.join(PROJECT_DIR, "data", "grid_triggers.json")
    if os.path.exists(f):
        try:
            with open(f) as fh:
                return list(json.load(fh).keys())
        except: pass
    return ["512880", "159915", "513180", "512690", "512010", "510300", "159920"]


def momentum_scan():
    """RSRS 动量信号扫描。先取 JSON 解析，再取文本展示。"""
    codes = ",".join(MOMENTUM_POOL.keys())
    out = run(f"python3 tools/momentum_signal.py --pool {codes}", timeout=120)
    json_out = run(f"python3 tools/momentum_signal.py --pool {codes} --json", timeout=120)

    # 从 JSON 解析信号
    signals = []
    try:
        data = json.loads(json_out)
        for r in data:
            if isinstance(r, dict) and "code" in r:
                signals.append({
                    "code": r["code"],
                    "name": r.get("name", ""),
                    "passed": r.get("pass", False),
                    "rsrs_score": r.get("rsrs_score", 0),
                    "signal_strength": r.get("signal_strength", "none"),
                    "close": r.get("close", 0),
                })
    except Exception:
        # JSON 解析失败，用文本提取
        for line in out.split("\n"):
            m = re.search(r'(\d{6})\s+(\S+).*RSRS:\s*([\d.]+)', line)
            if m:
                signals.append({"code": m.group(1), "name": m.group(2),
                               "passed": "不通过" not in line, "rsrs_score": float(m.group(3))})
            if "买入信号:" in line:
                m2 = re.search(r'买入信号:\s*(\d{6})\s*(\S+)', line)
                if m2:
                    signals.append({"code": m2.group(1), "name": m2.group(2),
                                   "passed": True, "is_signal": True})
            if "无买入信号" in line:
                signals.append({"passed": False, "is_signal": True, "no_signal": True})

    return out, signals


def grid_scan():
    """网格持仓趋势检查。"""
    etfs = load_grid_etfs()
    results = []
    for etf in etfs:
        out = run(f"python3 tools/grid_trading.py trend {etf}", timeout=20)
        score_m = re.search(r'综合评分:\s*(-?\d+)', out)
        score = int(score_m.group(1)) if score_m else 99
        status = ""
        if "→" in out:
            status = out.split("→")[-1].strip().split("║")[0].strip()

        # 提取 BB 宽度
        bb_m = re.search(r'宽度\s+([\d.]+)%', out)
        bb_width = float(bb_m.group(1)) if bb_m else 0

        # 提取均线状态
        ma_state = ""
        if "空头排列" in out: ma_state = "空头"
        elif "多头排列" in out: ma_state = "多头"
        elif "均线缠绕" in out: ma_state = "缠绕"
        elif "MA20 附近" in out: ma_state = "MA20附近"

        results.append({
            "etf": etf, "score": score, "status": status,
            "bb_width": bb_width, "ma_state": ma_state,
        })
    return results


def print_momentum_report(out, signals):
    """格式化动量信号输出。"""
    print("═" * 70)
    print("  📈 动量轮动 · RSRS 信号")
    print("═" * 70)
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  推荐池: {' + '.join(f'{c}({n})' for c,n in MOMENTUM_POOL.items())}")
    print()

    # 直接输出 signal.py 的简洁版结果
    # 提取信号行和操作建议
    for line in out.split("\n"):
        s = line.strip()
        # 跳过头部装饰线
        if s.startswith("===") or "扫描日期" in s or "核心指标" in s or "过滤:" in s:
            if "扫描日期" in s or "核心指标" in s:
                print(f"  {s}")
            continue
        # 输出每只 ETF 的评分行和过滤行
        if any(kw in s for kw in ["RSRS:", "MA20:", "波动率:", "不通过:", "异动:"]):
            print(f"  {s}")
        # 输出操作建议段
        if "📋 操作建议" in s or "买入信号:" in s or "无买入信号" in s or "信号分布" in s:
            print(f"  {s}")
        if "操作:" in s and ("仓位" in s or "切换" in s or "持币" in s):
            print(f"  {s}")
        if "理由:" in s:
            print(f"  {s}")
    print()


def print_grid_report(results):
    """格式化网格状态输出。"""
    print("═" * 70)
    print("  📊 网格策略 · 持仓趋势")
    print("═" * 70)
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print()

    stop = [r for r in results if r["score"] <= -4]
    caution = [r for r in results if -3 <= r["score"] <= -2]
    ok = [r for r in results if r["score"] >= -1]

    if stop:
        print("  ⛔ 暂停买入 (TrendScore ≤ -4):")
        for r in stop:
            print(f"     {r['etf']} 评分{r['score']} BB{r['bb_width']:.1f}% {r['ma_state']} → {r['status'][:40]}")
        print()

    if caution:
        print("  🔴 仅保留顺势 (TrendScore -3~-2):")
        for r in caution:
            print(f"     {r['etf']} 评分{r['score']} BB{r['bb_width']:.1f}% {r['ma_state']} → {r['status'][:40]}")
        print()

    if ok:
        print("  🟡 谨慎/正常 (TrendScore ≥ -1):")
        for r in ok:
            print(f"     {r['etf']} 评分{r['score']} BB{r['bb_width']:.1f}% {r['ma_state']} → {r['status'][:40]}")
        print()

    if not stop and not caution:
        print("  ✅ 全部正常\n")
    elif stop:
        print(f"  ⚠️  {len(stop)}只需暂停买入 | 💡 观察 BB 宽度何时回落\n")


def print_advice(momentum_pass, grid_stop):
    """综合建议。"""
    print("═" * 70)
    print("  📋 操作清单")
    print("═" * 70)
    print()

    # 动量
    if momentum_pass:
        print("  [动量] 🟢 有信号 → 按信号换仓")
    else:
        print("  [动量] 🔴 无信号 → 持币或 511880")
    print()

    # 网格
    if grid_stop:
        codes = [r["etf"] for r in grid_stop]
        print(f"  [网格] ⛔ {', '.join(codes)} → APP 中暂停买入条件单")
    else:
        print("  [网格] ✅ 无需操作，条件单自动运行")
    print()

    # 审计风险指标（静态提示）
    print("  ── 风险参数 (日频审计) ──")
    print("  日频 MaxDD:  16.9% (双周采样 11.3% 低估了 5.6pp)")
    print("  日频 Sharpe: 1.49 | VaR(95%): -1.74%/日")
    print("  RSRS IC(10日): 0.074 | IC(20日): 0.028 (短周期动量)")
    print()

    print("  ── 命令速查 ──")
    print("  每日收盘:   python3 tools/strategy_monitor.py")
    print("  仅动量:     python3 tools/strategy_monitor.py --momentum-only")
    print("  仅网格:     python3 tools/strategy_monitor.py --grid-only")
    print("  动量扫描:   python3 tools/momentum_signal.py")
    print("  回测验证:   python3 tools/enumerate_pool_backtest.py --pool veteran")
    print("  量化审计:   python3 tools/strategy_audit.py       (日频指标+IC/IR+压力)")
    print()


def run_audit():
    """运行量化审计，输出日频指标摘要。"""
    import subprocess
    print("\n  ⏳ 运行量化审计 (IC/IR + 压力测试 + 日频指标)...")
    result = subprocess.run(
        ["python3", os.path.join(SCRIPT_DIR, "strategy_audit.py")],
        capture_output=True, text=True, timeout=300, cwd=PROJECT_DIR
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="双策略综合监测 v2.0")
    parser.add_argument("--momentum-only", action="store_true")
    parser.add_argument("--grid-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--audit", action="store_true", help="运行量化审计 (日频指标+IC/IR+压力测试)")
    args = parser.parse_args()

    if args.audit:
        run_audit()
        return

    print()
    print("╔" + "═" * 68 + "╗")
    print("║  动量轮动(RSRS v2.1) + 网格趋势 双策略监测" + " " * 23 + "║")
    print("╚" + "═" * 68 + "╝")
    print()

    # 采集数据
    momentum_out, momentum_signals = "", []
    grid_results = []
    if not args.grid_only:
        momentum_out, momentum_signals = momentum_scan()
    if not args.momentum_only:
        grid_results = grid_scan()

    # 计算派生指标
    has_momentum_signal = any(s.get("passed") for s in momentum_signals if s.get("code"))
    has_buy_signal = has_momentum_signal  # 有任一通过 = 有买入信号
    grid_stop = [r for r in grid_results if r["score"] <= -4]

    if args.json:
        result = {
            "time": datetime.now().isoformat(),
            "momentum": {
                "has_signal": has_momentum_signal if not args.grid_only else None,
                "buy_signal": has_buy_signal if not args.grid_only else None,
                "signals": [{"code": s["code"], "name": s["name"], "passed": s["passed"],
                            "rsrs": s.get("rsrs_score", 0)}
                           for s in momentum_signals if s.get("code")],
            },
            "grid": [{"etf": r["etf"], "score": r["score"], "bb_width": r["bb_width"],
                      "ma_state": r["ma_state"]} for r in grid_results],
            "grid_stop_list": [r["etf"] for r in grid_stop],
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    # 格式化输出
    if not args.grid_only:
        print_momentum_report(momentum_out, momentum_signals)
    if not args.momentum_only:
        print_grid_report(grid_results)
    print_advice(has_buy_signal, grid_stop)


if __name__ == "__main__":
    main()
