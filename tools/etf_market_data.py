#!/usr/bin/env python3
"""Contract-enforced ETF daily market data loading and caching."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_TIMEOUT = 15
_RETRY_COUNT = 3
_RETRY_BASE_DELAY = 2.0
_SCHEMA_VERSION = 2
_VALID_SOURCES = frozenset({"eastmoney", "tencent"})
_ADJUSTMENT = "qfq"
_VOLUME_ADJUSTMENT = "none"
_MAX_DAILY_RETURN = 0.20
_TENCENT_RETURN_TOLERANCE = 0.03
_VERIFICATION_VERSION = 1
_DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"


@dataclass(frozen=True)
class MarketDataManifest:
    schema_version: int
    code: str
    source: str
    adjustment: str
    volume_adjustment: str
    fetched_at: str
    start_date: str
    end_date: str
    bar_count: int
    content_hash: str
    adjustment_verified: bool
    verification_source: str
    verification_version: int
    overlap_start: str
    overlap_end: str
    overlap_count: int
    verification_tolerance: float
    max_return_error: float
    overlap_content_hash: str


@dataclass(frozen=True)
class MarketDataSeries:
    bars: tuple[dict, ...]
    manifest: MarketDataManifest


class MarketDataQualityError(ValueError):
    """Raised when market data cannot satisfy the v2 correctness contract."""


def _normalize_code(code: str) -> str:
    normalized = code.strip().upper()
    for suffix in (".SH", ".SZ", ".BJ"):
        normalized = normalized.removesuffix(suffix)
    if len(normalized) != 6 or not normalized.isdigit():
        raise ValueError(f"非法证券代码: {code!r}")
    return normalized


def _eastmoney_secid(code: str) -> str:
    return ("1." if code.startswith(("5", "6", "9")) else "0.") + code


def _tencent_code(code: str) -> str:
    return ("sh" if code.startswith(("5", "6", "9")) else "sz") + code


def _default_transport(url: str):
    last_error = None
    for attempt in range(_RETRY_COUNT):
        try:
            result = subprocess.run(
                [
                    "/usr/bin/curl",
                    "-sS",
                    "--fail",
                    "--connect-timeout", "10",
                    "--max-time", str(_TIMEOUT),
                    "--noproxy",
                    "*",
                    "-H",
                    "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                    url,
                ],
                capture_output=True,
                timeout=_TIMEOUT + 5,
            )
            if result.returncode == 0 and result.stdout.strip():
                break
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            last_error = ConnectionError(f"行情请求失败: {detail or url}")
        except (subprocess.TimeoutExpired, OSError) as exc:
            last_error = ConnectionError(f"行情请求超时: {url}")
        if attempt < _RETRY_COUNT - 1:
            time.sleep(_RETRY_BASE_DELAY * (2 ** attempt))
    if last_error is not None:
        raise last_error
    try:
        return json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectionError(f"行情响应不是有效 JSON: {url}") from exc


def _request_json(transport: Callable[[str], object], url: str) -> dict:
    payload = transport(url)
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ConnectionError(f"行情响应格式错误: {url}")
    return payload


def _canonical_bars(bars) -> str:
    return json.dumps(list(bars), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_hash(bars) -> str:
    return hashlib.sha256(_canonical_bars(bars).encode("utf-8")).hexdigest()


def _parse_eastmoney(payload: dict) -> list[dict]:
    rows = (payload.get("data") or {}).get("klines") or []
    bars = []
    for row in rows:
        fields = row.split(",") if isinstance(row, str) else row
        if len(fields) < 6:
            raise MarketDataQualityError("东方财富 K 线字段不足")
        try:
            bars.append(
                {
                    "date": str(fields[0]),
                    "open": float(fields[1]),
                    "close": float(fields[2]),
                    "high": float(fields[3]),
                    "low": float(fields[4]),
                    "volume": float(fields[5]),
                }
            )
        except (TypeError, ValueError) as exc:
            raise MarketDataQualityError("东方财富 K 线包含非法数值") from exc
    if not bars:
        raise ConnectionError("东方财富未返回 ETF K 线")
    return bars


def _parse_tencent(payload: dict, symbol: str) -> list[dict]:
    rows = ((payload.get("data") or {}).get(symbol) or {}).get("qfqday") or []
    bars = []
    previous_date = None
    for row in rows:
        if len(row) < 6:
            raise MarketDataQualityError("腾讯 qfqday K 线字段不足")
        try:
            date = str(row[0])
            datetime.strptime(date, "%Y-%m-%d")
            values = [float(row[index]) for index in range(1, 6)]
        except (TypeError, ValueError) as exc:
            raise MarketDataQualityError("腾讯 qfqday 包含非法日期或数值") from exc
        if previous_date is not None and date <= previous_date:
            raise MarketDataQualityError("腾讯 qfqday 日期重复或未升序")
        if not all(math.isfinite(value) for value in values):
            raise MarketDataQualityError("腾讯 qfqday 包含非有限数值")
        open_price, close, high, low, volume = values
        if min(open_price, close, high, low) <= 0 or volume < 0:
            raise MarketDataQualityError("腾讯 qfqday 包含非正价格或负成交量")
        if high < max(open_price, close, low) or low > min(open_price, close, high):
            raise MarketDataQualityError("腾讯 qfqday 包含非法 OHLC")
        bars.append({
            "date": date, "open": open_price, "close": close,
            "high": high, "low": low, "volume": volume,
        })
        previous_date = date
    return bars


def _verify_tencent_overlap(
    primary: list[dict], reference: list[dict]
) -> tuple[str, str, int, float, str]:
    primary_close = {bar["date"]: bar["close"] for bar in primary}
    reference_close = {bar["date"]: bar["close"] for bar in reference}
    dates = sorted(set(primary_close).intersection(reference_close))[-121:]
    required = min(120, len(primary))
    if len(dates) < required:
        raise MarketDataQualityError(
            f"腾讯 qfq 有效重叠不足: 需要 {required} 日，实际 {len(dates)} 日"
        )
    errors = []
    for previous, current in zip(dates, dates[1:]):
        primary_return = primary_close[current] / primary_close[previous] - 1
        reference_return = reference_close[current] / reference_close[previous] - 1
        if not math.isfinite(primary_return) or not math.isfinite(reference_return):
            raise MarketDataQualityError("腾讯 qfq 收益包含非有限数值")
        error = abs(primary_return - reference_return)
        errors.append(error)
        if error > _TENCENT_RETURN_TOLERANCE:
            raise MarketDataQualityError(f"腾讯 qfq 收益校验失败: {current} 误差 {error:.2%}")
    overlap_rows = [
        {"date": date, "primary_close": primary_close[date], "reference_close": reference_close[date]}
        for date in dates
    ]
    return dates[0], dates[-1], len(dates), max(errors, default=0.0), _content_hash(overlap_rows)


def _make_series(
    code: str, bars: list[dict], verification: tuple[str, str, int, float, str]
) -> MarketDataSeries:
    immutable_bars = tuple(bars)
    overlap_start, overlap_end, overlap_count, max_return_error, overlap_content_hash = verification
    manifest = MarketDataManifest(
        schema_version=_SCHEMA_VERSION,
        code=code,
        source="eastmoney",
        adjustment=_ADJUSTMENT,
        volume_adjustment=_VOLUME_ADJUSTMENT,
        fetched_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        start_date=bars[0]["date"],
        end_date=bars[-1]["date"],
        bar_count=len(bars),
        content_hash=_content_hash(immutable_bars),
        adjustment_verified=True,
        verification_source="tencent_qfqday",
        verification_version=_VERIFICATION_VERSION,
        overlap_start=overlap_start,
        overlap_end=overlap_end,
        overlap_count=overlap_count,
        verification_tolerance=_TENCENT_RETURN_TOLERANCE,
        max_return_error=max_return_error,
        overlap_content_hash=overlap_content_hash,
    )
    series = MarketDataSeries(immutable_bars, manifest)
    validate_market_data(series)
    return series


def _make_series_standalone(
    code: str, bars: list[dict], source: str, verification: dict
) -> MarketDataSeries:
    """Build provider-declared qfq data without claiming cross-verification."""
    immutable_bars = tuple(bars)
    manifest = MarketDataManifest(
        schema_version=_SCHEMA_VERSION,
        code=code,
        source=source,
        adjustment=_ADJUSTMENT,
        volume_adjustment=_VOLUME_ADJUSTMENT,
        fetched_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        start_date=bars[0]["date"],
        end_date=bars[-1]["date"],
        bar_count=len(bars),
        content_hash=_content_hash(immutable_bars),
        adjustment_verified=False,
        verification_source=verification.get(
            "verification_source",
            "provider_declared_qfqday" if source == "tencent" else "provider_declared_fqt1",
        ),
        verification_version=verification.get("version", 1),
        overlap_start=verification.get("overlap_start", bars[0]["date"]),
        overlap_end=verification.get("overlap_end", bars[-1]["date"]),
        overlap_count=verification.get("overlap_count", len(bars)),
        verification_tolerance=_TENCENT_RETURN_TOLERANCE,
        max_return_error=verification.get("max_return_error", 0.0),
        overlap_content_hash=verification.get(
            "overlap_content_hash", _content_hash(immutable_bars)
        ),
    )
    series = MarketDataSeries(immutable_bars, manifest)
    validate_market_data(series)
    return series


def validate_market_data(series: MarketDataSeries) -> None:
    """Validate manifest integrity, bar shape, ordering, OHLC, and ETF breaks."""
    manifest = series.manifest
    bars = series.bars
    if manifest.schema_version != _SCHEMA_VERSION:
        raise MarketDataQualityError("缓存 schema 不是 v2")
    if manifest.source not in _VALID_SOURCES or manifest.adjustment != _ADJUSTMENT:
        raise MarketDataQualityError(f"数据源不可信(source={manifest.source})，仅接受 qfq 前复权")
    if manifest.volume_adjustment != _VOLUME_ADJUSTMENT:
        raise MarketDataQualityError("成交量复权口径不明确")
    valid_verification = (
        manifest.adjustment_verified
        and manifest.verification_source == "tencent_qfqday"
    ) or (
        manifest.source == "tencent"
        and not manifest.adjustment_verified
        and manifest.verification_source == "provider_declared_qfqday"
    ) or (
        manifest.source == "eastmoney"
        and not manifest.adjustment_verified
        and manifest.verification_source == "provider_declared_fqt1"
    )
    if (
        not valid_verification
        or manifest.verification_version != _VERIFICATION_VERSION
        or manifest.overlap_count <= 0
        or not manifest.overlap_start
        or not manifest.overlap_end
        or manifest.overlap_start > manifest.overlap_end
        or manifest.verification_tolerance != _TENCENT_RETURN_TOLERANCE
        or not math.isfinite(manifest.max_return_error)
        or manifest.max_return_error < 0
        or manifest.max_return_error > manifest.verification_tolerance
        or len(manifest.overlap_content_hash) != 64
    ):
        raise MarketDataQualityError("前复权交叉验证状态无效")
    if not bars:
        raise MarketDataQualityError("K 线为空")
    if manifest.bar_count != len(bars):
        raise MarketDataQualityError("manifest bar_count 与内容不一致")
    if manifest.start_date != bars[0].get("date") or manifest.end_date != bars[-1].get("date"):
        raise MarketDataQualityError("manifest 日期范围与内容不一致")
    if manifest.content_hash != _content_hash(bars):
        raise MarketDataQualityError("market data content hash 校验失败")

    previous_date = None
    previous_close = None
    for bar in bars:
        if set(("date", "open", "close", "high", "low", "volume")) - set(bar):
            raise MarketDataQualityError("K 线字段不完整")
        date = bar["date"]
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except (TypeError, ValueError) as exc:
            raise MarketDataQualityError(f"非法交易日期: {date!r}") from exc
        if previous_date is not None and date <= previous_date:
            raise MarketDataQualityError(f"交易日期重复或未升序: {date}")

        values = {}
        for field in ("open", "close", "high", "low", "volume"):
            value = bar[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise MarketDataQualityError(f"{date} {field} 不是有限数值")
            values[field] = value
        if any(values[field] <= 0 for field in ("open", "close", "high", "low")):
            raise MarketDataQualityError(f"{date} 价格必须为正值")
        if values["volume"] < 0:
            raise MarketDataQualityError(f"{date} 成交量不能为负值")
        if values["high"] < max(values["open"], values["close"], values["low"]):
            raise MarketDataQualityError(f"{date} 非法 OHLC: high 低于其他价格")
        if values["low"] > min(values["open"], values["close"], values["high"]):
            raise MarketDataQualityError(f"{date} 非法 OHLC: low 高于其他价格")
        if previous_close is not None:
            daily_return = values["close"] / previous_close - 1
            if abs(daily_return) > _MAX_DAILY_RETURN:
                raise MarketDataQualityError(
                    f"{date} 出现未解释的绝对单日收益 >20%: {daily_return:.2%}"
                )
        previous_date = date
        previous_close = values["close"]


def _load_cache(path: Path, code: str, count: int, as_of: str | None) -> MarketDataSeries | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        manifest = MarketDataManifest(**payload["manifest"])
        series = MarketDataSeries(tuple(payload["bars"]), manifest)
        validate_market_data(series)
        if manifest.code != code or manifest.bar_count < count:
            return None
        if as_of is not None:
            if manifest.end_date < as_of:
                return None
            return truncate_series(series, as_of)
        return series
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, MarketDataQualityError):
        return None


def _write_cache(path: Path, series: MarketDataSeries) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"manifest": asdict(series.manifest), "bars": list(series.bars)}
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def load_etf_series(
    code: str,
    count: int = 2000,
    adjustment: str = "qfq",
    cache_dir: str | None = None,
    transport=None,
    as_of: str | None = None,
) -> MarketDataSeries:
    """Load a qfq ETF series with multi-source fallback.

    Source priority: East Money (primary) → Tencent (standalone fallback)
    Both sources generate validated v2 caches.  Tencent data undergoes the same
    quality checks (single-day return, OHLC, hash) as East Money.
    """
    code = _normalize_code(code)
    if count <= 0:
        raise ValueError("count 必须为正整数")
    if adjustment != _ADJUSTMENT:
        raise MarketDataQualityError("仅允许明确的 qfq 前复权数据")

    target_date = as_of
    try:
        if target_date is not None:
            datetime.strptime(target_date, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"非法 as_of: {as_of!r}") from exc

    directory = Path(cache_dir) if cache_dir is not None else _DEFAULT_CACHE_DIR
    request = transport or _default_transport

    # ── 1. check v2 caches (any source) ──
    cache_eastmoney = directory / f"etf_v2_eastmoney_{code}_{adjustment}_{count}.json"
    cache_tencent = directory / f"etf_v2_tencent_{code}_{adjustment}_{count}.json"

    for cache_path in (cache_eastmoney, cache_tencent):
        if cache_path.exists():
            cached = _load_cache(cache_path, code, count, target_date)
            if target_date is not None and cached is not None:
                return cached

    # ── 2. try East Money (primary) ──
    secid = _eastmoney_secid(code)
    eastmoney_url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&klt=101&fqt=1&lmt={count}&end=20500101"
        "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56"
    )
    eastmoney_error = None
    try:
        primary = _parse_eastmoney(_request_json(request, eastmoney_url))
        symbol = _tencent_code(code)
        tencent_url = (
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?param={symbol},day,,,{min(count, 130)},qfq"
        )
        reference = _parse_tencent(_request_json(request, tencent_url), symbol)
        try:
            verification = _verify_tencent_overlap(primary, reference)
            series = _make_series(code, primary, verification)
        except MarketDataQualityError as verification_error:
            if reference:
                raise
            verification = {
                "verification_source": "provider_declared_fqt1",
                "version": 1,
                "overlap_start": primary[0]["date"],
                "overlap_end": primary[-1]["date"],
                "overlap_count": len(primary),
                "overlap_content_hash": _content_hash(primary),
            }
            series = _make_series_standalone(
                code, primary, "eastmoney", verification
            )
        if target_date is not None and series.manifest.end_date < target_date:
            raise MarketDataQualityError(
                f"主源未覆盖请求截止日 {target_date}: 最新 {series.manifest.end_date}"
            )
        _write_cache(cache_eastmoney, series)
        return series
    except MarketDataQualityError:
        raise
    except Exception as exc:
        eastmoney_error = exc

    # ── 3. try Tencent (standalone fallback) ──
    try:
        symbol = _tencent_code(code)
        tencent_url = (
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?param={symbol},day,,,{count},qfq"
        )
        bars = _parse_tencent(_request_json(request, tencent_url), symbol)
        if not bars:
            raise ConnectionError("腾讯未返回 K 线数据")
        # standalone verification: same quality checks as East Money
        verification = {
            "source": "tencent", "version": 1,
            "overlap_start": bars[0]["date"], "overlap_end": bars[-1]["date"],
            "overlap_count": len(bars), "overlap_content_hash": _content_hash(bars),
        }
        series = _make_series_standalone(code, bars, "tencent", verification)
        if target_date is not None and series.manifest.end_date < target_date:
            raise MarketDataQualityError(
                f"腾讯数据未覆盖请求截止日 {target_date}: 最新 {series.manifest.end_date}"
            )
        validate_market_data(series)
        _write_cache(cache_tencent, series)
        return series
    except MarketDataQualityError:
        raise
    except Exception as exc:
        tencent_error = exc

    # ── 4. last resort: any valid v2 cache ──
    if target_date is not None:
        for cache_path in (cache_eastmoney, cache_tencent):
            if cache_path.exists():
                fallback = _load_cache(cache_path, code, count, target_date)
                if fallback is not None:
                    return fallback

    raise ConnectionError(
        f"前复权数据获取失败(东方财富/腾讯均不可用)且无覆盖区间的合格 v2 缓存: {code}"
    ) from tencent_error


def truncate_series(series: MarketDataSeries, end_date: str) -> MarketDataSeries:
    """Return a validated view ending at or before ``end_date`` with a new hash."""
    try:
        datetime.strptime(end_date, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"非法结束日期: {end_date!r}") from exc
    bars = tuple(bar for bar in series.bars if bar["date"] <= end_date)
    if not bars:
        raise MarketDataQualityError(f"{end_date} 之前没有行情")
    manifest = replace(
        series.manifest,
        start_date=bars[0]["date"],
        end_date=bars[-1]["date"],
        bar_count=len(bars),
        content_hash=_content_hash(bars),
    )
    truncated = MarketDataSeries(bars, manifest)
    validate_market_data(truncated)
    return truncated
