import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tools.etf_market_data import (
    MarketDataManifest,
    MarketDataQualityError,
    MarketDataSeries,
    load_etf_series,
    truncate_series,
    validate_market_data,
)


FIXTURE = Path(__file__).parent / "fixtures" / "etf_adjustment_513100.json"


def content_hash(bars):
    payload = json.dumps(bars, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_series(bars, **manifest_overrides):
    manifest = MarketDataManifest(
        schema_version=2,
        code="513100",
        source="eastmoney",
        adjustment="qfq",
        volume_adjustment="none",
        fetched_at="2026-07-28T00:00:00Z",
        start_date=bars[0]["date"],
        end_date=bars[-1]["date"],
        bar_count=len(bars),
        content_hash=content_hash(bars),
        adjustment_verified=True,
        verification_source="tencent_qfqday",
        verification_version=1,
        overlap_start=bars[0]["date"],
        overlap_end=bars[-1]["date"],
        overlap_count=len(bars),
        verification_tolerance=0.03,
        max_return_error=0.0,
        overlap_content_hash=content_hash(bars),
    )
    return MarketDataSeries(tuple(bars), replace(manifest, **manifest_overrides))


class MarketDataContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_legacy_qfq_cache_is_a_miss_without_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "etf_kline_513100_qfq_2000.json"
            legacy.write_text(json.dumps({"code": "513100", "data": self.fixture["qfq_bars"]}), encoding="utf-8")

            with self.assertRaises(ConnectionError):
                load_etf_series(
                    "513100",
                    cache_dir=directory,
                    transport=lambda _url: (_ for _ in ()).throw(ConnectionError("offline")),
                )

            self.assertTrue(legacy.exists())

    def test_raw_share_conversion_break_is_rejected(self):
        with self.assertRaisesRegex(MarketDataQualityError, "20%"):
            validate_market_data(make_series(self.fixture["raw_bars"]))

    def test_qfq_fixture_is_continuous(self):
        validate_market_data(make_series(self.fixture["qfq_bars"]))

    def test_duplicate_date_is_rejected(self):
        bars = list(self.fixture["qfq_bars"])
        bars.insert(2, dict(bars[1]))
        with self.assertRaisesRegex(MarketDataQualityError, "日期"):
            validate_market_data(make_series(bars))

    def test_invalid_ohlc_is_rejected(self):
        bars = [dict(bar) for bar in self.fixture["qfq_bars"]]
        bars[1]["high"] = bars[1]["low"] - 0.01
        with self.assertRaisesRegex(MarketDataQualityError, "OHLC"):
            validate_market_data(make_series(bars))

    def test_non_positive_price_and_negative_volume_are_rejected(self):
        for field, value in (("close", 0), ("volume", -1)):
            with self.subTest(field=field):
                bars = [dict(bar) for bar in self.fixture["qfq_bars"]]
                bars[1][field] = value
                with self.assertRaises(MarketDataQualityError):
                    validate_market_data(make_series(bars))

    def test_modified_content_fails_hash_validation(self):
        series = make_series(self.fixture["qfq_bars"])
        changed = [dict(bar) for bar in series.bars]
        changed[-1]["close"] += 0.01
        with self.assertRaisesRegex(MarketDataQualityError, "hash"):
            validate_market_data(MarketDataSeries(tuple(changed), series.manifest))

    def test_provider_declared_data_is_validated_before_return(self):
        def transport(url):
            if "push2his.eastmoney.com" in url:
                return {"data": {"klines": [
                    "2022-01-10,1.000,1.000,1.000,1.000,1200000",
                    "2022-01-11,1.300,1.300,1.300,1.300,1180000",
                ]}}
            return {"data": {"sh513100": {"qfqday": []}}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MarketDataQualityError, "20%"):
                load_etf_series(
                    "513100", count=2, cache_dir=directory, transport=transport
                )

    def test_eastmoney_without_tencent_overlap_is_provider_declared(self):
        def transport(url):
            if "push2his.eastmoney.com" in url:
                return {"data": {"klines": [
                    "2022-01-10,0.997,1.009,1.017,0.993,1200000",
                    "2022-01-11,1.009,1.015,1.021,1.001,1180000",
                ]}}
            return {"data": {"sh513100": {"qfqday": []}}}

        with tempfile.TemporaryDirectory() as directory:
            series = load_etf_series(
                "513100", count=2, cache_dir=directory, transport=transport
            )
        self.assertEqual(series.manifest.source, "eastmoney")
        self.assertFalse(series.manifest.adjustment_verified)
        self.assertEqual(
            series.manifest.verification_source,
            "provider_declared_fqt1",
        )

    def test_tencent_standalone_is_provider_declared_not_cross_verified(self):
        def transport(url):
            if "push2his.eastmoney.com" in url:
                raise ConnectionError("eastmoney offline")
            return {"data": {"sh513100": {"qfqday": [
                ["2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"],
                ["2022-01-11", "1.009", "1.015", "1.021", "1.001", "1180000"],
            ]}}}

        with tempfile.TemporaryDirectory() as directory:
            series = load_etf_series(
                "513100", count=2, cache_dir=directory, transport=transport
            )
        self.assertEqual(series.manifest.source, "tencent")
        self.assertFalse(series.manifest.adjustment_verified)
        self.assertEqual(
            series.manifest.verification_source,
            "provider_declared_qfqday",
        )

    def test_tencent_verification_is_fail_closed(self):
        def transport(url):
            if "push2his.eastmoney.com" in url:
                return {"data": {"klines": [
                    "2022-01-10,0.997,1.009,1.017,0.993,1200000",
                    "2022-01-11,1.009,1.015,1.021,1.001,1180000",
                ]}}
            return {"data": {"sh513100": {"qfqday": [
                ["2022-01-10", "1.500", "1.500", "1.500", "1.500", "1200000"],
                ["2022-01-11", "1.800", "1.800", "1.800", "1.800", "1180000"],
            ]}}}

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MarketDataQualityError, "腾讯"):
                load_etf_series("513100", count=2, cache_dir=directory, transport=transport)
            self.assertEqual([], list(Path(directory).glob("etf_v2_*.json")))

    def test_tencent_non_finite_or_non_positive_values_are_rejected(self):
        for field, value in (("close", "nan"), ("close", "inf"), ("open", "0")):
            with self.subTest(field=field, value=value):
                row = ["2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"]
                row[{"open": 1, "close": 2}[field]] = value

                def transport(url):
                    if "push2his.eastmoney.com" in url:
                        return {"data": {"klines": [
                            "2022-01-10,0.997,1.009,1.017,0.993,1200000",
                        ]}}
                    return {"data": {"sh513100": {"qfqday": [row]}}}

                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaisesRegex(MarketDataQualityError, "腾讯"):
                        load_etf_series("513100", count=1, cache_dir=directory, transport=transport)
                    self.assertEqual([], list(Path(directory).glob("etf_v2_*.json")))

    def test_tencent_invalid_date_is_rejected(self):
        def transport(url):
            if "push2his.eastmoney.com" in url:
                return {"data": {"klines": [
                    "2022-01-10,0.997,1.009,1.017,0.993,1200000",
                ]}}
            return {"data": {"sh513100": {"qfqday": [
                ["not-a-date", "0.997", "1.009", "1.017", "0.993", "1200000"],
            ]}}}

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MarketDataQualityError, "腾讯"):
                load_etf_series("513100", count=1, cache_dir=directory, transport=transport)

    def test_v2_cache_round_trip_and_atomic_write(self):
        calls = []

        def transport(url):
            calls.append(url)
            if "push2his.eastmoney.com" in url:
                return {
                    "data": {
                        "klines": [
                            "2022-01-10,0.997,1.009,1.017,0.993,1200000",
                            "2022-01-11,1.009,1.015,1.021,1.001,1180000",
                        ]
                    }
                }
            return {"data": {"sh513100": {"qfqday": [
                ["2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"],
                ["2022-01-11", "1.009", "1.015", "1.021", "1.001", "1180000"],
            ]}}}

        with tempfile.TemporaryDirectory() as directory:
            first = load_etf_series("513100", count=2, as_of="2022-01-11", cache_dir=directory, transport=transport)
            cache_files = list(Path(directory).glob("etf_v2_eastmoney_513100_qfq_2.json"))
            temp_files = list(Path(directory).glob("*.tmp"))
            self.assertEqual(2, first.manifest.schema_version)
            self.assertEqual("eastmoney", first.manifest.source)
            self.assertEqual("qfq", first.manifest.adjustment)
            self.assertEqual(1, len(cache_files))
            self.assertEqual([], temp_files)

            cached = load_etf_series(
                "513100",
                count=2,
                as_of="2022-01-11",
                cache_dir=directory,
                transport=lambda _url: (_ for _ in ()).throw(ConnectionError("offline")),
            )
            self.assertEqual(first, cached)
            self.assertTrue(first.manifest.adjustment_verified)
            self.assertEqual("tencent_qfqday", first.manifest.verification_source)
            self.assertEqual(2, first.manifest.overlap_count)
            self.assertEqual(0.03, first.manifest.verification_tolerance)
            self.assertEqual(0.0, first.manifest.max_return_error)
            self.assertEqual(64, len(first.manifest.overlap_content_hash))
            self.assertTrue(any("fqt=1" in url for url in calls))

    def test_default_mode_does_not_fallback_to_existing_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "etf_v2_eastmoney_513100_qfq_5.json"
            series = make_series(self.fixture["qfq_bars"])
            cache.write_text(json.dumps({"manifest": series.manifest.__dict__, "bars": list(series.bars)}), encoding="utf-8")

            with self.assertRaises(ConnectionError):
                load_etf_series(
                    "513100", count=5, cache_dir=directory,
                    transport=lambda _url: (_ for _ in ()).throw(ConnectionError("offline")),
                )

    def test_cache_before_as_of_is_refreshed_and_not_used_when_refresh_fails(self):
        calls = []

        def seed_transport(url):
            if "push2his.eastmoney.com" in url:
                return {"data": {"klines": [
                    "2022-01-10,0.997,1.009,1.017,0.993,1200000",
                    "2022-01-11,1.009,1.015,1.021,1.001,1180000",
                ]}}
            return {"data": {"sh513100": {"qfqday": [
                ["2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"],
                ["2022-01-11", "1.009", "1.015", "1.021", "1.001", "1180000"],
            ]}}}

        with tempfile.TemporaryDirectory() as directory:
            load_etf_series("513100", count=2, as_of="2022-01-11", cache_dir=directory, transport=seed_transport)

            def offline(url):
                calls.append(url)
                raise ConnectionError("offline")

            with self.assertRaises(ConnectionError):
                load_etf_series("513100", count=2, as_of="2022-01-12", cache_dir=directory, transport=offline)
            self.assertTrue(calls)

    def test_invalid_or_short_v2_cache_is_not_used_after_primary_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "etf_v2_eastmoney_513100_qfq_5.json"
            series = make_series(self.fixture["qfq_bars"][:2])
            cache.write_text(json.dumps({"manifest": series.manifest.__dict__, "bars": list(series.bars)}), encoding="utf-8")
            with self.assertRaises(ConnectionError):
                load_etf_series(
                    "513100", count=5, cache_dir=directory,
                    transport=lambda _url: (_ for _ in ()).throw(ConnectionError("offline")),
                )

    def test_truncate_rebuilds_manifest_and_hash(self):
        series = make_series(self.fixture["qfq_bars"])
        truncated = truncate_series(series, "2022-01-12")
        self.assertEqual(3, len(truncated.bars))
        self.assertEqual("2022-01-12", truncated.manifest.end_date)
        self.assertNotEqual(series.manifest.content_hash, truncated.manifest.content_hash)
        validate_market_data(truncated)

    def test_backtest_loader_delegates_to_contract_layer(self):
        from unittest.mock import patch
        from tools import momentum_etf_backtest

        expected = make_series(self.fixture["qfq_bars"])
        with patch.object(momentum_etf_backtest, "load_etf_series", return_value=expected) as loader:
            bars = momentum_etf_backtest.fetch_kline("513100", count=5, adjust="qfq")

        self.assertEqual(list(expected.bars), bars)
        self.assertEqual(bars.manifest, expected.manifest)
        loader.assert_called_once_with(
            "513100", count=5, adjustment="qfq",
            cache_dir=momentum_etf_backtest._CACHE_DIR, as_of=None,
        )

    def test_backtest_loader_truncates_manifest_to_as_of(self):
        from unittest.mock import patch
        from tools import momentum_etf_backtest

        expected = make_series(self.fixture["qfq_bars"])
        with patch.object(momentum_etf_backtest, "load_etf_series", return_value=expected):
            bars = momentum_etf_backtest.fetch_kline(
                "513100", count=5, adjust="qfq", as_of="2022-01-11"
            )
        self.assertEqual(bars[-1]["date"], "2022-01-11")
        self.assertEqual(bars.manifest.end_date, "2022-01-11")
        self.assertEqual(bars.manifest.bar_count, 2)
        self.assertNotEqual(
            bars.manifest.content_hash,
            expected.manifest.content_hash,
        )

    def test_backtest_fails_closed_when_any_pool_member_fails(self):
        from unittest.mock import patch
        from tools import momentum_etf_backtest

        def fetch(code, **_kwargs):
            if code == "513100":
                raise MarketDataQualityError("bad adjustment")
            return list(self.fixture["qfq_bars"])

        with patch.object(momentum_etf_backtest, "fetch_kline", side_effect=fetch):
            with self.assertRaisesRegex(MarketDataQualityError, "bad adjustment"):
                momentum_etf_backtest.run_backtest(
                    {"513100": "纳指", "510300": "沪深300"}, "2022-01-10", quiet=True
                )

    def test_rolling_backtest_fails_closed_when_any_pool_member_fails(self):
        from unittest.mock import patch
        from tools import momentum_etf_backtest

        with patch.object(
            momentum_etf_backtest,
            "fetch_kline",
            side_effect=MarketDataQualityError("bad adjustment"),
        ):
            with self.assertRaisesRegex(MarketDataQualityError, "bad adjustment"):
                momentum_etf_backtest.run_rolling_backtest({"513100": "纳指"})

    def test_rolling_window_run_failure_is_not_swallowed_after_preload(self):
        from datetime import datetime, timedelta
        from unittest.mock import patch
        from tools import momentum_etf_backtest

        start = datetime(2020, 1, 2)
        bars = []
        for index in range(1000):
            date = (start + timedelta(days=index)).strftime("%Y-%m-%d")
            bars.append({"date": date, "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0})

        with patch.object(momentum_etf_backtest, "fetch_kline", return_value=bars), patch.object(
            momentum_etf_backtest,
            "run_backtest",
            side_effect=ConnectionError("window fetch failed"),
        ):
            with self.assertRaisesRegex(ConnectionError, "window fetch failed"):
                momentum_etf_backtest.run_rolling_backtest(
                    {"513100": "纳指"}, window_months=3, step_months=3
                )


if __name__ == "__main__":
    unittest.main()
