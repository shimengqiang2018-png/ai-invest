import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "momentum-dashboard"))

import db as dashboard_db  # noqa: E402


class DbLayerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_path = dashboard_db.DB_PATH
        self.orig_conn = dashboard_db._conn
        self.orig_backend = dashboard_db.db_backend()
        dashboard_db.configure("sqlite")
        dashboard_db.DB_PATH = Path(self.tmp.name) / "test.db"
        dashboard_db._conn = None
        dashboard_db.stats()  # 初始化 schema

    def tearDown(self):
        dashboard_db.configure(self.orig_backend)
        dashboard_db.DB_PATH = self.orig_path
        dashboard_db._conn = self.orig_conn
        self.tmp.cleanup()

    def test_cache_ttl_roundtrip(self):
        dashboard_db.cache_set("k1", {"a": 1})
        self.assertEqual({"a": 1}, dashboard_db.cache_get("k1", ttl=60))
        self.assertIsNone(dashboard_db.cache_get("k1", ttl=0))  # 过期

    def test_cache_update_upsert(self):
        dashboard_db.cache_set("k1", {"a": 1})
        dashboard_db.cache_set("k1", {"a": 2})
        self.assertEqual({"a": 2}, dashboard_db.cache_get("k1", ttl=60))

    def test_positions_snapshot_roundtrip(self):
        dashboard_db.save_positions_snapshot(
            {"date": "2026-08-10T10:00:00+08:00", "source": "测试", "holdings": [1]}
        )
        latest = dashboard_db.latest_positions_snapshot()
        self.assertEqual("2026-08-10T10:00:00+08:00", latest["date"])
        self.assertEqual(1, len(latest["holdings"]))

    def test_logs_roundtrip(self):
        dashboard_db.append_log("2026-08-10T10:00:00+08:00", "INFO", "hello db")
        logs = dashboard_db.recent_logs(10)
        self.assertEqual(1, len(logs))
        self.assertEqual("hello db", logs[0]["message"])
        self.assertEqual("INFO", logs[0]["level"])

    def test_cleanup_old_logs(self):
        from datetime import datetime, timedelta
        old = (datetime.now().astimezone() - timedelta(days=10)).isoformat(
            timespec="seconds"
        )
        dashboard_db.append_log("2026-08-01T10:00:00+08:00", "INFO", "old")
        dashboard_db._exec(
            "UPDATE api_logs SET created_at = ? WHERE message = 'old'", (old,)
        )
        removed = dashboard_db.cleanup_old_logs(7)
        self.assertEqual(1, removed)
        logs = dashboard_db.recent_logs(50)
        self.assertTrue(all(l["message"] != "old" for l in logs))

    def test_db_info(self):
        info = dashboard_db.db_info()
        self.assertEqual("sqlite", info["backend"])
        self.assertIn("db_path", info)

    def test_list_tables_includes_columns(self):
        tables = dashboard_db.list_tables()
        names = {t["name"] for t in tables}
        self.assertEqual(
            {
                "cache",
                "positions_snapshots",
                "parse_history",
                "api_logs",
                "signal_history",
                "grid_triggers",
                "backtest_results",
                "scheduler_runs",
            },
            names,
        )
        cache_table = next(t for t in tables if t["name"] == "cache")
        self.assertIn("id", cache_table["columns"])
        self.assertIn("created_at", cache_table["columns"])
        self.assertIn("updated_at", cache_table["columns"])

    def test_signal_history_upsert(self):
        dashboard_db.append_signal_history(
            "2026-08-10", "recommended", 25, "ok",
            [{"code": "159920", "pass": True}],
            "159920", "恒生ETF", {"action": "buy", "target": {"code": "159920"}},
            {"items": 1},
        )
        dashboard_db.append_signal_history(
            "2026-08-10", "recommended", 25, "ok",
            [{"code": "159920", "pass": True}, {"code": "518880", "pass": True}],
            "159920", "恒生ETF", None,
            {"items": 2},
        )
        rows = dashboard_db.recent_signal_history(10)
        self.assertEqual(1, len(rows))  # upsert 不产生重复
        self.assertEqual(2, len(rows[0]["items"]))

    def test_backtest_result_upsert_and_get(self):
        dashboard_db.upsert_backtest_result(
            "backtest", "k1", {"a": 1}, {"annual": 10.0}, {"perf": "x"}
        )
        dashboard_db.upsert_backtest_result(
            "backtest", "k1", {"a": 2}, {"annual": 20.0}, {"perf": "y"}
        )
        got = dashboard_db.get_backtest_result("backtest", "k1")
        self.assertEqual({"a": 2}, got["params"])
        self.assertEqual({"annual": 20.0}, got["summary"])

    def test_grid_triggers_seed_idempotent(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False
        ) as handle:
            import json as _json
            _json.dump(
                {
                    "512010": [
                        {
                            "date": "2026-07-07", "action": "sell",
                            "price": "0.583", "shares": 200,
                            "base_price_before": "0.564",
                            "base_price_after": "0.583",
                        }
                    ]
                },
                handle,
            )
            path = handle.name
        try:
            self.assertEqual(1, dashboard_db.seed_grid_triggers_from_file(path))
            self.assertEqual(0, dashboard_db.seed_grid_triggers_from_file(path))
            rows = dashboard_db.recent_grid_triggers(limit=10)
            self.assertEqual(1, len(rows))
            self.assertEqual("sell", rows[0]["action"])
        finally:
            Path(path).unlink(missing_ok=True)

    def test_append_grid_trigger_dedupe(self):
        first = dashboard_db.append_grid_trigger(
            "512010", "医药ETF", "2026-08-10", "09:30:00", "buy", 0.405, 500,
            base_price_before=0.395, base_price_after=0.405, source="manual",
        )
        second = dashboard_db.append_grid_trigger(
            "512010", "医药ETF", "2026-08-10", "09:30:00", "buy", 0.405, 500,
            base_price_before=0.39, base_price_after=0.405, source="manual",
        )
        self.assertEqual("inserted", first)
        self.assertEqual("duplicate", second)
        rows = dashboard_db.recent_grid_triggers("512010", limit=10)
        self.assertEqual(1, len(rows))
        self.assertEqual(0.395, float(rows[0]["base_price_before"]))

    def test_append_grid_trigger_same_day_different_time(self):
        first = dashboard_db.append_grid_trigger(
            "512010", "医药ETF", "2026-08-10", "09:30:00", "sell", 0.405, 500,
            source="manual",
        )
        second = dashboard_db.append_grid_trigger(
            "512010", "医药ETF", "2026-08-10", "10:15:00", "sell", 0.405, 500,
            source="manual",
        )
        self.assertEqual("inserted", first)
        self.assertEqual("inserted", second)
        rows = dashboard_db.recent_grid_triggers("512010", limit=10)
        self.assertEqual(2, len(rows))
        self.assertEqual(
            {"2026-08-10 09:30:00", "2026-08-10 10:15:00"},
            {r["trigger_date"] for r in rows},
        )

    def test_append_grid_trigger_type(self):
        dashboard_db.append_grid_trigger(
            "512010", "医药ETF", "2026-08-10", "09:30:00", "buy", 0.405, 500,
            trigger_type="momentum", source="manual",
        )
        dashboard_db.append_grid_trigger(
            "159920", "恒生ETF", "2026-08-10", "10:00:00", "buy", 1.5, 100,
            source="manual",
        )
        rows = dashboard_db.recent_grid_triggers(limit=10)
        by_code = {r["code"]: r["trigger_type"] for r in rows}
        self.assertEqual("momentum", by_code["512010"])
        self.assertEqual("grid", by_code["159920"])  # 默认类型

    def test_query_grid_triggers_by_code_and_date(self):
        dashboard_db.append_grid_trigger(
            "512010", "医药ETF", "2026-08-08", "09:30:00", "buy", 0.4, 500,
            source="manual",
        )
        dashboard_db.append_grid_trigger(
            "512010", "医药ETF", "2026-08-10", "10:00:00", "sell", 0.42, 500,
            source="manual",
        )
        dashboard_db.append_grid_trigger(
            "159920", "恒生ETF", "2026-08-10", "11:00:00", "buy", 1.5, 100,
            source="manual",
        )
        rows = dashboard_db.query_grid_triggers(
            code="512010", start="2026-08-09", end="2026-08-11"
        )
        self.assertEqual(1, len(rows))
        self.assertEqual("sell", rows[0]["action"])
        rows_all = dashboard_db.query_grid_triggers(limit=10)
        self.assertGreaterEqual(len(rows_all), 3)

    def test_scheduler_run_append(self):
        dashboard_db.append_scheduler_run(
            "manual", "2026-08-10T20:00:00+08:00",
            "2026-08-10T20:00:05+08:00", 5000, "ok",
            {"result": "done"}, True,
        )
        rows = dashboard_db.recent_scheduler_runs(10)
        self.assertEqual(1, len(rows))
        self.assertEqual("ok", rows[0]["result"])
        self.assertEqual(1, rows[0]["email_sent"])


if __name__ == "__main__":
    unittest.main()
