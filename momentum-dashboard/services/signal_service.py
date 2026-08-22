#!/usr/bin/env python3
"""动量信号服务层：池配置 + 信号缓存/历史 + 盘中预测 + 定时任务。"""

from __future__ import annotations

import os
import time
from datetime import datetime

import db  # noqa: E402
from bizlog import _biz, _log, parse_json_output, run_script  # noqa: E402
from cache import set as cache_set  # noqa: E402
from market_tools import _etf_display_name, _num, fetch_realtime_quotes  # noqa: E402


# ---------------------------------------------------------------------------
# 池与预设配置：默认值仅作兜底，运行期以 momentum_pools 表为准
# ---------------------------------------------------------------------------

_DEFAULT_SIGNAL_POOLS = {
    "recommended": {
        "desc": "推荐 4-ETF（黄金+纳指+创业板+恒生）",
        "codes": ["518880", "513100", "159915", "159920"],
    },
    "full": {
        "desc": "全池 11 只（宽基/行业/跨境/防御）",
        "codes": [
            "510300", "510500", "159915", "588000",
            "512880", "512690", "512010",
            "513100", "513180", "159920", "518880",
        ],
    },
}

_DEFAULT_BACKTEST_PRESETS = {
    "default": ("518880,513100,159915", "黄金+纳指+创业板 (推荐3-ETF)"),
    "best3": ("518880,513100,159915", "黄金+纳指+创业板 (最优)"),
    "best4": ("518880,513100,159915,510300", "黄金+纳指+创业板+沪深300 (备选)"),
    "aggressive": ("518880,513100,159915,588000", "黄金+纳指+创业板+科创50 (激进)"),
    "full5": ("518880,513100,159915,510300,159920", "5只全明星"),
    "ashare": ("510300,159915,588000,510500", "A股纯宽基 (对比用)"),
    "original": ("159915,510300,512880,513180,512690,512010,159920,588000", "原始8-ETF池"),
    "all": ("518880,513100,159915,510300,588000,510050,512880,513180", "全品种大池"),
}

# 运行期池配置（DB 加载；默认值兜底）
SIGNAL_POOLS = dict(_DEFAULT_SIGNAL_POOLS)
BACKTEST_PRESETS = dict(_DEFAULT_BACKTEST_PRESETS)
for _preset_key, (_preset_codes, _preset_desc) in BACKTEST_PRESETS.items():
    SIGNAL_POOLS.setdefault(
        _preset_key,
        {"desc": f"回测预设: {_preset_desc}", "codes": _preset_codes.split(",")},
    )

# 动量池配置短缓存：避免每次 API 请求全表读 + 全局变量反复替换（30s 内复用）
_POOLS_TTL = 30.0
_POOLS_LOADED_AT = 0.0


def _reload_momentum_pools_from_db(force: bool = False) -> bool:
    """从 momentum_pools 表刷新 SIGNAL_POOLS/BACKTEST_PRESETS；表空则保持默认兜底。

    TTL 内（30s）直接复用内存池，避免高频请求重复读表；force=True 强制重读。
    """
    global SIGNAL_POOLS, BACKTEST_PRESETS, _POOLS_LOADED_AT
    now = time.time()
    # if not force and now - _POOLS_LOADED_AT < _POOLS_TTL:
    #     return True
    try:
        rows = db.load_momentum_pools()
        if not rows:
            _log("momentum_pools 表为空，使用默认池（如需自定义请先插入配置）", "WARN")
            return False
        pools: dict = {}
        presets: dict = {}
        for row in rows:
            if not row.get("enabled"):
                continue
            key = row.get("pool_key")
            if not key:
                continue
            codes = [c for c in str(row.get("codes") or "").split(",") if c]
            desc = row.get("description") or key
            pool_type = row.get("pool_type") or "signal"
            entry: dict = {"desc": desc, "codes": codes}
            if row.get("defensive_code"):
                entry["defensive_code"] = str(row["defensive_code"])
            if pool_type == "backtest":
                presets[key] = (",".join(codes), desc)
                entry["desc"] = f"回测预设: {desc}"
            pools[key] = entry
        if not pools:
            _log("momentum_pools 表无启用记录，使用默认池", "WARN")
            return False
        SIGNAL_POOLS = pools
        BACKTEST_PRESETS = presets
        _POOLS_LOADED_AT = now
        return True
    except Exception as exc:  # noqa: BLE001 - DB 不可达时回退默认池
        _log(f"momentum_pools 读取失败，使用默认池: {exc}", "WARN")
        return False


def cache_signals_from_monitor(momentum: dict) -> None:
    """把策略监测里的动量信号写入信号页缓存，页面立即展示最新数据。"""
    codes = ",".join(SIGNAL_POOLS["best4"]["codes"])
    key = f"signals-v3|25|{codes}"
    data = {
        "status": momentum.get("status"),
        "as_of": momentum.get("as_of"),
        "items": momentum.get("items", []),
        "errors": momentum.get("errors", []),
        "selected": momentum.get("selected"),
        "rotation": momentum.get("rotation"),
        "pool_complete": momentum.get("pool_complete"),
        "pool_label": SIGNAL_POOLS["best4"]["desc"],
        "momentum_period": 25,
    }
    if momentum.get("intraday_prediction"):
        data["intraday_prediction"] = True
    payload = {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": data,
    }
    try:
        cache_set(key, payload)
        _log(f"SCHED 信号缓存已更新 [{key}] as_of={momentum.get('as_of')}")
    except Exception as exc:
        _log(f"SCHED 信号缓存写入失败: {exc}", "WARN")


def record_signal_history(pool: str, momentum: int, data: dict) -> None:
    """把一次信号扫描结果写入 signal_history（每天每池每周期 upsert）。"""
    try:
        selected = data.get("selected") or {}
        rotation = data.get("rotation") or {}
        db.append_signal_history(
            data.get("as_of") or "",
            pool,
            momentum,
            data.get("status") or "",
            data.get("items") or [],
            selected.get("code"),
            selected.get("name"),
            rotation,
            data,
        )
    except Exception as exc:  # noqa: BLE001 - 落库失败不影响主流程
        _log(f"DB 信号历史写入失败: {exc}", "WARN")


def run_intraday_prediction() -> dict:
    """每日 14:30 盘中信号预测：用当天实时价合成当日 bar 跑动量扫描。

    14:30-15:00 为 provisional；15:00 后 market_closed=True（当日锁定信号）。
    实时行情整体失败直接抛错；部分失败返回 intraday_complete=False，
    由调用方决定不覆盖已有缓存。非交易日（周末）直接拒绝。
    """
    from dataclasses import replace
    from zoneinfo import ZoneInfo

    from tools.etf_market_data import (
        MarketDataSeries,
        _content_hash,
        load_etf_series,
    )
    from tools.momentum_signal import scan

    now_cn = datetime.now(ZoneInfo("Asia/Shanghai"))
    if now_cn.weekday() >= 5:
        raise ValueError("非交易日（周末），跳过盘中预测")
    codes = list(SIGNAL_POOLS["best4"]["codes"])
    pool = {code: _etf_display_name(code) for code in codes}
    today = now_cn.date().isoformat()
    quotes = fetch_realtime_quotes(codes)
    series_by_code = {}
    errors: list[str] = []
    fresh_codes: list[str] = []
    for code in codes:
        try:
            saved_offline = os.environ.get("ETF_DATA_OFFLINE")
            os.environ["ETF_DATA_OFFLINE"] = "1"
            try:
                series = load_etf_series(code, count=300)
            finally:
                if saved_offline is not None:
                    os.environ["ETF_DATA_OFFLINE"] = saved_offline
                else:
                    os.environ.pop("ETF_DATA_OFFLINE", None)
            bars = list(series.bars)
            price = _num((quotes or {}).get(code, {}).get("price"))
            if price is None or price <= 0:
                # 停牌/行情缺失/无效价：不合成假 bar，保持昨日序列并标记缺失
                series_by_code[code] = series
                errors.append(f"{code}: 实时价缺失或非法（{price}），使用缓存")
                continue
            if bars[-1]["date"] == today:
                series_by_code[code] = series
                fresh_codes.append(code)
                continue
            prev_close = bars[-1]["close"]
            new_bar = {
                "date": today,
                "open": prev_close,
                "close": price,
                "high": max(prev_close, price),
                "low": min(prev_close, price),
                "volume": bars[-1]["volume"],  # 沿用昨量，避免 volume=0 触发"缩量/放量"误报
            }
            merged = bars + [new_bar]
            manifest = replace(
                series.manifest,
                end_date=today,
                bar_count=len(merged),
                content_hash=_content_hash(merged),
            )
            series_by_code[code] = MarketDataSeries(tuple(merged), manifest)
            fresh_codes.append(code)
        except Exception as exc:
            errors.append(f"{code}: {exc}")
    if not fresh_codes:
        raise ValueError("实时行情整体获取失败，无法生成盘中预测")
    envelope = scan(
        pool,
        25,
        series_by_code=series_by_code,
        market_closed=(now_cn.hour * 60 + now_cn.minute) >= 15 * 60,
        now=now_cn,
    )
    envelope["intraday_prediction"] = True
    envelope["as_of"] = today
    envelope["intraday_complete"] = len(fresh_codes) == len(codes) and not errors
    if errors:
        envelope.setdefault("errors", []).extend(errors)
    return envelope


def scheduled_job() -> None:
    """定时任务：14:30 后用当天实时价做盘中信号预测 + 网格趋势 + 发送邮件；
    其余时段跑 strategy_monitor 全量报告 + 发送邮件。"""
    from tools import monitor_alert as ma
    from zoneinfo import ZoneInfo

    started = time.time()
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    _log("SCHED 定时任务开始：刷新信号 + 发送邮件")
    result = "error"
    detail = {}
    email_sent = False
    try:
        now_cn = datetime.now(ZoneInfo("Asia/Shanghai"))
        hour_minute = now_cn.hour * 60 + now_cn.minute
        # ── 14:30 之后：用实时行情合成当日 bar 跑信号（不用历史 bar，当天数据可能未更新）───
        if hour_minute >= 14 * 60 + 30:
            envelope = run_intraday_prediction()
            selected = envelope.get("selected") or {}
            is_complete = bool(envelope.get("intraday_complete"))

            # 完整时才覆盖缓存 + 写历史；不完整时保留上一版缓存
            if is_complete:
                cache_signals_from_monitor(envelope)
                record_signal_history("best4", 25, envelope)
            else:
                _log(
                    f"SCHED 盘中预测不完整（{len(envelope.get('errors') or [])} 个问题），"
                    f"保留上一版信号缓存",
                    "WARN",
                )

            # 始终发送邮件（即使行情部分失败也要通知用户，标注不完整）
            try:
                from tools import strategy_monitor as sm

                # 获取网格趋势分析（不跑 momentum/audit，避免重复计算）
                grid_report = sm.build_monitor_report(
                    include_momentum=False,
                    include_grid=True,
                    include_audit=False,
                )
                grid_data = grid_report.get("grid", [])
                grid_groups = grid_report.get("grid_groups", {})
                grid_action = sm._grid_action(grid_groups)

                # 收集所有 ETF 代码（动量 + 网格）拉取实时行情
                all_codes: set[str] = set()
                for item in (envelope.get("items") or []):
                    if item.get("code"):
                        all_codes.add(item["code"])
                for item in grid_data:
                    code = item.get("code") or item.get("etf", "")
                    if code and item.get("status") != "unknown":
                        all_codes.add(code)
                prices = fetch_realtime_quotes(list(all_codes)) if all_codes else {}

                momentum_action = (
                    "盘中信号预测（14:30 后 · provisional，未收盘，"
                    "收盘后以正式信号为准）"
                )
                if not is_complete:
                    error_count = len(envelope.get("errors") or [])
                    momentum_action = (
                        f"⚠️ 盘中预测不完整（{error_count} 个行情问题）· "
                        + momentum_action
                    )

                report = {
                    "momentum": envelope,
                    "grid": grid_data,
                    "grid_groups": grid_groups,
                    "risk": None,
                    "advice": {
                        "momentum_action": momentum_action,
                        "grid_action": grid_action or "",
                    },
                }
                html = ma.format_email_body(report, prices)
                smtp = ma._load_env()
                ma.send_email(smtp, html)
                email_sent = True
            except Exception as exc:  # noqa: BLE001 - 邮件失败不影响调度主流程
                _log(f"SCHED 盘中预测邮件发送失败: {exc}", "WARN")
            _log(
                f"SCHED 盘中信号预测: as_of={envelope.get('as_of')} "
                f"selected={selected.get('code')} {selected.get('signal_strength')} "
                f"complete={is_complete}"
            )
            result = "ok"
            detail = {
                "as_of": envelope.get("as_of"),
                "intraday": True,
                "complete": is_complete,
                "errors": (envelope.get("errors") or [])[:5],
                "selected": selected.get("code"),
            }
            return
        stdout = run_script(
            ["strategy_monitor.py", "--json"],
            timeout=600,
            offline=False,
        )
        report = parse_json_output(stdout)
        momentum = report.get("momentum") or {}
        if momentum.get("items"):
            cache_signals_from_monitor(momentum)
            record_signal_history("best4", 25, momentum)
        codes = [
            item.get("code")
            for item in (momentum.get("items") or [])
            if item.get("code")
        ]
        prices = fetch_realtime_quotes(codes) if codes else {}
        html = ma.format_email_body(report, prices)
        smtp = ma._load_env()
        ma.send_email(smtp, html)
        email_sent = True
        result = "ok"
        detail = {
            "as_of": momentum.get("as_of"),
            "items": len(momentum.get("items") or []),
            "selected": (momentum.get("selected") or {}).get("code"),
        }
        _biz(
            "SCHED",
            f"信号刷新 as_of={momentum.get('as_of')} 邮件已发送 "
            f"耗时 {time.time() - started:.1f}s",
        )
    except Exception as exc:  # noqa: BLE001 - 任务失败记录后不再向上抛
        detail = {"error": str(exc)}
        _log(f"SCHED 定时任务失败: {exc}", "ERROR")
    finally:
        try:
            db.append_scheduler_run(
                "schedule",
                started_at,
                datetime.now().astimezone().isoformat(timespec="seconds"),
                int((time.time() - started) * 1000),
                result,
                detail,
                email_sent,
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"DB 调度记录写入失败: {exc}", "WARN")
