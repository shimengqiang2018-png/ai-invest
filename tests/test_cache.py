import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "momentum-dashboard"))

import cache as cache_layer  # noqa: E402
import db as dashboard_db  # noqa: E402


class CacheLayerMemoryTests(unittest.TestCase):
    """内存后端：TTL、cached() 命中/重算/失败回退、后端切换。"""

    def setUp(self):
        self.orig_backend = cache_layer.backend_name()
        cache_layer.configure("memory")
        cache_layer.flush()

    def tearDown(self):
        cache_layer.flush()  # 先清当前(内存)后端，避免恢复默认后误连 MySQL
        cache_layer.configure(self.orig_backend)

    def test_memory_roundtrip_ttl(self):
        cache_layer.set("k1", {"a": 1})
        self.assertEqual({"a": 1}, cache_layer.get("k1", ttl=60))
        self.assertIsNone(cache_layer.get("k1", ttl=0))  # 过期

    def test_memory_upsert(self):
        cache_layer.set("k1", {"a": 1})
        cache_layer.set("k1", {"a": 2})
        self.assertEqual({"a": 2}, cache_layer.get("k1", ttl=60))

    def test_cached_produces_and_hits(self):
        calls = []

        def producer():
            calls.append(1)
            return {"x": len(calls)}

        payload, hit, stale = cache_layer.cached("ck", 60, False, producer)
        self.assertFalse(hit)
        self.assertFalse(stale)
        self.assertEqual({"x": 1}, payload["data"])

        payload2, hit2, stale2 = cache_layer.cached("ck", 60, False, producer)
        self.assertTrue(hit2)
        self.assertFalse(stale2)
        self.assertEqual(1, len(calls))  # 未重复执行 producer
        self.assertTrue(payload2["cached"])

    def test_cached_refresh_forces_rerun(self):
        calls = []

        def producer():
            calls.append(1)
            return {"x": len(calls)}

        cache_layer.cached("rk", 60, False, producer)
        payload, hit, _ = cache_layer.cached("rk", 60, True, producer)
        self.assertFalse(hit)
        self.assertEqual({"x": 2}, payload["data"])

    def test_cached_stale_fallback(self):
        cache_layer.set("sk", {"data": {"old": 1}})

        def bad_producer():
            raise RuntimeError("boom")

        payload, hit, stale = cache_layer.cached("sk", 60, True, bad_producer)
        self.assertTrue(hit)
        self.assertTrue(stale)
        self.assertEqual({"old": 1}, payload["data"])
        self.assertIn("boom", payload["stale_error"])

    def test_cached_no_stale_raises(self):
        def bad_producer():
            raise ValueError("bad input")

        with self.assertRaises(ValueError):
            cache_layer.cached("nk", 60, True, bad_producer)

    def test_unknown_backend_rejected(self):
        with self.assertRaises(ValueError):
            cache_layer.configure("redis-not-exist")

    def test_backend_list(self):
        self.assertIn("db", cache_layer.backends())
        self.assertIn("memory", cache_layer.backends())

    def test_sqlite_alias_maps_to_db(self):
        cache_layer.configure("sqlite")
        self.assertEqual("db", cache_layer.backend_name())
        cache_layer.configure("memory")
        self.assertEqual("memory", cache_layer.backend_name())


class CacheLayerSqliteTests(unittest.TestCase):
    """SQLite 后端：持久化到 db.cache 表，切库后数据仍可读。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_path = dashboard_db.DB_PATH
        self.orig_conn = dashboard_db._conn
        self.orig_db_backend = dashboard_db.db_backend()
        dashboard_db.configure("sqlite")
        dashboard_db.DB_PATH = Path(self.tmp.name) / "test.db"
        dashboard_db._conn = None
        dashboard_db.stats()  # 初始化 schema
        self.orig_backend = cache_layer.backend_name()
        cache_layer.configure("sqlite")

    def tearDown(self):
        cache_layer.configure(self.orig_backend)
        dashboard_db.configure(self.orig_db_backend)
        dashboard_db.DB_PATH = self.orig_path
        dashboard_db._conn = self.orig_conn
        self.tmp.cleanup()

    def test_sqlite_backend_roundtrip(self):
        cache_layer.set("k1", {"a": 1})
        self.assertEqual({"a": 1}, cache_layer.get("k1", ttl=60))
        self.assertIsNone(cache_layer.get("k1", ttl=0))

    def test_sqlite_backend_persists_via_db(self):
        cache_layer.set("k1", {"a": 1})
        self.assertEqual({"a": 1}, dashboard_db.cache_get("k1", ttl=60))

    def test_sqlite_delete_expired(self):
        cache_layer.set("old", {"a": 1})
        dashboard_db._exec(
            "UPDATE cache SET updated_at = ? WHERE key = ?", (0, "old")
        )
        self.assertEqual(1, cache_layer.delete_expired())
        self.assertIsNone(cache_layer.get("old"))

    def test_sqlite_delete_expired_keeps_fresh(self):
        cache_layer.set("fresh", {"a": 1})
        self.assertEqual(0, cache_layer.delete_expired())
        self.assertEqual({"a": 1}, cache_layer.get("fresh", ttl=60))

    def test_sqlite_flush(self):
        cache_layer.set("k1", {"a": 1})
        cache_layer.set("k2", {"b": 2})
        self.assertEqual(2, cache_layer.flush())
        self.assertIsNone(cache_layer.get("k1"))


if __name__ == "__main__":
    unittest.main()
