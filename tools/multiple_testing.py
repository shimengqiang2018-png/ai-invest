#!/usr/bin/env python3
"""多重比较校正 — Deflated Sharpe Ratio (DSR)。

背景：组合枚举跑 N 次回测后选「年化最高」的组合，本质是数据挖掘——
N 次独立试验里总会冒出几个纯靠运气的高收益。DSR 把「从 N 个策略里
选最优」这件事的运气成分量化：给定试验次数 N、收益的偏度/峰度、观测期数，
计算纯运气下 N 次试验的期望最优 Sharpe，再判断观测到的 Sharpe 是否显著超过它。

参考文献：Bailey & López de Prado, "The Deflated Sharpe Ratio:
Correcting for Selection Bias, Backtest Overfitting and Non-Normality" (2014)。

依赖：仅标准库（math），不引入 scipy。
"""

from __future__ import annotations

import math

# Euler-Mascheroni 常数（Bailey 2014 用 γ 表示）
_EULER_GAMMA = 0.5772156649015329
_E = math.e

__all__ = [
    "returns_from_nav",
    "return_stats",
    "probit",
    "expected_max_sharpe",
    "deflated_sharpe_ratio",
    "significance_label",
]


# ---------------------------------------------------------------------------
# 收益统计
# ---------------------------------------------------------------------------

def returns_from_nav(daily_nav) -> list[float]:
    """[(date, nav), ...] -> 逐期收益率列表（相邻两期 NAV 的简单收益率）。"""
    values = [float(n[1]) for n in daily_nav]
    returns = []
    for i in range(1, len(values)):
        if values[i - 1] > 0:
            returns.append(values[i] / values[i - 1] - 1)
    return returns


def return_stats(returns: list[float]) -> dict:
    """从收益率序列计算逐期 Sharpe、偏度、原始峰度、观测数。

    DSR 公式要求「逐期（非年化）Sharpe」与「原始峰度」（正态分布 = 3），
    这里统一在此产出，避免调用方在年化/超额峰度之间来回换算出错。

    返回 dict: {sr, skew, kurt, n}，样本不足（<4）时返回 None 字段。
    """
    n = len(returns)
    if n < 4:
        return {"sr": None, "skew": None, "kurt": None, "n": n}

    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(var)
    if std == 0:
        return {"sr": 0.0, "skew": 0.0, "kurt": 3.0, "n": n}

    sr = mean / std  # 逐期 Sharpe（非年化）
    skew = (sum((r - mean) ** 3 for r in returns) / n) / std ** 3
    kurt = (sum((r - mean) ** 4 for r in returns) / n) / std ** 4  # 原始峰度，正态=3
    return {"sr": sr, "skew": skew, "kurt": kurt, "n": n}


# ---------------------------------------------------------------------------
# 标准正态逆 CDF（probit）
# ---------------------------------------------------------------------------

def probit(p: float) -> float:
    """标准正态分布逆 CDF Φ⁻¹(p)，Acklam 算法（精度 ~1e-9，无外部依赖）。"""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf

    # Acklam's algorithm 系数
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]

    plow, phigh = 0.02425, 1 - 0.02425

    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
           ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)


# ---------------------------------------------------------------------------
# DSR 核心
# ---------------------------------------------------------------------------

def expected_max_sharpe(sr: float, n_trials: int, skew: float, kurt: float, n_obs: int) -> float:
    """纯运气下 N 次独立试验的期望最优 Sharpe（逐期，非年化）。

    sr:      观测 Sharpe（逐期，非年化），用于估计其方差 V[SR]
    n_trials: 试验次数（跑了多少个组合/参数）
    skew:     收益偏度
    kurt:     收益原始峰度（正态 = 3）
    n_obs:    观测期数（收益样本数）
    """
    n_trials = max(1, int(n_trials))
    n_obs = max(2, int(n_obs))
    if n_trials <= 1:
        # 只试一个策略：不存在「选最优」的选择偏差，期望最优即 0。
        return 0.0
    # V[SR] = (1 - skew·SR + (kurt-1)/4·SR²) / (n-1)
    var_sr = (1 - skew * sr + (kurt - 1) / 4 * sr ** 2) / (n_obs - 1)
    if var_sr < 0:
        var_sr = 0.0
    std_sr = math.sqrt(var_sr)
    # E[max SR_N] ≈ σ_SR · [(1-γ)·Φ⁻¹(1 - 1/N) + γ·Φ⁻¹(1 - 1/(N·e))]
    z1 = probit(1 - 1 / n_trials)
    z2 = probit(1 - 1 / (n_trials * _E))
    return std_sr * ((1 - _EULER_GAMMA) * z1 + _EULER_GAMMA * z2)


def deflated_sharpe_ratio(
    sr: float,
    n_trials: int,
    skew: float,
    kurt: float,
    n_obs: int,
) -> dict:
    """计算 Deflated Sharpe Ratio。

    返回 dict:
        sr:              观测 Sharpe（逐期，非年化）
        expected_max_sr: 纯运气下 N 次试验的期望最优 Sharpe
        deflated_sr:     观测 SR 扣除运气后剩余（可能为负）
        prob:            DSR 概率 = Φ((SR - E[max SR]) / σ_SR)，∈ (0,1)
                          > 0.95 通常视为「显著优于运气」
    """
    n_trials = max(1, int(n_trials))
    n_obs = max(2, int(n_obs))
    var_sr = (1 - skew * sr + (kurt - 1) / 4 * sr ** 2) / (n_obs - 1)
    if var_sr < 0:
        var_sr = 0.0
    std_sr = math.sqrt(var_sr)

    exp_max = expected_max_sharpe(sr, n_trials, skew, kurt, n_obs)
    deflated = sr - exp_max
    # Φ 是标准正态 CDF；用 0.5·erfc(-x/√2) 实现
    prob = 0.5 * math.erfc(-deflated / (std_sr * math.sqrt(2))) if std_sr > 0 else 0.5
    return {
        "sr": sr,
        "expected_max_sr": exp_max,
        "deflated_sr": deflated,
        "prob": prob,
    }


def significance_label(prob: float) -> str:
    """把 DSR 概率转成人话。"""
    if prob >= 0.95:
        return "显著（>95%）"
    if prob >= 0.90:
        return "边缘显著（>90%）"
    return "不显著（可能运气）"


def strategy_dsr(daily_nav, n_trials: int) -> dict:
    """从策略日频 NAV 计算 DSR 置信度（统一 ~biweekly 降采样，与枚举回测一致）。

    daily_nav: [(date, nav), ...]（momentum_etf_backtest 的 daily_nav 格式）
    n_trials:  策略从多少次试验中被选出（多重比较校正的 N）

    返回 dict: {dsr_prob, significance_label, sr_period, skew, kurt, n_obs}。
    样本不足时 dsr_prob/significance_label 分别为 None / "数据不足"。
    """
    ret_stats = return_stats(returns_from_nav((daily_nav or [])[::10]))
    sr = ret_stats["sr"]
    if sr is None or ret_stats["n"] < 4:
        return {
            "dsr_prob": None,
            "significance_label": "数据不足",
            "sr_period": None,
            "skew": None,
            "kurt": None,
            "n_obs": ret_stats["n"],
        }
    dsr = deflated_sharpe_ratio(
        sr, n_trials, ret_stats["skew"], ret_stats["kurt"], ret_stats["n"]
    )
    return {
        "dsr_prob": round(dsr["prob"], 3),
        "significance_label": significance_label(dsr["prob"]),
        "sr_period": round(sr, 4),
        "skew": round(ret_stats["skew"], 3),
        "kurt": round(ret_stats["kurt"], 3),
        "n_obs": ret_stats["n"],
    }


if __name__ == "__main__":
    # 快速自检：演示「同样 Sharpe，试验次数越多越不值钱」
    print(f"{'N试验':>6} {'SR_obs':>8} {'E[maxSR]':>10} {'DSR概率':>10}")
    for n_trials in (1, 10, 100, 1000, 3000):
        r = deflated_sharpe_ratio(sr=0.10, n_trials=n_trials, skew=0.0, kurt=3.0, n_obs=250)
        print(f"{n_trials:>6} {r['sr']:>8.3f} {r['expected_max_sr']:>10.4f} {r['prob']:>10.1%}")
