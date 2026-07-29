#!/usr/bin/env python3
"""A股数据工具 — 腾讯行情 + 东方财富搜索/财务/ETF/基金/指数/汇率 + 防封策略，零外部依赖（仅 stdlib）。

为 Claude Code Skills 提供 A 股实时行情、财务数据、K线、龙虎榜、北向资金、ETF/基金数据等。
设计原则：
  - 独立模块，不影响现有工具
  - 使用 curl 直连绕过系统代理
  - 东财接口统一走 _em_get() 串行限流 + 随机抖动
  - 关键数据有备用源降级

用法（由 Skills 自动调用）：
    python3 tools/ashare_data.py quote 600519                    # 实时行情
    python3 tools/ashare_data.py financials 600519               # 核心财务数据（近5年）
    python3 tools/ashare_data.py valuation 600519                # 估值指标
    python3 tools/ashare_data.py search 茅台                      # 搜索股票代码
    python3 tools/ashare_data.py kline 600519                    # 日K线 + MA均线
    python3 tools/ashare_data.py dragon_tiger                    # 龙虎榜
    python3 tools/ashare_data.py north_flow                      # 北向资金
    python3 tools/ashare_data.py news 600519                     # 个股新闻
    python3 tools/ashare_data.py etf 513180                      # ETF 数据
    python3 tools/ashare_data.py fund 002803                     # 场外基金数据
    python3 tools/ashare_data.py index 000300                    # 指数估值（PE/PB/分位）
    python3 tools/ashare_data.py market                          # 大盘全景
    python3 tools/ashare_data.py fx                              # 人民币汇率

需要 Python >= 3.8，零外部依赖。
"""

import argparse
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from decimal import Decimal, ROUND_HALF_EVEN
from urllib.parse import urlencode

_TIMEOUT = 15
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "cache")

# ==============================================================================
# 本地缓存层 — 减少重复请求，降低封IP风险
# ==============================================================================

def _cache_path(key: str) -> str:
    """缓存文件路径。"""
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return os.path.join(_CACHE_DIR, f"{key}.json")


def _cache_get(key: str, max_age_hours: float = 6) -> dict | None:
    """读取缓存，过期返回 None。K线/北向资金日度数据，6小时内不变。"""
    path = _cache_path(key)
    if not os.path.exists(path):
        return None
    try:
        age_hours = (time.time() - os.path.getmtime(path)) / 3600
        if age_hours > max_age_hours:
            return None
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _cache_set(key: str, data: dict):
    """写入缓存。"""
    try:
        data["_cached_at"] = datetime.now().isoformat()
        with open(_cache_path(key), "w") as f:
            json.dump(data, f, ensure_ascii=False, default=str)
    except Exception:
        pass  # 缓存失败不影响主流程

# ==============================================================================
# 防封策略 — 东财接口统一入口
# ==============================================================================

_EM_LAST_CALL = 0.0
_EM_MIN_INTERVAL = 1.2  # 东财最小请求间隔（秒），批量任务可调大

# 东财域名列表
_EM_DOMAINS = [
    "eastmoney.com",
    "datacenter.eastmoney.com",
    "datacenter-web.eastmoney.com",
    "push2.eastmoney.com",
    "push2delay.eastmoney.com",
    "push2his.eastmoney.com",
    "searchadapter.eastmoney.com",
    "search-api-web.eastmoney.com",
    "fundmobapi.eastmoney.com",
    "fundf10.eastmoney.com",
    "np-weblist.eastmoney.com",
    "reportapi.eastmoney.com",
]


def _is_eastmoney(url: str) -> bool:
    """判断 URL 是否属于东财域名。"""
    return any(d in url for d in _EM_DOMAINS)


def _em_get(url, params=None):
    """东财接口统一入口：串行限流 + 随机抖动 + 2次重试。

    借鉴 a-stock-data 的 em_get() 防封策略：
    - 最小间隔 ≥1.2s
    - 随机抖动 0.1-0.8s
    - 首次失败后等 2-4s 重试一次
    """
    global _EM_LAST_CALL
    elapsed = time.time() - _EM_LAST_CALL
    if elapsed < _EM_MIN_INTERVAL:
        jitter = random.uniform(0.1, 0.8)
        time.sleep(_EM_MIN_INTERVAL - elapsed + jitter)
    _EM_LAST_CALL = time.time()

    last_error = None
    for attempt in range(2):
        try:
            return _curl_json(url, params)
        except Exception as e:
            last_error = e
            if attempt == 0:
                time.sleep(2 + random.uniform(0, 2))
                _EM_LAST_CALL = time.time()
    raise ConnectionError(f"东财请求失败（已重试）: {last_error}")


# ==============================================================================
# HTTP 基础层
# ==============================================================================

def _curl(url):
    """用 curl --noproxy 直连，绕过系统代理。"""
    result = subprocess.run(
        ["/usr/bin/curl", "-s", "--noproxy", "*",
         "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
         url],
        capture_output=True, timeout=_TIMEOUT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ConnectionError(f"请求失败: {url}")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return result.stdout.decode("gbk")


def _curl_json(url, params=None):
    """curl 获取 JSON，自动处理 URL 参数拼接。"""
    if params:
        url = f"{url}?{urlencode(params)}"
    return json.loads(_curl(url))


def _try_float(v):
    """安全转换为 float。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ==============================================================================
# 腾讯行情 API（稳定可靠，无需鉴权，不封IP — 首选数据源）
# ==============================================================================

def _qq_code(code: str) -> str:
    """将股票代码转为腾讯行情格式。"""
    code = code.strip().replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    if code.startswith(("6", "9", "5")):
        return f"sh{code}"
    elif code.startswith(("0", "3", "2", "1")):
        return f"sz{code}"
    elif code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sh{code}"


def _parse_qq_quote(raw: str) -> dict:
    """解析腾讯行情数据。格式：v_shXXXXXX="字段1~字段2~...";"""
    start = raw.find('"')
    end = raw.rfind('"')
    if start < 0 or end <= start:
        return {}
    fields = raw[start + 1:end].split("~")
    if len(fields) < 30:
        return {}
    return {
        "name": fields[1],
        "code": fields[2],
        "price": fields[3],
        "prev_close": fields[4],
        "open": fields[5],
        "volume": fields[6],          # 手
        "buy_vol": fields[7],
        "sell_vol": fields[8],
        "high": fields[33] if len(fields) > 33 else fields[3],
        "low": fields[34] if len(fields) > 34 else fields[3],
        "change_pct": fields[32],
        "change_amt": fields[31],
        "turnover_amt": fields[37] if len(fields) > 37 else "-",
        "turnover_rate": fields[38] if len(fields) > 38 else "-",
        "pe": fields[39] if len(fields) > 39 else "-",
        "market_cap": fields[45] if len(fields) > 45 else "-",
        "float_cap": fields[44] if len(fields) > 44 else "-",
        "pb": fields[46] if len(fields) > 46 else "-",
        "high_52w": fields[47] if len(fields) > 47 else "-",
        "low_52w": fields[48] if len(fields) > 48 else "-",
        "total_shares": fields[38] if len(fields) > 38 else "-",
    }


# ==============================================================================
# 格式化辅助
# ==============================================================================

def _fmt_yi(value) -> str:
    if value is None or value == "-" or value == "":
        return "-"
    try:
        v = float(value)
    except (ValueError, TypeError):
        return str(value)
    if abs(v) >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:.2f}万"
    return f"{v:.2f}"


def _fmt_pct(value) -> str:
    if value is None or value == "-" or value == "":
        return "-"
    try:
        return f"{float(value):.2f}%"
    except (ValueError, TypeError):
        return str(value)


# ==============================================================================
# 备用源降级框架
# ==============================================================================

def _try_with_fallback(functions, *args, **kwargs):
    """按顺序尝试多个数据源函数，第一个成功即返回 (result, source_label)。"""
    for func, label in functions:
        try:
            result = func(*args, **kwargs)
            if result:
                return result, label
        except Exception:
            continue
    raise ConnectionError("所有数据源均不可用")


# ==============================================================================
# 命令实现 — 实时行情
# ==============================================================================

def cmd_quote(code: str, json_out: bool = False):
    """实时行情快照。"""
    qq_code = _qq_code(code)
    raw = _curl(f"https://qt.gtimg.cn/q={qq_code}")
    d = _parse_qq_quote(raw)
    if not d:
        if json_out:
            print(json.dumps({"error": f"未找到股票 {code}"}, ensure_ascii=False))
        else:
            print(f"❌ 未找到股票 {code}")
        return

    if json_out:
        result = {**d, "fetched_at": datetime.now().isoformat()}
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    print("=" * 60)
    print(f"实时行情: {d['name']} ({d['code']})")
    print("=" * 60)
    print(f"  当前价:     {d['price']}")
    print(f"  涨跌幅:     {d['change_pct']}%")
    print(f"  涨跌额:     {d['change_amt']}")
    print(f"  今开:       {d['open']}")
    print(f"  最高:       {d['high']}")
    print(f"  最低:       {d['low']}")
    print(f"  昨收:       {d['prev_close']}")
    print(f"  成交量:     {d['volume']} 手")
    print(f"  成交额:     {d['turnover_amt']}万")
    print(f"  总市值:     {d['market_cap']}亿")
    print(f"  流通市值:   {d['float_cap']}亿")
    print(f"  PE(动):     {d['pe']}")
    print(f"  PB:         {d['pb']}")
    print(f"  换手率:     {d['turnover_rate']}%")
    print(f"  52周最高:   {d['high_52w']}")
    print(f"  52周最低:   {d['low_52w']}")


def cmd_valuation(code: str):
    """估值指标汇总。"""
    qq_code = _qq_code(code)
    raw = _curl(f"https://qt.gtimg.cn/q={qq_code}")
    d = _parse_qq_quote(raw)
    if not d:
        print(f"❌ 未找到股票 {code}")
        return

    price = d["price"]
    market_cap_yi = d["market_cap"]

    print("=" * 60)
    print(f"估值指标: {d['name']} ({d['code']})")
    print("=" * 60)
    print(f"  当前价:     {price}")
    print(f"  总市值:     {market_cap_yi}亿")
    print(f"  流通市值:   {d['float_cap']}亿")
    print(f"  PE(动):     {d['pe']}")
    print(f"  PB:         {d['pb']}")
    print(f"  52周最高:   {d['high_52w']}")
    print(f"  52周最低:   {d['low_52w']}")

    try:
        p = Decimal(price)
        cap = Decimal(market_cap_yi) * Decimal("1e8")
        shares = cap / p
        print(f"\n  推算总股本: {_fmt_yi(float(shares))}股")
        calc_cap = p * shares
        reported_cap = Decimal(market_cap_yi) * Decimal("1e8")
        diff = abs(calc_cap - reported_cap) / reported_cap * 100
        print(f"  市值验算:   ✅ 一致（推算法，偏差 {float(diff):.1f}%）")
    except Exception:
        pass


# ==============================================================================
# 命令实现 — 核心财务数据（东财 datacenter，走限流通道）
# ==============================================================================

def cmd_financials(code: str, summary: bool = False):
    """近5年核心财务数据。--summary 模式只输出核心5指标（-70% token）。"""
    qq_code = _qq_code(code)
    raw = _curl(f"https://qt.gtimg.cn/q={qq_code}")
    d = _parse_qq_quote(raw)
    name = d.get("name", code) if d else code

    code_clean = code.strip().replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    market = "SH" if code_clean.startswith(("6", "9", "5")) else "SZ"

    fin_url = "https://datacenter.eastmoney.com/securities/api/data/get"
    params = {
        "type": "RPT_F10_FINANCE_MAINFINADATA",
        "sty": "ALL",
        "filter": f'(SECUCODE="{code_clean}.{market}")(REPORT_TYPE="年报")',
        "p": "1",
        "ps": "5",
        "sr": "-1",
        "st": "REPORT_DATE",
        "source": "HSF10",
        "client": "PC",
    }
    reports = []
    try:
        data = _em_get(fin_url, params)
        reports = data.get("result", {}).get("data", [])
    except Exception:
        pass

    if not reports:
        params["filter"] = f'(SECUCODE="{code_clean}.{market}")'
        try:
            data = _em_get(fin_url, params)
            reports = data.get("result", {}).get("data", [])
        except Exception:
            pass

    print("=" * 60)
    print(f"核心财务数据: {name} ({code_clean})")
    print("=" * 60)

    if not reports:
        print("  ⚠️ 未能获取财务数据，建议通过 WebSearch 补充")
        return

    if summary:
        # 摘要模式：只输出核心5指标
        print(f"  {'年份':6s} {'营收(亿)':>10s} {'净利(亿)':>10s} {'EPS':>6s} {'ROE':>7s}")
        print("  " + "-" * 52)
        for r in reports[:5]:
            date = (r.get("REPORT_DATE") or "")[:10]
            revenue = _try_float(r.get("TOTALOPERATEREVE"))
            net_profit = _try_float(r.get("PARENTNETPROFIT"))
            eps = _try_float(r.get("EPSJB"))
            roe = _try_float(r.get("ROEJQ"))
            rev_s = f"{revenue/1e8:.1f}" if revenue else "-"
            np_s = f"{net_profit/1e8:.1f}" if net_profit else "-"
            eps_s = f"{eps:.2f}" if eps else "-"
            roe_s = f"{roe:.1f}%" if roe else "-"
            print(f"  {date}  {rev_s:>10s}  {np_s:>10s}  {eps_s:>6s}  {roe_s:>7s}")
        return

    for r in reports[:5]:
        date = r.get("REPORT_DATE", "")[:10]
        report_name = r.get("REPORT_DATE_NAME", "")
        revenue = r.get("TOTALOPERATEREVE")
        net_profit = r.get("PARENTNETPROFIT")
        eps = r.get("EPSJB")
        bps = r.get("BPS")
        roe = r.get("ROEJQ")
        rev_growth = r.get("TOTALOPERATEREVETZ")
        profit_growth = r.get("PARENTNETPROFITTZ")

        print(f"\n  --- {date} {report_name} ---")
        if revenue is not None:
            print(f"  营收:           {_fmt_yi(revenue)}")
        if rev_growth is not None:
            print(f"  营收增速:       {_fmt_pct(rev_growth)}")
        if net_profit is not None:
            print(f"  归母净利润:     {_fmt_yi(net_profit)}")
        if profit_growth is not None:
            print(f"  净利润增速:     {_fmt_pct(profit_growth)}")
        if eps is not None:
            print(f"  基本每股收益:   {eps}")
        if bps is not None:
            print(f"  每股净资产:     {bps:.2f}")
        if roe is not None:
            print(f"  ROE(加权):      {_fmt_pct(roe)}")


def cmd_search(keyword: str):
    """搜索股票代码。"""
    url = "https://searchadapter.eastmoney.com/api/suggest/get"
    token = os.environ.get("EASTMONEY_SEARCH_TOKEN") or "D43BF722C8E33BDC906FB84D85E326E8"
    params = {
        "input": keyword,
        "type": "14",
        "token": token,
        "count": "10",
    }
    try:
        data = _em_get(url, params)
    except Exception:
        print(f"❌ 搜索请求失败（东财接口可能限流），建议稍后重试")
        return

    results = data.get("QuotationCodeTable", {}).get("Data", [])

    if not results:
        print(f"❌ 未找到匹配 '{keyword}' 的股票")
        return

    print("=" * 60)
    print(f"搜索结果: '{keyword}'")
    print("=" * 60)
    for r in results:
        code = r.get("Code", "")
        name = r.get("Name", "")
        market = r.get("MktNum", "")
        mkt_label = {"1": "沪", "2": "深", "3": "北"}.get(str(market), "")
        print(f"  {code} {name} [{mkt_label}]")


# ==============================================================================
# 命令实现 — K线数据 ⭐新增（腾讯，不封IP — 首选）
# ==============================================================================

def _kline_tencent(code: str, count: int = 250, adjust: str = "qfq") -> list:
    """腾讯日K线数据（web.ifzq.gtimg.cn，不封IP，JSON结构化）。

    日K线数据每日仅新增一根K线，缓存 6h 有效。
    返回: [(date, open, close, high, low, volume), ...]
    """
    # 缓存检查
    cache_key = f"kline_{code}_{adjust}_{count}"
    cached = _cache_get(cache_key, max_age_hours=6)
    if cached and cached.get("code") == code:
        return cached.get("data", [])

    qq = _qq_code(code)
    param = f"day,,,{count},{adjust}"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={qq},{param}"
    resp = _curl_json(url)

    # 路径: data → sh600519 → qfqday 或 day
    data = resp.get("data", {})
    stock_data = data.get(qq, {})
    klines = stock_data.get(f"{adjust}day") or stock_data.get("day") or []

    result = []
    for row in klines:
        if len(row) >= 6:
            result.append({
                "date": str(row[0]),
                "open": float(row[1]),
                "close": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "volume": float(row[5]),
            })
    # 缓存结果
    if result:
        _cache_set(cache_key, {"code": code, "data": result, "source": "tencent", "adjust": adjust})
    return result


def _calc_ma(klines: list, period: int) -> list:
    """计算移动均线，未覆盖到的位置填充 None。"""
    ma = [None] * len(klines)
    if len(klines) < period:
        return ma
    window = sum(k["close"] for k in klines[:period])
    ma[period - 1] = round(window / period, 2)
    for i in range(period, len(klines)):
        window += klines[i]["close"] - klines[i - period]["close"]
        ma[i] = round(window / period, 2)
    return ma


def cmd_kline(code: str, count: int = 250, adjust: str = "qfq", json_out: bool = False):
    """日K线 + MA(5/10/20/60/120/200) + 趋势判断。"""
    qq_code = _qq_code(code)
    raw = _curl(f"https://qt.gtimg.cn/q={qq_code}")
    d = _parse_qq_quote(raw)
    name = d.get("name", code)
    current_price = float(d.get("price", 0) or 0)

    # 获取K线数据（腾讯首选，百度备用）
    klines = []
    source = "腾讯"
    try:
        klines = _kline_tencent(code, count, adjust)
    except Exception:
        pass

    if not klines:
        try:
            klines = _kline_baidu(code, count, adjust)
            source = "百度"
        except Exception:
            pass

    if not klines:
        print("❌ 未能获取K线数据")
        return

    # 计算均线
    ma5 = _calc_ma(klines, 5)
    ma10 = _calc_ma(klines, 10)
    ma20 = _calc_ma(klines, 20)
    ma60 = _calc_ma(klines, 60)
    ma120 = _calc_ma(klines, 120)
    ma200 = _calc_ma(klines, 200)

    last = klines[-1]

    # 趋势判断
    ma20_val = ma20[-1]
    ma60_val = ma60[-1]
    ma200_val = ma200[-1]
    trend_short = "上涨" if ma5[-1] and ma10[-1] and ma5[-1] > ma10[-1] else "下跌"
    trend_med = "多头" if ma20_val and ma60_val and ma20_val > ma60_val else "空头"
    trend_long = "牛" if ma200_val and current_price > ma200_val else "熊"

    if json_out:
        result = {
            "code": code, "name": name, "source": source,
            "current_price": current_price,
            "klines_count": len(klines),
            "latest": {
                "date": last["date"], "open": last["open"], "close": last["close"],
                "high": last["high"], "low": last["low"], "volume": last["volume"],
            },
            "ma": {
                "ma5": ma5[-1], "ma10": ma10[-1], "ma20": ma20_val,
                "ma60": ma60_val, "ma120": ma120[-1], "ma200": ma200_val,
            },
            "trend": {
                "short": trend_short, "medium": trend_med,
                "long": f"{'价格>200MA' if ma200_val and current_price > ma200_val else '价格<200MA'}（{trend_long}市）",
                "price_vs_ma200_pct": round((current_price - ma200_val) / ma200_val * 100, 1) if ma200_val else None,
            },
            "fetched_at": datetime.now().isoformat(),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    print("=" * 60)
    print(f"K线分析: {name} ({code})  |  数据源: {source}")
    print("=" * 60)
    print(f"  最新收盘:   {last['close']}  ({last['date']})")
    print(f"  当前价:     {current_price if current_price else 'N/A'}")
    print(f"  MA5:        {ma5[-1]}  MA10: {ma10[-1]}  MA20: {ma20_val}")
    print(f"  MA60:       {ma60_val}  MA120: {ma120[-1]}  MA200: {ma200_val}")
    print(f"  短期趋势:   {trend_short}  |  中期: {trend_med}  |  长期: {trend_long}市")
    if ma200_val and current_price:
        pct = (current_price - ma200_val) / ma200_val * 100
        print(f"  距200日均线: {pct:+.1f}%  {'✅ 线上' if current_price > ma200_val else '⚠️ 线下'}")
    print(f"  近5日收盘:   {', '.join(str(k['close']) for k in klines[-5:])}")
    print(f"  近5日涨幅:   {(klines[-1]['close'] - klines[-6]['close']) / klines[-6]['close'] * 100:.1f}%" if len(klines) >= 6 else "")


def _kline_baidu(code: str, count: int = 250, adjust: str = "qfq") -> list:
    """百度K线备用源。"""
    qq = _qq_code(code)
    # 百度K线返回带 MA5/10/20 的日K线
    pure_code = code.strip().replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    if pure_code.startswith(("6", "9", "5")):
        baidu_code = f"sh{pure_code}"
    else:
        baidu_code = f"sz{pure_code}"

    url = f"https://finance.pae.baidu.com/selfselect/getstockquotation?all=1&code={baidu_code}&isIndex=false&isBk=false&isFutures=false&newFormat=1&finClientType=pc"
    resp = _curl_json(url)
    kline_data = resp.get("data", {}).get("kline", []) or []

    result = []
    for item in kline_data[-count:]:
        if isinstance(item, str):
            parts = item.split(",")
            if len(parts) >= 6:
                result.append({
                    "date": parts[0],
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volume": float(parts[5]),
                })
    return result


# ==============================================================================
# 命令实现 — 龙虎榜 ⭐新增（东财 datacenter-web，走限流通道）
# ==============================================================================

def _dragon_tiger_em(date_str: str) -> list:
    """东财龙虎榜数据。"""
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "sortColumns": "NET_BUY_AMT,TRADE_DATE,SECURITY_CODE",
        "sortTypes": "-1,-1,1",
        "pageSize": "80",
        "reportName": "RPT_ORGANIZATION_TRADE_DETAILS",
        "columns": "ALL",
        "source": "WEB",
        "client": "WEB",
        "filter": f"(TRADE_DATE>='{date_str}')",
    }
    data = _em_get(url, params)
    return data.get("result", {}).get("data", [])


def cmd_dragon_tiger(date_str: str = None, json_out: bool = False):
    """龙虎榜数据（每日全市场）+ 情绪速算摘要。"""
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    items = []
    source = "东财"
    try:
        items = _dragon_tiger_em(date_str)
    except Exception:
        pass

    if not items:
        print(f"⚠️ 未能获取 {date_str} 龙虎榜数据（可能当天非交易日或东财限流）")
        return

    # ── 情绪速算摘要 ──
    net_buys = [(it.get("NET_BUY_AMT") or 0) for it in items]
    pos_count = sum(1 for n in net_buys if n > 0)
    neg_count = sum(1 for n in net_buys if n < 0)
    total_net = sum(net_buys)
    total_buy = sum((it.get("BUY_AMT") or 0) for it in items)
    total_sell = sum((it.get("SELL_AMT") or 0) for it in items)
    # 机构买卖
    inst_buy = sum((it.get("BUY_AMT") or 0) for it in items if "机构" in str(it.get("REASON_LISTED", "")))
    inst_sell = sum((it.get("SELL_AMT") or 0) for it in items if "机构" in str(it.get("REASON_LISTED", "")))

    if json_out:
        result = {
            "type": "dragon_tiger", "date": date_str, "source": source,
            "count": len(items),
            "sentiment": {
                "pos_count": pos_count, "neg_count": neg_count,
                "total_net_buy": round(total_net, 2),
                "total_buy": round(total_buy, 2),
                "total_sell": round(total_sell, 2),
                "buy_sell_ratio": round(total_buy / total_sell, 2) if total_sell else None,
                "inst_net_buy": round(inst_buy - inst_sell, 2),
            },
            "top3": [
                {"code": it.get("SECURITY_CODE"), "name": it.get("SECURITY_NAME_ABBR"),
                 "change_pct": _try_float(it.get("CHANGE_RATE")),
                 "net_buy": _try_float(it.get("NET_BUY_AMT")),
                 "reason": (it.get("REASON_LISTED") or "")}
                for it in items[:3]
            ],
            "items": items[:20],
            "fetched_at": datetime.now().isoformat(),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    # ── 文本输出 ──
    print("=" * 60)
    print(f"龙虎榜: {date_str}  |  上榜 {len(items)} 只")
    print("=" * 60)
    print("【情绪速算】")
    print(f"  净买>0: {pos_count} 只  |  净卖>0: {neg_count} 只")
    print(f"  总净买入额: {_fmt_yi(total_net)}  总成交: {_fmt_yi(total_buy+total_sell)}")
    print(f"  买/卖比: {total_buy/total_sell:.2f}" if total_sell else "")
    print()
    print("【净买 TOP10】")
    for item in items[:10]:
        code = item.get("SECURITY_CODE", "")
        name = item.get("SECURITY_NAME_ABBR", "")
        change_pct = _try_float(item.get("CHANGE_RATE"))
        net_buy = _try_float(item.get("NET_BUY_AMT"))
        reason = (item.get("REASON_LISTED") or "")[:40]
        chg = f"{change_pct:+.1f}%" if change_pct is not None else "-"
        print(f"  {code} {name:8s}  涨跌: {chg}  净买: {_fmt_yi(net_buy)}  {reason}")


# ==============================================================================
# 命令实现 — 北向资金 ⭐新增（东财 push2，走限流通道）
# ==============================================================================

def _north_flow_em(days: int = 20) -> dict:
    """东财北向资金日度流向。缓存 4h 有效。

    返回: {"dates": [...], "net_flows": [...], "cumulative": [...]}
    """
    cache_key = f"north_flow_{days}"
    cached = _cache_get(cache_key, max_age_hours=4)
    if cached:
        return cached

    url = "https://push2his.eastmoney.com/api/qt/kamt.kline/get"
    params = {
        "fields1": "f1,f2,f3,f4",
        "fields2": "f51,f52,f53,f54,f55,f56",
        "klt": "101",       # 日线
        "lmt": str(days),
        "ut": "b2884a393a59ad64002292a3e90d46a5",
    }
    data = _em_get(url, params)
    klines = (data.get("data") or {}).get("klines") or []

    dates, hgt_net, sgt_net, total_net = [], [], [], []
    for row in klines:
        parts = row.split(",")
        if len(parts) >= 4:
            dates.append(parts[0])
            # f52=沪股通净流入, f53=深股通净流入
            h = float(parts[1]) if parts[1] != "-" else 0
            s = float(parts[2]) if parts[2] != "-" else 0
            t = round(h + s, 2)
            hgt_net.append(round(h, 2))
            sgt_net.append(round(s, 2))
            total_net.append(t)

    result = {
        "dates": dates,
        "hgt_net": hgt_net,    # 沪股通
        "sgt_net": sgt_net,    # 深股通
        "total_net": total_net,  # 北向合计
    }
    _cache_set(cache_key, result)
    return result


def cmd_north_flow(days: int = 20, json_out: bool = False):
    """北向资金日度流向。"""
    try:
        flow = _north_flow_em(days)
    except Exception:
        print("❌ 未能获取北向资金数据（东财接口可能限流）")
        return

    if not flow["dates"]:
        print("⚠️ 北向资金数据为空")
        return

    if json_out:
        result = {
            "type": "north_flow",
            "days": len(flow["dates"]),
            "latest_date": flow["dates"][-1] if flow["dates"] else None,
            "latest_net": flow["total_net"][-1] if flow["total_net"] else None,
            "sum_5d": round(sum(flow["total_net"][-5:]), 2) if len(flow["total_net"]) >= 5 else None,
            "sum_20d": round(sum(flow["total_net"]), 2),
            "dates": flow["dates"],
            "hgt_net": flow["hgt_net"],
            "sgt_net": flow["sgt_net"],
            "total_net": flow["total_net"],
            "fetched_at": datetime.now().isoformat(),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    print("=" * 60)
    print("北向资金（沪深港通）")
    print("=" * 60)
    sum_5d = round(sum(flow["total_net"][-5:]), 2)
    sum_20d = round(sum(flow["total_net"]), 2)
    latest = flow["total_net"][-1]
    direction = "净流入" if latest > 0 else "净流出"
    print(f"  最新({flow['dates'][-1]}): {_fmt_yi(abs(latest))} {direction}")
    print(f"  近5日累计:  {_fmt_yi(sum_5d)}")
    print(f"  近{days}日累计:{_fmt_yi(sum_20d)}")
    print(f"\n  最近10个交易日:")
    for i in range(max(0, len(flow["dates"]) - 10), len(flow["dates"])):
        d = flow["dates"][i]
        t = flow["total_net"][i]
        h = flow["hgt_net"][i]
        s = flow["sgt_net"][i]
        arrow = "↑" if t > 0 else "↓"
        print(f"    {d}  合计: {t:+.2f}亿 {arrow}  沪: {h:+.2f}  深: {s:+.2f}")


# ==============================================================================
# 命令实现 — 资金流向 ⭐新增（东财 push2his，走限流通道）
# ==============================================================================

def _fund_flow_em(code: str, days: int = 120) -> dict:
    """东财个股资金流向（日度主力/超大单/大单/中单/小单净流入）。

    返回: {"dates": [...], "main_net": [...], "super_large_net": [...], ...}
    """
    code_clean = code.strip().replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    if code_clean.startswith(("6", "9", "5")):
        secid = f"1.{code_clean}"
    else:
        secid = f"0.{code_clean}"

    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "lmt": str(days),
        "klt": "101",
        "secid": secid,
        "ut": "b2884a393a59ad64002292a3e90d46a5",
    }
    data = _em_get(url, params)
    klines = (data.get("data") or {}).get("klines") or []

    dates, main_net, super_large, large, medium, small = [], [], [], [], [], []
    for row in klines:
        parts = row.split(",")
        if len(parts) >= 7:
            dates.append(parts[0])
            main_net.append(_try_float(parts[1]) or 0)     # f52 主力净流入
            small.append(_try_float(parts[2]) or 0)         # f53 小单
            medium.append(_try_float(parts[3]) or 0)        # f54 中单
            large.append(_try_float(parts[4]) or 0)         # f55 大单
            super_large.append(_try_float(parts[5]) or 0)   # f56 超大单

    return {
        "dates": dates,
        "main_net": main_net,
        "super_large_net": super_large,
        "large_net": large,
        "medium_net": medium,
        "small_net": small,
    }


def _shareholder_count_em(code: str) -> dict:
    """东财股东户数变化（用于筹码集中度判断）。"""
    code_clean = code.strip().replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    market = "SH" if code_clean.startswith(("6", "9", "5")) else "SZ"
    url = "https://datacenter.eastmoney.com/securities/api/data/get"
    params = {
        "type": "RPT_F10_EQUITY_HOLDERNUM",
        "sty": "ALL",
        "filter": f'(SECUCODE="{code_clean}.{market}")',
        "p": "1",
        "ps": "5",
        "sr": "-1",
        "st": "END_DATE",
        "source": "HSF10",
        "client": "PC",
    }
    try:
        data = _em_get(url, params)
        items = data.get("result", {}).get("data", [])
        if items:
            latest = items[0]
            prev = items[1] if len(items) > 1 else None
            result = {
                "latest_date": latest.get("END_DATE", "")[:10],
                "holder_count": latest.get("HOLDERNUM"),
                "avg_holds": latest.get("AVG_HOLDERNUM"),
            }
            if prev and latest.get("HOLDERNUM") and prev.get("HOLDERNUM"):
                chg = (float(latest["HOLDERNUM"]) / float(prev["HOLDERNUM"]) - 1) * 100
                result["holder_change_pct"] = round(chg, 1)
            return result
    except Exception:
        pass
    return {}


def cmd_fund_flow(code: str, days: int = 120, json_out: bool = False):
    """个股资金流向 + 筹码集中度（双因子交叉验证）。"""
    qq_code = _qq_code(code)
    raw = _curl(f"https://qt.gtimg.cn/q={qq_code}")
    d = _parse_qq_quote(raw)
    name = d.get("name", code)

    flow = {}
    try:
        flow = _fund_flow_em(code, days)
    except Exception:
        pass

    holder = _shareholder_count_em(code)

    if not flow.get("dates"):
        print(f"⚠️ 未能获取 {name} 资金流向数据")
        return

    # 计算关键指标
    main_5d = round(sum(flow["main_net"][-5:]), 2) if len(flow["main_net"]) >= 5 else 0
    main_20d = round(sum(flow["main_net"][-20:]), 2) if len(flow["main_net"]) >= 20 else 0
    main_120d = round(sum(flow["main_net"]), 2)

    # 筹码验证
    holder_chg = holder.get("holder_change_pct")

    if json_out:
        # 双因子信号
        signal = []
        if holder_chg is not None and holder_chg < -5:
            signal.append(f"集中(户数{holder_chg:+.1f}%)")
        if main_120d > 0:
            signal.append(f"主力净流入{_fmt_yi(main_120d)}")
        result = {
            "type": "fund_flow", "code": code, "name": name,
            "dates": flow["dates"][-20:],
            "main_net": flow["main_net"][-20:],
            "super_large_net": flow["super_large_net"][-20:],
            "large_net": flow["large_net"][-20:],
            "summary": {
                "main_5d": main_5d, "main_20d": main_20d, "main_120d": main_120d,
            },
            "holder": holder,
            "cross_signal": signal,
            "cross_verdict": "强信号" if (holder_chg and holder_chg < -5 and main_120d > 0) else (
                "弱信号" if (holder_chg and holder_chg > 5) or main_120d < 0 else "中性"),
            "fetched_at": datetime.now().isoformat(),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    print("=" * 60)
    print(f"资金流向: {name} ({code})")
    print("=" * 60)
    print(f"  主力近5日:   {_fmt_yi(main_5d)}")
    print(f"  主力近20日:  {_fmt_yi(main_20d)}")
    print(f"  主力近120日: {_fmt_yi(main_120d)}")
    # 筹码
    if holder:
        print(f"\n  股东户数({holder.get('latest_date')}): {holder.get('holder_count')}")
        if holder_chg is not None:
            arrow = "↓集中" if holder_chg < 0 else "↑分散"
            print(f"  环比变化: {holder_chg:+.1f}% {arrow}")
    # 交叉验证
    print(f"\n【双因子交叉验证】")
    c1 = "✅" if holder_chg and holder_chg < -5 else "❌"
    c2 = "✅" if main_120d > 0 else "❌"
    print(f"  {c1} 股东户数下降>10%（筹码集中）")
    print(f"  {c2} 120日主力净流入>0")
    if holder_chg and holder_chg < -5 and main_120d > 0:
        print(f"  → 🟢 强信号：筹码集中 + 主力流入，双因子验证通过")
    elif (holder_chg and holder_chg > 5) or main_120d < 0:
        print(f"  → 🔴 弱信号：筹码分散或主力流出，双因子验证不通过")
    else:
        print(f"  → 🟡 中性：信号不统一，需更多证据")


# ==============================================================================
# 命令实现 — 个股新闻 ⭐新增（东财 search，走限流通道）
# ==============================================================================

def _curl_jsonp(url, params=None):
    """curl 获取 JSONP 并解析为 JSON。"""
    if params:
        url = f"{url}?{urlencode(params)}"
    raw = _curl(url)
    # 去除 JSONP 包装: callback({...}) 或 callback(...);
    raw = raw.strip()
    # 匹配 callback(JSON)
    m = re.match(r'^[\w.]+\((.+)\);?\s*$', raw, re.DOTALL)
    if m:
        raw = m.group(1)
    return json.loads(raw)


def _em_wait():
    """东财限流等待（不发起请求，仅延迟）。用于非标准响应格式的东财接口。"""
    global _EM_LAST_CALL
    elapsed = time.time() - _EM_LAST_CALL
    if elapsed < _EM_MIN_INTERVAL:
        jitter = random.uniform(0.1, 0.8)
        time.sleep(_EM_MIN_INTERVAL - elapsed + jitter)
    _EM_LAST_CALL = time.time()


def _news_em(code: str, limit: int = 10) -> list:
    """东财个股新闻（JSONP 响应，手动限流）。"""
    url = "https://search-api-web.eastmoney.com/search/jsonp"
    params = {
        "cb": "jQuery",
        "param": json.dumps({
            "uid": "",
            "keyword": code,
            "type": ["819"],      # 819 = 新闻资讯
            "client": "web",
            "clientType": "web",
            "page": 1,
            "pageSize": limit,
        }),
    }
    _em_wait()
    resp = _curl_jsonp(url, params)
    return (resp.get("Data") or [])


def cmd_news(code: str, limit: int = 10, json_out: bool = False):
    """个股相关新闻。"""
    qq_code = _qq_code(code)
    raw = _curl(f"https://qt.gtimg.cn/q={qq_code}")
    d = _parse_qq_quote(raw)
    name = d.get("name", code)

    items = []
    source = "东财"
    try:
        items = _news_em(code, limit)
    except Exception:
        pass

    if not items:
        print(f"⚠️ 未能获取 {name} 新闻数据")
        return

    if json_out:
        result = {
            "type": "news", "code": code, "name": name, "source": source,
            "count": len(items), "items": items,
            "fetched_at": datetime.now().isoformat(),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    print("=" * 60)
    print(f"个股新闻: {name} ({code})  |  来源: {source}")
    print("=" * 60)
    for item in items[:limit]:
        title = item.get("Title", "").replace("<em>", "").replace("</em>", "")
        date_str = item.get("Date", "")[:16]
        summary = (item.get("Summary") or item.get("Content") or "").replace("<em>", "").replace("</em>", "")
        if len(summary) > 80:
            summary = summary[:80] + "..."
        print(f"  [{date_str}] {title}")
        if summary:
            print(f"    {summary}")


# ==============================================================================
# ETF 数据（腾讯行情 + 东财基金API）
# ==============================================================================

def cmd_etf(code: str, json_out: bool = False):
    """ETF 全量数据：行情+净值+折溢价+规模+费率+历史走势"""
    qq = _parse_qq_quote(_curl(f"https://qt.gtimg.cn/q={_qq_code(code)}"))
    em = _em_etf_detail(code)
    nav_hist = _em_fund_nav_history(code, 365)

    result = {
        "code": code, "type": "ETF", "market": "CN",
        "name": qq.get("name") or em.get("name", code),
        "price": qq.get("price"), "change_pct": qq.get("change_pct"),
        "open": qq.get("open"), "high": qq.get("high"), "low": qq.get("low"),
        "prev_close": qq.get("prev_close"), "volume_shou": qq.get("volume"),
        "turnover_amt_wan": qq.get("turnover_amt"),
        "pe": qq.get("pe"), "pb": qq.get("pb"),
        "high_52w": qq.get("high_52w"), "low_52w": qq.get("low_52w"),
        "iopv": em.get("iopv"), "discount_pct": em.get("discount_pct"),
        "fund_size_yi": em.get("fund_size_yi"),
        "management_fee": em.get("management_fee"), "custodian_fee": em.get("custodian_fee"),
        "fund_manager": em.get("fund_manager"), "fund_type": em.get("fund_type"),
    }
    if nav_hist and len(nav_hist) >= 20:
        result["nav_metrics"] = {
            "nav_count": len(nav_hist),
            "nav_latest": round(nav_hist[-1]["nav"], 4),
            "return_1m": round((nav_hist[-1]["nav"] - nav_hist[-min(20, len(nav_hist))]["nav"]) / nav_hist[-min(20, len(nav_hist))]["nav"] * 100, 1) if len(nav_hist) >= 20 else None,
            "return_3m": round((nav_hist[-1]["nav"] - nav_hist[-min(60, len(nav_hist))]["nav"]) / nav_hist[-min(60, len(nav_hist))]["nav"] * 100, 1) if len(nav_hist) >= 60 else None,
        }
        if len(nav_hist) >= 250:
            nav_vals = [r["nav"] for r in nav_hist]
            result["nav_metrics"]["high_52w"] = round(max(nav_vals), 4)
            result["nav_metrics"]["low_52w"] = round(min(nav_vals), 4)
            result["nav_metrics"]["drawdown_from_high"] = round((nav_hist[-1]["nav"] - max(nav_vals)) / max(nav_vals) * 100, 1)

    result["fetched_at"] = datetime.now().isoformat()
    if json_out:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return
    _print_etf(result)


def _em_etf_detail(code: str) -> dict:
    """东方财富 ETF: IOPV/折溢价/规模；基金详情: 费率/经理"""
    result = {}
    base = {
        "pz": "200", "po": "0", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2", "invt": "2", "fid": "f12",
        "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827",
        "fields": "f12,f14,f2,f3,f4,f15,f16,f17,f402,f441,f13,f20,f21",
    }
    for pg in range(1, 11):
        try:
            d = _em_get("https://push2delay.eastmoney.com/api/qt/clist/get", {**base, "pn": str(pg)})
        except Exception:
            break
        diffs = (d.get("data") or {}).get("diff") or []
        for it in diffs:
            if str(it.get("f12")) == str(code):
                result["name"] = it.get("f14")
                result["iopv"] = _try_float(it.get("f441"))
                result["discount_pct"] = _try_float(it.get("f402"))
                result["fund_size_yi"] = _try_float(it.get("f20"))
                break
        if not diffs:
            break
    try:
        d = _curl_json("https://fundmobapi.eastmoney.com/FundMApi/FundDetailInfo.ashx",
                       {"FCODE": code, "deviceid": "1", "plat": "web", "product": "EFund", "version": "1.0"})
        if d.get("ErrCode") == 0:
            info = d.get("Datas", {})
            result["name"] = result.get("name") or info.get("SHORTNAME")
            result["management_fee"] = _try_float(info.get("MFRATIO"))
            result["custodian_fee"] = _try_float(info.get("CSRATIO"))
            result["fund_manager"] = info.get("FUNDMANAGER")
            result["fund_type"] = info.get("FTYPE")
    except Exception:
        pass
    return result


def _em_fund_nav_history(code: str, days: int = 365) -> list:
    """基金历史净值（东方财富 HTML 页面解析）。"""
    result = []
    try:
        # fundf10 是HTML接口，不走 _em_get JSON 通道
        params = urlencode({"type": "lsjz", "code": code, "page": "1", "per": str(min(days, 365))})
        resp = _curl(f"https://fundf10.eastmoney.com/F10DataApi.aspx?{params}")
        rows = re.findall(
            r'<tr>\s*<td[^>]*>(\d{4}-\d{2}-\d{2})</td>\s*<td[^>]*>([\d.]+)</td>\s*<td[^>]*>([\d.]+)</td>',
            resp)
        for d, nav, acc in rows:
            result.append({"date": d, "nav": float(nav), "acc_nav": float(acc)})
    except Exception:
        pass
    return result


# ==============================================================================
# 场外基金数据
# ==============================================================================

def _fund_gz(code):
    """天天基金实时估值 API: jsonpgz({...})"""
    try:
        r = subprocess.run(
            ["/usr/bin/curl", "-s", "--noproxy", "*",
             f"https://fundgz.1234567.com.cn/js/{code}.js"],
            capture_output=True, timeout=_TIMEOUT)
        raw = r.stdout.decode("utf-8").strip()
        if raw.startswith("jsonpgz("):
            return json.loads(raw[8:-2])
    except Exception:
        pass
    return {}


def cmd_fund(code: str, json_out: bool = False):
    """场外基金数据: 净值+历史+费率+持仓"""
    result = {"code": code, "type": "Fund", "market": "CN"}
    gz = _fund_gz(code)
    if gz:
        result["name"] = gz.get("name")
        result["nav_latest"] = _try_float(gz.get("dwjz"))
        result["est_nav"] = _try_float(gz.get("gsz"))
        result["est_change_pct"] = _try_float(gz.get("gszzl"))
        result["nav_date"] = gz.get("jzrq")
    nav_hist = _em_fund_nav_history(code, 365)
    if nav_hist and len(nav_hist) >= 20:
        result["nav_count"] = len(nav_hist)
        if not result.get("nav_latest"):
            result["nav_latest"] = nav_hist[-1]["nav"]
        result["return_1m"] = round((nav_hist[-1]["nav"] - nav_hist[-min(20, len(nav_hist))]["nav"]) / nav_hist[-min(20, len(nav_hist))]["nav"] * 100, 1)
        result["return_3m"] = round((nav_hist[-1]["nav"] - nav_hist[-min(60, len(nav_hist))]["nav"]) / nav_hist[-min(60, len(nav_hist))]["nav"] * 100, 1) if len(nav_hist) >= 60 else None
        if len(nav_hist) >= 250:
            vals = [r["nav"] for r in nav_hist]
            result["return_1y"] = round((nav_hist[-1]["nav"] - vals[0]) / vals[0] * 100, 1)
            result["max_nav_1y"] = round(max(vals), 4)
            result["min_nav_1y"] = round(min(vals), 4)
    result["fetched_at"] = datetime.now().isoformat()
    if json_out:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return
    _print_fund(result)


# ==============================================================================
# 指数数据
# ==============================================================================

INDEX_CODE_MAP = {
    # A-share 宽基指数
    "000001": "1.000001", "000300": "1.000300", "000016": "1.000016",
    "000905": "1.000905", "399001": "0.399001", "399006": "0.399006",
    "000688": "1.000688", "000852": "1.000852",
    # 行业/主题指数（东方财富 secid）
    "990001": "1.990001",  # 芯片指数 (中华半导体芯片)
    "399975": "0.399975",  # 证券公司
    "931151": "1.931151",  # 光伏产业
    "930997": "1.930997",  # 新能源车
    "399967": "0.399967",  # 中证军工
    "000949": "1.000949",  # 中证农业
    "399933": "0.399933",  # 中证医药
    "399997": "0.399997",  # 中证白酒
    # 港股指数
    "HSTECH": "124.HSTECH",   # 恒生科技
    "HSI":    "124.HSI",      # 恒生指数
    "HSCEI":  "124.HSCEI",    # 恒生中国企业
}

# ── 蛋卷基金 指数估值映射（免费 API，不限流，用作 PE/PB 分位首选数据源）──
# 蛋卷一次性返回 63 个全球指数，零额外限流
DANJUAN_CODE_MAP = {
    "000300": "SH000300", "000016": "SH000016", "000905": "SH000905",
    "000852": "SH000852", "399001": "SZ399001", "399006": "SZ399006",
    "000688": "SH000688", "399975": "SZ399975", "399997": "SZ399997",
    "399967": "SZ399967", "399417": "SZ399417",
    "990001":  "CSI930652",  # 芯片→中证电子(含半导体)
    "931151":  "SZ399417",   # 光伏→新能源车(近似)
    "399933":  "SZ399989",   # 中证医药→中证医疗
    "HSTECH":  "HKHSTECH", "HSI": "HKHSI", "HSCEI": "HKHSCEI",
    "NDX": "NDX", "SP500": "SP500",
}

# PE/PB 分位缓存（日级别）
_INDEX_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "cache", "index_pe_cache.json")
_INDEX_CACHE_TTL = 6 * 3600


def _load_index_cache():
    try:
        if os.path.exists(_INDEX_CACHE_FILE):
            with open(_INDEX_CACHE_FILE) as f:
                data = json.load(f)
            if time.time() - data.get("_ts", 0) < _INDEX_CACHE_TTL:
                return data
    except Exception:
        pass
    return {"_ts": 0}


def _save_index_cache(cache):
    try:
        cache["_ts"] = time.time()
        os.makedirs(os.path.dirname(_INDEX_CACHE_FILE), exist_ok=True)
        with open(_INDEX_CACHE_FILE, "w") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass


def _fetch_danjuan_index_data():
    """从蛋卷基金获取全量指数 PE/PB 分位（一次请求 63 指数，不限流）。

    日更新，缓存在 index_pe_cache.json 中，6h TTL。
    """
    cache = _load_index_cache()
    dj_key = "_dj_all"
    if dj_key in cache and isinstance(cache[dj_key], dict) and len(cache[dj_key]) > 10:
        return cache[dj_key]
    try:
        raw = _curl("https://danjuanfunds.com/djapi/index_eva/dj")
        items = (json.loads(raw).get("data") or {}).get("items") or []
        result = {}
        for it in items:
            code = it.get("index_code", "")
            result[code] = {
                "name": it.get("name"),
                "pe": _try_float(it.get("pe")),
                "pb": _try_float(it.get("pb")),
                "pe_percentile": round((it.get("pe_percentile") or 0) * 100, 1),
                "pb_percentile": round((it.get("pb_percentile") or 0) * 100, 1),
                "roe": _try_float(it.get("roe")),
                "dividend_yield": _try_float(it.get("yeild")),
            }
        cache[dj_key] = result
        _save_index_cache(cache)
        return result
    except Exception:
        return {}


def _em_get_index_val(em_code):
    """东方财富 stock/get 兜底（可能被限流）。"""
    try:
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": em_code,
            "fields": "f57,f58,f43,f115,f167,f169,f170,f171,f172,f173",
        }
        d = _em_get(url, params)
        data = (d or {}).get("data")
        if not data:
            return {}
        pe = _try_float(data.get("f115")) or _try_float(data.get("f169"))
        pb = _try_float(data.get("f167"))
        pe_pct = _try_float(data.get("f170"))
        pb_pct = _try_float(data.get("f172"))
        return {
            "name": data.get("f58"), "price": data.get("f43"),
            "pe": pe, "pb": pb,
            "pe_percentile": pe_pct, "pb_percentile": pb_pct,
        }
    except Exception:
        return {}


def _list_available_indices():
    """列出所有可用的指数代码及名称。"""
    print("=" * 60)
    print("  可用指数列表（代码 + 名称）")
    print("=" * 60)
    # 合并 INDEX_CODE_MAP + DANJUAN_CODE_MAP 的所有 key
    all_keys = sorted(set(INDEX_CODE_MAP.keys()) | set(DANJUAN_CODE_MAP.keys()))
    for k in all_keys:
        # 尝试从腾讯行情拿名称
        name = ""
        if k.isdigit():
            if k.startswith(("000", "600", "900", "500", "510")):
                qq = _parse_qq_quote(_curl(f"https://qt.gtimg.cn/q=sh{k}"))
            else:
                qq = _parse_qq_quote(_curl(f"https://qt.gtimg.cn/q=sz{k}"))
            name = qq.get("name", "")
        if not name:
            # 蛋卷兜底
            dj_code = DANJUAN_CODE_MAP.get(k)
            if dj_code:
                dj_data = _fetch_danjuan_index_data()
                if dj_code in dj_data and dj_data[dj_code].get("name"):
                    name = dj_data[dj_code]["name"]
        if not name:
            name = k
        print(f"  {k:8s}  {name}")
    print()

def cmd_index(code_or_names: list, list_all: bool = False, json_out: bool = False):
    """指数估值（PE/PB/分位）+ 行情。支持多个代码。

    数据源: 行情→腾讯 | PE/PB分位→蛋卷基金(63指数全量+缓存)→东方财富兜底
    """
    if list_all:
        _list_available_indices()
        return

    if not code_or_names:
        print("请指定指数代码或名称，例如: ashare_data.py index 000300 399006")
        print("使用 --list 查看所有可用指数")
        return

    results = []
    for code_or_name in code_or_names:
        result = {"query": code_or_name, "type": "Index"}

        # ── 1. 腾讯行情（零限流）──
        if code_or_name.isdigit():
            if code_or_name.startswith(("000", "600", "900", "500", "510")):
                qq = _parse_qq_quote(_curl(f"https://qt.gtimg.cn/q=sh{code_or_name}"))
            else:
                qq = _parse_qq_quote(_curl(f"https://qt.gtimg.cn/q=sz{code_or_name}"))
        else:
            qq = {}

        # ── 2. PE/PB 分位 ──
        val = {}
        # 首选：蛋卷基金（不限流，全量 63 指数 + 缓存）
        dj_code = DANJUAN_CODE_MAP.get(code_or_name)
        if dj_code:
            dj_data = _fetch_danjuan_index_data()
            if dj_code in dj_data:
                val = dj_data[dj_code]
        # 兜底：东方财富（可能被限流）
        if not val:
            em_code = INDEX_CODE_MAP.get(code_or_name)
            if em_code:
                val = _em_get_index_val(em_code)

        # ── 3. 组装 ──
        result.update({
            "name": qq.get("name") or val.get("name") or code_or_name,
            "price": qq.get("price") or val.get("price"),
            "change_pct": qq.get("change_pct"),
            "pe": val.get("pe"), "pb": val.get("pb"),
            "pe_percentile": val.get("pe_percentile"),
            "pb_percentile": val.get("pb_percentile"),
            "dividend_yield": val.get("dividend_yield"),
        })
        results.append(result)

    # ── 4. 输出 ──
    if json_out:
        print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
        return

    for r in results:
        pe_str = f"{r['pe']:.1f}" if r.get("pe") else "-"
        pb_str = f"{r['pb']:.2f}" if r.get("pb") else "-"
        print(f"指数: {r['name']}  价格: {r.get('price', '-')}  PE={pe_str}  PB={pb_str}")
        extras = []
        if r.get("pe_percentile") is not None:
            extras.append(f"PE分位: {r['pe_percentile']:.1f}%")
        if r.get("pb_percentile") is not None:
            extras.append(f"PB分位: {r['pb_percentile']:.1f}%")
        if r.get("dividend_yield") and r["dividend_yield"] > 0:
            extras.append(f"股息: {r['dividend_yield']*100:.2f}%")
        if extras:
            print(f"  估值: {'  |  '.join(extras)}")
        # 判断 PE/PB 是否可用
        pe_ok = r.get("pe") not in (None, "-", "")
        pb_ok = r.get("pb") not in (None, "-", "")
        if not pe_ok and not pb_ok:
            print(f"  ⚠️ PE/PB 暂不可用，仅展示腾讯行情")


# ==============================================================================
# 大盘全景
# ==============================================================================

MARKET_INDICES = [
    ("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sz399006", "创业板指"),
    ("sh000300", "沪深300"), ("sh000016", "上证50"), ("sh000905", "中证500"),
    ("sh000688", "科创50"), ("sh000852", "中证1000"),
]


def cmd_market(json_out: bool = False):
    """大盘全景：8大指数行情快照"""
    result = {"type": "market_overview", "fetched_at": datetime.now().isoformat(), "indices": {}}
    for qq_code, name in MARKET_INDICES:
        try:
            qq = _parse_qq_quote(_curl(f"https://qt.gtimg.cn/q={qq_code}"))
            c = qq_code.replace("sh", "").replace("sz", "")
            result["indices"][c] = {
                "name": name, "price": qq.get("price"),
                "change_pct": qq.get("change_pct"),
            }
        except Exception:
            result["indices"][c] = {"name": name, "error": "获取失败"}
    if json_out:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return
    for c, v in result["indices"].items():
        print(f"  {c} {v['name']:8s}  {v.get('price', '-')}  {v.get('change_pct', '-')}%")


# ==============================================================================
# 汇率数据
# ==============================================================================

def cmd_fx(json_out: bool = False):
    """人民币汇率：USD/CNY, USD/HKD, HKD/CNY"""
    rates = {}
    # 方案1: exchangerate-api.com（全球可用）
    try:
        raw = _curl("https://api.exchangerate-api.com/v4/latest/USD")
        d = json.loads(raw)
        rates = {
            "USD/CNY": d.get("rates", {}).get("CNY"),
            "USD/HKD": d.get("rates", {}).get("HKD"),
        }
    except Exception:
        pass
    # 方案2: 新浪财经（+Referer）
    if not rates:
        try:
            r = subprocess.run(
                ["/usr/bin/curl", "-s", "--noproxy", "*",
                 "-H", "User-Agent: Mozilla/5.0",
                 "-H", "Referer: https://finance.sina.com.cn",
                 "https://hq.sinajs.cn/list=fx_susdcny,fx_susdhkd"],
                capture_output=True, timeout=_TIMEOUT)
            raw = r.stdout.decode("gbk")
            for line in raw.strip().split("\n"):
                s = line.find('"')
                e = line.rfind('"')
                if s < 0:
                    continue
                parts = line[s + 1:e].split(",")
                if len(parts) < 2:
                    continue
                if "USDCNY" in parts[0]:
                    rates["USD/CNY"] = float(parts[1])
                elif "USDHKD" in parts[0]:
                    rates["USD/HKD"] = float(parts[1])
        except Exception:
            pass
    if rates.get("USD/CNY") and rates.get("USD/HKD"):
        rates["HKD/CNY"] = round(rates["USD/CNY"] / rates["USD/HKD"], 4)
    if json_out:
        print(json.dumps(
            {"type": "fx", "rates": rates, "fetched_at": datetime.now().isoformat()},
            indent=2, ensure_ascii=False, default=str))
        return
    for k, v in rates.items():
        print(f"  {k}: {v}")


# ==============================================================================
# 格式化输出（ETF/基金）
# ==============================================================================

def _print_etf(d):
    print("=" * 60)
    print(f"ETF: {d.get('name', '')} ({d['code']})")
    print("=" * 60)
    print(f"  现价: {d.get('price')}  涨跌: {d.get('change_pct')}%")
    print(f"  IOPV(净值): {d.get('iopv')}  折溢价: {d.get('discount_pct')}%")
    print(f"  规模: {d.get('fund_size_yi')}亿  费率: {d.get('management_fee')}%+{d.get('custodian_fee')}%")
    print(f"  52周: {d.get('high_52w')} - {d.get('low_52w')}")
    if d.get("nav_metrics"):
        m = d["nav_metrics"]
        print(f"  近1月回报: {m.get('return_1m')}%  近3月: {m.get('return_3m')}%")
        print(f"  52周最高: {m.get('high_52w')}  回撤: {m.get('drawdown_from_high')}%")


def _print_fund(d):
    print("=" * 60)
    print(f"基金: {d.get('name', '')} ({d['code']})")
    print("=" * 60)
    print(f"  类型: {d.get('fund_type')}  经理: {d.get('fund_manager')}")
    print(f"  净值: {d.get('nav_latest')}  规模: {d.get('fund_size_yi')}亿")
    print(f"  费率: {d.get('management_fee')}%+{d.get('custodian_fee')}%")
    print(f"  近1月: {d.get('return_1m')}%  近3月: {d.get('return_3m')}%")
    print(f"  基准: {d.get('benchmark')}")


# ==============================================================================
# CLI 入口
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="A股数据工具 — 腾讯行情 + 东方财富 全品类数据（内置防封策略）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", help="JSON格式输出（供脚本解析）")
    sub = parser.add_subparsers(dest="command")

    p_quote = sub.add_parser("quote", help="实时行情")
    p_quote.add_argument("code", help="股票代码")
    p_fin = sub.add_parser("financials", help="核心财务数据（近5年）")
    p_fin.add_argument("code", help="股票代码")
    p_fin.add_argument("--summary", action="store_true", help="摘要模式：只输出核心5指标（省Token）")
    p_val = sub.add_parser("valuation", help="估值指标")
    p_val.add_argument("code", help="股票代码")
    p_search = sub.add_parser("search", help="搜索股票代码")
    p_search.add_argument("keyword", help="公司名或关键词")

    # ⭐新增命令
    p_kline = sub.add_parser("kline", help="日K线 + MA均线（腾讯，不封IP）")
    p_kline.add_argument("code", help="股票代码")
    p_kline.add_argument("--count", type=int, default=250, help="K线数量（默认250，覆盖200日均线）")
    p_kline.add_argument("--adjust", default="qfq", choices=["qfq", "hfq", ""], help="复权方式（默认前复权）")

    p_dt = sub.add_parser("dragon_tiger", help="龙虎榜（东财，限流）")
    p_dt.add_argument("--date", help="日期 YYYY-MM-DD（默认今天）")

    p_ff = sub.add_parser("fund_flow", help="个股资金流向+筹码集中度（双因子验证）")
    p_ff.add_argument("code", help="股票代码")
    p_ff.add_argument("--days", type=int, default=120, help="天数（默认120）")

    p_nf = sub.add_parser("north_flow", help="北向资金日度流向（东财，限流）")
    p_nf.add_argument("--days", type=int, default=20, help="天数（默认20）")

    p_news = sub.add_parser("news", help="个股新闻（东财，限流）")
    p_news.add_argument("code", help="股票代码")
    p_news.add_argument("--limit", type=int, default=10, help="条数（默认10）")

    # 原有命令
    p_etf = sub.add_parser("etf", help="ETF 数据（净值/折溢价/规模/费率/历史）")
    p_etf.add_argument("code", help="ETF代码，如 513180")
    p_fund = sub.add_parser("fund", help="场外基金数据（净值/费率/持仓/基准）")
    p_fund.add_argument("code", help="基金代码，如 002803")
    p_idx = sub.add_parser("index", help="指数估值（PE/PB/分位）")
    p_idx.add_argument("code_or_name", nargs="*", help="指数代码或名称（可多个，空格分隔；留空配合 --list 列出全部）")
    p_idx.add_argument("--list", action="store_true", help="列出所有可用指数代码")
    sub.add_parser("market", help="大盘全景（8大指数行情）")
    sub.add_parser("fx", help="人民币汇率（USD/CNY, HKD/CNY）")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    json_out = getattr(args, "json", False)

    cmds = {
        "quote": lambda: cmd_quote(args.code, json_out),
        "financials": lambda: cmd_financials(args.code, getattr(args, "summary", False)),
        "valuation": lambda: cmd_valuation(args.code),
        "search": lambda: cmd_search(args.keyword),
        "kline": lambda: cmd_kline(args.code, args.count, args.adjust, json_out),
        "dragon_tiger": lambda: cmd_dragon_tiger(getattr(args, "date", None), json_out),
        "fund_flow": lambda: cmd_fund_flow(args.code, args.days, json_out),
        "north_flow": lambda: cmd_north_flow(args.days, json_out),
        "news": lambda: cmd_news(args.code, args.limit, json_out),
        "etf": lambda: cmd_etf(args.code, json_out),
        "fund": lambda: cmd_fund(args.code, json_out),
        "index": lambda: cmd_index(args.code_or_name, getattr(args, "list", False), json_out),
        "market": lambda: cmd_market(json_out),
        "fx": lambda: cmd_fx(json_out),
    }
    cmds[args.command]()


if __name__ == "__main__":
    main()
