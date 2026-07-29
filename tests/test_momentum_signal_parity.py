import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.momentum_core import MomentumConfig, evaluate_momentum_signal, rank_momentum_signals
from tools.momentum_etf_backtest import check_signal, rank_signals
from tools.momentum_signal import scan


FIXTURE = Path(__file__).parent / "fixtures" / "momentum_280_days.json"


class MomentumSignalParityTests(unittest.TestCase):
    def setUp(self):
        self.bars = tuple(json.loads(FIXTURE.read_text(encoding="utf-8"))["bars"])
        self.config = MomentumConfig()
        self.index = len(self.bars) - 1

    def test_scanner_and_backtest_match_shared_core_metrics(self):
        expected = evaluate_momentum_signal("AAA", self.bars, self.index, self.config)
        with patch("tools.momentum_signal.load_etf_series") as loader:
            loader.return_value.bars = self.bars
            scanner = scan({"AAA": "Alpha"}, config=self.config, market_closed=True)[0]
        backtest = check_signal("AAA", list(self.bars), self.index, config=self.config)

        for key in ("ma", "current_volatility", "historical_volatility_median", "volume_ratio", "rsi"):
            self.assertAlmostEqual(expected.metrics[key], scanner["metrics"][key], places=12)
            self.assertAlmostEqual(expected.metrics[key], backtest.metrics[key], places=12)
        self.assertEqual(expected.raw_rsrs_score, scanner["raw_rsrs_score"])
        self.assertEqual(expected.raw_rsrs_score, backtest.raw_rsrs_score)
        self.assertEqual(expected.passed, scanner["pass"])
        self.assertEqual(expected.passed, backtest.passed)

    def test_scanner_marks_intraday_bar_provisional_and_never_formal(self):
        with patch("tools.momentum_signal.load_etf_series") as loader:
            loader.return_value.bars = self.bars
            result = scan({"AAA": "Alpha"}, config=self.config, market_closed=False)[0]
        self.assertTrue(result["provisional"])
        self.assertFalse(result["formal"])
        self.assertFalse(result["pass"])

    def test_scanner_and_backtest_use_identical_ranking(self):
        signals = [
            evaluate_momentum_signal(code, self.bars, self.index, self.config)
            for code in ("BBB", "AAA")
        ]
        expected = rank_momentum_signals(signals)
        self.assertEqual(expected, rank_signals(list(signals)))


if __name__ == "__main__":
    unittest.main()
