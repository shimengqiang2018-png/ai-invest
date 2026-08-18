import hashlib
import json
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tools import etf_market_data as market_data
from tools.etf_market_data import (
    MarketDataManifest,
    MarketDataQualityError,
    MarketDataSeries,
    _fetch_ths_full,
    _is_cache_fresh,
    _merge_incremental,
    _parse_sina,
    _parse_ths,
    _should_refresh_online,
    _throttle,
    load_etf_series,
    truncate_series,
    validate_market_data,
)


FIXTURE = Path(__file__).parent / "fixtures" / "etf_adjustment_513100.json"


class FakeCurl:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def content_hash(bars):
    payload = json.dumps(bars, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_series(bars, **manifest_overrides):
    manifest = MarketDataManifest(
        schema_version=3,
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
        verification_version=2,
        overlap_start=bars[0]["date"],
        overlap_end=bars[-1]["date"],
        overlap_count=len(bars),
        verification_tolerance=0.005,
        max_return_error=0.0,
        overlap_content_hash=content_hash(bars),
        max_ratio_deviation=0.0,
    )
    return MarketDataSeries(tuple(bars), replace(manifest, **manifest_overrides))


class TransportRetryTests(unittest.TestCase):
    def test_successful_retry_discards_previous_error(self):
        responses = [
            FakeCurl(1, b"", b"temporary"),
            FakeCurl(0, b'{"ok":1}', b""),
        ]
        with patch.object(market_data.subprocess, "run", side_effect=responses), \
             patch.object(market_data.time, "sleep"):
            self.assertEqual(
                market_data._default_transport("https://example.test"),
                b'{"ok":1}',
            )


class MarketDataContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def write_valid_cache(
        self, directory, code="159920", count=300,
        end_date="2026-07-29", source="eastmoney",
    ):
        cursor = datetime.strptime(end_date, "%Y-%m-%d")
        dates = []
        while len(dates) < count:
            if cursor.weekday() < 5:
                dates.append(cursor.strftime("%Y-%m-%d"))
            cursor -= timedelta(days=1)
        bars = [
            {
                "date": date,
                "open": 1.0,
                "close": 1.0,
                "high": 1.0,
                "low": 1.0,
                "volume": 1.0,
            }
            for date in reversed(dates)
        ]
        manifest_overrides = {"code": code, "source": source}
        if source == "tencent":
            manifest_overrides.update(
                adjustment_verified=False,
                verification_source="provider_declared_qfqday",
            )
        elif source == "eastmoney":
            manifest_overrides.update(
                adjustment_verified=False,
                verification_source="provider_declared_fqt1",
            )
        series = make_series(bars, **manifest_overrides)
        cache = Path(directory) / f"etf_v2_{source}_{code}_qfq_{count}.json"
        cache.write_text(
            json.dumps({"manifest": series.manifest.__dict__, "bars": list(series.bars)}),
            encoding="utf-8",
        )
        return series

    # ── Source-specific helpers ──

    def _sina_rows(self, *rows):
        """Build Sina JSON array from (date, open, close, high, low, volume_shares)."""
        return [
            {"day": d, "open": str(o), "high": str(h), "low": str(l),
             "close": str(c), "volume": str(v)}
            for d, o, c, h, l, v in rows
        ]

    def _em_payload(self, *rows):
        """Build Eastmoney fqt=1 payload from (date, open, close, high, low, volume)."""
        return {
            "data": {
                "klines": [
                    f"{d},{o},{c},{h},{l},{v},0" for d, o, c, h, l, v in rows
                ]
            }
        }

    def _tencent_payload(self, *rows):
        """Build Tencent qfqday payload from (date, open, close, high, low, volume)."""
        return {"data": {"sh513100": {"qfqday": [list(map(str, row)) for row in rows]}}}

    # ══════════════════════════════════════════════════════════════════
    # Validation tests
    # ══════════════════════════════════════════════════════════════════

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
            if "money.finance.sina.com.cn" in url:
                # 22% daily return: exceeds 20% validation guard but below
                # 25% split-detection threshold
                return self._sina_rows(
                    ("2022-01-10", "1.000", "1.000", "1.000", "1.000", "120000000"),
                    ("2022-01-11", "1.220", "1.220", "1.220", "1.220", "118000000"),
                )
            return {"data": {"sh513100": {"qfqday": []}}}

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MarketDataQualityError, "20%"):
                load_etf_series(
                    "513100", count=2, cache_dir=directory, transport=transport
                )

    # ══════════════════════════════════════════════════════════════════
    # Source fallback & verification tests
    # ══════════════════════════════════════════════════════════════════

    def test_sina_fallback_is_provider_declared_when_reference_unavailable(self):
        def transport(url):
            if "push2his.eastmoney.com" in url:
                raise ConnectionError("em offline")
            if "money.finance.sina.com.cn" in url:
                return self._sina_rows(
                    ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "120000000"),
                    ("2022-01-11", "1.009", "1.015", "1.021", "1.001", "118000000"),
                )
            return {"data": {"sh513100": {"qfqday": []}}}

        with tempfile.TemporaryDirectory() as directory:
            series = load_etf_series(
                "513100", count=2, cache_dir=directory, transport=transport
            )
        self.assertEqual(series.manifest.source, "sina")
        self.assertFalse(series.manifest.adjustment_verified)
        self.assertEqual(
            series.manifest.verification_source,
            "provider_declared_qfqday",
        )

    def test_tencent_standalone_is_provider_declared_not_cross_verified(self):
        def transport(url):
            if "push2his.eastmoney.com" in url:
                raise ConnectionError("em offline")
            return self._tencent_payload(
                ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"),
                ("2022-01-11", "1.009", "1.015", "1.021", "1.001", "1180000"),
            )

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

    def test_reference_mismatch_degrades_to_provider_declared(self):
        """腾讯参考源与东财主源严重不一致时降级为 provider_declared，不抛异常。"""
        def transport(url):
            if "push2his.eastmoney.com" in url:
                return self._em_payload(
                    ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"),
                    ("2022-01-11", "1.009", "1.015", "1.021", "1.001", "1180000"),
                )
            return self._tencent_payload(
                ("2022-01-10", "1.500", "1.500", "1.500", "1.500", "1200000"),
                ("2022-01-11", "1.800", "1.800", "1.800", "1.800", "1180000"),
            )

        with tempfile.TemporaryDirectory() as directory:
            series = load_etf_series("513100", count=2, cache_dir=directory, transport=transport)
        self.assertEqual("eastmoney", series.manifest.source)
        self.assertFalse(series.manifest.adjustment_verified)
        self.assertEqual("provider_declared_fqt1", series.manifest.verification_source)

    def test_level_drift_is_detected_and_degrades_to_provider_declared(self):
        """主序列漏掉分红（价格比阶梯漂移）时，验证必须失败并降级。"""
        def transport(url):
            if "push2his.eastmoney.com" in url:
                # 主序列：2022-01-11 起分红跳空（不复权），与参考序列差 5%
                return self._em_payload(
                    ("2022-01-10", "1.000", "1.000", "1.000", "1.000", "1200000"),
                    ("2022-01-11", "0.950", "0.950", "0.950", "0.950", "1200000"),
                    ("2022-01-12", "0.950", "0.960", "0.960", "0.950", "1200000"),
                    ("2022-01-13", "0.960", "0.970", "0.970", "0.960", "1200000"),
                )
            # 参考序列：平滑 qfq，无跳空
            return self._tencent_payload(
                ("2022-01-10", "1.000", "1.000", "1.000", "1.000", "1200000"),
                ("2022-01-11", "1.000", "1.000", "1.000", "1.000", "1200000"),
                ("2022-01-12", "1.000", "1.010", "1.010", "1.000", "1200000"),
                ("2022-01-13", "1.010", "1.020", "1.020", "1.010", "1200000"),
            )

        with tempfile.TemporaryDirectory() as directory:
            series = load_etf_series("513100", count=4, cache_dir=directory, transport=transport)
        self.assertEqual("eastmoney", series.manifest.source)
        self.assertFalse(series.manifest.adjustment_verified)
        self.assertGreater(series.manifest.max_ratio_deviation, 0.02)

    def test_tencent_non_finite_or_non_positive_values_degrade_to_sina(self):
        """腾讯数据含非法值时，降级到新浪 standalone，不抛异常。"""
        for field, value in (("close", "nan"), ("close", "inf"), ("open", "0")):
            with self.subTest(field=field, value=value):
                row = ["2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"]
                row[{"open": 1, "close": 2}[field]] = value

                def transport(url):
                    if "push2his.eastmoney.com" in url:
                        raise ConnectionError("em offline")
                    if "money.finance.sina.com.cn" in url:
                        return self._sina_rows(
                            ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "120000000"),
                        )
                    return {"data": {"sh513100": {"qfqday": [row]}}}

                with tempfile.TemporaryDirectory() as directory:
                    series = load_etf_series("513100", count=1, cache_dir=directory, transport=transport)
                    self.assertIsNotNone(series)
                    self.assertEqual("provider_declared_qfqday", series.manifest.verification_source,
                                     f"field={field}, value={value}: 应降级到新浪 standalone")
                    cached = list(Path(directory).glob("etf_v2_*.json"))
                    self.assertEqual(1, len(cached), "降级后应写入缓存文件")

    def test_tencent_invalid_date_degrade_to_sina(self):
        """腾讯数据日期格式异常时，降级到新浪 standalone。"""
        def transport(url):
            if "push2his.eastmoney.com" in url:
                raise ConnectionError("em offline")
            if "money.finance.sina.com.cn" in url:
                return self._sina_rows(
                    ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "120000000"),
                )
            return {"data": {"sh513100": {"qfqday": [
                ["not-a-date", "0.997", "1.009", "1.017", "0.993", "1200000"],
            ]}}}

        with tempfile.TemporaryDirectory() as directory:
            series = load_etf_series("513100", count=1, cache_dir=directory, transport=transport)
            self.assertIsNotNone(series)
            self.assertEqual("provider_declared_qfqday", series.manifest.verification_source,
                             "应降级到新浪 standalone")

    # ══════════════════════════════════════════════════════════════════
    # Cache round-trip tests
    # ══════════════════════════════════════════════════════════════════

    def test_v2_cache_round_trip_and_atomic_write(self):
        calls = []

        def transport(url):
            calls.append(url)
            if "push2his.eastmoney.com" in url:
                return self._em_payload(
                    ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"),
                    ("2022-01-11", "1.009", "1.015", "1.021", "1.001", "1180000"),
                )
            return self._tencent_payload(
                ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"),
                ("2022-01-11", "1.009", "1.015", "1.021", "1.001", "1180000"),
            )

        with tempfile.TemporaryDirectory() as directory:
            first = load_etf_series("513100", count=2, as_of="2022-01-11", cache_dir=directory, transport=transport)
            cache_files = list(Path(directory).glob("etf_v2_eastmoney_513100_qfq_2.json"))
            temp_files = list(Path(directory).glob("*.tmp"))
            self.assertEqual(3, first.manifest.schema_version)
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
            self.assertEqual(0.005, first.manifest.verification_tolerance)
            self.assertEqual(0.0, first.manifest.max_return_error)
            self.assertEqual(0.0, first.manifest.max_ratio_deviation)
            self.assertEqual(64, len(first.manifest.overlap_content_hash))

    def test_default_mode_falls_back_to_valid_cache_after_network_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            old_date = (datetime.utcnow() - timedelta(days=10)).strftime("%Y-%m-%d")
            cached = self.write_valid_cache(directory, end_date=old_date)
            calls = []

            def always_fail_transport(url):
                calls.append(url)
                raise ConnectionError("offline")

            loaded = load_etf_series(
                "159920", count=300, cache_dir=directory,
                transport=always_fail_transport,
            )

            self.assertTrue(calls, "transport should be attempted first")
            self.assertEqual(loaded.manifest.content_hash, cached.manifest.content_hash)

    def test_cache_before_as_of_is_used_after_refresh_fails(self):
        calls = []

        def seed_transport(url):
            if "push2his.eastmoney.com" in url:
                return self._em_payload(
                    ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"),
                    ("2022-01-11", "1.009", "1.015", "1.021", "1.001", "1180000"),
                )
            return self._tencent_payload(
                ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"),
                ("2022-01-11", "1.009", "1.015", "1.021", "1.001", "1180000"),
            )

        with tempfile.TemporaryDirectory() as directory:
            load_etf_series("513100", count=2, as_of="2022-01-11", cache_dir=directory, transport=seed_transport)

            def offline(url):
                calls.append(url)
                raise ConnectionError("offline")

            loaded = load_etf_series(
                "513100", count=2, as_of="2022-01-12",
                cache_dir=directory, transport=offline,
            )
            self.assertTrue(calls)
            self.assertEqual("2022-01-11", loaded.manifest.end_date)

    def test_cache_fallback_skips_candidate_without_bars_before_as_of(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write_valid_cache(
                directory, count=2, end_date="2026-08-04", source="sina",
            )
            expected = self.write_valid_cache(
                directory, count=2, end_date="2026-07-31", source="tencent",
            )

            loaded = load_etf_series(
                "159920", count=2, as_of="2026-07-31", cache_dir=directory,
                transport=lambda _url: (_ for _ in ()).throw(ConnectionError("offline")),
            )

        self.assertEqual(expected.manifest.content_hash, loaded.manifest.content_hash)
        self.assertEqual("tencent", loaded.manifest.source)

    def test_cache_source_priority_prefers_eastmoney_over_tencent(self):
        """同新鲜度下，缓存选择按 eastmoney > tencent > sina 优先级。"""
        with tempfile.TemporaryDirectory() as directory:
            self.write_valid_cache(directory, count=2, end_date="2026-08-04", source="tencent")
            expected = self.write_valid_cache(
                directory, count=2, end_date="2026-07-31", source="eastmoney",
            )
            loaded = load_etf_series(
                "159920", count=2, cache_dir=directory,
                transport=lambda _url: (_ for _ in ()).throw(ConnectionError("offline")),
            )

        self.assertEqual("eastmoney", loaded.manifest.source)
        self.assertEqual(expected.manifest.content_hash, loaded.manifest.content_hash)

    def test_invalid_or_short_v2_cache_is_not_used_after_primary_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "etf_v2_sina_513100_qfq_5.json"
            series = make_series(self.fixture["qfq_bars"][:2])
            cache.write_text(json.dumps({"manifest": series.manifest.__dict__, "bars": list(series.bars)}), encoding="utf-8")
            with self.assertRaises(ConnectionError):
                load_etf_series(
                    "513100", count=5, cache_dir=directory,
                    transport=lambda _url: (_ for _ in ()).throw(ConnectionError("offline")),
                )

    # ══════════════════════════════════════════════════════════════════
    # Date / weekend / truncation tests
    # ══════════════════════════════════════════════════════════════════

    def test_latest_completed_trading_date_uses_latest_bar_before_weekend(self):
        bars = (
            {"date": "2026-07-30"},
            {"date": "2026-07-31"},
        )

        self.assertEqual(
            "2026-07-31",
            market_data.latest_completed_trading_date("2026-08-02", bars),
        )

    def test_weekend_as_of_accepts_latest_prior_trading_day(self):
        def weekend_fixture_transport(url):
            if "push2his.eastmoney.com" in url:
                return self._em_payload(
                    ("2026-07-30", "1.000", "1.000", "1.000", "1.000", "1200000"),
                    ("2026-07-31", "1.000", "1.010", "1.010", "1.000", "1180000"),
                )
            return self._tencent_payload(
                ("2026-07-30", "1.000", "1.000", "1.000", "1.000", "1200000"),
                ("2026-07-31", "1.000", "1.010", "1.010", "1.000", "1180000"),
            )

        with tempfile.TemporaryDirectory() as directory:
            loaded = load_etf_series(
                "159920", count=300, as_of="2026-08-02",
                cache_dir=directory, transport=weekend_fixture_transport,
            )

        self.assertEqual(loaded.manifest.end_date, "2026-07-31")

    def test_truncate_rebuilds_manifest_and_hash(self):
        series = make_series(self.fixture["qfq_bars"])
        truncated = truncate_series(series, "2022-01-12")
        self.assertEqual(3, len(truncated.bars))
        self.assertEqual("2022-01-12", truncated.manifest.end_date)
        self.assertNotEqual(series.manifest.content_hash, truncated.manifest.content_hash)
        validate_market_data(truncated)

    # ══════════════════════════════════════════════════════════════════
    # Backtest integration tests (unchanged)
    # ══════════════════════════════════════════════════════════════════

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

    # ══════════════════════════════════════════════════════════════════
    # Sina volume normalisation test
    # ══════════════════════════════════════════════════════════════════

    def test_sina_volume_normalisation(self):
        """Sina volume (股) is divided by 100 to normalise to 手."""
        payload = [
            {"day": "2022-01-10", "open": "1.0", "high": "1.1", "low": "0.9",
             "close": "1.05", "volume": "50000000"},
            {"day": "2022-01-11", "open": "1.05", "high": "1.2", "low": "1.0",
             "close": "1.15", "volume": "30000000"},
        ]
        bars = _parse_sina(payload, "sh518880")
        self.assertEqual(500000.0, bars[0]["volume"])
        self.assertEqual(300000.0, bars[1]["volume"])

    # ══════════════════════════════════════════════════════════════════
    # THS (同花顺) source tests
    # ══════════════════════════════════════════════════════════════════

    def _ths_payload(self, start, *rows):
        """Build THS JS-wrapped payload from (date8, open, high, low, close, volume_shares).

        Matches the real endpoint shape:
        quotebridge_v6_line_...({"start":"20120528","total":"N",
        "data":"YYYYMMDD,o,h,l,c,v,amount,turnover,,,0","marketType":...})
        """
        data_rows = ";".join(
            f"{d},{o},{h},{l},{c},{v},0,0,,,0" for d, o, h, l, c, v in rows
        )
        return (
            'quotebridge_v6_line_hs_513100_01_last({"num":140,"start":"'
            f'{start}","total":"{len(rows)}","name":"513100",'
            f'"data":"{data_rows}","marketType":"","issuePrice":"1.0"}})'
        )

    def test_parse_ths_ohlc_order_and_volume_normalisation(self):
        """THS payload: OHL**C** order (≠ Tencent), volume 股→手, date8→date."""
        payload = self._ths_payload(
            "20220110",
            ("20220110", "1.000", "1.100", "0.900", "1.050", "50000000"),
            ("20220111", "1.050", "1.200", "1.000", "1.150", "30000000"),
        )
        bars = _parse_ths(payload, "513100")
        self.assertEqual(2, len(bars))
        self.assertEqual("2022-01-10", bars[0]["date"])
        self.assertEqual(1.000, bars[0]["open"])
        self.assertEqual(1.100, bars[0]["high"])
        self.assertEqual(0.900, bars[0]["low"])
        self.assertEqual(1.050, bars[0]["close"])
        self.assertEqual(500000.0, bars[0]["volume"])  # 股→手
        self.assertEqual("2022-01-11", bars[1]["date"])

    def test_parse_ths_without_markettype_falls_back(self):
        """Payload missing trailing marketType metadata still parses (data 段保底正则)."""
        payload = (
            'quotebridge_v6_line_hs_513100_01_2022({"num":2,"start":"20220110",'
            '"data":"20220110,1.0,1.1,0.9,1.05,50000000,0,0,,,0"})'
        )
        bars = _parse_ths(payload, "513100")
        self.assertEqual(1, len(bars))
        self.assertEqual(500000.0, bars[0]["volume"])

    def test_fetch_ths_full_pages_years_and_sorts(self):
        """THS full history pages year files, merges, de-dupes, sorts ascending."""
        def transport(url):
            if "d.10jqka.com.cn" in url:
                if url.endswith("/2022.js"):
                    return self._ths_payload(
                        "20220101",
                        ("20220103", "1.0", "1.1", "0.9", "1.05", "50000000"),
                        ("20220104", "1.05", "1.2", "1.0", "1.15", "30000000"),
                    )
                if url.endswith("/2023.js"):
                    return self._ths_payload(
                        "20220101",
                        ("20230103", "1.1", "1.2", "1.0", "1.15", "50000000"),
                        ("20230104", "1.15", "1.3", "1.1", "1.25", "30000000"),
                    )
                return self._ths_payload("20220101")  # 其他年份/空 data → 解析抛异常被跳过
            raise ConnectionError(f"unexpected url: {url}")

        bars = _fetch_ths_full("sh513100", transport)
        self.assertEqual(4, len(bars))
        self.assertEqual("2022-01-03", bars[0]["date"])
        self.assertEqual("2023-01-04", bars[-1]["date"])

    def test_load_etf_series_ths_primary_cross_verified(self):
        """THS 主源成功，腾讯参考源交叉验证通过 → source=ths, verified."""
        def transport(url):
            if "d.10jqka.com.cn" in url:
                return self._ths_payload(
                    "20220110",
                    ("20220110", "0.997", "1.017", "0.993", "1.009", "120000000"),
                    ("20220111", "1.009", "1.021", "1.001", "1.015", "118000000"),
                    ("20220112", "1.015", "1.030", "1.010", "1.020", "116000000"),
                    ("20220113", "1.020", "1.040", "1.015", "1.030", "114000000"),
                )
            if "web.ifzq.gtimg.cn" in url:
                return self._tencent_payload(
                    ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"),
                    ("2022-01-11", "1.009", "1.015", "1.021", "1.001", "1180000"),
                    ("2022-01-12", "1.015", "1.020", "1.030", "1.010", "1160000"),
                    ("2022-01-13", "1.020", "1.030", "1.040", "1.015", "1140000"),
                )
            if "push2his.eastmoney.com" in url:
                raise ConnectionError("em offline")
            if "money.finance.sina.com.cn" in url:
                return self._sina_rows(
                    ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "120000000"),
                )
            return {}

        with tempfile.TemporaryDirectory() as directory:
            series = load_etf_series(
                "513100", count=4, cache_dir=directory, transport=transport
            )
        self.assertEqual("ths", series.manifest.source)
        self.assertTrue(series.manifest.adjustment_verified)
        self.assertEqual("tencent_qfqday", series.manifest.verification_source)

    def test_load_etf_series_ths_provider_declared_when_reference_unavailable(self):
        """腾讯参考源不可用 → THS 降级 provider_declared，不抛异常."""
        def transport(url):
            if "d.10jqka.com.cn" in url:
                return self._ths_payload(
                    "20220110",
                    ("20220110", "0.997", "1.017", "0.993", "1.009", "120000000"),
                    ("20220111", "1.009", "1.021", "1.001", "1.015", "118000000"),
                )
            if "web.ifzq.gtimg.cn" in url:
                return {"data": {"sh513100": {"qfqday": []}}}
            if "push2his.eastmoney.com" in url:
                raise ConnectionError("em offline")
            if "money.finance.sina.com.cn" in url:
                return self._sina_rows(
                    ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "120000000"),
                )
            return {}

        with tempfile.TemporaryDirectory() as directory:
            series = load_etf_series(
                "513100", count=2, cache_dir=directory, transport=transport
            )
        self.assertEqual("ths", series.manifest.source)
        self.assertFalse(series.manifest.adjustment_verified)
        self.assertEqual("provider_declared_qfqday", series.manifest.verification_source)

    def test_load_etf_series_ths_fallback_to_eastmoney(self):
        """THS 主源不可用 → 回退东财 fqt=1 主源."""
        def transport(url):
            if "d.10jqka.com.cn" in url:
                raise ConnectionError("ths offline")
            if "push2his.eastmoney.com" in url:
                return self._em_payload(
                    ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"),
                    ("2022-01-11", "1.009", "1.015", "1.021", "1.001", "1180000"),
                    ("2022-01-12", "1.015", "1.020", "1.030", "1.010", "1160000"),
                    ("2022-01-13", "1.020", "1.030", "1.040", "1.015", "1140000"),
                )
            if "web.ifzq.gtimg.cn" in url:
                return self._tencent_payload(
                    ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"),
                    ("2022-01-11", "1.009", "1.015", "1.021", "1.001", "1180000"),
                    ("2022-01-12", "1.015", "1.020", "1.030", "1.010", "1160000"),
                    ("2022-01-13", "1.020", "1.030", "1.040", "1.015", "1140000"),
                )
            if "money.finance.sina.com.cn" in url:
                return self._sina_rows(
                    ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "120000000"),
                )
            return {}

        with tempfile.TemporaryDirectory() as directory:
            series = load_etf_series(
                "513100", count=4, cache_dir=directory, transport=transport
            )
        self.assertEqual("eastmoney", series.manifest.source)
        self.assertTrue(series.manifest.adjustment_verified)

    def test_source_order_env_skips_ths_when_absent(self):
        """ETF_DATA_SOURCE_ORDER 不含 ths 时，同花顺完全不发起请求（可切换验证）."""
        import os

        ths_called = []

        def transport(url):
            if "d.10jqka.com.cn" in url:
                ths_called.append(url)
                raise ConnectionError("ths should not be called")
            if "push2his.eastmoney.com" in url:
                return self._em_payload(
                    ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"),
                    ("2022-01-11", "1.009", "1.015", "1.021", "1.001", "1180000"),
                    ("2022-01-12", "1.015", "1.020", "1.030", "1.010", "1160000"),
                    ("2022-01-13", "1.020", "1.030", "1.040", "1.015", "1140000"),
                )
            if "web.ifzq.gtimg.cn" in url:
                return self._tencent_payload(
                    ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"),
                    ("2022-01-11", "1.009", "1.015", "1.021", "1.001", "1180000"),
                    ("2022-01-12", "1.015", "1.020", "1.030", "1.010", "1160000"),
                    ("2022-01-13", "1.020", "1.030", "1.040", "1.015", "1140000"),
                )
            return {}

        with patch.dict(os.environ, {"ETF_DATA_SOURCE_ORDER": "eastmoney,tencent"}):
            with tempfile.TemporaryDirectory() as directory:
                series = load_etf_series(
                    "513100", count=4, cache_dir=directory, transport=transport
                )
        self.assertEqual("eastmoney", series.manifest.source)
        self.assertEqual([], ths_called, "THS 不应被请求")

    # ══════════════════════════════════════════════════════════════════
    # Incremental cache tests
    # ══════════════════════════════════════════════════════════════════

    def test_is_cache_fresh_within_3_days(self):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        bars = [{"date": today, "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0}]
        series = make_series(bars)
        self.assertTrue(_is_cache_fresh(series.manifest, max_age_days=3))

    def test_is_cache_fresh_older_than_3_days(self):
        old_date = (datetime.utcnow() - timedelta(days=5)).strftime("%Y-%m-%d")
        bars = [{"date": old_date, "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0}]
        series = make_series(bars)
        self.assertFalse(_is_cache_fresh(series.manifest, max_age_days=3))

    def test_should_refresh_online_after_close_when_missing_today(self):
        """收盘后在线模式，缓存缺今日 bar → 刷新。"""
        from zoneinfo import ZoneInfo
        now = datetime(2026, 8, 11, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertTrue(_should_refresh_online("2026-08-10", now=now))
        self.assertFalse(_should_refresh_online("2026-08-11", now=now))

    def test_should_refresh_online_intraday_skips(self):
        """盘中（<15:10）今日日 K 尚未形成，不重复拉取。"""
        from zoneinfo import ZoneInfo
        now = datetime(2026, 8, 11, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertFalse(_should_refresh_online("2026-08-10", now=now))

    def test_should_refresh_online_weekend_skips(self):
        """周末不强制刷新（周五收盘缓存即最新）。"""
        from zoneinfo import ZoneInfo
        now = datetime(2026, 8, 15, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai"))  # 周六
        self.assertFalse(_should_refresh_online("2026-08-14", now=now))

    def test_incremental_merge_success(self):
        """Stale cache + overlapping fresh data produces merged result."""
        cached_bars = [
            {"date": "2022-01-10", "open": 1.0, "close": 1.01, "high": 1.02, "low": 0.99, "volume": 1000},
            {"date": "2022-01-11", "open": 1.01, "close": 1.02, "high": 1.03, "low": 1.00, "volume": 1100},
            {"date": "2022-01-12", "open": 1.02, "close": 1.03, "high": 1.04, "low": 1.01, "volume": 1200},
        ]
        fresh_bars = [
            {"date": "2022-01-11", "open": 1.01, "close": 1.02, "high": 1.03, "low": 1.00, "volume": 1100},
            {"date": "2022-01-12", "open": 1.02, "close": 1.03, "high": 1.04, "low": 1.01, "volume": 1200},
            {"date": "2022-01-13", "open": 1.03, "close": 1.04, "high": 1.05, "low": 1.02, "volume": 1300},
        ]
        cached = make_series(cached_bars)
        merged = _merge_incremental(cached, fresh_bars)
        self.assertIsNotNone(merged)
        self.assertEqual(4, len(merged))
        self.assertEqual("2022-01-10", merged[0]["date"])
        self.assertEqual("2022-01-13", merged[-1]["date"])

    def test_incremental_merge_no_overlap_returns_none(self):
        cached_bars = [
            {"date": "2022-01-10", "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0, "volume": 1000},
        ]
        fresh_bars = [
            {"date": "2022-02-10", "open": 2.0, "close": 2.0, "high": 2.0, "low": 2.0, "volume": 2000},
        ]
        cached = make_series(cached_bars)
        self.assertIsNone(_merge_incremental(cached, fresh_bars))

    def test_incremental_merge_close_mismatch_returns_none(self):
        """Divergent close prices in the overlap window cause merge failure."""
        cached_bars = [
            {"date": "2022-01-10", "open": 1.0, "close": 1.000, "high": 1.0, "low": 1.0, "volume": 1000},
        ]
        fresh_bars = [
            {"date": "2022-01-10", "open": 1.0, "close": 1.050, "high": 1.0, "low": 1.0, "volume": 1000},
            {"date": "2022-01-11", "open": 1.1, "close": 1.100, "high": 1.1, "low": 1.1, "volume": 1100},
        ]
        cached = make_series(cached_bars)
        self.assertIsNone(_merge_incremental(cached, fresh_bars))

    def test_fresh_cache_skips_network(self):
        """Cache with today's end_date should return immediately without HTTP."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as directory:
            self.write_valid_cache(directory, code="518880", count=2, end_date=today)
            calls = []

            def transport(url):
                calls.append(url)
                raise ConnectionError("should not be called")

            series = load_etf_series("518880", count=2, cache_dir=directory, transport=transport)
            self.assertEqual([], calls, "fresh cache should not trigger any HTTP requests")
            self.assertEqual(today, series.manifest.end_date)

    def test_refresh_ignores_fresh_cache_and_hits_network(self):
        """refresh=True 跳过新鲜缓存，强制联网重建。"""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as directory:
            self.write_valid_cache(directory, code="518880", count=2, end_date=today)
            calls = []

            def transport(url):
                calls.append(url)
                if "push2his.eastmoney.com" in url:
                    return self._em_payload(
                        ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"),
                        ("2022-01-11", "1.009", "1.015", "1.021", "1.001", "1180000"),
                    )
                return self._tencent_payload(
                    ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"),
                    ("2022-01-11", "1.009", "1.015", "1.021", "1.001", "1180000"),
                )

            series = load_etf_series(
                "518880", count=2, cache_dir=directory, transport=transport, refresh=True
            )
            self.assertTrue(calls, "refresh=True 应触发网络请求")
            self.assertEqual("eastmoney", series.manifest.source)

    def test_stale_cache_triggers_full_fetch_from_eastmoney(self):
        """Stale cache triggers a full Eastmoney fetch + Tencent verification."""
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            stale_date = (datetime.utcnow() - timedelta(days=5)).strftime("%Y-%m-%d")
            bars = [{"date": stale_date, "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0}]
            series = make_series(bars, code="518880", source="sina")
            cache = Path(directory) / "etf_v2_sina_518880_qfq_2.json"
            cache.write_text(
                json.dumps({"manifest": series.manifest.__dict__, "bars": list(series.bars)}),
                encoding="utf-8",
            )

            def transport(url):
                calls.append(url)
                if "push2his.eastmoney.com" in url:
                    return self._em_payload(
                        ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"),
                        ("2022-01-11", "1.009", "1.015", "1.021", "1.001", "1180000"),
                    )
                return self._tencent_payload(
                    ("2022-01-10", "0.997", "1.009", "1.017", "0.993", "1200000"),
                    ("2022-01-11", "1.009", "1.015", "1.021", "1.001", "1180000"),
                )

            series = load_etf_series("518880", count=2, cache_dir=directory, transport=transport)
            self.assertIsNotNone(series)
            em_calls = [c for c in calls if "push2his.eastmoney.com" in c]
            self.assertGreaterEqual(len(em_calls), 1)
            self.assertEqual("eastmoney", series.manifest.source)

    # ══════════════════════════════════════════════════════════════════
    # Rate limiter test
    # ══════════════════════════════════════════════════════════════════

    def test_rate_limiter_enforces_minimum_interval(self):
        """_throttle should introduce delays when called rapidly."""
        import tools.etf_market_data as emd
        original_enabled = emd._RATE_LIMIT_ENABLED
        original_interval = emd._MIN_REQUEST_INTERVAL
        original_last = emd._last_request_time

        try:
            emd._RATE_LIMIT_ENABLED = True
            emd._MIN_REQUEST_INTERVAL = 0.1
            emd._last_request_time = 0.0

            start = time.time()
            emd._throttle()
            first_elapsed = time.time() - start
            self.assertLess(first_elapsed, 0.05, "first call should not throttle")

            start = time.time()
            emd._throttle()
            second_elapsed = time.time() - start
            self.assertGreaterEqual(second_elapsed, 0.05,
                                    "second call should be throttled")
        finally:
            emd._RATE_LIMIT_ENABLED = original_enabled
            emd._MIN_REQUEST_INTERVAL = original_interval
            emd._last_request_time = original_last


if __name__ == "__main__":
    unittest.main()
