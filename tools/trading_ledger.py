"""统一交易账本：ExecutionConfig、FIFO 持仓、逐笔交易记录。

所有动量回测和网格回测共用此模块，确保佣金、滑点、手数、FIFO 和 NAV 恒等式
口径一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class ExecutionConfig:
    """交易执行参数，全局唯一口径。"""
    commission_rate: Decimal = Decimal("0.00025")   # 单边佣金费率
    slippage_rate: Decimal = Decimal("0.0005")       # 单边滑点费率
    minimum_commission: Decimal = Decimal(0)        # 最低佣金（免五=0）
    board_lot: int = 100                              # 最小交易单位（股）
    etf_tax_rate: Decimal = Decimal(0)              # ETF 交易税费
    cash_return_rate: Decimal = Decimal(0)          # 现金年化收益


def _round_down_shares(value: float, lot: int) -> int:
    """向下取整到 board_lot 的整数倍（float 版本）。"""
    return (int(value) // lot) * lot


@dataclass
class LedgerEntry:
    """单笔交易记录。"""
    signal_date: str
    execution_date: str
    reference_price: float       # 信号日收盘价（参考价）
    fill_price: float            # 实际成交价（含滑点）
    action: str                  # "buy" | "sell"
    code: str
    quantity: int                # 成交股数（正数）
    commission: float            # 佣金
    slippage: float              # 滑点金额（成交价与参考价之差 × 股数）
    net_cash_flow: float         # 净现金流（卖出为正，买入为负，含费用）
    realized_pnl: float          # 已实现盈亏（仅卖出时有意义）
    cash_after: float            # 交易后现金
    shares_after: int            # 交易后持仓股数
    cost_basis_after: float      # 交易后持仓成本（FIFO 调整后）
    reason: str = ""             # 交易原因摘要


@dataclass
class Position:
    """当前持仓状态。"""
    code: str = ""
    shares: int = 0
    cost_basis: float = 0.0      # FIFO 持仓成本（不含未实现盈亏）
    # FIFO lot 列表: [(买入日期, 股数, 买入总成本)]
    lots: list[tuple[str, int, float]] = field(default_factory=list)

    @property
    def avg_cost(self) -> float:
        """每股平均成本。"""
        return self.cost_basis / self.shares if self.shares > 0 else 0.0


@dataclass
class TradingLedger:
    """交易账本：记录所有交易并维护 FIFO 持仓。"""
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    entries: list[LedgerEntry] = field(default_factory=list)
    cash: float = 100000.0
    positions: dict[str, Position] = field(default_factory=dict)
    total_cash_return: float = 0.0
    last_cash_accrual_date: str | None = None

    def accrue_cash_return(self, trading_date: str) -> float:
        """按 ISO 交易日计提现金收益；同日重复调用不重复计提。"""
        parsed_date = date.fromisoformat(trading_date)
        if parsed_date.isoformat() != trading_date:
            raise ValueError("现金收益计提日期必须使用 YYYY-MM-DD 格式")
        if self.last_cash_accrual_date == trading_date:
            return 0.0
        if (
            self.last_cash_accrual_date is not None
            and parsed_date < date.fromisoformat(self.last_cash_accrual_date)
        ):
            raise ValueError("现金收益计提日期不得倒退")

        rate = float(self.execution.cash_return_rate)
        amount = self.cash * rate / 252 if rate else 0.0
        self.cash += amount
        self.total_cash_return += amount
        self.last_cash_accrual_date = trading_date
        return amount

    def position(self, code: str) -> Position:
        """返回指定代码的账本持仓；不存在时返回并登记空持仓。"""
        if code not in self.positions:
            self.positions[code] = Position(code=code)
        return self.positions[code]

    def quote_buy(self, reference_price: float, quantity: int) -> tuple[float, float, float]:
        """返回买入成交价、佣金和总现金支出，不修改账本。"""
        fill_price = reference_price * (1 + float(self.execution.slippage_rate))
        gross = fill_price * quantity
        commission = max(
            float(self.execution.minimum_commission),
            gross * float(self.execution.commission_rate),
        )
        return fill_price, commission, gross + commission

    def quote_sell(self, reference_price: float, quantity: int) -> tuple[float, float, float]:
        """返回卖出成交价、佣金和净现金收入，不修改账本。"""
        fill_price = reference_price * (1 - float(self.execution.slippage_rate))
        gross = fill_price * quantity
        commission = max(
            float(self.execution.minimum_commission),
            gross * float(self.execution.commission_rate),
        )
        tax = gross * float(self.execution.etf_tax_rate)
        return fill_price, commission, gross - commission - tax

    def affordable_buy_quantity(self, cash: float, reference_price: float) -> int:
        """按完整买入费用计算 cash 可买的最大整手数量。"""
        fill_price = reference_price * (1 + float(self.execution.slippage_rate))
        shares = compute_buy_quantity(cash, fill_price, self.execution.board_lot)
        while shares >= self.execution.board_lot:
            _, _, cash_outflow = self.quote_buy(reference_price, shares)
            if cash_outflow <= cash:
                return shares
            shares -= self.execution.board_lot
        return 0

    def add_buy(self, signal_date: str, execution_date: str,
                reference_price: float, code: str, quantity: int,
                reason: str = "") -> LedgerEntry | None:
        """执行买入并记录。

        quantity 必须是 board_lot 的整数倍。
        返回 LedgerEntry，如果资金不足返回 None。
        """
        ex = self.execution
        if quantity <= 0 or quantity % ex.board_lot != 0:
            raise ValueError(f"买入股数必须是 {ex.board_lot} 的正整数倍，实际 {quantity}")

        fill_price, commission, cash_outflow = self.quote_buy(reference_price, quantity)
        gross = fill_price * quantity
        net_cash_flow = -cash_outflow

        if self.cash + net_cash_flow < 0:  # 资金不足
            return None

        self.cash += net_cash_flow
        position = self.position(code)
        lot_cost = gross + commission
        position.shares += quantity
        position.cost_basis += lot_cost
        position.lots.append((execution_date, quantity, lot_cost))
        entry = LedgerEntry(
            signal_date=signal_date,
            execution_date=execution_date,
            reference_price=reference_price,
            fill_price=fill_price,
            action="buy",
            code=code,
            quantity=quantity,
            commission=commission,
            slippage=(fill_price - reference_price) * quantity,
            net_cash_flow=net_cash_flow,
            realized_pnl=0.0,
            cash_after=self.cash,
            shares_after=position.shares,
            cost_basis_after=position.cost_basis,
            reason=reason,
        )
        self.entries.append(entry)
        return entry

    def add_sell(self, signal_date: str, execution_date: str,
                 reference_price: float, code: str, quantity: int,
                 lots: list[tuple[str, int, float]] | None = None,
                 reason: str = "") -> LedgerEntry | None:
        """FIFO 卖出并记录。

        从 lots 中按 FIFO 顺序扣除，计算已实现盈亏。
        返回 LedgerEntry，如果卖出失败返回 None。
        """
        ex = self.execution
        if quantity <= 0 or quantity % ex.board_lot != 0:
            raise ValueError(f"卖出股数必须是 {ex.board_lot} 的正整数倍，实际 {quantity}")

        position = self.position(code)
        # 兼容旧调用：若账本尚无该持仓，可由调用方提供的 lots 初始化一次。
        if not position.lots and lots:
            position.lots = [tuple(lot) for lot in lots]
            position.shares = sum(lot[1] for lot in position.lots)
            position.cost_basis = sum(lot[2] for lot in position.lots)
        active_lots = position.lots
        total_available = position.shares
        if quantity > total_available:
            return None  # 持仓不足，卖出失败

        fill_price, commission, net_cash_flow = self.quote_sell(reference_price, quantity)

        # FIFO: 从最早的 lot 开始扣
        remaining = quantity
        cost_of_sold = 0.0
        new_lots: list[tuple[str, int, float]] = []
        for lot_date, lot_shares, lot_cost in active_lots:
            if remaining <= 0:
                new_lots.append((lot_date, lot_shares, lot_cost))
                continue
            if lot_shares <= remaining:
                # 整笔卖出
                cost_of_sold += lot_cost
                remaining -= lot_shares
            else:
                # 部分卖出
                unit_cost = lot_cost / lot_shares
                cost_of_sold += unit_cost * remaining
                new_lots.append((lot_date, lot_shares - remaining,
                                 lot_cost - unit_cost * remaining))
                remaining = 0

        realized_pnl = net_cash_flow - cost_of_sold
        self.cash += net_cash_flow

        # 更新账本持仓，FIFO lot 是唯一事实源。
        total_after = total_available - quantity
        new_cost_basis = sum(lot[2] for lot in new_lots)
        position.lots = new_lots
        position.shares = total_after
        position.cost_basis = new_cost_basis

        entry = LedgerEntry(
            signal_date=signal_date,
            execution_date=execution_date,
            reference_price=reference_price,
            fill_price=fill_price,
            action="sell",
            code=code,
            quantity=quantity,
            commission=commission,
            slippage=(reference_price - fill_price) * quantity,
            net_cash_flow=net_cash_flow,
            realized_pnl=realized_pnl,
            cash_after=self.cash,
            shares_after=total_after,
            cost_basis_after=new_cost_basis,
            reason=reason,
        )
        self.entries.append(entry)
        return entry

    def total_commission(self) -> float:
        return sum(e.commission for e in self.entries)

    def total_slippage(self) -> float:
        return sum(e.slippage for e in self.entries)


def compute_buy_quantity(cash: float, price: float, board_lot: int = 100) -> int:
    """计算可买入股数（向下取整到 board_lot 倍数）。"""
    if price <= 0:
        return 0
    raw = int(cash / price)
    return _round_down_shares(raw, board_lot)
