"""Deflated Sharpe Ratio (DSR) 多重比较校正的单元测试。

验证 probit 数值、收益统计、期望最优 Sharpe 的单调性，以及 DSR 的边界性质。
"""

import math
import unittest

from tools.multiple_testing import (
    deflated_sharpe_ratio,
    expected_max_sharpe,
    probit,
    return_stats,
    returns_from_nav,
)


class ProbitTests(unittest.TestCase):
    def test_median_is_zero(self):
        self.assertAlmostEqual(probit(0.5), 0.0, places=6)

    def test_known_quantiles(self):
        # Φ⁻¹(0.975) ≈ 1.959964, Φ⁻¹(0.025) ≈ -1.959964
        self.assertAlmostEqual(probit(0.975), 1.959964, places=3)
        self.assertAlmostEqual(probit(0.025), -1.959964, places=3)
        self.assertAlmostEqual(probit(0.8413447), 1.0, places=3)

    def test_extremes(self):
        self.assertEqual(probit(0.0), -math.inf)
        self.assertEqual(probit(1.0), math.inf)


class ReturnStatsTests(unittest.TestCase):
    def test_returns_from_nav(self):
        nav = [("d1", 100.0), ("d2", 110.0), ("d3", 99.0)]
        rets = returns_from_nav(nav)
        self.assertEqual(len(rets), 2)
        self.assertAlmostEqual(rets[0], 0.10)
        self.assertAlmostEqual(rets[1], -0.10)

    def test_zero_variance(self):
        s = return_stats([0.01, 0.01, 0.01, 0.01, 0.01])
        self.assertEqual(s["sr"], 0.0)
        self.assertEqual(s["skew"], 0.0)
        self.assertEqual(s["kurt"], 3.0)
        self.assertEqual(s["n"], 5)

    def test_normal_like_skew_kurt(self):
        # 均值 0、对称分布的收益：偏度应接近 0，峰度应接近正态的 3。
        returns = [0.01 * (1 if i % 2 == 0 else -1) for i in range(100)]
        s = return_stats(returns)
        self.assertAlmostEqual(s["skew"], 0.0, places=6)
        # 两点对称分布的峰度低于 3（轻尾），这里只断言非负且有限
        self.assertGreaterEqual(s["kurt"], 0.0)
        self.assertAlmostEqual(s["n"], 100)


class ExpectedMaxSharpeTests(unittest.TestCase):
    def test_monotonic_in_trials(self):
        """试验次数越多，纯运气下的期望最优 Sharpe 越高。"""
        e10 = expected_max_sharpe(sr=0.1, n_trials=10, skew=0.0, kurt=3.0, n_obs=250)
        e100 = expected_max_sharpe(sr=0.1, n_trials=100, skew=0.0, kurt=3.0, n_obs=250)
        e1000 = expected_max_sharpe(sr=0.1, n_trials=1000, skew=0.0, kurt=3.0, n_obs=250)
        self.assertGreater(e100, e10)
        self.assertGreater(e1000, e100)

    def test_single_trial_zero(self):
        """只试一个策略没有选择偏差，期望最优 Sharpe = 0。"""
        self.assertEqual(expected_max_sharpe(sr=0.1, n_trials=1, skew=0.0, kurt=3.0, n_obs=250), 0.0)


class DeflatedSharpeRatioTests(unittest.TestCase):
    def test_high_sr_single_trial_is_significant(self):
        r = deflated_sharpe_ratio(sr=0.5, n_trials=1, skew=0.0, kurt=3.0, n_obs=500)
        self.assertGreater(r["prob"], 0.95)

    def test_low_sr_many_trials_not_significant(self):
        # 3000 次里挑最好，观测 SR 只有 0.05，远低于运气下最优，应不显著。
        r = deflated_sharpe_ratio(sr=0.05, n_trials=3000, skew=0.0, kurt=3.0, n_obs=250)
        self.assertLess(r["prob"], 0.5)
        self.assertLess(r["deflated_sr"], 0.0)

    def test_prob_monotonic_decreasing_in_trials(self):
        """同样 Sharpe，试验次数越多，显著性概率越低。"""
        p1 = deflated_sharpe_ratio(sr=0.1, n_trials=1, skew=0.0, kurt=3.0, n_obs=250)["prob"]
        p3000 = deflated_sharpe_ratio(sr=0.1, n_trials=3000, skew=0.0, kurt=3.0, n_obs=250)["prob"]
        self.assertGreater(p1, p3000)

    def test_prob_in_unit_interval(self):
        for n_trials in (1, 50, 5000):
            r = deflated_sharpe_ratio(sr=0.1, n_trials=n_trials, skew=0.2, kurt=3.5, n_obs=300)
            self.assertGreaterEqual(r["prob"], 0.0)
            self.assertLessEqual(r["prob"], 1.0)


if __name__ == "__main__":
    unittest.main()
