import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "momentum-dashboard"))

from grid_parser import verify_grid_configs, verify_grid_records  # noqa: E402


class GridRecordVerifyTests(unittest.TestCase):
    def test_ok_record(self):
        records = verify_grid_records(
            [{
                "code": "512010",
                "date": "2026-08-10",
                "time": "09:30",
                "action": "buy",
                "price": "0.405",
                "shares": "500",
                "base_price_before": "0.395",
                "base_price_after": "0.405",
            }],
            known_codes={"512010"},
            grid_sizes={"512010": 500},
        )
        self.assertEqual("ok", records[0]["status"])
        self.assertEqual(0.405, records[0]["price"])
        self.assertEqual(500, records[0]["shares"])
        self.assertEqual("09:30:00", records[0]["time"])  # HH:MM 补秒

    def test_error_record(self):
        records = verify_grid_records(
            [{
                "code": "123",
                "date": "2026/08/10",
                "action": "hold",
                "price": "0",
                "shares": "-5",
            }],
            known_codes={"512010"},
        )
        self.assertEqual("error", records[0]["status"])
        self.assertTrue(records[0]["issues"])

    def test_warn_when_not_grid_multiple(self):
        records = verify_grid_records(
            [{
                "code": "512010",
                "date": "2026-08-10",
                "action": "sell",
                "price": "0.42",
                "shares": "300",
            }],
            known_codes={"512010"},
            grid_sizes={"512010": 500},
        )
        self.assertEqual("warn", records[0]["status"])
        self.assertTrue(records[0]["warns"])

    def test_unknown_code(self):
        records = verify_grid_records(
            [{
                "code": "999999",
                "date": "2026-08-10",
                "action": "buy",
                "price": "1.0",
                "shares": "100",
            }],
            known_codes={"512010"},
        )
        # 6 位合法代码但不在网格配置中 → 警告而非错误（真实成交可能含货币基金等）
        self.assertEqual("warn", records[0]["status"])
        self.assertTrue(records[0]["warns"])

    def test_invalid_code_still_error(self):
        records = verify_grid_records(
            [{
                "code": "abc123",
                "date": "2026-08-10",
                "action": "buy",
                "price": "1.0",
                "shares": "100",
            }],
            known_codes={"512010"},
        )
        self.assertEqual("error", records[0]["status"])

    def test_invalid_time_is_error(self):
        records = verify_grid_records(
            [{
                "code": "512010",
                "date": "2026-08-10",
                "time": "25:99",
                "action": "buy",
                "price": "0.405",
                "shares": "500",
            }],
            known_codes={"512010"},
        )
        self.assertEqual("error", records[0]["status"])
        self.assertIn("时间格式应为 HH:MM:SS", records[0]["issues"])

    def test_trigger_type_validation(self):
        records = verify_grid_records(
            [{
                "code": "512010",
                "date": "2026-08-10",
                "action": "buy",
                "price": "0.405",
                "shares": "500",
                "trigger_type": "foo",
            }],
            known_codes={"512010"},
        )
        self.assertEqual("error", records[0]["status"])
        self.assertIn("类型需为 grid/add/reduce/momentum", records[0]["issues"])

    def test_trigger_type_default_grid(self):
        records = verify_grid_records(
            [{
                "code": "512010",
                "date": "2026-08-10",
                "action": "sell",
                "price": "0.42",
                "shares": "500",
            }],
            known_codes={"512010"},
        )
        self.assertEqual("grid", records[0]["trigger_type"])


class GridConfigVerifyTests(unittest.TestCase):
    def test_ok_config(self):
        configs = verify_grid_configs(
            [{
                "code": "513180", "name": "恒生科技ETF",
                "strategy_type": "网格交易",
                "base_price": 0.609, "spacing_up_pct": 3.5,
                "spacing_down_pct": 2.5, "price_low": 0.53,
                "price_high": 0.68,
                "order_type_sell": "限价即时买一价卖出",
                "order_type_buy": "限价即时卖一价买入",
                "shares_per_grid": 500, "base_position": 8000,
                "max_position": 18100, "levels_above": 5, "levels_below": 5,
            }],
            known_codes={"513180"},
        )
        self.assertEqual("ok", configs[0]["status"])
        self.assertEqual("513180", configs[0]["code"])
        self.assertEqual(500, configs[0]["shares_per_grid"])
        self.assertEqual([], configs[0]["issues"])

    def test_error_config(self):
        configs = verify_grid_configs(
            [{
                "code": "12x", "base_price": -1,
                "spacing_up_pct": 30, "price_low": 0.7, "price_high": 0.3,
                "shares_per_grid": 0, "base_position": 9000,
                "max_position": 8000,
            }],
            known_codes={"513180"},
        )
        self.assertEqual("error", configs[0]["status"])
        joined = "；".join(configs[0]["issues"])
        self.assertIn("代码格式非法", joined)
        self.assertIn("基准价缺失或非法", joined)
        self.assertIn("上涨间距非法", joined)
        self.assertIn("下限 >= 上限", joined)
        self.assertIn("委托数量必须为正", joined)
        self.assertIn("下限 > 上限", joined)

    def test_warn_unknown_code_and_lot_size(self):
        configs = verify_grid_configs(
            [{
                "code": "999999", "base_price": 1.0,
                "spacing_up_pct": 3.0, "spacing_down_pct": 2.0,
                "price_low": 0.9, "price_high": 1.1,
                "shares_per_grid": 350, "base_position": 1000,
                "max_position": 5000,
            }],
            known_codes={"513180"},
        )
        self.assertEqual("warn", configs[0]["status"])
        joined = "；".join(configs[0]["issues"])
        self.assertIn("不在已知 ETF 列表", joined)
        self.assertIn("不是 100 的整数倍", joined)


if __name__ == "__main__":
    unittest.main()
