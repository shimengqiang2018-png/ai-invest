"""Shared, causal momentum signal calculations for scanners and backtests."""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median

# 止损参数（实盘扫描器与回测审计共用）
STOP_LOSS_PCT = 0.08  # 绝对止损 8%，入场价下跌超过此比例触发止损


@dataclass(frozen=True)
class MomentumConfig:
    rsrs_period: int = 25
    ma_period: int = 20
    warmup_days: int = 252
    vol_period: int = 20
    vol_history_days: int = 252
    vol_limit_multiple: float = 1.5
    volume_limit_multiple: float = 2.5
    rsi_period: int = 14
    rsi_limit: float = 80.0


@dataclass(frozen=True)
class SignalSnapshot:
    code: str
    date: str
    raw_rsrs_score: float
    slope_annual_pct: float
    r_squared: float
    signal_strength: str
    passed: bool
    metrics: dict


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _moving_average(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    return _mean(values[-period:])


def _annualized_volatility(closes: list[float], period: int) -> float:
    if period <= 0 or len(closes) < period + 1:
        return 0.0
    window = closes[-(period + 1):]
    returns = [math.log(window[i] / window[i - 1]) for i in range(1, len(window))]
    if len(returns) < 2:
        return 0.0
    average = _mean(returns)
    variance = sum((value - average) ** 2 for value in returns) / (len(returns) - 1)
    return math.sqrt(max(0.0, variance)) * math.sqrt(252)


def _historical_volatility_median(closes: list[float], period: int) -> float:
    if len(closes) <= period:
        return 0.0
    vols = [
        _annualized_volatility(closes[: end + 1], period)
        for end in range(period, len(closes))
    ]
    usable = [value for value in vols if value > 0]
    return median(usable) if usable else 0.0


def _raw_rsrs(bars: tuple[dict, ...], index: int, period: int) -> tuple[float, float, float]:
    """标准 OLS 线性回归 + 指数年化，对齐小薪ETF轮动算法。

    Returns:
        (raw_score, slope_annual_pct, r_squared)
        raw_score = 年化收益率(%) × R²（负值时取 0）
    """
    if period <= 0 or index < period:
        return 0.0, 0.0, 0.0
    window = bars[index - period:index + 1]
    log_prices = [math.log(float(bar["close"])) for bar in window]
    size = len(log_prices)

    # 标准 OLS: y = slope × x + intercept，等权
    x_mean = (size - 1) / 2.0
    y_mean = _mean(log_prices)
    sxy = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(log_prices))
    sxx = sum((i - x_mean) ** 2 for i in range(size))
    if abs(sxx) < 1e-12:
        return 0.0, 0.0, 0.0
    slope = sxy / sxx
    intercept = y_mean - slope * x_mean

    # R²: 标准 OLS 下的拟合优度
    total_error = sum((y - y_mean) ** 2 for y in log_prices)
    residual_error = sum(
        (y - (slope * i + intercept)) ** 2
        for i, y in enumerate(log_prices)
    )
    r_squared = max(0.0, 1 - residual_error / total_error) if total_error > 1e-12 else 0.0

    # 指数年化: e^(slope×250) − 1，用 250 天（≈一年交易日）
    annual_ret = math.exp(slope * 250) - 1
    slope_annual_pct = annual_ret * 100
    # 动量分 = 年化收益率(小数) × R²，对齐小薪ETF轮动
    raw_score = annual_ret * r_squared
    return raw_score, slope_annual_pct, r_squared


def _volume_ratio(bars: tuple[dict, ...], index: int) -> float | None:
    historical = [float(bar["volume"]) for bar in bars[index - 5:index]]
    current = float(bars[index]["volume"])
    volumes = [*historical, current]
    if (
        len(historical) < 5
        or any(not math.isfinite(volume) or volume <= 0 for volume in volumes)
    ):
        return None
    return current / _mean(historical)


def _rsi(closes: list[float], period: int) -> float:
    if period <= 0 or len(closes) < period + 1:
        return 50.0
    differences = [
        closes[position] - closes[position - 1]
        for position in range(len(closes) - period, len(closes))
    ]
    average_gain = _mean([max(value, 0.0) for value in differences])
    average_loss = _mean([max(-value, 0.0) for value in differences])
    if average_loss == 0:
        return 100.0
    return 100 - 100 / (1 + average_gain / average_loss)


def evaluate_momentum_signal(
    code: str,
    bars: tuple[dict, ...],
    index: int,
    config: MomentumConfig = MomentumConfig(),
) -> SignalSnapshot:
    """Evaluate one completed daily bar without consulting any later bar."""
    if not bars:
        raise ValueError("bars 不能为空")
    if index < 0 or index >= len(bars):
        raise IndexError("信号索引超出行情范围")

    available = bars[:index + 1]
    closes = [float(bar["close"]) for bar in available]
    strict_history_days = index
    formal = strict_history_days >= config.warmup_days

    raw_score, slope_pct, r_squared = _raw_rsrs(bars, index, config.rsrs_period)
    ma_value = _moving_average(closes, config.ma_period)
    ma60 = _moving_average(closes, 60)
    current_close = closes[-1]
    current_volatility = _annualized_volatility(closes, config.vol_period)

    history_start = max(0, index - config.vol_history_days)
    history_end = index - 1
    history_closes = [float(bar["close"]) for bar in bars[history_start:index]]
    historical_median = _historical_volatility_median(history_closes, config.vol_period)
    volume_ratio = _volume_ratio(bars, index)
    rsi = _rsi(closes, config.rsi_period)

    rsrs_ok = raw_score > -5.0  # 允许负分排行，仅过滤极端异常值
    ma_ok = ma_value is not None and current_close > ma_value
    volatility_ok = historical_median > 0 and (
        current_volatility <= historical_median * config.vol_limit_multiple
    )
    # 放量仅在下行日过滤（防 distribution），放量上行日通过（突破确认）
    _day_down = index > 0 and bars[index]["close"] < bars[index - 1]["close"]
    volume_ok = volume_ratio is not None and (
        volume_ratio <= config.volume_limit_multiple or not _day_down
    )
    rsi_ok = rsi <= config.rsi_limit
    passed = formal and rsrs_ok and ma_ok and volatility_ok and volume_ok and rsi_ok
    golden_cross = bool(passed and ma60 is not None and current_close > ma_value > ma60)
    strength = "strong" if golden_cross else "medium" if passed else "none"

    metrics = {
        "formal": formal,
        "strict_history_days": strict_history_days,
        "close": current_close,
        "ma": ma_value,
        "ma_period": config.ma_period,
        "ma60": ma60,
        "above_ma": ma_ok,
        "golden_cross": golden_cross,
        "current_volatility": current_volatility,
        "historical_volatility_median": historical_median,
        "volatility_ratio": current_volatility / historical_median if historical_median > 0 else None,
        "volatility_ok": volatility_ok,
        "volatility_history_index_range": (history_start, history_end),
        "volume_ratio": volume_ratio,
        "volume_ok": volume_ok,
        "day_down": _day_down,
        "rsi": rsi,
        "rsi_ok": rsi_ok,
        "rsrs_ok": rsrs_ok,
        "display_rsrs_score": raw_score,
    }
    return SignalSnapshot(
        code=code,
        date=str(bars[index]["date"]),
        raw_rsrs_score=raw_score,
        slope_annual_pct=slope_pct,
        r_squared=r_squared,
        signal_strength=strength,
        passed=passed,
        metrics=metrics,
    )


def rank_momentum_signals(signals: list[SignalSnapshot]) -> SignalSnapshot | None:
    """Return the first passing signal by raw score, R², then ascending code."""
    passing = [signal for signal in signals if signal.passed]
    if not passing:
        return None
    return min(
        passing,
        key=lambda signal: (-signal.raw_rsrs_score, -signal.r_squared, signal.code),
    )


def select_rotation_target(
    holding_code: str | None,
    snapshots: list[SignalSnapshot],
    switch_buffer: float = 1.0,
) -> dict:
    """带迟滞的轮动决策，返回 {"action", "target", "reason"}。

    action ∈ {buy, switch, hold, liquidate, none}
    target: SignalSnapshot | None

    规则（buffer≥1.0，buffer=1.0 位级退化为无迟滞现行为）：
    1. 无持仓：有通过者 → buy/switch（同一语义），否则 none
    2. 持仓在池内且已通过：全局第一 == 持仓 → hold；
       否则第一为 challenger，
       challenger.raw_score < 持仓.raw_score × buffer → hold，反之 switch
    3. 持仓在池内但未通过：有第一 → switch，否则 liquidate
    4. 持仓不在池内（如防御资产511880）：同无持仓处理
    """
    ranked = rank_momentum_signals(snapshots)  # 全局第一（仅通过者）
    snap_by_code = {s.code: s for s in snapshots}

    if holding_code is None or holding_code not in snap_by_code:
        # 无持仓或持仓不在快照内（如防御资产）
        if ranked is not None:
            return {
                "action": "buy" if holding_code is None else "switch",
                "target": ranked,
                "reason": f"排名第一 {ranked.code} RSRS={ranked.raw_rsrs_score:.2f}",
            }
        return {"action": "none", "target": None, "reason": "候选池无通过过滤的标的"}

    holding = snap_by_code[holding_code]

    if not holding.passed:
        # 持仓失效
        if ranked is not None:
            return {
                "action": "switch",
                "target": ranked,
                "reason": f"持仓 {holding_code} 信号失效，切换至 {ranked.code} RSRS={ranked.raw_rsrs_score:.2f}",
            }
        return {
            "action": "liquidate",
            "target": None,
            "reason": f"持仓 {holding_code} 信号失效，候选池无替代标的",
        }

    # 持仓仍通过 → 迟滞
    if ranked is None:
        # 理论上不应发生（holding 已通过则 ranked 至少含 holding），保守处理
        return {"action": "hold", "target": holding, "reason": "无排名可用，继续持有"}

    if ranked.code == holding_code:
        return {
            "action": "hold",
            "target": holding,
            "reason": f"持仓 {holding_code} 仍为全局第一 RSRS={holding.raw_rsrs_score:.2f}",
        }

    # challenger != holding
    challenger = ranked
    threshold = holding.raw_rsrs_score * switch_buffer
    if challenger.raw_rsrs_score < threshold:
        return {
            "action": "hold",
            "target": holding,
            "reason": (
                f"challenger {challenger.code} RSRS={challenger.raw_rsrs_score:.2f} "
                f"未超阈值 {threshold:.2f}（持仓 {holding_code} RSRS={holding.raw_rsrs_score:.2f} × {switch_buffer}）"
            ),
        }
    return {
        "action": "switch",
        "target": challenger,
        "reason": (
            f"challenger {challenger.code} RSRS={challenger.raw_rsrs_score:.2f} "
            f"超过阈值 {threshold:.2f}（持仓 {holding_code} RSRS={holding.raw_rsrs_score:.2f} × {switch_buffer}）"
        ),
    }
