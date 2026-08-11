import json
import shutil
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "momentum-dashboard"))

import db as dashboard_db
from positions_parser import update_positions, verify_parsed  # noqa: E402


class PositionsParserTests(unittest.TestCase):
    def setUp(self):
        self.parsed = {
            "account_summary": {
                "total_assets": 100000,
                "securities_value": 90000,
                "available_cash": 10000,
                "total_pnl": 5000,
                "daily_pnl": 100,
            },
            "holdings": [
                {
                    "code": "518880", "name": "黄金ETF", "shares": 5000,
                    "price": 9.0, "cost": 8.5,
                    "market_value": 45000, "pnl": 2500, "pnl_pct": 5.88,
                },
                {
                    "code": "512880", "name": "证券ETF", "shares": 10000,
                    "price": 1.1, "cost": 1.0,
                    "market_value": 99999, "pnl": 1000, "pnl_pct": 10.0,
                },
            ],
            "trades": [
                {
                    "date": "2026-08-10", "action": "买入", "code": "518880",
                    "name": "黄金ETF", "price": 8.5, "shares": 5000, "amount": 42500,
                },
            ],
        }

    def test_verify_flags_bad_market_value(self):
        verified = verify_parsed(self.parsed)
        by_code = {h["code"]: h for h in verified["holdings"]}
        self.assertEqual("ok", by_code["518880"]["status"])
        self.assertEqual("error", by_code["512880"]["status"])
        self.assertTrue(any("市值与 股数×现价 不符" in i for i in by_code["512880"]["issues"]))
        self.assertEqual("error", verified["status"])

    def test_pnl_rounding_allowance_for_large_positions(self):
        """大持仓下成本 3 位小数舍入会按股数放大盈亏误差，不应误报。"""
        parsed = {
            "account_summary": {},
            "holdings": [
                {
                    # 真实成本 1.501775（券商盈亏 130.05 反推），显示成本 1.502 为舍入值
                    "code": "159920", "name": "恒生ETF", "shares": 18000,
                    "price": 1.509, "cost": 1.502,
                    "market_value": 27162.0, "pnl": 130.05, "pnl_pct": 0.466,
                },
            ],
            "trades": [],
        }
        verified = verify_parsed(parsed)
        holding = verified["holdings"][0]
        self.assertEqual("ok", holding["status"])
        self.assertFalse(any("盈亏与" in i for i in holding["issues"]))

    def test_pnl_mismatch_still_flagged_beyond_rounding(self):
        """超出舍入容差的真实盈亏错误仍须报错。"""
        parsed = {
            "account_summary": {},
            "holdings": [
                {
                    "code": "159920", "name": "恒生ETF", "shares": 18000,
                    "price": 1.509, "cost": 1.502,
                    "market_value": 27162.0, "pnl": 500.0, "pnl_pct": 0.466,
                },
            ],
            "trades": [],
        }
        verified = verify_parsed(parsed)
        holding = verified["holdings"][0]
        self.assertEqual("error", holding["status"])
        self.assertTrue(any("盈亏与" in i for i in holding["issues"]))

    def test_verify_flags_unknown_code(self):
        self.parsed["holdings"][0]["code"] = "999999"
        verified = verify_parsed(self.parsed)
        by_code = {h["code"]: h for h in verified["holdings"]}
        self.assertEqual("warn", by_code["999999"]["status"])
        self.assertTrue(any("不在已知 ETF 列表" in i for i in by_code["999999"]["warnings"]))

    def test_verify_flags_bad_trade(self):
        self.parsed["trades"][0]["code"] = "99"
        verified = verify_parsed(self.parsed)
        self.assertEqual("error", verified["trades"][0]["status"])
        self.assertTrue(any("代码格式非法" in i for i in verified["trades"][0]["issues"]))

    def test_cross_validation_flags_trade_mismatch(self):
        # 成交记录只买入 2000 股，与持仓 5000 股不符 → 应触发反推校验
        parsed = {
            "account_summary": {},
            "holdings": [
                {
                    "code": "518880", "name": "黄金ETF", "shares": 5000,
                    "price": 9.0, "cost": 8.5,
                    "market_value": 45000, "pnl": 2500, "pnl_pct": 5.88,
                },
            ],
            "trades": [
                {
                    "date": "2026-08-10", "action": "买入", "code": "518880",
                    "name": "黄金ETF", "price": 9.0, "shares": 2000, "amount": 18000,
                },
            ],
        }
        verified = verify_parsed(parsed)
        holding = verified["holdings"][0]
        self.assertEqual("warn", holding["status"])
        self.assertTrue(any("成交反推持仓" in i for i in holding["warnings"]))
        self.assertEqual(1, len(verified["cross_validation"]))
        self.assertEqual(2000, verified["cross_validation"][0]["calc_shares"])

    def test_update_blocks_on_errors_and_writes_snapshot(self):
        verified = verify_parsed(self.parsed)
        with self.assertRaises(ValueError):
            update_positions(verified, "测试")

        # 修正后应成功写入
        for holding in verified["holdings"]:
            if holding["code"] == "512880":
                holding["market_value"] = 11000
                holding["issues"] = []
                holding["status"] = "ok"
        verified["status"] = "ok"

        data_dir = Path(tempfile.mkdtemp())
        with tempfile.TemporaryDirectory() as tmp:
            # 让模块写临时目录（monkeypatch 路径）
            import positions_parser
            orig_file = positions_parser.POSITIONS_FILE
            orig_history = positions_parser.HISTORY_FILE
            orig_reports = positions_parser.REPORTS_DIR
            orig_db_path = dashboard_db.DB_PATH
            orig_db_conn = dashboard_db._conn
            orig_db_backend = dashboard_db.db_backend()
            positions_parser.POSITIONS_FILE = Path(tmp) / "positions_latest.json"
            positions_parser.HISTORY_FILE = Path(tmp) / "ai_parse_history.json"
            positions_parser.REPORTS_DIR = Path(tmp) / "reports"
            dashboard_db.configure("sqlite")
            dashboard_db.DB_PATH = Path(tmp) / "dashboard.db"
            dashboard_db._conn = None
            try:
                snapshot = update_positions(verified, "测试")
                db_stats = dashboard_db.stats()
                self.assertEqual(1, db_stats["tables"]["positions_snapshots"])
                self.assertEqual(1, db_stats["tables"]["parse_history"])
            finally:
                positions_parser.POSITIONS_FILE = orig_file
                positions_parser.HISTORY_FILE = orig_history
                positions_parser.REPORTS_DIR = orig_reports
                dashboard_db.configure(orig_db_backend)
                dashboard_db.DB_PATH = orig_db_path
                dashboard_db._conn = orig_db_conn

        self.assertEqual(2, len(snapshot["holdings"]))
        self.assertEqual(1, len(snapshot["trades"]))
        self.assertEqual(90.0, snapshot["account_summary"]["position_ratio"])
        self.assertIsNotNone(snapshot["holdings"][0].get("weight_pct"))
        self.assertTrue(snapshot.get("excel_file", "").endswith(".xlsx"))


if __name__ == "__main__":
    unittest.main()
