import json
import math
import unittest
from dataclasses import replace
from pathlib import Path

from tools.momentum_core import (
    MomentumConfig,
    SignalSnapshot,
    evaluate_momentum_signal,
    rank_momentum_signals,
)


FIXTURE = Path(__file__).parent / "fixtures" / "momentum_280_days.json"


def make_bars(count=280, *, drift=0.0008):
    bars = []
    previous = 100.0
    for index in range(count):
        close = 100.0 * math.exp(
            drift * index + 0.012 * math.sin(index * 0.7) + 0.005 * math.sin(index * 0.17)
        )
        bars.append({
            "date": f"2025-{index + 1:03d}",
            "open": previous,
            "high": max(previous, close) * 1.003,
            "low": min(previous, close) * 0.997,
            "close": close,
            "volume": 1_000_000 + (index % 5) * 10_000,
        })
        previous = close
    return tuple(bars)


class MomentumCoreTests(unittest.TestCase):
    def setUp(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.bars = tuple(payload["bars"])
        self.config = MomentumConfig()

    def test_requires_252_strictly_historical_trading_days(self):
        too_early = evaluate_momentum_signal("AAA", self.bars, 251, self.config)
        first_formal = evaluate_momentum_signal("AAA", self.bars, 252, self.config)

        self.assertFalse(too_early.metrics["formal"])
        self.assertFalse(too_early.passed)
        self.assertEqual("none", too_early.signal_strength)
        self.assertTrue(first_formal.metrics["formal"])
        self.assertEqual(252, first_formal.metrics["strict_history_days"])

    def test_future_bars_do_not_change_historical_result(self):
        signal_index = 260
        original = evaluate_momentum_signal("AAA", self.bars, signal_index, self.config)
        changed = list(self.bars)
        for index in range(signal_index + 1, len(changed)):
            changed[index] = dict(changed[index], close=changed[index]["close"] * 50, volume=1)

        after_future_change = evaluate_momentum_signal(
            "AAA", tuple(changed), signal_index, self.config
        )
        self.assertEqual(original, after_future_change)

    def test_historical_volatility_uses_only_previous_252_days(self):
        signal_index = 260
        original = evaluate_momentum_signal("AAA", self.bars, signal_index, self.config)
        changed = list(self.bars)
        changed[0] = dict(changed[0], close=changed[0]["close"] * 10)

        result = evaluate_momentum_signal("AAA", tuple(changed), signal_index, self.config)
        self.assertEqual(
            original.metrics["historical_volatility_median"],
            result.metrics["historical_volatility_median"],
        )
        self.assertEqual((8, 259), result.metrics["volatility_history_index_range"])

    def test_custom_ma_period_metrics_use_only_neutral_names(self):
        config = replace(self.config, ma_period=55)

        snapshot = evaluate_momentum_signal(
            "AAA", self.bars, len(self.bars) - 1, config
        )

        self.assertEqual(55, snapshot.metrics["ma_period"])
        self.assertIn("ma", snapshot.metrics)
        self.assertIn("above_ma", snapshot.metrics)
        self.assertNotIn("ma20", snapshot.metrics)
        self.assertNotIn("above_ma20", snapshot.metrics)

    def test_raw_scores_above_display_cap_still_rank_by_raw_score(self):
        base = SignalSnapshot(
            code="BBB", date="2026-01-01", raw_rsrs_score=51.0,
            slope_annual_pct=60.0, r_squared=0.9,
            signal_strength="medium", passed=True, metrics={"display_rsrs_score": 50.0},
        )
        higher = replace(base, code="CCC", raw_rsrs_score=80.0)
        tie_b = replace(base, code="BBB", raw_rsrs_score=80.0, r_squared=0.8)
        tie_a = replace(base, code="AAA", raw_rsrs_score=80.0, r_squared=0.8)

        self.assertEqual("CCC", rank_momentum_signals([base, tie_b, higher]).code)
        self.assertEqual("AAA", rank_momentum_signals([tie_b, tie_a]).code)

    def test_strength_vocabulary_has_no_weak_state(self):
        strong = evaluate_momentum_signal("STRONG", self.bars, 279, self.config)

        medium_bars = list(make_bars(drift=-0.0005))
        for index in range(275, 280):
            factor = math.exp(0.004 * (index - 274))
            medium_bars[index] = dict(
                medium_bars[index], close=medium_bars[274]["close"] * factor,
            )
        medium = evaluate_momentum_signal("MEDIUM", tuple(medium_bars), 279, self.config)

        none_bars = list(self.bars)
        # 下行日 + 放量（distribution）→ 触发量能过滤
        none_bars[279] = dict(
            none_bars[279],
            close=none_bars[278]["close"] * 0.985,
            volume=20_000_000,
        )
        none = evaluate_momentum_signal("NONE", tuple(none_bars), 279, self.config)

        # 上行日放量 = 突破确认，不过滤（v3.0 对齐扫描器行为）
        surge_up_bars = list(self.bars)
        surge_up_bars[279] = dict(surge_up_bars[279], volume=20_000_000)
        surge_up = evaluate_momentum_signal("UP", tuple(surge_up_bars), 279, self.config)

        self.assertEqual("strong", strong.signal_strength)
        self.assertEqual("medium", medium.signal_strength)
        self.assertEqual("none", none.signal_strength)
        self.assertTrue(surge_up.metrics["volume_ok"])
        self.assertNotIn("weak", {strong.signal_strength, medium.signal_strength, none.signal_strength})

    def test_zero_historical_volume_baseline_fails_closed(self):
        bars = list(make_bars(300))
        for index in range(294, 299):
            bars[index] = dict(bars[index], volume=0)
        bars[299] = dict(bars[299], volume=1_000_000)

        snapshot = evaluate_momentum_signal(
            "159920", tuple(bars), len(bars) - 1, self.config
        )

        self.assertFalse(snapshot.metrics["volume_ok"])
        self.assertIsNone(snapshot.metrics["volume_ratio"])

    def test_nonfinite_or_invalid_volume_fails_closed(self):
        cases = (
            ("historical_inf", 298, float("inf")),
            ("historical_nan", 298, float("nan")),
            ("current_zero", 299, 0),
            ("current_inf", 299, float("inf")),
            ("current_nan", 299, float("nan")),
        )
        for name, index, volume in cases:
            with self.subTest(name=name):
                bars = list(make_bars(300))
                bars[index] = dict(bars[index], volume=volume)

                snapshot = evaluate_momentum_signal(
                    "159920", tuple(bars), len(bars) - 1, self.config
                )

                self.assertFalse(snapshot.metrics["volume_ok"])
                self.assertIsNone(snapshot.metrics["volume_ratio"])

    def test_rank_returns_first_passing_signal_or_cash(self):
        passing = SignalSnapshot("AAA", "2026-01-01", 10, 10, 0.8, "medium", True, {})
        rejected = SignalSnapshot("BBB", "2026-01-01", 100, 100, 1, "none", False, {})
        self.assertEqual(passing, rank_momentum_signals([rejected, passing]))
        self.assertIsNone(rank_momentum_signals([rejected]))


if __name__ == "__main__":
    unittest.main()
