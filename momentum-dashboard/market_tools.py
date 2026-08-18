#!/usr/bin/env python3
"""行情与技术面工具（从 server.py 拆出）。

- 本地数据读取：K 线缓存 / ETF 目录 / JSON 安全清洗
- 实时行情：腾讯批量报价
- 技术指标：波动率 / 趋势 / RSI / 布林带宽 / 网格评分
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

from bizlog import _log  # noqa: E402 - 日志


ROOT = Path(__file__).resolve().parent
PROJECT_DIR = ROOT.parent
DATA_DIR = PROJECT_DIR / "data"


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _read_json_file(path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def _json_safe(value):
    """把 NaN / Infinity 等非标准 JSON 数值清洗为 null，保证浏览器可解析。"""
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_ETF_NAME_CACHE: dict = {"mtime_ns": 0, "names": {}}


def _etf_display_name(code: str) -> str:
    """从全市场 ETF 目录取证券名称，未收录则回退代码本身。"""
    meta_path = DATA_DIR / "etf_meta.json"
    try:
        if meta_path.stat().st_mtime_ns != _ETF_NAME_CACHE["mtime_ns"]:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            _ETF_NAME_CACHE["names"] = {
                str(etf.get("code")): str(etf.get("name") or "")
                for etf in (meta.get("etfs") or [])
            }
            _ETF_NAME_CACHE["mtime_ns"] = meta_path.stat().st_mtime_ns
    except Exception as exc:
        _log(f"ETF 目录读取失败，名称回退代码: {exc}", "WARN")
    return _ETF_NAME_CACHE["names"].get(str(code)) or str(code)


# ---------------------------------------------------------------------------
# K 线
# ---------------------------------------------------------------------------

def _kline_from_cache(code, count):
    """从 data/cache 中读取 K 线，优先 v2 格式，其次旧格式。

    返回 (bars, meta)；找不到返回 (None, None)。
    """
    cache_dir = DATA_DIR / "cache"
    v2_hits = sorted(
        cache_dir.glob(f"etf_v2_*_{code}_qfq_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    old_hits = sorted(
        cache_dir.glob(f"etf_kline_{code}_qfq_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in v2_hits:
        data = _read_json_file(path)
        if not data or not data.get("bars"):
            continue
        bars = data["bars"]
        manifest = data.get("manifest", {})
        meta = {
            "source": manifest.get("source", "cache"),
            "start_date": manifest.get("start_date", bars[0].get("date")),
            "end_date": manifest.get("end_date", bars[-1].get("date")),
            "fetched_at": manifest.get("fetched_at"),
            "total_bars": len(bars),
        }
        return bars, meta
    for path in old_hits:
        data = _read_json_file(path)
        if not data:
            continue
        bars = data.get("data") or data.get("bars")
        if not bars:
            continue
        meta = {
            "source": data.get("source", "cache"),
            "start_date": bars[0].get("date"),
            "end_date": bars[-1].get("date"),
            "total_bars": len(bars),
        }
        return bars, meta
    return None, None


def load_kline(code, count=300):
    """读取 K 线；缓存不足时回退到 etf_market_data（可能触发网络刷新）。"""
    bars, meta = _kline_from_cache(code, count)
    if bars:
        view = bars[-count:]
        meta = dict(meta)
        meta["start_date"] = view[0]["date"]
        meta["total_bars"] = len(view)
        _log(
            f"DATA {code} 使用本地缓存 {meta.get('source', 'cache')} "
            f"{meta.get('total_bars', '?')} 根 ({meta.get('start_date')}~{meta.get('end_date')})",
            "INFO",
        )
        return view, meta
    try:
        sys.path.insert(0, str(PROJECT_DIR))
        from tools.etf_market_data import load_etf_series
        _log(f"DATA {code} 本地缓存不足，回退 load_etf_series (count={max(count, 300)})", "WARN")
        series = load_etf_series(code, count=max(count, 300))
        bars = list(series.bars)
        manifest = series.manifest
        meta = {
            "source": manifest.source,
            "start_date": manifest.start_date,
            "end_date": manifest.end_date,
            "fetched_at": manifest.fetched_at,
            "total_bars": len(bars),
        }
        return bars[-count:], meta
    except Exception as exc:
        _log(f"DATA {code} 加载失败: {exc}", "ERROR")
        return None, None


def _kline_end_date(code: str) -> str:
    """读取标的 K 线缓存的最新交易日（作为寻优缓存 key 的数据指纹）。"""
    try:
        from tools.etf_market_data import load_etf_series
        saved_offline = os.environ.get("ETF_DATA_OFFLINE")
        os.environ["ETF_DATA_OFFLINE"] = "1"
        try:
            series = load_etf_series(code, count=1500)
            return series.manifest.end_date or "?"
        finally:
            if saved_offline is not None:
                os.environ["ETF_DATA_OFFLINE"] = saved_offline
            else:
                os.environ.pop("ETF_DATA_OFFLINE", None)
    except Exception as exc:
        _log(f"K线最新交易日读取失败: {code} ({exc})", "WARN")
        return "?"


def _refresh_grid_kline_cache(codes: list[str], force: bool = False) -> None:
    """寻优前在线刷新目标标的 K 线缓存（写入 data/cache，供离线回测读取）。

    收盘后（>=15:10）缓存缺当日 bar 的标的会自动补拉；force=True 强制联网。
    """
    try:
        from tools.etf_market_data import load_etf_series
    except Exception as exc:
        _log(f"GRID-OPT 行情模块导入失败，跳过 K线刷新: {exc}", "WARN")
        return
    saved_offline = os.environ.get("ETF_DATA_OFFLINE")
    os.environ["ETF_DATA_OFFLINE"] = "0"
    try:
        for code in codes:
            try:
                load_etf_series(code, count=1500, refresh=force)
            except Exception as exc:
                _log(f"GRID-OPT 刷新 {code} K线失败: {exc}", "WARN")
    finally:
        if saved_offline is not None:
            os.environ["ETF_DATA_OFFLINE"] = saved_offline
        else:
            os.environ.pop("ETF_DATA_OFFLINE", None)


# ---------------------------------------------------------------------------
# 实时行情
# ---------------------------------------------------------------------------

def _qq_code(code):
    """ETF 代码转腾讯行情代码（sh/sz 前缀）。"""
    code = code.strip()
    if code.startswith(("6", "9", "5")):
        return "sh" + code
    elif code.startswith(("0", "3", "2", "1")):
        return "sz" + code
    return "sh" + code


def fetch_realtime_quotes(codes):
    """腾讯行情实时报价（批量），网络失败返回空 dict。"""
    symbols = ",".join(_qq_code(c) for c in codes)
    url = f"https://qt.gtimg.cn/q={symbols}"
    try:
        result = subprocess.run(
            [
                "/usr/bin/curl", "-s", "--noproxy", "*", "--max-time", "8",
                "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                url,
            ],
            capture_output=True,
            timeout=12,
        )
    except Exception as exc:
        _log(f"REALTIME 请求失败: {exc}", "WARN")
        return {}
    raw = result.stdout
    try:
        text = raw.decode("gbk", errors="replace")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    quotes = {}
    for line in text.splitlines():
        match = re.search(r'v_([a-z]+\d{6})="(.*)"', line)
        if not match:
            continue
        code = match.group(1)[-6:]
        if code not in codes:
            continue
        fields = match.group(2).split("~")
        if len(fields) < 34:
            continue
        try:
            price = float(fields[3])
        except ValueError:
            continue
        change_pct = None
        try:
            change_pct = float(fields[32])
        except (ValueError, IndexError):
            pass
        prev_close = None
        try:
            prev_close = float(fields[4]) if fields[4] not in ("", "0.00") else None
        except ValueError:
            pass
        quotes[code] = {
            "code": code,
            "name": fields[1],
            "price": price,
            "change_pct": change_pct,
            "prev_close": prev_close,
            "source": "tencent_realtime",
        }
    return quotes


# ---------------------------------------------------------------------------
# 交易制度 / 技术指标 / 网格评分
# ---------------------------------------------------------------------------

def _is_lof(code: str) -> bool:
    """LOF 判断：深交所 16xxxx；上交所 501-506。其余按 ETF。"""
    return code.startswith("16") or (
        code.startswith("50") and len(code) == 6 and 1 <= int(code[2:4]) <= 6
    )


def _is_t0(category: str) -> bool:
    """T+0 支持：跨境ETF/商品ETF/债券ETF/货币基金 可 T+0；A股股票 ETF/LOF 为 T+1。"""
    return str(category) in ("跨境ETF", "商品ETF", "债券ETF", "货币基金")


def _compute_technicals(bars: list) -> dict | None:
    """从 K 线 bar 列表计算技术指标（vol20/trend20/ma/bb/rsi/振幅/成交额）。"""
    if len(bars) < 30:
        return None
    closes = [float(b["close"]) for b in bars]
    vols = [float(b["volume"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    n = len(closes)
    trend20 = round((closes[-1] / closes[-21] - 1) * 100, 2)
    rets = [closes[i] / closes[i - 1] - 1 for i in range(n - 20, n)]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    vol20 = round(math.sqrt(var) * math.sqrt(252) * 100, 1)
    ma20 = sum(closes[-20:]) / 20
    std20 = math.sqrt(sum((c - ma20) ** 2 for c in closes[-20:]) / 20)
    bb_width = round(4 * std20 / ma20 * 100, 2)
    gains = losses = 0.0
    for i in range(n - 14, n):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    avg_g = gains / 14
    avg_l = losses / 14
    rsi = round(100 - 100 / (1 + avg_g / avg_l), 1) if avg_l > 0 else 100.0
    avg_amount_wan = round(
        sum(vols[-60:][i] * closes[-60:][i] * 100 for i in range(len(vols[-60:])))
        / min(60, len(vols[-60:]))
        / 10000,
        0,
    )
    amp_window = min(60, n - 1)
    amps = [
        (highs[i] - lows[i]) / closes[i - 1] * 100
        for i in range(n - amp_window, n)
        if closes[i - 1] > 0
    ]
    amplitude = round(sum(amps) / len(amps), 2) if amps else None
    # 近 120 日最大回撤（峰值到谷值），替代旧回测快照，保证评分与 K 线同版本
    mdd_window = closes[-120:]
    peak = mdd_window[0]
    max_dd = 0.0
    for close in mdd_window:
        if close > peak:
            peak = close
        elif peak > 0:
            drawdown = (peak - close) / peak
            if drawdown > max_dd:
                max_dd = drawdown
    return {
        "close": closes[-1],
        "ma20": round(ma20, 4),
        "ma60": round(sum(closes[-60:]) / 60, 4),
        "vol20": vol20,
        "trend20": trend20,
        "bb_width": bb_width,
        "rsi": rsi,
        "avg_amount_wan": avg_amount_wan,
        "amplitude": amplitude,
        "max_drawdown": round(max_dd, 4),
    }


def _grid_screener_technicals(codes: set[str]) -> dict:
    """从 K 线缓存直接读取各标的近期技术指标（避免逐个 load 太慢）。"""
    cache_dir = DATA_DIR / "cache"
    out: dict[str, dict] = {}
    for path in cache_dir.glob("etf_v2_*_qfq_2000.json"):
        code = path.name.split("_")[3]
        if code not in codes:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            tech = _compute_technicals(payload.get("bars") or [])
            if tech:
                out[code] = tech
        except Exception as exc:
            _log(f"GRID 选品技术指标计算失败: {code} ({exc})", "WARN")
            continue
    return out


def _single_code_technicals(code: str) -> dict:
    """读取单个标的 K 线缓存计算近期技术指标（vol20/trend20/rsi/bb）。"""
    for path in (DATA_DIR / "cache").glob(f"etf_v2_*_{code}_qfq_2000.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            tech = _compute_technicals(payload.get("bars") or [])
            if tech:
                return tech
        except Exception as exc:
            _log(f"单标的 K线缓存解析失败: {code} ({exc})", "WARN")
            continue
    return {}


def _lightweight_trend_score(tech: dict) -> int | None:
    """从缓存技术指标估算趋势评分（-5 空头 ~ +5 多头，0 缠绕），
    与网格 analyze_trend 同量纲，供选品池展示与筛选。"""
    close = tech.get("close")
    ma20 = tech.get("ma20")
    ma60 = tech.get("ma60")
    if close is None or ma20 is None or ma60 is None:
        return None
    trend20 = tech.get("trend20") or 0
    if close > ma20 > ma60:
        return 5 if trend20 > 0 else 3
    if close < ma20 < ma60:
        return -5 if trend20 < 0 else -3
    if ma20 > ma60:
        return 2
    if ma20 < ma60:
        return -2
    return 0


def _single_grid_score(code: str, tech: dict) -> float:
    """单标的网格适配评分（与选品池同口径：波动/规模/均值回归/回撤，T+0 加分）。

    波动/回撤全部来自 K 线实时技术指标（tech），不再依赖旧回测快照。
    """
    meta = _read_json_file(DATA_DIR / "etf_meta.json", {})
    etf = next(
        (e for e in (meta.get("etfs") or []) if str(e.get("code")) == code),
        {},
    )
    vol = tech.get("vol20")
    mdd = float(tech.get("max_drawdown") or 0)
    fund_size = float(etf.get("fund_size") or 0)
    trend20 = tech.get("trend20")
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
        score += 2.5
    if mdd:
        m = mdd * 100
        score += 5 if m < 25 else 3.5 if m < 35 else 2 if m < 45 else 0.5
    if _is_t0(str(etf.get("category"))):
        score += 1
    return round(score, 1)
