"""交易账本单元测试：佣金、滑点、FIFO、手数取整、NAV 恒等式。"""

import unittest
from decimal import Decimal

from tools.trading_ledger import (
    ExecutionConfig,
    LedgerEntry,
    Position,
    TradingLedger,
    compute_buy_quantity,
)


class ExecutionConfigTests(unittest.TestCase):
    """费率 / 手数默认值校验。"""

    def test_default_commission_is_two_point_five_per_ten_thousand(self):
        config = ExecutionConfig()
        self.assertEqual(config.commission_rate, Decimal("0.00025"))

    def test_default_slippage_is_five_per_ten_thousand(self):
        config = ExecutionConfig()
        self.assertEqual(config.slippage_rate, Decimal("0.0005"))

    def test_board_lot_defaults_to_100(self):
        config = ExecutionConfig()
        self.assertEqual(config.board_lot, 100)

    def test_minimum_commission_is_zero_for_mianwu(self):
        config = ExecutionConfig()
        self.assertEqual(config.minimum_commission, Decimal("0"))


class CommissionCalculationTests(unittest.TestCase):
    """佣金计算测试。"""

    def setUp(self):
        self.config = ExecutionConfig()

    def test_ten_thousand_yuan_commission_is_two_point_five(self):
        """¥10,000 成交金额，佣金 = ¥2.50。"""
        ledger = TradingLedger(self.config)
        entry = ledger.add_buy("2024-01-01", "2024-01-02", 1.0, "TEST", 10000,
                               reason="test")
        self.assertIsNotNone(entry)
        self.assertAlmostEqual(entry.commission, 2.50, places=2)
        # gross = 1.0 * 1.0005 * 10000 = 10005
        # commission = 10005 * 0.00025 = 2.50125 ≈ 2.50

    def test_sell_ten_thousand_yuan_commission_is_two_point_five(self):
        """卖出 ¥10,000 成交金额，佣金 = ¥2.50。"""
        lots = [("2024-01-01", 10000, 10000.0)]
        ledger = TradingLedger(self.config)
        entry = ledger.add_sell("2024-01-02", "2024-01-03", 1.0, "TEST", 10000,
                                lots, reason="test")
        self.assertIsNotNone(entry)
        self.assertAlmostEqual(entry.commission, 2.50, places=2)


class SlippageTests(unittest.TestCase):
    """滑点计算测试。"""

    def setUp(self):
        self.config = ExecutionConfig()

    def test_buy_slippage_is_upward(self):
        """买入时 fill_price = reference * (1 + 0.0005)，向上滑点。"""
        ledger = TradingLedger(self.config)
        entry = ledger.add_buy("2024-01-01", "2024-01-02", 10.0, "TEST", 1000)
        self.assertIsNotNone(entry)
        self.assertAlmostEqual(entry.reference_price, 10.0)
        self.assertGreater(entry.fill_price, entry.reference_price)
        expected_fill = 10.0 * 1.0005
        self.assertAlmostEqual(entry.fill_price, expected_fill, places=6)

    def test_sell_slippage_is_downward(self):
        """卖出时 fill_price = reference * (1 - 0.0005)，向下滑点。"""
        lots = [("2024-01-01", 1000, 10000.0)]
        ledger = TradingLedger(self.config)
        entry = ledger.add_sell("2024-01-02", "2024-01-03", 10.0, "TEST", 1000, lots)
        self.assertIsNotNone(entry)
        self.assertLess(entry.fill_price, entry.reference_price)
        expected_fill = 10.0 * 0.9995
        self.assertAlmostEqual(entry.fill_price, expected_fill, places=6)


class BoardLotTests(unittest.TestCase):
    """100 股整数手测试。"""

    def test_buy_quantity_is_multiple_of_100(self):
        qty = compute_buy_quantity(50000, 10.5)
        self.assertEqual(qty % 100, 0)
        self.assertEqual(qty, 4700)  # 50000/10.5 ≈ 4761 → 4700

    def test_buy_rejects_non_lot_quantity(self):
        ledger = TradingLedger()
        with self.assertRaises(ValueError):
            ledger.add_buy("2024-01-01", "2024-01-02", 10.0, "TEST", 150)

    def test_sell_rejects_non_lot_quantity(self):
        ledger = TradingLedger()
        with self.assertRaises(ValueError):
            ledger.add_sell("2024-01-01", "2024-01-02", 10.0, "TEST", 150, [])


class FIFOTests(unittest.TestCase):
    """FIFO 出库测试。"""

    def test_fifo_sells_oldest_lot_first(self):
        """先买入的 lot 先被卖出。"""
        config = ExecutionConfig()
        lots = [
            ("2024-01-01", 500, 5000.0),   # cost=10/股
            ("2024-01-10", 500, 6000.0),   # cost=12/股
        ]
        ledger = TradingLedger(config)
        entry = ledger.add_sell("2024-01-15", "2024-01-16", 12.0, "TEST", 500, lots)
        self.assertIsNotNone(entry)
        # 卖出 500 股 @ 12.0，滑点向下: 12 * 0.9995 = 11.994
        # 从 lot1 出: cost = 5000
        # gross = 11.994 * 500 = 5997
        # commission = 5997 * 0.00025 ≈ 1.50
        # realized = 5997 - 1.50 - 5000 = 995.50
        self.assertAlmostEqual(entry.cost_basis_after, 6000.0, places=2)  # 只剩 lot2
        self.assertAlmostEqual(entry.shares_after, 500)

    def test_fifo_partial_lot_sell(self):
        """部分卖出一个 lot 时，成本按比例分摊。"""
        lots = [("2024-01-01", 1000, 10000.0)]  # cost=10/股
        config = ExecutionConfig()
        ledger = TradingLedger(config)
        entry = ledger.add_sell("2024-01-15", "2024-01-16", 12.0, "TEST", 300, lots)
        self.assertIsNotNone(entry)
        # cost_of_sold = 300 * 10 = 3000
        self.assertAlmostEqual(entry.cost_basis_after, 7000.0, places=2)
        self.assertEqual(entry.shares_after, 700)

    def test_sell_fails_when_shares_insufficient(self):
        """持仓不足时卖出失败，返回 None。"""
        lots = [("2024-01-01", 100, 1000.0)]
        ledger = TradingLedger()
        entry = ledger.add_sell("2024-01-15", "2024-01-16", 10.0, "TEST", 500, lots)
        self.assertIsNone(entry)


class LedgerPositionStateTests(unittest.TestCase):
    """账本必须是 FIFO 持仓和完整净成本的唯一事实源。"""

    def test_same_fill_round_trip_deducts_both_commissions(self):
        ledger = TradingLedger(ExecutionConfig())
        buy = ledger.add_buy("2024-01-01", "2024-01-02", 10.0, "TEST", 1000)
        sell_reference = buy.fill_price / (1 - float(ledger.execution.slippage_rate))
        sell = ledger.add_sell(
            "2024-01-03", "2024-01-04", sell_reference, "TEST", 1000
        )

        self.assertIsNotNone(sell)
        self.assertAlmostEqual(
            sell.realized_pnl,
            -(buy.commission + sell.commission),
            places=8,
        )
        self.assertEqual(ledger.position("TEST").shares, 0)

    def test_consecutive_buys_report_accumulated_position(self):
        ledger = TradingLedger(ExecutionConfig())
        first = ledger.add_buy("2024-01-01", "2024-01-02", 10.0, "TEST", 1000)
        second = ledger.add_buy("2024-01-03", "2024-01-04", 11.0, "TEST", 500)

        self.assertEqual(second.shares_after, 1500)
        self.assertAlmostEqual(
            second.cost_basis_after,
            abs(first.net_cash_flow) + abs(second.net_cash_flow),
            places=8,
        )
        self.assertEqual(len(ledger.position("TEST").lots), 2)

    def test_partial_fifo_sell_allocates_full_buy_cost(self):
        ledger = TradingLedger(ExecutionConfig())
        first = ledger.add_buy("2024-01-01", "2024-01-02", 10.0, "TEST", 1000)
        second = ledger.add_buy("2024-01-03", "2024-01-04", 12.0, "TEST", 500)
        sell_reference = first.fill_price / (1 - float(ledger.execution.slippage_rate))
        sell = ledger.add_sell(
            "2024-01-05", "2024-01-08", sell_reference, "TEST", 500
        )

        expected_first_remaining = abs(first.net_cash_flow) / 2
        expected_cost_after = expected_first_remaining + abs(second.net_cash_flow)
        self.assertAlmostEqual(sell.cost_basis_after, expected_cost_after, places=8)
        self.assertEqual(sell.shares_after, 1000)


class NAVInvariantTests(unittest.TestCase):
    """账本恒等式测试。"""

    def test_cash_entry_consistency(self):
        """每笔交易后 cash_after = 前一笔 cash_after + net_cash_flow。"""
        config = ExecutionConfig()
        lots = [("2024-01-01", 1000, 10000.0)]
        ledger = TradingLedger(config)
        ledger.cash = 50000.0
        initial = ledger.cash
        entry = ledger.add_sell("2024-01-15", "2024-01-16", 12.0, "TEST", 500, lots)
        self.assertIsNotNone(entry)
        self.assertAlmostEqual(initial + entry.net_cash_flow, entry.cash_after, places=2)

    def test_buy_does_not_have_realized_pnl(self):
        """买入不产生已实现盈亏。"""
        ledger = TradingLedger(ExecutionConfig())
        entry = ledger.add_buy("2024-01-01", "2024-01-02", 10.0, "TEST", 1000)
        self.assertEqual(entry.realized_pnl, 0.0)

    def test_commission_and_slippage_totals(self):
        """佣金和滑点总额正确。"""
        config = ExecutionConfig()
        lots = [("2024-01-01", 2000, 20000.0)]
        ledger = TradingLedger(config)
        ledger.add_buy("2024-01-01", "2024-01-02", 10.0, "TEST", 1000)
        ledger.add_sell("2024-01-15", "2024-01-16", 12.0, "TEST", 1000, lots)
        self.assertEqual(len(ledger.entries), 2)
        total_comm = ledger.total_commission()
        total_slip = ledger.total_slippage()
        self.assertGreater(total_comm, 0)
        self.assertGreater(total_slip, 0)


if __name__ == "__main__":
    unittest.main()
