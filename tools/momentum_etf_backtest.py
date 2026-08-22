#!/usr/bin/env python3
"""
ETF 动量轮动回测工具

策略规则（五条件同时满足才买入，v2.1 对齐实盘扫描器）:
  条件一: RSRS 动量 > 0（年化斜率 × R²，趋势强度 × 趋势质量）
  条件二: 收盘价 > 20日均线（确认多头趋势）
  条件三: 近20日年化波动率 ≤ 历史中位数 × 1.5（防过热追顶）
  条件四: 当日成交量 ≤ 5日均量 × 2.5（防放量异动/高位接盘）
  条件五: RSI(14) ≤ 80（防超买）

操作节奏: 每周末收盘后跑信号，触发则次周开盘换仓
持币规则: 无标的满足条件时清仓持币

数据源: 东方财富前复权日K主源，腾讯 qfqday 交叉验证

用法:
    python3 tools/momentum_etf_backtest.py                    # 默认ETF池，5年回测
    python3 tools/momentum_etf_backtest.py --pool 159915,510300,512880,513180
    python3 tools/momentum_etf_backtest.py --start 2021-01-01 --freq biweekly
    python3 tools/momentum_etf_backtest.py --pool 513180,159915,510300 --no-bench
"""

import argparse
import copy
import json
import math
import os
import sys
from datetime import datetime, timedelta

try:
    from tools.etf_market_data import MarketDataSeries, load_etf_series, truncate_series
    from tools.momentum_core import (
        MomentumConfig, STOP_LOSS_PCT, SignalSnapshot,
        evaluate_momentum_signal, rank_momentum_signals,
    )
    from tools.trading_ledger import ExecutionConfig, TradingLedger, compute_buy_quantity
except ModuleNotFoundError:  # 支持直接执行 tools/momentum_etf_backtest.py
    from etf_market_data import MarketDataSeries, load_etf_series, truncate_series
    from momentum_core import (
        MomentumConfig, STOP_LOSS_PCT, SignalSnapshot,
        evaluate_momentum_signal, rank_momentum_signals,
    )
    from trading_ledger import ExecutionConfig, TradingLedger, compute_buy_quantity

_TIMEOUT = 15
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "cache")

# ==============================================================================
# ETF 池 — 按类别分组
# ==============================================================================

ETF_POOL = {
    # 宽基成长
    "159915": "创业板ETF",
    "588000": "科创50ETF",
    # 宽基价值
    "510300": "沪深300ETF",
    "510050": "上证50ETF",
    "510500": "中证500ETF",
    # 港股
    "513180": "恒生科技ETF",
    "159920": "恒生ETF",
    # 行业
    "512880": "证券ETF",
    "512690": "酒ETF",
    "512010": "医药ETF",
    # 跨境 / 另类
    "513100": "纳指ETF",
    "518880": "黄金ETF",
}

# 用 data/etf_meta.json 的全市场名称补全 ETF_POOL，保证枚举/回测结果里的
# 组合标签显示标的名字而不是裸代码（新增/动态池标的无需改硬编码映射）。
try:
    _META_FILE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "etf_meta.json",
    )
    if os.path.exists(_META_FILE):
        with open(_META_FILE, encoding="utf-8") as _meta_f:
            _meta_data = json.load(_meta_f)
        for _meta_etf in _meta_data.get("etfs", []):
            _meta_code = str(_meta_etf.get("code") or "")
            if _meta_code:
                ETF_POOL.setdefault(_meta_code, str(_meta_etf.get("name") or _meta_code))
except Exception:
    pass  # 元信息缺失时回退到硬编码映射，不影响回测主流程

# 预设ETF池（一键回测）
PRESET_POOLS = {
    "default":   ("518880,513100,159915,159920", "推荐 4-ETF（黄金+纳指+创业板+恒生）"),
    "best3":     ("518880,513100,159915",       "黄金+纳指+创业板 (最优)"),
    "best4":     ("518880,513100,159915,510300", "黄金+纳指+创业板+沪深300 (备选)"),
    "aggressive":("518880,513100,159915,588000", "黄金+纳指+创业板+科创50 (激进)"),
    "full5":     ("518880,513100,159915,510300,159920", "5只全明星"),
    "ashare":    ("510300,159915,588000,510500", "A股纯宽基 (对比用)"),
    "original":  ("159915,510300,512880,513180,512690,512010,159920,588000", "原始8-ETF池"),
    "all":       ("518880,513100,159915,510300,588000,510050,512880,513180", "全品种大池"),
    "cifang":    ("161912,161128,513100,159834,161130,159806,161631,515580,159871,512550", "cifang"),
    "cifang2":   ("518880,162415,501225,159915,159985,161226,159941,513030,563230,161127,159852,512980,162719,513090,159582,162411,159995,160723,159667,512200", "次方2"),
}

# 货币ETF（持币用，但回测中直接算现金）
CASH_PROXY = "CASH"

class MarketBars(list):
    """兼容 list 调用方，同时保留行情 manifest。"""

    def __init__(self, bars, manifest):
        super().__init__(bars)
        self.manifest = manifest


def fetch_kline(
    code: str, count: int = 2000, adjust: str = "qfq", as_of: str | None = None
) -> list[dict]:
    """获取通过数据契约验证的 ETF 前复权日 K 线。"""
    series = load_etf_series(
        code,
        count=count,
        adjustment=adjust,
        cache_dir=_CACHE_DIR,
        as_of=as_of,
    )
    if as_of is not None and series.manifest.end_date > as_of:
        series = truncate_series(series, as_of)
    return MarketBars(series.bars, series.manifest)


# ==============================================================================
# RSRS 动量计算（v2.0 核心改进: 年化斜率 × R² 替代简单涨幅）
# ==============================================================================

def calc_rsrs(klines: list[dict], idx: int, period: int = 20) -> tuple[float, float, float]:
    """
    RSRS 动量: 年化斜率 × R²（拟合优度）

    对 klines[:idx+1] 近 period 日的 log(收盘价) 做加权线性回归:
      - 权重: 近期更高（1 → 2 线性递增）
      - 斜率: 年化处理 (×252)
      - R²: 衡量价格沿趋势线的稳定程度

    返回: (rsrs_score, slope_annual_pct, r_squared)
    """
    if idx < period: return (0, 0, 0)

    window = klines[idx - period:idx + 1]
    log_prices = [math.log(k["close"]) for k in window if k["close"] > 0]
    if len(log_prices) < period // 2: return (0, 0, 0)

    n = len(log_prices)
    x = list(range(n))
    # 线性递增权重: 1 → 2
    weights = [1 + i / (n - 1) for i in range(n)] if n > 1 else [1] * n

    wx = sum(w * xi for w, xi in zip(weights, x))
    wy = sum(w * yi for w, yi in zip(weights, log_prices))
    wxx = sum(w * xi * xi for w, xi in zip(weights, x))
    wxy = sum(w * xi * yi for w, xi, yi in zip(weights, x, log_prices))
    w_sum = sum(weights)
    wyy = sum(w * yi * yi for w, yi in zip(weights, log_prices))

    denominator = w_sum * wxx - wx * wx
    if abs(denominator) < 1e-10: return (0, 0, 0)

    slope = (w_sum * wxy - wx * wy) / denominator

    # R²
    y_mean = wy / w_sum
    ss_total = wyy - 2 * y_mean * wy + w_sum * y_mean * y_mean
    ss_error = 0
    intercept = (wy - slope * wx) / w_sum
    for xi, yi, wi in zip(x, log_prices, weights):
        pred = slope * xi + intercept
        ss_error += wi * (yi - pred) ** 2
    r_squared = max(0, 1 - ss_error / ss_total) if ss_total > 1e-10 else 0

    # 年化斜率（交易日 ×252）
    slope_annual = slope * 252

    # RSRS 得分 = 年化斜率 × R²（截断下限=0 防负分，上限 50 确保排名区分度）
    # 审查修正: 原 cap=5 导致 91% 买入信号得分并列 5.0，排名退化为字典序
    rsrs = max(0, min(50, slope_annual * r_squared * 100))

    return (round(rsrs, 2), round(slope_annual * 100, 2), round(r_squared, 4))


# ==============================================================================
# 信号引擎（v2.0: RSRS 动量排名）
# ==============================================================================

def check_signal(
    code: str, klines: list[dict], idx: int, *, config: MomentumConfig
) -> SignalSnapshot:
    """委托共享核心计算历史信号，供 parity 测试和兼容调用使用。"""
    bars_tuple = tuple(klines[:idx + 1])
    return evaluate_momentum_signal(code, bars_tuple, idx, config)


def rank_signals(signals: list) -> dict | None:
    """横向排名：取 RSRS 得分第一的标的。

    支持 SignalSnapshot（通过共享核心 rank_momentum_signals）和旧版 dict。
    """
    if not signals:
        return None
    if isinstance(signals[0], SignalSnapshot):
        return rank_momentum_signals(signals)
    # 旧版 dict 排名（向后兼容）
    signals.sort(key=lambda s: s.get("rsrs_score", s["pct_n_days"]), reverse=True)
    return signals[0]


# ==============================================================================
# 回测引擎
# ==============================================================================

def run_backtest(
    pool: dict[str, str],
    start_date: str,
    end_date: str = None,
    freq: str = "weekly",
    momentum_period: int = 25,
    include_bench: bool = True,
    quiet: bool = False,
    market_data=None,
    execution: ExecutionConfig = ExecutionConfig(),
    switch_buffer: float = 1.0,
) -> dict:
    """运行动量轮动回测。

    参数:
        pool: {code: name} ETF池
        start_date: 回测起始日期 "YYYY-MM-DD"
        end_date: 回测结束日期（默认今天）
        freq: 信号检查频率 "weekly"(周末) | "biweekly"(双周) | "monthly"(月末)
        include_bench: 是否包含等权买入持有基准
        quiet: 静默模式（滚动窗口时使用）

    返回:
        { 回测结果字典 }
    """
    if not quiet:
        print(f"\n{'='*60}")
        print(f"  ETF 动量轮动回测")
        print(f"{'='*60}")
        print(f"  池子: {len(pool)} 只  |  频率: {freq}  |  RSRS周期: {momentum_period}日  |  起始: {start_date}")
        print(f"  规则: RSRS(年化斜率×R²)排名第一 + 收盘>MA{momentum_period} + 波动率不过热 + 量价过滤(放量2.5x/RSI>80剔除)")

    # ── 1. 获取所有ETF的K线数据 ──
    if not quiet:
        print(f"\n  [1/4] 获取K线数据...")
    all_klines: dict[str, list[dict]] = {}
    manifests = {}
    frozen_market_data = {}
    for code, name in pool.items():
        if market_data is not None:
            source = market_data[code]
            raw = list(source.bars) if hasattr(source, "bars") else list(source)
            if hasattr(source, "manifest"):
                manifests[code] = source.manifest
        else:
            raw = fetch_kline(code, count=2000, as_of=end_date)
            if hasattr(raw, "manifest"):
                manifests[code] = raw.manifest
        if end_date:
            raw = [bar for bar in raw if bar["date"] <= end_date]
        raw = copy.deepcopy(raw)
        all_klines[code] = raw
        if code in manifests:
            from dataclasses import replace
            manifest = replace(
                manifests[code],
                start_date=raw[0]["date"],
                end_date=raw[-1]["date"],
                bar_count=len(raw),
            )
            # truncate_series is the canonical hash rebuilder.
            provisional = MarketDataSeries(tuple(copy.deepcopy(raw)), manifest)
            frozen_market_data[code] = truncate_series(provisional, raw[-1]["date"])
            manifests[code] = frozen_market_data[code].manifest
        if not quiet:
            print(f"    {code} {name}: {len(raw)} 个交易日 ({raw[0]['date']} ~ {raw[-1]['date']})")
    if not all_klines:
        if not quiet:
            print("  ❌ 无可用数据")
        return {}

    # ── 2. 确定有效起点: 所有 ETF 都至少有 252 个严格历史交易日的最晚日期 ──
    warmup = MomentumConfig().warmup_days  # 252
    effective_starts: dict[str, str] = {}
    for code, klines in all_klines.items():
        if len(klines) > warmup:
            effective_starts[code] = klines[warmup]["date"]
        else:
            effective_starts[code] = klines[0]["date"]
    effective_start = max(start_date, max(effective_starts.values()))

    if not end_date:
        end_date = max(k[-1]["date"] for k in all_klines.values())

    # 窗口截断检测: 请求起点与实际起点差距超过90天时警告并标记
    window_truncated = False
    if start_date < effective_start:
        delta = (datetime.strptime(effective_start, "%Y-%m-%d") -
                 datetime.strptime(start_date, "%Y-%m-%d")).days
        window_truncated = delta > 90
        if window_truncated and not quiet:
            print(f"  ⚠️  数据窗口截断: 请求起点 {start_date}，实际起点 {effective_start}"
                  f"（延迟 {delta} 天），回测结果不代表完整请求期")

    if not quiet:
        print(f"\n  回测区间: {effective_start} ~ {end_date}")
        print(f"  （有效起点: 所有ETF均满足252日预热的最晚日期）")

    # ── 3. 构建日期索引和信号检查日期 ──
    if not quiet:
        print(f"\n  [2/4] 构建日期索引...")

    # 每个 ETF date→index 映射
    kline_index: dict[str, dict[str, int]] = {}
    for code, klines in all_klines.items():
        kline_index[code] = {k["date"]: i for i, k in enumerate(klines)}

    # 所有 ETF 共同交易日（并集）
    all_dates = sorted(set().union(*(set(kline_index[code].keys()) for code in all_klines)))
    dates_in_range = [d for d in all_dates if effective_start <= d <= end_date]

    # ── 信号检查日期（嵌套函数） ──
    def generate_check_dates(all_dates: list[str], actual_start: str, actual_end: str, freq: str) -> list[str]:
        """按指定频率生成信号检查日期。"""
        in_range = [d for d in all_dates if actual_start <= d <= actual_end]
        if freq == "weekly":
            week_last: dict[tuple, str] = {}
            for d in sorted(in_range):
                dt = datetime.strptime(d, "%Y-%m-%d")
                iso_week = dt.isocalendar()[:2]
                week_last[iso_week] = d
            return sorted(week_last.values())
        elif freq == "biweekly":
            weekly = generate_check_dates(all_dates, actual_start, actual_end, "weekly")
            return [weekly[i] for i in range(0, len(weekly), 2)]
        elif freq == "monthly":
            month_last: dict[str, str] = {}
            for d in sorted(in_range):
                month_last[d[:7]] = d
            return sorted(month_last.values())
        else:
            return sorted(in_range)

    # 用第一只ETF的日期列表作为参考生成信号检查日
    ref_klines = list(all_klines.values())[0]
    ref_dates = sorted(set(k["date"] for k in ref_klines))
    check_dates = generate_check_dates(ref_dates, effective_start, end_date, freq)
    check_dates_set = set(check_dates)

    if not quiet:
        print(f"\n  [3/4] 信号检查点: {len(check_dates)} 个 ({freq})")

    # ── 4. 初始化交易账本和持仓 ──
    if not quiet:
        print(f"\n  [4/4] 执行回测（逐日模拟，下一交易日开盘成交）...")

    ex_config = execution
    ledger = TradingLedger(ex_config)
    position = None  # {"code": str, "shares": int, "lots": [(date, shares, cost)]}
    trades: list[dict] = []
    daily_nav: list[tuple[str, float]] = []
    stop_loss_audits: list[dict] = []
    pending_order = None
    last_close: dict[str, float] = {}  # 缺价时沿用最近已知收盘价

    # 辅助: 查找订单所需标的均有正式开盘价的下一共同交易日。
    def _next_trading_day(signal_date: str, required_codes=()) -> str | None:
        for candidate in all_dates:
            if candidate <= signal_date or candidate > end_date:
                continue
            if all(candidate in kline_index.get(code, {}) for code in required_codes):
                return candidate
        return None

    # 辅助: 将 LedgerEntry 转为旧格式交易记录
    def _make_trade(entry, action_label: str) -> dict:
        t = {
            "action": action_label,
            "date": entry.execution_date,
            "code": entry.code,
            "name": pool.get(entry.code, entry.code),
            "price": round(entry.fill_price, 4),
            "shares": entry.quantity,
            "amount": round(abs(entry.net_cash_flow), 2),
            "reason": entry.reason,
        }
        if entry.action == "sell":
            cost_of_sold = entry.fill_price * entry.quantity - entry.commission - entry.realized_pnl
            t["pnl"] = round(entry.realized_pnl, 2)
            t["pnl_pct"] = round(entry.realized_pnl / cost_of_sold * 100, 2) if cost_of_sold > 0 else 0.0
        return t

    # ── 5. 逐日回测循环 ──
    for date in dates_in_range:
        stop_loss_triggered_today = False
        # 5a. 执行当天到期的 pending order（开盘价成交，含滑点）
        if pending_order and pending_order["exec_date"] == date:
            po = pending_order

            sell_required = bool(position and po.get("sell"))
            sell_succeeded = not sell_required
            # 原子换仓前先确认目标至少能买一手（按当前 NAV 预估卖出后现金）。
            if po.get("buy"):
                target_code = po["buy"]["code"]
                target_open = all_klines[target_code][kline_index[target_code][date]]["open"]
                available_cash = ledger.cash
                if sell_required:
                    pos_code = position["code"]
                    pos_open = all_klines[pos_code][kline_index[pos_code][date]]["open"]
                    _, _, sell_cash_flow = ledger.quote_sell(
                        pos_open, position["shares"]
                    )
                    available_cash += sell_cash_flow
                if ledger.affordable_buy_quantity(
                    available_cash, target_open
                ) < ex_config.board_lot:
                    pending_order = None
                    continue
            if sell_required:
                pos_code = position["code"]
                idx = kline_index[pos_code][date]
                open_price = all_klines[pos_code][idx]["open"]
                entry = ledger.add_sell(
                    signal_date=po["signal_date"],
                    execution_date=date,
                    reference_price=open_price,
                    code=pos_code,
                    quantity=position["shares"],
                    reason=po["sell"]["reason"],
                )
                if entry:
                    action_label = (
                        "止损卖出"
                        if po["sell"]["reason"].startswith("止损")
                        else "卖出(无信号)"
                        if po["sell"]["reason"].startswith("池内无标的")
                        else "卖出"
                    )
                    trades.append(_make_trade(entry, action_label))
                    position = None
                    sell_succeeded = True

            # 买入只能在无需卖出或卖出已经成功后执行。
            if po.get("buy") and sell_succeeded:
                buy_info = po["buy"]
                to_code = buy_info["code"]
                if date in kline_index.get(to_code, {}):
                    idx = kline_index[to_code][date]
                    open_price = all_klines[to_code][idx]["open"]
                    shares = ledger.affordable_buy_quantity(ledger.cash, open_price)
                    if shares >= ex_config.board_lot:
                        entry = ledger.add_buy(
                            signal_date=po["signal_date"],
                            execution_date=date,
                            reference_price=open_price,
                            code=to_code,
                            quantity=shares,
                            reason=buy_info["reason"],
                        )
                        if entry:
                            ledger_position = ledger.position(to_code)
                            position = {
                                "code": to_code,
                                "shares": ledger_position.shares,
                                "lots": ledger_position.lots,
                                "entry_fill": entry.fill_price,
                            }
                            trades.append(_make_trade(entry, "买入"))
                # 缺价: 保持持仓不动（若刚已卖出则持币）

            pending_order = None

        # 5b. 计算当日收盘 NAV
        nav = ledger.cash
        if position:
            pos_code = position["code"]
            if date in kline_index.get(pos_code, {}):
                idx = kline_index[pos_code][date]
                close_price = all_klines[pos_code][idx]["close"]
                nav += position["shares"] * close_price
                last_close[pos_code] = close_price
            elif pos_code in last_close:
                # 缺价: 沿用最近已知收盘价估值（不将持仓归零）
                nav += position["shares"] * last_close[pos_code]
            # 止损审计: 检查持仓收盘价是否触发 8% 止损线
            if position and "entry_fill" in position:
                if date in kline_index.get(position["code"], {}):
                    idx = kline_index[position["code"]][date]
                    c = all_klines[position["code"]][idx]["close"]
                    stop_line = position["entry_fill"] * (1 - STOP_LOSS_PCT)
                    if c <= stop_line:
                        stop_loss_audits.append({
                            "date": date,
                            "code": position["code"],
                            "entry_fill": position["entry_fill"],
                            "close": c,
                            "stop_line": round(stop_line, 4),
                            "loss_pct": round((c / position["entry_fill"] - 1) * 100, 2),
                        })
                        # 真正执行止损：次日开盘清仓（与实盘 check_stop_loss 口径一致）。
                        if pending_order is None:
                            exec_date = _next_trading_day(date, [position["code"]])
                            if exec_date is not None:
                                pending_order = {
                                    "signal_date": date,
                                    "exec_date": exec_date,
                                    "sell": {
                                        "reason": f"止损（-{STOP_LOSS_PCT * 100:.0f}%）",
                                    },
                                }
                                stop_loss_triggered_today = True
        daily_nav.append((date, round(nav, 2)))

        # 5c. 信号检查日: 评估全量信号，按迟滞规则决策
        if date in check_dates_set and not stop_loss_triggered_today:
            signal_config = MomentumConfig(
                rsrs_period=momentum_period,
                # 策略规范: 收盘 > MA20（与信号扫描器 momentum_signal 保持一致）
                ma_period=MomentumConfig().ma_period,
            )
            all_snapshots = []
            for code in pool:
                if code not in kline_index or date not in kline_index[code]:
                    continue
                idx = kline_index[code][date]
                klines = all_klines[code]
                all_snapshots.append(
                    evaluate_momentum_signal(code, tuple(klines), idx, signal_config)
                )

            holding_code = position["code"] if position else None
            try:
                from tools.momentum_core import select_rotation_target
            except ModuleNotFoundError:
                from momentum_core import select_rotation_target
            rotation = select_rotation_target(
                holding_code, all_snapshots, switch_buffer
            )

            if rotation["action"] in ("buy", "switch") and rotation["target"]:
                target_code = rotation["target"].code
                if (position and position["code"] == target_code
                        and rotation["action"] == "hold"):
                    continue
                required_codes = [target_code]
                if position and position["code"] != target_code:
                    required_codes.append(position["code"])
                exec_date = _next_trading_day(date, required_codes)
                if exec_date is None:
                    continue
                pending_order = {
                    "signal_date": date,
                    "exec_date": exec_date,
                }
                if position and position["code"] != target_code:
                    pending_order["sell"] = {
                        "reason": f"换入 {pool[target_code]}（{rotation['reason']}）",
                    }
                pending_order["buy"] = {
                    "code": target_code,
                    "reason": rotation["reason"],
                }
            elif rotation["action"] == "liquidate":
                if position:
                    exec_date = _next_trading_day(date, [position["code"]])
                    if exec_date is None:
                        continue
                    pending_order = {
                        "signal_date": date,
                        "exec_date": exec_date,
                        "sell": {
                            "reason": rotation["reason"],
                        },
                    }
            # hold / none: 不动

    # ── 6. 期末估值: final_nav = daily_nav[-1]（NAV 恒等式） ──
    if daily_nav:
        final_nav = daily_nav[-1][1]
    else:
        final_nav = ledger.cash

    # ── 7. 风险指标（从逐日 NAV 计算） ──
    daily_metrics = _compute_daily_risk_metrics(daily_nav) if len(daily_nav) >= 5 else {}

    annual_return = daily_metrics.get("annual_return_pct", 0.0)
    annual_vol = daily_metrics.get("annual_vol_pct", 0.0)
    sharpe = daily_metrics.get("sharpe", 0.0)
    sortino = daily_metrics.get("sortino", 0.0)
    calmar = daily_metrics.get("calmar", 0.0)
    max_dd = daily_metrics.get("max_dd_pct", 0.0)

    # nav_history: 从 daily_nav 降采样到检查日（供绘图用）
    nav_history = [(d, n) for d, n in daily_nav if d in check_dates_set]
    if not nav_history and daily_nav:
        nav_history = [daily_nav[0], daily_nav[-1]]

    # 回撤持续天数
    max_dd_days = 0
    if daily_metrics.get("max_dd_start") and daily_metrics.get("max_dd_end"):
        try:
            dd_start = datetime.strptime(daily_metrics["max_dd_start"], "%Y-%m-%d")
            dd_end = datetime.strptime(daily_metrics["max_dd_end"], "%Y-%m-%d")
            max_dd_days = (dd_end - dd_start).days
        except (ValueError, KeyError):
            pass

    # 回测年限
    start_dt = datetime.strptime(effective_start, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    years = (end_dt - start_dt).days / 365.25

    # 总收益率
    total_return = (final_nav - 100000) / 100000 * 100

    # ── 8. 等权 B&H 基准（含费用、滑点、手数、相同估值日期） ──
    bench_returns: dict[str, float] = {}
    if include_bench:
        n_etfs = len(pool)
        capital_per_etf = 100000.0 / n_etfs
        for code in pool:
            klines = all_klines[code]
            # 找第一个 >= effective_start 的交易日
            first_idx = None
            for i, k in enumerate(klines):
                if k["date"] >= effective_start:
                    first_idx = i
                    break
            # 找最后一个 <= end_date 的交易日
            last_idx = None
            for i, k in enumerate(klines):
                if k["date"] <= end_date:
                    last_idx = i
            if first_idx is not None and last_idx is not None and first_idx < last_idx:
                buy_open = klines[first_idx]["open"]
                sell_close = klines[last_idx]["close"]
                # 买入: 滑点 + 佣金 + 手数
                fill_price = buy_open * (1 + float(ex_config.slippage_rate))
                shares = compute_buy_quantity(capital_per_etf, fill_price, ex_config.board_lot)
                if shares >= ex_config.board_lot:
                    gross = fill_price * shares
                    commission = max(float(ex_config.minimum_commission), gross * float(ex_config.commission_rate))
                    actual_cost = gross + commission
                    end_value = shares * sell_close
                    leftover = capital_per_etf - actual_cost
                    total_end = end_value + leftover
                    ret = (total_end - capital_per_etf) / capital_per_etf * 100
                else:
                    ret = 0.0
                bench_returns[code] = round(ret, 2)

    if bench_returns:
        avg_bench = sum(bench_returns.values()) / len(bench_returns)
        etf_bench_returns = {}
        for code, ret in bench_returns.items():
            etf_bench_returns[code] = {
                "name": pool.get(code, code),
                "return_pct": ret,
            }
        bench_annual = ((1 + avg_bench / 100) ** (1 / years) - 1) * 100 if years > 0 else 0
    else:
        avg_bench = 0.0
        etf_bench_returns = {}
        bench_annual = 0.0

    # ── 9. 汇总结果 ──
    result = {
        "strategy": {
            "name": "ETF动量轮动（RSRS v3.0）",
            "rules": [
                f"RSRS 动量（年化斜率×R²）排名第一",
                f"收盘价 > MA{momentum_period}",
                "近20日年化波动率 ≤ 历史中位数×1.5",
            ],
            "frequency": freq,
            "pool": pool,
        },
        "period": {
            "requested_start": start_date,
            "start": effective_start,
            "end": end_date,
            "years": round(years, 2),
            "window_truncated": window_truncated,
        },
        "performance": {
            "initial_capital": 100000,
            "final_nav": round(final_nav, 2),
            "total_return_pct": round(total_return, 2),
            "annual_return_pct": round(annual_return, 2),
            "num_trades": len(trades),
            "benchmark_equal_weight_pct": avg_bench,
            "benchmark_annual_pct": round(bench_annual, 2),
            "excess_return_pct": round(total_return - avg_bench, 2),
            "etf_benchmarks": etf_bench_returns,
            # 日频风险指标
            "annual_vol_pct": round(annual_vol, 2),
            "sharpe": round(sharpe, 2),
            "sortino": round(sortino, 2),
            "calmar": round(calmar, 2),
            "max_dd_pct": round(max_dd, 2),
            "max_dd_days": max_dd_days,
            "downside_dev_pct": 0,  # 日频指标中由 daily.sortino 覆盖
            "daily": daily_metrics,
        },
        "trades": trades,
        "nav_history": nav_history[::max(1, len(nav_history) // 50)],  # 抽样50个点用于绘图
        "daily_nav": daily_nav,
        "market_data": frozen_market_data if frozen_market_data else copy.deepcopy(all_klines),
        "data_manifest": {
            code: manifest.__dict__ for code, manifest in manifests.items()
        },
        "stop_loss_audit": stop_loss_audits,
        "execution_config": {
            "commission_rate": str(ex_config.commission_rate),
            "slippage_rate": str(ex_config.slippage_rate),
            "minimum_commission": str(ex_config.minimum_commission),
            "board_lot": ex_config.board_lot,
            "etf_tax_rate": str(ex_config.etf_tax_rate),
            "cash_return_rate": str(ex_config.cash_return_rate),
        },
    }

    return result


def _compute_daily_risk_metrics(daily_navs):
    """从日频 NAV 计算全部风险指标。"""
    if len(daily_navs) < 5:
        return {}

    values = [n[1] for n in daily_navs]
    returns = [(values[i] - values[i-1]) / values[i-1]
               for i in range(1, len(values)) if values[i-1] > 0]

    n = len(returns)
    if n < 3:
        return {}

    # 基础统计
    mean_r = sum(returns) / n
    var_r = sum((r - mean_r)**2 for r in returns) / (n - 1)
    std_r = var_r ** 0.5
    annual_vol = std_r * math.sqrt(252) * 100
    annual_ret = ((values[-1] / values[0]) ** (252 / n) - 1) * 100

    # 日频 Sharpe (无风险 2.5%)
    rf_daily = 0.025 / 252
    sharpe = (mean_r - rf_daily) / std_r * math.sqrt(252) if std_r > 0 else 0

    # 日频 MaxDD
    peak = values[0]
    peak_date = daily_navs[0][0]
    max_dd = 0
    max_dd_start = peak_date
    max_dd_end = peak_date
    for i, v in enumerate(values):
        if v > peak:
            peak = v
            peak_date = daily_navs[i][0]
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd
            max_dd_start = peak_date
            max_dd_end = daily_navs[i][0]

    # VaR / CVaR
    sorted_rets = sorted(returns)
    var_95_idx = int(n * 0.05)
    var_95 = sorted_rets[var_95_idx] * 100 if var_95_idx < n else 0
    cvar_95 = sum(sorted_rets[:var_95_idx]) / var_95_idx * 100 if var_95_idx > 0 else 0

    # Sortino (日频下行偏差)
    downside = [r for r in returns if r < 0]
    if len(downside) > 2:
        down_std = (sum((r - mean_r)**2 for r in downside) / (len(downside) - 1)) ** 0.5
        sortino = (annual_ret - 2.5) / (down_std * math.sqrt(252) * 100) if down_std > 0 else 0
    else:
        sortino = 0

    # Calmar
    calmar = annual_ret / max_dd if max_dd > 0 else 0

    # 偏度/峰度
    skew = (sum((r - mean_r)**3 for r in returns) / n) / (std_r ** 3) if std_r > 0 else 0
    kurt = (sum((r - mean_r)**4 for r in returns) / n) / (std_r ** 4) - 3 if std_r > 0 else 0

    # 日胜率 + 盈亏比
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    avg_win = sum(wins) / len(wins) * 100 if wins else 0
    avg_loss = sum(losses) / len(losses) * 100 if losses else 0
    win_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    win_rate = len(wins) / n * 100

    return {
        "count": n,
        "annual_return_pct": round(annual_ret, 2),
        "annual_vol_pct": round(annual_vol, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "calmar": round(calmar, 2),
        "max_dd_pct": round(max_dd, 2),
        "max_dd_start": max_dd_start,
        "max_dd_end": max_dd_end,
        "var_95_daily_pct": round(var_95, 2),
        "cvar_95_daily_pct": round(cvar_95, 2),
        "skewness": round(skew, 3),
        "kurtosis": round(kurt, 3),
        "win_rate_pct": round(win_rate, 1),
        "win_loss_ratio": round(win_loss_ratio, 2),
    }


# ==============================================================================
# 报告输出
# ==============================================================================

def print_report(result: dict):
    """打印回测报告。"""
    if not result:
        return

    perf = result["performance"]
    period = result["period"]
    strat = result["strategy"]

    print(f"\n{'='*70}")
    print(f"  📊 回测报告")
    print(f"{'='*70}")

    # 绩效摘要
    print(f"\n  ┌─────────────────────────────────────────────────────┐")
    print(f"  │  绩效摘要                                           │")
    print(f"  ├─────────────────────────────────────────────────────┤")
    return_str = f"+{perf['total_return_pct']:.1f}%" if perf["total_return_pct"] >= 0 else f"{perf['total_return_pct']:.1f}%"
    annual_str = f"+{perf['annual_return_pct']:.1f}%" if perf["annual_return_pct"] >= 0 else f"{perf['annual_return_pct']:.1f}%"
    excess_str = f"+{perf['excess_return_pct']:.1f}%" if perf['excess_return_pct'] >= 0 else f"{perf['excess_return_pct']:.1f}%"
    print(f"  │  初始资金:   ¥{perf['initial_capital']:,.0f}")
    print(f"  │  最终净值:   ¥{perf['final_nav']:,.2f}")
    print(f"  │  总收益率:   {return_str}")
    print(f"  │  年化收益:   {annual_str}")
    print(f"  │  回测年限:   {period['years']} 年")
    print(f"  │  交易次数:   {perf['num_trades']} 次")
    print(f"  │  等权基准:   {perf['benchmark_equal_weight_pct']:.1f}%")
    print(f"  │  超额收益:   {excess_str}")
    print(f"  └─────────────────────────────────────────────────────┘")

    # ETF 基准对比
    if perf.get("etf_benchmarks"):
        print(f"\n  ┌─────────────────────────────────────────────────────┐")
        print(f"  │  各ETF买入持有 vs 策略                               │")
        print(f"  ├─────────────────────────────────────────────────────┤")
        for code, b in perf["etf_benchmarks"].items():
            ret_str = f"{b['return_pct']:+.1f}%"
            print(f"  │  {code} {b['name']:<12s} B&H: {ret_str:>8s}          │")
        print(f"  │  {'─' * 49} │")
        ret_str = f"{perf['total_return_pct']:+.1f}%"
        print(f"  │  策略总收益: {ret_str:>8s}                            │")
        print(f"  └─────────────────────────────────────────────────────┘")

    # 日频 vs 双周风险指标对比
    daily = perf.get("daily", {})
    if daily:
        print(f"\n  ┌─────────────────────────────────────────────────────┐")
        print(f"  │  风险指标 — 双周采样 vs 日频（真实）                  │")
        print(f"  ├─────────────────────────────────────────────────────┤")
        print(f"  │  {'指标':<20s} {'双周(偏乐观)':>14s} {'日频(真实)':>14s} │")
        print(f"  │  {'─'*50} │")
        vol_bw = f"{perf['annual_vol_pct']:.1f}%"
        vol_daily = f"{daily['annual_vol_pct']:.1f}%"
        print(f"  │  {'年化波动率':<20s} {vol_bw:>14s} {vol_daily:>14s} │")
        sharpe_bw = f"{perf['sharpe']:.2f}"
        sharpe_daily = f"{daily['sharpe']:.2f}"
        print(f"  │  {'Sharpe Ratio':<20s} {sharpe_bw:>14s} {sharpe_daily:>14s} │")
        sortino_bw = f"{perf.get('sortino', 0):.2f}"
        sortino_daily = f"{daily['sortino']:.2f}"
        print(f"  │  {'Sortino Ratio':<20s} {sortino_bw:>14s} {sortino_daily:>14s} │")
        calmar_bw = f"{perf.get('calmar', 0):.2f}"
        calmar_daily = f"{daily['calmar']:.2f}"
        print(f"  │  {'Calmar Ratio':<20s} {calmar_bw:>14s} {calmar_daily:>14s} │")
        dd_bw = f"{perf['max_dd_pct']:.1f}%"
        dd_daily = f"{daily['max_dd_pct']:.1f}%"
        print(f"  │  {'最大回撤':<20s} {dd_bw:>14s} {dd_daily:>14s} │")
        print(f"  │   回撤区间: {daily.get('max_dd_start','?'):<10s} ~ {daily.get('max_dd_end','?'):<10s}         │")
        var_str = f"{daily['var_95_daily_pct']:.2f}%/日"
        cvar_str = f"{daily['cvar_95_daily_pct']:.2f}%/日"
        print(f"  │  {'VaR(95%)':<20s} {'—':>14s} {var_str:>14s} │")
        print(f"  │  {'CVaR(95%)':<20s} {'—':>14s} {cvar_str:>14s} │")
        skew_str = f"{daily['skewness']:.3f}"
        kurt_str = f"{daily['kurtosis']:.3f}"
        print(f"  │  {'偏度':<20s} {'—':>14s} {skew_str:>14s} │")
        print(f"  │  {'超额峰度':<20s} {'—':>14s} {kurt_str:>14s} │")
        print(f"  └─────────────────────────────────────────────────────┘")

    # 交易记录（最近20笔）
    trades = result["trades"]
    if trades:
        print(f"\n  ┌─────────────────────────────────────────────────────┐")
        print(f"  │  交易记录（最近20笔）                                │")
        print(f"  ├─────────────────────────────────────────────────────┤")
        for t in trades[-20:]:
            action = t["action"]
            date = t["date"]
            name = t.get("name", "")
            price = t.get("price", 0)
            pnl_str = f"盈亏{t['pnl']:+.0f}元({t['pnl_pct']:+.1f}%)" if "pnl" in t else ""
            shares = t.get("shares", "")
            info = f"{shares}股" if shares else ""
            reason = t.get("reason", "")
            print(f"  │  {date} {action:<12s} {name:<12s} @{price:<8.4f} {info:<10s} {pnl_str}")
            if reason:
                print(f"  │    → {reason}")
        print(f"  └─────────────────────────────────────────────────────┘")

    # 信号统计
    if trades:
        buys = [t for t in trades if "买入" in t["action"]]
        sells = [t for t in trades if "卖出" in t["action"]]
        win_trades = [t for t in sells if t.get("pnl", 0) > 0]
        loss_trades = [t for t in sells if t.get("pnl", 0) <= 0]

        print(f"\n  ┌─────────────────────────────────────────────────────┐")
        print(f"  │  交易统计                                           │")
        print(f"  ├─────────────────────────────────────────────────────┤")
        print(f"  │  买入次数:   {len(buys)}")
        print(f"  │  卖出次数:   {len(sells)}")
        if sells:
            win_rate = len(win_trades) / len(sells) * 100
            avg_win = sum(t["pnl"] for t in win_trades) / len(win_trades) if win_trades else 0
            avg_loss = sum(t["pnl"] for t in loss_trades) / len(loss_trades) if loss_trades else 0
            total_pnl = sum(t["pnl"] for t in sells)
            print(f"  │  胜率:       {win_rate:.0f}% ({len(win_trades)}/{len(sells)})")
            print(f"  │  平均盈利:   ¥{avg_win:,.0f}")
            print(f"  │  平均亏损:   ¥{avg_loss:,.0f}")
            print(f"  │  累计盈亏:   ¥{total_pnl:,.0f}")

            # 按标的统计
            by_etf = {}
            for t in sells:
                code = t["code"]
                if code not in by_etf:
                    by_etf[code] = {"count": 0, "wins": 0, "total_pnl": 0}
                by_etf[code]["count"] += 1
                by_etf[code]["total_pnl"] += t.get("pnl", 0)
                if t.get("pnl", 0) > 0:
                    by_etf[code]["wins"] += 1
            print(f"  │                                                    │")
            print(f"  │  按标的盈亏:                                        │")
            for code, stats in sorted(by_etf.items(), key=lambda x: x[1]["total_pnl"], reverse=True):
                wr = stats["wins"] / stats["count"] * 100 if stats["count"] > 0 else 0
                pnl_str = f"+¥{stats['total_pnl']:,.0f}" if stats["total_pnl"] >= 0 else f"-¥{abs(stats['total_pnl']):,.0f}"
                etf_name = result["strategy"]["pool"].get(code, code)
                print(f"  │    {code} {etf_name:<12s} {stats['count']:>2d}笔  胜率{wr:.0f}%  盈亏{pnl_str}")

        print(f"  └─────────────────────────────────────────────────────┘")

    # 持仓分布
    if trades:
        holdings = {}
        current_holding = None
        for t in trades:
            if "买入" in t["action"]:
                current_holding = t
            elif "卖出" in t["action"]:
                if current_holding:
                    code = current_holding["code"]
                    name = current_holding["name"]
                    if code not in holdings:
                        holdings[code] = {"name": name, "days": 0, "count": 0}
                    holdings[code]["count"] += 1
                current_holding = None

        if holdings:
            print(f"\n  ┌─────────────────────────────────────────────────────┐")
            print(f"  │  标的偏好（被选为持仓的次数）                        │")
            print(f"  ├─────────────────────────────────────────────────────┤")
            for code, h in sorted(holdings.items(), key=lambda x: x[1]["count"], reverse=True):
                bar = "█" * h["count"]
                print(f"  │  {code} {h['name']:<12s} {h['count']:>2d} 次 {bar}")
            print(f"  └─────────────────────────────────────────────────────┘")


def print_trades_csv(result: dict):
    """输出交易记录为 CSV（方便导入 Excel 分析）。"""
    trades = result.get("trades", [])
    if not trades:
        return
    print(f"\n  --- 交易记录 CSV ---")
    print("date,action,code,name,price,shares,amount,pnl,pnl_pct,reason")
    for t in trades:
        print(
            f"{t['date']},{t['action']},{t['code']},{t.get('name','')},"
            f"{t.get('price','')},{t.get('shares','')},{t.get('amount','')},"
            f"{t.get('pnl','')},{t.get('pnl_pct','')},{t.get('reason','')}"
        )


# ==============================================================================
# 滚动窗口验证
# ==============================================================================

def _add_months(dt: datetime, months: int) -> datetime:
    """日期加减月份。"""
    m = dt.month + months
    y = dt.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    d = min(dt.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return dt.replace(year=y, month=m, day=d)


def run_rolling_backtest(
    pool: dict[str, str],
    window_months: int = 12,
    step_months: int = 3,
    freq: str = "biweekly",
    momentum_period: int = 60,
    switch_buffer: float = 1.0,
) -> list[dict]:
    """滚动窗口回测。

    按 window_months 的窗口长度、step_months 的步长，在可用数据范围内滑动。
    每个窗口独立跑一次完整回测，返回所有窗口的结果列表。
    """
    # 先获取数据，确定可用日期范围
    all_klines: dict[str, list[dict]] = {}
    for code, name in pool.items():
        all_klines[code] = fetch_kline(code, count=2000)
    if not all_klines:
        return []

    common_start = max(k[0]["date"] for k in all_klines.values())
    common_end = min(k[-1]["date"] for k in all_klines.values())

    # 需要预热期（start前至少252个交易日≈1年）
    warmup_needed = timedelta(days=365)
    earliest_possible = datetime.strptime(common_start, "%Y-%m-%d") + warmup_needed
    latest_possible = datetime.strptime(common_end, "%Y-%m-%d")

    # 生成窗口
    windows = []
    win_start = earliest_possible
    while True:
        win_end = _add_months(win_start, window_months)
        if win_end > latest_possible:
            break
        ws = win_start.strftime("%Y-%m-%d")
        we = win_end.strftime("%Y-%m-%d")
        windows.append((ws, we))
        win_start = _add_months(win_start, step_months)

    if len(windows) < 2:
        print(f"  ❌ 数据不足以生成滚动窗口（需要至少能跑2个窗口）")
        print(f"     可用区间: {earliest_possible.strftime('%Y-%m-%d')} ~ {latest_possible.strftime('%Y-%m-%d')}")
        return []

    print(f"\n{'='*60}")
    print(f"  滚动窗口验证")
    print(f"{'='*60}")
    print(f"  窗口长度: {window_months} 月  |  步长: {step_months} 月  |  共 {len(windows)} 个窗口")
    print(f"  频率: {freq}  |  动量周期: {momentum_period}日")
    print(f"  数据范围: {common_start} ~ {common_end}")

    results = []
    for i, (ws, we) in enumerate(windows):
        print(f"\n  [{i+1}/{len(windows)}] 窗口 {ws} ~ {we} ...", end=" ", flush=True)
        r = run_backtest(
            pool=pool,
            start_date=ws,
            end_date=we,
            freq=freq,
            momentum_period=momentum_period,
            include_bench=True,
            quiet=True,
            market_data=all_klines,
            switch_buffer=switch_buffer,
        )
        if r:
            p = r["performance"]
            wins = len([t for t in r["trades"] if "卖出" in t["action"] and t.get("pnl", 0) > 0])
            total_sells = len([t for t in r["trades"] if "卖出" in t["action"]])
            wr = wins / total_sells * 100 if total_sells > 0 else 0
            print(f"策略{p['total_return_pct']:+.1f}% | 基准{p['benchmark_equal_weight_pct']:+.1f}% | 超额{p['excess_return_pct']:+.1f}% | {p['num_trades']}笔 | 胜率{wr:.0f}%")
            results.append({
                "window": f"{ws}~{we}",
                "return_pct": p["total_return_pct"],
                "annual_pct": p["annual_return_pct"],
                "benchmark_pct": p["benchmark_equal_weight_pct"],
                "excess_pct": p["excess_return_pct"],
                "num_trades": p["num_trades"],
                "win_rate": round(wr, 1),
                "nav": p["final_nav"],
            })
        else:
            print("❌ 数据不足，跳过")

    return results


def print_rolling_report(results: list[dict]):
    """打印滚动窗口汇总报告。"""
    if not results:
        return

    n = len(results)
    returns = [r["return_pct"] for r in results]
    excesses = [r["excess_pct"] for r in results]
    win_rates = [r["win_rate"] for r in results]
    trades = [r["num_trades"] for r in results]
    benchmarks = [r["benchmark_pct"] for r in results]

    pos_returns = sum(1 for r in returns if r > 0)
    pos_excess = sum(1 for e in excesses if e > 0)

    # ── 窗口明细表 ──
    print(f"\n{'='*90}")
    print(f"  📊 滚动窗口明细（共 {n} 个窗口）")
    print(f"{'='*90}")
    header = f"  {'窗口':<22s} {'策略收益':>8s} {'年化':>7s} {'基准收益':>8s} {'超额收益':>8s} {'交易':>5s} {'胜率':>6s}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in results:
        ret_s = f"{r['return_pct']:+.1f}%"
        ann_s = f"{r['annual_pct']:+.1f}%"
        bmk_s = f"{r['benchmark_pct']:+.1f}%"
        exc_s = f"{r['excess_pct']:+.1f}%"
        wr_s = f"{r['win_rate']:.0f}%"
        # 超额收益着色
        flag = "✅" if r["excess_pct"] > 0 else ("⚠️" if r["excess_pct"] > -5 else "🔴")
        print(f"  {r['window']:<22s} {ret_s:>8s} {ann_s:>7s} {bmk_s:>8s} {exc_s:>8s} {flag} {r['num_trades']:>3d}  {wr_s:>6s}")

    # ── 汇总统计 ──
    mean_ret = sum(returns) / n
    mean_excess = sum(excesses) / n
    std_ret = (sum((r - mean_ret) ** 2 for r in returns) / n) ** 0.5
    std_excess = (sum((e - mean_excess) ** 2 for e in excesses) / n) ** 0.5
    mean_wr = sum(win_rates) / n
    mean_trades = sum(trades) / n

    print(f"\n{'='*90}")
    print(f"  📈 稳定性统计")
    print(f"{'='*90}")
    print(f"\n  ┌─────────────────────────────────────────────────────┐")
    print(f"  │  策略收益                                           │")
    print(f"  │    均值: {mean_ret:+.1f}%  |  标准差: {std_ret:.1f}%  |  范围: [{min(returns):+.1f}%, {max(returns):+.1f}%]")
    print(f"  │    正收益窗口: {pos_returns}/{n} ({pos_returns/n*100:.0f}%)")
    print(f"  ├─────────────────────────────────────────────────────┤")
    print(f"  │  超额收益（相对等权基准）                            │")
    print(f"  │    均值: {mean_excess:+.1f}%  |  标准差: {std_excess:.1f}%  |  范围: [{min(excesses):+.1f}%, {max(excesses):+.1f}%]")
    print(f"  │    正超额窗口: {pos_excess}/{n} ({pos_excess/n*100:.0f}%)")
    print(f"  ├─────────────────────────────────────────────────────┤")
    print(f"  │  交易行为                                           │")
    print(f"  │    平均交易次数: {mean_trades:.0f} 次/窗口")
    print(f"  │    平均胜率: {mean_wr:.0f}%")
    print(f"  │    基准均值: {sum(benchmarks)/n:+.1f}%")
    print(f"  └─────────────────────────────────────────────────────┘")

    # ── 参数稳定性判定 ──
    print(f"\n  ┌─────────────────────────────────────────────────────┐")
    print(f"  │  参数稳定性判定                                     │")
    print(f"  ├─────────────────────────────────────────────────────┤")

    # 超额收益稳定性
    if pos_excess == n:
        print(f"  │  🟢 超额收益: 所有窗口均跑赢基准                    │")
    elif pos_excess >= n * 0.75:
        print(f"  │  🟡 超额收益: {pos_excess}/{n} 窗口跑赢基准，存在不稳定窗口     │")
    else:
        print(f"  │  🔴 超额收益: 仅 {pos_excess}/{n} 窗口跑赢基准，策略不稳健       │")

    # 收益波动
    if std_ret < 15:
        print(f"  │  🟢 收益波动: 标准差 {std_ret:.1f}%，策略收益分布稳定          │")
    elif std_ret < 30:
        print(f"  │  🟡 收益波动: 标准差 {std_ret:.1f}%，窗口间差异较大            │")
    else:
        print(f"  │  🔴 收益波动: 标准差 {std_ret:.1f}%，极端不稳定               │")

    # Sharpe 近似（假设无风险利率=2%，窗口长度从第一个窗口估计）
    sharpe = (mean_ret - 2) / std_ret if std_ret > 0 else 0
    if sharpe > 1.0:
        print(f"  │  🟢 近似Sharpe: {sharpe:.2f}（风险调整后表现优秀）            │")
    elif sharpe > 0.5:
        print(f"  │  🟡 近似Sharpe: {sharpe:.2f}（风险调整后可接受）            │")
    else:
        print(f"  │  🔴 近似Sharpe: {sharpe:.2f}（风险调整后不足）              │")

    print(f"  └─────────────────────────────────────────────────────┘")


def run_batch(presets, momentums, freq, start_date, end_date, include_bench,
              switch_buffer: float = 1.0):
    """批量对比: 多个ETF池 × 多个动量周期 → 汇总对比表。"""
    print(f"\n{'='*90}")
    print(f"  批量对比 — {len(presets)}个池 × {len(momentums)}个动量周期")
    print(f"{'='*90}")

    all_results = []
    for preset_name in presets:
        if preset_name not in PRESET_POOLS:
            continue
        codes_str, desc = PRESET_POOLS[preset_name]
        codes = [c.strip() for c in codes_str.split(",")]
        pool = {c: ETF_POOL.get(c, c) for c in codes}

        for mp in momentums:
            print(f"  {preset_name}({len(codes)}只) × {mp}日 ...", end=" ", flush=True)
            try:
                r = run_backtest(
                    pool=pool, start_date=start_date, end_date=end_date,
                    freq=freq, momentum_period=mp,
                    include_bench=include_bench, quiet=True,
                    switch_buffer=switch_buffer,
                )
                if r:
                    p = r["performance"]
                    sells = [t for t in r["trades"] if "卖出" in t["action"]]
                    wins = len([t for t in sells if t.get("pnl", 0) > 0])
                    total_sells = len(sells)
                    wr = wins / total_sells * 100 if total_sells > 0 else 0
                    print(f"策略{p['total_return_pct']:+.1f}% 年化{p['annual_return_pct']:+.1f}% 超额{p['excess_return_pct']:+.1f}% 胜率{wr:.0f}%")
                    all_results.append({
                        "preset": preset_name, "desc": desc, "etf_count": len(codes),
                        "momentum": mp, "freq": freq,
                        "return_pct": p["total_return_pct"],
                        "annual_pct": p["annual_return_pct"],
                        "excess_pct": p["excess_return_pct"],
                        "benchmark_pct": p["benchmark_equal_weight_pct"],
                        "num_trades": p["num_trades"],
                        "win_rate": round(wr, 1),
                        "period_years": r["period"]["years"],
                        "window_start": r["period"]["start"],
                        "window_end": r["period"]["end"],
                        "window_truncated": r["period"].get("window_truncated", False),
                    })
                else:
                    print("❌ 数据不足")
            except Exception as e:
                print(f"❌ {e}")

    if not all_results:
        print("\n  无有效结果")
        return

    # ── 汇总表 ──
    print(f"\n{'='*90}")
    print(f"  对比汇总（按年化收益排序）")
    print(f"{'='*90}")

    # 检测窗口不一致
    windows = {(r["window_start"], r["window_end"]) for r in all_results}
    if len(windows) > 1:
        print(f"  ⚠️ 各组合窗口不一致，年化不可直接比较")

    all_results.sort(key=lambda r: r["annual_pct"], reverse=True)

    header = f"  {'池':<12s} {'ETF':>3s} {'周期':>5s} {'年化':>8s} {'总收益':>8s} {'超额':>8s} {'基准':>8s} {'胜率':>6s} {'交易':>5s} {'窗口':>12s}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in all_results:
        ret = f"{r['return_pct']:+.1f}%"
        ann = f"{r['annual_pct']:+.1f}%"
        exc = f"{r['excess_pct']:+.1f}%"
        bmk = f"{r['benchmark_pct']:+.1f}%"
        wr = f"{r['win_rate']:.0f}%"
        win = r['window_start'][:7] if r['window_start'] else '?'
        flag = "✅" if r["excess_pct"] > 0 else "🔴"
        trunc = " ⚠️" if r.get("window_truncated") else ""
        print(f"  {r['preset']:<12s} {r['etf_count']:>3d} {r['momentum']:>4d}日 {ann:>8s} {ret:>8s} {exc:>8s} {bmk:>8s} {wr:>6s} {r['num_trades']:>4d} {win:>12s}{trunc}")

    # ── 最佳组合 ──
    best = all_results[0]
    print(f"\n  🏆 最佳: {best['preset']} × {best['momentum']}日动量 — 年化{best['annual_pct']:+.1f}%, 超额{best['excess_pct']:+.1f}%, 胜率{best['win_rate']:.0f}%")
    print(f"     池子: {best['desc']}")
    print()


# ==============================================================================
# CLI
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="ETF 动量轮动回测 — 三条件策略验证\n"
                    "用法: python3 tools/momentum_etf_backtest.py [--preset best4] [--momentum 20] [--rolling]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--pool",
        default=None,
        help="ETF池，逗号分隔（如 518880,513100,588000,510300）。不指定时用 --preset 或默认 best4",
    )
    parser.add_argument(
        "--preset",
        default="default",
        choices=list(PRESET_POOLS.keys()),
        help="预设ETF池（默认 default: 黄金+纳指+创业板）",
    )
    parser.add_argument("--list-presets", action="store_true", help="列出所有预设池")
    parser.add_argument("--start", default="2013-01-01", help="回测起始日期（默认 2013-01-01）")
    parser.add_argument("--end", default=None, help="回测结束日期（默认今天）")
    parser.add_argument(
        "--freq", default="biweekly", choices=["weekly", "biweekly", "monthly"],
        help="信号检查频率（默认 biweekly）",
    )
    parser.add_argument(
        "--momentum", type=int, default=25,
        help="动量/均线周期（默认20日）",
    )
    parser.add_argument(
        "--commission-rate", type=float, default=None,
        help="单边佣金费率（小数，如 0.00025=万2.5；默认 0.00025）",
    )
    parser.add_argument(
        "--min-commission", type=float, default=None,
        help="单笔最低佣金（元，0=免5；默认 0=免5）",
    )
    parser.add_argument(
        "--slippage-rate", type=float, default=None,
        help="单边滑点费率（小数，默认 0.0005）",
    )
    parser.add_argument("--no-bench", action="store_true", help="不计算基准收益")
    parser.add_argument("--csv", action="store_true", help="输出交易记录CSV")
    parser.add_argument("--json", action="store_true", help="JSON格式输出完整结果")
    parser.add_argument(
        "--rolling", action="store_true",
        help="启用滚动窗口验证模式",
    )
    parser.add_argument(
        "--window", type=int, default=12,
        help="滚动窗口长度（月，默认12）",
    )
    parser.add_argument(
        "--step", type=int, default=3,
        help="滚动窗口步长（月，默认3）",
    )
    parser.add_argument(
        "--batch", action="store_true",
        help="批量对比: 对多个预设池+多动量周期同时回测，输出对比表",
    )
    parser.add_argument(
        "--batch-presets", default="best4,3etf,aggressive,ashare",
        help="批量对比的预设池列表（逗号分隔，默认 best4,3etf,aggressive,ashare）",
    )
    parser.add_argument(
        "--batch-momentums", default="20,40,60",
        help="批量对比的动量周期列表（逗号分隔，默认 20,40,60）",
    )
    parser.add_argument(
        "--switch-buffer", type=float, default=1.0,
        help="换仓迟滞系数（默认 1.0=无迟滞，≥1.0；如 1.25 表示挑战者RSRS需超持仓25%%才换仓）",
    )

    args = parser.parse_args()

    # 列出预设池
    if args.list_presets:
        print("\n预设ETF池:")
        for key, (codes, desc) in PRESET_POOLS.items():
            etf_list = ", ".join(f"{c}({ETF_POOL.get(c, c)})" for c in codes.split(","))
            print(f"  {key:<12s} — {desc}")
            print(f"             {etf_list}")
        print()
        return

    # 批量对比模式
    if args.batch:
        run_batch(
            presets=[p.strip() for p in args.batch_presets.split(",") if p.strip()],
            momentums=[int(m.strip()) for m in args.batch_momentums.split(",") if m.strip()],
            freq=args.freq,
            start_date=args.start,
            end_date=args.end,
            include_bench=not args.no_bench,
            switch_buffer=args.switch_buffer,
        )
        return

    # 解析ETF池
    if args.pool:
        codes = [c.strip() for c in args.pool.split(",") if c.strip()]
        pool = {c: ETF_POOL.get(c, c) for c in codes}
    else:
        preset_codes, _ = PRESET_POOLS[args.preset]
        codes = [c.strip() for c in preset_codes.split(",") if c.strip()]
        pool = {c: ETF_POOL.get(c, c) for c in codes}

    if args.rolling:
        # 滚动窗口模式
        results = run_rolling_backtest(
            pool=pool,
            window_months=args.window,
            step_months=args.step,
            freq=args.freq,
            momentum_period=args.momentum,
            switch_buffer=args.switch_buffer,
        )
        if results:
            print_rolling_report(results)
            if args.json:
                print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    else:
        # 单次回测模式
        execution = ExecutionConfig(
            commission_rate=(
                str(args.commission_rate) if args.commission_rate is not None else "0.00025"
            ),
            minimum_commission=(
                str(args.min_commission) if args.min_commission is not None else "0"
            ),
            slippage_rate=(
                str(args.slippage_rate) if args.slippage_rate is not None else "0.0005"
            ),
        )
        result = run_backtest(
            pool=pool,
            start_date=args.start,
            end_date=args.end,
            freq=args.freq,
            momentum_period=args.momentum,
            include_bench=not args.no_bench,
            switch_buffer=args.switch_buffer,
            execution=execution,
        )

        if not result:
            sys.exit(1)

        sys.stdout.flush()
        if args.json:
            # 清空前面的进度输出后输出纯JSON
            print("\n__JSON_START__")
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
            print("__JSON_END__")
        else:
            print_report(result)
            if args.csv:
                print_trades_csv(result)

    print()


if __name__ == "__main__":
    main()
