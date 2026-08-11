import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from tools import momentum_signal
from tools.momentum_core import MomentumConfig, evaluate_momentum_signal, rank_momentum_signals
from tools.momentum_etf_backtest import check_signal, rank_signals
from tools.strategy_models import RunStatus, StrategyError


main = momentum_signal.main
scan = momentum_signal.scan


FIXTURE = Path(__file__).parent / "fixtures" / "momentum_280_days.json"
SHANGHAI = ZoneInfo("Asia/Shanghai")
POOL = {"AAA": "Alpha ETF", "BBB": "Beta ETF"}


class MomentumSignalParityTests(unittest.TestCase):
    def setUp(self):
        self.bars = tuple(json.loads(FIXTURE.read_text(encoding="utf-8"))["bars"])
        self.config = MomentumConfig()
        self.index = len(self.bars) - 1

    def frozen_series(self, end_date="2026-07-29"):
        result = {}
        for code in POOL:
            bars = list(self.bars)
            bars[-1] = dict(bars[-1], date=end_date)
            result[code] = SimpleNamespace(
                bars=tuple(bars), manifest=SimpleNamespace(end_date=end_date)
            )
        return result

    def test_scanner_and_backtest_match_shared_core_metrics(self):
        expected = evaluate_momentum_signal("AAA", self.bars, self.index, self.config)
        series = SimpleNamespace(
            bars=self.bars,
            manifest=SimpleNamespace(end_date=self.bars[-1]["date"]),
        )
        scanner = scan(
            {"AAA": "Alpha"}, series_by_code={"AAA": series}, market_closed=True
        )["items"][0]
        backtest = check_signal("AAA", list(self.bars), self.index, config=self.config)

        for key in ("ma", "current_volatility", "historical_volatility_median", "volume_ratio", "rsi"):
            self.assertAlmostEqual(expected.metrics[key], scanner["metrics"][key], places=12)
            self.assertAlmostEqual(expected.metrics[key], backtest.metrics[key], places=12)
        self.assertEqual(expected.raw_rsrs_score, scanner["raw_rsrs_score"])
        self.assertEqual(expected.raw_rsrs_score, backtest.raw_rsrs_score)
        self.assertEqual(expected.passed, scanner["pass"])
        self.assertEqual(expected.passed, backtest.passed)

    def test_scanner_marks_intraday_bar_provisional_and_never_formal(self):
        result = scan(
            POOL,
            series_by_code=self.frozen_series("2026-07-30"),
            now=datetime(2026, 7, 30, 14, 0, tzinfo=SHANGHAI),
        )

        self.assertEqual(RunStatus.PROVISIONAL, result["status"])
        self.assertIsNone(result["selected"])
        self.assertTrue(all(item["provisional"] for item in result["items"]))
        self.assertTrue(all(not item["formal"] for item in result["items"]))
        self.assertTrue(all(not item["pass"] for item in result["items"]))

    def test_market_close_requires_shanghai_weekday_and_finalization_buffer(self):
        date = "2026-07-30"

        self.assertFalse(momentum_signal.determine_market_closed(
            date, now=datetime(2026, 7, 30, 15, 4, tzinfo=SHANGHAI)
        ))
        self.assertTrue(momentum_signal.determine_market_closed(
            date, now=datetime(2026, 7, 30, 15, 5, tzinfo=SHANGHAI)
        ))
        self.assertFalse(momentum_signal.determine_market_closed(
            "2026-08-01", now=datetime(2026, 8, 1, 16, 0, tzinfo=SHANGHAI)
        ))
        self.assertTrue(momentum_signal.determine_market_closed(
            "2026-07-29", now=datetime(2026, 7, 30, 9, 0, tzinfo=SHANGHAI)
        ))

    def test_missing_pool_member_makes_scan_unknown(self):
        series = self.frozen_series()
        del series["BBB"]

        result = scan(POOL, series_by_code=series, market_closed=True)

        self.assertEqual(RunStatus.UNKNOWN, result["status"])
        self.assertFalse(result["pool_complete"])
        self.assertIsNone(result["selected"])
        self.assertEqual(2, len(result["items"]))
        self.assertEqual(1, len(result["errors"]))
        self.assertIsInstance(result["errors"][0], StrategyError)

    def test_mixed_manifest_dates_make_scan_unknown(self):
        series = self.frozen_series()
        series["BBB"].manifest.end_date = "2026-07-28"

        result = scan(POOL, series_by_code=series, market_closed=True)

        self.assertEqual(RunStatus.UNKNOWN, result["status"])
        self.assertFalse(result["pool_complete"])
        self.assertIsNone(result["selected"])
        self.assertIsNone(result["as_of"])
        self.assertTrue(any(error.stage == "cross_section" for error in result["errors"]))

    def test_mixed_historical_dates_are_unknown_but_not_provisional(self):
        series = self.frozen_series("2026-07-29")
        older_bars = list(series["BBB"].bars)
        older_bars[-1] = dict(older_bars[-1], date="2026-07-28")
        series["BBB"].bars = tuple(older_bars)
        series["BBB"].manifest.end_date = "2026-07-28"

        result = scan(
            POOL,
            series_by_code=series,
            now=datetime(2026, 7, 30, 9, 0, tzinfo=SHANGHAI),
        )

        self.assertEqual(RunStatus.UNKNOWN, result["status"])
        self.assertTrue(all(not item["provisional"] for item in result["items"]))

    def test_nonformal_scan_preserves_pool_order_without_ranking(self):
        pool = {"BBB": "Beta ETF", "AAA": "Alpha ETF"}
        series = self.frozen_series("2026-07-30")

        result = scan(
            pool,
            series_by_code=series,
            now=datetime(2026, 7, 30, 14, 0, tzinfo=SHANGHAI),
        )

        self.assertEqual(RunStatus.PROVISIONAL, result["status"])
        self.assertEqual(["BBB", "AAA"], [item["code"] for item in result["items"]])

    def test_unknown_scan_preserves_pool_order_without_ranking(self):
        pool = {"BBB": "Beta ETF", "AAA": "Alpha ETF"}
        series = self.frozen_series()
        del series["AAA"]

        result = scan(pool, series_by_code=series, market_closed=True)

        self.assertEqual(RunStatus.UNKNOWN, result["status"])
        self.assertEqual(["BBB", "AAA"], [item["code"] for item in result["items"]])

    def test_evaluation_failure_revokes_all_formal_signals(self):
        series = self.frozen_series()
        broken = list(series["BBB"].bars)
        broken[-1] = {"date": "2026-07-29"}
        series["BBB"].bars = tuple(broken)

        result = scan(POOL, series_by_code=series, market_closed=True)

        self.assertEqual(RunStatus.UNKNOWN, result["status"])
        self.assertFalse(result["pool_complete"])
        self.assertIsNone(result["selected"])
        self.assertTrue(all(not item["pass"] for item in result["items"]))
        self.assertTrue(all(not item["formal"] for item in result["items"]))

    def test_short_history_pool_member_makes_scan_unknown(self):
        series = self.frozen_series()
        series["BBB"].bars = series["BBB"].bars[-100:]

        result = scan(POOL, series_by_code=series, market_closed=True)

        self.assertEqual(RunStatus.UNKNOWN, result["status"])
        self.assertFalse(result["pool_complete"])
        self.assertIsNone(result["selected"])
        self.assertTrue(all(not item["pass"] for item in result["items"]))
        self.assertTrue(any(error.stage == "history" for error in result["errors"]))

    def test_complete_formal_scan_returns_ranked_selection(self):
        result = scan(POOL, series_by_code=self.frozen_series(), market_closed=True)

        expected_status = (
            RunStatus.OK if any(item["pass"] for item in result["items"])
            else RunStatus.NO_SIGNAL
        )
        self.assertEqual(expected_status, result["status"])
        self.assertTrue(result["pool_complete"])
        self.assertEqual("2026-07-29", result["as_of"])
        if result["status"] == RunStatus.OK:
            self.assertEqual(result["items"][0]["code"], result["selected"]["code"])
        else:
            self.assertIsNone(result["selected"])

    def test_scan_loads_each_pool_member_once(self):
        series = self.frozen_series()

        with patch("tools.momentum_signal.load_etf_series", side_effect=series.__getitem__) as loader:
            scan(POOL, market_closed=True)

        self.assertEqual(len(POOL), loader.call_count)

    def test_scanner_and_backtest_use_identical_ranking(self):
        signals = [
            evaluate_momentum_signal(code, self.bars, self.index, self.config)
            for code in ("BBB", "AAA")
        ]
        expected = rank_momentum_signals(signals)
        self.assertEqual(expected, rank_signals(list(signals)))

    def test_custom_ma_period_uses_neutral_fields(self):
        result = scan(
            POOL,
            ma_period=55,
            series_by_code=self.frozen_series(),
            market_closed=True,
        )

        for item in result["items"]:
            self.assertEqual(55, item["ma_period"])
            self.assertEqual(55, item["metrics"]["ma_period"])
            self.assertAlmostEqual(item["metrics"]["ma"], item["ma"], places=4)
            self.assertEqual(item["metrics"]["above_ma"], item["above_ma"])
            self.assertNotIn("ma20", item)
            self.assertNotIn("above_ma20", item)
            self.assertNotIn("ma20", item["metrics"])
            self.assertNotIn("above_ma20", item["metrics"])

    def test_cli_json_is_strict_and_forwards_periods(self):
        envelope = {
            "status": RunStatus.UNKNOWN,
            "as_of": None,
            "items": [],
            "errors": [StrategyError("AAA", "load", None, "missing")],
            "selected": None,
            "pool_complete": False,
        }
        output = io.StringIO()

        with patch(
            "sys.argv",
            ["momentum_signal.py", "--json", "--momentum", "33", "--ma-period", "55"],
        ), patch("tools.momentum_signal.scan", return_value=envelope) as scanner, redirect_stdout(output):
            main()

        parsed = json.loads(output.getvalue())
        self.assertEqual("unknown", parsed["status"])
        self.assertEqual("missing", parsed["errors"][0]["message"])
        self.assertEqual(33, scanner.call_args.args[1])
        self.assertEqual(55, scanner.call_args.kwargs["ma_period"])

    def test_json_entry_mode_is_structured(self):
        stop_loss = {
            "entry_price": 1.0,
            "current_price": 0.9,
            "loss_pct": -10.0,
            "stop_loss_line": 0.92,
            "triggered": True,
        }
        output = io.StringIO()

        with patch(
            "sys.argv", ["momentum_signal.py", "--json", "--entry", "AAA", "1.0"]
        ), patch(
            "tools.momentum_signal.check_stop_loss", return_value=stop_loss
        ), redirect_stdout(output):
            main()

        self.assertEqual(stop_loss, json.loads(output.getvalue()))

    def test_human_nonformal_status_does_not_issue_formal_advice(self):
        item = {
            "code": "AAA", "name": "Alpha ETF", "date": "2026-07-30",
            "close": 1.0, "rsrs_score": 1.0, "slope_annual_pct": 1.0,
            "r_squared": 1.0, "ma": 1.0, "ma_period": 20, "ma60": 1.0,
            "above_ma": True, "above_ma60": True, "golden_cross": False,
            "vol_20d": 1.0, "vol_median": 1.0, "vol_ok": True,
            "vol_ratio": 1.0, "volume_surge": False, "rsi": 50.0,
            "rsi_overbought": False, "pct_5d": 1.0, "pct_20d": 1.0,
            "pass": False, "formal": False, "provisional": True,
            "signal_strength": "none",
        }
        for status in (RunStatus.UNKNOWN, RunStatus.PROVISIONAL):
            envelope = {
                "status": status, "as_of": "2026-07-30", "items": [item],
                "errors": [], "selected": None,
                "pool_complete": status == RunStatus.PROVISIONAL,
            }
            output = io.StringIO()
            with self.subTest(status=status), patch(
                "sys.argv", ["momentum_signal.py", "--pool", "AAA"]
            ), patch(
                "tools.momentum_signal.scan", return_value=envelope
            ), redirect_stdout(output):
                main()
            text = output.getvalue()
            self.assertIn("无法形成正式结论", text)
            self.assertNotIn("无买入信号", text)
            self.assertNotIn("切换至防御资产", text)
            self.assertNotIn("  1.", text)

    def test_human_output_uses_configured_ma_label(self):
        item = {
            "code": "AAA", "name": "Alpha ETF", "date": "2026-07-29",
            "close": 1.0, "rsrs_score": 1.0, "slope_annual_pct": 1.0,
            "r_squared": 1.0, "ma": 1.0, "ma_period": 55, "ma60": 1.0,
            "above_ma": True, "above_ma60": True, "golden_cross": False,
            "vol_20d": 1.0, "vol_median": 1.0, "vol_ok": True,
            "vol_ratio": 1.0, "volume_surge": False, "rsi": 50.0,
            "rsi_overbought": False, "pct_5d": 1.0, "pct_20d": 1.0,
            "pass": False, "formal": True, "provisional": False,
            "signal_strength": "none",
        }
        envelope = {
            "status": RunStatus.NO_SIGNAL, "as_of": "2026-07-29",
            "items": [item], "errors": [], "selected": None,
            "pool_complete": True,
        }
        output = io.StringIO()

        with patch(
            "sys.argv", ["momentum_signal.py", "--pool", "AAA", "--ma-period", "55"]
        ), patch(
            "tools.momentum_signal.scan", return_value=envelope
        ), redirect_stdout(output):
            main()

        text = output.getvalue()
        self.assertIn("MA55", text)
        self.assertNotIn("MA20", text)

    def test_human_output_keeps_etf_titles_and_complete_distribution(self):
        items = []
        for code, name, strength in (
            ("AAA", "Alpha ETF", "strong"),
            ("BBB", "Beta ETF", "medium"),
            ("CCC", "Gamma ETF", "none"),
        ):
            items.append({
                "code": code,
                "name": name,
                "date": "2026-07-29",
                "close": 1.0,
                "rsrs_score": 1.0,
                "slope_annual_pct": 1.0,
                "r_squared": 1.0,
                "ma": 1.0,
                "ma_period": 20,
                "ma60": 1.0,
                "above_ma": True,
                "above_ma60": True,
                "golden_cross": strength == "strong",
                "vol_20d": 1.0,
                "vol_median": 1.0,
                "vol_ok": True,
                "vol_ratio": 1.0,
                "volume_surge": False,
                "rsi": 50.0,
                "rsi_overbought": False,
                "pct_5d": 1.0,
                "pct_20d": 1.0,
                "pass": strength != "none",
                "formal": True,
                "provisional": False,
                "signal_strength": strength,
            })
        envelope = {
            "status": RunStatus.OK,
            "as_of": "2026-07-29",
            "items": items,
            "errors": [],
            "selected": items[0],
            "pool_complete": True,
        }
        output = io.StringIO()

        with patch("sys.argv", ["momentum_signal.py", "--pool", "AAA,BBB,CCC"]), patch(
            "tools.momentum_signal.scan", return_value=envelope
        ), redirect_stdout(output):
            main()

        text = output.getvalue()
        self.assertIn("Alpha ETF", text)
        self.assertIn("Beta ETF", text)
        self.assertIn("Gamma ETF", text)
        self.assertIn("强信号: 1", text)
        self.assertIn("中等: 1", text)
        self.assertIn("无信号: 1", text)


if __name__ == "__main__":
    unittest.main()
