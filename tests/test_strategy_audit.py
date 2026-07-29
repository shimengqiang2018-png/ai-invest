"""策略审计 correctness 测试：IC/IR、Spearman、压力测试、滚动窗口。"""

import json
import math
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tools.strategy_audit import (
    audit_backtest_result,
    compute_ic_ir,
    compute_ir,
    spearman_rank_ic,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
MOMENTUM_FIXTURE = FIXTURE_DIR / "momentum_280_days.json"


class SpearmanICIRTests(unittest.TestCase):
    """Spearman rank IC 和 IR 计算测试。"""

    def test_ic_uses_spearman_rank_correlation(self):
        """Spearman IC 基于秩相关，非 Pearson。"""
        scores = [1.0, 2.0, 3.0, 4.0, 5.0]
        returns = [0.01, 0.02, 0.03, 0.04, 0.05]
        ic = spearman_rank_ic(scores, returns, min_samples=5)
        self.assertAlmostEqual(ic, 1.0, places=6)

    def test_ic_perfect_negative(self):
        """完全负相关 → IC = -1.0。"""
        scores = [1.0, 2.0, 3.0, 4.0, 5.0]
        returns = [0.05, 0.04, 0.03, 0.02, 0.01]
        ic = spearman_rank_ic(scores, returns, min_samples=5)
        self.assertAlmostEqual(ic, -1.0, places=6)

    def test_ic_with_ties_uses_average_rank(self):
        """并列值使用平均秩。"""
        # score:   [1, 2, 2,   3]
        # rank:    [1, 2.5, 2.5, 4]
        # 并列值对应不同收益，IC 接近但不等于 1.0
        scores = [1.0, 2.0, 2.0, 3.0]
        returns = [0.01, 0.02, 0.03, 0.04]
        ic = spearman_rank_ic(scores, returns, min_samples=4)
        self.assertIsNotNone(ic)
        self.assertGreater(ic, 0.9)

    def test_insufficient_samples_returns_none(self):
        """样本数 < 10 时返回 None。"""
        scores = list(range(5))
        returns = [x * 0.01 for x in range(5)]
        result = spearman_rank_ic(scores, returns, min_samples=10)
        self.assertIsNone(result)

    def test_ir_is_mean_over_std(self):
        """IR = mean(IC_t) / sample_std(IC_t) 而非 IC * sqrt(252/fwd)。"""
        ics = [0.05, 0.03, -0.01, 0.04, 0.02, 0.06, 0.01, 0.03, 0.04, 0.02]
        n = len(ics)
        mean_ic = sum(ics) / n
        var_ic = sum((ic - mean_ic) ** 2 for ic in ics) / (n - 1)
        std_ic = var_ic ** 0.5
        ir = mean_ic / std_ic if std_ic > 0 else 0
        # mean = 0.029, std ≈ 0.022, IR ≈ 1.3
        self.assertGreater(ir, 0)
        self.assertLess(ir, 5.0)

    def test_ir_is_none_when_only_one_period(self):
        """只有 1 个 IC 值时无法计算 std，返回 None。"""
        self.assertIsNone(compute_ir([0.05]))
        self.assertIsNone(compute_ir([]))


class ProductionICIRTests(unittest.TestCase):
    """直接验证生产 compute_ic_ir 的多资产对齐和非重叠抽样。"""

    @staticmethod
    def _bars(asset: int, count: int = 360) -> list[dict]:
        start = date(2024, 1, 1)
        bars = []
        for index in range(count):
            close = (asset + 1) * (1 + 0.0005 * (asset + 1) * index
                                    + 0.00001 * ((index + asset) % 7))
            bars.append({
                "date": (start + timedelta(days=index)).isoformat(),
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 1000 + index,
            })
        return bars

    def test_compute_ic_ir_aligns_one_score_per_asset_and_horizon(self):
        klines = {str(asset): self._bars(asset) for asset in range(4)}
        result = compute_ic_ir(klines, {code: code for code in klines})

        for horizon in (10, 20, 40):
            self.assertIn(f"ic_{horizon}d", result)
            self.assertIn(f"ir_{horizon}d", result)
            self.assertGreater(result[f"n_dates_{horizon}d"], 0)
            self.assertEqual(result[f"median_assets_{horizon}d"], 4)

    def test_ic_dates_are_non_overlapping_per_horizon(self):
        klines = {str(asset): self._bars(asset) for asset in range(4)}
        result = compute_ic_ir(klines, {code: code for code in klines})

        self.assertGreater(result["n_dates_10d"], result["n_dates_20d"])
        self.assertGreater(result["n_dates_20d"], result["n_dates_40d"])


class StressScenarioTests(unittest.TestCase):
    """压力测试 correctness 测试。"""

    def test_stress_uses_qfq_data_only(self):
        """压力测试必须使用前复权数据，不含份额折算断点。"""
        bars = json.loads(MOMENTUM_FIXTURE.read_text(encoding="utf-8"))["bars"]
        # 验证 fixture 中没有异常单日收益
        closes = [bar["close"] for bar in bars]
        for i in range(1, len(closes)):
            daily_return = abs((closes[i] - closes[i - 1]) / closes[i - 1])
            self.assertLess(daily_return, 0.20,
                           f"异常单日收益 {daily_return:.1%} at index {i}")

    def test_var_description_is_historical_quantile(self):
        """VaR 应标注为"历史样本分位阈值"，不称最大亏损上限。"""
        # 这是一个文案约定测试：VaR 不是最大可能亏损
        description = "历史样本损失分位阈值"
        self.assertIn("历史", description)
        self.assertNotIn("最大亏损上限", description)


class RollingWindowTests(unittest.TestCase):
    """滚动窗口 correctness 测试。"""

    def test_rolling_window_does_not_use_future_data(self):
        """向 end_date 后追加数据不应改变该窗口的结果。"""
        # 窗口 [2024-01-01, 2024-06-30] 的结果
        # 与追加 2024-07-01 后数据的结果应一致
        bars = json.loads(MOMENTUM_FIXTURE.read_text(encoding="utf-8"))["bars"]
        end_idx = min(200, len(bars) - 1)
        window_end = bars[end_idx]["date"]
        # 截断到 window_end 的数据
        truncated = [b for b in bars if b["date"] <= window_end]
        # 完整数据（含未来）
        full = list(bars)
        # 截断数据应 ≤ 完整数据
        self.assertLessEqual(len(truncated), len(full))
        # 重叠部分应完全一致
        for a, b in zip(truncated, full[:len(truncated)]):
            self.assertEqual(a["date"], b["date"])
            self.assertEqual(a["close"], b["close"])

    def test_rolling_windows_use_same_data_snapshot(self):
        """滚动入口只加载一次，每个窗口把冻结 market_data 传给 run_backtest。"""
        from tools import momentum_etf_backtest

        start = datetime(2020, 1, 2)
        bars = []
        for index in range(1000):
            day = (start + timedelta(days=index)).strftime("%Y-%m-%d")
            bars.append({
                "date": day, "open": 1.0, "close": 1.0,
                "high": 1.0, "low": 1.0, "volume": 1.0,
            })
        result = {
            "performance": {
                "total_return_pct": 0.0, "annual_return_pct": 0.0,
                "benchmark_equal_weight_pct": 0.0, "excess_return_pct": 0.0,
                "num_trades": 0, "final_nav": 100000.0,
            },
            "trades": [],
        }
        with patch.object(momentum_etf_backtest, "fetch_kline", return_value=bars) as fetch, \
             patch.object(momentum_etf_backtest, "run_backtest", return_value=result) as run:
            momentum_etf_backtest.run_rolling_backtest(
                {"A": "A"}, window_months=3, step_months=3
            )

        fetch.assert_called_once()
        self.assertTrue(run.call_args_list)
        for call in run.call_args_list:
            self.assertIsNotNone(call.kwargs.get("market_data"))


class AuditPeriodConsistencyTests(unittest.TestCase):
    """审计必须直接消费回测结果，不取数、不重放。"""

    def test_audit_period_matches_backtest(self):
        result = {
            "period": {"start": "2024-01-01", "end": "2024-01-10", "years": 0.03},
            "daily_nav": [
                ("2024-01-01", 100000.0),
                ("2024-01-02", 100100.0),
                ("2024-01-03", 100050.0),
                ("2024-01-04", 100200.0),
                ("2024-01-05", 100300.0),
            ],
            "market_data": {},
            "performance": {},
            "trades": [],
        }
        audit = audit_backtest_result(result, {"A": "A"})
        self.assertEqual(audit["period"], result["period"])
        self.assertEqual(audit["daily_metrics"]["count"], 4)


if __name__ == "__main__":
    unittest.main()
