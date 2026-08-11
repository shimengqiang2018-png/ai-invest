# Strategy Production Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复策略监控、行情、信号、回测、审计和账本中全部已确认 correctness 缺陷，使生产输出基于单次完整快照、严格区分未知与无信号，并可用严格 JSON 可靠消费。

**Architecture:** 共享 Python 核心返回结构化对象，CLI 只负责渲染；监控器直接调用核心 API，不再启动 shell 子进程或解析人类文本。行情采用网络优先、合格缓存回退，正式信号要求已收盘且横截面完整；回测、审计和账本共享参数、截止日、费用与现金计息语义。

**Tech Stack:** Python 3.12 标准库、`dataclasses`、`unittest`/`pytest`、现有 `urllib`/`curl` 行情传输与 JSON CLI。

## Global Constraints

- 不改变 4-ETF 选池、RSRS 五条件阈值或默认 `rsrs_period=20`、`ma_period=20`。
- 默认 `cash_return_rate=0`；非零现金收益按每个交易日 `cash * rate / 252` 计入。
- 默认网络优先；仅当网络失败时回退到通过 schema、hash、OHLC、复权和截止日校验的 v2 缓存。
- 当日未完成 bar 只能生成 `provisional`，不得进入正式买入建议。
- 正式动量排名要求候选池所有成员成功且数据截止日一致。
- `unknown` 不得降级为 `no_signal`；失败时不得生成买卖动作。
- 所有 `--json` stdout 必须是单一严格 JSON 文档，禁止 `NaN`、`Infinity` 和前置横幅。
- 不引入数据库、外部服务或新的全局依赖；不执行真实交易。
- 每个任务按 TDD 执行，并在实现后由独立 reviewer 验收；当前目录不是 Git 仓库，计划中的 commit 步骤改为记录变更清单，不执行 `git commit`。

## File Map

- Create `tools/strategy_models.py`: 统一 `RunStatus`、`StrategyError`、扫描/监控结果 dataclass 与严格 JSON 转换。
- Create `tests/test_strategy_monitor.py`: 监控 API、状态语义、单次扫描、纯 JSON 和展示回归测试。
- Modify `tools/etf_market_data.py`: 重试、deadline、缓存回退、非交易日截止日和错误上下文。
- Modify `tests/test_etf_market_data.py`: 行情失败路径和缓存语义回归测试。
- Modify `tools/momentum_core.py`: 零成交量基准 fail-closed；配置周期保持显式。
- Modify `tests/test_momentum_core.py`: 零成交量和周期语义测试。
- Modify `tools/momentum_signal.py`: 收盘状态检测、完整横截面、结构化 API 和 CLI 渲染。
- Modify `tests/test_momentum_signal_parity.py`: provisional、完整横截面与非默认周期一致性。
- Modify `tools/strategy_monitor.py`: 直接调用结构化 API、动态审计、严格 JSON 和未知状态。
- Modify `tools/trading_ledger.py`: 交易日/252 现金计息入口及累计值。
- Modify `tests/test_trading_ledger.py`: 零/非零现金收益测试。
- Modify `tools/momentum_etf_backtest.py`: 完整横截面、rolling end、显式 MA 周期、现金计息。
- Modify `tests/test_momentum_backtest.py`: 缺 bar、截止日、周期、现金 NAV 测试。
- Modify `tools/strategy_audit.py`: 回测期内压力场景、统一指标 schema、删除伪采样对比。
- Modify `tests/test_strategy_audit.py`: 审计区间、指标符号和 schema 测试。
- Modify `tools/grid_trading.py`: 结构化趋势 API、严格 JSON、统一最低佣金/税费、止损后估值和配置门禁。
- Modify `tests/test_grid_backtest.py`: JSON、费用、OHLC、止损和配置一致性测试。
- Modify `reports/ETF/ETF-网格+动量双策略方案-20260726.md`: 更新生产门禁、命令语义和重跑结果。
- Create `tests/fixtures/strategy_snapshot.json`: 4-ETF + 网格 ETF 的确定性离线快照。
- Create `tests/test_strategy_e2e.py`: 基于冻结夹具的端到端监控和严格 JSON 验收。

---

### Task 1: 统一运行状态与严格 JSON 模型

**Files:**
- Create: `tools/strategy_models.py`
- Create: `tests/test_strategy_monitor.py`

**Interfaces:**
- Produces: `RunStatus(str, Enum)` with `OK`, `PROVISIONAL`, `UNKNOWN`, `NO_SIGNAL`.
- Produces: `StrategyError(code: str | None, stage: str, source: str | None, message: str)`.
- Produces: `strict_json_dumps(value: object, *, indent: int | None = 2) -> str`.
- Produces: `to_jsonable(value: object) -> object`, converting dataclasses/enums and non-finite floats to `None`.

- [ ] **Step 1: Write failing model and JSON tests**

```python
# tests/test_strategy_monitor.py
import json
import math
import unittest

from strategy_models import RunStatus, StrategyError, strict_json_dumps


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
```

- [ ] **Step 2: Run tests and confirm import failure**

Run: `uv run --with pytest python -m pytest tests/test_strategy_monitor.py -q`

Expected: FAIL because `strategy_models` does not exist.

- [ ] **Step 3: Implement the shared model module**

```python
# tools/strategy_models.py
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
import json
import math


class RunStatus(str, Enum):
    OK = "ok"
    PROVISIONAL = "provisional"
    UNKNOWN = "unknown"
    NO_SIGNAL = "no_signal"


@dataclass(frozen=True)
class StrategyError:
    code: str | None
    stage: str
    source: str | None
    message: str


def to_jsonable(value):
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def strict_json_dumps(value, *, indent=2):
    return json.dumps(
        to_jsonable(value), ensure_ascii=False, indent=indent, allow_nan=False,
    )
```

- [ ] **Step 4: Run model tests**

Run: `uv run --with pytest python -m pytest tests/test_strategy_monitor.py -q`

Expected: PASS.

- [ ] **Step 5: Record task changes**

Record: `tools/strategy_models.py`, `tests/test_strategy_monitor.py`; no commit because the workspace is not a Git repository.

### Task 2: 修复行情重试、缓存回退与非交易日截止日

**Files:**
- Modify: `tools/etf_market_data.py:18-110,368-526`
- Modify: `tests/test_etf_market_data.py`

**Interfaces:**
- Consumes: `StrategyError` only at higher layers; this task continues raising `ConnectionError`/`MarketDataQualityError`.
- Produces: `_default_transport(url: str) -> bytes` that returns on any successful attempt.
- Produces: `latest_completed_trading_date(as_of: str | None, bars: tuple[dict, ...]) -> str` through cache validation logic, not a market-calendar dependency.
- Produces: `load_etf_series(..., as_of=None)` with network-first and validated-cache fallback.

- [ ] **Step 1: Add failing retry and cache tests**

```python
class TransportRetryTests(unittest.TestCase):
    def test_successful_retry_discards_previous_error(self):
        responses = [FakeCurl(1, b"", b"temporary"), FakeCurl(0, b'{"ok":1}', b"")]
        with patch.object(market_data.subprocess, "run", side_effect=responses), \
             patch.object(market_data.time, "sleep"):
            self.assertEqual(market_data._default_transport("https://example.test"), b'{"ok":1}')


class MarketDataContractTests(unittest.TestCase):
    def test_default_mode_falls_back_to_valid_cache_after_network_failure(self):
        cached = self.write_valid_cache(end_date="2026-07-29")
        with self.assertRaisesRegex(AssertionError, "transport should be attempted first"):
            pass
        loaded = load_etf_series(
            "159920", count=300, cache_dir=self.temp_dir,
            transport=always_fail_transport,
        )
        self.assertEqual(loaded.manifest.content_hash, cached.manifest.content_hash)

    def test_weekend_as_of_accepts_latest_prior_trading_day(self):
        loaded = load_etf_series(
            "159920", count=300, as_of="2026-08-02",
            cache_dir=self.temp_dir, transport=weekend_fixture_transport,
        )
        self.assertEqual(loaded.manifest.end_date, "2026-07-31")
```

Implementer must reuse existing fixture/cache helpers in `test_etf_market_data.py`; do not duplicate manifest construction.

- [ ] **Step 2: Run focused tests and confirm failures**

Run: `uv run --with pytest python -m pytest tests/test_etf_market_data.py -q`

Expected: retry test raises the first error; default cache fallback and weekend tests fail under current `target_date` checks.

- [ ] **Step 3: Make transport return immediately on success**

```python
def _default_transport(url: str):
    last_error = None
    for attempt in range(_RETRY_COUNT):
        try:
            result = subprocess.run(...)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            last_error = ConnectionError(
                f"行情请求失败 attempt={attempt + 1}/{_RETRY_COUNT}: {detail or url}"
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            last_error = ConnectionError(
                f"行情请求超时 attempt={attempt + 1}/{_RETRY_COUNT}: {url}"
            )
        if attempt < _RETRY_COUNT - 1:
            time.sleep(_RETRY_BASE_DELAY * (2 ** attempt))
    raise last_error or ConnectionError(f"行情请求失败: {url}")
```

- [ ] **Step 4: Refactor cache selection to network-first fallback**

Add an internal helper with exact contract:

```python
def _validated_cached_series(paths, code, count, as_of):
    candidates = []
    for path in paths:
        if not path.exists():
            continue
        cached = _load_cache(path, code, count, None)
        if cached is None:
            continue
        if as_of is not None:
            cached = truncate_series(cached, as_of)
        candidates.append(cached)
    return max(candidates, key=lambda item: item.manifest.end_date, default=None)
```

Call the network sources first. After both network sources fail, return the validated cached series. For a non-trading `as_of`, accept `end_date <= as_of`; remove checks requiring `end_date >= as_of`. For `as_of=None`, only use the newest valid cache after network failure.

- [ ] **Step 5: Run all market-data tests**

Run: `uv run --with pytest python -m pytest tests/test_etf_market_data.py -q`

Expected: PASS, including existing hash, qfq, OHLC and fail-closed tests.

- [ ] **Step 6: Record task changes**

Record: `tools/etf_market_data.py`, `tests/test_etf_market_data.py`.

### Task 3: 修复动量成交量基准和正式收盘状态

**Files:**
- Modify: `tools/momentum_core.py:11-120`
- Modify: `tools/momentum_signal.py:227-335`
- Modify: `tests/test_momentum_core.py`
- Modify: `tests/test_momentum_signal_parity.py`

**Interfaces:**
- Produces: `determine_market_closed(last_bar_date: str, *, now: datetime | None = None) -> bool`.
- Produces: `scan(pool, momentum_period=20, *, ma_period=20, market_closed=None, now=None, series_by_code=None) -> dict` where result has `status`, `as_of`, `items`, `errors`, `selected`.
- A complete formal scan has `status=ok` or `no_signal`; incomplete pool has `unknown`; intraday has `provisional`.

- [ ] **Step 1: Add failing zero-volume and market-close tests**

```python
def test_zero_historical_volume_baseline_fails_closed(self):
    bars = make_bars(300)
    for bar in bars[-6:-1]:
        bar["volume"] = 0
    bars[-1]["volume"] = 1_000_000
    snapshot = evaluate_momentum_signal("159920", tuple(bars), len(bars) - 1, self.config)
    self.assertFalse(snapshot.metrics["volume_ok"])
    self.assertIsNone(snapshot.metrics["volume_ratio"])


def test_cli_default_marks_current_day_bar_provisional(self):
    result = scan(
        POOL, series_by_code=frozen_series("2026-07-30"),
        now=datetime(2026, 7, 30, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    self.assertEqual(result["status"], "provisional")
    self.assertIsNone(result["selected"])
    self.assertTrue(all(not item["pass"] for item in result["items"]))


def test_missing_pool_member_makes_scan_unknown(self):
    series = frozen_series("2026-07-29")
    del series["513100"]
    result = scan(POOL, series_by_code=series, market_closed=True)
    self.assertEqual(result["status"], "unknown")
    self.assertIsNone(result["selected"])
```

- [ ] **Step 2: Run focused tests and confirm failures**

Run: `uv run --with pytest python -m pytest tests/test_momentum_core.py tests/test_momentum_signal_parity.py -q`

Expected: current `_volume_ratio` returns `1.0`; scan defaults to formal and returns a list rather than a structured envelope.

- [ ] **Step 3: Make volume ratio optional and fail-closed**

```python
def _volume_ratio(bars, index):
    historical = [float(bar["volume"]) for bar in bars[index - 5:index]]
    if len(historical) < 5 or any(volume <= 0 for volume in historical):
        return None
    return float(bars[index]["volume"]) / (sum(historical) / len(historical))
```

Set `volume_ok = volume_ratio is not None and volume_ratio <= config.max_volume_ratio`. Update result rendering to support `None`.

- [ ] **Step 4: Add explicit Shanghai close determination**

```python
from datetime import datetime, time
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def determine_market_closed(last_bar_date, *, now=None):
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if last_bar_date < current.date().isoformat():
        return True
    if last_bar_date > current.date().isoformat():
        return False
    return current.weekday() < 5 and current.time() >= time(15, 5)
```

The five-minute buffer avoids declaring the bar formal during provider finalization.

- [ ] **Step 5: Return one structured scan envelope**

The implementation must:

```python
{
  "status": RunStatus.OK | RunStatus.NO_SIGNAL | RunStatus.PROVISIONAL | RunStatus.UNKNOWN,
  "as_of": "YYYY-MM-DD" | None,
  "items": [...],
  "errors": [StrategyError(...)],
  "selected": item | None,
  "pool_complete": bool,
}
```

Load all series once, reject mixed `manifest.end_date`, and call `rank_momentum_signals` only when `pool_complete` and formal.

- [ ] **Step 6: Keep CLI compatibility and strict JSON**

`--momentum` maps to `rsrs_period`; add `--ma-period` default 20. `--json` prints only `strict_json_dumps(result)`. Human output iterates `result["items"]`, includes each ETF title and the complete signal distribution.

- [ ] **Step 7: Run focused tests**

Run: `uv run --with pytest python -m pytest tests/test_momentum_core.py tests/test_momentum_signal_parity.py -q`

Expected: PASS.

- [ ] **Step 8: Record task changes**

Record all four files.

### Task 4: 重写综合监控为单次结构化调用

**Files:**
- Modify: `tools/strategy_monitor.py:18-296`
- Modify: `tests/test_strategy_monitor.py`

**Interfaces:**
- Consumes: `momentum_signal.scan(...) -> scan envelope`.
- Consumes: `grid_trading.analyze_trend(code, ...) -> dict` from Task 8; until Task 8 lands, inject `grid_analyzer` in tests.
- Consumes: `strategy_audit.run_audit(...) -> dict` from Task 7; inject `audit_provider` in tests.
- Produces: `build_monitor_report(*, momentum_provider=scan, grid_analyzer=None, audit_provider=None) -> dict`.

- [ ] **Step 1: Add failing single-scan, unknown and display tests**

```python
class StrategyMonitorTests(unittest.TestCase):
    def test_monitor_calls_momentum_provider_once(self):
        provider = Mock(return_value=formal_scan())
        build_monitor_report(momentum_provider=provider, grid_analyzer=fake_grid, audit_provider=fake_audit)
        provider.assert_called_once()

    def test_unknown_momentum_has_no_action(self):
        report = build_monitor_report(
            momentum_provider=lambda **_: unknown_scan(),
            grid_analyzer=fake_grid, audit_provider=fake_audit,
        )
        self.assertEqual(report["momentum"]["status"], "unknown")
        self.assertIsNone(report["advice"]["momentum_action"])

    def test_positive_grid_score_is_preserved(self):
        report = build_monitor_report(
            momentum_provider=lambda **_: no_signal_scan(),
            grid_analyzer=lambda code: {"code": code, "status": "ok", "score": 3},
            audit_provider=fake_audit,
        )
        self.assertEqual(report["grid"][0]["score"], 3)

    def test_human_report_contains_item_headers_and_distribution(self):
        text = render_human_report(build_monitor_report(...))
        self.assertIn("159920 恒生ETF", text)
        self.assertIn("强信号: 0", text)
        self.assertIn("中等: 1", text)
```

- [ ] **Step 2: Run tests and confirm current subprocess design fails**

Run: `uv run --with pytest python -m pytest tests/test_strategy_monitor.py -q`

Expected: FAIL because `build_monitor_report` and render functions do not exist.

- [ ] **Step 3: Implement dependency-injected report builder**

Remove `run()` and all regex parsing. Build the report from direct Python results. Grid failures use `score=None`, `status=unknown`; grouping excludes unknown entries. Advice is generated only for formal `ok`/`no_signal` states.

- [ ] **Step 4: Replace hard-coded audit lines**

Map these exact audit fields into report output:

```python
risk = {
    "as_of": audit["period"]["end"],
    "max_dd_pct": audit["daily_metrics"]["max_dd_pct"],
    "sharpe": audit["daily_metrics"]["sharpe"],
    "var_95_loss_pct": audit["daily_metrics"]["var_95_loss_pct"],
    "ic_10d": audit["ic_ir"]["ic_10d"],
    "ic_20d": audit["ic_ir"]["ic_20d"],
}
```

If audit loading fails, show risk `status=unknown`; do not print stale values.

- [ ] **Step 5: Make JSON mode pure and interruptible**

Print banner only after checking `not args.json`. Catch `KeyboardInterrupt` only at `main()` to print a short stderr message and return exit code 130. There are no child processes to leak.

- [ ] **Step 6: Run monitor tests**

Run: `uv run --with pytest python -m pytest tests/test_strategy_monitor.py -q`

Expected: PASS.

- [ ] **Step 7: Record task changes**

Record `tools/strategy_monitor.py`, `tests/test_strategy_monitor.py`.

### Task 5: 为账本增加交易日现金计息

**Files:**
- Modify: `tools/trading_ledger.py:15-220`
- Modify: `tests/test_trading_ledger.py`

**Interfaces:**
- Produces: `TradingLedger.accrue_cash_return(trading_date: str) -> float`.
- Produces: `TradingLedger.total_cash_return: float` and `last_cash_accrual_date: str | None`.
- Uses `ExecutionConfig.cash_return_rate` as annual rate divided by 252.

- [ ] **Step 1: Add failing cash accrual tests**

```python
class CashReturnTests(unittest.TestCase):
    def test_zero_rate_is_no_op(self):
        ledger = TradingLedger(ExecutionConfig(cash_return_rate=0), cash=100000)
        self.assertEqual(ledger.accrue_cash_return("2026-07-29"), 0)
        self.assertEqual(ledger.cash, 100000)

    def test_positive_rate_compounds_once_per_trading_day(self):
        ledger = TradingLedger(ExecutionConfig(cash_return_rate=0.0252), cash=100000)
        first = ledger.accrue_cash_return("2026-07-29")
        duplicate = ledger.accrue_cash_return("2026-07-29")
        second = ledger.accrue_cash_return("2026-07-30")
        self.assertAlmostEqual(first, 10.0, places=6)
        self.assertEqual(duplicate, 0)
        self.assertAlmostEqual(second, 100010 * 0.0252 / 252, places=6)
        self.assertAlmostEqual(ledger.total_cash_return, first + second, places=6)
```

- [ ] **Step 2: Run tests and confirm missing method**

Run: `uv run --with pytest python -m pytest tests/test_trading_ledger.py -q`

Expected: FAIL with missing `accrue_cash_return`.

- [ ] **Step 3: Implement idempotent daily accrual**

```python
def accrue_cash_return(self, trading_date: str) -> float:
    if self.last_cash_accrual_date == trading_date:
        return 0.0
    rate = float(self.execution.cash_return_rate)
    amount = self.cash * rate / 252 if rate else 0.0
    self.cash += amount
    self.total_cash_return += amount
    self.last_cash_accrual_date = trading_date
    return amount
```

Validate dates are nondecreasing; an older date raises `ValueError`.

- [ ] **Step 4: Run ledger tests**

Run: `uv run --with pytest python -m pytest tests/test_trading_ledger.py -q`

Expected: PASS.

- [ ] **Step 5: Record task changes**

Record both files.

### Task 6: 修复动量回测横截面、周期、截止日和现金 NAV

**Files:**
- Modify: `tools/momentum_etf_backtest.py:401-720,1100-1580`
- Modify: `tests/test_momentum_backtest.py`

**Interfaces:**
- Consumes: `TradingLedger.accrue_cash_return(date)`.
- Produces: `run_backtest(..., momentum_period=20, ma_period=20, ...)`.
- Produces: `run_rolling_backtest(..., end_date: str | None, market_data=None)`.
- Result adds `diagnostics.incomplete_cross_section_dates` and `performance.cash_return_amount`.

- [ ] **Step 1: Strengthen the incomplete cross-section regression test**

```python
def test_missing_cross_section_bar_does_not_create_signal_order(self):
    result = run_backtest(
        pool=self.pool,
        start_date="2020-01-01", end_date="2021-12-31",
        market_data=self.market_data_with_missing_signal_bar,
        quiet=True,
    )
    self.assertIn(self.signal_date, result["diagnostics"]["incomplete_cross_section_dates"])
    self.assertFalse(any(t["signal_date"] == self.signal_date for t in result["trades"]))
```

Ensure the fixture's `effective_start` precedes `signal_date`; assert that explicitly so the test reaches the target branch.

- [ ] **Step 2: Add rolling end, MA parity and cash tests**

```python
def test_rolling_respects_end_date(self):
    results = run_rolling_backtest(
        self.pool, end_date="2024-12-31", market_data=self.snapshot,
        window_months=12, step_months=3,
    )
    self.assertTrue(all(window["end"] <= "2024-12-31" for window in results))


def test_non_default_rsrs_keeps_default_ma20(self):
    result = run_backtest(..., momentum_period=60, ma_period=20, market_data=self.snapshot)
    self.assertEqual(result["strategy"]["rsrs_period"], 60)
    self.assertEqual(result["strategy"]["ma_period"], 20)


def test_cash_return_is_reflected_in_zero_trade_nav(self):
    execution = ExecutionConfig(cash_return_rate=0.0252)
    result = run_backtest(..., execution=execution, market_data=self.no_signal_snapshot)
    days = len(result["daily_nav"])
    self.assertAlmostEqual(result["performance"]["final_nav"], 100000 * (1 + 0.0252 / 252) ** days, places=2)
```

- [ ] **Step 3: Run focused backtest tests and confirm failures**

Run: `uv run --with pytest python -m pytest tests/test_momentum_backtest.py -q`

Expected: at least incomplete cross-section, rolling end and cash tests fail.

- [ ] **Step 4: Require complete cross-section before evaluation**

Before evaluating signals:

```python
missing = [code for code in pool if date not in kline_index.get(code, {})]
if missing:
    incomplete_cross_section_dates.append({"date": date, "missing": missing})
    continue
```

Only then evaluate every member and rank passing signals.

- [ ] **Step 5: Separate RSRS and MA parameters**

Add `ma_period=20` to `run_backtest`, `run_rolling_backtest`, CLI `--ma-period`, batch and audit call sites. Construct `MomentumConfig(rsrs_period=momentum_period, ma_period=ma_period)` everywhere.

- [ ] **Step 6: Accrue cash before daily closing NAV**

Call `ledger.accrue_cash_return(date)` after pending open execution and before closing NAV calculation. Include `ledger.total_cash_return` in performance. With zero rate, fixed baseline must remain unchanged.

- [ ] **Step 7: Thread `end_date` through rolling mode**

Preload/truncate market data once at `end_date`. Build no window ending after it. CLI `--rolling --end` passes `args.end`.

- [ ] **Step 8: Run backtest and parity tests**

Run: `uv run --with pytest python -m pytest tests/test_momentum_backtest.py tests/test_momentum_signal_parity.py -q`

Expected: PASS.

- [ ] **Step 9: Reproduce zero-rate fixed baseline**

Run:

```bash
uv run python tools/momentum_etf_backtest.py \
  --pool 518880,513100,159915,159920 \
  --start 2016-01-01 --end 2026-07-28 \
  --freq biweekly --momentum 20 --ma-period 20 --json
```

Expected baseline: final NAV `409052.64`, total return `309.05%`, annual return `22.55%`, 173 trades, subject only to explicit corrections from complete-cross-section behavior. If values differ, record exact changed signal dates and cause before proceeding.

- [ ] **Step 10: Record task changes**

Record both files and baseline comparison.

### Task 7: 修复审计区间与统一动态指标 schema

**Files:**
- Modify: `tools/strategy_audit.py:250-450, main`
- Modify: `tests/test_strategy_audit.py`

**Interfaces:**
- Produces: `run_audit(*, end_date=None, market_data=None, backtest_result=None) -> dict`.
- Output includes `period`, `daily_metrics`, `ic_ir`, `stress_test`; removes mislabeled sampling comparison.
- `daily_metrics.var_95_loss_pct` and `cvar_95_loss_pct` are positive loss magnitudes.

- [ ] **Step 1: Add failing period/schema tests**

```python
def test_stress_scenarios_are_inside_effective_backtest_period(self):
    result = run_audit(backtest_result=self.result_with_prewarm_data)
    start = result["period"]["start"]
    end = result["period"]["end"]
    for scenario in result["stress_test"]["scenarios"]:
        self.assertGreaterEqual(scenario["start"], start)
        self.assertLessEqual(scenario["end"], end)


def test_audit_schema_uses_positive_loss_magnitudes(self):
    result = run_audit(backtest_result=self.result)
    self.assertGreaterEqual(result["daily_metrics"]["var_95_loss_pct"], 0)
    self.assertGreaterEqual(result["daily_metrics"]["cvar_95_loss_pct"], 0)
    self.assertNotIn("frequency_comparison", result)
```

- [ ] **Step 2: Run audit tests and confirm failures**

Run: `uv run --with pytest python -m pytest tests/test_strategy_audit.py -q`

Expected: stress scenario can precede effective start; old schema lacks positive loss fields and retains duplicate comparison.

- [ ] **Step 3: Truncate scenario inputs to period**

Build each asset's scenario bars with:

```python
period_bars = [bar for bar in bars if period_start <= bar["date"] <= period_end]
```

Return explicit `start` and `end` fields rather than requiring callers to parse a display string.

- [ ] **Step 4: Remove pseudo frequency comparison and normalize metrics**

Delete the block comparing two computations over the same `daily_nav`. Preserve raw return quantiles only internally; public JSON reports positive loss magnitudes:

```python
"var_95_loss_pct": round(abs(var_95_return) * 100, 2),
"var_99_loss_pct": round(abs(var_99_return) * 100, 2),
"cvar_95_loss_pct": round(abs(cvar_95_return) * 100, 2),
```

- [ ] **Step 5: Make CLI strict JSON and end-aware**

`--json` prints only `strict_json_dumps(run_audit(end_date=args.end))`. Human mode renders the same object. Ensure `args.end` reaches market loading and backtest.

- [ ] **Step 6: Run audit and monitor tests**

Run: `uv run --with pytest python -m pytest tests/test_strategy_audit.py tests/test_strategy_monitor.py -q`

Expected: PASS and monitor fields equal audit fixture fields.

- [ ] **Step 7: Record task changes**

Record both files.

### Task 8: 修复网格结构化趋势、费用、止损估值与 JSON 契约

**Files:**
- Modify: `tools/grid_trading.py:1909-2735,2738-3040`
- Modify: `tests/test_grid_backtest.py`

**Interfaces:**
- Produces: `analyze_trend(etf_code: str, *, market_data=None) -> dict` with `status`, `score: int | None`, `bb_width`, `ma_state`, `message`, `error`.
- Produces: `run_grid_backtest(..., execution: ExecutionConfig) -> dict` using `minimum_commission` and `etf_tax_rate` on every trade.
- Produces: `build_grid_backtest_payload(result, ...) -> dict` for both CLI renderers.
- Produces: `validate_live_grid_state(etf_code, config, trigger_ledger) -> list[str]`.

- [ ] **Step 1: Add failing trend and strict JSON tests**

```python
def test_positive_trend_score_is_structured_integer(self):
    result = analyze_trend("512880", market_data=self.sideways_fixture)
    self.assertEqual(result["status"], "ok")
    self.assertEqual(result["score"], 3)


def test_grid_json_is_strict_and_has_no_human_prefix(self):
    completed = run_cli("backtest", "512880", "--json", fixture=self.fixture)
    payload = json.loads(completed.stdout, parse_constant=lambda value: self.fail(value))
    self.assertEqual(payload["meta"]["etf_code"], "512880")
    self.assertNotIn("正在回测", completed.stdout)
```

- [ ] **Step 2: Add failing cost and stop-loss valuation tests**

```python
def test_every_small_trade_pays_minimum_commission(self):
    execution = ExecutionConfig(minimum_commission=5)
    result = run_grid_backtest(..., execution=execution)
    self.assertTrue(all(t["commission"] >= 5 for t in result["trades"]))


def test_sell_tax_is_deducted_from_grid_sell(self):
    execution = ExecutionConfig(etf_tax_rate=0.001)
    result = run_grid_backtest(..., execution=execution)
    sell = next(t for t in result["trades"] if t["action"] == "sell")
    self.assertGreater(sell["tax"], 0)


def test_stop_loss_continues_daily_valuation_through_end(self):
    result = run_grid_backtest(self.crash_then_recover_closes, self.dates, ...)
    self.assertEqual(result["actual_end_date"], self.dates[-1])
    self.assertEqual(len(result["equity_curve"]), len(self.dates))
```

- [ ] **Step 3: Run grid tests and confirm failures**

Run: `uv run --with pytest python -m pytest tests/test_grid_backtest.py -q`

Expected: strict JSON, minimum commission/tax and stop-loss continuation tests fail.

- [ ] **Step 4: Centralize quote functions in grid execution**

Use helpers equivalent to `TradingLedger.quote_buy/quote_sell` for every initial, grid and stop-loss trade:

```python
commission = max(float(execution.minimum_commission), gross * comm_rate)
tax = gross * float(execution.etf_tax_rate)  # sell only
net = gross - commission - tax
```

Add `tax` to trade records and totals. OHLC two-path simulations must call the same helpers.

- [ ] **Step 5: Continue valuation after stop loss**

After liquidation, disable further grid triggers but append closing NAV for every remaining date using remaining base shares plus cash. Do not truncate `equity_curve` or `actual_end_date`.

- [ ] **Step 6: Extract structure before rendering**

`analyze_trend` computes values without printing. `cmd_trend` renders it. `cmd_backtest` builds payload first; JSON mode prints only strict JSON, human mode prints the report. Map infinite profit factor to `None` plus `profit_factor_status="no_losing_trades"`.

- [ ] **Step 7: Add live state fail-closed validation**

Compare configured `shares_per_grid`/initial position with trigger ledger state. Return explicit mismatch messages. Commands `table/status/trigger/risk/pnl` must stop before advice when validation errors exist. Tests use a 6,800-share snapshot against stale config and assert nonzero command status/no advice.

- [ ] **Step 8: Run grid and monitor tests**

Run: `uv run --with pytest python -m pytest tests/test_grid_backtest.py tests/test_strategy_monitor.py -q`

Expected: PASS.

- [ ] **Step 9: Record task changes**

Record both files.

### Task 9: 建立冻结快照端到端验收

**Files:**
- Create: `tests/fixtures/strategy_snapshot.json`
- Create: `tests/test_strategy_e2e.py`
- Modify: `tools/strategy_monitor.py` only if fixture injection needs a public parameter.

**Interfaces:**
- Consumes all public APIs from Tasks 1-8.
- Produces no production API; establishes end-to-end contract.

- [ ] **Step 1: Create a compact deterministic fixture**

The fixture must contain at least 300 valid qfq bars for each momentum code and enough bars for each grid code, plus manifests. Generate it from existing test builders, not live network. Store fixed dates ending `2026-07-29` and content hashes produced by `_content_hash`.

- [ ] **Step 2: Write end-to-end tests before wiring fixture support**

```python
class StrategyEndToEndTests(unittest.TestCase):
    def test_monitor_uses_one_frozen_snapshot_without_network(self):
        snapshot = load_fixture("strategy_snapshot.json")
        with patch("etf_market_data._default_transport", side_effect=AssertionError("network")):
            report = build_monitor_report(market_data=snapshot, now=self.after_close)
        self.assertIn(report["momentum"]["status"], {"ok", "no_signal"})
        self.assertTrue(report["momentum"]["pool_complete"])

    def test_all_json_clis_emit_single_strict_document(self):
        for command in self.json_commands(snapshot_path=self.fixture_path):
            completed = subprocess.run(command, capture_output=True, text=True, check=True)
            json.loads(completed.stdout, parse_constant=lambda value: self.fail(value))
            self.assertEqual(completed.stderr, "")
```

Use internal test-only injection or a shared loader argument; do not add a public trading CLI flag solely for tests.

- [ ] **Step 3: Run E2E tests and confirm missing injection**

Run: `uv run --with pytest python -m pytest tests/test_strategy_e2e.py -q`

Expected: FAIL until monitor/audit/grid APIs accept supplied snapshot objects.

- [ ] **Step 4: Wire snapshot injection through public Python APIs**

Pass `market_data` to momentum, grid and audit providers. No layer may refetch when supplied data exists.

- [ ] **Step 5: Run complete test suite**

Run: `uv run --with pytest python -m pytest -q`

Expected: all existing and new tests pass; report exact count.

- [ ] **Step 6: Run AST/compile verification**

Run:

```bash
python3 -m py_compile \
  tools/strategy_models.py tools/etf_market_data.py tools/momentum_core.py \
  tools/momentum_signal.py tools/strategy_monitor.py tools/trading_ledger.py \
  tools/momentum_etf_backtest.py tools/strategy_audit.py tools/grid_trading.py
```

Expected: exit 0.

- [ ] **Step 7: Record task changes**

Record fixture, E2E test and any injection changes.

### Task 10: 重跑固定结果并更新策略文档

**Files:**
- Modify: `reports/ETF/ETF-网格+动量双策略方案-20260726.md`
- Create: `/Users/shimengqiang/IdeaProjects/docs/ai-invest/20260729_策略生产链路修复验证_v1.md`

**Interfaces:**
- Consumes final CLI JSON schemas and test results.
- Produces updated operational documentation and archived verification evidence.

- [ ] **Step 1: Run strict JSON commands and save outputs outside the repo**

```bash
python3 tools/momentum_signal.py --pool 518880,513100,159915,159920 --json > /tmp/momentum-signal.json
python3 tools/strategy_monitor.py --json > /tmp/strategy-monitor.json
python3 tools/momentum_etf_backtest.py --pool 518880,513100,159915,159920 --start 2016-01-01 --end 2026-07-28 --freq biweekly --momentum 20 --ma-period 20 --json > /tmp/momentum-backtest.json
python3 tools/strategy_audit.py --end 2026-07-28 --json > /tmp/strategy-audit.json
python3 tools/grid_trading.py backtest 512880 --start 2020-05-25 --end 2026-07-28 --shares 300 --json > /tmp/grid-backtest.json
```

- [ ] **Step 2: Strictly parse every output**

```bash
python3 - <<'PY'
import json
for path in [
    "/tmp/momentum-signal.json", "/tmp/strategy-monitor.json",
    "/tmp/momentum-backtest.json", "/tmp/strategy-audit.json",
    "/tmp/grid-backtest.json",
]:
    with open(path) as handle:
        json.load(handle, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    print("PASS", path)
PY
```

Expected: five PASS lines.

- [ ] **Step 3: Compare corrected outputs with documented baselines**

Write a comparison table with old/new values and exact reason for each change. If complete-cross-section or grid fee fixes alter results, do not preserve old numbers; update them and state the correction.

- [ ] **Step 4: Update the strategy document**

Replace obsolete production gate bullets with resolved status and remaining external limitations. Update command descriptions: provisional semantics, strict JSON, network/cache policy, explicit MA period and rolling end behavior. Update all metrics from parsed machine output only.

- [ ] **Step 5: Write the required analysis archive**

The archive must include: background, core changes, test evidence, before/after metrics, remaining cautions and references. Use filename `/Users/shimengqiang/IdeaProjects/docs/ai-invest/20260729_策略生产链路修复验证_v1.md`.

- [ ] **Step 6: Run final verification**

Run:

```bash
uv run --with pytest python -m pytest -q
python3 -m py_compile tools/*.py
```

Then manually confirm the five JSON files still parse after documentation changes.

Expected: zero test failures, compile exit 0, five JSON parse passes.

- [ ] **Step 7: Record final change inventory**

List every modified/created file and verification command. Do not commit or push because this workspace is not a Git repository and the user did not request publishing.
