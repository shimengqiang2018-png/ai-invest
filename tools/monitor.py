#!/usr/bin/env python3
"""
ETF 网格交易实时监控 — 每 10 秒刷新，全标的覆盖，醒目标注触发信号。

用法:
  python3 tools/monitor.py                持续监控（每 10s 刷新）
  python3 tools/monitor.py --once         一次性快照
  python3 tools/monitor.py --interval 5   自定义刷新间隔（秒）
  python3 tools/monitor.py --etf 512880,513180   只监控指定标的
  python3 tools/monitor.py --alert-only   仅在有告警时输出
  python3 tools/monitor.py --no-color     纯文本模式（适合日志/管道）

数据源: 腾讯行情 (qt.gtimg.cn，不限频) + 腾讯日K (web.ifzq.gtimg.cn)
零外部依赖，纯 Python stdlib + curl。
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

# ============================================================
# 配置区：ETF 列表 + 网格参数 + 告警阈值
# ============================================================

# A 组：已开网格（核心监控 — 价格 + 趋势 + 触发价）
CORE_ETFS = {
    "512880": {
        "name": "证券ETF", "group": "A-核心", "market": "sh",
        "base_price": 1.128, "spacing_up": 3.5, "spacing_down": 2.5,
        "levels_up": 5, "levels_down": 5, "shares_per_grid": 300,
        "stop_loss": 0.940,
    },
    "513180": {
        "name": "恒生科技", "group": "A-核心", "market": "sh",
        "base_price": 0.595, "spacing_up": 3.5, "spacing_down": 2.5,
        "levels_up": 4, "levels_down": 4, "shares_per_grid": 300,
        "stop_loss": 0.525,
    },
    "512690": {
        "name": "酒ETF", "group": "A-核心", "market": "sh",
        "base_price": 0.425, "spacing_up": 3.0, "spacing_down": 2.0,
        "levels_up": 5, "levels_down": 5, "shares_per_grid": 700,
        "stop_loss": 0.354,
    },
}

# B 组：持仓观察（价格 + 趋势，无网格触发价）
HOLD_ETFS = {
    "159920": {"name": "恒生ETF", "group": "B-持仓", "market": "sz"},
    "510300": {"name": "沪深300", "group": "B-持仓", "market": "sh"},
    "159915": {"name": "创业板", "group": "B-持仓", "market": "sz"},
}

# C 组：候选观察（仅趋势，等信号开仓）
WATCH_ETFS = {
    "513330": {"name": "恒生互联", "group": "C-候选", "market": "sh"},
    "512010": {"name": "医药ETF", "group": "C-候选", "market": "sh"},
    "513050": {"name": "中概互联", "group": "C-候选", "market": "sh"},
    "512100": {"name": "中证1000", "group": "C-候选", "market": "sh"},
    "515030": {"name": "新能源车", "group": "C-候选", "market": "sh"},
    "512660": {"name": "军工ETF", "group": "C-候选", "market": "sh"},
    "515790": {"name": "光伏ETF", "group": "C-候选", "market": "sh"},
    "512760": {"name": "芯片ETF", "group": "C-候选", "market": "sh"},
}

ALL_ETFS = {**CORE_ETFS, **HOLD_ETFS, **WATCH_ETFS}

# 告警阈值
ALERT_PROXIMITY_PCT = 0.8     # 价格距触发价 ≤ 0.8% 时告警
ALERT_STOP_LOSS_PCT = 3.0     # 价格距止损价 ≤ 3% 时告警
ALERT_PREMIUM_PCT = 1.5       # IOPV 折溢价 > 1.5% 时告警
KLINES_REFRESH_SEC = 300      # K 线数据每 5 分钟刷新一次
QUOTE_TIMEOUT = 5             # 行情请求超时（秒）
KLINES_COUNT = 120            # K 线数量（用于 MA/BB/ATR）

# ANSI 颜色
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_RED = "\033[31m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_CYAN = "\033[36m"
C_WHITE = "\033[37m"
C_BG_RED = "\033[41m"
C_BG_GREEN = "\033[42m"
C_BG_YELLOW = "\033[43m"
C_BLINK = "\033[5m"

# ============================================================
# 工具函数
# ============================================================


def _curl_text(url, timeout=10, encoding=None):
    """用 curl 直连公开接口，返回文本。"""
    try:
        result = subprocess.run(
            ["/usr/bin/curl", "-s", "--noproxy", "*",
             "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
             url],
            capture_output=True, timeout=timeout,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        if encoding:
            return result.stdout.decode(encoding, errors="replace")
        try:
            return result.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return result.stdout.decode("gbk", errors="replace")
    except Exception:
        return None


def _qq_code(code):
    """股票代码 → 腾讯行情前缀。"""
    code = code.strip()
    if code.startswith(("6", "9", "5")):
        return f"sh{code}"
    return f"sz{code}"


def _to_float(value, default=None):
    """安全转 float。"""
    if value is None or value == "" or value == "-":
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


# ============================================================
# 行情获取
# ============================================================


def fetch_batch_quotes(codes):
    """腾讯 API 批量获取行情（一次 HTTP 请求）。
    返回 {code: {price, change_pct, high, low, volume, name, prev_close, quote_time}, ...}
    """
    qq_codes = ",".join(_qq_code(c) for c in codes)
    url = f"https://qt.gtimg.cn/q={qq_codes}"
    raw = _curl_text(url, timeout=QUOTE_TIMEOUT, encoding="gbk")
    if not raw:
        return {}

    results = {}
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        try:
            # 格式: v_sh512880="1~证券ETF国泰~512880~1.128~..."
            code_part = line.split("=")[0]  # v_sh512880
            code = code_part.replace("v_sh", "").replace("v_sz", "")
            start = line.find('"')
            end = line.rfind('"')
            if start < 0 or end <= start:
                continue
            fields = line[start + 1:end].split("~")
            if len(fields) < 40:
                continue

            results[code] = {
                "name": fields[1] if len(fields) > 1 else code,
                "price": _to_float(fields[3]),
                "prev_close": _to_float(fields[4]),
                "open": _to_float(fields[5]),
                "high": _to_float(fields[33]),
                "low": _to_float(fields[34]),
                "volume": fields[6] if len(fields) > 6 else "",
                "change_pct": _to_float(fields[32]),
                "quote_time": fields[30] if len(fields) > 30 else "",
            }
        except Exception:
            continue
    return results


def fetch_index_quote(code="000001"):
    """获取上证指数行情。"""
    qq = f"sh{code}" if code.startswith("0") else _qq_code(code)
    raw = _curl_text(f"https://qt.gtimg.cn/q={qq}", timeout=5, encoding="gbk")
    if not raw:
        return None
    try:
        start = raw.find('"')
        end = raw.rfind('"')
        if start < 0 or end <= start:
            return None
        fields = raw[start + 1:end].split("~")
        return {
            "price": _to_float(fields[3]),
            "change_pct": _to_float(fields[32]),
            "name": fields[1],
        }
    except Exception:
        return None


# ============================================================
# K 线获取 + 技术指标
# ============================================================


def fetch_klines(code, count=KLINES_COUNT):
    """获取日K线收盘价序列（腾讯）。返回 [close, ...]，最早在前。"""
    prefix = "sh" if code.startswith(("6", "9", "5")) else "sz"
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={prefix}{code},day,,,{count},qfq")
    try:
        raw = _curl_text(url, timeout=10)
        if not raw:
            return []
        data = json.loads(raw)
        days = (data.get("data", {}).get(f"{prefix}{code}", {})
                     .get("qfqday", []) or
                data.get("data", {}).get(f"{prefix}{code}", {})
                     .get("day", []))
        closes = [float(d[2]) for d in days if len(d) > 2]
        return closes
    except Exception:
        return []


def calc_ma(closes, period):
    """简单移动平均（最新值）。"""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def calc_bollinger(closes, period=20, std_mult=2.0):
    """布林带。返回 (middle, upper, lower, width_pct)。"""
    if len(closes) < period:
        return None, None, None, None
    ma = calc_ma(closes, period)
    recent = closes[-period:]
    variance = sum((x - ma) ** 2 for x in recent) / period
    std = variance ** 0.5
    upper = ma + std_mult * std
    lower = ma - std_mult * std
    width = (upper - lower) / ma * 100 if ma > 0 else 0
    return ma, upper, lower, width


def calc_atr(closes, period=14):
    """简化 ATR（仅用收盘价估算真实波幅）。"""
    if len(closes) < period + 1:
        return None
    tr_list = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    if len(tr_list) < period:
        return None
    return sum(tr_list[-period:]) / period


def calc_trend_score(price, ma20, ma60, bb_width):
    """趋势评分（同 grid_trading.py 逻辑）。正=震荡有利，负=趋势不利。

    关键: 价格贴近 MA20(<1%)时优先按"MA20附近磨底/磨顶"处理,
    避免价格在MA20上下1厘钱波动导致评分在 +2 和 -2 之间跳跃。
    """
    score = 0

    if price and ma20 and ma60:
        near_ma20 = abs(price - ma20) / price < 0.01  # 价格在 MA20 ±1% 内

        if near_ma20:
            # 价格贴着 MA20: 优先按附近震荡处理，不按多头/空头排列
            if bb_width and bb_width < 8:
                score += 2  # MA20附近 + BB正常 → 理想震荡
            else:
                score += 1  # MA20附近但 BB 未确认 → 温和偏震荡
        elif price > ma20 > ma60:
            score -= 1  # 多头排列
        elif price < ma20 < ma60:
            score -= 2  # 空头排列
        elif abs(price - ma20) / price < 0.02:
            score += 2  # 价格在 MA20 附近(1-2%)
        else:
            score += 1  # 均线缠绕

    if bb_width:
        if bb_width < 4:
            score += 3  # BB 收窄
        elif bb_width < 8:
            score += 1  # BB 正常
        else:
            score -= 2  # BB 扩张

    return score


def trend_verdict(score):
    """趋势评分的文字判定。"""
    if score >= 4:
        return "✅ 震荡"
    elif score >= 1:
        return "🟢 偏震荡"
    elif score >= -1:
        return "🟡 偏趋势"
    elif score >= -3:
        return "🔴 趋势"
    else:
        return "⛔ 强趋势"


# ============================================================
# 触发价计算 + 告警判断
# ============================================================


def calc_triggers(cfg):
    """计算买卖触发价列表。返回 (buy_triggers: list[float], sell_triggers: list[float])。"""
    base = cfg["base_price"]
    buy = []
    for i in range(1, cfg.get("levels_down", 5) + 1):
        p = base * (1 - cfg["spacing_down"] / 100) ** i
        buy.append(round(p, 3))
    sell = []
    for i in range(1, cfg.get("levels_up", 5) + 1):
        p = base * (1 + cfg["spacing_up"] / 100) ** i
        sell.append(round(p, 3))
    return buy, sell


def nearest_trigger(price, buy_triggers, sell_triggers):
    """找到最近的触发价和距离。返回 (side, trigger_price, distance_pct)。"""
    nearest = None
    min_dist = float("inf")

    for t in buy_triggers:
        if t < price:
            dist = (price - t) / price * 100
            if dist < min_dist:
                min_dist = dist
                nearest = ("买入", t, dist)

    for t in sell_triggers:
        if t > price:
            dist = (t - price) / price * 100
            if dist < min_dist:
                min_dist = dist
                nearest = ("卖出", t, dist)

    return nearest if nearest else (None, None, float("inf"))


def check_alerts(code, cfg, quote, tech, triggers, prev_alerts):
    """检查所有告警条件。返回告警列表。"""
    alerts = []
    price = quote.get("price")
    if not price:
        return alerts

    buy_triggers, sell_triggers = triggers
    side, trig_price, dist = nearest_trigger(price, buy_triggers, sell_triggers)
    stop_loss = cfg.get("stop_loss")

    # 1. 买入/卖出逼近
    if side and dist <= ALERT_PROXIMITY_PCT:
        emoji = "🔴" if side == "买入" else "🟢"
        level = "HIGH" if dist <= 0.3 else "MEDIUM"
        alerts.append({
            "code": code, "name": cfg["name"], "type": f"{side}逼近",
            "msg": f"{emoji} {side}触发价 ¥{trig_price:.3f} 仅差 {dist:.1f}%",
            "level": level, "color": C_RED if side == "买入" else C_GREEN,
        })

    # 2. 止损逼近
    if stop_loss and price > 0:
        dist_sl = (price - stop_loss) / price * 100
        if dist_sl <= ALERT_STOP_LOSS_PCT:
            alerts.append({
                "code": code, "name": cfg["name"], "type": "止损逼近",
                "msg": f"⛔ 距止损价 ¥{stop_loss:.3f} 仅 {dist_sl:.1f}%，危险！",
                "level": "CRITICAL", "color": C_BG_RED + C_BLINK,
            })

    # 3. BB 宽度信号
    bb_w = tech.get("bb_width")
    prev_bb = prev_alerts.get(f"{code}_bb_w", bb_w)
    if bb_w is not None:
        if bb_w < 8:
            alerts.append({
                "code": code, "name": cfg["name"], "type": "BB收窄",
                "msg": (f"📊 {code} {cfg['name']} BB宽度 {bb_w:.1f}% "
                        f"→ 震荡市，网格最佳环境"),
                "level": "INFO", "color": C_CYAN,
            })
        elif prev_bb and prev_bb > 10 and bb_w <= 10:
            alerts.append({
                "code": code, "name": cfg["name"], "type": "BB改善",
                "msg": (f"📊 {code} {cfg['name']} BB宽度 "
                        f"从 {prev_bb:.1f}% 降到 {bb_w:.1f}% → 回归震荡"),
                "level": "MEDIUM", "color": C_YELLOW,
            })

    return alerts


# ============================================================
# 仪表盘渲染
# ============================================================


def fmt_price(p, decimals=3):
    """价格格式化。"""
    if p is None:
        return "  N/A  "
    return f"{p:{decimals + 4}.{decimals}f}"


def fmt_pct(p):
    """百分比格式化（带正负号）。"""
    if p is None:
        return "  N/A "
    return f"{p:+6.2f}%"


def colorize(text, color, use_color=True):
    """添加 ANSI 颜色。"""
    if not use_color:
        return text
    return f"{color}{text}{C_RESET}"


def render_dashboard(quotes, tech_data, triggers_map, alerts, market, opts, etf_list):
    """渲染整个仪表盘。"""
    use_color = not opts.get("no_color")
    lines = []
    sep = "═" * 78
    thin = "─" * 78

    # 标题行
    now = datetime.now().strftime("%H:%M:%S")
    mkt_str = ""
    if market and market.get("price") and market["price"] > 100:
        mkt_str = (f"上证{market['price']:.0f} "
                   f"{market.get('change_pct', 0):+.2f}%")
    header = (f"  ETF 网格监控  │  {now}  │  {mkt_str}  " if mkt_str
              else f"  ETF 网格监控  │  {now}")
    lines.append(f"╔{sep}╗")
    lines.append(f"║{colorize(header, C_BOLD + C_WHITE, use_color):<{78 + len(C_BOLD + C_WHITE + C_RESET)}}║")
    lines.append(f"╠{sep}╣")

    # 表头
    hdr = (f"  {'代码':<8s} {'名称':<8s} {'现价':>8s} {'涨跌':>8s} "
           f"{'BB宽':>5s} {'趋势':<8s} {'最近触发':>14s} {'距离':>6s}")
    lines.append(colorize(hdr, C_BOLD, use_color))

    # 按分组排序：A-核心 > B-持仓 > C-候选
    group_order = {"A-核心": 0, "B-持仓": 1, "C-候选": 2}
    sorted_etfs = sorted(etf_list.items(),
                         key=lambda x: group_order.get(x[1]["group"], 9))

    for code, cfg in sorted_etfs:
        q = quotes.get(code, {})
        tech = tech_data.get(code, {})
        price = q.get("price")
        chg = q.get("change_pct")
        bb_w = tech.get("bb_width")
        ts = tech.get("trend_score")
        tv = trend_verdict(ts) if ts is not None else "  —"

        # 最近触发
        trig_str = "     —"
        dist_str = "  —"
        if code in triggers_map:
            buy_t, sell_t = triggers_map[code]
            side, tp, d = nearest_trigger(price, buy_t, sell_t)
            if side:
                trig_str = f"{'买' if side == '买入' else '卖'}:{tp:.3f}"
                dist_str = f"{d:5.1f}%"

        p_str = fmt_price(price) if price else "   N/A  "
        c_str = fmt_pct(chg) if chg is not None else "   N/A "
        bb_str = f"{bb_w:4.1f}%" if bb_w is not None else "   —"

        # 高亮接近触发价的标的
        if price and code in triggers_map:
            _, _, d = nearest_trigger(price, *triggers_map[code])
            if d <= 1.5:
                p_str = colorize(p_str, C_BOLD + C_YELLOW, use_color)

        line = (f"  {code:<8s} {cfg['name']:<8s} {p_str:>8s} {c_str:>8s} "
                f"{bb_str:>5s} {tv:<8s} {trig_str:>14s} {dist_str:>6s}")
        lines.append(line)

    # 告警区域
    if alerts:
        lines.append(f"╠{sep}╣")
        critical = [a for a in alerts if a["level"] == "CRITICAL"]
        high = [a for a in alerts if a["level"] == "HIGH"]
        medium = [a for a in alerts if a["level"] == "MEDIUM"]
        info = [a for a in alerts if a["level"] == "INFO"]
        sorted_alerts = critical + high + medium + info

        count_str = f"⚠️ 活跃告警 ({len(alerts)})"
        lines.append(colorize(f"║  {count_str}", C_BOLD + C_YELLOW, use_color))
        for a in sorted_alerts:  # 最多显示 8 条
            lines.append(colorize(f"║  {a['msg']}", a["color"], use_color))
    elif not opts.get("alert_only"):
        lines.append(f"╠{sep}╣")
        lines.append(colorize("║  ✅ 无告警，所有标的状态正常", C_DIM + C_GREEN, use_color))

    # 底部状态栏
    lines.append(f"╠{sep}╣")
    quote_count = sum(1 for q in quotes.values() if q.get("price"))
    kline_count = sum(1 for t in tech_data.values() if t.get("bb_width"))
    status = (f"  报价: {quote_count}/{len(etf_list)}  "
              f"|  K线: {kline_count}/{len(etf_list)}  "
              f"|  刷新间隔: {opts['interval']}s  "
              f"|  Ctrl+C 退出")
    lines.append(colorize(f"║{status:<78s}║", C_DIM, use_color))
    lines.append(f"╚{sep}╝")

    return "\n".join(lines)


# ============================================================
# 主控逻辑
# ============================================================


def load_tech_data(etf_codes):
    """为指定 ETF 列表加载技术指标（K线 → MA/BB/ATR/趋势评分）。"""
    tech = {}
    for code in etf_codes:
        cfg = ALL_ETFS.get(code, {})
        closes = fetch_klines(code)
        if len(closes) < 20:
            tech[code] = {"error": "K线不足"}
            continue

        ma20 = calc_ma(closes, 20)
        ma60 = calc_ma(closes, 60)
        bb_ma, bb_upper, bb_lower, bb_width = calc_bollinger(closes)
        atr = calc_atr(closes)
        score = calc_trend_score(
            closes[-1] if closes else None, ma20, ma60, bb_width)

        tech[code] = {
            "ma20": round(ma20, 4) if ma20 else None,
            "ma60": round(ma60, 4) if ma60 else None,
            "bb_mid": round(bb_ma, 4) if bb_ma else None,
            "bb_upper": round(bb_upper, 4) if bb_upper else None,
            "bb_lower": round(bb_lower, 4) if bb_lower else None,
            "bb_width": round(bb_width, 2) if bb_width else None,
            "atr": round(atr, 5) if atr else None,
            "atr_pct": round(atr / closes[-1] * 100, 2) if atr and closes[-1] else None,
            "trend_score": score,
        }
    return tech


def build_triggers_map(etf_codes):
    """为核心 ETF 预计算触发价表。"""
    triggers = {}
    for code in etf_codes:
        cfg = CORE_ETFS.get(code)
        if cfg:
            triggers[code] = calc_triggers(cfg)
    return triggers


def main():
    parser = argparse.ArgumentParser(description="ETF 网格交易实时监控")
    parser.add_argument("--once", action="store_true", help="一次性快照")
    parser.add_argument("--interval", type=int, default=10, help="刷新间隔（秒），默认 10")
    parser.add_argument("--etf", type=str, default="", help="只监控指定标的，逗号分隔")
    parser.add_argument("--alert-only", action="store_true", help="仅在有告警时输出")
    parser.add_argument("--no-color", action="store_true", help="纯文本模式")
    args = parser.parse_args()

    # 确定监控范围
    if args.etf:
        codes = [c.strip() for c in args.etf.split(",") if c.strip() in ALL_ETFS]
        etf_list = {c: ALL_ETFS[c] for c in codes}
    else:
        codes = list(ALL_ETFS.keys())
        etf_list = ALL_ETFS
    if not codes:
        print("没有有效的 ETF 代码")
        sys.exit(1)

    opts = {
        "interval": max(args.interval, 2),
        "alert_only": args.alert_only,
        "no_color": args.no_color,
    }

    # 初始加载技术指标
    tech_data = load_tech_data(codes)
    triggers_map = build_triggers_map(codes)
    prev_alerts_state = {}  # 记录上一次告警状态，用于信号变化检测
    last_kline_refresh = time.time()
    market = None  # 大盘行情缓存
    loop_count = 0

    while True:
        loop_start = time.time()
        loop_count += 1

        # 每 5 分钟刷新 K 线
        if time.time() - last_kline_refresh > KLINES_REFRESH_SEC:
            tech_data = load_tech_data(codes)
            last_kline_refresh = time.time()

        # 获取行情
        quotes = fetch_batch_quotes(codes)
        if loop_count % 6 == 1 or loop_count == 1:
            market = fetch_index_quote("000001") or market

        # 生成告警
        all_alerts = []
        for code in codes:
            cfg = etf_list.get(code, {})
            q = quotes.get(code, {})
            tech = tech_data.get(code, {})
            trig = triggers_map.get(code, ([], []))
            alerts = check_alerts(code, cfg, q, tech, trig, prev_alerts_state)
            all_alerts.extend(alerts)

        # 更新告警状态缓存
        for code in codes:
            tech = tech_data.get(code, {})
            if tech.get("bb_width"):
                prev_alerts_state[f"{code}_bb_w"] = tech["bb_width"]

        # 渲染输出
        show_dashboard = not opts["alert_only"] or all_alerts
        if show_dashboard:
            # ANSI 清屏（持续模式）
            if not args.once:
                sys.stdout.write("\033[2J\033[H")
            dashboard = render_dashboard(quotes, tech_data, triggers_map,
                                         all_alerts, market, opts, etf_list)
            sys.stdout.write(dashboard + "\n")
            sys.stdout.flush()
        elif args.once and opts["alert_only"]:
            print("✅ 无告警")
            sys.stdout.flush()

        if args.once:
            if opts.get("alert_only") and not all_alerts:
                print("✅ 无告警")
            break

        # 控制刷新间隔
        elapsed = time.time() - loop_start
        sleep_time = max(0, opts["interval"] - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{C_RESET}监控已停止")
        sys.exit(0)
