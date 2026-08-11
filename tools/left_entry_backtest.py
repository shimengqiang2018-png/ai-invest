#!/usr/bin/env python3
"""左侧交易买点信号回测 v2.0

对动量池 10 只 ETF，回测改进版左侧信号的历史预测准确性。

信号条件（v2.0 改进版）:
  ① 估值: 价格距 2 年高点回撤 > 25%（替代 PE 分位 < P25）
  ② RSRS 反转: RSRS 当前值 > -0.1 且 14日前 < -0.3（从深度负值回到近零——趋势企稳）
  ③ 双底确认: 60日内出现两个低点，第二个 ≥ 第一个×95%（不创新低）
               且当前价 > 第一个低点×1.05（已从底部反弹5%+）

v1.0 → v2.0 核心改进:
  - 将"RSRS仅回升"改为"RSRS从负翻近零"——过滤死猫反弹
  - 新增"双底不创新低"结构确认——过滤单底暴跌接刀
  - 回撤阈值 30%→25% 提高覆盖率

测试:
  - 信号触发后，未来 20/40/60/120 个交易日的收益分布
  - 命中率（正收益概率）
  - 平均收益 vs 无条件平均收益（超额）
  - 信号频率（不会频繁到没有意义）

用法:
  python3 tools/left_entry_backtest.py                     # 全部 10 只 ETF
  python3 tools/left_entry_backtest.py --code 512880        # 单只
  python3 tools/left_entry_backtest.py --horizon 20,40,60   # 指定回看窗口
  python3 tools/left_entry_backtest.py --json               # JSON 输出
"""

import argparse
import json
import math
import os
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "tools"))

# ── 复用 K 线获取 ────────────────────────────────────────
try:
    from etf_screener import fetch_kline as fetch_etf_kline
except ImportError:
    fetch_etf_kline = None

try:
    from left_entry_checker import _fetch_klines, _qq_code, _fetch_tencent_kline
except ImportError:
    _fetch_klines = None
    _qq_code = None
    _fetch_tencent_kline = None

# ══════════════════════════════════════════════════════════
# 10 只动量池 ETF
# ══════════════════════════════════════════════════════════

MOMENTUM_POOL: dict[str, str] = {
    "510300": "沪深300ETF",
    "510500": "中证500ETF",
    "159915": "创业板ETF",
    "588000": "科创50ETF",
    "512880": "证券ETF",
    "512690": "酒ETF",
    "512010": "医药ETF",
    "513100": "纳指ETF",
    "513180": "恒生科技ETF",
    "159920": "恒生ETF",
    "518880": "黄金ETF",
}

DEFAULT_HORIZONS = [20, 40, 60, 120]


# ══════════════════════════════════════════════════════════
# 数据获取
# ══════════════════════════════════════════════════════════

def fetch_klines(code: str) -> list[dict]:
    """获取 ETF 全部历史日 K。"""
    klines = []

    # 路径 1: etf_screener 缓存
    if fetch_etf_kline:
        try:
            klines = fetch_etf_kline(code, count=2000)
            if klines and len(klines) >= 500:
                return klines
        except Exception:
            pass

    # 路径 2: left_entry_checker
    if _fetch_klines:
        try:
            klines = _fetch_klines(code, count=2000)
            if klines and len(klines) >= 500:
                return klines
        except Exception:
            pass

    # 路径 3: 直连腾讯
    if _fetch_tencent_kline:
        klines = _fetch_tencent_kline(code, 2000)

    return klines


# ══════════════════════════════════════════════════════════
# 信号计算
# ══════════════════════════════════════════════════════════

def compute_rolling_indicators(
    closes: list[float],
    volumes: list[float],
    dates: list[str],
) -> dict[int, dict]:
    """
    v2.0: 滑动计算每个交易日的左侧信号。

    改进条件:
      ① 估值: 回撤 > 25%
      ② RSRS 企稳: 当前 RSRS > -0.1 且 14日前 < -0.3
      ③ 双底: 60日内两低点，第二个不创新低(≥95%)，当前反弹>5%
    """
    n = len(closes)
    if n < 500:
        return {}

    results = {}

    for i in range(500, n):
        window_closes = closes[:i + 1]
        window_volumes = volumes[:i + 1]

        # ── 条件①: 价格回撤 ──
        two_year_idx = max(0, i - 500)
        peak_2y = max(closes[two_year_idx:i + 1])
        drawdown = (closes[i] / peak_2y - 1) if peak_2y > 0 else 0
        is_cheap = drawdown < -0.25

        # ── 条件②: RSRS 反转（从负翻正）──
        rsrs_current = _calc_ols_rsrs(window_closes[-25:]) if len(window_closes) >= 25 else None
        rsrs_7d_ago = _calc_ols_rsrs(window_closes[-32:-7]) if len(window_closes) >= 32 else None
        rsrs_14d_ago = _calc_ols_rsrs(window_closes[-39:-14]) if len(window_closes) >= 39 else None

        is_rsrs_reversal = False
        if (rsrs_current is not None and rsrs_14d_ago is not None
                and rsrs_current > -0.1 and rsrs_14d_ago < -0.3):
            is_rsrs_reversal = True

        # ── 条件③: 双底确认 ──
        is_double_bottom = False
        bottom1_price = None
        bottom2_price = None
        if i >= 60:
            lookback_60 = closes[i - 59:i + 1]
            # 找 60 日内两个最低点（间隔至少 10 日）
            lows = []
            for j in range(10, len(lookback_60) - 10):
                local_min = True
                for k in range(j - 10, j + 11):
                    if k != j and k < len(lookback_60) and lookback_60[k] < lookback_60[j]:
                        local_min = False
                        break
                if local_min:
                    lows.append((j, lookback_60[j]))
            # 取最低的两个局部低点
            lows.sort(key=lambda x: x[1])
            if len(lows) >= 2:
                # 第一个低点（更低那个）
                b1_idx, b1_price = lows[0]
                # 第二个低点（次低那个，必须在第一个之后）
                b2_candidates = [(idx, p) for idx, p in lows if idx > b1_idx]
                if not b2_candidates and len(lows) >= 3:
                    # 如果次低在第一个之前，取更后面的
                    b2_candidates = [(idx, p) for idx, p in lows[1:] if idx > b1_idx]
                if b2_candidates:
                    b2_idx, b2_price = min(b2_candidates, key=lambda x: x[1])
                    # 双底条件: 第二个底 >= 第一个底的 95%（不创新低）
                    if b2_price >= b1_price * 0.95:
                        # 当前价已经从第一个底反弹 5%+
                        if closes[i] > b1_price * 1.05:
                            is_double_bottom = True
                            bottom1_price = b1_price
                            bottom2_price = b2_price

        # ── 辅助: 量能萎缩（v2.0 降级为辅助指标）──
        if i >= 60:
            vol_20d = sum(window_volumes[-20:]) / 20
            vol_60d_sorted = sorted(window_volumes[-60:])
            vol_60d_median = vol_60d_sorted[len(vol_60d_sorted) // 2]
            vol_ratio = vol_20d / vol_60d_median if vol_60d_median > 0 else 1.0
            vol_shrinking = vol_ratio < 0.60
            rsi = _calc_rsi(window_closes[-15:]) if len(window_closes) >= 15 else 50
        else:
            vol_ratio = 1.0
            vol_shrinking = False
            rsi = 50

        is_washed_out = vol_shrinking and rsi > 25

        # ── 综合评分（v2.0: 估值 + RSRS反转 + 双底）──
        score = sum([is_cheap, is_rsrs_reversal, is_double_bottom])

        results[i] = {
            "date": dates[i],
            "close": closes[i],
            "drawdown": drawdown,
            "is_cheap": is_cheap,
            "is_rsrs_reversal": is_rsrs_reversal,
            "is_double_bottom": is_double_bottom,
            "bottom1_price": bottom1_price,
            "bottom2_price": bottom2_price,
            "vol_ratio": vol_ratio,
            "vol_shrinking": vol_shrinking,
            "rsi": rsi,
            "is_washed_out": is_washed_out,
            "rsrs_current": rsrs_current,
            "rsrs_7d_ago": rsrs_7d_ago,
            "rsrs_14d_ago": rsrs_14d_ago,
            "score": score,
        }

    return results


def _calc_rsi(closes_15: list[float]) -> float:
    """简单 RSI(14) 计算。"""
    if len(closes_15) < 15:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes_15)):
        diff = closes_15[i] - closes_15[i - 1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    avg_gain = sum(gains[-14:]) / 14
    avg_loss = sum(losses[-14:]) / 14
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _calc_ols_rsrs(closes_window: list[float]) -> float | None:
    """OLS RSRS 得分 = 年化收益率 × R²。"""
    if len(closes_window) < 5:
        return None
    n = len(closes_window)
    log_c = [math.log(c) for c in closes_window if c > 0]
    if len(log_c) < n * 0.8:
        return None
    n = len(log_c)
    x_mean = (n - 1) / 2.0
    y_mean = sum(log_c) / n
    num = sum((i - x_mean) * (log_c[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    if den == 0:
        return None
    slope = num / den
    annual_ret = math.exp(slope * 250) - 1
    ss_res = sum((log_c[i] - (slope * i + (y_mean - slope * x_mean))) ** 2 for i in range(n))
    ss_tot = sum((lc - y_mean) ** 2 for lc in log_c)
    r2 = max(0.0, 1 - ss_res / ss_tot) if ss_tot > 0 else 0
    return annual_ret * r2


# ══════════════════════════════════════════════════════════
# 前向收益计算
# ══════════════════════════════════════════════════════════

@dataclass
class SignalStats:
    """单个信号级别的统计。"""
    count: int = 0
    hit_rate: float = 0.0  # 正收益比例
    mean_return: float = 0.0
    median_return: float = 0.0
    max_return: float = 0.0
    min_return: float = 0.0
    std_return: float = 0.0
    unconditional_mean: float = 0.0  # 无条件平均（全时段）
    excess: float = 0.0  # 超额收益
    returns: list[float] = field(default_factory=list)


@dataclass
class BacktestResult:
    """单只 ETF 的回测结果。"""
    code: str
    name: str
    total_days: int = 0
    signal_counts: dict[int, int] = field(default_factory=dict)  # score→count
    # horizon_days → SignalStats（信号触发后）
    triggered: dict[int, SignalStats] = field(default_factory=dict)
    # horizon_days → SignalStats（无条件对照）
    unconditional: dict[int, SignalStats] = field(default_factory=dict)
    # 得分拆分
    by_score: dict[int, dict[int, SignalStats]] = field(default_factory=dict)
    error: str | None = None


def compute_forward_returns(
    closes: list[float],
    indicators: dict[int, dict],
    horizons: list[int],
) -> dict:
    """计算各信号触发后的前向收益。"""
    n = len(closes)

    # 无条件基准：所有交易日
    unconditional = {h: SignalStats() for h in horizons}
    # 信号触发
    triggered = {h: SignalStats() for h in horizons}
    # 按得分拆分
    by_score: dict[int, dict[int, SignalStats]] = defaultdict(
        lambda: {h: SignalStats() for h in horizons}
    )

    for i, ind in indicators.items():
        if i + max(horizons) >= n:
            continue

        score = ind["score"]

        for h in horizons:
            if i + h >= n:
                continue
            fwd_ret = (closes[i + h] / closes[i] - 1) * 100

            # 无条件
            unconditional[h].returns.append(fwd_ret)
            unconditional[h].count += 1

            # 信号触发（score >= 1）
            if score >= 1:
                triggered[h].returns.append(fwd_ret)
                triggered[h].count += 1

            # 按得分
            if score >= 1:
                by_score[score][h].returns.append(fwd_ret)
                by_score[score][h].count += 1

    # 计算统计量
    for stats_dict in [unconditional, triggered]:
        for h, ss in stats_dict.items():
            _finalize_stats(ss)

    for score, hdict in by_score.items():
        for h, ss in hdict.items():
            _finalize_stats(ss)

    # 计算超额
    for h in horizons:
        if unconditional[h].count > 0 and triggered[h].count > 0:
            triggered[h].excess = triggered[h].mean_return - unconditional[h].mean_return
            triggered[h].unconditional_mean = unconditional[h].mean_return

    return {
        "unconditional": unconditional,
        "triggered": triggered,
        "by_score": dict(by_score),
    }


def _finalize_stats(ss: SignalStats):
    """从 returns 列表计算统计量。"""
    if not ss.returns:
        return
    ss.mean_return = sum(ss.returns) / len(ss.returns)
    ss.median_return = sorted(ss.returns)[len(ss.returns) // 2]
    ss.max_return = max(ss.returns)
    ss.min_return = min(ss.returns)
    ss.hit_rate = sum(1 for r in ss.returns if r > 0) / len(ss.returns)
    m = ss.mean_return
    ss.std_return = math.sqrt(sum((r - m) ** 2 for r in ss.returns) / len(ss.returns))


# ══════════════════════════════════════════════════════════
# 主回测
# ══════════════════════════════════════════════════════════

def backtest_etf(code: str, name: str, horizons: list[int]) -> BacktestResult:
    """对单只 ETF 执行完整的左侧信号回测。"""
    result = BacktestResult(code=code, name=name)

    klines = fetch_klines(code)
    if not klines or len(klines) < 500:
        result.error = f"K线不足（{len(klines) if klines else 0}根, 需要≥500）"
        return result

    closes = [k["close"] for k in klines]
    volumes = [k.get("volume", 0) for k in klines]
    dates = [k.get("date", str(i)) for i, k in enumerate(klines)]

    result.total_days = len(closes)

    # 计算滚动指标
    indicators = compute_rolling_indicators(closes, volumes, dates)
    if not indicators:
        result.error = "指标计算失败"
        return result

    # 统计信号分布
    for ind in indicators.values():
        s = ind["score"]
        result.signal_counts[s] = result.signal_counts.get(s, 0) + 1

    # 前向收益
    fwd = compute_forward_returns(closes, indicators, horizons)
    result.triggered = fwd["triggered"]
    result.unconditional = fwd["unconditional"]
    result.by_score = fwd["by_score"]

    return result


# ══════════════════════════════════════════════════════════
# 汇总 & 输出
# ══════════════════════════════════════════════════════════

def render_backtest(results: list[BacktestResult], horizons: list[int], json_out: bool = False):
    """渲染回测结果。"""
    if json_out:
        _render_json(results, horizons)
        return
    _render_terminal(results, horizons)


def _term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except Exception:
        return 130


def _render_terminal(results: list[BacktestResult], horizons: list[int]):
    """终端渲染。"""
    w = min(_term_width(), 130)
    print()
    print("╔" + "═" * (w - 2) + "╗")
    print("║" + "  📊 左侧交易信号回测 — 历史预测准确性".ljust(w - 2) + "║")
    print("║" + f"  回测标的: {len(results)} 只 ETF  |  回看窗口: "
          f"{', '.join(f'{h}日' for h in horizons)}  |  "
          f"{datetime.now().strftime('%Y-%m-%d %H:%M')}".ljust(w - 2) + "║")
    print("║" + "  信号条件: ①回撤>25% ②RSRS企稳(>-0.1且14日前<-0.3) ③双底不创新低(60日)".ljust(w - 2) + "║")
    print("╚" + "═" * (w - 2) + "╝")
    print()

    # ── 表头 ──
    header = f"  {'ETF':<16s} {'总天数':>6s}"
    for h in horizons:
        header += f" {'触发':>5s} {'均值':>7s} {'命中':>5s} {'超额':>7s}"
    header += f"  {'评分≥1%':>7s}  {'评分≥2%':>7s}"
    print(header)
    print("  " + "─" * (len(header) + 4))

    valid_results = [r for r in results if not r.error]

    # ── 逐行 ──
    for r in results:
        if r.error:
            print(f"  [{r.code}] {r.name:<12s}  ❌ {r.error}")
            continue

        total_signal_days = sum(v for k, v in r.signal_counts.items() if k >= 1)
        total_signal_days_2 = sum(v for k, v in r.signal_counts.items() if k >= 2)
        signal_pct = total_signal_days / max(r.total_days, 1) * 100
        signal_pct_2 = total_signal_days_2 / max(r.total_days, 1) * 100

        line = f"  [{r.code}] {r.name:<12s} {r.total_days:>6d}"
        for h in horizons:
            ts = r.triggered.get(h)
            if ts and ts.count > 5:
                line += f" {ts.count:>5d} {ts.mean_return:>+6.1f}% {ts.hit_rate:>4.0%} {ts.excess:>+6.1f}%"
            else:
                line += f" {'-':>5s} {'-':>7s} {'-':>5s} {'-':>7s}"

        line += f"  {signal_pct:>6.1f}%  {signal_pct_2:>6.1f}%"
        print(line)

        # 高分信号详情（有足够样本时）
        if total_signal_days_2 > 5:
            best_h = max(horizons, key=lambda h: r.triggered.get(h, SignalStats()).excess)
            ts = r.triggered.get(best_h)
            if ts and ts.count > 5:
                print(f"      最佳窗口 {best_h}日: 触发{ts.count}次 | "
                      f"均值{ts.mean_return:+.1f}% | 命中{ts.hit_rate:.0%} | "
                      f"超额{ts.excess:+.1f}% | 最差{ts.min_return:+.1f}%")

    # ── 汇总 ──
    print()
    print("  " + "═" * (len(header) + 4))
    _print_pool_summary(valid_results, horizons)

    # ── 信号频率分析 ──
    print()
    print("  📈 信号频率分析（过高=噪声，过低=无意义）:")
    for r in valid_results:
        total = max(r.total_days, 1)
        s1 = sum(v for k, v in r.signal_counts.items() if k >= 1)
        s2 = sum(v for k, v in r.signal_counts.items() if k >= 2)
        s3 = sum(v for k, v in r.signal_counts.items() if k >= 3)
        freq_info = f"⭐×{s1}({s1/total:.1%})"
        if s2 > 0:
            freq_info += f" ⭐⭐×{s2}({s2/total:.1%})"
        if s3 > 0:
            freq_info += f" ⭐⭐⭐×{s3}({s3/total:.1%})"
        print(f"  [{r.code}] {r.name:<12s} {freq_info}")

    # ── 结论（只看有信号的 ETF）──
    print()
    print("  ── 信号质量评估 ──")

    # 只看有实际触发的 ETF
    triggered_etfs = [r for r in valid_results
                      if sum(v for k, v in r.signal_counts.items() if k >= 1) >= 10]

    if not triggered_etfs:
        print("  无足够信号样本")
        print()
        return

    # 全池合并统计
    combined = {h: SignalStats() for h in horizons}
    for r in triggered_etfs:
        for h in horizons:
            ts = r.triggered.get(h)
            if ts and ts.returns:
                combined[h].returns.extend(ts.returns)
                combined[h].count += ts.count
    for h in horizons:
        _finalize_stats(combined[h])

    print(f"  有信号 ETF ({len(triggered_etfs)}/11):")
    for r in triggered_etfs:
        parts = []
        for h in horizons:
            ts = r.triggered.get(h)
            if ts and ts.count >= 5:
                sig = "✅" if ts.excess > 1 else ("🟡" if ts.excess > 0 else "❌")
                parts.append(f"{h}日{sig}{ts.excess:+.1f}pp")
        total_sig = sum(v for k, v in r.signal_counts.items() if k >= 1)
        sc2 = sum(v for k, v in r.signal_counts.items() if k >= 2)
        sc3 = sum(v for k, v in r.signal_counts.items() if k >= 3)
        star_str = f"⭐⭐⭐×{sc3}" if sc3 > 0 else (f"⭐⭐×{sc2}" if sc2 > 0 else f"⭐×{total_sig}")
        print(f"  [{r.code}] {r.name:<12s} {star_str:<10s} {' | '.join(parts)}")

    # 汇总判断
    print()
    pos_20 = sum(1 for r in triggered_etfs
                 if r.triggered.get(20) and r.triggered[20].count >= 5 and r.triggered[20].excess > 0)
    pos_120 = sum(1 for r in triggered_etfs
                  if r.triggered.get(120) and r.triggered[120].count >= 5 and r.triggered[120].excess > 0)

    all_excess_20 = [r.triggered[20].excess for r in triggered_etfs
                     if r.triggered.get(20) and r.triggered[20].count >= 5]
    avg_excess = sum(all_excess_20) / len(all_excess_20) if all_excess_20 else 0

    print(f"  20日窗口: {pos_20}/{len(triggered_etfs)} 只有正超额，均值超额 {avg_excess:+.1f}pp")
    print(f"  120日窗口: {pos_120}/{len(triggered_etfs)} 只有正超额")

    if avg_excess >= 1.0 and pos_20 >= len(triggered_etfs) * 0.5:
        print(f"  ✅ 信号可靠: 有信号的 ETF 在多数窗口获得显著正超额")
        print(f"  ✅ 可用场景: 左侧买入 + 止损 MA60 × 0.95")
        print(f"  ⚠️ 注意: 仅 {len(triggered_etfs)}/11 只 ETF 会产生信号，高波动品种为主")
    elif avg_excess > 0:
        print(f"  🟡 信号有效但弱: 超额为正但不显著，建议作为辅助参考")
    print()


def _print_pool_summary(valid_results: list[BacktestResult], horizons: list[int]):
    """打印全池汇总统计。"""
    # 合并所有 ETF 的信号触发记录
    combined_triggered = {h: SignalStats() for h in horizons}
    combined_uncond = {h: SignalStats() for h in horizons}

    for r in valid_results:
        for h in horizons:
            ts = r.triggered.get(h)
            if ts and ts.returns:
                combined_triggered[h].returns.extend(ts.returns)
                combined_triggered[h].count += ts.count
            us = r.unconditional.get(h)
            if us and us.returns:
                combined_uncond[h].returns.extend(us.returns)
                combined_uncond[h].count += us.count

    for h in horizons:
        _finalize_stats(combined_triggered[h])
        _finalize_stats(combined_uncond[h])

    print(f"  {'全池合并':<16s} {'-':>6s}", end="")
    for h in horizons:
        ts = combined_triggered.get(h)
        us = combined_uncond.get(h)
        if ts and ts.count > 10:
            excess = ts.mean_return - us.mean_return if us.count > 0 else 0
            print(f" {ts.count:>5d} {ts.mean_return:>+6.1f}% {ts.hit_rate:>4.0%} {excess:>+6.1f}%", end="")
        else:
            print(f" {'-':>5s} {'-':>7s} {'-':>5s} {'-':>7s}", end="")
    print()


def _render_json(results: list[BacktestResult], horizons: list[int]):
    """JSON 输出。"""
    output = {
        "as_of": date.today().isoformat(),
        "horizons": horizons,
        "items": [],
    }
    for r in results:
        item = {
            "code": r.code,
            "name": r.name,
            "total_days": r.total_days,
            "signal_counts": r.signal_counts,
            "error": r.error,
        }
        if not r.error:
            for h in horizons:
                ts = r.triggered.get(h)
                if ts:
                    item[f"horizon_{h}d"] = {
                        "count": ts.count,
                        "hit_rate": round(ts.hit_rate, 4),
                        "mean_return": round(ts.mean_return, 2),
                        "median_return": round(ts.median_return, 2),
                        "max_return": round(ts.max_return, 2),
                        "min_return": round(ts.min_return, 2),
                        "excess": round(ts.excess, 2),
                    }
        output["items"].append(item)
    print(json.dumps(output, indent=2, ensure_ascii=False, default=str))


# ══════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="左侧交易信号回测 — 历史预测准确性验证",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--code", help="单只 ETF 代码")
    parser.add_argument("--horizon", default="20,40,60,120",
                        help="前向窗口（天），逗号分隔，默认 20,40,60,120")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    horizons = [int(x.strip()) for x in args.horizon.split(",") if x.strip()]

    if args.code:
        code = args.code
        name = MOMENTUM_POOL.get(code, code)
        pool = {code: name}
    else:
        pool = MOMENTUM_POOL

    results = []
    for code, name in pool.items():
        print(f"  ⏳ [{code}] {name} 回测中...", file=sys.stderr, end=" ", flush=True)
        result = backtest_etf(code, name, horizons)
        status = "✅" if not result.error else f"❌ {result.error}"
        print(status, file=sys.stderr, flush=True)
        results.append(result)

    render_backtest(results, horizons, json_out=args.json)


if __name__ == "__main__":
    main()
