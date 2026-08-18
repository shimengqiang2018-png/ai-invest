"""Walk-forward 样本外验证的单元测试。

用 mock 回测函数 + 合成行情，验证折生成、train 选参、样本外汇总与排名。
"""

import unittest
from datetime import datetime, timedelta

from tools.walk_forward import (
    concat_fold_returns,
    generate_folds,
    run_walk_forward,
)


def _trading_days(start: str, end: str) -> list[str]:
    """生成 start~end 之间的工作日日期字符串。"""
    days = []
    d = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    while d <= e:
        if d.weekday() < 5:
            days.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return days


def _make_market_data(codes, start="2018-01-01", end="2026-01-01"):
    days = _trading_days(start, end)
    return {
        code: [{"date": d, "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0}
               for d in days]
        for code in codes
    }


class GenerateFoldsTests(unittest.TestCase):
    def test_fold_boundaries(self):
        folds = generate_folds("2018-01-01", "2026-01-01", train_months=24, test_months=12, step_months=12)
        self.assertGreaterEqual(len(folds), 3)
        for train_start, train_end, test_end in folds:
            ts = datetime.strptime(train_start, "%Y-%m-%d")
            te = datetime.strptime(train_end, "%Y-%m-%d")
            ee = datetime.strptime(test_end, "%Y-%m-%d")
            self.assertGreaterEqual((te - ts).days, 365)   # train ≈ 24 月
            self.assertGreaterEqual((ee - te).days, 365)   # test ≈ 12 月

    def test_insufficient_data(self):
        folds = generate_folds("2025-01-01", "2025-06-01", train_months=24, test_months=12, step_months=12)
        self.assertEqual(folds, [])


class WalkForwardRunTests(unittest.TestCase):
    def _mock_run(self, sharpe_map, return_map):
        def _run(pool, start_date, end_date, freq=None, momentum_period=None,
                 include_bench=False, quiet=True, market_data=None, switch_buffer=1.0):
            code = next(iter(pool))
            sharpe = sharpe_map[code]
            ret = return_map[code]
            nav = [(start_date, 100.0), (end_date, 100.0 * (1 + ret / 100))]
            return {
                "performance": {
                    "sharpe": sharpe,
                    "total_return_pct": ret,
                    "benchmark_equal_weight_pct": 5.0,
                },
                "daily_nav": nav,
            }
        return _run

    def test_train_selects_highest_sharpe(self):
        """train 段应选 Sharpe 最高的候选。"""
        candidates = [
            {"codes": ["510300"], "momentum": 40, "label": "A"},
            {"codes": ["159915"], "momentum": 40, "label": "B"},
        ]
        run_fn = self._mock_run({"510300": 1.0, "159915": 0.5},
                                {"510300": 10.0, "159915": 20.0})
        market_data = _make_market_data(["510300", "159915"])
        r = run_walk_forward(candidates, market_data=market_data, run_fn=run_fn)
        self.assertGreaterEqual(r["n_folds"], 3)
        for fr in r["folds"]:
            self.assertEqual(fr["selected"], "A", "train 段应选 Sharpe 最高的 A")

    def test_oos_ranking_differs_from_train_selection(self):
        """train 选 A 但 test 段 B 表现更好 → 样本外排名 B 居前，验证排名迁移。"""
        candidates = [
            {"codes": ["510300"], "momentum": 40, "label": "A"},
            {"codes": ["159915"], "momentum": 40, "label": "B"},
        ]
        run_fn = self._mock_run({"510300": 1.0, "159915": 0.5},
                                {"510300": 10.0, "159915": 20.0})
        r = run_walk_forward(candidates, market_data=_make_market_data(["510300", "159915"]), run_fn=run_fn)
        by_label = {c["label"]: c for c in r["candidates"]}
        self.assertLess(by_label["B"]["oos_rank"], by_label["A"]["oos_rank"],
                        "test 段 B 收益更高，样本外排名应领先")

    def test_oos_sharpe_and_total_consistent(self):
        candidates = [{"codes": ["510300"], "momentum": 40, "label": "A"}]
        run_fn = self._mock_run({"510300": 1.0}, {"510300": 10.0})
        r = run_walk_forward(candidates, market_data=_make_market_data(["510300"]), run_fn=run_fn)
        c = r["candidates"][0]
        # 每折 test 收益都是 +10%，几何连乘应 > 单折
        self.assertGreater(c["oos_total_pct"], 10.0)
        self.assertIsNotNone(c["oos_sharpe"])


class ConcatFoldReturnsTests(unittest.TestCase):
    def test_concat(self):
        nav1 = [("d1", 100.0), ("d2", 110.0)]   # +10%
        nav2 = [("d3", 100.0), ("d4", 90.0)]    # -10%
        rets = concat_fold_returns([nav1, nav2])
        self.assertEqual(len(rets), 2)
        self.assertAlmostEqual(rets[0], 0.10)
        self.assertAlmostEqual(rets[1], -0.10)


if __name__ == "__main__":
    unittest.main()
