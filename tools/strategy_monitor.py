"""动量轮动、网格趋势和策略审计的结构化综合监测。"""

import argparse
import importlib
import json
import os
import sys
from datetime import datetime

try:
    from tools.momentum_signal import scan
    from tools.strategy_models import RunStatus, strict_json_dumps
except ModuleNotFoundError as exc:  # 支持直接执行 tools/strategy_monitor.py
    if exc.name not in {"tools", "tools.momentum_signal", "tools.strategy_models"}:
        raise
    from momentum_signal import scan
    from strategy_models import RunStatus, strict_json_dumps


MOMENTUM_POOL = {
    "518880": "黄金ETF",
    "513100": "纳指ETF",
    "159915": "创业板ETF",
    "159920": "恒生ETF",
}

DEFAULT_GRID_ETFS = (
    "512880",
    "159915",
    "513180",
    "512690",
    "512010",
    "510300",
    "159920",
)

_UNKNOWN_RISK = {
    "status": RunStatus.UNKNOWN.value,
    "as_of": None,
    "max_dd_pct": None,
    "sharpe": None,
    "var_95_loss_pct": None,
    "ic_10d": None,
    "ic_20d": None,
}


def load_grid_etfs():
    """Return configured grid ETF codes without invoking another process."""
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "grid_triggers.json"
    )
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and data:
            return list(data)
    except (OSError, json.JSONDecodeError):
        pass
    return list(DEFAULT_GRID_ETFS)


def _status_value(status):
    return status.value if isinstance(status, RunStatus) else status


def _unknown_grid(code, message):
    return {
        "code": code,
        "etf": code,
        "name": code,
        "status": RunStatus.UNKNOWN.value,
        "score": None,
        "bb_width": None,
        "ma_state": None,
        "verdict": None,
        "error": message,
    }


def _legacy_grid_adapter(code, error=None):
    """Do not infer structured values from the legacy text-only trend command."""
    return _unknown_grid(code, error or "grid_trading.analyze_trend 尚不可用")


def _legacy_audit_adapter(error=None):
    """Do not infer risk metrics from the legacy text-only audit command."""
    return {
        "status": RunStatus.UNKNOWN.value,
        "error": error or "strategy_audit.run_audit 尚不可用",
    }


def _import_optional_provider(module_names, attribute, fallback):
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            parent_package = module_name.partition(".")[0]
            if exc.name in {module_name, parent_package}:
                continue
            return None, exc
        provider = getattr(module, attribute, None)
        if callable(provider):
            return provider, None
        return fallback, None
    return fallback, None


def _default_grid_analyzer():
    analyzer, error = _import_optional_provider(
        ("tools.grid_trading", "grid_trading"), "analyze_trend", _legacy_grid_adapter
    )
    if error is not None:
        return lambda code, error=error: _legacy_grid_adapter(code, str(error))
    return analyzer


def _default_audit_provider():
    provider, error = _import_optional_provider(
        ("tools.strategy_audit", "strategy_audit"), "run_audit", _legacy_audit_adapter
    )
    if error is not None:
        return lambda error=error: _legacy_audit_adapter(str(error))
    return provider


def _build_momentum(momentum_provider):
    envelope = momentum_provider(pool=MOMENTUM_POOL)
    if not isinstance(envelope, dict):
        raise TypeError("momentum provider 必须返回 dict")
    status = _status_value(envelope.get("status"))
    items = envelope.get("items")
    if not isinstance(items, list):
        items = []
    return {
        "status": status,
        "as_of": envelope.get("as_of"),
        "items": items,
        "errors": envelope.get("errors", []),
        "selected": envelope.get("selected"),
        "pool_complete": envelope.get("pool_complete"),
    }


def _build_grid(grid_analyzer, codes):
    results = []
    for code in codes:
        try:
            raw = grid_analyzer(code)
            if not isinstance(raw, dict):
                raise TypeError("grid analyzer 必须返回 dict")
            status = _status_value(raw.get("status", RunStatus.UNKNOWN.value))
            score = raw.get("score") if status == RunStatus.OK.value else None
            if score is not None and not isinstance(score, (int, float)):
                raise TypeError("grid score 必须是数值或 None")
            item = dict(raw)
            item.update({
                "code": raw.get("code", raw.get("etf", code)),
                "etf": raw.get("etf", raw.get("code", code)),
                "name": raw.get("name", raw.get("code", code)),
                "status": status,
                "score": score,
            })
            results.append(item)
        except Exception as exc:  # noqa: BLE001 - provider boundary becomes unknown
            results.append(_unknown_grid(code, str(exc) or "网格分析失败"))
    return results


def _group_grid(results):
    known = [
        item for item in results
        if item.get("status") == RunStatus.OK.value and item.get("score") is not None
    ]
    return {
        "stop": [item for item in known if item["score"] <= -4],
        "caution": [item for item in known if -3 <= item["score"] <= -2],
        "ok": [item for item in known if item["score"] >= -1],
    }


def _build_risk(audit_provider):
    try:
        audit = audit_provider()
        if not isinstance(audit, dict):
            raise TypeError("audit provider 必须返回 dict")
        if _status_value(audit.get("status")) == RunStatus.UNKNOWN.value:
            raise ValueError(audit.get("error") or "审计结果不可用")
        period = audit["period"]
        daily = audit["daily_metrics"]
        ic_ir = audit["ic_ir"]
        return {
            "status": RunStatus.OK.value,
            "as_of": period["end"],
            "max_dd_pct": daily["max_dd_pct"],
            "sharpe": daily["sharpe"],
            "var_95_loss_pct": daily.get("var_95_daily_pct", daily.get("var_95_loss_pct")),
            "ic_10d": ic_ir["ic_10d"],
            "ic_20d": ic_ir["ic_20d"],
        }
    except Exception as exc:  # noqa: BLE001 - provider boundary becomes unknown
        return {**_UNKNOWN_RISK, "error": str(exc) or "审计加载失败"}


def _momentum_action(momentum):
    status = momentum.get("status")
    if status == RunStatus.OK.value and momentum.get("selected"):
        selected = momentum["selected"]
        return f"按信号换仓至 {selected.get('code')} {selected.get('name', '')}".strip()
    if status == RunStatus.NO_SIGNAL.value:
        return "持币或切换至 511880 银华日利"
    return None


def _grid_action(groups):
    stopped = [item.get("code") for item in groups["stop"]]
    if stopped:
        return f"暂停 {', '.join(stopped)} 的买入条件单"
    if any(groups.values()):
        return "无需操作，条件单按趋势评分运行"
    return None


def build_monitor_report(
    *,
    momentum_provider=scan,
    grid_analyzer=None,
    audit_provider=None,
    include_momentum=True,
    include_grid=True,
    include_audit=True,
):
    """Build one report from direct, structured Python provider results."""
    if include_momentum:
        try:
            momentum = _build_momentum(momentum_provider)
        except Exception as exc:  # noqa: BLE001 - provider boundary becomes unknown
            momentum = {
                "status": RunStatus.UNKNOWN.value,
                "as_of": None,
                "items": [],
                "errors": [{"stage": "monitor", "message": str(exc)}],
                "selected": None,
                "pool_complete": False,
            }
    else:
        momentum = None

    if include_grid:
        analyzer = grid_analyzer or _default_grid_analyzer()
        grid = _build_grid(analyzer, load_grid_etfs())
    else:
        grid = []
    grid_groups = _group_grid(grid)

    if include_audit:
        risk = _build_risk(audit_provider or _default_audit_provider())
    else:
        risk = None

    return {
        "time": datetime.now().astimezone().isoformat(),
        "momentum": momentum,
        "grid": grid,
        "grid_groups": grid_groups,
        "risk": risk,
        "advice": {
            "momentum_action": _momentum_action(momentum) if momentum else None,
            "grid_action": _grid_action(grid_groups),
        },
    }


def _format_optional(value, suffix=""):
    return "未知" if value is None else f"{value}{suffix}"


def render_human_report(report):
    """Render a report without invoking providers or mutating report data."""
    lines = [
        "动量轮动(RSRS v3.0) + 网格趋势 双策略监测",
        f"时间: {report['time']}",
    ]

    momentum = report.get("momentum")
    if momentum is not None:
        lines.extend(["", "动量轮动 · RSRS 信号", f"状态: {momentum.get('status')}"])
        for item in momentum.get("items", []):
            lines.append(f"{item.get('code', '-')} {item.get('name', '')}".rstrip())
            if item.get("error"):
                lines.append(f"  数据错误: {item['error']}")
            else:
                lines.append(
                    f"  RSRS: {_format_optional(item.get('rsrs_score'))} | "
                    f"信号: {item.get('signal_strength', 'none')}"
                )
        strengths = [item.get("signal_strength") for item in momentum.get("items", [])]
        lines.append(
            "信号分布: "
            f"强信号: {strengths.count('strong')} | "
            f"中等: {strengths.count('medium')} | "
            f"无信号: {strengths.count('none')}"
        )

    grid = report.get("grid", [])
    if grid:
        lines.extend(["", "网格策略 · 持仓趋势"])
        for item in grid:
            score = _format_optional(item.get("score"))
            lines.append(
                f"{item.get('code', item.get('etf', '-'))} {item.get('name', '')} "
                f"评分: {score} 状态: {item.get('status', 'unknown')}"
            )

    risk = report.get("risk")
    if risk is not None:
        lines.extend(["", "风险参数 (日频审计)", f"状态: {risk.get('status')}"])
        if risk.get("status") == RunStatus.OK.value:
            lines.append(
                f"截至: {risk['as_of']} | MaxDD: {risk['max_dd_pct']}% | "
                f"Sharpe: {risk['sharpe']} | VaR(95%): {risk['var_95_loss_pct']}%"
            )
            lines.append(f"RSRS IC(10日): {risk['ic_10d']} | IC(20日): {risk['ic_20d']}")
        else:
            lines.append("审计数据不可用，未使用静态风险值")

    advice = report.get("advice", {})
    lines.extend(["", "操作清单"])
    lines.append(f"动量: {advice.get('momentum_action') or '无正式动作'}")
    lines.append(f"网格: {advice.get('grid_action') or '无正式动作'}")
    return "\n".join(lines) + "\n"


def _parser():
    parser = argparse.ArgumentParser(description="双策略综合监测 v3.0")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--momentum-only", action="store_true")
    mode.add_argument("--grid-only", action="store_true")
    mode.add_argument("--audit", action="store_true", help="仅运行结构化量化审计")
    parser.add_argument("--json", action="store_true", help="输出严格 JSON")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        report = build_monitor_report(
            include_momentum=not args.grid_only and not args.audit,
            include_grid=not args.momentum_only and not args.audit,
            include_audit=not args.momentum_only and not args.grid_only,
        )
        if args.json:
            print(strict_json_dumps(report))
        else:
            print(render_human_report(report), end="")
        return 0
    except KeyboardInterrupt:
        print("监测已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
