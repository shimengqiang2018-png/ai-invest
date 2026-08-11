import json
import math
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from enum import Enum
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import strategy_monitor
from strategy_models import RunStatus, StrategyError, strict_json_dumps
from strategy_monitor import build_monitor_report, main, render_human_report


class StrategyModelTests(unittest.TestCase):
    def test_status_vocabulary_is_closed(self):
        self.assertEqual(
            {status.value for status in RunStatus},
            {"ok", "provisional", "unknown", "no_signal"},
        )

    def test_strict_json_converts_non_finite_values_to_null(self):
        payload = {
            "status": RunStatus.OK,
            "profit_factor": math.inf,
            "error": StrategyError("512880", "grid", None, "failed"),
        }
        encoded = strict_json_dumps(payload)
        decoded = json.loads(encoded, parse_constant=lambda value: self.fail(value))
        self.assertIsNone(decoded["profit_factor"])
        self.assertEqual(decoded["error"]["stage"], "grid")

    def test_strict_json_recursively_converts_enum_values(self):
        class NonFiniteStatus(Enum):
            INVALID = math.nan

        encoded = strict_json_dumps({"status": NonFiniteStatus.INVALID})

        self.assertIsNone(json.loads(encoded)["status"])


def momentum_scan(status=RunStatus.OK):
    items = [
        {
            "code": "159920",
            "name": "恒生ETF",
            "pass": status == RunStatus.OK,
            "formal": status in (RunStatus.OK, RunStatus.NO_SIGNAL),
            "signal_strength": "medium" if status == RunStatus.OK else "none",
            "rsrs_score": 2.4,
        }
    ]
    return {
        "status": status,
        "as_of": "2026-07-29",
        "items": items,
        "errors": [],
        "selected": items[0] if status == RunStatus.OK else None,
        "pool_complete": status != RunStatus.UNKNOWN,
    }


def fake_grid(code):
    return {
        "code": code,
        "name": "恒生ETF" if code == "159920" else code,
        "status": "ok",
        "score": 1,
        "bb_width": 5.2,
        "ma_state": "缠绕",
        "verdict": "网格正常运行",
    }


def fake_audit():
    return {
        "period": {"end": "2026-07-29"},
        "daily_metrics": {
            "max_dd_pct": 12.3,
            "sharpe": 1.4,
            "var_95_loss_pct": 1.7,
        },
        "ic_ir": {"ic_10d": 0.07, "ic_20d": 0.03},
    }


class StrategyMonitorTests(unittest.TestCase):
    def test_monitor_calls_momentum_provider_once(self):
        provider = Mock(return_value=momentum_scan())

        build_monitor_report(
            momentum_provider=provider,
            grid_analyzer=fake_grid,
            audit_provider=fake_audit,
        )

        provider.assert_called_once()

    def test_unknown_and_provisional_momentum_have_no_action(self):
        for status in (RunStatus.UNKNOWN, RunStatus.PROVISIONAL):
            with self.subTest(status=status):
                report = build_monitor_report(
                    momentum_provider=lambda status=status, **_: momentum_scan(status),
                    grid_analyzer=fake_grid,
                    audit_provider=fake_audit,
                )
                self.assertEqual(report["momentum"]["status"], status.value)
                self.assertIsNone(report["advice"]["momentum_action"])

    def test_positive_grid_score_is_preserved(self):
        report = build_monitor_report(
            momentum_provider=lambda **_: momentum_scan(RunStatus.NO_SIGNAL),
            grid_analyzer=lambda code: {"code": code, "status": "ok", "score": 3},
            audit_provider=fake_audit,
        )

        self.assertEqual(report["grid"][0]["score"], 3)

    def test_grid_error_is_unknown_and_excluded_from_groups(self):
        report = build_monitor_report(
            momentum_provider=lambda **_: momentum_scan(RunStatus.NO_SIGNAL),
            grid_analyzer=Mock(side_effect=RuntimeError("offline")),
            audit_provider=fake_audit,
        )

        self.assertTrue(report["grid"])
        self.assertTrue(all(item["status"] == "unknown" for item in report["grid"]))
        self.assertTrue(all(item["score"] is None for item in report["grid"]))
        self.assertEqual(report["grid_groups"], {"stop": [], "caution": [], "ok": []})

    def test_grid_module_internal_import_error_keeps_dependency_name(self):
        missing = ModuleNotFoundError(
            "No module named 'provider_dependency'", name="provider_dependency"
        )
        with (
            patch("importlib.import_module", side_effect=missing),
            patch.object(strategy_monitor, "load_grid_etfs", return_value=["159920"]),
        ):
            report = build_monitor_report(
                include_momentum=False,
                include_audit=False,
            )

        self.assertEqual(report["grid"][0]["status"], "unknown")
        self.assertIn("provider_dependency", report["grid"][0]["error"])

    def test_audit_module_internal_import_error_keeps_dependency_name(self):
        missing = ModuleNotFoundError(
            "No module named 'audit_dependency'", name="audit_dependency"
        )
        with patch("importlib.import_module", side_effect=missing):
            report = build_monitor_report(
                include_momentum=False,
                include_grid=False,
            )

        self.assertEqual(report["risk"]["status"], "unknown")
        self.assertIn("audit_dependency", report["risk"]["error"])

    def test_internal_import_matching_fallback_name_is_not_hidden(self):
        missing = ModuleNotFoundError(
            "No module named 'grid_trading'; provider bootstrap failed",
            name="grid_trading",
        )
        with (
            patch("importlib.import_module", side_effect=missing),
            patch.object(strategy_monitor, "load_grid_etfs", return_value=["159920"]),
        ):
            report = build_monitor_report(
                include_momentum=False,
                include_audit=False,
            )

        self.assertIn("provider bootstrap failed", report["grid"][0]["error"])

    def test_missing_tools_parent_falls_back_to_top_level_provider(self):
        top_level_module = Mock(analyze_trend=lambda code: {
            "code": code,
            "status": "ok",
            "score": 2,
        })

        def import_module(name):
            if name == "tools.grid_trading":
                raise ModuleNotFoundError("No module named 'tools'", name="tools")
            return top_level_module

        with (
            patch("importlib.import_module", side_effect=import_module),
            patch.object(strategy_monitor, "load_grid_etfs", return_value=["159920"]),
        ):
            report = build_monitor_report(
                include_momentum=False,
                include_audit=False,
            )

        self.assertEqual(report["grid"][0]["status"], "ok")
        self.assertEqual(report["grid"][0]["score"], 2)

    def test_risk_fields_come_from_audit_provider(self):
        report = build_monitor_report(
            momentum_provider=lambda **_: momentum_scan(),
            grid_analyzer=fake_grid,
            audit_provider=fake_audit,
        )

        self.assertEqual(
            report["risk"],
            {
                "status": "ok",
                "as_of": "2026-07-29",
                "max_dd_pct": 12.3,
                "sharpe": 1.4,
                "var_95_loss_pct": 1.7,
                "ic_10d": 0.07,
                "ic_20d": 0.03,
            },
        )

    def test_audit_error_does_not_fall_back_to_stale_risk_values(self):
        report = build_monitor_report(
            momentum_provider=lambda **_: momentum_scan(),
            grid_analyzer=fake_grid,
            audit_provider=Mock(side_effect=RuntimeError("audit unavailable")),
        )

        self.assertEqual(report["risk"]["status"], "unknown")
        for field in (
            "as_of", "max_dd_pct", "sharpe", "var_95_loss_pct", "ic_10d", "ic_20d"
        ):
            self.assertIsNone(report["risk"][field])

    def test_human_report_contains_item_headers_and_distribution(self):
        text = render_human_report(
            build_monitor_report(
                momentum_provider=lambda **_: momentum_scan(),
                grid_analyzer=fake_grid,
                audit_provider=fake_audit,
            )
        )

        self.assertIn("159920 恒生ETF", text)
        self.assertIn("强信号: 0", text)
        self.assertIn("中等: 1", text)

    def test_json_cli_is_banner_free_and_strict(self):
        report = {
            "time": "2026-07-29T00:00:00",
            "momentum": {"status": "unknown"},
            "grid": [{"score": math.nan}],
            "risk": {"status": "unknown"},
            "advice": {"momentum_action": None},
        }
        stdout = StringIO()
        with (
            patch("strategy_monitor.build_monitor_report", return_value=report),
            redirect_stdout(stdout),
        ):
            exit_code = main(["--json"])

        self.assertEqual(exit_code, 0)
        self.assertIsNone(json.loads(stdout.getvalue())["grid"][0]["score"])
        self.assertNotIn("双策略监测", stdout.getvalue())
        self.assertNotIn("NaN", stdout.getvalue())

    def test_keyboard_interrupt_returns_130(self):
        stderr = StringIO()
        with (
            patch("strategy_monitor.build_monitor_report", side_effect=KeyboardInterrupt),
            redirect_stderr(stderr),
        ):
            exit_code = main([])

        self.assertEqual(exit_code, 130)
        self.assertIn("已中断", stderr.getvalue())
