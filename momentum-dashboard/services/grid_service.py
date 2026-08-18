#!/usr/bin/env python3
"""网格服务层：配置/触发/持仓/分析纯业务逻辑。"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import db  # noqa: E402
from bizlog import _log  # noqa: E402
from market_tools import _num, fetch_realtime_quotes  # noqa: E402
from services.position_service import load_holdings_strategy  # noqa: E402


def _build_grid_positions(configs: dict, holdings_map: dict, quotes: dict) -> list[dict]:
    """网格持仓详情：底仓=数据库 holdings_current.base_shares，
    网格仓=总持仓-底仓（持仓/成本来自数据库，现价与盈亏按实时行情）。"""
    strategy_cfg = load_holdings_strategy()
    by_code_strategy = strategy_cfg.get("by_code", {}) or {}
    positions = []
    for code in configs:
        cfg = configs.get(code) or {}
        holding = holdings_map.get(code) or {}
        try:
            triggers = db.grid_triggers_for_code(code)
        except Exception as exc:
            _log(f"GRID 触发记录读取失败，按无触发处理: {code} ({exc})", "WARN")
            triggers = []
        total_shares = _num(holding.get("shares"))
        entry = by_code_strategy.get(code) or {}
        cost = _num(holding.get("cost"))
        if cost is None:
            cost = _num(cfg.get("cost_price"))
        price = _num((quotes or {}).get(code, {}).get("price"))
        base_now = _num(cfg.get("base_price"))
        if triggers:
            last_base = _num(triggers[-1].get("base_price_after"))
            if last_base is not None:
                base_now = last_base
        note = None
        if total_shares is None or total_shares <= 0:
            # 数据库中无该标的持仓：不拿 CONFIGS 兜底臆造持仓
            grid_shares = 0
            base_position = 0
            total_shares = 0
            note = "无持仓"
        else:
            strategy = str(
                holding.get("strategy") or entry.get("strategy") or ""
            )
            if strategy in ("网格", "共用"):
                db_base = _num(holding.get("base_shares"))
                if db_base is not None:
                    base_position = max(0, min(int(db_base), int(total_shares)))
                    if db_base > total_shares:
                        note = "底仓超出总持仓，已按总持仓截断"
                else:
                    # 数据库无底仓记录（旧数据）：回退配置并提示核对
                    cfg_base = _num(cfg.get("base_position")) or 0
                    cfg_grid = _num(cfg.get("grid_position")) or 0
                    if cfg_base > total_shares:
                        note = "配置底仓超出实际持仓，请核对 base_position"
                    elif abs((cfg_base + cfg_grid) - total_shares) / max(total_shares, 1) > 0.05:
                        note = "配置(base+grid)与实盘持仓不一致，底仓为估算"
                    base_position = min(cfg_base, total_shares)
                grid_shares = max(total_shares - base_position, 0)
            elif strategy == "底仓":
                base_position = int(total_shares)
                grid_shares = 0
            else:
                # 动量/现金等非网格标的：不参与网格拆分
                base_position = 0
                grid_shares = 0
                if note is None:
                    note = "非网格标的，不参与网格拆分"
        market_value = (
            round(total_shares * price, 3)
            if total_shares is not None and price
            else None
        )
        pnl = (
            round((price - cost) * total_shares, 3)
            if price is not None and cost is not None and total_shares is not None
            else None
        )
        pnl_pct = (
            round((price - cost) / cost * 100, 2)
            if price is not None and cost
            else None
        )
        positions.append({
            "code": code,
            "name": cfg.get("name", code),
            "strategy": holding.get("strategy") or entry.get("strategy") or "—",
            "bucket": holding.get("bucket") or entry.get("bucket") or "—",
            "base_position": int(base_position),
            "grid_position": int(grid_shares),
            "total_shares": int(total_shares),
            "price": price,
            "cost": cost,
            "market_value": market_value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "base_price": base_now,
            "note": note,
        })
    positions.sort(key=lambda p: -(p.get("market_value") or 0))
    return positions


def _grid_config_summary(code: str, meta: dict, base=None) -> dict:
    """根据回测实际生效参数生成网格配置摘要（触发条件/价格区间/委托/持仓区间）。"""
    from tools import grid_trading as gt

    cfg = gt.CONFIGS.get(code) or {}
    gp = meta.get("grid_params") or {}
    if base is None:
        base = _num(cfg.get("base_price"))
    up = _num(
        gp.get("spacing_up_pct") or cfg.get("grid_spacing_up_pct") or cfg.get("grid_spacing_pct")
    )
    down = _num(
        gp.get("spacing_down_pct") or cfg.get("grid_spacing_down_pct") or cfg.get("grid_spacing_pct")
    )
    levels_above = int(gp.get("levels_above") or cfg.get("levels_above") or 5)
    levels_below = int(gp.get("levels_below") or cfg.get("levels_below") or 5)
    order_size = int(gp.get("shares_per_grid") or cfg.get("shares_per_grid") or 1000)
    base_position = int(cfg.get("base_position") or 0)

    price_min = (
        round(base * (1 - down / 100) ** levels_below, 3)
        if base is not None and down
        else None
    )
    price_max = (
        round(base * (1 + up / 100) ** levels_above, 3)
        if base is not None and up
        else None
    )
    return {
        "base_price": round(base, 3) if base is not None else None,
        "sell_spacing_pct": up,    # 上涨多少卖
        "buy_spacing_pct": down,   # 下跌多少买
        "levels_above": levels_above,
        "levels_below": levels_below,
        "order_size": order_size,  # 委托数量（每笔）
        "price_range": {"min": price_min, "max": price_max},
        "position_range": {
            "min": base_position,
            "max": base_position + levels_below * order_size,
        },
        "execution_mode": "限价即时买一价卖出",
        "multiples": "已开启",
        "trigger_desc": (
            f"以 {base} 元为基准价，每上涨 +{up}% 卖出 / 每下跌 -{down}% 买入"
            if base is not None
            else "基准价未配置"
        ),
    }


GRID_OPT_SPACINGS = [1.0, 2.0, 3.0, 4.0, 5.0]     # 内置间距候选 %
GRID_OPT_LEVELS = [3, 5, 8]                        # 内置层数候选
GRID_OPT_VALUES = [200, 500, 1000, 2000, 5000]     # 内置每格金额候选（元）


def _grid_shares_candidates(base_price):
    """按基准价把「每格金额」折算成股数候选（100 股取整）。"""
    candidates = []
    for value in GRID_OPT_VALUES:
        if base_price:
            shares = max(100, int(round(value / base_price / 100)) * 100)
        else:
            shares = 500
        if shares not in candidates:
            candidates.append(shares)
    return candidates


def _grid_configs():
    """返回 (CONFIGS, code->name, code->shares_per_grid)。"""
    from tools import grid_trading as gt
    configs = gt.CONFIGS
    names = {code: (cfg or {}).get("name") for code, cfg in configs.items()}
    sizes = {
        code: (cfg or {}).get("shares_per_grid")
        for code, cfg in configs.items()
    }
    return configs, names, sizes


def _grid_base_chain(records: list[dict], configs: dict) -> list[dict]:
    """按时间顺序给记录计算基准价变化：
    网格类型每成交一次，基准价即变为成交价（before=当前基准，after=成交价）；
    加仓/减仓/动量不改变网格基准价（before=after=当前基准）。
    """
    base_cache: dict[str, float] = {}
    ordered = sorted(
        records,
        key=lambda r: (str(r.get("date") or ""), str(r.get("time") or "")),
    )
    for record in ordered:
        code = str(record.get("code") or "")
        if code not in base_cache:
            base = _num((configs.get(code) or {}).get("base_price"))
            existing = db.grid_triggers_for_code(code)
            if existing:
                last_base = _num(existing[-1].get("base_price_after"))
                if last_base is not None:
                    base = last_base
            base_cache[code] = base if base is not None else 0.0
        current = base_cache[code]
        trigger_type = (record.get("trigger_type") or "grid").strip() or "grid"
        price = _num(record.get("price"))
        if trigger_type == "grid" and price:
            record["base_price_before"] = current
            record["base_price_after"] = price
            base_cache[code] = price
        else:
            record["base_price_before"] = current
            record["base_price_after"] = current
    return ordered


def _recalc_grid_base_prices() -> int:
    """按新逻辑重算全部网格记录的基准价变化（触发记录已全部在数据库）。"""
    configs, _, _ = _grid_configs()
    codes = {row["code"] for row in db.query_grid_triggers(limit=2000)}
    changed = 0
    for code in codes:
        base = _num((configs.get(code) or {}).get("base_price")) or 0.0
        for row in db.grid_triggers_for_code(code):
            trigger_type = row.get("trigger_type") or "grid"
            price = _num(row.get("price"))
            before, after = base, base
            if trigger_type == "grid" and price:
                after = price
                base = price
            db.update_grid_trigger_base_prices(row["id"], before, after)
            changed += 1
    _log(f"GRID-BASE 已重算 {changed} 条记录基准价变化")
    return changed


def _mark_grid_duplicates(records: list[dict]) -> list[dict]:
    """与历史触发记录比对，标记 duplicate=True（同 code+date+time+金额+shares）。"""
    try:
        existing = db.query_grid_triggers(limit=2000)
        seen = {
            (
                str(row.get("code") or ""),
                str(row.get("trigger_date") or ""),
                round(
                    float(row.get("amount") or 0)
                    or (float(row.get("price") or 0) * int(row.get("shares") or 0)),
                    4,
                ),
                int(row.get("shares") or 0),
            )
            for row in existing
        }
        for record in records:
            price = _num(record.get("price")) or 0
            shares = int(record.get("shares") or 0)
            date = str(record.get("date") or "").strip()
            trade_time = str(record.get("time") or "").strip()
            combined = f"{date} {trade_time or '00:00:00'}" if date else ""
            key = (
                str(record.get("code") or ""),
                combined,
                round(price * shares, 4),
                shares,
            )
            record["duplicate"] = key in seen
    except Exception as exc:  # noqa: BLE001 - 比对失败不阻断识别
        _log(f"GRID-PARSE 历史比对失败: {exc}", "WARN")
        for record in records:
            record["duplicate"] = False
    return records


def _grid_trigger_analysis(code: str, current_price=None) -> dict:
    """结合网格触发记录分析当前配置合理性。

    只把 trigger_type='grid' 的记录视为网格自动行为（频率/方向链/连续同向）；
    主动加仓(add)/减仓(reduce)单列，不计入"网格接飞刀"判断。
    """
    config = db.get_grid_config(code) or {}
    records = db.grid_triggers_for_code(code) or []
    out = {
        "code": code,
        "has_triggers": bool(records),
        "count": len(records),
    }
    if not records:
        out["verdict"] = "暂无触发记录，无法评估实际触发行为"
        out["issues"] = []
        return out

    def _is_buy(record):
        action = str(record.get("action") or "").lower()
        return "buy" in action or "买" in action

    grid_records = [
        r for r in records
        if str(r.get("trigger_type") or "grid").strip().lower() == "grid"
    ]
    active_records = [
        r for r in records
        if str(r.get("trigger_type") or "grid").strip().lower() != "grid"
    ]
    adds = [r for r in active_records if str(r.get("trigger_type") or "").lower() == "add"]
    reduces = [
        r for r in active_records if str(r.get("trigger_type") or "").lower() == "reduce"
    ]
    out["grid_count"] = len(grid_records)
    out["add_count"] = len(adds)
    out["reduce_count"] = len(reduces)
    out["other_count"] = len(active_records) - len(adds) - len(reduces)

    if adds:
        add_shares = sum(int(r.get("shares") or 0) for r in adds)
        out["add_shares"] = add_shares
        out["add_avg_price"] = round(
            sum(float(r["price"]) for r in adds) / len(adds), 3
        )
    if reduces:
        out["reduce_shares"] = sum(int(r.get("shares") or 0) for r in reduces)
        out["reduce_avg_price"] = round(
            sum(float(r["price"]) for r in reduces) / len(reduces), 3
        )

    buys = [r for r in grid_records if _is_buy(r)]
    sells = [r for r in grid_records if not _is_buy(r)]
    out["buys"] = len(buys)
    out["sells"] = len(sells)

    dates = [
        str(r.get("trigger_date") or "")[:10]
        for r in grid_records if r.get("trigger_date")
    ]
    span_days = 1
    if len(dates) >= 2:
        try:
            span_days = (
                datetime.strptime(dates[-1], "%Y-%m-%d")
                - datetime.strptime(dates[0], "%Y-%m-%d")
            ).days + 1
        except ValueError:
            pass
    out["span_days"] = span_days
    out["freq_per_day"] = round(len(grid_records) / max(span_days, 1), 3)

    recent = grid_records[-8:]
    out["recent_chain"] = "".join("买" if _is_buy(r) else "卖" for r in recent)
    consec_buy = 0
    consec_sell = 0
    for r in reversed(recent):
        if _is_buy(r):
            consec_buy += 1
        else:
            break
    for r in reversed(recent):
        if not _is_buy(r):
            consec_sell += 1
        else:
            break
    out["consec_buy"] = consec_buy
    out["consec_sell"] = consec_sell

    avg_buy = (
        round(sum(float(r["price"]) for r in buys) / len(buys), 3)
        if buys else None
    )
    avg_sell = (
        round(sum(float(r["price"]) for r in sells) / len(sells), 3)
        if sells else None
    )
    out["avg_buy"] = avg_buy
    out["avg_sell"] = avg_sell
    if grid_records:
        grid_total = sum(
            int(r.get("shares") or 0) for r in grid_records
        )
        out["grid_shares"] = grid_total

    price = current_price
    if price is None:
        try:
            quotes = fetch_realtime_quotes([code])
            price = _num((quotes or {}).get(code, {}).get("price"))
        except Exception:
            pass
    base = _num(config.get("base_price"))
    low = _num(config.get("price_low"))
    high = _num(config.get("price_high"))
    out["current_price"] = price

    issues: list[str] = []
    if price is not None and low is not None and high is not None:
        if price < low:
            issues.append(f"现价 {price} 已低于区间下限 {low}，网格持续买入但价格在区间外")
        elif price > high:
            issues.append(f"现价 {price} 已高于区间上限 {high}，网格全部卖出后空转")
        elif base is not None and price > base * 1.02:
            issues.append(f"现价 {price} 高于基准 {base} 约 {(price / base - 1) * 100:.1f}%，卖出层更易触发")
        elif base is not None and price < base * 0.98:
            issues.append(f"现价 {price} 低于基准 {base} 约 {(1 - price / base) * 100:.1f}%，买入层更易触发")
    if span_days >= 20 and len(grid_records) / span_days > 0.5:
        issues.append("触发过于频繁（日均>0.5 次），间距可能偏窄，手续费侵蚀收益")
    elif span_days >= 30 and len(records) / span_days < 0.1:
        issues.append("触发过少（月均<3 次），间距可能偏宽或价格远离基准")
    if consec_buy >= 3:
        issues.append(f"最近连续 {consec_buy} 次买入，下跌中网格持续接飞刀，建议暂停买入或降低买入层")
    if consec_sell >= 3:
        issues.append(f"最近连续 {consec_sell} 次卖出，上涨中网格可能卖飞，建议加大卖出间距或减少层数")
    if buys and sells:
        ratio = len(buys) / max(len(sells), 1)
        if ratio > 1.8:
            issues.append("买入次数远多于卖出，价格重心下移，注意浮亏扩大")
        elif ratio < 0.55:
            issues.append("卖出次数远多于买入，价格重心上移，网格可能过早离场")
    notes: list[str] = []
    if adds:
        notes.append(
            f"另有 {len(adds)} 次主动加仓共 {out.get('add_shares')} 股"
            f"（均价 {out.get('add_avg_price')}），为策略操作，不计入网格判断"
        )
    if reduces:
        notes.append(
            f"另有 {len(reduces)} 次主动减仓共 {out.get('reduce_shares')} 股"
            f"（均价 {out.get('reduce_avg_price')}）"
        )
    out["notes"] = notes
    out["issues"] = issues
    out["verdict"] = "；".join(issues) if issues else "触发行为与当前配置基本匹配，暂无明显异常"
    # 最近 30 天成功触发的成交明细（供模型与前端综合分析）
    recent_trades = []
    try:
        cutoff = (datetime.now().astimezone() - timedelta(days=30)).strftime(
            "%Y-%m-%d"
        )
        for record in records:
            date = str(record.get("trigger_date") or "")[:10]
            if date >= cutoff:
                recent_trades.append({
                    "date": date,
                    "time": str(record.get("trigger_date") or "")[11:19],
                    "action": record.get("action"),
                    "type": record.get("trigger_type") or "grid",
                    "price": record.get("price"),
                    "shares": record.get("shares"),
                })
    except Exception as exc:
        _log(f"GRID 近期成交构建失败: {exc}", "WARN")
    out["recent_trades"] = recent_trades[-40:]
    return out
