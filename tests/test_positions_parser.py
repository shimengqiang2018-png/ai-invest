import json
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "momentum-dashboard"))

from positions_parser import verify_parsed  # noqa: E402


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

    def test_ocr_code_auto_correction(self):
        """不在已知列表的代码，若与某已知代码仅一位不同则自动修正，名称同步修正。"""
        from unittest.mock import patch

        parsed = {
            "account_summary": {},
            "holdings": [
                {
                    # 512880 被 OCR 读成 519880，名称读成 WE ETF
                    "code": "519880", "name": "WE ETF", "shares": 14100,
                    "price": 1.097, "cost": 1.09,
                    "market_value": 15467.7, "pnl": 98.7, "pnl_pct": 0.64,
                },
            ],
            "trades": [],
        }
        # 用 K 线收盘价校验：只有 512880 的收盘价接近 市值÷份额(≈1.097)
        def fake_close(code):
            return 1.097 if code == "512880" else None

        with patch("positions_parser._cached_last_close", side_effect=fake_close):
            verified = verify_parsed(parsed)
        holding = verified["holdings"][0]
        self.assertEqual("512880", holding["code"])
        self.assertEqual("证券ETF国泰", holding["name"])
        self.assertTrue(
            any("已自动修正为 512880" in w for w in holding["warnings"])
        )
        self.assertNotEqual("error", holding["status"])

    def test_classify_holding_grid_momentum_shared(self):
        from positions_parser import _classify_holding
        # 512880 网格（配置底仓 8000，不超过持仓内）
        strategy, bucket, base = _classify_holding("512880", 14100)
        self.assertEqual("网格", strategy)
        self.assertEqual(8000, base)
        # 159920 动量 → 底仓 0
        strategy, bucket, base = _classify_holding("159920", 18000)
        self.assertEqual("动量", strategy)
        self.assertEqual(0, base)
        # 159915 共用 → 无网格配置时底仓为 0
        strategy, bucket, base = _classify_holding("159915", 1600)
        self.assertEqual("共用", strategy)
        self.assertEqual(0, base)


if __name__ == "__main__":
    unittest.main()
