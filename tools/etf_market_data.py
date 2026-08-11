#!/usr/bin/env python3
"""Contract-enforced ETF daily market data loading and caching."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

_TIMEOUT = 15
_RETRY_COUNT = 4
_RETRY_BASE_DELAY = 3.0
_SCHEMA_VERSION = 3
_VALID_SOURCES = frozenset({"eastmoney", "tencent", "sina"})
# 数据源优先级：东财前复权(fqt=1)为主，腾讯 qfq 为参考/兜底，新浪为最后网络兜底。
# 新浪对 ETF 返回不复权序列（工具只能修正 >25% 的份额折算，无法处理分红），
# 因此新浪序列永远不允许作为已交叉验证的主源。
_SOURCE_PRIORITY = ("eastmoney", "tencent", "sina")
_ADJUSTMENT = "qfq"
_VOLUME_ADJUSTMENT = "none"
_MAX_DAILY_RETURN = 0.20
_REFERENCE_RETURN_TOLERANCE = 0.005      # 参考源逐日收益误差上限（0.5%）
_MAX_RATIO_DRIFT = 0.02                  # 参考源全历史价格比漂移上限（2%）
_HARD_FAIL_RETURN_ERROR = 0.10           # 单日收益误差超过 10% → 硬失败
_HARD_FAIL_RATIO_DRIFT = 0.20            # 价格比漂移超过 20% → 硬失败
_MIN_REFERENCE_OVERLAP = 0.8             # 参考源重叠覆盖主序列的比例下限
_VERIFICATION_VERSION = 2
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"

# --- Rate limiting ---
_RATE_LIMIT_ENABLED = True
_MIN_REQUEST_INTERVAL = 1.0        # Minimum seconds between HTTP requests
_RATE_LIMIT_JITTER = 0.3           # Max additional random jitter
_last_request_time = 0.0

# --- Sina Finance ---
_SINA_MAX_DATALEN = 2000           # Sina API max bars per request
_SINA_INCREMENTAL_DAYS = 90        # Calendar days to fetch for incremental updates


def _throttle() -> None:
    """Serialize HTTP requests to avoid triggering API rate limits."""
    global _last_request_time
    if not _RATE_LIMIT_ENABLED:
        return
    elapsed = time.time() - _last_request_time
    if elapsed < _MIN_REQUEST_INTERVAL:
        jitter = random.uniform(0, _RATE_LIMIT_JITTER)
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed + jitter)
    _last_request_time = time.time()


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
    max_ratio_deviation: float = 0.0


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


def _exchange_symbol(code: str) -> str:
    """Return sh/sz prefix + 6-digit code.  Shared by Sina and Tencent."""
    return ("sh" if code.startswith(("5", "6", "9")) else "sz") + code


def _sina_url(symbol: str, datalen: int = 2000) -> str:
    """Build Sina Finance daily K-line API URL.

    Symbol format: sh518880, sz159915.
    Returns qfq (前复权) daily bars, up to 2000 entries (~8 years).
    Volume is in 股 (shares) — normalised to 手 (lots) in the parser.
    """
    return (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={datalen}"
    )


def _eastmoney_url(code: str, beg: str = "20130101") -> str:
    """Build Eastmoney 前复权(fqt=1) daily K-line API URL (full history)."""
    market = "1" if code.startswith(("5", "6", "9")) else "0"
    return (
        "http://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={market}.{code}&klt=101&fqt=1&beg={beg}&end=20500101"
        "&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57"
    )


def _tencent_url(symbol: str, count: int) -> str:
    return (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={symbol},day,,,{count},qfq"
    )


def _tencent_url_range(symbol: str, start: str, end: str, count: int = 1600) -> str:
    """Tencent qfq daily K-line for an explicit date range (full-history paging)."""
    return (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={symbol},day,{start},{end},{count},qfq"
    )


def _default_transport(url: str) -> bytes:
    _throttle()
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
                return result.stdout
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            last_error = ConnectionError(
                f"行情请求失败 attempt={attempt + 1}/{_RETRY_COUNT}: {detail or url}"
            )
        except (subprocess.TimeoutExpired, OSError):
            last_error = ConnectionError(
                f"行情请求超时 attempt={attempt + 1}/{_RETRY_COUNT}: {url}"
            )
        if attempt < _RETRY_COUNT - 1:
            time.sleep(_RETRY_BASE_DELAY * (2 ** attempt))
    raise last_error or ConnectionError(f"行情请求失败: {url}")


def _request_json(transport: Callable[[str], object], url: str) -> dict:
    payload = transport(url)
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ConnectionError(f"行情响应格式错误: {url}")
    return payload


def _request_sina_json(transport: Callable[[str], object], url: str) -> list:
    """Fetch Sina JSON endpoint.  Sina returns a JSON array directly;
    may return a JSON object on errors."""
    payload = transport(url)
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, dict):
        raise ConnectionError(f"新浪财经 API 错误: {payload}")
    if not isinstance(payload, list):
        raise ConnectionError(f"新浪财经响应格式错误: {url}")
    return payload


def _canonical_bars(bars) -> str:
    return json.dumps(list(bars), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_hash(bars) -> str:
    return hashlib.sha256(_canonical_bars(bars).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

_SPLIT_RETURN_THRESHOLD = 0.25  # Daily return above which a split/merge is assumed


def _apply_qfq_correction(bars: list[dict]) -> list[dict]:
    """Apply 前复权 correction to unadjusted Sina ETF data.

    Sina's K-line API returns unadjusted (不复权) data.  When an ETF
    undergoes a share split or merge the unadjusted series shows a price
    discontinuity (>25 % single-day return).  This function detects such
    events and adjusts all earlier bars so the full series is qfq-equivalent
    and passes the 20 % daily-return guard in validate_market_data.
    """
    if len(bars) < 2:
        return bars
    adjusted = [dict(b) for b in bars]
    for i in range(len(adjusted) - 1, 0, -1):
        curr_close = adjusted[i]["close"]
        prev_close = adjusted[i - 1]["close"]
        if prev_close == 0:
            continue
        daily_return = curr_close / prev_close - 1.0
        if abs(daily_return) > _SPLIT_RETURN_THRESHOLD:
            split_ratio = curr_close / prev_close
            for j in range(i):
                for field in ("open", "close", "high", "low"):
                    adjusted[j][field] *= split_ratio
                adjusted[j]["volume"] /= split_ratio
    return adjusted


def _parse_sina(payload: list, symbol: str) -> list[dict]:
    """Parse Sina Finance daily K-line JSON response.

    Input: JSON array of {day, open, high, low, close, volume}.
    Volume is normalised from 股 (shares) to 手 (lots) by dividing by 100.
    Unadjusted data is corrected to qfq via split detection.
    """
    if not isinstance(payload, list):
        raise ConnectionError(f"新浪财经 K 线响应格式错误: {symbol}")
    if not payload:
        raise ConnectionError(f"新浪财经未返回 ETF K 线: {symbol}")

    bars: list[dict] = []
    previous_date: str | None = None
    for row in payload:
        if not isinstance(row, dict):
            raise MarketDataQualityError(f"新浪财经 K 线条目格式错误: {symbol}")
        try:
            date = str(row["day"])
            datetime.strptime(date, "%Y-%m-%d")
            open_price = float(row["open"])
            high_price = float(row["high"])
            low_price = float(row["low"])
            close_price = float(row["close"])
            volume_raw = float(row["volume"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MarketDataQualityError(
                f"新浪财经 K 线包含非法字段或数值: {symbol} {row.get('day', '?')}"
            ) from exc

        if not all(math.isfinite(v) for v in (open_price, close_price, high_price, low_price, volume_raw)):
            raise MarketDataQualityError(f"新浪财经 K 线包含非有限数值: {symbol} {date}")
        if min(open_price, close_price, high_price, low_price) <= 0 or volume_raw < 0:
            raise MarketDataQualityError(f"新浪财经 K 线包含非正价格或负成交量: {symbol} {date}")
        if high_price < max(open_price, close_price, low_price) or low_price > min(open_price, close_price, high_price):
            raise MarketDataQualityError(f"新浪财经 K 线包含非法 OHLC: {symbol} {date}")
        if previous_date is not None and date <= previous_date:
            raise MarketDataQualityError(f"新浪财经 K 线日期重复或未升序: {symbol} {date}")

        volume = volume_raw / 100.0  # 股 → 手

        bars.append({
            "date": date,
            "open": open_price,
            "close": close_price,
            "high": high_price,
            "low": low_price,
            "volume": volume,
        })
        previous_date = date

    return _apply_qfq_correction(bars)


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


def _parse_eastmoney(payload: dict, code: str) -> list[dict]:
    """Parse Eastmoney fqt=1 (前复权) daily K-line JSON response.

    ``data.klines`` rows are CSV strings:
    date,open,close,high,low,volume,amount
    Volume unit is 手 (lots), matching Tencent/Sina-normalised conventions.
    """
    data = payload.get("data") or {}
    rows = data.get("klines") or []
    if not isinstance(rows, list) or not rows:
        raise ConnectionError(f"东方财富未返回 ETF K 线: {code}")
    bars: list[dict] = []
    previous_date: str | None = None
    for row in rows:
        fields = row.split(",")
        if len(fields) < 6:
            raise MarketDataQualityError(f"东方财富 K 线条目字段不足: {code} {row[:40]}")
        try:
            date = str(fields[0])
            datetime.strptime(date, "%Y-%m-%d")
            open_price = float(fields[1])
            close_price = float(fields[2])
            high_price = float(fields[3])
            low_price = float(fields[4])
            volume = float(fields[5])
        except (TypeError, ValueError) as exc:
            raise MarketDataQualityError(
                f"东方财富 K 线包含非法字段或数值: {code} {fields[0]}"
            ) from exc
        if not all(
            math.isfinite(v)
            for v in (open_price, close_price, high_price, low_price, volume)
        ):
            raise MarketDataQualityError(f"东方财富 K 线包含非有限数值: {code} {date}")
        if min(open_price, close_price, high_price, low_price) <= 0 or volume < 0:
            raise MarketDataQualityError(f"东方财富 K 线包含非正价格或负成交量: {code} {date}")
        if high_price < max(open_price, close_price, low_price) or low_price > min(
            open_price, close_price, high_price
        ):
            raise MarketDataQualityError(f"东方财富 K 线包含非法 OHLC: {code} {date}")
        if previous_date is not None and date <= previous_date:
            raise MarketDataQualityError(f"东方财富 K 线日期重复或未升序: {code} {date}")
        bars.append({
            "date": date,
            "open": open_price,
            "close": close_price,
            "high": high_price,
            "low": low_price,
            "volume": volume,
        })
        previous_date = date
    return bars


def _drop_incomplete_last_bar(bars: list[dict], now: datetime | None = None) -> list[dict]:
    """Drop a same-day bar that was captured before the A-share close (15:10).

    Daily K-lines must only contain completed trading days; an intraday snapshot
    of today's bar is a wrong close and would poison backtests/cache freshness.
    """
    if not bars:
        return bars
    current = (now or datetime.now(_SHANGHAI_TZ)).astimezone(_SHANGHAI_TZ)
    last_date = bars[-1]["date"]
    if last_date == current.date().isoformat() and (current.hour, current.minute) < (15, 10):
        return bars[:-1]
    return bars


# ---------------------------------------------------------------------------
# Cross-verification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReferenceVerification:
    """Result of comparing a primary series against an independent qfq source."""

    ok: bool
    overlap_start: str
    overlap_end: str
    overlap_count: int
    max_return_error: float
    max_ratio_deviation: float
    overlap_content_hash: str
    reason: str = ""


def _verify_reference_overlap(
    primary: list[dict],
    reference: list[dict],
    tolerance: float = _REFERENCE_RETURN_TOLERANCE,
    max_drift: float = _MAX_RATIO_DRIFT,
    require_full: bool = True,
) -> ReferenceVerification:
    """Full-history daily-return + level-drift verification against a reference.

    Returns a result object instead of raising so callers can decide between
    fail-closed (huge mismatch) and provider-declared (mild mismatch).
    """
    primary_close = {bar["date"]: bar["close"] for bar in primary}
    reference_close = {bar["date"]: bar["close"] for bar in reference}
    dates = sorted(set(primary_close).intersection(reference_close))
    if not dates:
        return ReferenceVerification(
            False, "", "", 0, 0.0, 0.0, "",
            reason="参考源无重叠日期",
        )
    if require_full:
        required = max(
            int(len(primary) * _MIN_REFERENCE_OVERLAP),
            min(100, len(primary)),
        )
        if len(dates) < required:
            return ReferenceVerification(
                False, dates[0], dates[-1], len(dates), 0.0, 0.0, "",
                reason=f"参考源全历史重叠不足: 需要 {required} 日，实际 {len(dates)} 日",
            )
    else:
        dates = dates[-121:]

    # 价格比漂移检测：正确的 qfq 对，历史任意时点价格比都应≈常数。
    # 若主序列漏掉分红/份额折算，价格比会呈阶梯式漂移，这里直接抓出来。
    ratios = [reference_close[date] / primary_close[date] for date in dates]
    median_ratio = sorted(ratios)[len(ratios) // 2]
    max_deviation = max(
        (abs(ratio / median_ratio - 1) for ratio in ratios), default=0.0
    )

    errors = []
    for previous, current in zip(dates, dates[1:]):
        primary_return = primary_close[current] / primary_close[previous] - 1
        reference_return = reference_close[current] / reference_close[previous] - 1
        if not math.isfinite(primary_return) or not math.isfinite(reference_return):
            return ReferenceVerification(
                False, dates[0], dates[-1], len(dates), 0.0, max_deviation, "",
                reason="收益包含非有限数值",
            )
        error = abs(primary_return - reference_return)
        errors.append(error)
        if error > tolerance:
            return ReferenceVerification(
                False, dates[0], dates[-1], len(dates), error, max_deviation, "",
                reason=f"{current} 收益误差 {error:.2%} > {tolerance:.2%}",
            )
    if max_deviation > max_drift:
        return ReferenceVerification(
            False, dates[0], dates[-1], len(dates), max(errors, default=0.0),
            max_deviation, "",
            reason=f"价格比漂移 {max_deviation:.2%} > {max_drift:.2%}",
        )

    overlap_rows = [
        {"date": date, "primary_close": primary_close[date], "reference_close": reference_close[date]}
        for date in dates
    ]
    return ReferenceVerification(
        True,
        dates[0],
        dates[-1],
        len(dates),
        max(errors, default=0.0),
        round(max_deviation, 6),
        _content_hash(overlap_rows),
    )


def _fetch_tencent_full(
    symbol: str,
    request: Callable[[str], object],
    start: str = "2013-01-01",
) -> list[dict]:
    """Fetch Tencent qfq history by paging date ranges.

    Tencent caps each request at ~640 bars (~2.6 years), so we page with
    2-year windows to cover the full span without gaps.
    """
    windows = (
        (start, "2016-12-31"),
        ("2017-01-01", "2019-12-31"),
        ("2020-01-01", "2022-12-31"),
        ("2023-01-01", "2026-12-31"),
    )
    if start < "2015-01-01":
        windows = (
            (start, "2014-12-31"),
            ("2015-01-01", "2016-12-31"),
            ("2017-01-01", "2018-12-31"),
            ("2019-01-01", "2020-12-31"),
            ("2021-01-01", "2022-12-31"),
            ("2023-01-01", "2024-12-31"),
            ("2025-01-01", "2026-12-31"),
        )
    merged: dict[str, dict] = {}
    for window_start, window_end in windows:
        try:
            payload = _request_json(
                request, _tencent_url_range(symbol, window_start, window_end)
            )
            bars = _parse_tencent(payload, symbol)
        except Exception:
            continue
        for bar in bars:
            merged[bar["date"]] = bar
    bars = [merged[date] for date in sorted(merged)]
    if not bars:
        # 区间分页失败时退化为最近窗口（与旧行为一致）
        payload = _request_json(request, _tencent_url(symbol, 130))
        bars = _parse_tencent(payload, symbol)
    return bars


# ---------------------------------------------------------------------------
# Series construction
# ---------------------------------------------------------------------------

def _make_series(
    code: str,
    bars: list[dict],
    verification: tuple[str, str, int, float, str, float],
) -> MarketDataSeries:
    immutable_bars = tuple(bars)
    (
        overlap_start,
        overlap_end,
        overlap_count,
        max_return_error,
        overlap_content_hash,
        max_ratio_deviation,
    ) = verification
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
        verification_tolerance=_REFERENCE_RETURN_TOLERANCE,
        max_return_error=max_return_error,
        overlap_content_hash=overlap_content_hash,
        max_ratio_deviation=max_ratio_deviation,
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
            "provider_declared_fqt1"
            if source == "eastmoney"
            else "provider_declared_qfqday",
        ),
        verification_version=verification.get("version", 1),
        overlap_start=verification.get("overlap_start", bars[0]["date"]),
        overlap_end=verification.get("overlap_end", bars[-1]["date"]),
        overlap_count=verification.get("overlap_count", len(bars)),
        verification_tolerance=_REFERENCE_RETURN_TOLERANCE,
        max_return_error=verification.get("max_return_error", 0.0),
        overlap_content_hash=verification.get(
            "overlap_content_hash", _content_hash(immutable_bars)
        ),
        max_ratio_deviation=verification.get("max_ratio_deviation", 0.0),
    )
    series = MarketDataSeries(immutable_bars, manifest)
    validate_market_data(series)
    return series


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_market_data(series: MarketDataSeries) -> None:
    """Validate manifest integrity, bar shape, ordering, OHLC, and ETF breaks."""
    manifest = series.manifest
    bars = series.bars
    if manifest.schema_version != _SCHEMA_VERSION:
        raise MarketDataQualityError("缓存 schema 版本不匹配")
    if manifest.source not in _VALID_SOURCES or manifest.adjustment != _ADJUSTMENT:
        raise MarketDataQualityError(f"数据源不可信(source={manifest.source})，仅接受 qfq 前复权")
    if manifest.volume_adjustment != _VOLUME_ADJUSTMENT:
        raise MarketDataQualityError("成交量复权口径不明确")
    valid_verification = (
        manifest.adjustment_verified
        and manifest.verification_source == "tencent_qfqday"
    ) or (
        manifest.source in ("tencent", "sina")
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
        or manifest.verification_tolerance != _REFERENCE_RETURN_TOLERANCE
        or not math.isfinite(manifest.max_return_error)
        or manifest.max_return_error < 0
        or manifest.max_return_error > manifest.verification_tolerance
        or not math.isfinite(manifest.max_ratio_deviation)
        or manifest.max_ratio_deviation < 0
        or (
            manifest.adjustment_verified
            and manifest.max_ratio_deviation > _MAX_RATIO_DRIFT
        )
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


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------

def _load_cache(path: Path, code: str, count: int, as_of: str | None) -> MarketDataSeries | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        manifest = MarketDataManifest(**payload["manifest"])
        series = MarketDataSeries(tuple(payload["bars"]), manifest)
        validate_market_data(series)
        if manifest.code != code or manifest.bar_count < count:
            return None
        if as_of is not None:
            return truncate_series(series, as_of)
        return series
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, MarketDataQualityError):
        return None


def _validated_cached_series(paths, code, count, as_of):
    candidates = []
    for path in paths:
        if not path.exists():
            continue
        cached = _load_cache(path, code, count, None)
        if cached is None:
            continue
        if as_of is not None:
            try:
                cached = truncate_series(cached, as_of)
            except MarketDataQualityError:
                continue
        candidates.append(cached)
    return max(candidates, key=lambda item: item.manifest.end_date, default=None)


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


# ---------------------------------------------------------------------------
# Incremental cache helpers
# ---------------------------------------------------------------------------

def _is_cache_fresh(manifest: MarketDataManifest, max_age_days: int = 1) -> bool:
    """Check if cached data's end_date is within max_age_days of today."""
    try:
        end_date = datetime.strptime(manifest.end_date, "%Y-%m-%d").date()
        today = datetime.now(timezone.utc).date()
        return (today - end_date).days <= max_age_days
    except (TypeError, ValueError):
        return False


def _should_refresh_online(end_date: str, now: datetime | None = None) -> bool:
    """在线模式下是否需要联网补拉最新日 K。

    日 K 只在收盘后才产生今日 bar，因此：
    - 周末/节假日：不强制刷新（周五收盘缓存即最新）；
    - 交易日盘中（<15:10）：不重复拉取（今日 bar 尚未形成）；
    - 交易日收盘后（>=15:10）：缓存缺今日 bar 时才刷新。
    """
    try:
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return True
    current = (now or datetime.now(_SHANGHAI_TZ)).astimezone(_SHANGHAI_TZ)
    today = current.date()
    if today.weekday() >= 5:
        return False
    if (current.hour, current.minute) < (15, 10):
        return False
    return end < today


def _merge_incremental(
    cached: MarketDataSeries, fresh_bars: list[dict]
) -> list[dict] | None:
    """Merge fresh bars into cached series, verifying overlap integrity.

    Returns merged bar list if successful, None if overlap verification fails.
    """
    cached_dates = {bar["date"]: bar for bar in cached.bars}
    fresh_dates = {bar["date"]: bar for bar in fresh_bars}

    overlap_dates = sorted(set(cached_dates) & set(fresh_dates))
    if not overlap_dates:
        return None  # No overlap — cannot verify

    # Verify overlapping bars match (same Sina source, should be near-identical)
    for date in overlap_dates:
        cb = cached_dates[date]
        fb = fresh_dates[date]
        if cb["close"] != 0:
            diff = abs(fb["close"] / cb["close"] - 1)
            if diff > 0.001:  # 0.1% tolerance for same-source data
                return None  # Data mismatch — likely a restatement

    # Merge: cached bars up to overlap_start, then all fresh bars from there onward
    first_overlap = overlap_dates[0]
    merged = [bar for bar in cached.bars if bar["date"] < first_overlap]
    merged.extend(bar for bar in fresh_bars if bar["date"] >= first_overlap)

    # Verify chronological order
    for i in range(1, len(merged)):
        if merged[i]["date"] <= merged[i - 1]["date"]:
            return None

    return merged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_etf_series(
    code: str,
    count: int = 2000,
    adjustment: str = "qfq",
    cache_dir: str | None = None,
    transport=None,
    as_of: str | None = None,
    refresh: bool = False,
) -> MarketDataSeries:
    """Load a qfq ETF series with multi-source fallback.

    Source priority:
      fresh validated cache → Eastmoney fqt=1 full (primary) + Tencent
      full-history cross-verification → Tencent standalone → Sina standalone
      (legacy, qfq-correction only) → newest valid stale cache.

    Eastmoney fqt=1 is the primary provider because its 前复权 series correctly
    handles both dividends and 份额折算; Sina returns unadjusted ETF data and is
    only used as a last-resort fallback (never marked cross-verified).

    ``refresh=True`` skips the fresh-cache shortcut and re-fetches from the
    network (used by manual 刷新数据 / cache rebuilds).
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

    symbol = _exchange_symbol(code)
    cache_paths = {
        "eastmoney": directory / f"etf_v2_eastmoney_{code}_{adjustment}_{count}.json",
        "tencent": directory / f"etf_v2_tencent_{code}_{adjustment}_{count}.json",
        "sina": directory / f"etf_v2_sina_{code}_{adjustment}_{count}.json",
    }

    def _source_rank(source: str) -> int:
        return _SOURCE_PRIORITY.index(source) if source in _SOURCE_PRIORITY else 99

    def _scan_cached(fresh_only: bool):
        """Return (best, largest) validated cached series across sources/counts.

        best = highest-priority fresh-or-valid series with bar_count >= count
        largest = deepest series regardless of count (次新 ETF 兜底)
        """
        best = None  # (rank, end_date, series)
        largest = None
        for path in sorted(
            directory.glob(f"etf_v2_*_{code}_qfq_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            try:
                cached = _load_cache(path, code, 1, None)
            except Exception:  # noqa: BLE001 - 跳过损坏缓存
                continue
            if cached is None:
                continue
            if target_date is not None:
                try:
                    cached = truncate_series(cached, target_date)
                except MarketDataQualityError:
                    continue
            manifest = cached.manifest
            rank = _source_rank(manifest.source)
            if manifest.bar_count >= count:
                if fresh_only and not _is_cache_fresh(manifest):
                    continue
                key = (rank, manifest.end_date)
                # 源优先级 rank 越小越优；同源取最新 end_date
                if best is None or (
                    rank < best[0]
                    or (rank == best[0] and manifest.end_date > best[1])
                ):
                    best = (rank, manifest.end_date, cached)
            elif largest is None or manifest.bar_count > largest.manifest.bar_count:
                largest = cached
        return best, largest

    # ── Helper: write + truncate ──
    def _finalise(series: MarketDataSeries, path: Path) -> MarketDataSeries:
        _write_cache(path, series)
        if target_date is not None:
            series = truncate_series(series, target_date)
        return series

    def _standalone(primary_bars: list[dict], source: str) -> dict:
        return {
            "verification_source": (
                "provider_declared_fqt1" if source == "eastmoney"
                else "provider_declared_qfqday"
            ),
            "version": _VERIFICATION_VERSION,
            "overlap_start": primary_bars[0]["date"],
            "overlap_end": primary_bars[-1]["date"],
            "overlap_count": len(primary_bars),
            "overlap_content_hash": _content_hash(primary_bars),
        }

    def _verify_with_tencent(
        primary_bars: list[dict], require_full: bool = True
    ) -> tuple[tuple | None, float]:
        """Cross-verify primary bars against Tencent qfq full history.

        Returns (verification_6tuple, measured_drift) on success, or
        (None, measured_drift) for provider_declared fallback.
        Failures are never silent: a warning (with severity) is printed, and
        the manifest keeps adjustment_verified=False so consumers can audit.
        """
        try:
            reference = _fetch_tencent_full(symbol, request)
        except Exception:
            reference = []
        if not reference:
            return None, 0.0  # 参考源不可用 → provider_declared
        result = _verify_reference_overlap(
            primary_bars, reference, require_full=require_full
        )
        if result.ok:
            return (
                (
                    result.overlap_start,
                    result.overlap_end,
                    result.overlap_count,
                    result.max_return_error,
                    result.overlap_content_hash,
                    result.max_ratio_deviation,
                ),
                result.max_ratio_deviation,
            )
        severe = (
            result.max_return_error > _HARD_FAIL_RETURN_ERROR
            or result.max_ratio_deviation > _HARD_FAIL_RATIO_DRIFT
            or result.overlap_count == 0
        )
        level = "严重不一致" if severe else "轻微差异"
        print(
            f"[etf_market_data] {code}: 腾讯参考源与主源{level}"
            f"（{result.reason}），标记为 provider_declared",
            file=sys.stderr,
        )
        return None, result.max_ratio_deviation

    # ══════════════════════════════════════════════════════════════════
    # STEP 1: Check for fresh cache (no network needed)
    # ══════════════════════════════════════════════════════════════════
    best, _ = _scan_cached(fresh_only=True)
    if best is not None and not refresh:
        if os.environ.get("ETF_DATA_OFFLINE") == "1":
            return best[2]
        if not _should_refresh_online(best[2].manifest.end_date):
            return best[2]
        # 在线模式且收盘后缓存缺最近交易日 bar → 继续联网刷新

    # ══════════════════════════════════════════════════════════════════
    # STEP 1.5: 离线模式 — 只读本地缓存，不发起任何网络请求
    # 设置环境变量 ETF_DATA_OFFLINE=1 时启用（仪表盘默认开启，刷新时才联网）
    # ══════════════════════════════════════════════════════════════════
    if os.environ.get("ETF_DATA_OFFLINE") == "1":
        best, largest = _scan_cached(fresh_only=False)
        if best is not None:
            return best[2]
        if largest is not None:
            # 请求根数超出该 ETF 实际历史（如次新 ETF 不足 2000 根）时，
            # 回退到可用最大缓存，保证离线回测不因数据深度而整体失败。
            print(
                f"[etf_market_data] {code}: 离线模式请求 {count} 根，"
                f"实际最大缓存 {largest.manifest.bar_count} 根，回退使用",
                file=sys.stderr,
            )
            return largest
        raise ConnectionError(
            f"离线模式(ETF_DATA_OFFLINE=1)下无可用 v2 缓存: {code} (count={count})"
        )

    # ══════════════════════════════════════════════════════════════════
    # STEP 2: Full fetch from Eastmoney fqt=1 (primary) + Tencent full-history
    # cross-verification（含逐日收益比对与价格比漂移检测）
    # ══════════════════════════════════════════════════════════════════
    em_error = None
    try:
        bars = _parse_eastmoney(_request_json(request, _eastmoney_url(code)), code)
        bars = _drop_incomplete_last_bar(bars)
        verification, measured_drift = _verify_with_tencent(bars, require_full=True)
        if verification is None:
            declared = _standalone(bars, "eastmoney")
            declared["max_ratio_deviation"] = measured_drift
            series = _make_series_standalone(
                code, bars, "eastmoney", declared
            )
        else:
            series = _make_series(code, bars, verification)
        return _finalise(series, cache_paths["eastmoney"])
    except Exception as exc:
        em_error = exc
        print(
            f"[etf_market_data] {code}: 东方财富主源不可用/数据未过质量校验"
            f" ({exc})，尝试腾讯源",
            file=sys.stderr,
        )

    # ══════════════════════════════════════════════════════════════════
    # STEP 3: Tencent standalone fallback
    # ══════════════════════════════════════════════════════════════════
    tencent_error = None
    try:
        # 分页拉取腾讯 qfq 全历史（单次请求被端点限制在 ~640 根）
        bars = _fetch_tencent_full(symbol, request)
        bars = _drop_incomplete_last_bar(bars)
        if not bars:
            raise ConnectionError("腾讯未返回 K 线数据")
        series = _make_series_standalone(
            code, bars, "tencent", _standalone(bars, "tencent")
        )

        print(
            f"[etf_market_data] {code}: 东方财富不可用，已回退至腾讯源"
            f"（数据深度受限，约 {len(bars)} 根K线）",
            file=sys.stderr,
        )
        return _finalise(series, cache_paths["tencent"])
    except MarketDataQualityError as exc:
        tencent_error = exc
        print(
            f"[etf_market_data] {code}: 腾讯源数据质量问题 ({exc})，降级新浪源",
            file=sys.stderr,
        )
    except Exception as exc:
        tencent_error = exc

    # ══════════════════════════════════════════════════════════════════
    # STEP 4: Sina standalone fallback（不复权+折算修正，不处理分红）
    # ══════════════════════════════════════════════════════════════════
    try:
        sina_url = _sina_url(symbol, datalen=min(count, _SINA_MAX_DATALEN))
        bars = _parse_sina(_request_sina_json(request, sina_url), symbol)
        bars = _drop_incomplete_last_bar(bars)
        if not bars:
            raise ConnectionError("新浪未返回 K 线数据")
        series = _make_series_standalone(
            code, bars, "sina", _standalone(bars, "sina")
        )
        print(
            f"[etf_market_data] {code}: 东财/腾讯均不可用，回退新浪源；"
            f"新浪 ETF 序列不处理分红，分红标的历史可能不准",
            file=sys.stderr,
        )
        return _finalise(series, cache_paths["sina"])
    except MarketDataQualityError:
        raise
    except Exception as exc:
        print(
            f"[etf_market_data] {code}: 新浪源获取失败 ({exc})",
            file=sys.stderr,
        )

    # ══════════════════════════════════════════════════════════════════
    # STEP 5: Last resort — newest valid stale cache
    # ══════════════════════════════════════════════════════════════════
    best, _ = _scan_cached(fresh_only=False)
    if best is not None:
        return best[2]

    raise ConnectionError(
        f"前复权数据获取失败(东财/腾讯/新浪均不可用)且无合格 v2 缓存: {code}"
    ) from (em_error or tencent_error)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def latest_completed_trading_date(as_of: str | None, bars: tuple[dict, ...]) -> str:
    """Return the latest date present in ``bars`` at or before ``as_of``."""
    if as_of is None:
        if not bars:
            raise MarketDataQualityError("K 线为空")
        return bars[-1]["date"]
    try:
        datetime.strptime(as_of, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"非法结束日期: {as_of!r}") from exc
    dates = [bar["date"] for bar in bars if bar["date"] <= as_of]
    if not dates:
        raise MarketDataQualityError(f"{as_of} 之前没有行情")
    return dates[-1]


def truncate_series(series: MarketDataSeries, end_date: str) -> MarketDataSeries:
    """Return a validated view ending at or before ``end_date`` with a new hash."""
    completed_date = latest_completed_trading_date(end_date, series.bars)
    bars = tuple(bar for bar in series.bars if bar["date"] <= completed_date)
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
