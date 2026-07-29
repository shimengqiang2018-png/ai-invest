"""Shared, causal momentum signal calculations for scanners and backtests."""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class MomentumConfig:
    rsrs_period: int = 20
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
    return math.sqrt(variance) * math.sqrt(252)


def _historical_volatility_median(closes: list[float], period: int) -> float:
    vols = [
        _annualized_volatility(closes[: end + 1], period)
        for end in range(period, len(closes))
    ]
    usable = [value for value in vols if value > 0]
    return median(usable) if usable else 0.0


def _raw_rsrs(bars: tuple[dict, ...], index: int, period: int) -> tuple[float, float, float]:
    if period <= 0 or index < period:
        return 0.0, 0.0, 0.0
    window = bars[index - period:index + 1]
    log_prices = [math.log(float(bar["close"])) for bar in window]
    size = len(log_prices)
    weights = [1 + position / (size - 1) for position in range(size)]
    x_values = list(range(size))
    weight_sum = sum(weights)
    weighted_x = sum(weight * x for weight, x in zip(weights, x_values))
    weighted_y = sum(weight * y for weight, y in zip(weights, log_prices))
    weighted_xx = sum(weight * x * x for weight, x in zip(weights, x_values))
    weighted_xy = sum(
        weight * x * y for weight, x, y in zip(weights, x_values, log_prices)
    )
    denominator = weight_sum * weighted_xx - weighted_x * weighted_x
    if abs(denominator) < 1e-12:
        return 0.0, 0.0, 0.0
    slope = (weight_sum * weighted_xy - weighted_x * weighted_y) / denominator
    intercept = (weighted_y - slope * weighted_x) / weight_sum
    y_mean = weighted_y / weight_sum
    total_error = sum(
        weight * (y - y_mean) ** 2 for weight, y in zip(weights, log_prices)
    )
    residual_error = sum(
        weight * (y - (slope * x + intercept)) ** 2
        for weight, x, y in zip(weights, x_values, log_prices)
    )
    r_squared = max(0.0, 1 - residual_error / total_error) if total_error > 1e-12 else 0.0
    slope_annual_pct = slope * 252 * 100
    raw_score = max(0.0, slope_annual_pct * r_squared)
    return raw_score, slope_annual_pct, r_squared


def _volume_ratio(bars: tuple[dict, ...], index: int) -> float:
    if index < 5:
        return 1.0
    previous = [float(bar["volume"]) for bar in bars[index - 5:index]]
    average = _mean(previous)
    return float(bars[index]["volume"]) / average if average > 0 else 1.0


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

    rsrs_ok = raw_score > 0
    ma_ok = ma_value is not None and current_close > ma_value
    volatility_ok = historical_median > 0 and (
        current_volatility <= historical_median * config.vol_limit_multiple
    )
    volume_ok = volume_ratio <= config.volume_limit_multiple
    rsi_ok = rsi <= config.rsi_limit
    passed = formal and rsrs_ok and ma_ok and volatility_ok and volume_ok and rsi_ok
    golden_cross = bool(passed and ma60 is not None and current_close > ma_value > ma60)
    strength = "strong" if golden_cross else "medium" if passed else "none"

    metrics = {
        "formal": formal,
        "strict_history_days": strict_history_days,
        "close": current_close,
        "ma": ma_value,
        "ma20": ma_value,
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
        "rsi": rsi,
        "rsi_ok": rsi_ok,
        "rsrs_ok": rsrs_ok,
        "display_rsrs_score": min(50.0, raw_score),
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
