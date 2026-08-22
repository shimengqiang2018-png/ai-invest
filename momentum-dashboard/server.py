#!/usr/bin/env python3
"""动量轮动策略仪表盘 — 本地 API 服务。

封装仓库内已有的动量轮动工具（信号扫描 / 策略监测 / 回测 / 审计 / 选品），
统一以 JSON API 提供给前端页面。数据统一走 db.py 数据访问层（MySQL）与
cache.py 缓存抽象层（默认 DB 持久化后端）。

用法:
    python3 momentum-dashboard/server.py
    python3 momentum-dashboard/server.py --port 8765 --host 127.0.0.1

打开 http://127.0.0.1:8765 访问仪表盘。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PROJECT_DIR = ROOT.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import db  # noqa: E402 - 数据访问层（唯一含 SQL 的层，可整体切换数据库）
from bizlog import (  # noqa: E402 - 日志 / 子进程执行器
    _log,
    _biz,
    _maintenance_loop,
    _MAINT_STOP,
    parse_json_output,
    run_script,
    run_script_stream,
)
from market_tools import (  # noqa: E402 - 行情 / 技术面工具
    _etf_display_name,
    _grid_screener_technicals,
    _is_lof,
    _is_t0,
    _json_safe,
    _kline_end_date,
    _kline_from_cache,
    _lightweight_trend_score,
    _num,
    _qq_code,
    _read_json_file,
    _refresh_grid_kline_cache,
    _single_code_technicals,
    _single_grid_score,
    fetch_realtime_quotes,
    load_kline,
)
import services.signal_service as signal_svc  # noqa: E402 - 信号服务层
import services.position_service as position_svc  # noqa: E402 - 持仓服务层
import services.grid_service as grid_svc  # noqa: E402 - 网格服务层
from cache import cached  # noqa: E402 - 缓存抽象层（后端默认 DB）
from cache import backend_name as cache_backend_name  # noqa: E402
from cache import get as cache_get  # noqa: E402
from cache import set as cache_set  # noqa: E402
from cache import set_logger as cache_set_logger  # noqa: E402

cache_set_logger(_log)

STATIC_DIR = ROOT / "static"
TOOLS_DIR = PROJECT_DIR / "tools"
DATA_DIR = PROJECT_DIR / "data"

CODE_RE = re.compile(r"^\d{6}(,\d{6})*$")




def _load_env_file():
    """从项目根目录 .env 加载环境变量（模型 API Key 等，仅本地）。"""
    env_path = ROOT.parent / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


_load_env_file()



# 默认离线模式: 页面加载只读本地缓存，秒开不卡；点“刷新数据”才联网更新。
# 用 --online 启动则页面加载也允许联网刷新。
ALLOW_ONLINE = False


def _resolve_backtest_start(start_raw):
    """把回测区间参数解析为起始日期：full / 1y-20y / YYYY-MM-DD。"""
    if not start_raw or start_raw == "full":
        return "2013-01-01"
    if re.fullmatch(r"\d+y", start_raw):
        years = int(start_raw[:-1])
        if not 1 <= years <= 20:
            raise ValueError("回测区间年份需在 1-20 之间")
        today = datetime.now().date()
        try:
            target = today.replace(year=today.year - years)
        except ValueError:
            target = today.replace(year=today.year - years, day=28)
        return target.isoformat()
    try:
        datetime.strptime(start_raw, "%Y-%m-%d")
    except ValueError:
        raise ValueError(
            f"start 需为 full / 1y / 3y / 5y / 10y 或 YYYY-MM-DD，当前: {start_raw}"
        )
    return start_raw





# ---------------------------------------------------------------------------




def api_pools(params=None):
    signal_svc._reload_momentum_pools_from_db()
    presets = {
        key: {"codes": codes, "desc": desc}
        for key, (codes, desc) in signal_svc.BACKTEST_PRESETS.items()
    }
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "signal_pools": signal_svc.SIGNAL_POOLS,
            "backtest_presets": presets,
        },
    }


def api_signals(params):
    signal_svc._reload_momentum_pools_from_db()
    pool = params.get("pool", ["best4"])[0]
    refresh = params.get("refresh", ["0"])[0] == "1"
    momentum = int(params.get("momentum", ["25"])[0])
    holding = (params.get("holding", [None])[0] or "").strip().upper() or None
    switch_buffer = float((params.get("switch_buffer", ["1.5"])[0] or "1.5").strip())
    if not 5 <= momentum <= 120:
        raise ValueError("动量周期需在 5-120 之间")
    if pool in signal_svc.SIGNAL_POOLS:
        codes = ",".join(signal_svc.SIGNAL_POOLS[pool]["codes"])
        pool_label = signal_svc.SIGNAL_POOLS[pool]["desc"]
    elif CODE_RE.match(pool):
        codes = pool
        pool_label = f"自定义池 {pool}"
    else:
        raise ValueError(f"未知信号池: {pool}")

    key = f"signals-v3|{momentum}|{codes}|h={holding or ''}|sb={switch_buffer}"

    def producer():
        cli = ["momentum_signal.py", "--pool", codes, "--momentum", str(momentum),
               "--switch-buffer", str(switch_buffer)]
        if holding:
            cli.extend(["--holding", holding])
        cli.append("--json")
        stdout = run_script(
            cli,
            timeout=420,
            offline=not (refresh or ALLOW_ONLINE),
        )
        data = parse_json_output(stdout)
        data["pool_label"] = pool_label
        data["momentum_period"] = momentum
        return data

    payload, _, _ = cached(key, 600, refresh, producer)
    data = payload.get("data") or {}
    items = data.get("items") or []
    for item in items:
        if not item.get("name") or item.get("name") == item.get("code"):
            item["name"] = _etf_display_name(item.get("code") or "")
    _biz(
        "SIGNAL",
        f"pool={pool_label} momentum={momentum}日 status={data.get('status')} "
        f"as_of={data.get('as_of')} items={len(items)} "
        f"holding={holding or '-'} buffer={switch_buffer}",
    )
    for item in items:
        _biz(
            "SIGNAL",
            f"  {item.get('code')} {item.get('name')} "
            f"rsrs={item.get('rsrs_score')} slope={item.get('slope_annual_pct')} "
            f"pass={item.get('pass')} strength={item.get('signal_strength')}",
        )
    selected = data.get("selected")
    if selected:
        _biz(
            "SIGNAL",
            f"  >> 目标 {selected.get('code')} {selected.get('name')} "
            f"strength={selected.get('signal_strength')}",
        )
    rotation = data.get("rotation")
    if rotation:
        target = rotation.get("target") or {}
        _biz(
            "SIGNAL",
            f"  >> 轮动 action={rotation.get('action')} "
            f"target={target.get('code') or ''} {target.get('name') or ''} "
            f"reason={rotation.get('reason') or ''}",
        )
    signal_svc.record_signal_history(pool, momentum, data)
    return payload


def api_overview(params):
    signal_svc._reload_momentum_pools_from_db()
    refresh = params.get("refresh", ["0"])[0] == "1"
    pool = (params.get("pool", [""])[0] or "").strip()
    switch_buffer = float((params.get("switch_buffer", ["1.0"])[0] or "1.0").strip())
    pool_codes = ""
    pool_label = ""
    if pool in signal_svc.SIGNAL_POOLS:
        pool_codes = ",".join(signal_svc.SIGNAL_POOLS[pool]["codes"])
        pool_label = signal_svc.SIGNAL_POOLS[pool]["desc"]
    elif CODE_RE.match(pool):
        pool_codes = pool
        pool_label = f"自定义池 {pool}"
    if not pool_label:
        pool_label = signal_svc.SIGNAL_POOLS.get("best4", {}).get("desc", "")
    key = f"overview-v3|{pool_codes or 'full'}|sb={switch_buffer}"

    def producer():
        cli = ["strategy_monitor.py", "--json"]
        stdout = run_script(
            cli,
            timeout=600,
            offline=not (refresh or ALLOW_ONLINE),
        )
        report = parse_json_output(stdout)
        if pool_codes:
            # 组合池：动量段用所选池重扫（含迟滞），网格/审计沿用监测报告
            mom_cli = [
                "momentum_signal.py",
                "--pool", pool_codes,
                "--switch-buffer", str(switch_buffer),
                "--json",
            ]
            mom = parse_json_output(
                run_script(
                    mom_cli,
                    timeout=420,
                    offline=not (refresh or ALLOW_ONLINE),
                )
            )
            report["momentum"] = {
                "status": mom.get("status"),
                "as_of": mom.get("as_of"),
                "items": mom.get("items") or [],
                "errors": mom.get("errors") or [],
                "selected": mom.get("selected"),
                "pool_complete": mom.get("pool_complete"),
                "confidence": None,
            }
            status = report["momentum"].get("status")
            selected = report["momentum"].get("selected")
            advice = report.get("advice") or {}
            if status == "ok" and selected:
                advice["momentum_action"] = (
                    f"按信号换仓至 {selected.get('code')} {selected.get('name', '')}"
                ).strip()
            elif status == "no_signal":
                advice["momentum_action"] = "持币或切换至 511880 银华日利"
            else:
                advice["momentum_action"] = None
            report["advice"] = advice
        report["pool_label"] = pool_label
        report["pool"] = pool
        return report

    payload, _, _ = cached(key, 900, refresh, producer)
    data = payload.get("data") or {}
    momentum = data.get("momentum") or {}
    selected = momentum.get("selected")
    _biz(
        "OVERVIEW",
        f"动量 pool={pool_label} status={momentum.get('status')} as_of={momentum.get('as_of')} "
        f"selected={selected and selected.get('code')} "
        f"pool_complete={momentum.get('pool_complete')}",
    )
    groups = data.get("grid_groups") or {}
    _biz(
        "OVERVIEW",
        f"网格 stop={[g.get('code') for g in groups.get('stop', [])]} "
        f"caution={[g.get('code') for g in groups.get('caution', [])]} "
        f"ok={len(groups.get('ok', []))} 只",
    )
    risk = data.get("risk") or {}
    _biz(
        "OVERVIEW",
        f"风险 status={risk.get('status')} sharpe={risk.get('sharpe')} "
        f"maxDD={risk.get('max_dd_pct')}% VaR95={risk.get('var_95_loss_pct')}% "
        f"IC10={risk.get('ic_10d')} IC20={risk.get('ic_20d')}",
    )
    advice = data.get("advice") or {}
    _biz(
        "OVERVIEW",
        f"建议 动量={advice.get('momentum_action')} | 网格={advice.get('grid_action')}",
    )
    return payload


def api_backtest(params):
    signal_svc._reload_momentum_pools_from_db()
    preset = (params.get("preset", [""])[0] or "").strip()
    pool_param = (params.get("pool", [""])[0] or "").strip()
    if pool_param and not preset:
        # 兼容 pool 参数：回测预设名 / 信号池名 / 逗号分隔代码
        preset = pool_param
    if not preset:
        preset = "best4"
    momentum = int(params.get("momentum", ["25"])[0])
    freq = params.get("freq", ["biweekly"])[0]
    start_raw = (params.get("start", [""])[0] or "").strip() or "full"
    commission = float(params.get("commission", ["0.00025"])[0])
    min_commission = float(params.get("min_commission", ["0"])[0])
    refresh = params.get("refresh", ["0"])[0] == "1"
    switch_buffer = float((params.get("switch_buffer", ["1.0"])[0] or "1.0").strip())
    if not 5 <= momentum <= 120:
        raise ValueError("动量周期需在 5-120 之间")
    if freq not in {"daily", "weekly", "biweekly", "monthly"}:
        raise ValueError("freq 需为 daily/weekly/biweekly/monthly")
    if switch_buffer < 1.0:
        raise ValueError("switch_buffer 需 >= 1.0")
    resolved_start = _resolve_backtest_start(start_raw)
    if not 0 <= commission <= 0.01:
        raise ValueError("佣金费率需在 0-0.01 之间（如 0.00025=万2.5）")
    if not 0 <= min_commission <= 100:
        raise ValueError("最低佣金需在 0-100 元之间（0=免5）")

    # 解析池：回测预设 / 信号池 / 自定义代码
    pool_codes = None
    if preset in signal_svc.BACKTEST_PRESETS:
        pool_label = signal_svc.BACKTEST_PRESETS[preset][1]
    elif preset in signal_svc.SIGNAL_POOLS:
        pool_codes = ",".join(signal_svc.SIGNAL_POOLS[preset]["codes"])
        pool_label = signal_svc.SIGNAL_POOLS[preset]["desc"]
    elif CODE_RE.match(preset):
        pool_codes = preset
        pool_label = f"自定义池 {preset}"
    else:
        signal_names = ", ".join(
            key for key in signal_svc.SIGNAL_POOLS if key not in signal_svc.BACKTEST_PRESETS
        )
        raise ValueError(
            f"未知回测池: {preset}（可用预设: {', '.join(signal_svc.BACKTEST_PRESETS)}；"
            f"可用信号池: {signal_names}；或逗号分隔的 ETF 代码）"
        )

    pool_spec = pool_codes or preset
    key = (
        f"backtest-v3|{pool_spec}|{momentum}|{freq}|{start_raw}"
        f"|{commission}|{min_commission}|sb={switch_buffer}"
    )

    def producer():
        args = ["momentum_etf_backtest.py"]
        if pool_codes:
            args += ["--pool", pool_codes]
        else:
            args += ["--preset", preset]
        args += [
            "--momentum", str(momentum),
            "--freq", freq,
            "--start", resolved_start,
            "--commission-rate", str(commission),
            "--min-commission", str(min_commission),
            "--switch-buffer", str(switch_buffer),
            "--json",
        ]
        stdout = run_script(args, timeout=900, offline=not (refresh or ALLOW_ONLINE))
        data = parse_json_output(stdout)
        # market_data 是冻结的行情对象，序列化后体积过大，前端不需要
        data.pop("market_data", None)
        data.pop("data_manifest", None)
        # 日频 NAV 降采样到最多 1000 点供绘图
        daily_nav = data.get("daily_nav") or []
        if len(daily_nav) > 1000:
            step = len(daily_nav) / 1000
            data["daily_nav"] = [
                daily_nav[int(i * step)]
                for i in range(1000)
            ] + [daily_nav[-1]]
            data["daily_nav_sampled"] = True
        data["preset"] = preset
        data["pool_label"] = pool_label
        data["momentum"] = momentum
        data["freq"] = freq
        data["start_raw"] = start_raw
        data["commission"] = commission
        data["min_commission"] = min_commission
        data["switch_buffer"] = switch_buffer
        return data

    payload, from_cache, _ = cached(key, 7200, refresh, producer)
    data = payload.get("data") or {}
    period = data.get("period") or {}
    perf = data.get("performance") or {}
    _biz(
        "BACKTEST",
        f"pool={data.get('pool_label') or preset} momentum={momentum}日 freq={freq} "
        f"start={start_raw}({resolved_start}) 佣金={commission}(最低{min_commission}元) "
        f"区间={period.get('start')}~{period.get('end')} ({period.get('years')}年)",
    )
    _biz(
        "BACKTEST",
        f"  total={perf.get('total_return_pct')}% annual={perf.get('annual_return_pct')}% "
        f"excess={perf.get('excess_return_pct')}pp maxDD={perf.get('max_dd_pct')}% "
        f"sharpe={perf.get('sharpe')} sortino={perf.get('sortino')} "
        f"calmar={perf.get('calmar')} trades={perf.get('num_trades')} "
        f"winRate={perf.get('trade_win_rate_pct')}% plRatio={perf.get('profit_loss_ratio')} "
        f"nav={perf.get('final_nav')}",
    )
    trades = data.get("trades") or []
    buy_count = sum(1 for t in trades if "买入" in str(t.get("action", "")))
    sell_count = sum(1 for t in trades if "卖出" in str(t.get("action", "")))
    _biz("BACKTEST", f"  交易 {len(trades)} 笔 (买入 {buy_count} / 卖出 {sell_count})")
    if not from_cache:
        try:
            db.upsert_backtest_result(
                "backtest",
                key,
                {
                    "preset": preset,
                    "pool": pool_spec,
                    "momentum": momentum,
                    "freq": freq,
                    "start": start_raw,
                    "commission": commission,
                    "min_commission": min_commission,
                    "switch_buffer": switch_buffer,
                },
                {
                    "total_return_pct": perf.get("total_return_pct"),
                    "annual_return_pct": perf.get("annual_return_pct"),
                    "max_dd_pct": perf.get("max_dd_pct"),
                    "sharpe": perf.get("sharpe"),
                    "sortino": perf.get("sortino"),
                    "calmar": perf.get("calmar"),
                    "num_trades": perf.get("num_trades"),
                    "final_nav": perf.get("final_nav"),
                    "trade_win_rate_pct": perf.get("trade_win_rate_pct"),
                    "profit_loss_ratio": perf.get("profit_loss_ratio"),
                    "period_start": period.get("start"),
                    "period_end": period.get("end"),
                    "period_years": period.get("years"),
                },
                data,
            )
        except Exception as exc:  # noqa: BLE001 - 落库失败不影响主流程
            _log(f"DB 回测结果写入失败: {exc}", "WARN")
    return payload


def api_backtest_history(params=None):
    """回测历史列表（分页）：最近运行的 backtest 组合（不含完整 payload）。"""
    params = params or {}
    limit = int((params.get("limit") or ["10"])[0] or 10)
    offset = int((params.get("offset") or ["0"])[0] or 0)
    items, total = db.list_backtest_results("backtest", limit=limit, offset=offset)
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"items": items, "total": total, "limit": limit, "offset": offset},
    }


def api_audit(params):
    refresh = params.get("refresh", ["0"])[0] == "1"
    key = "audit-v3|full"

    def producer():
        stdout = run_script(
            ["strategy_audit.py", "--json"],
            timeout=900,
            offline=not (refresh or ALLOW_ONLINE),
        )
        return parse_json_output(stdout)

    payload, _, _ = cached(key, 7200, refresh, producer)
    data = payload.get("data") or {}
    daily = data.get("daily_metrics") or {}
    ic = data.get("ic_ir") or {}
    stress = data.get("stress_test") or {}
    _biz(
        "AUDIT",
        f"日频 annual={daily.get('annual_return_pct')}% sharpe={daily.get('sharpe')} "
        f"maxDD={daily.get('max_dd_pct')}% VaR95={daily.get('var_95_daily_pct')}% "
        f"win_rate={daily.get('win_rate_pct')}% samples={daily.get('count')}",
    )
    _biz(
        "AUDIT",
        f"  IC/IR 10日={ic.get('ic_10d')}/{ic.get('ir_10d')} "
        f"20日={ic.get('ic_20d')}/{ic.get('ir_20d')} "
        f"40日={ic.get('ic_40d')}/{ic.get('ir_40d')} n_dates={ic.get('n_dates')}",
    )
    _biz(
        "AUDIT",
        f"  压力情景 {len(stress.get('scenarios', []))} 个 "
        f"VaR95={stress.get('var_95_pct')}% VaR99={stress.get('var_99_pct')}% "
        f"CVaR95={stress.get('cvar_95_pct')}%",
    )
    return payload


def api_screener(params):
    refresh = params.get("refresh", ["0"])[0] == "1"
    key = "screener|full"

    def producer():
        stdout = run_script(
            ["etf_screener.py", "--json"],
            timeout=900,
            offline=not (refresh or ALLOW_ONLINE),
        )
        return parse_json_output(stdout)

    payload, from_cache, _ = cached(key, 86400, refresh, producer)
    data = payload.get("data") or {}
    results = data.get("results") or []
    top5 = [(r.get("code"), r.get("total")) for r in results[:5]]
    _biz(
        "SCREENER",
        f"候选 {data.get('candidates_count')} 只 top5={top5} "
        f"avg_corr={data.get('avg_selected_corr')}",
    )
    recommended = data.get("recommended") or []
    _biz(
        "SCREENER",
        f"  推荐组合 {[(r.get('code'), r.get('total')) for r in recommended]}",
    )
    if not from_cache:
        try:
            db.upsert_backtest_result(
                "screener",
                "full",
                {"top": len(results), "generated_at": data.get("generated_at")},
                {
                    "candidates_count": data.get("candidates_count"),
                    "avg_selected_corr": data.get("avg_selected_corr"),
                    "recommended": [
                        (r.get("code"), r.get("total")) for r in recommended
                    ],
                },
                data,
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"DB 选品结果写入失败: {exc}", "WARN")
    return payload


def api_positions(params=None):
    data = position_svc.latest_positions()
    _log(
        f"FILE positions 快照 {data.get('date', '?')} "
        f"({len(data.get('holdings', []))} 只持仓)",
        "INFO",
    )
    summary = data.get("account_summary") or {}
    holdings = data.get("holdings") or []
    # 策略归属：按方案文档的网格/动量标的口径自动标注（优先保留已有标注）
    strategy_config = position_svc.load_holdings_strategy()
    by_code = strategy_config.get("by_code", {})
    default_cfg = strategy_config.get("default", {})
    for holding in holdings:
        code = str(holding.get("code") or "").strip()
        entry = by_code.get(code) or {}
        strategy = holding.get("strategy") or entry.get("strategy") or default_cfg.get("strategy", "其他")
        holding["strategy"] = strategy
        holding["bucket"] = holding.get("bucket") or entry.get("bucket") or default_cfg.get("bucket", "其他")
        holding["strategy_note"] = entry.get("note") or default_cfg.get("note", "")
    buckets: dict[str, dict] = {}
    for holding in holdings:
        bucket = holding.get("bucket") or "其他"
        item = buckets.setdefault(bucket, {"market_value": 0.0, "count": 0})
        item["market_value"] += holding.get("market_value") or 0
        item["count"] += 1
    total_mv = sum(item["market_value"] for item in buckets.values())
    for bucket, item in buckets.items():
        item["weight"] = (
            round(item["market_value"] / total_mv * 100, 2) if total_mv else 0
        )
        item["market_value"] = round(item["market_value"], 3)
    data["strategy_summary"] = buckets
    data["notes"] = [
        "两套策略资金物理隔离：网格子账户与动量子账户互相不救、不补、不挪用",
        "策略归属按「网格+动量双策略操作方案」标的口径自动标注，可在 holdings_strategy.json 调整",
    ]
    _biz(
        "POSITION",
        f"date={data.get('date')} total={summary.get('total_assets')} "
        f"sec={summary.get('securities_value')} cash={summary.get('available_cash')} "
        f"pos_ratio={summary.get('position_ratio')}% total_pnl={summary.get('total_pnl')} "
        f"daily_pnl={summary.get('daily_pnl')} holdings={len(holdings)}",
    )
    _biz(
        "POSITION",
        f"  子账户市值 { {k: round(v['market_value']) for k, v in buckets.items()} }",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": data,
    }


def api_kline(params):
    code = params.get("code", [""])[0]
    count = min(max(int(params.get("count", ["300"])[0]), 60), 2000)
    if not CODE_RE.match(code) or "," in code:
        raise ValueError("非法证券代码")
    key = f"kline-v2|{code}|{count}"

    def producer():
        bars, meta = load_kline(code, count)
        if not bars:
            raise ValueError(f"{code} 无可用 K 线数据（缓存缺失且网络不可用）")
        return {"code": code, "count": len(bars), "meta": meta, "bars": bars}

    payload, _, _ = cached(key, 3600, False, producer)
    data = payload.get("data") or {}
    meta = data.get("meta") or {}
    _biz(
        "KLINE",
        f"{code} bars={data.get('count')} 根 "
        f"({meta.get('start_date')}~{meta.get('end_date')}, 来源 {meta.get('source')})",
    )
    return payload


def api_stoploss(params):
    code = params.get("code", [""])[0]
    entry = params.get("entry", [""])[0]
    if not CODE_RE.match(code) or "," in code:
        raise ValueError("非法证券代码")
    try:
        price = float(entry)
    except ValueError:
        raise ValueError("entry 必须是数字价格")
    key = f"stoploss|{code}|{price}"

    def producer():
        stdout = run_script(
            ["momentum_signal.py", "--entry", code, str(price), "--json"],
            timeout=120,
            offline=True,
        )
        return parse_json_output(stdout)

    payload, _, _ = cached(key, 300, False, producer)
    data = payload.get("data") or {}
    _biz(
        "STOPLOSS",
        f"{code} entry={data.get('entry_price')} current={data.get('current_price')} "
        f"loss={data.get('loss_pct')}% stop_line={data.get('stop_loss_line')} "
        f"triggered={data.get('triggered')}",
    )
    return payload


def api_realtime(params):
    codes_raw = params.get("codes", [""])[0]
    codes = [c.strip() for c in codes_raw.split(",") if c.strip()]
    if not codes or len(codes) > 20 or not CODE_RE.match(",".join(codes)):
        raise ValueError("codes 需为逗号分隔的 6 位证券代码（最多 20 个）")
    key = f"realtime|{','.join(codes)}"

    def producer():
        quotes = fetch_realtime_quotes(codes)
        live = len(quotes) == len(codes)
        merged = dict(quotes)
        if not live:
            for code in codes:
                if code in merged:
                    continue
                bars, meta = load_kline(code, 120)
                if bars:
                    merged[code] = {
                        "code": code,
                        "name": code,
                        "price": bars[-1]["close"],
                        "change_pct": None,
                        "prev_close": None,
                        "source": "daily_close",
                    }
        _biz(
            "REALTIME",
            f"codes={','.join(codes)} live={live} "
            f"quotes={[(c, q.get('price')) for c, q in merged.items()]}",
        )
        return {
            "live": live,
            "quotes": merged,
            "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
            "note": None if live else "实时行情获取失败，展示最近收盘价",
        }

    payload, _, _ = cached(key, 20, False, producer)
    return payload


_ENUM_NAME_CACHE: dict[str, str] | None = None


def _etf_name_map() -> dict[str, str]:
    """懒加载 data/etf_meta.json 的 {code: name}，用于枚举组合标签显示标的名字。"""
    global _ENUM_NAME_CACHE
    if _ENUM_NAME_CACHE is None:
        _ENUM_NAME_CACHE = {}
        meta = _read_json_file(DATA_DIR / "etf_meta.json", None) or {}
        for e in meta.get("etfs") or []:
            _ENUM_NAME_CACHE[str(e.get("code") or "")] = str(e.get("name") or "")
    return _ENUM_NAME_CACHE


def _combo_label(combo: str, fallback: str) -> str:
    """组合代码串 → 名称(代码) 串（如 159696+159834 → 纳100ETF(159696)+金ETF(159834)）。"""
    parts = [c for c in str(combo).split("+") if c]
    if not parts:
        return fallback
    names = _etf_name_map()
    rendered = []
    for p in parts:
        n = names.get(p)
        rendered.append(f"{n}({p})" if n else p)
    return "+".join(rendered)


def _normalize_enum_results(data: dict) -> list[dict]:
    """字段归一化：兼容新旧 schema（新: annual_pct/label/n_etf/win_rate/num_trades）。"""
    results = []
    for r in data.get("results") or []:
        combo = r.get("combo", "")
        results.append({
            "combo": combo,
            "label": _combo_label(combo, r.get("label", combo)),
            "n": r.get("n", r.get("n_etf", len(combo.split("+")))),
            "ann": r.get("ann", r.get("annual_pct")),
            "total": r.get("total", r.get("total_pct")),
            "dd": r.get("dd", r.get("max_dd_pct")),
            "sharpe": r.get("sharpe"),
            "sortino": r.get("sortino"),
            "calmar": r.get("calmar"),
            "wr": r.get("wr", r.get("win_rate")),
            "trades": r.get("trades", r.get("num_trades")),
            "dsr": r.get("dsr_prob"),
            "excess": r.get("excess_pct"),
            "vol": r.get("annual_vol_pct"),
            "dd_days": r.get("max_dd_days"),
            "momentum": r.get("momentum"),
            "window_start": r.get("window_start"),
            "period_years": r.get("period_years"),
            "window_truncated": r.get("window_truncated"),
        })
    return results


def _enum_db_row_to_data(row: dict) -> dict:
    """把 MySQL backtest_results 里的 enum 行还原成 /api/enum 的数据结构。"""
    payload = row.get("payload") or {}
    summary = row.get("summary") or {}
    return {
        "generated_at": payload.get("generated_at") or row.get("updated_at"),
        "total_combos": summary.get("total_combos") or payload.get("total_combos"),
        "valid_results": summary.get("valid_results"),
        "config": row.get("params_key"),
        "results": payload.get("results") or [],
    }


def _latest_enum_data() -> tuple[dict, str]:
    """最新枚举结果 (data, 来源描述)。读取顺序：重算缓存 → MySQL → 仓库快照。"""
    live_path = DATA_DIR / "cache" / "enum_backtest_latest.json"
    enum_path = DATA_DIR / "enum_backtest_veteran_c3_25d.json"
    if not enum_path.exists():
        enum_path = DATA_DIR / "enum_backtest_15c3.json"
    data = _read_json_file(live_path, None)
    source = live_path.name
    if data is None or not data.get("results"):
        db_row = db.latest_backtest_result("enum")
        if db_row and (db_row.get("payload") or {}).get("results"):
            data = _enum_db_row_to_data(db_row)
            source = "mysql:enum"
    if data is None or not data.get("results"):
        data = _read_json_file(enum_path, {})
        source = enum_path.name
    return data, source


def api_enum(params=None):
    # 读取顺序：后台重算写入的最新缓存 → MySQL 最近一次枚举 → 仓库内快照。
    data, source_path = _latest_enum_data()
    _log(
        f"FILE enum 组合枚举 {data.get('total_combos', '?')} 组 "
        f"配置={data.get('config', '?')} (生成于 {data.get('generated_at', '?')})"
    )
    results = _normalize_enum_results(data)
    _biz(
        "ENUM",
        f"组合数 {data.get('total_combos')} 有效 {data.get('valid_results')} "
        f"配置={data.get('config')} 生成于 {data.get('generated_at')} "
        f"top5={[(r.get('combo'), r.get('ann')) for r in results[:5]]}",
    )
    try:
        db.upsert_backtest_result(
            "enum",
            str(data.get("config") or "default"),
            {
                "generated_at": data.get("generated_at"),
                "total_combos": data.get("total_combos"),
                "file": source_path,
            },
            {
                "total_combos": data.get("total_combos"),
                "valid_results": data.get("valid_results"),
                "top5": [(r.get("combo"), r.get("ann")) for r in results[:5]],
            },
            {"generated_at": data.get("generated_at"), "results": results},
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"DB 枚举结果写入失败: {exc}", "WARN")
    return {
        "ok": True,
        "cached": True,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "generated_at": data.get("generated_at"),
            "total_combos": data.get("total_combos"),
            "valid_results": data.get("valid_results"),
            "config": data.get("config"),
            "results": results,
        },
    }


def api_etf_scan(params):
    top = min(max(int(params.get("top", ["100"])[0]), 1), 640)
    category = params.get("category", [""])[0]
    # 优先读取后台重算（/api/etf-scan/recalc）写入的最新结果，回退仓库内快照。
    data = _read_json_file(DATA_DIR / "cache" / "etf_backtest_results.json", None)
    if data is None or not data.get("results"):
        data = _read_json_file(DATA_DIR / "etf_backtest_results.json", {})
    results = data.get("results", [])
    if category:
        results = [r for r in results if category in (r.get("category") or "")]
    results = sorted(results, key=lambda r: r.get("composite_score", 0), reverse=True)
    _log(
        f"FILE etf-scan 全市场回测 top={top} category='{category}' "
        f"(共 {len(results)} 条, 生成于 {data.get('generated_at', '?')})"
    )
    _biz(
        "ETF-SCAN",
        f"top={top} category={category or '全部'} rows={len(results)} "
        f"best={[(r.get('code'), r.get('name'), r.get('composite_score')) for r in results[:5]]}",
    )
    return {
        "ok": True,
        "cached": True,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "generated_at": data.get("generated_at"),
            "config": data.get("config"),
            "total_tested": data.get("total_tested"),
            "results": results[:top],
        },
    }


# ---------------------------------------------------------------------------
# 后台长任务：枚举回测 / 样本外验证 / 全市场扫描。
# 页面点击“重新计算”触发后台线程，前端轮询 /api/job 获取进度与结果，
# 保证页面展示的是真实工具实时算出的结果，而非仓库里冻结的静态 JSON。
# ---------------------------------------------------------------------------
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def _new_job(kind: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "id": job_id,
            "kind": kind,
            "status": "running",
            "progress": 0.0,
            "message": "排队中",
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "result": None,
            "error": None,
        }
    return job_id


def _job_update(job_id: str, **fields) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job:
            job.update(fields)


def _start_job(kind: str, fn) -> str:
    job_id = _new_job(kind)

    def _runner():
        try:
            result = fn(job_id)
            _job_update(job_id, status="done", progress=1.0, message="完成", result=result)
        except Exception as exc:  # noqa: BLE001
            _job_update(job_id, status="error", message=str(exc)[:300], error=str(exc))

    threading.Thread(target=_runner, name=f"job-{job_id}", daemon=True).start()
    return job_id


def api_job(params=None):
    job_id = ((params or {}).get("id") or [""])[0]
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        raise ValueError(f"未知任务: {job_id}")
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "id": job["id"],
            "kind": job["kind"],
            "status": job["status"],
            "progress": job["progress"],
            "message": job["message"],
            "started_at": job["started_at"],
            "result": job["result"],
            "error": job["error"],
        },
    }


def _job_int_param(params, key, default, lo, hi, label):
    raw = (params.get(key, [str(default)])[0] or str(default)).strip()
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        raise ValueError(f"{label} 需为整数")
    if not lo <= value <= hi:
        raise ValueError(f"{label} 需在 {lo}-{hi} 之间")
    return value


def _running_job_id(kind: str) -> str | None:
    """返回同类型正在运行的任务 id（避免重复触发多个并行长任务）。"""
    with _JOBS_LOCK:
        for job in _JOBS.values():
            if job["kind"] == kind and job["status"] == "running":
                return job["id"]
    return None


def api_enum_recalc(params=None):
    """触发枚举回测重算（后台线程运行 enumerate_pool_backtest.py，含 live DSR）。"""
    params = params or {}
    universe = (params.get("universe", ["veteran"])[0] or "veteran").strip()
    if universe not in {"full", "veteran"}:
        raise ValueError("universe 需为 full 或 veteran")
    min_n = _job_int_param(params, "min", 3, 2, 6, "最小组合数")
    max_n = _job_int_param(params, "max", 5, 2, 6, "最大组合数")
    if min_n > max_n:
        raise ValueError("min 不能大于 max")
    momentum = (params.get("momentum", ["25"])[0] or "25").strip()
    top = _job_int_param(params, "top", 30, 1, 200, "top")
    switch_buffer = float((params.get("switch-buffer", ["1.0"])[0] or "1.0").strip())
    if switch_buffer < 1.0:
        raise ValueError("switch-buffer 需 >= 1.0")

    existing = _running_job_id("enum")
    if existing:
        return {
            "ok": True,
            "cached": False,
            "stale": False,
            "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "data": {
                "job_id": existing,
                "status": "running",
                "reused": True,
                "note": "已有枚举任务运行中，复用该任务",
            },
        }

    # 候选池来源：--codes 优先；否则从 MySQL momentum_pools 按 pool_key 读取；
    # 都找不到时回退内置列表（脚本会打印弃用告警）。
    signal_svc._reload_momentum_pools_from_db()
    codes_param = (params.get("codes", [""])[0] or "").strip()
    resolved_codes = None
    if codes_param:
        resolved_codes = [c.strip() for c in codes_param.split(",") if c.strip()]
        if not resolved_codes or not CODE_RE.match(",".join(resolved_codes)):
            raise ValueError("codes 参数非法（需 6 位代码，逗号分隔）")
    else:
        pool_entry = signal_svc.SIGNAL_POOLS.get(universe)
        if pool_entry and pool_entry.get("codes"):
            resolved_codes = pool_entry["codes"]
    if resolved_codes:
        _log(f"ENUM 候选池来自 MySQL momentum_pools[{universe}]: {','.join(resolved_codes)}")
    else:
        _log(f"ENUM 未在 momentum_pools 找到池 {universe}，回退内置列表（请先生成预设池）", "WARN")

    def job(job_id):
        args = [
            "enumerate_pool_backtest.py",
            "--min", str(min_n),
            "--max", str(max_n),
            "--momentum", momentum,
            "--top", str(top),
            "--switch-buffer", str(switch_buffer),
            "--json",
        ]
        if resolved_codes:
            args += ["--codes", ",".join(resolved_codes)]
        else:
            args += ["--universe", universe]
        _job_update(job_id, message="枚举启动中…")
        _log(f"ENUM 开始: {' '.join(args)}")
        import re as _re
        job_started = time.time()
        last_logged = [0]

        def on_line(line):
            match = _re.search(r"\[(\d+)/(\d+)\]", line)
            if match:
                done, total = int(match.group(1)), int(match.group(2))
                pct = done / total if total else 0.0
                _job_update(
                    job_id,
                    progress=pct,
                    message=f"枚举中 {done}/{total}",
                )
                if done - last_logged[0] >= 10 or done >= total:
                    last_logged[0] = done
                    _log(f"ENUM 进度 {done}/{total} ({pct * 100:.0f}%)")

        stdout = run_script_stream(
            args, timeout=3600, offline=True, on_line=on_line
        )
        data = parse_json_output(stdout)
        # 写入可再生成的缓存，供 /api/enum 优先读取（不再依赖仓库内冻结快照）。
        live_path = DATA_DIR / "cache" / "enum_backtest_latest.json"
        try:
            live_path.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            _log(f"枚举结果写入缓存失败: {exc}", "WARN")
        # 持久化到 MySQL：任务完成即写入，页面刷新后从 /api/enum 也能取到最新结果。
        try:
            norm_results = _normalize_enum_results(data)
            db.upsert_backtest_result(
                "enum",
                str(data.get("config") or "default"),
                {
                    "generated_at": data.get("generated_at"),
                    "total_combos": data.get("total_combos"),
                    "file": live_path.name,
                },
                {
                    "total_combos": data.get("total_combos"),
                    "valid_results": data.get("valid_results"),
                    "top5": [
                        (r.get("combo"), r.get("ann"))
                        for r in norm_results[:5]
                    ],
                },
                {"generated_at": data.get("generated_at"), "results": norm_results},
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"DB 枚举结果写入失败: {exc}", "WARN")
        _job_update(
            job_id,
            message=f"枚举完成，共 {data.get('valid_results')} 组有效回测",
        )
        _log(
            f"ENUM 完成: 有效 {data.get('valid_results')} 组 "
            f"耗时 {time.time() - job_started:.0f}s"
        )
        out = dict(data)
        out["results"] = _normalize_enum_results(data)
        return out

    job_id = _start_job("enum", job)
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"job_id": job_id, "status": "running"},
    }


def api_walk_forward(params=None):
    """触发样本外验证（后台线程直接调用 walk_forward.run_walk_forward）。"""
    params = params or {}
    enum_source = (params.get("enum", [""])[0] or "").strip()
    top = _job_int_param(params, "top", 20, 1, 100, "候选数")
    train_months = _job_int_param(params, "train-months", 24, 3, 60, "训练段月数")
    test_months = _job_int_param(params, "test-months", 12, 3, 36, "测试段月数")
    step_months = _job_int_param(params, "step-months", 12, 1, 36, "滚动步长月数")
    metric = (params.get("metric", ["sharpe"])[0] or "sharpe").strip()
    if metric not in {"sharpe", "annual_return_pct", "sortino", "calmar"}:
        raise ValueError("metric 需为 sharpe/annual_return_pct/sortino/calmar")

    existing = _running_job_id("walk_forward")
    if existing:
        return {
            "ok": True,
            "cached": False,
            "stale": False,
            "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "data": {
                "job_id": existing,
                "status": "running",
                "reused": True,
                "note": "已有样本外验证任务运行中，复用该任务",
            },
        }

    def job(job_id):
        from tools.walk_forward import _candidates_from_enum, run_walk_forward

        if enum_source:
            payload = _read_json_file(Path(enum_source), {})
        else:
            # 与 /api/enum 一致：优先使用最近一次重新枚举的结果（缓存/MySQL），
            # 而不是仓库里的旧快照，保证样本外验证与页面枚举展示同源。
            payload, _ = _latest_enum_data()
        results = payload.get("results") or (payload if isinstance(payload, list) else [])
        candidates = _candidates_from_enum(results, top)
        if not candidates:
            raise ValueError("枚举结果里没有可解析的候选组合（缺少 combo/momentum 字段）")
        _job_update(job_id, message=f"样本外验证 top-{len(candidates)} 进行中")
        _log(
            f"WF 开始: top={len(candidates)} train={train_months}月 "
            f"test={test_months}月 step={step_months}月 metric={metric}"
        )
        wf_started = time.time()
        out = run_walk_forward(
            candidates,
            train_months=train_months,
            test_months=test_months,
            step_months=step_months,
            select_metric=metric,
        )
        if "数据不足以生成至少 2 个 walk-forward 折" in (out.get("error") or ""):
            # 候选池含次新 ETF 时，请求的 train/test 窗口可能超出数据跨度：
            # 逐级缩小窗口重试，保证样本外验证仍能产出 ≥2 折，并记录实际生效窗口。
            _log(
                f"WF 数据跨度不足（请求 train={train_months}月 test={test_months}月），"
                f"尝试自动缩窗"
            )
            for tr, te, st in (
                (18, 9, 9), (12, 6, 6), (9, 6, 6), (6, 6, 6), (6, 3, 3),
            ):
                if tr > train_months:
                    continue
                alt = run_walk_forward(
                    candidates,
                    train_months=tr,
                    test_months=te,
                    step_months=st,
                    select_metric=metric,
                )
                if not alt.get("error"):
                    alt["auto_shrunk_windows"] = {
                        "train_months": tr,
                        "test_months": te,
                        "step_months": st,
                    }
                    _log(
                        f"WF 自动缩窗成功: train={tr}月 test={te}月 step={st}月 "
                        f"({alt.get('n_folds')} 折)"
                    )
                    out = alt
                    break
        if out.get("error"):
            raise ValueError(out["error"])
        # 持久化最近一次样本外验证结果，供信号扫描/总览等页面读取。
        live_path = DATA_DIR / "cache" / "walk_forward_latest.json"
        try:
            live_path.write_text(
                json.dumps(
                    {
                        "generated_at": datetime.now()
                        .astimezone()
                        .isoformat(timespec="seconds"),
                        **out,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            _log(f"样本外结果写入缓存失败: {exc}", "WARN")
        # 持久化到 MySQL：任务完成即写入，刷新/重启后也能从 /api/walk-forward/latest 取到。
        try:
            follow = out.get("follow_strategy") or {}
            db.upsert_backtest_result(
                "walk_forward",
                "latest",
                {
                    "generated_at": datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds"),
                    "windows": out.get("auto_shrunk_windows") or {
                        "train_months": train_months,
                        "test_months": test_months,
                        "step_months": step_months,
                    },
                },
                {
                    "n_folds": out.get("n_folds"),
                    "oos_total_pct": follow.get("oos_total_pct"),
                    "benchmark_total_pct": follow.get("benchmark_total_pct"),
                    "excess_pct": follow.get("excess_pct"),
                },
                out,
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"DB 样本外结果写入失败: {exc}", "WARN")
        follow = out.get("follow_strategy") or {}
        _log(
            f"WF 完成: {out.get('n_folds')} 折 跟随样本外 "
            f"{follow.get('oos_total_pct')}% vs 基准 {follow.get('benchmark_total_pct')}% "
            f"超额 {follow.get('excess_pct')}% 耗时 {time.time() - wf_started:.0f}s"
        )
        return out

    job_id = _start_job("walk_forward", job)
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"job_id": job_id, "status": "running"},
    }


def api_walk_forward_latest(params=None):
    """返回最近一次样本外验证结果（未运行过则为 null）。"""
    data = _read_json_file(DATA_DIR / "cache" / "walk_forward_latest.json", None)
    if data is None or not data.get("candidates"):
        db_row = db.latest_backtest_result("walk_forward")
        if db_row and (db_row.get("payload") or {}).get("candidates"):
            data = db_row["payload"]
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": data,
    }


def api_etf_scan_recalc(params=None):
    """触发全市场扫描重算（后台线程运行 etf_full_backtest.py，结果写入 data/cache）。"""
    params = params or {}
    category = (params.get("category", [""])[0] or "").strip()
    top = _job_int_param(params, "top", 100, 1, 640, "top")
    min_days = _job_int_param(params, "min-days", 500, 100, 3000, "最少K线天数")
    min_size = float((params.get("min-size", ["1000000000"])[0] or "1000000000").strip())
    min_turnover = float((params.get("min-turnover", ["1"])[0] or "1").strip())
    workers = _job_int_param(params, "workers", 4, 1, 12, "并行抓取线程数")
    fast = (params.get("fast", ["1"])[0] or "1") == "1"
    source = (params.get("source", ["auto"])[0] or "auto").strip()
    if source not in {"auto", "eastmoney", "tencent", "sina"}:
        raise ValueError("source 需为 auto/eastmoney/tencent/sina")
    out_file = DATA_DIR / "cache" / "etf_backtest_results.json"

    existing = _running_job_id("etf_scan")
    if existing:
        return {
            "ok": True,
            "cached": False,
            "stale": False,
            "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "data": {
                "job_id": existing,
                "status": "running",
                "reused": True,
                "note": "已有全市场扫描任务运行中，复用该任务",
            },
        }

    def job(job_id):
        args = [
            "etf_full_backtest.py",
            "--min-days", str(min_days),
            "--min-size", str(min_size),
            "--min-turnover", str(min_turnover),
            "--top", str(top),
            "--workers", str(workers),
            "--output", str(out_file),
        ]
        if fast:
            args += ["--fast"]
        if source != "auto":
            args += ["--source", source]
        if category:
            args += ["--category", category]
        _job_update(job_id, message="全市场扫描中…")
        _log(f"ETF-SCAN 开始: {' '.join(args)}")
        scan_started = time.time()
        import re as _re

        def on_line(line):
            match = _re.search(r"\[(\d+)/(\d+)\]", line)
            if match:
                done, total = int(match.group(1)), int(match.group(2))
                pct = done / total if total else 0.0
                _job_update(
                    job_id,
                    progress=pct,
                    message=f"扫描中 {done}/{total}",
                )
                if done % 25 == 0 or done >= total:
                    _log(f"ETF-SCAN 进度 {done}/{total} ({pct * 100:.0f}%)")

        # 全市场扫描本身就是"刷新数据"动作：必须联网，把最新 K 线写入缓存，
        # 否则后续枚举/回测在离线缓存里找不到这些新标的。
        run_script_stream(args, timeout=7200, offline=False, on_line=on_line)
        data = _read_json_file(out_file, {})
        if not data.get("results"):
            raise ValueError("全市场扫描未产出有效结果")
        _job_update(job_id, message=f"扫描完成，共 {data.get('total_tested')} 只")
        _log(
            f"ETF-SCAN 完成: 测试 {data.get('total_tested')} 只 "
            f"耗时 {time.time() - scan_started:.0f}s"
        )
        return data

    job_id = _start_job("etf_scan", job)
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"job_id": job_id, "status": "running"},
    }


def api_preset_pool_build(params=None):
    """选品池 → 预设池：后台运行 build_preset_pool.py 生成候选，供页面预览/确认。"""
    params = params or {}
    target_size = _job_int_param(params, "target-size", 10, 5, 20, "预设池规模")
    min_score = float((params.get("min-score", ["30"])[0] or "30").strip())
    min_vol = float((params.get("min-vol", ["0.12"])[0] or "0.12").strip())
    max_vol = float((params.get("max-vol", ["0.45"])[0] or "0.45").strip())
    max_corr = float((params.get("max-corr", ["0.85"])[0] or "0.85").strip())
    min_bars = _job_int_param(params, "min-bars", 0, 0, 3000, "最少历史K线条数")
    min_size = float((params.get("min-size", ["100000000"])[0] or "100000000").strip())
    min_turnover = float((params.get("min-turnover", ["0"])[0] or "0").strip())
    scan_source = DATA_DIR / "cache" / "etf_backtest_results.json"
    if not scan_source.exists():
        scan_source = DATA_DIR / "etf_backtest_results.json"

    def job(job_id):
        # 先跑四维选品（离线缓存），把四维分/相关性矩阵缓存下来，合并进预设池生成。
        screener_path = DATA_DIR / "cache" / "screener_latest.json"
        try:
            stdout = run_script(["etf_screener.py", "--json"], timeout=300, offline=True)
            screener_payload = parse_json_output(stdout)
            screener_path.write_text(
                json.dumps(screener_payload, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:
            _log(f"四维选品获取失败（继续生成预设池）: {exc}", "WARN")
        args = [
            "build_preset_pool.py",
            "--source", str(scan_source),
            "--target-size", str(target_size),
            "--min-score", str(min_score),
            "--min-vol", str(min_vol),
            "--max-vol", str(max_vol),
            "--min-size", str(min_size),
            "--max-corr", str(max_corr),
            "--min-bars", str(min_bars),
            "--min-turnover", str(min_turnover),
            "--screener-json", str(screener_path),
            "--json",
        ]
        _job_update(job_id, message="正在从全市场评分生成预设池…")
        _log(f"PRESET-POOL 开始: {' '.join(args)}")
        job_started = time.time()
        stdout = run_script(args, timeout=300, offline=True)
        data = parse_json_output(stdout)
        live_path = DATA_DIR / "cache" / "preset_pool_latest.json"
        try:
            live_path.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            _log(f"预设池候选写入缓存失败: {exc}", "WARN")
        count = len(data.get("candidates") or [])
        _job_update(job_id, message=f"已选出 {count} 只候选")
        _log(
            f"PRESET-POOL 完成: 候选 {count} 只 "
            f"耗时 {time.time() - job_started:.0f}s"
        )
        return data

    job_id = _start_job("preset_pool", job)
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"job_id": job_id, "status": "running"},
    }


def api_preset_pool(params=None):
    """当前预设池（MySQL momentum_pools）+ 最近一次生成候选 + 全市场扫描元信息。"""
    signal_svc._reload_momentum_pools_from_db()
    rows = db.load_momentum_pools() or []
    names = _etf_name_map()
    presets = []
    for row in rows:
        if row.get("pool_type") != "preset":
            continue
        codes = [c for c in str(row.get("codes") or "").split(",") if c]
        presets.append({
            "pool_key": row.get("pool_key"),
            "pool_type": row.get("pool_type"),
            "description": row.get("description"),
            "codes": codes,
            "names": [names.get(c, c) for c in codes],
            "defensive_code": row.get("defensive_code"),
            "enabled": bool(row.get("enabled")),
        })
    latest = _read_json_file(DATA_DIR / "cache" / "preset_pool_latest.json", None)
    scan = _read_json_file(DATA_DIR / "cache" / "etf_backtest_results.json", None)
    if scan is None or not scan.get("results"):
        scan = _read_json_file(DATA_DIR / "etf_backtest_results.json", {})
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "presets": presets,
            "latest": latest,
            "scan": {
                "generated_at": scan.get("generated_at"),
                "total_tested": scan.get("total_tested"),
            },
        },
    }


def _api_preset_pool_apply(body):
    """把候选确认保存为 MySQL momentum_pools 里的预设池（pool_type=preset）。"""
    pool_key = (body.get("pool_key") or "").strip()
    codes = [str(c).strip() for c in (body.get("codes") or []) if str(c).strip()]
    description = (body.get("description") or "").strip()[:255]
    defensive_code = (body.get("defensive_code") or "").strip() or None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", pool_key):
        raise ValueError("pool_key 需为字母/数字/下划线/连字符（1-64 位）")
    if not 3 <= len(codes) <= 12:
        raise ValueError("预设池标的数量需在 3-12 之间")
    if not CODE_RE.match(",".join(codes)):
        raise ValueError("存在非法代码（需 6 位数字）")
    description = description or f"预设池 {pool_key}（{len(codes)} 只）"
    db.save_momentum_pools([{
        "pool_key": pool_key,
        "pool_type": "preset",
        "description": description,
        "codes": ",".join(codes),
        "defensive_code": defensive_code,
        "is_recommended": 0,
        "sort_order": 0,
        "enabled": 1,
    }])
    signal_svc._reload_momentum_pools_from_db(force=True)
    try:
        db.upsert_backtest_result(
            "preset_pool",
            pool_key,
            {
                "pool_key": pool_key,
                "codes": codes,
                "description": description,
                "defensive_code": defensive_code,
            },
            {"count": len(codes), "codes": codes},
            {
                "pool_key": pool_key,
                "codes": codes,
                "description": description,
                "defensive_code": defensive_code,
            },
        )
    except Exception as exc:  # noqa: BLE001 - 历史落库失败不影响主流程
        _log(f"预设池历史写入失败: {exc}", "WARN")
    _biz("PRESET-POOL", f"已保存预设池 {pool_key}: {','.join(codes)}")
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "pool_key": pool_key,
            "codes": codes,
            "description": description,
        },
    }


ROUTES = {
    "/api/pools": api_pools,
    "/api/signals": api_signals,
    "/api/overview": api_overview,
    "/api/backtest": api_backtest,
    "/api/backtest/history": api_backtest_history,
    "/api/audit": api_audit,
    "/api/screener": api_screener,
    "/api/positions": api_positions,
    "/api/kline": api_kline,
    "/api/stoploss": api_stoploss,
    "/api/realtime": api_realtime,
    "/api/enum": api_enum,
    "/api/etf-scan": api_etf_scan,
    "/api/job": api_job,
    "/api/enum/recalc": api_enum_recalc,
    "/api/walk-forward": api_walk_forward,
    "/api/walk-forward/latest": api_walk_forward_latest,
    "/api/etf-scan/recalc": api_etf_scan_recalc,
    "/api/preset-pool": api_preset_pool,
    "/api/preset-pool/build": api_preset_pool_build,
    "/api/models": lambda params: _api_models(),
    "/api/db/stats": lambda params: _api_db_stats(params),
    "/api/db/tables": lambda params: _api_db_tables(params),
    "/api/db/table": lambda params: _api_db_table(params),
    "/api/logs": lambda params: _api_logs(params),
    "/api/scheduler": lambda params: _api_scheduler(params),
    "/api/grid": lambda params: api_grid(params),
    "/api/grid/optimize": lambda params: api_grid_optimize(params),
    "/api/grid/configs": lambda params: api_grid_configs(params),
    "/api/grid/positions": lambda params: api_grid_positions(params),
    "/api/grid/screener": lambda params: api_grid_screener(params),
    "/api/grid/triggers/list": lambda params: api_grid_triggers_list(params),
}

MAX_BODY_BYTES = 12 * 1024 * 1024

_SCHEDULER = None


def _api_scheduler(params=None):
    if _SCHEDULER is None:
        return {
            "ok": True,
            "cached": False,
            "stale": False,
            "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "data": {"enabled": False, "reason": "未启用（--no-scheduler）"},
        }
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": _SCHEDULER.status(),
    }


def _api_scheduler_run(body=None):
    if _SCHEDULER is None:
        raise ValueError("调度器未启用（--no-scheduler）")
    started = time.time()
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    result = "error"
    detail = {}
    try:
        run_value = _SCHEDULER.run_now()
        result = "ok"
        detail = {"result": str(run_value)}
    except Exception as exc:
        detail = {"error": str(exc)}
        _log(f"SCHED 手动执行失败: {exc}", "ERROR")
        raise
    finally:
        try:
            db.append_scheduler_run(
                "manual",
                started_at,
                datetime.now().astimezone().isoformat(timespec="seconds"),
                int((time.time() - started) * 1000),
                result,
                detail,
                None,
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"DB 调度记录写入失败: {exc}", "WARN")
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"result": result, **_SCHEDULER.status()},
    }


def _int_param(params, key, default, lo, hi, label):
    raw = (params.get(key, [str(default)])[0] or str(default)).strip()
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        raise ValueError(f"{label} 需为整数")
    if not lo <= value <= hi:
        raise ValueError(f"{label} 需在 {lo}-{hi} 之间")
    return value


def api_grid_positions(params=None):
    """实时网格持仓详情：直接从数据库 holdings_current + 实时行情计算（不走 5 分钟缓存）。"""
    from tools import grid_trading as gt
    configs = gt.CONFIGS
    holdings = position_svc.latest_positions()  # 权威来源：数据库
    holdings_map = {
        str(h.get("code") or ""): h
        for h in (holdings.get("holdings") or [])
    }
    quotes = fetch_realtime_quotes(list(configs.keys()))
    positions = grid_svc._build_grid_positions(configs, holdings_map, quotes)
    held = sum(1 for p in positions if p.get("total_shares"))
    total_mv = sum(p.get("market_value") or 0 for p in positions)
    total_pnl = sum(p.get("pnl") or 0 for p in positions)
    _biz(
        "GRID-POSITION",
        f"实时持仓 {held} 只 市值={total_mv:.2f} 盈亏={total_pnl:+.2f}",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
            "positions": positions,
        },
    }


def api_grid(params=None):
    """网格策略总览：标的趋势分析 + 触发价格 + 触发记录。"""
    refresh = params.get("refresh", ["0"])[0] == "1" if params else False
    key = "grid|overview"

    def _normalize_db_triggers(rows):
        """把 grid_triggers 表记录转成 grid_trading 期望的触发记录格式。"""
        out = []
        for row in rows or []:
            trigger_date = str(row.get("trigger_date") or "")
            out.append({
                "date": trigger_date[:10],
                "time": trigger_date[11:19] if len(trigger_date) > 10 else "",
                "action": row.get("action"),
                "type": row.get("trigger_type") or "grid",
                "price": row.get("price"),
                "shares": row.get("shares"),
                "amount": row.get("amount"),
                "base_price_before": row.get("base_price_before"),
                "base_price_after": row.get("base_price_after"),
                "name": row.get("name"),
                "source": row.get("source"),
            })
        return out

    def _triggers_for(code):
        """从 MySQL grid_triggers 表读取（权威持久化，本地文件不再使用）。"""
        try:
            rows = db.grid_triggers_for_code(code)
        except Exception:
            _log(f"GRID-TRIGGERS 表读取失败，按无触发处理: {code}", "WARN")
            return []
        return _normalize_db_triggers(rows)

    def producer():
        from tools import grid_trading as gt

        configs = gt.CONFIGS
        codes = list(configs.keys())
        all_history = {code: _triggers_for(code) for code in codes}
        quotes = fetch_realtime_quotes(codes)
        items = []
        for code in codes:
            cfg = configs[code]
            try:
                trend = gt.analyze_trend(code)
            except Exception as exc:  # noqa: BLE001 - 单项失败不影响整体
                trend = {
                    "code": code,
                    "name": cfg.get("name", code),
                    "status": "unknown",
                    "error": str(exc)[:160],
                }
            history = all_history.get(code) or []
            # 用触发记录推导动态基准价；无触发时回退静态配置价
            try:
                base = float(gt.get_dynamic_bp(cfg, history))
            except Exception as exc:
                _log(f"GRID 动态基准价计算失败，回退静态配置: {code} ({exc})", "WARN")
                base = _num(cfg.get("base_price"))
            up_pct = _num(
                cfg.get("grid_spacing_up_pct") or cfg.get("grid_spacing_pct") or 0
            )
            down_pct = _num(
                cfg.get("grid_spacing_down_pct") or cfg.get("grid_spacing_pct") or 0
            )
            quote = (quotes or {}).get(code) or {}
            last_trigger = history[-1] if history else None
            # 多维度评分（参考网格信号）：趋势/网格适配/动量RSRS/技术指标/触发信号
            scores = {"trend": trend.get("score")}
            try:
                tech = _single_code_technicals(code)
                scores.update({
                    "grid": _single_grid_score(code, tech),
                    "vol20": tech.get("vol20"),
                    "rsi": tech.get("rsi"),
                    "trend20": tech.get("trend20"),
                    "bb_width": tech.get("bb_width") or trend.get("bb_width"),
                })
            except Exception as exc:
                _log(f"GRID 技术指标计算失败: {code} ({exc})", "WARN")
            try:
                from tools.momentum_signal import scan as _momentum_scan
                sig_items = _momentum_scan(
                    {code: cfg.get("name", code)}, 25
                ).get("items") or []
                if sig_items:
                    scores["rsrs"] = round(
                        float(sig_items[0].get("raw_rsrs_score") or 0), 3
                    )
                    scores["momentum"] = sig_items[0].get("signal_strength")
            except Exception as exc:
                _log(f"GRID RSRS/动量评分失败: {code} ({exc})", "WARN")
            try:
                ta = grid_svc._grid_trigger_analysis(
                    code, current_price=quote.get("price")
                )
                scores["trigger"] = {
                    "count": ta.get("count"),
                    "freq": ta.get("freq_per_day"),
                    "chain": ta.get("recent_chain"),
                    "verdict": ta.get("verdict"),
                }
            except Exception as exc:
                _log(f"GRID 触发分析失败: {code} ({exc})", "WARN")
            grid_cfg = None
            try:
                grid_cfg = db.get_grid_config(code)
            except Exception as exc:
                _log(f"GRID 配置读取失败: {code} ({exc})", "WARN")
                grid_cfg = None
            items.append({
                "code": code,
                "name": cfg.get("name", code),
                "base_price": round(base, 3) if base is not None else None,
                "spacing_up_pct": up_pct,
                "spacing_down_pct": down_pct,
                "buy_price": (
                    round(base * (1 - down_pct / 100), 3)
                    if base is not None and down_pct
                    else None
                ),
                "sell_price": (
                    round(base * (1 + up_pct / 100), 3)
                    if base is not None and up_pct
                    else None
                ),
                "current_price": quote.get("price"),
                "change_pct": quote.get("change_pct"),
                "score": trend.get("score"),
                "status": trend.get("status"),
                "bb_width": trend.get("bb_width"),
                "ma_state": trend.get("ma_state"),
                "verdict": trend.get("verdict"),
                "error": trend.get("error"),
                "trigger_count": len(history),
                "last_trigger": last_trigger,
                "scores": scores,
                "config": (
                    {
                        "strategy_type": grid_cfg.get("strategy_type"),
                        "price_low": grid_cfg.get("price_low"),
                        "price_high": grid_cfg.get("price_high"),
                        "order_type_sell": grid_cfg.get("order_type_sell"),
                        "order_type_buy": grid_cfg.get("order_type_buy"),
                        "shares_per_grid": grid_cfg.get("shares_per_grid"),
                        "base_position": grid_cfg.get("base_position"),
                        "max_position": grid_cfg.get("max_position"),
                    }
                    if grid_cfg
                    else None
                ),
            })

        def _ok(item):
            return item.get("status") == "ok" and item.get("score") is not None

        groups = {
            "stop": [i for i in items if _ok(i) and i["score"] <= -4],
            "caution": [i for i in items if _ok(i) and -3 <= i["score"] <= -2],
            "ok": [i for i in items if _ok(i) and i["score"] >= -1],
            "unknown": [i for i in items if not _ok(i)],
        }
        recent_triggers = []
        for code, history in all_history.items():
            for record in history:
                recent_triggers.append({
                    **record,
                    "code": code,
                    "name": configs.get(code, {}).get("name", code),
                })
        recent_triggers.sort(
            key=lambda r: str(r.get("date", "")), reverse=True
        )
        holdings_map = {
            str(h.get("code") or ""): h
            for h in (position_svc.latest_positions().get("holdings") or [])
        }
        positions = grid_svc._build_grid_positions(configs, holdings_map, quotes)
        return {
            "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
            "codes": codes,
            "items": items,
            "groups": {k: [i["code"] for i in v] for k, v in groups.items()},
            "recent_triggers": recent_triggers[:40],
            "positions": positions,
        }

    payload, _, _ = cached(key, 300, refresh, producer)
    data = payload.get("data") or {}
    groups = data.get("groups") or {}
    _biz(
        "GRID",
        f"标的 {len(data.get('items') or [])} 只 "
        f"stop={groups.get('stop', [])} caution={groups.get('caution', [])} "
        f"ok={len(groups.get('ok', []))} 最近触发 {len(data.get('recent_triggers') or [])} 条",
    )
    positions = data.get("positions") or []
    if positions:
        total_mv = sum(p.get("market_value") or 0 for p in positions)
        total_pnl = sum(p.get("pnl") or 0 for p in positions)
        _biz(
            "GRID-POSITION",
            f"网格持仓 {len(positions)} 只 市值={total_mv:.2f} "
            f"盈亏={total_pnl:+.2f} "
            f"top={[(p['code'], p['total_shares'], p.get('base_price')) for p in positions[:3]]}",
        )
    return payload


def api_grid_configs(params=None):
    """读取已保存的网格交易配置列表（grid_configs 表）。"""
    configs = db.load_grid_configs()
    for config in configs:
        try:
            last = db.latest_grid_opt_result(config.get("code") or "")
            if last and last.get("best"):
                best = last["best"]
                config["last_opt"] = {
                    key: best.get(key)
                    for key in (
                        "spacing", "levels", "shares", "grid_value",
                        "annual_return_pct", "max_dd_pct", "sharpe",
                    )
                }
                config["last_opt_at"] = last.get("updated_at")
        except Exception as exc:
            _log(
                f"GRID 配置寻优结果读取失败: {config.get('code')} ({exc})",
                "WARN",
            )
    _biz(
        "GRID-CONFIG-LIST",
        f"读取 {len(configs)} 条网格配置（含寻优结果 "
        f"{sum(1 for c in configs if c.get('last_opt'))} 条）",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"configs": configs},
    }


def api_grid_triggers_list(params=None):
    """按标的/日期区间查询网格触发记录（读 MySQL，可搜索）。"""
    params = params or {}
    code = (params.get("code", [""])[0] or "").strip() or None
    start = (params.get("start", [""])[0] or "").strip() or None
    end = (params.get("end", [""])[0] or "").strip() or None
    trigger_type = (params.get("trigger_type", [""])[0] or "").strip() or None
    try:
        limit = min(max(int(params.get("limit", ["500"])[0]), 1), 2000)
    except (TypeError, ValueError):
        limit = 500
    records = db.query_grid_triggers(code, start, end, trigger_type, limit)
    _biz(
        "GRID-TRIGGERS",
        f"查询 code={code or '全部'} type={trigger_type or '全部'} "
        f"{start or '…'}~{end or '…'} rows={len(records)}",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"records": records, "total": len(records)},
    }


def _num_list(params, key, default, lo, hi, label, integer=False):
    if default is None:
        values = params.get(key)
        if not values:
            return []
        raw = (values[0] or "").strip()
    else:
        raw = (params.get(key, [default])[0] or default).strip()
    parts = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
    if not parts and default is not None:
        parts = [p.strip() for p in default.split(",")]
    out = []
    for part in parts:
        try:
            value = float(part)
        except ValueError:
            raise ValueError(f"{label} 列表含非法值: {part}")
        if not lo <= value <= hi:
            raise ValueError(f"{label} 需在 {lo}-{hi} 之间")
        if integer:
            value = int(value)
        if value not in out:
            out.append(value)
    return out


def api_grid_optimize(params=None):
    """网格参数自动寻优：内置间距/层数/每格金额候选，多标的多线程并行扫描，
    每个标的输出最优配置（按年化收益）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    params = params or {}
    codes_raw = (params.get("codes", [""])[0] or "").strip()
    if codes_raw in ("", "all"):
        from tools import grid_trading as gt
        codes = list(gt.CONFIGS.keys())
    else:
        codes = [c.strip() for c in codes_raw.replace("，", ",").split(",") if c.strip()]
        if not codes:
            raise ValueError("请选择至少一个标的")
        for code in codes:
            if not CODE_RE.match(code):
                raise ValueError(f"非法证券代码: {code}")
        if len(codes) > 20:
            raise ValueError("标的最多 20 个")

    capital = _int_param(params, "capital", 100000, 1000, 10000000, "回测资金")
    start_raw = (params.get("start", ["2y"])[0] or "2y").strip()
    if start_raw == "full":
        start_arg = "2018-01-01"
    else:
        start_arg = _resolve_backtest_start(start_raw)
    # 可选覆盖（不传则用内置候选，按每格金额自动折算股数）
    spacings = _num_list(params, "spacings", None, 0.1, 20, "间距") or grid_svc.GRID_OPT_SPACINGS
    levels = _num_list(params, "levels", None, 1, 50, "层数", integer=True) or grid_svc.GRID_OPT_LEVELS
    explicit_shares = _num_list(params, "shares", None, 100, 1000000, "每格股数", integer=True)
    refresh = params.get("refresh", ["0"])[0] == "1" if params else False
    # 寻优前先刷新目标标的 K 线缓存（收盘后自动补拉当日 bar；refresh=1 强制联网）
    _refresh_grid_kline_cache(codes, force=refresh)

    def scan_code(code):
        from tools import grid_trading as gt

        cfg = gt.CONFIGS.get(code) or {}
        # 实时价：既用于基准价兜底（未在 CONFIGS 的标的），也用于资金容量
        current_price = None
        try:
            quotes = fetch_realtime_quotes([code])
            current_price = _num((quotes or {}).get(code, {}).get("price"))
        except Exception as exc:
            _log(f"GRID-OPT 实时价获取失败，回退配置基准: {code} ({exc})", "WARN")
            current_price = None
        base = _num(cfg.get("base_price"))
        if base is None:
            base = current_price  # 选品池候选（无 CONFIGS 配置）用实时价当基准
        if base is None:
            # 兜底：用刚刷新的 K 线最新收盘价（实时行情被限流时）
            try:
                from tools.etf_market_data import load_etf_series
                saved_offline = os.environ.get("ETF_DATA_OFFLINE")
                os.environ["ETF_DATA_OFFLINE"] = "1"
                try:
                    series = load_etf_series(code, count=1500)
                    if series.bars:
                        base = _num(series.bars[-1]["close"])
                finally:
                    if saved_offline is not None:
                        os.environ["ETF_DATA_OFFLINE"] = saved_offline
                    else:
                        os.environ.pop("ETF_DATA_OFFLINE", None)
            except Exception as exc:
                _log(f"GRID-OPT K线刷新失败，基准缺失: {code} ({exc})", "WARN")
                base = None
        shares_candidates = explicit_shares or grid_svc._grid_shares_candidates(base)
        kline_end = _kline_end_date(code)
        # ── 当前网格已有持仓（数据库 holdings_current）──
        holding = {}
        try:
            holdings = db.load_holdings_current() or {}
            holding = next(
                (
                    h for h in (holdings.get("holdings") or [])
                    if h.get("code") == code
                ),
                {},
            )
        except Exception as exc:
            _log(f"GRID-OPT 持仓读取失败，按无持仓处理: {code} ({exc})", "WARN")
            holding = {}
        total_shares = int(holding.get("shares") or 0)
        db_base = int(holding.get("base_shares") or 0)
        strategy = str(holding.get("strategy") or "")
        if strategy == "底仓":
            position_min = total_shares
        elif strategy in ("网格", "共用") and db_base > 0:
            position_min = db_base
        else:
            position_min = int(cfg.get("base_position") or 0)
        # 资金容量：capital 能买的总股数（100 股取整）
        capacity_shares = 10 ** 9
        if current_price and current_price > 0:
            capacity_shares = int(capital / current_price / 100) * 100
        code_results = []
        for spacing in spacings:
            for level in levels:
                for share in shares_candidates:
                    key = (
                        f"grid-opt|{code}|{start_arg}|{capital}"
                        f"|{spacing}|{level}|{share}|{kline_end}"
                    )

                    def producer(code=code, spacing=spacing, level=level, share=share):
                        args = [
                            "grid_trading.py", "backtest", code,
                            "--capital", str(capital),
                            "--spacing", str(spacing),
                            "--levels", str(level),
                            "--shares", str(share),
                            "--json",
                        ]
                        if start_arg:
                            args += ["--start", start_arg]
                        stdout = run_script(args, timeout=120, offline=True)
                        return parse_json_output(stdout)

                    payload, _, _ = cached(key, 86400, refresh, producer)
                    data = payload.get("data") or {}
                    perf_all = data.get("performance") or {}
                    perf = perf_all.get("grid") or {}
                    # 基准对比：买入持有（同费率建仓）。判断网格是否真的创造超额，
                    # 避免寻优选出「网格年化最高但跑输买入持有」的配置。
                    bh = perf_all.get("buy_and_hold") or {}
                    alpha_pct = perf_all.get("alpha_pct")
                    meta = data.get("meta") or {}
                    # 持仓范围：下限=当前底仓，上限=底仓+买入层×每格（不超过资金容量）
                    position_max = min(
                        position_min + level * share, capacity_shares
                    )
                    grid_config = grid_svc._grid_config_summary(code, meta, base=base)
                    grid_config["position_range"] = {
                        "min": position_min,
                        "max": position_max,
                    }
                    code_results.append({
                        "code": code,
                        "name": meta.get("name", code),
                        "spacing": spacing,
                        "levels": level,
                        "shares": share,
                        "grid_value": round(share * base, 0) if base else None,
                        "annual_return_pct": perf.get("annual_return_pct"),
                        "total_return_pct": perf.get("total_return_pct"),
                        "max_dd_pct": perf.get("max_dd_pct"),
                        "sharpe": perf.get("sharpe"),
                        "win_rate_pct": perf.get("win_rate_pct"),
                        "profit_factor": perf.get("profit_factor"),
                        "triggered_buy": perf.get("triggered_buy"),
                        "triggered_sell": perf.get("triggered_sell"),
                        "trades": len(data.get("trades") or []),
                        "final_equity": perf.get("final_equity"),
                        "grade": perf.get("grade"),
                        # 基准对比：买入持有年化/总收益 + 网格超额（α = 网格年化 − 买入持有年化）
                        "buy_and_hold_annual_pct": bh.get("annual_return_pct"),
                        "buy_and_hold_total_pct": bh.get("total_return_pct"),
                        "alpha_pct": alpha_pct,
                        "beat_benchmark": bool(alpha_pct is not None and alpha_pct > 0),
                        "position_range": {
                            "min": position_min,
                            "max": position_max,
                        },
                        "grid_config": grid_config,
                    })
        return code_results

    # 多标的多线程并行
    results = []
    total = len(codes) * len(spacings) * len(levels) * (
        len(explicit_shares) if explicit_shares else len(grid_svc.GRID_OPT_VALUES)
    )
    if total > 2000:
        raise ValueError(f"组合数 {total} 超过上限 2000，请缩小标的范围")
    _log(f"GRID-OPT 开始并行扫描: 标的 {len(codes)} × 间距{len(spacings)} × 层数{len(levels)} × 每格金额{len(grid_svc.GRID_OPT_VALUES) if not explicit_shares else len(explicit_shares)} = 约{total} 组")
    workers = min(12, len(codes))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(scan_code, code): code for code in codes}
        for future in as_completed(futures):
            code = futures[future]
            code_results = future.result()
            results.extend(code_results)
            _log(f"GRID-OPT {code} 完成 {len(code_results)} 组（并行度 {workers}）")

    results.sort(key=lambda r: (r["code"], -(r["annual_return_pct"] or 0)))
    # 每个标的最优：优先在「跑赢买入持有基准」的配置里选年化最高；
    # 若全部跑输基准，退回年化最高（即跑输最少），避免纯年化排序误荐跑输基准的配置。
    best_per_code = {}
    for code in codes:
        cands = [r for r in results if r["code"] == code]
        if not cands:
            continue
        beaters = [r for r in cands if r.get("beat_benchmark")]
        best_per_code[code] = max(
            beaters or cands,
            key=lambda r: r["annual_return_pct"]
            if r["annual_return_pct"] is not None else float("-inf"),
        )
    _biz(
        "GRID-OPT",
        f"标的 {len(codes)} 并行扫描完成（{total} 组合）| 各标的最优: "
        + "；".join(
            f"{code} {r['spacing']}%/{r['levels']}层/{r['shares']}股"
            f"（每格约{r['grid_value'] or '?'}元）年化{r['annual_return_pct'] or 0:.1f}%"
            f"(基准{r['buy_and_hold_annual_pct'] or 0:.1f}% α{r['alpha_pct'] or 0:+.1f}%)"
            for code, r in best_per_code.items()
        ),
    )
    try:
        db.upsert_backtest_result(
            "grid_opt",
            f"{','.join(codes)}|{capital}|{start_raw}",
            {"codes": codes, "capital": capital, "start": start_raw},
            {
                code: {
                    key: item.get(key)
                    for key in (
                        "spacing", "levels", "shares", "grid_value",
                        "annual_return_pct", "total_return_pct", "max_dd_pct",
                        "sharpe", "win_rate_pct", "trades",
                        "buy_and_hold_annual_pct", "alpha_pct", "beat_benchmark",
                    )
                }
                for code, item in best_per_code.items()
            },
            {"best_per_code": best_per_code, "results": results},
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"DB 网格寻优结果写入失败: {exc}", "WARN")
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "params": {
                "codes": codes,
                "capital": capital,
                "start": start_raw,
                "spacings": spacings,
                "levels": levels,
                "grid_values": grid_svc.GRID_OPT_VALUES if not explicit_shares else [],
                "shares": explicit_shares,
            },
            "best_per_code": best_per_code,
            "results": results,
        },
    }


def api_grid_screener(params=None):
    """全市场 ETF/LOF 网格选品池：多维度评分 + 筛选（10 分钟缓存）。

    基础四维（波动/规模/均值回归/回撤，T+0 加分，满分 21）
    + 补充评分（趋势评分/动量 RSRS/信号/触发记录），均可筛选。
    """
    category = (params.get("category", [""])[0] or "").strip()
    want_lof = params.get("lof", ["0"])[0] == "1"
    want_t0 = params.get("t0", [""])[0]
    min_size = _num((params.get("min_size", [""])[0] or "")) or 0
    min_score = _num((params.get("min_score", [""])[0] or "")) or 0
    min_vol = _num((params.get("min_vol", [""])[0] or ""))
    max_vol = _num((params.get("max_vol", [""])[0] or ""))
    min_amount = _num((params.get("min_amount", [""])[0] or "")) or 0
    max_trend = _num((params.get("max_trend", [""])[0] or ""))
    trend_min = _num((params.get("trend_min", [""])[0] or ""))
    min_amplitude = _num((params.get("min_amplitude", [""])[0] or "")) or 0
    max_dd = _num((params.get("max_dd", [""])[0] or ""))
    sort_key = (params.get("sort", ["score"])[0] or "score").strip()
    refresh = params.get("refresh", ["0"])[0] == "1" if params else False
    key = (
        "grid-screener|"
        + "|".join([
            category or "-", "lof" if want_lof else "etf", want_t0 or "-",
            str(min_size), str(min_score), str(min_vol or ""), str(max_vol or ""),
            str(min_amount), str(max_trend or ""), str(trend_min or ""),
            str(min_amplitude), str(max_dd or ""), sort_key,
        ])
    )

    def producer():
        meta = _read_json_file(DATA_DIR / "etf_meta.json", {})
        etfs = meta.get("etfs") or []
        codes = {str(e.get("code")) for e in etfs if e.get("code")}
        tech = _grid_screener_technicals(codes)

        rows = []
        for etf in etfs:
            code = str(etf.get("code") or "").strip()
            if not CODE_RE.match(code):
                continue
            if category and str(etf.get("category")) != category:
                continue
            is_lof = _is_lof(code)
            if want_lof and not is_lof:
                continue
            is_t0 = _is_t0(str(etf.get("category")))
            if want_t0 == "1" and not is_t0:
                continue
            if want_t0 == "0" and is_t0:
                continue
            fund_size = float(etf.get("fund_size") or 0)
            if min_size and fund_size / 1e8 < min_size:
                continue
            t = tech.get(code, {})
            vol = t.get("vol20")
            mdd = float(t.get("max_drawdown") or 0)
            if max_dd is not None and mdd and mdd * 100 > max_dd:
                continue
            trend20 = t.get("trend20")
            amount_wan = t.get("avg_amount_wan")
            amplitude = t.get("amplitude")
            if min_amount and (amount_wan or 0) < min_amount:
                continue
            if min_amplitude and (amplitude or 0) < min_amplitude:
                continue
            if max_trend is not None and trend20 is not None and abs(trend20) > max_trend:
                continue
            if min_vol is not None and vol is not None and vol < min_vol:
                continue
            if max_vol is not None and vol is not None and vol > max_vol:
                continue

            score = 0.0
            if vol is not None:
                score += (
                    5 if 15 <= vol <= 35 else
                    3.5 if 10 <= vol < 15 or 35 < vol <= 45 else
                    2 if 8 <= vol < 10 or 45 < vol <= 60 else
                    0.5
                )
            size_b = fund_size / 1e8
            score += (
                5 if size_b >= 50 else 4 if size_b >= 10 else
                3 if size_b >= 3 else 2 if size_b >= 1 else 1
            )
            if trend20 is not None:
                t_abs = abs(trend20)
                score += (
                    5 if t_abs < 5 else 4 if t_abs < 8 else
                    2.5 if t_abs < 12 else 1 if t_abs < 18 else 0
                )
            else:
                score += 2.5
            if mdd:
                m = mdd * 100
                score += 5 if m < 25 else 3.5 if m < 35 else 2 if m < 45 else 0.5
            if is_t0:
                score += 1
            score = round(score, 1)
            if min_score and score < min_score:
                continue
            trend_score = _lightweight_trend_score(t)
            if trend_min is not None and (
                trend_score is None or trend_score < trend_min
            ):
                continue
            rows.append({
                "code": code,
                "name": etf.get("name") or code,
                "category": etf.get("category"),
                "subcategory": etf.get("subcategory"),
                "type": "LOF" if is_lof else "ETF",
                "t0": is_t0,
                "fund_size_yi": round(size_b, 2),
                "close": t.get("close"),
                "vol_pct": vol,
                "trend20_pct": trend20,
                "bb_width": t.get("bb_width"),
                "rsi": t.get("rsi"),
                "avg_amount_wan": amount_wan,
                "amplitude": amplitude,
                "max_dd_pct": round(mdd * 100, 1) if mdd else None,
                "grid_score": score,
                "trend_score": trend_score,
            })

        # ── 补充多维度评分（仅对有缓存的候选计算） ──
        candidates = [r for r in rows if r.get("close") is not None]
        quotes = {}
        try:
            quotes = fetch_realtime_quotes([r["code"] for r in candidates])
        except Exception as exc:
            _log(f"GRID-SCREENER 实时行情获取失败: {exc}", "WARN")
        for r in candidates:
            code = r["code"]
            try:
                ta = grid_svc._grid_trigger_analysis(
                    code, current_price=(quotes or {}).get(code, {}).get("price")
                )
                r["trigger_count"] = ta.get("count")
                r["trigger_freq"] = ta.get("freq_per_day")
                r["trigger_chain"] = ta.get("recent_chain")
                r["trigger_verdict"] = ta.get("verdict")
            except Exception as exc:
                _log(f"GRID-SCREENER 触发分析失败: {r.get('code')} ({exc})", "WARN")

        sort_key_map = {
            "score": lambda r: (-r["grid_score"], r["code"]),
            "vol": lambda r: (r["vol_pct"] or 0, r["code"]),
            "size": lambda r: (-r["fund_size_yi"], r["code"]),
            "amount": lambda r: (-(r["avg_amount_wan"] or 0), r["code"]),
            "trend": lambda r: (abs(r["trend20_pct"] or 0), r["code"]),
            "trend_score": lambda r: (-(r["trend_score"] or 0), r["code"]),
            "amplitude": lambda r: (-(r["amplitude"] or 0), r["code"]),
        }
        rows.sort(key=sort_key_map.get(sort_key, sort_key_map["score"]))
        return {
            "total": len(rows),
            "rows": rows[:300],
            "categories": sorted(
                {str(e.get("category")) for e in etfs if e.get("category")}
            ),
        }

    payload, _, _ = cached(key, 600, refresh, producer)
    data = payload.get("data") or {}
    _biz(
        "GRID-SCREENER",
        f"筛选 {data.get('total')} 只（key={key[-70:]}）",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": data,
    }


def _grid_screener_legacy(params=None):
    """全市场 ETF/LOF 网格选品池：按网格适配度评分。

    四维评分（各 0-5）：
      波动率甜区（年化 15-35% 最优）、流动性（基金规模）、
      均值回归（|20日趋势| 小 → 震荡适合网格）、回撤可控。
    """
    meta = _read_json_file(DATA_DIR / "etf_meta.json", {})
    etfs = meta.get("etfs") or []
    codes = {str(e.get("code")) for e in etfs if e.get("code")}
    tech = _grid_screener_technicals(codes)

    category = (params.get("category", [""])[0] or "").strip()
    want_lof = params.get("lof", ["0"])[0] == "1"
    want_t0 = params.get("t0", [""])[0]
    min_size = _num((params.get("min_size", [""])[0] or "")) or 0
    min_score = _num((params.get("min_score", [""])[0] or "")) or 0
    min_vol = _num((params.get("min_vol", [""])[0] or ""))
    max_vol = _num((params.get("max_vol", [""])[0] or ""))
    min_amount = _num((params.get("min_amount", [""])[0] or "")) or 0
    max_trend = _num((params.get("max_trend", [""])[0] or ""))
    sort_key = (params.get("sort", ["score"])[0] or "score").strip()

    rows = []
    for etf in etfs:
        code = str(etf.get("code") or "").strip()
        if not CODE_RE.match(code):
            continue
        if category and str(etf.get("category")) != category:
            continue
        is_lof = _is_lof(code)
        if want_lof and not is_lof:
            continue
        is_t0 = _is_t0(str(etf.get("category")))
        if want_t0 == "1" and not is_t0:
            continue
        if want_t0 == "0" and is_t0:
            continue
        fund_size = float(etf.get("fund_size") or 0)
        if min_size and fund_size / 1e8 < min_size:
            continue
        t = tech.get(code, {})
        vol = t.get("vol20")
        mdd = float(t.get("max_drawdown") or 0)
        trend20 = tech.get(code, {}).get("trend20")
        amount_wan = tech.get(code, {}).get("avg_amount_wan")
        if min_amount and (amount_wan or 0) < min_amount:
            continue
        if max_trend is not None and trend20 is not None and abs(trend20) > max_trend:
            continue
        if min_vol is not None and vol is not None and vol < min_vol:
            continue
        if max_vol is not None and vol is not None and vol > max_vol:
            continue

        score = 0.0
        if vol is not None:
            score += (
                5 if 15 <= vol <= 35 else
                3.5 if 10 <= vol < 15 or 35 < vol <= 45 else
                2 if 8 <= vol < 10 or 45 < vol <= 60 else
                0.5
            )
        size_b = fund_size / 1e8
        score += (
            5 if size_b >= 50 else 4 if size_b >= 10 else
            3 if size_b >= 3 else 2 if size_b >= 1 else 1
        )
        if trend20 is not None:
            t = abs(trend20)
            score += 5 if t < 5 else 4 if t < 8 else 2.5 if t < 12 else 1 if t < 18 else 0
        else:
            score += 2.5  # 无缓存时给中性分
        if mdd:
            m = mdd * 100
            score += 5 if m < 25 else 3.5 if m < 35 else 2 if m < 45 else 0.5
        if is_t0:
            score += 1  # T+0 加分：日内可进出，网格灵活性更高
        score = round(score, 1)
        if min_score and score < min_score:
            continue
        rows.append({
            "code": code,
            "name": name if (name := etf.get("name")) else code,
            "category": etf.get("category"),
            "subcategory": etf.get("subcategory"),
            "type": "LOF" if is_lof else "ETF",
            "t0": is_t0,
            "fund_size_yi": round(size_b, 2),
            "close": tech.get(code, {}).get("close"),
            "vol_pct": vol,
            "trend20_pct": trend20,
            "bb_width": tech.get(code, {}).get("bb_width"),
            "rsi": tech.get(code, {}).get("rsi"),
            "avg_amount_wan": amount_wan,
            "max_dd_pct": round(mdd * 100, 1) if mdd else None,
            "grid_score": score,
        })

    sort_key_map = {
        "score": lambda r: (-r["grid_score"], r["code"]),
        "vol": lambda r: (r["vol_pct"] or 0, r["code"]),
        "size": lambda r: (-r["fund_size_yi"], r["code"]),
        "amount": lambda r: (-(r["avg_amount_wan"] or 0), r["code"]),
        "trend": lambda r: (abs(r["trend20_pct"] or 0), r["code"]),
    }
    rows.sort(key=sort_key_map.get(sort_key, sort_key_map["score"]))
    _biz(
        "GRID-SCREENER",
        f"全市场筛选 {len(rows)} 只（category={category or '全部'} "
        f"lof={want_lof} min_size={min_size}亿 评分≥{min_score}）",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "total": len(rows),
            "rows": rows[:300],
            "categories": sorted(
                {str(e.get("category")) for e in etfs if e.get("category")}
            ),
        },
    }


def _api_models():
    from models import list_providers, load_config
    providers = list_providers()
    _biz("MODEL", f"厂商列表 {len(providers)} 个: "
                  f"{[(p['name'], p['model'], '已配置' if p['configured'] else '未配置Key') for p in providers]}")
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "default_provider": (load_config() or {}).get("default_provider", ""),
            "providers": providers,
        },
    }


def _api_db_stats(params=None):
    s = db.stats()
    _biz(
        "DB",
        f"统计 tables={s['tables']} 大小={s['size_bytes']}B "
        f"最新快照={s.get('latest_snapshot')}",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": s,
    }


def _api_logs(params=None):
    params = params or {}
    try:
        limit = min(max(int(params.get("limit", ["100"])[0]), 1), 500)
    except (TypeError, ValueError):
        limit = 100
    level = (params.get("level", [""])[0] or "").strip() or None
    if level is not None and level not in ("INFO", "WARN", "ERROR"):
        raise ValueError("level 需为 INFO/WARN/ERROR")
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"logs": db.recent_logs(limit, level)},
    }


def _api_db_tables(params=None):
    s = db.stats()
    s["tables_detail"] = db.list_tables()
    _biz("DB", f"表清单 {[(t['name'], t['count']) for t in s['tables_detail']]}")
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": s,
    }


def _api_db_table(params=None):
    params = params or {}
    name = (params.get("name", [""])[0] or "").strip()
    try:
        limit = int(params.get("limit", ["100"])[0] or "100")
        offset = int(params.get("offset", ["0"])[0] or "0")
    except (TypeError, ValueError):
        raise ValueError("limit/offset 需为整数")
    data = db.table_rows(name, limit, offset)
    _biz("DB", f"读表 {name} rows={len(data['rows'])} offset={offset}")
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": data,
    }


def _api_model_chat(body):
    from models import chat, ModelError
    provider = body.get("provider") or ""
    messages = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages 不能为空")
    system = "\n".join(
        str(m.get("content", "")) for m in messages if m.get("role") == "system"
    )
    user_text = "\n".join(
        str(m.get("content", "")) for m in messages if m.get("role") == "user"
    )
    text = chat(provider, system, user_text)
    _biz("MODEL", f"provider={provider or '(默认)'} 返回 {len(text)} 字")
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"provider": provider, "text": text},
    }


def _api_positions_parse(body):
    from positions_parser import parse_images, verify_parsed
    from models import ModelError

    provider = body.get("provider") or ""
    images = body.get("images") or []
    if not isinstance(images, list) or not 1 <= len(images) <= 6:
        raise ValueError("需要上传 1-6 张图片")
    for image in images:
        data_b64 = image.get("data_b64") or ""
        if not data_b64 or len(data_b64) > 6 * 1024 * 1024:
            raise ValueError("图片数据缺失或超过 6MB")
    _biz("POSITION-PARSE", f"provider={provider or '(默认)'} images={len(images)} 开始解析")
    parsed = parse_images(provider, images, log=_log)
    for index, holding in enumerate(parsed.get("holdings") or [], 1):
        _biz(
            "POSITION-PARSE",
            f"[持仓 {index}] {holding.get('code')} {holding.get('name')} "
            f"市值={holding.get('market_value')} 份额={holding.get('shares')} "
            f"来源={holding.get('source')}",
        )
    for index, trade in enumerate(parsed.get("trades") or [], 1):
        _biz(
            "POSITION-PARSE",
            f"[交易 {index}] {trade.get('date')} {trade.get('action')} "
            f"{trade.get('code')} {trade.get('name')} "
            f"价格={trade.get('price')} 数量={trade.get('shares')}",
        )
    _biz(
        "POSITION-PARSE",
        f"pipeline={parsed.get('parse_pipeline')} holdings={len(parsed.get('holdings') or [])} "
        f"trades={len(parsed.get('trades') or [])}",
    )

    codes = [
        str(h.get("code") or "").strip()
        for h in (parsed.get("holdings") or [])
        if h.get("code")
    ]
    realtime = None
    if codes:
        try:
            realtime = {"quotes": fetch_realtime_quotes(codes)}
        except Exception as exc:
            _log(f"POS 核验实时行情获取失败: {exc}", "WARN")
            realtime = None
    verified = verify_parsed(parsed, realtime)
    counts = verified.get("counts", {})
    _biz(
        "POSITION-PARSE",
        f"核验 status={verified.get('status')} 持仓 {counts.get('holdings_ok')}/{counts.get('holdings')} 通过 "
        f"错误 {counts.get('holdings_error')} 交易 {counts.get('trades')} 笔",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"provider": provider, "parsed": parsed, "verification": verified},
    }


def _api_positions_update(body):
    from positions_parser import update_positions
    verified = body.get("verified") or {}
    source = (body.get("source") or "AI 图片解析").strip()[:80]
    if not isinstance(verified, dict) or not verified.get("holdings"):
        raise ValueError("缺少核验后的持仓数据（请先解析并核验）")
    snapshot = update_positions(verified, source)
    _biz(
        "POSITION-UPDATE",
        f"持仓已更新: {len(snapshot['holdings'])} 只 / 交易 {len(snapshot['trades'])} 笔 "
        f"总资产 {snapshot['account_summary'].get('total_assets')}",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": snapshot,
    }


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "MomentumDashboard/1.0"

    def _request_started(self):
        self._req_t0 = time.time()
        self._resp_size = 0

    def log_message(self, fmt, *args):  # 标准请求行 + 耗时
        if self.path.startswith("/api/"):
            return  # API 请求已由 START / PARAMS / OUT 完整记录
        duration = ""
        if getattr(self, "_req_t0", None):
            duration = f" ({(time.time() - self._req_t0) * 1000:.0f}ms)"
        _log(fmt % args + duration)

    def _send_json(self, obj, status=200):
        body = json.dumps(
            _json_safe(obj), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        self._resp_size = len(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status, message):
        self._send_json(
            {
                "ok": False,
                "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "error": message,
            },
            status=status,
        )

    def _send_static(self, path):
        rel = path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self._send_error_json(404, "静态文件不存在")
            return
        ext = target.suffix.lower()
        content_type = MIME_TYPES.get(ext, "application/octet-stream")
        body = target.read_bytes()
        self._resp_size = len(body)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _summarize_response(result):
        """压缩展示响应结果，便于一眼看出“出场”内容。"""
        if not isinstance(result, dict):
            return f"type={type(result).__name__}"
        data = result.get("data")
        parts = []
        if isinstance(data, dict):
            keys = list(data.keys())
            parts.append(f"keys={keys[:8]}")
            for key, value in data.items():
                if isinstance(value, (list, dict)) and value:
                    parts.append(f"{key}=x{len(value)}")
                elif key in ("live", "status") and not isinstance(value, (dict, list)):
                    parts.append(f"{key}={value}")
        return " ".join(parts[:6]) or "data=empty"

    def do_GET(self):  # noqa: N802
        self._request_started()
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        cache_flag = ""
        if path == "/api/trace":
            msg = params.get("msg", [""])[0]
            _log(f"TRACE {msg[:300]}")
            self._send_json({"ok": True})
            return
        if path.startswith("/api/"):
            _log(f"START GET {self.path}")
            param_view = {key: value[0] for key, value in params.items()}
            _log(f"PARAMS {path} {json.dumps(param_view, ensure_ascii=False)}")
        try:
            if path == "/" or path == "/index.html":
                self._send_static("index.html")
                _log(f"END {path} (static) {self._resp_size}B")
                return
            if path.startswith("/static/"):
                self._send_static(path[len("/static/"):])
                _log(f"END {path} (static) {self._resp_size}B")
                return
            handler = ROUTES.get(path)
            if handler is None:
                _log(f"404 {path}", "WARN")
                self._send_error_json(404, f"未知接口: {path}")
                return
            result = handler(params)
            if isinstance(result, dict) and "cached" in result:
                if result.get("cached"):
                    cache_flag = " [cache]" + (" [stale]" if result.get("stale") else "")
                else:
                    cache_flag = " [fresh]"
            self._send_json(result)
            duration = (time.time() - self._req_t0) * 1000
            _log(
                f"OUT {path} 200{cache_flag} {duration:.0f}ms "
                f"{self._resp_size}B {self._summarize_response(result)}"
            )
        except ValueError as exc:
            _log(f"400 {path}: {exc}", "WARN")
            self._send_error_json(400, str(exc))
            _log(f"OUT {path} 400 {self._resp_size}B error={exc}")
        except Exception as exc:  # noqa: BLE001
            _log(f"500 {path}: {exc}", "ERROR")
            traceback.print_exc()
            self._send_error_json(500, str(exc))
            _log(f"OUT {path} 500 {self._resp_size}B error={exc}")

    def do_POST(self):  # noqa: N802
        self._request_started()
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            _log(f"404 {path}", "WARN")
            self._send_error_json(404, f"未知接口: {path}")
            return
        _log(f"START POST {self.path}")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > MAX_BODY_BYTES:
            _log(f"413 {path} 请求体 {length}B 超限", "WARN")
            self._send_error_json(413, f"请求体过大（上限 {MAX_BODY_BYTES} 字节）")
            return
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("请求体必须是 JSON 对象")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _log(f"400 {path}: JSON 解析失败 {exc}", "WARN")
            self._send_error_json(400, "请求体必须为 UTF-8 JSON")
            return
        except ValueError as exc:
            _log(f"400 {path}: {exc}", "WARN")
            self._send_error_json(400, str(exc))
            return

        handler = POST_ROUTES.get(path)
        if handler is None:
            _log(f"404 {path}", "WARN")
            self._send_error_json(404, f"未知接口: {path}")
            return
        try:
            result = handler(body)
            self._send_json(result)
            duration = (time.time() - self._req_t0) * 1000
            _log(
                f"OUT {path} 200 {duration:.0f}ms "
                f"{self._resp_size}B {self._summarize_response(result)}"
            )
        except ValueError as exc:
            _log(f"400 {path}: {exc}", "WARN")
            self._send_error_json(400, str(exc))
            _log(f"OUT {path} 400 {self._resp_size}B error={exc}")
        except ImportError as exc:
            _log(f"500 {path}: {exc}", "ERROR")
            traceback.print_exc()
            self._send_error_json(500, f"依赖模块加载失败: {exc}")
        except Exception as exc:  # noqa: BLE001 - 模型/上游错误统一 502
            _log(f"502 {path}: {exc}", "ERROR")
            traceback.print_exc()
            self._send_error_json(502, str(exc))
            _log(f"OUT {path} 502 {self._resp_size}B error={exc}")


def main():
    parser = argparse.ArgumentParser(description="动量轮动策略仪表盘")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--online",
        action="store_true",
        help="允许页面加载时联网刷新数据（默认离线缓存模式，刷新按钮才会联网）",
    )
    parser.add_argument(
        "--no-scheduler",
        action="store_true",
        help="禁用交易时段定时任务（默认启用：交易日 09:07-11:57 / 13:07-15:27 每 10 分钟）",
    )
    args = parser.parse_args()

    global ALLOW_ONLINE, _SCHEDULER
    ALLOW_ONLINE = args.online
    os.environ["ETF_DATA_OFFLINE"] = "0" if args.online else "1"

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    _log("=" * 64)
    _log(f"动量轮动策略仪表盘启动: http://{args.host}:{args.port}")
    _log(f"数据模式: {'联网(--online)' if args.online else '离线缓存（点刷新数据才联网）'}")
    _log(f"项目根目录: {PROJECT_DIR}")
    _log(f"静态资源: {STATIC_DIR}")
    _log(f"数据存储: db.py 后端={db.db_info()}")
    _log(f"缓存层: cache.py 后端={cache_backend_name()} (持久化/可切换 Redis 等)")
    today_log = ROOT / f"server-{datetime.now().strftime('%Y%m%d')}.log"
    _log(f"日志文件: {today_log}（按天命名，保留 7 天自动删除）")
    _log(f"信号池: {len(signal_svc.SIGNAL_POOLS)} 个预设, 回测预设: {len(signal_svc.BACKTEST_PRESETS)} 个")
    scheduler_enabled = not args.no_scheduler and os.environ.get(
        "MOMENTUM_SCHEDULER", "1"
    ) != "0"
    if scheduler_enabled:
        from scheduler import Scheduler
        _SCHEDULER = Scheduler(job=signal_svc.scheduled_job, log=_log, name="sched")
        _SCHEDULER.start()
        _log("定时任务: 已启用（交易日 09:07-11:57 / 13:07-15:27 每 10 分钟）")
    else:
        _log("定时任务: 已禁用（--no-scheduler）")
    signal_svc._reload_momentum_pools_from_db()
    maintenance = threading.Thread(
        target=_maintenance_loop, name="db-maint", daemon=True
    )
    maintenance.start()
    _log("日志保留策略: server-YYYYMMDD.log 按天命名保留 7 天；api_logs 自动清理 7 天前数据（每 6 小时）")
    _log("=" * 64)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("收到 Ctrl-C，服务停止", "WARN")
    finally:
        if _SCHEDULER is not None:
            _SCHEDULER.stop()
        _MAINT_STOP.set()
        server.server_close()


def _api_grid_trigger_add(body):
    """手动录入一条网格触发记录（写数据库）。"""
    from grid_parser import verify_grid_records
    configs, names, _ = grid_svc._grid_configs()
    code = str((body or {}).get("code") or "").strip()
    date = str((body or {}).get("date") or "").strip()
    trade_time = str((body or {}).get("time") or "").strip()
    action = str((body or {}).get("action") or "").strip().lower()
    trigger_type = str((body or {}).get("trigger_type") or "").strip().lower()
    price = (body or {}).get("price")
    shares = (body or {}).get("shares")
    records = verify_grid_records(
        [{
            "code": code,
            "date": date,
            "time": trade_time,
            "action": action,
            "trigger_type": trigger_type,
            "price": price,
            "shares": shares,
            "base_price_before": (body or {}).get("base_price_before"),
            "base_price_after": (body or {}).get("base_price_after"),
        }],
        known_codes=set(names),
    )
    record = records[0]
    if record["status"] == "error":
        raise ValueError("录入信息有误：" + "；".join(record["issues"]))
    if record["code"] not in names:
        raise ValueError(
            f"{record['code']} 不在网格标的中，无法手动录入（请从下拉框选择）"
        )
    record = grid_svc._grid_base_chain([record], configs)[0]
    db_status = db.append_grid_trigger(
        record["code"],
        names.get(record["code"]),
        record["date"],
        record.get("time") or "",
        record["action"],
        record["price"],
        record["shares"],
        trigger_type=record.get("trigger_type") or "grid",
        base_price_before=record.get("base_price_before"),
        base_price_after=record.get("base_price_after"),
        source="manual",
    )
    _biz(
        "GRID-TRIGGER",
        f"手动录入 {record['code']} {record['date']} {record['action']} "
        f"{record.get('time') or ''} {record['price']}×{record['shares']} → {db_status}",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"db_status": db_status, "record": record},
    }


def _api_grid_trigger_parse(body):
    """截图识别网格触发记录（视觉模型 或 OCR+文本模型），返回待核验记录。"""
    from grid_parser import parse_grid_images, verify_grid_records
    from models import ModelError
    _, names, sizes = grid_svc._grid_configs()
    provider = (body or {}).get("provider") or ""
    images = (body or {}).get("images") or []
    if not isinstance(images, list) or not 1 <= len(images) <= 6:
        raise ValueError("需要上传 1-6 张图片")
    for image in images:
        data_b64 = image.get("data_b64") or ""
        if not data_b64 or len(data_b64) > 6 * 1024 * 1024:
            raise ValueError("图片数据缺失或超过 6MB")
    _biz("GRID-PARSE", f"provider={provider or '(默认)'} images={len(images)} 开始识别")
    parsed = parse_grid_images(provider, images, log=_log)
    records = verify_grid_records(
        parsed.get("trades") or [],
        known_codes=set(names),
        grid_sizes=sizes,
    )
    records = grid_svc._mark_grid_duplicates(records)
    for index, record in enumerate(records, 1):
        _biz(
            "GRID-PARSE",
            f"[{index}/{len(records)}] {record.get('code')} "
            f"{record.get('date')} {record.get('action')} "
            f"{record.get('price')}×{record.get('shares')} "
            f"status={record.get('status')}"
            f"{' 重复' if record.get('duplicate') else ''}"
            f"{(' 问题=' + '；'.join(record.get('issues') or [])) if record.get('issues') else ''}"
            f"{(' 提示=' + '；'.join(record.get('warns') or [])) if record.get('warns') else ''}",
        )
    _biz(
        "GRID-PARSE",
        f"pipeline={parsed.get('parse_pipeline')} records={len(records)} "
        f"ok={sum(1 for r in records if r['status'] == 'ok')} "
        f"warn={sum(1 for r in records if r['status'] == 'warn')} "
        f"error={sum(1 for r in records if r['status'] == 'error')} "
        f"duplicate={sum(1 for r in records if r.get('duplicate'))}",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "pipeline": parsed.get("parse_pipeline"),
            "provider": provider,
            "detected_platforms": parsed.get("detected_platforms") or [],
            "records": records,
        },
    }


def _api_grid_trigger_confirm(body):
    """确认录入识别/手动编辑后的触发记录（写 DB + 同步文件）。"""
    from grid_parser import verify_grid_records
    configs, names, _ = grid_svc._grid_configs()
    records = (body or {}).get("records") or []
    if not isinstance(records, list) or not records:
        raise ValueError("缺少待确认的触发记录")
    verified = verify_grid_records(records, known_codes=set(names))
    errors = [r for r in verified if r["status"] == "error"]
    if errors:
        detail = "；".join(
            f"{r.get('code')} {r.get('date')}: " + "，".join(r["issues"])
            for r in errors
        )
        raise ValueError(
            f"存在错误记录，请修正后重试（或点「移除需核对」删除不需要的记录）：{detail}"
        )
    # 按 日期+时间 升序录入，保证记录按时间顺序入库
    verified.sort(
        key=lambda r: (str(r.get("date") or ""), str(r.get("time") or ""))
    )
    verified = grid_svc._grid_base_chain(verified, configs)
    added = []
    for record in verified:
        db_status = db.append_grid_trigger(
            record["code"],
            names.get(record["code"]),
            record["date"],
            record.get("time") or "",
            record["action"],
            record["price"],
            record["shares"],
            trigger_type=record.get("trigger_type") or "grid",
            base_price_before=record.get("base_price_before"),
            base_price_after=record.get("base_price_after"),
            source="ocr",
        )
        _biz(
            "GRID-TRIGGER",
            f"确认 {record['code']} {record['date']} {record['action']} "
            f"{record.get('time') or ''} {record['price']}×{record['shares']} → {db_status}",
        )
        added.append({**record, "db_status": db_status})
    _biz(
        "GRID-TRIGGER",
        f"确认录入 {len(added)} 条（新增 "
        f"{sum(1 for r in added if r['db_status'] == 'inserted')} / "
        f"重复 {sum(1 for r in added if r['db_status'] == 'duplicate')}）",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"added": len(added), "records": added},
    }


def _api_grid_config_parse(body):
    """截图识别网格条件单配置（视觉模型 或 OCR+文本模型），返回待核验草稿。"""
    from grid_parser import parse_grid_config_images, verify_grid_configs
    provider = (body or {}).get("provider") or ""
    images = (body or {}).get("images") or []
    if not isinstance(images, list) or not 1 <= len(images) <= 6:
        raise ValueError("需要上传 1-6 张图片")
    for image in images:
        data_b64 = image.get("data_b64") or ""
        if not data_b64 or len(data_b64) > 6 * 1024 * 1024:
            raise ValueError("图片数据缺失或超过 6MB")
    _, names, _ = grid_svc._grid_configs()
    _biz(
        "GRID-CONFIG-PARSE",
        f"provider={provider or '(默认)'} images={len(images)} 开始解析",
    )
    parsed = parse_grid_config_images(provider, images, log=_log)
    configs = verify_grid_configs(
        parsed.get("configs") or [], known_codes=set(names)
    )
    for index, config in enumerate(configs, 1):
        _biz(
            "GRID-CONFIG-PARSE",
            f"[{index}/{len(configs)}] {config.get('code')} "
            f"{config.get('name')} 基准={config.get('base_price')} "
            f"间距={config.get('spacing_up_pct')}%/{config.get('spacing_down_pct')}% "
            f"区间={config.get('price_low')}~{config.get('price_high')} "
            f"每格={config.get('shares_per_grid')} "
            f"持仓={config.get('base_position')}~{config.get('max_position')} "
            f"status={config.get('status')}"
            f"{(' 问题=' + '；'.join(config.get('issues') or [])) if config.get('issues') else ''}",
        )
    _biz(
        "GRID-CONFIG-PARSE",
        f"pipeline={parsed.get('parse_pipeline')} configs={len(configs)} "
        f"ok={sum(1 for c in configs if c['status'] == 'ok')} "
        f"warn={sum(1 for c in configs if c['status'] == 'warn')} "
        f"error={sum(1 for c in configs if c['status'] == 'error')}",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "pipeline": parsed.get("parse_pipeline"),
            "provider": provider,
            "detected_platforms": parsed.get("detected_platforms") or [],
            "configs": configs,
        },
    }


def _api_grid_config_update(body):
    """确认后的网格配置写入 grid_configs（新增或更新）。"""
    configs = (body or {}).get("configs") or []
    if not isinstance(configs, list) or not configs:
        raise ValueError("缺少网格配置数据")
    normalized = []
    for config in configs:
        code = str(config.get("code") or "").strip()
        if not CODE_RE.match(code):
            raise ValueError(f"代码格式非法: {code}")
        if config.get("status") == "error":
            raise ValueError(
                f"存在核验错误项（{code}）: "
                f"{'; '.join(config.get('issues') or [])}，请修正后再保存"
            )
        normalized.append({
            **config,
            "code": code,
            "strategy_type": (config.get("strategy_type") or "网格交易").strip(),
            "status": (config.get("status") or "active").strip(),
        })
    saved = db.save_grid_configs(normalized)
    try:
        from tools import grid_trading as gt
        gt.reload_configs()
    except Exception as exc:
        _log(f"GRID 配置热刷新失败: {exc}", "WARN")
    _biz(
        "GRID-CONFIG-UPDATE",
        f"网格配置保存 {len(configs)} 条（新增/更新 {saved} 行）",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "saved": len(normalized),
            "configs": db.load_grid_configs(),
        },
    }


_GRID_ADVISE_SYSTEM = """你是量化网格策略顾问。基于给定 ETF 的当前技术状态与回测寻优结果，
结合宏观、地缘、利率/流动性、汇率、产业周期等可能影响该 ETF 方向的因素，
输出适配当下市场环境的网格配置建议。只输出 JSON，不要解释。

## 输出 JSON 结构
{
  "market_verdict": "多头/空头/震荡/观望",
  "confidence": 0.0到1.0,
  "factors": [
    {"factor": "因素名(如美联储利率、中美关系、原油、汇率)", "impact": "利好/利空/中性", "detail": "一句话说明"}
  ],
  "direction_assessment": "对该ETF未来1-3个月方向的综合判断（一句话）",
  "recommended_config": {
    "spacing_up_pct": 数字, "spacing_down_pct": 数字,
    "levels_above": 数字, "levels_below": 数字,
    "shares_per_grid": 数字,
    "base_position": 数字, "max_position": 数字
  },
  "same_as_backtest": true或false,
  "deviation_note": "是否沿用回测最优：若一致，说明为何今天仍适用；若偏离，说明偏离点与理由",
  "reasoning": "为何这样配置（结合市场判断与寻优结果，2-3句）",
  "risks": ["风险1", "风险2"]
}

## 配置逻辑
- 回测寻优 Top 候选是「历史区间的最优基线」，不是答案本身。
  你的价值在于判断这个历史最优在当下是否仍然成立：
  - 若当前市场环境支持历史最优，可以沿用（recommended_config 可与回测最优一致），
    但 reasoning 必须说明「为什么今天仍然适用」（结合技术状态/触发记录/宏观因素）；
  - 若有理由偏离，给出偏离后的参数并说明偏离方向与理由：
    趋势转强/多头 → 适当加大间距、减少层数，别让网格过早卖飞；
    震荡 → 缩小间距、增加层数，多收割波动；
    风险偏高/地缘或汇率利空 → 降低 max_position/提高 base_position 占比。
  - 严禁把回测 Top 候选直接复述为建议而不给市场理由。
- 数据日期以系统当前日期为准，不确定的宏观事实不要编造，写"需关注"。
"""


def _api_grid_config_advise(body):
    """寻优后的大模型研判：结合技术状态+寻优结果+宏观因素，给出适配当下环境的配置。"""
    from models import ModelError, chat_json
    from tools.etf_market_data import load_etf_series

    code = str((body or {}).get("code") or "").strip()
    if not CODE_RE.match(code):
        raise ValueError(f"非法证券代码: {code}")
    provider = (body or {}).get("provider") or ""
    top = (body or {}).get("top") or []
    current = (body or {}).get("current_config") or {}
    trigger_analysis = grid_svc._grid_trigger_analysis(code)

    # ── 技术指标（从数据库 K 线离线计算） ──
    technicals = {}
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
        closes = [b["close"] for b in series.bars]
        n = len(closes)
        if n >= 61:
            technicals = {
                "price": round(closes[-1], 4),
                "pct_5d": round((closes[-1] / closes[-6] - 1) * 100, 2),
                "pct_20d": round((closes[-1] / closes[-21] - 1) * 100, 2),
                "pct_60d": round((closes[-1] / closes[-61] - 1) * 100, 2),
                "ma20": round(sum(closes[-20:]) / 20, 4),
                "ma60": round(sum(closes[-60:]) / 60, 4),
            }
            gains = losses = 0.0
            for i in range(n - 14, n):
                diff = closes[i] - closes[i - 1]
                if diff > 0:
                    gains += diff
                else:
                    losses -= diff
            avg_g = gains / 14
            avg_l = losses / 14
            technicals["rsi"] = round(
                100 - 100 / (1 + avg_g / avg_l) if avg_l > 0 else 100.0, 1
            )
            if technicals["ma20"] > technicals["ma60"]:
                technicals["ma_state"] = "MA20在MA60上方(多头)"
            elif technicals["ma20"] < technicals["ma60"]:
                technicals["ma_state"] = "MA20在MA60下方(空头)"
            else:
                technicals["ma_state"] = "均线缠绕"
    except Exception as exc:
        _log(f"GRID-ADVISE 技术指标计算失败: {exc}", "WARN")

    top_text = "；".join(
        f"{r.get('spacing')}%/{r.get('levels')}层/{r.get('shares')}股 "
        f"年化{r.get('annual_return_pct')}% 回撤{r.get('max_dd_pct')}%"
        for r in (top or [])[:5]
    ) or "无"
    current_text = json.dumps(current, ensure_ascii=False) if current else "无"
    trigger_text = (
        f"网格触发记录分析: 共{trigger_analysis.get('count')}条 "
        f"(买{trigger_analysis.get('buys')}/卖{trigger_analysis.get('sells')})，"
        f"跨度{trigger_analysis.get('span_days')}天，"
        f"最近方向链 {trigger_analysis.get('recent_chain') or '无'}，"
        f"买均价{trigger_analysis.get('avg_buy')} 卖均价{trigger_analysis.get('avg_sell')}，"
        f"现价{trigger_analysis.get('current_price')}，"
        f"问题: {'；'.join(trigger_analysis.get('issues') or []) or '无明显异常'}"
    )
    recent_trades_text = "\n".join(
        f"{t.get('date')} {t.get('time') or ''} "
        f"{'买' if ('buy' in str(t.get('action') or '').lower() or '买' in str(t.get('action'))) else '卖'}"
        f"({t.get('type')}) {t.get('price')}×{t.get('shares')}"
        for t in (trigger_analysis.get("recent_trades") or [])
    ) or "无"
    user_text = (
        f"ETF: {code} {current.get('name') or code}\n"
        f"现价: {technicals.get('price')} | 5日 {technicals.get('pct_5d')}% | "
        f"20日 {technicals.get('pct_20d')}% | 60日 {technicals.get('pct_60d')}%\n"
        f"技术状态: {technicals.get('ma_state')} | RSI {technicals.get('rsi')}\n"
        f"回测寻优 Top 候选: {top_text}\n"
        f"当前配置: {current_text}\n"
        f"{trigger_text}\n"
        f"最近一个月成功触发成交记录:\n{recent_trades_text}\n"
        "请结合当前市场环境（宏观/地缘/周期/汇率等可能影响该ETF的因素）给出适配配置建议。"
    )
    _biz("GRID-ADVISE", f"{code} 调用模型研判（provider={provider or '默认'}）")
    parsed, text = chat_json(
        provider or None,
        _GRID_ADVISE_SYSTEM,
        user_text,
        max_tokens=2500,
        retries=1,
        log=_log,
    )
    _log(f"GRID-ADVISE 模型返回 {len(text)} 字符")

    recommended = parsed.get("recommended_config") or {}
    for key in ("spacing_up_pct", "spacing_down_pct"):
        try:
            recommended[key] = round(float(recommended.get(key)), 1)
        except (TypeError, ValueError):
            recommended[key] = None
    # 是否与回测最优一致：模型声明优先，缺失时按参数比对兜底
    same_as_backtest = bool(parsed.get("same_as_backtest"))
    if "same_as_backtest" not in parsed:
        same_as_backtest = any(
            abs(float((r.get("spacing") or 0)) - float(recommended.get("spacing_up_pct") or 0)) < 0.05
            and int(r.get("levels") or 0) == int(recommended.get("levels_above") or 0)
            and int(r.get("shares") or 0) == int(recommended.get("shares_per_grid") or 0)
            for r in (top or [])
            if recommended.get("spacing_up_pct") is not None
        )
    deviation_note = parsed.get("deviation_note") or (
        "与回测最优一致" if same_as_backtest else "已偏离回测最优"
    )
    for key in (
        "levels_above", "levels_below", "shares_per_grid",
        "base_position", "max_position",
    ):
        try:
            value = int(float(recommended.get(key)))
            if key == "shares_per_grid":
                value = max(100, int(round(value / 100)) * 100)
            recommended[key] = value
        except (TypeError, ValueError):
            recommended[key] = None
    _biz(
        "GRID-ADVISE",
        f"{code} 研判={parsed.get('market_verdict')} "
        f"信心={parsed.get('confidence')} "
        f"建议={recommended.get('spacing_up_pct')}%/"
        f"{recommended.get('levels_above')}层/{recommended.get('shares_per_grid')}股",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "code": code,
            "provider": provider or "default",
            "market_verdict": parsed.get("market_verdict"),
            "confidence": parsed.get("confidence"),
            "factors": parsed.get("factors") or [],
            "direction_assessment": parsed.get("direction_assessment"),
            "recommended_config": recommended,
            "same_as_backtest": same_as_backtest,
            "deviation_note": deviation_note,
            "reasoning": parsed.get("reasoning"),
            "risks": parsed.get("risks") or [],
            "input_context": user_text,
            "raw_text": text,
            "trigger_analysis": trigger_analysis,
        },
    }


POST_ROUTES = {
    "/api/model/chat": _api_model_chat,
    "/api/positions/parse": _api_positions_parse,
    "/api/positions/update": _api_positions_update,
    "/api/scheduler/run": _api_scheduler_run,
    "/api/preset-pool/apply": _api_preset_pool_apply,
    "/api/grid/triggers": _api_grid_trigger_add,
    "/api/grid/triggers/parse": _api_grid_trigger_parse,
    "/api/grid/triggers/confirm": _api_grid_trigger_confirm,
    "/api/grid/configs/parse": _api_grid_config_parse,
    "/api/grid/configs/update": _api_grid_config_update,
    "/api/grid/configs/advise": _api_grid_config_advise,
}


if __name__ == "__main__":
    main()
