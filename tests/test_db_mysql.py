"""db 层 MySQL 集成测试（使用独立测试库 ai_invest_scratch，不污染生产库）。

db.py 已改为 SQLAlchemy + MySQL only；此测试覆盖核心数据层函数的往返与联动。
依赖本地 MySQL（invest 用户已授予 ai_invest_scratch 库权限）。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent / "momentum-dashboard"
TEST_DB = "ai_invest_scratch"
MYSQL = "/usr/local/opt/mysql@8.4/bin/mysql"


def _run_mysql_root(sql_or_path: str, is_file: bool = False):
    args = [MYSQL, "-u", "root", "-p123456"]
    if is_file:
        with open(sql_or_path, encoding="utf-8") as handle:
            subprocess.run(args + [TEST_DB], stdin=handle, check=True)
    else:
        subprocess.run(args + ["-e", sql_or_path], check=True)


class DbMysqlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 独立加载 db 模块连 scratch 库，短暂切换 DB_NAME 后立即恢复，
        # 避免污染同进程其他测试（生产库单例）。
        _orig_db_name = os.environ.get("DB_NAME")
        os.environ["DB_NAME"] = TEST_DB
        _spec = importlib.util.spec_from_file_location("db_scratch", str(ROOT / "db.py"))
        cls.db = importlib.util.module_from_spec(_spec)
        sys.modules["db_scratch"] = cls.db
        _spec.loader.exec_module(cls.db)
        if _orig_db_name is None:
            os.environ.pop("DB_NAME", None)
        else:
            os.environ["DB_NAME"] = _orig_db_name

        _run_mysql_root(
            f"CREATE DATABASE IF NOT EXISTS {TEST_DB} "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        _run_mysql_root(str(ROOT / "schema_mysql.sql"), is_file=True)

    def setUp(self):
        for table in self.db.ALLOWED_TABLES:
            self.db._exec(f"DELETE FROM `{table}`")

    def test_cache_roundtrip(self):
        self.db.cache_set("k1", {"a": 1})
        self.assertEqual({"a": 1}, self.db.cache_get("k1", ttl=60))
        self.assertIsNone(self.db.cache_get("k1", ttl=0))  # 过期

    def test_positions_snapshot_roundtrip(self):
        self.db.save_positions_snapshot(
            {"date": "2026-08-13T10:00:00+08:00", "source": "test", "holdings": [1]}
        )
        latest = self.db.latest_positions_snapshot()
        self.assertEqual("2026-08-13T10:00:00+08:00", latest["date"])
        self.assertEqual([1], latest["holdings"])

    def test_holdings_roundtrip(self):
        self.db.save_holdings_current(
            [
                {
                    "code": "159915", "name": "创业板ETF", "shares": 1600,
                    "price": 3.5, "cost": 3.0, "market_value": 5600,
                    "pnl": 800, "pnl_pct": 16.7, "strategy": "动量",
                    "bucket": "动量子账户", "base_shares": 0,
                    "source": "test", "verified": True,
                }
            ],
            {"total_assets": 100000, "source": "test"},
        )
        current = self.db.load_holdings_current()
        self.assertEqual(1, len(current["holdings"]))
        self.assertEqual("159915", current["holdings"][0]["code"])
        self.assertEqual(100000, current["account_summary"]["total_assets"])

    def test_grid_configs_roundtrip(self):
        self.db.save_grid_configs([
            {
                "code": "513180", "name": "恒生科技ETF", "strategy_type": "网格交易",
                "base_price": 0.609, "spacing_up_pct": 3.5, "spacing_down_pct": 2.5,
                "base_position": 8000, "max_position": 18100,
                "shares_per_grid": 500, "status": "active",
            }
        ])
        cfg = self.db.get_grid_config("513180")
        self.assertEqual("恒生科技ETF", cfg["name"])
        self.assertEqual(0.609, cfg["base_price"])

    def test_momentum_pools_roundtrip(self):
        self.db.save_momentum_pools([
            {
                "pool_key": "recommended", "pool_type": "signal",
                "description": "推荐池", "codes": "159915,518880",
                "defensive_code": "511880", "is_recommended": 1, "enabled": 1,
            }
        ])
        pools = self.db.load_momentum_pools()
        self.assertEqual(1, len(pools))
        self.assertEqual("159915,518880", pools[0]["codes"])

    def test_grid_trigger_updates_holding(self):
        # 先写持仓 + 网格配置，再录入买入触发，验证持仓联动（买增卖减）
        self.db.save_holdings_current([
            {
                "code": "513180", "name": "恒生科技ETF", "shares": 1000,
                "price": 0.6, "cost": 0.5, "market_value": 600,
                "pnl": 100, "pnl_pct": 20.0, "strategy": "网格",
                "bucket": "网格子账户", "base_shares": 800,
                "source": "test", "verified": True,
            }
        ])
        self.db.append_grid_trigger(
            code="513180", name="恒生科技ETF", trigger_date="2026-08-13",
            trade_time="10:00:00", action="buy", price=0.6, shares=500,
        )
        current = self.db.load_holdings_current()
        holding = current["holdings"][0]
        self.assertEqual(1500, holding["shares"])  # 1000 + 500


if __name__ == "__main__":
    unittest.main()
