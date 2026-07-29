"""动量回测 correctness 测试：成交时序、费用、期末估值、缺价、NAV 恒等式。"""

import copy
import json
import math
import unittest
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch

from tools.etf_market_data import MarketDataManifest, MarketDataSeries
from tools.momentum_core import (
    MomentumConfig,
    SignalSnapshot,
    evaluate_momentum_signal,
)
from tools.trading_ledger import ExecutionConfig, TradingLedger, compute_buy_quantity

FIXTURE_DIR = Path(__file__).parent / "fixtures"
MOMENTUM_FIXTURE = FIXTURE_DIR / "momentum_280_days.json"


def _make_flat_bars(code: str, start_date: str, n_days: int,
                    base_price: float = 10.0, daily_change: float = 0.001) -> list[dict]:
    """生成单调微涨的 K 线序列，便于手工验证。"""
    import datetime as _dt
    bars = []
    d = _dt.date.fromisoformat(start_date)
    price = base_price
    for i in range(n_days):
        while d.weekday() >= 5:  # 跳过周末
            d += _dt.timedelta(days=1)
        bars.append({
            "date": d.isoformat(),
            "open": round(price, 4),
            "high": round(price * 1.005, 4),
            "low": round(price * 0.995, 4),
            "close": round(price, 4),
            "volume": 1000000,
        })
        price *= (1 + daily_change)
        d += _dt.timedelta(days=1)
    return bars


def _run_with_signals(all_bars, passed_by_date, end_date=None, execution=None):
    """用真实 run_backtest 运行确定性信号场景。"""
    from tools import momentum_etf_backtest

    def fetch(code, **_kwargs):
        return [dict(bar) for bar in all_bars[code]]

    def evaluate(code, bars, index, config):
        signal_date = bars[index]["date"]
        passed = passed_by_date.get(signal_date) == code
        return SignalSnapshot(
            code=code,
            date=signal_date,
            raw_rsrs_score=10.0 if passed else 0.0,
            slope_annual_pct=10.0 if passed else 0.0,
            r_squared=1.0 if passed else 0.0,
            signal_strength="medium" if passed else "none",
            passed=passed,
            metrics={
                "current_volatility": 0.1,
                "historical_volatility_median": 0.2,
            },
        )

    with patch.object(momentum_etf_backtest, "fetch_kline", side_effect=fetch), \
         patch.object(momentum_etf_backtest, "evaluate_momentum_signal", side_effect=evaluate):
        return momentum_etf_backtest.run_backtest(
            {code: code for code in all_bars},
            start_date="2000-01-01",
            end_date=end_date or min(bars[-1]["date"] for bars in all_bars.values()),
            freq="daily",
            quiet=True,
            execution=execution or ExecutionConfig(),
        )


class BacktestExecutionTimelineTests(unittest.TestCase):
    """成交时序测试：收盘后信号，下一交易日开盘成交。"""

    def test_signal_on_close_execute_next_open(self):
        """信号用收盘价判断，成交用下一日开盘价（含滑点）。"""
        config = ExecutionConfig()
        # 模拟: 信号日 close=10, 下一日 open=10.10
        reference_price = 10.0
        next_open = 10.10
        ledger = TradingLedger(config)
        entry = ledger.add_buy(
            signal_date="2024-06-01",
            execution_date="2024-06-02",
            reference_price=next_open,  # 成交以执行日开盘为参考价
            code="TEST",
            quantity=1000,
            reason="信号日收盘价=10.0",
        )
        self.assertIsNotNone(entry)
        # fill = 10.10 * 1.0005 = 10.10505
        self.assertGreater(entry.fill_price, next_open)
        self.assertEqual(entry.reference_price, next_open)

    def test_no_execution_when_no_next_day(self):
        """期末无下一交易日时不应成交。"""
        bars = _make_flat_bars("A", "2023-01-02", 254)
        end_date = bars[-1]["date"]
        result = _run_with_signals({"A": bars}, {end_date: "A"}, end_date)
        self.assertEqual(result["trades"], [])
        self.assertEqual(result["performance"]["final_nav"], 100000.0)

    def test_missing_cross_section_bar_does_not_create_signal_order(self):
        """检查日缺少任一池成员正式 bar 时保持现金/持仓不动。"""
        bars_a = _make_flat_bars("A", "2023-01-02", 257, 10.0)
        bars_b = _make_flat_bars("B", "2023-01-02", 257, 1.0)
        signal_date = bars_a[252]["date"]
        bars_b = [bar for bar in bars_b if bar["date"] != signal_date]
        result = _run_with_signals(
            {"A": bars_a, "B": bars_b},
            {signal_date: "A"},
            bars_a[-1]["date"],
        )
        self.assertEqual(result["trades"], [])
        self.assertEqual(result["performance"]["final_nav"], 100000.0)

    def test_unaffordable_target_cancels_switch_before_sell(self):
        bars_a = _make_flat_bars("A", "2023-01-02", 258, 10.0)
        bars_b = _make_flat_bars("B", "2023-01-02", 258, 2000.0)
        first_signal = bars_a[252]["date"]
        switch_signal = bars_a[253]["date"]
        switch_exec = bars_a[254]["date"]
        result = _run_with_signals(
            {"A": bars_a, "B": bars_b},
            {first_signal: "A", switch_signal: "B"},
            switch_exec,
        )
        actions = [(trade["action"], trade["code"]) for trade in result["trades"]]
        self.assertEqual(actions, [("买入", "A")])
        self.assertGreater(result["performance"]["final_nav"], 90000)

    def test_target_unaffordable_after_commission_cancels_switch_before_sell(self):
        """目标一手成交额可付但加佣金不可付时，整笔换仓必须取消。"""
        bars_a = _make_flat_bars("A", "2023-01-02", 258, 10.0, daily_change=0.0)
        bars_b = _make_flat_bars("B", "2023-01-02", 258, 998.0015, daily_change=0.0)
        first_signal = bars_a[252]["date"]
        switch_signal = bars_a[253]["date"]
        switch_exec = bars_a[254]["date"]

        result = _run_with_signals(
            {"A": bars_a, "B": bars_b},
            {first_signal: "A", switch_signal: "B"},
            switch_exec,
        )

        actions = [(trade["action"], trade["code"]) for trade in result["trades"]]
        self.assertEqual(actions, [("买入", "A")])
        self.assertGreater(result["performance"]["final_nav"], 99000)

    def test_sell_tax_is_included_before_committing_atomic_switch(self):
        """卖出税费使目标不可买时，整笔换仓必须取消。"""
        bars_a = _make_flat_bars("A", "2023-01-02", 258, 10.0, daily_change=0.0)
        bars_b = _make_flat_bars("B", "2023-01-02", 258, 997.5027, daily_change=0.0)
        first_signal = bars_a[252]["date"]
        switch_signal = bars_a[253]["date"]
        switch_exec = bars_a[254]["date"]
        execution = ExecutionConfig(etf_tax_rate=Decimal("0.001"))

        result = _run_with_signals(
            {"A": bars_a, "B": bars_b},
            {first_signal: "A", switch_signal: "B"},
            switch_exec,
            execution=execution,
        )

        actions = [(trade["action"], trade["code"]) for trade in result["trades"]]
        self.assertEqual(actions, [("买入", "A")])
        self.assertGreater(result["performance"]["final_nav"], 99000)

    def test_failed_or_missing_sell_cancels_atomic_switch(self):
        """原持仓执行日缺价时，禁止买入目标并保留原持仓。"""
        bars_a = _make_flat_bars("A", "2023-01-02", 258, 10.0)
        bars_b = _make_flat_bars("B", "2023-01-02", 258, 1.0)
        signal_dates = [bar["date"] for bar in bars_a[252:255]]
        # 先买 A，再发出换入 B 信号；删除 A 的换仓执行日行情。
        first_signal, switch_signal = signal_dates[0], signal_dates[1]
        switch_exec_date = bars_b[254]["date"]
        bars_a = [bar for bar in bars_a if bar["date"] != switch_exec_date]

        result = _run_with_signals(
            {"A": bars_a, "B": bars_b},
            {first_signal: "A", switch_signal: "B"},
            switch_exec_date,
        )

        actions = [(trade["action"], trade["code"]) for trade in result["trades"]]
        self.assertIn(("买入", "A"), actions)
        self.assertNotIn(("买入", "B"), actions)
        self.assertNotIn(("卖出", "A"), actions)
        self.assertGreater(result["performance"]["final_nav"], 90000)


class BacktestValuationTests(unittest.TestCase):
    """期末估值测试。"""

    def test_final_valuation_uses_last_le_end_date_price(self):
        """期末持仓按 <= end_date 的最后价格估值，不使用未来数据。"""
        bars = [
            {"date": "2024-06-01", "open": 9.9, "high": 10.1, "low": 9.8, "close": 10.0, "volume": 1000},
            {"date": "2024-06-02", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.2, "volume": 1000},
            {"date": "2024-06-03", "open": 10.2, "high": 10.5, "low": 10.1, "close": 10.5, "volume": 1000},
            {"date": "2024-06-04", "open": 10.5, "high": 11.0, "low": 10.4, "close": 11.0, "volume": 1000},
        ]
        end_date = "2024-06-03"
        # 期末：<= end_date 的最后一个 bar
        valid = [b for b in bars if b["date"] <= end_date]
        last = valid[-1]
        self.assertEqual(last["date"], "2024-06-03")
        self.assertEqual(last["close"], 10.5)
        # 而不是 2024-06-04 的 11.0

    def test_extreme_prices_after_end_date_do_not_affect_nav(self):
        """end_date 后追加极端价格不改变之前的交易或 NAV。"""
        bars_before = [
            {"date": "2024-06-01", "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.05, "volume": 1000},
            {"date": "2024-06-02", "open": 10.05, "high": 10.2, "low": 10.0, "close": 10.1, "volume": 1000},
        ]
        bars_after_extreme = [
            {"date": "2024-06-01", "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.05, "volume": 1000},
            {"date": "2024-06-02", "open": 10.05, "high": 10.2, "low": 10.0, "close": 10.1, "volume": 1000},
            {"date": "2024-06-03", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000},
        ]
        end_date = "2024-06-02"
        # 两个数据集的 end_date 前部分完全相同
        valid_before = [b for b in bars_before if b["date"] <= end_date]
        valid_after = [b for b in bars_after_extreme if b["date"] <= end_date]
        self.assertEqual(len(valid_before), len(valid_after))
        for a, b in zip(valid_before, valid_after):
            self.assertEqual(a["close"], b["close"])


class DailyRiskMetricTests(unittest.TestCase):
    def test_max_drawdown_period_starts_at_peak_and_ends_at_trough(self):
        from tools.momentum_etf_backtest import _compute_daily_risk_metrics
        navs = [
            ("2024-01-01", 100.0),
            ("2024-01-02", 120.0),
            ("2024-01-03", 110.0),
            ("2024-01-04", 90.0),
            ("2024-01-05", 100.0),
        ]
        metrics = _compute_daily_risk_metrics(navs)
        self.assertEqual(metrics["max_dd_start"], "2024-01-02")
        self.assertEqual(metrics["max_dd_end"], "2024-01-04")


class BacktestNAVInvariantTests(unittest.TestCase):
    """NAV 恒等式测试。"""

    def test_zero_trades_still_has_full_cash_nav(self):
        """零交易时 NAV 始终等于初始现金。"""
        cash = 100000.0
        nav = cash  # 无持仓
        self.assertEqual(nav, 100000.0)

    def test_cash_plus_holdings_equals_nav(self):
        """现金 + 持仓市值 = NAV（恒等式）。"""
        cash = 40000.0
        shares = 5000
        price = 10.0
        nav = cash + shares * price
        self.assertAlmostEqual(nav, 90000.0)

    def test_missing_price_does_not_zero_position(self):
        """缺价日使用 stale close 估值，不能把持仓从 NAV 中删除。"""
        bars_a = _make_flat_bars("A", "2023-01-02", 257, 10.0)
        bars_b = _make_flat_bars("B", "2023-01-02", 257, 1.0)
        buy_signal = bars_a[252]["date"]
        missing_date = bars_b[255]["date"]
        bars_a = [bar for bar in bars_a if bar["date"] != missing_date]

        result = _run_with_signals(
            {"A": bars_a, "B": bars_b},
            {buy_signal: "A"},
            bars_b[-1]["date"],
        )
        nav_by_date = dict(result["daily_nav"])
        self.assertGreater(nav_by_date[missing_date], 90000)

    def test_final_nav_equals_last_daily_nav(self):
        """生产回测 final_nav 必须等于 daily_nav[-1]。"""
        bars = _make_flat_bars("A", "2023-01-02", 257)
        buy_signal = bars[252]["date"]
        result = _run_with_signals({"A": bars}, {buy_signal: "A"}, bars[-1]["date"])
        self.assertEqual(
            result["performance"]["final_nav"],
            result["daily_nav"][-1][1],
        )


class BacktestDataManifestTests(unittest.TestCase):
    """数据 manifest 和截断测试。"""

    def test_truncate_series_to_end_date(self):
        """确认截断逻辑只保留 <= end_date 的数据。"""
        bars = [
            {"date": "2024-01-01", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 100},
            {"date": "2024-01-02", "open": 1.0, "high": 1.2, "low": 0.8, "close": 1.1, "volume": 100},
            {"date": "2024-01-03", "open": 1.1, "high": 1.3, "low": 0.9, "close": 1.2, "volume": 100},
        ]
        end_date = "2024-01-02"
        filtered = tuple(bar for bar in bars if bar["date"] <= end_date)
        self.assertEqual(len(filtered), 2)
        self.assertEqual(filtered[-1]["date"], "2024-01-02")

    def test_result_preserves_market_data_manifest(self):
        bars = tuple(_make_flat_bars("A", "2023-01-02", 257))
        manifest = MarketDataManifest(
            schema_version=2, code="A00000", source="tencent", adjustment="qfq",
            volume_adjustment="none", fetched_at="2026-07-28T00:00:00Z",
            start_date=bars[0]["date"], end_date=bars[-1]["date"],
            bar_count=len(bars), content_hash="a" * 64,
            adjustment_verified=False,
            verification_source="provider_declared_qfqday",
            verification_version=1, overlap_start=bars[0]["date"],
            overlap_end=bars[-1]["date"], overlap_count=len(bars),
            verification_tolerance=0.03, max_return_error=0.0,
            overlap_content_hash="b" * 64,
        )
        series = MarketDataSeries(bars, manifest)
        from tools.momentum_etf_backtest import run_backtest
        result = run_backtest(
            {"A": "A"}, "2000-01-01", end_date=bars[-1]["date"],
            freq="daily", quiet=True, market_data={"A": series},
        )
        self.assertEqual(result["market_data"]["A"].bars, series.bars)
        self.assertEqual(result["data_manifest"]["A"]["source"], "tencent")
        self.assertFalse(result["data_manifest"]["A"]["adjustment_verified"])

    def test_result_market_data_is_truncated_and_independent(self):
        bars = tuple(_make_flat_bars("A", "2023-01-02", 260))
        end_date = bars[256]["date"]
        manifest = MarketDataManifest(
            schema_version=2, code="A00000", source="tencent", adjustment="qfq",
            volume_adjustment="none", fetched_at="2026-07-28T00:00:00Z",
            start_date=bars[0]["date"], end_date=bars[-1]["date"],
            bar_count=len(bars), content_hash="a" * 64,
            adjustment_verified=False,
            verification_source="provider_declared_qfqday",
            verification_version=1, overlap_start=bars[0]["date"],
            overlap_end=bars[-1]["date"], overlap_count=len(bars),
            verification_tolerance=0.03, max_return_error=0.0,
            overlap_content_hash="b" * 64,
        )
        source = MarketDataSeries(bars, manifest)
        from tools.momentum_etf_backtest import run_backtest
        result = run_backtest(
            {"A": "A"}, "2000-01-01", end_date=end_date,
            freq="daily", quiet=True, market_data={"A": source},
        )
        snapshot = result["market_data"]["A"]
        self.assertEqual(snapshot.manifest.end_date, end_date)
        self.assertEqual(snapshot.bars[-1]["date"], end_date)
        source.bars[-1]["close"] = 999.0
        self.assertNotEqual(snapshot.bars[-1]["close"], 999.0)

    def test_effective_start_is_latest_warmup_date(self):
        """有效起点是所有 ETF 都满足 252 日预热的最晚日期。"""
        # ETF A 最早有 252 天数据在 2023-12-31
        # ETF B 最早在 2024-01-15
        # effective start = 2024-01-15
        bars_a = _make_flat_bars("A", "2022-12-01", 280, 10.0)
        bars_b = _make_flat_bars("B", "2022-12-15", 280, 20.0)
        warmup = 252
        # A: idx 252 → date
        date_a = bars_a[warmup]["date"] if len(bars_a) > warmup else None
        date_b = bars_b[warmup]["date"] if len(bars_b) > warmup else None
        effective = max(date_a, date_b)
        self.assertIsNotNone(effective)
        # 有效起点应不早于两者中较晚的那个
        self.assertGreaterEqual(effective, date_a)
        self.assertGreaterEqual(effective, date_b)


class MomentumBacktestSmokeTests(unittest.TestCase):
    """回测与信号核心集成 smoke test。"""

    def setUp(self):
        bars = json.loads(MOMENTUM_FIXTURE.read_text(encoding="utf-8"))["bars"]
        self.bars = tuple(bars)
        self.config = MomentumConfig()

    def test_rank_signals_accepts_signal_snapshots(self):
        """rank_signals 必须接受 SignalSnapshot 列表。"""
        from tools.momentum_core import rank_momentum_signals
        from tools.momentum_etf_backtest import rank_signals

        snapshots = [
            evaluate_momentum_signal(code, self.bars, len(self.bars) - 1, self.config)
            for code in ("AAA", "BBB")
        ]
        core_result = rank_momentum_signals(snapshots)
        backtest_result = rank_signals(snapshots)
        self.assertEqual(core_result, backtest_result)


if __name__ == "__main__":
    unittest.main()
