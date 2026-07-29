#!/usr/bin/env python3
"""
网格交易管理工具 — 独立脚本，零外部依赖。

用法:
  python3 grid_trading.py table             生成网格价格表
  python3 grid_trading.py status            查看当前网格状态
  python3 grid_trading.py trigger           交互式录入成交
  python3 grid_trading.py pnl               盈亏统计
  python3 grid_trading.py risk              风险检查

首次使用：编辑下方配置区，填入你的 ETF 持仓参数。
"""

import json
import os
import subprocess
import sys
import time
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlencode

try:
    from tools.etf_market_data import load_etf_series
    from tools.trading_ledger import ExecutionConfig
except ModuleNotFoundError:  # 支持直接执行 tools/grid_trading.py
    from etf_market_data import load_etf_series
    from trading_ledger import ExecutionConfig

# ============================================================
# 配置区（按你的实际持仓修改）
# ============================================================

CONFIGS = {
    "513180": {
        "name": "恒生科技ETF",
        "base_price": "0.603",          # 动态基准价（2026-07-20 卖出触发后更新）
        "cost_price": "0.578",          # 初始持仓成本价，用于动态成本计算
        "grid_spacing_up_pct": "3.5",   # 上涨间距%（卖出方向）
        "grid_spacing_down_pct": "2.5", # 下跌间距%（买入方向）
        "grid_spacing_pct": "2.5",      # 默认间距（向后兼容，实际用 up/down）
        "levels_above": 5,              # 上方卖出层数
        "levels_below": 5,              # 下方买入层数
        "shares_per_grid": 300,         # 每格委托股数（7/9起300股，对齐条件单）
        "base_position": 10000,         # 底仓（锁定不参与网格），2026-07-17 +3100股@0.590
        "grid_position": 3000,          # 初始网格仓位（工具用trigger记录自动推算当前仓位）
        "max_position": 18100,          # 最大持仓上限（底仓10000+网格最多8100）
        "stop_loss_price": "0.530",      # 止损/暂停网格价（条件单区间下限，第-5买入层0.531）
        "note": "2026-07-20卖出@0.603，基准价更新为0.603；间距调整为+3.5%/-2.5%（上宽下窄，跟随条件单调整）",
    },
    "159920": {
        "name": "恒生ETF",
        "base_price": "1.468",          # 条件单基准价，2026-07-15 开网格
        "cost_price": "1.409",          # 当前券商持仓成本价，用于浮盈统计
        "grid_spacing_pct": "2.0",      # 间距 ±2.0%
        "levels_above": 5,
        "levels_below": 5,
        "shares_per_grid": 100,         # 每格100股
        "base_position": 2700,          # 底仓（锁定），条件单持仓下限
        "grid_position": 500,           # 网格仓位（3200 - 2700）
        "max_position": 3700,           # 条件单持仓上限
        "stop_loss_price": "1.260",     # 最低买入层 1.327 再跌 5%
        "note": "2026-07-15开网格：基准1.468，±2.0%，5+5层，100股/格，区间1.327~1.621",
    },
    "510300": {
        "name": "沪深300ETF",
        "base_price": "4.767",
        "cost_price": "4.537",
        "grid_spacing_pct": "1.80",
        "levels_above": 5,
        "levels_below": 5,
        "shares_per_grid": 100,
        "base_position": 100,
        "grid_position": 500,
        "max_position": 1100,
        "stop_loss_price": "4.200",
        "note": "2026-07-21已触发2次卖出(4.682/4.767)，基准价4.767，成本4.537(对齐券商)",
    },
    "159915": {
        "name": "创业板ETF",
        "base_price": "3.682",
        "cost_price": "3.301",
        "grid_spacing_pct": "2.50",
        "levels_above": 6,
        "levels_below": 6,
        "shares_per_grid": 100,
        "base_position": 200,
        "grid_position": 600,
        "max_position": 1400,
        "stop_loss_price": "2.940",
        "note": "2026-07-21已触发卖3.507/买3.418/卖3.505/卖3.592/卖3.682，基准3.682，成本3.301(对齐券商)",
    },
    "588000": {
        "name": "科创50ETF",
        "base_price": "1.888",
        "cost_price": "1.888",
        "grid_spacing_up_pct": "4.0",     # 上涨间距%（卖出方向，让利润奔跑）
        "grid_spacing_down_pct": "3.0",   # 下跌间距%（买入方向，控制风险）
        "grid_spacing_pct": "3.5",        # 默认间距（向后兼容）
        "levels_above": 6,
        "levels_below": 6,
        "shares_per_grid": 800,           # 每格800股（单格≈1510元）
        "base_position": 8000,            # 底仓8000股（锁定）
        "grid_position": 3200,            # 初始网格仓位（4层×800）
        "max_position": 16800,            # 最大持仓（底仓8000+网格最多8800）
        "stop_loss_price": "1.470",       # 跌破最低买入层1.572再5%
        "note": "2026-07-23新开：基准1.888，+4.0%/-3.0%，6+6层，800股/格。⚠️PE分位98.9%极高，初始建仓仅50%",
    },
    "512760": {
        "name": "芯片ETF",
        "base_price": "1.199",
        "cost_price": "1.199",
        "grid_spacing_up_pct": "4.0",
        "grid_spacing_down_pct": "3.0",
        "grid_spacing_pct": "3.5",
        "levels_above": 6,
        "levels_below": 6,
        "shares_per_grid": 1300,          # 每格1300股（单格≈1559元）
        "base_position": 12500,           # 底仓12500股（锁定）
        "grid_position": 5300,            # 初始网格仓位（≈4层×1300）
        "max_position": 26900,            # 最大持仓
        "stop_loss_price": "0.950",       # 跌破最低买入层0.999再5%
        "note": "2026-07-23新开：基准1.199，+4.0%/-3.0%，6+6层，1300股/格。半导体周期性强，波动极大",
    },
    "512880": {
        "name": "证券ETF",
        "base_price": "1.128",
        "cost_price": "1.128",
        "grid_spacing_up_pct": "3.5",
        "grid_spacing_down_pct": "2.5",
        "grid_spacing_pct": "3.0",
        "levels_above": 5,
        "levels_below": 5,
        "shares_per_grid": 1300,          # 每格1300股（单格≈1466元）
        "base_position": 10600,           # 底仓10600股（锁定）
        "grid_position": 4800,            # 初始网格仓位（≈4层×1300）
        "max_position": 23200,            # 最大持仓
        "stop_loss_price": "0.940",       # 跌破最低买入层0.994再5%
        "note": "2026-07-23新开：基准1.128，+3.5%/-2.5%，5+5层，1300股/格。牛市弹性品种，成交活跃",
    },
    "512100": {
        "name": "中证1000ETF",
        "base_price": "2.914",
        "cost_price": "2.914",
        "grid_spacing_up_pct": "3.5",
        "grid_spacing_down_pct": "2.5",
        "grid_spacing_pct": "3.0",
        "levels_above": 5,
        "levels_below": 5,
        "shares_per_grid": 500,
        "base_position": 3400,
        "grid_position": 1500,
        "max_position": 6400,
        "stop_loss_price": "2.480",
        "note": "2026-07-23新开：基准2.914，+3.5%/-2.5%，5+5层，500股/格。PB分位37%偏低，波动28%适合网格",
    },
    "513310": {
        "name": "中韩半导体ETF",
        "base_price": "5.199",
        "cost_price": "5.199",
        "grid_spacing_pct": "4.0",
        "levels_above": 6,
        "levels_below": 6,
        "shares_per_grid": 100,
        "base_position": 200,
        "grid_position": 400,
        "max_position": 1200,
        "stop_loss_price": "4.100",
        "note": "2026-07-23 候选：日成交212亿, 换手174%, 波动极大, 待评估",
    },
    "513330": {
        "name": "恒生互联网ETF",
        "base_price": "0.384",
        "cost_price": "0.384",
        "grid_spacing_pct": "3.0",
        "levels_above": 5,
        "levels_below": 5,
        "shares_per_grid": 5000,
        "base_position": 20000,
        "grid_position": 15000,
        "max_position": 60000,
        "stop_loss_price": "0.300",
        "note": "2026-07-23 候选：日成交20.7亿, 港股互联网, 低估值, 待评估",
    },
    "512660": {
        "name": "军工ETF",
        "base_price": "1.101",
        "cost_price": "1.101",
        "grid_spacing_pct": "3.0",
        "levels_above": 5,
        "levels_below": 5,
        "shares_per_grid": 1500,
        "base_position": 7000,
        "grid_position": 4500,
        "max_position": 19000,
        "stop_loss_price": "0.910",
        "note": "2026-07-23 候选：日成交3亿, PE分位38%, PB分位49%, 待评估",
    },
    "515790": {
        "name": "光伏ETF",
        "base_price": "0.839",
        "cost_price": "0.839",
        "grid_spacing_pct": "3.0",
        "levels_above": 5,
        "levels_below": 5,
        "shares_per_grid": 2000,
        "base_position": 9000,
        "grid_position": 6000,
        "max_position": 25000,
        "stop_loss_price": "0.680",
        "note": "2026-07-23 候选：日成交2.6亿, 新能源车PE分位37%, 待评估",
    },
    "512010": {
        "name": "医药ETF",
        "base_price": "0.377",
        "cost_price": "0.377",
        "grid_spacing_up_pct": "3.0",
        "grid_spacing_down_pct": "2.0",
        "grid_spacing_pct": "2.5",
        "levels_above": 6,
        "levels_below": 6,
        "shares_per_grid": 3700,          # 每格3700股（单格≈1395元）
        "base_position": 18000,           # 底仓（估）
        "grid_position": 11000,           # 初始网格仓位
        "max_position": 40000,            # 最大持仓
        "stop_loss_price": "0.305",       # 跌破最低买入层0.321再5%
        "note": "2026-07-23推荐：PE分位17.5%深度低估，PB分位8.3%，集采利空充分消化，防御性网格标的",
    },
    "512690": {
        "name": "酒ETF",
        "base_price": "0.425",
        "cost_price": "0.425",
        "grid_spacing_up_pct": "3.0",
        "grid_spacing_down_pct": "2.0",
        "grid_spacing_pct": "2.5",
        "levels_above": 5,
        "levels_below": 5,
        "shares_per_grid": 2900,          # 每格2900股（单格≈1233元）
        "base_position": 12000,           # 底仓（估）
        "grid_position": 7000,            # 初始网格仓位
        "max_position": 26000,            # 最大持仓
        "stop_loss_price": "0.354",       # 跌破最低买入层0.372再5%
        "note": "2026-07-23推荐：PE分位13.3%+PB分位4.2%极度低估，股息率4.17%，白酒最优质商业模式+消费龙头",
    },
    "515030": {
        "name": "新能源车ETF",
        "base_price": "1.608",
        "cost_price": "1.608",
        "grid_spacing_up_pct": "4.0",
        "grid_spacing_down_pct": "3.0",
        "grid_spacing_pct": "3.5",
        "levels_above": 6,
        "levels_below": 6,
        "shares_per_grid": 1000,          # 每格1000股（单格≈1608元）
        "base_position": 5000,            # 底仓（估）
        "grid_position": 3000,            # 初始网格仓位
        "max_position": 14000,            # 最大持仓
        "stop_loss_price": "1.282",       # 跌破最低买入层1.350再5%
        "note": "2026-07-23推荐：PE分位37%估值合理，年化波动30-38%，新能源车渗透率持续提升，备选标的",
    },
}

# 风险参数（全局）
RISK_TOTAL_LOSS_WARN = Decimal("10")   # 总亏损警告线（%）
RISK_TOTAL_LOSS_EXIT = Decimal("15")   # 总亏损清仓线（%）

# ============================================================
# 以下无需手动修改
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRIGGER_FILE = os.path.join(SCRIPT_DIR, "..", "data", "grid_triggers.json")
VOLUME_FILE = os.path.join(SCRIPT_DIR, "..", "data", "grid_volume_history.json")

# 折溢价阈值
PREMIUM_EXPENSIVE = Decimal("1.0")   # 溢价 > 1%：偏贵，不利买入
PREMIUM_CHEAP = Decimal("-1.0")      # 折价 > 1%：便宜，有利买入
# 成交量突变阈值
VOL_SPIKE_MULTIPLE = Decimal("2.0")  # 当日量 > N 倍 5日均量 → 放量
VOL_SHRINK_MULTIPLE = Decimal("0.5") # 当日量 < N 倍 5日均量 → 缩量

# --- Decimal 工具 ---

def D(v):
    """安全转 Decimal：float 先转 str 避免精度丢失"""
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


def _sp_up(cfg):
    """上涨间距%（卖出方向），优先 grid_spacing_up_pct"""
    return D(cfg.get("grid_spacing_up_pct", cfg["grid_spacing_pct"]))


def _sp_down(cfg):
    """下跌间距%（买入方向），优先 grid_spacing_down_pct"""
    return D(cfg.get("grid_spacing_down_pct", cfg["grid_spacing_pct"]))


def _sp_label(cfg):
    """间距显示标签"""
    up = _sp_up(cfg)
    down = _sp_down(cfg)
    if up == down:
        return f"±{up}%"
    return f"+{up}%/-{down}%"

# --- 数据模型 ---

class GridLevel:
    """单个网格层级"""
    def __init__(self, idx, price, action, shares, cum_grid, cum_total, cum_cost):
        self.idx = idx
        self.price = price
        self.action = action            # "BUY" / "SELL" / "HOLD"
        self.shares = shares
        self.cum_grid = cum_grid        # 累计网格仓位
        self.cum_total = cum_total      # 累计总持仓（含底仓）
        self.cum_cost = cum_cost        # 累计总成本

# --- 网格层级计算 ---

def calc_grid_levels(cfg):
    """根据配置计算所有网格层级，返回 (levels, base_price)"""
    bp = D(cfg["base_price"])
    sp_up = _sp_up(cfg) / Decimal("100")
    sp_down = _sp_down(cfg) / Decimal("100")
    la = cfg["levels_above"]
    lb = cfg["levels_below"]
    spg = cfg["shares_per_grid"]
    bp_shares = cfg["base_position"]
    g_shares = cfg["grid_position"]

    levels = []

    # 卖出层（从上到下：+N → +1），使用上涨间距
    for i in range(la, 0, -1):
        price = bp * (Decimal("1") + sp_up) ** i
        cum_grid = g_shares - spg * (la - i + 1)
        cum_total = bp_shares + max(cum_grid, 0)
        cum_cost = bp * Decimal(str(bp_shares + max(cum_grid, 0)))
        levels.append(GridLevel(i, price, "SELL", spg, cum_grid, cum_total, cum_cost))

    # 基准层
    levels.append(GridLevel(0, bp, "HOLD", 0, g_shares, bp_shares + g_shares,
                            bp * Decimal(str(bp_shares + g_shares))))

    # 买入层（从近到远：-1 → -N），使用下跌间距
    for i in range(1, lb + 1):
        price = bp * (Decimal("1") - sp_down) ** i
        cum_grid = g_shares + spg * i
        cum_total = bp_shares + cum_grid
        buy_cost = bp * Decimal(str(bp_shares + g_shares))
        for j in range(1, i + 1):
            level_price = bp * (Decimal("1") - sp_down) ** j
            buy_cost += level_price * Decimal(str(spg))
        levels.append(GridLevel(-i, price, "BUY", spg, cum_grid, cum_total, buy_cost))

    levels.sort(key=lambda lv: lv.price, reverse=True)
    return levels, bp


# --- 持久化 ---

def load_triggers():
    if not os.path.exists(TRIGGER_FILE):
        return {}
    with open(TRIGGER_FILE, "r") as f:
        return json.load(f)


def save_triggers(data):
    os.makedirs(os.path.dirname(TRIGGER_FILE), exist_ok=True)
    with open(TRIGGER_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_triggers_for(etf_code):
    return load_triggers().get(etf_code, [])


# --- 成交量历史（用于量价分析） ---

def load_volume_history():
    if not os.path.exists(VOLUME_FILE):
        return {}
    with open(VOLUME_FILE, "r") as f:
        return json.load(f)


def save_volume_history(data):
    os.makedirs(os.path.dirname(VOLUME_FILE), exist_ok=True)
    with open(VOLUME_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_volume_today(etf_code, volume_today):
    """记录今日成交量（盘后自动调用）。只保留最近 30 天。"""
    data = load_volume_history()
    if etf_code not in data:
        data[etf_code] = []
    today_str = date.today().isoformat()
    # 如果今天已记录，更新；否则追加
    updated = False
    for entry in data[etf_code]:
        if entry["date"] == today_str:
            entry["volume"] = str(volume_today)
            updated = True
            break
    if not updated:
        data[etf_code].append({"date": today_str, "volume": str(volume_today)})
    # 只保留最近 30 天
    data[etf_code] = data[etf_code][-30:]
    save_volume_history(data)


def detect_volume_signal(etf_code, current_volume):
    """检测成交量突变。返回 (label, ratio, signal_type)
    signal_type: 'spike'(放量), 'shrink'(缩量), 'normal'(正常)
    """
    history = load_volume_history().get(etf_code, [])
    if len(history) < 5:
        return "数据不足(需≥5天)", None, "normal"

    # 取最近 5 天（不含今天）
    recent = [D(e["volume"]) for e in history[-5:]]
    avg_5d = sum(recent, Decimal("0")) / Decimal(str(len(recent)))

    if avg_5d == 0:
        return "无历史量", None, "normal"

    ratio = D(str(current_volume)) / avg_5d
    if ratio >= VOL_SPIKE_MULTIPLE:
        return f"🔥 放量 {ratio:.1f}x", ratio, "spike"
    elif ratio <= VOL_SHRINK_MULTIPLE:
        return f"💤 缩量 {ratio:.1f}x", ratio, "shrink"
    else:
        return f"正常 {ratio:.1f}x", ratio, "normal"


def _estimate_daily_volume(quote):
    """根据日内成交额和换手率估算全日成交量（手）。
    腾讯 API 返回的 turnover_amt_wan 是累计成交额(万元)，
    volume 是累计成交量(手)。直接用 volume 字段。
    """
    vol = quote.get("volume")
    if vol is None or vol == "-":
        return None
    try:
        return D(str(vol))
    except Exception:
        return None


# --- 动态基准价 ---

def get_dynamic_bp(cfg, triggers):
    """根据历史触发记录计算当前动态基准价"""
    bp = D(cfg["base_price"])
    for tr in triggers:
        bp = D(tr.get("base_price_after", tr["price"]))
    return bp


# --- 当前网格持仓 ---

def get_current_positions(cfg, triggers):
    """返回 (grid_shares, total_shares)"""
    gs = cfg["grid_position"]
    for tr in triggers:
        if tr["action"] == "buy":
            gs += tr["shares"]
        elif tr["action"] == "sell":
            gs -= tr["shares"]
    return gs, cfg["base_position"] + gs


# --- 动态成本计算 ---

def calc_dynamic_cost(cfg, triggers):
    """动态持仓成本：初始成本 + 买入加、卖出按平均成本减"""
    bp_shares = cfg["base_position"]
    g_shares = cfg["grid_position"]
    initial_shares = bp_shares + g_shares

    if "cost_price" in cfg:
        total_cost = D(cfg["cost_price"]) * D(str(initial_shares))
    else:
        total_cost = D(cfg["base_price"]) * D(str(initial_shares))

    current_shares = initial_shares
    for tr in triggers:
        price = D(tr["price"])
        shares = tr["shares"]
        if tr["action"] == "buy":
            total_cost += price * D(str(shares))
            current_shares += shares
        elif tr["action"] == "sell":
            if current_shares > 0:
                avg_cost = total_cost / D(str(current_shares))
                total_cost -= avg_cost * D(str(shares))
            current_shares -= shares

    return total_cost


# --- 实时行情（腾讯 API + 东方财富 ETF 数据，零鉴权） ---

PREMIUM_WARN_PCT = Decimal("0.50")
PREMIUM_HIGH_PCT = Decimal("1.00")
SPREAD_WARN_PCT = Decimal("0.30")


def _curl_text(url, timeout=10, encoding=None):
    """用 curl 直连公开行情接口，返回文本。"""
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


def _to_decimal(value, scale=None):
    """安全转 Decimal；缺失或非法值返回 None。"""
    if value is None or value == "" or value == "-":
        return None
    try:
        dec = D(value)
    except Exception:
        return None
    if scale:
        dec = dec / D(scale)
    return dec


def _qq_code(code):
    """股票代码转腾讯行情格式"""
    code = code.strip()
    if code.startswith(("6", "9", "5")):
        return f"sh{code}"
    elif code.startswith(("0", "3", "2", "1")):
        return f"sz{code}"
    return f"sh{code}"


def _em_secid(code):
    """股票代码转东方财富 secid。"""
    code = code.strip()
    if code.startswith(("6", "9", "5")):
        return f"1.{code}"
    if code.startswith(("0", "1", "2", "3")):
        return f"0.{code}"
    return f"1.{code}"


def _field(fields, index, default=""):
    return fields[index] if len(fields) > index else default


def _quote_levels(fields, start):
    levels = []
    for offset in range(0, 10, 2):
        price = _to_decimal(_field(fields, start + offset))
        volume = _field(fields, start + offset + 1)
        levels.append({"price": price, "volume": volume or "-"})
    return levels


def fetch_qq_quote(etf_code):
    """从腾讯行情获取完整快照，字段缺失时返回 None。"""
    try:
        raw = _curl_text(f"https://qt.gtimg.cn/q={_qq_code(etf_code)}", encoding="gbk")
        if not raw:
            return None
        start = raw.find('"')
        end = raw.rfind('"')
        if start < 0 or end <= start:
            return None
        fields = raw[start + 1:end].split("~")
        if len(fields) < 40:
            return None

        price = _to_decimal(_field(fields, 3))
        bid_levels = _quote_levels(fields, 9)
        ask_levels = _quote_levels(fields, 19)
        bid1 = bid_levels[0]["price"] if bid_levels else None
        ask1 = ask_levels[0]["price"] if ask_levels else None
        spread = ask1 - bid1 if bid1 is not None and ask1 is not None else None
        spread_pct = spread / price * 100 if spread is not None and price and price > 0 else None

        return {
            "source": "腾讯",
            "price_source": "腾讯",
            "name": _field(fields, 1),
            "code": _field(fields, 2) or etf_code,
            "price": price,
            "prev_close": _to_decimal(_field(fields, 4)),
            "open": _to_decimal(_field(fields, 5)),
            "volume": _field(fields, 6) or "-",
            "buy_vol": _field(fields, 7) or "-",
            "sell_vol": _field(fields, 8) or "-",
            "bid_levels": bid_levels,
            "ask_levels": ask_levels,
            "quote_time": _field(fields, 30),
            "change_amt": _to_decimal(_field(fields, 31)),
            "change_pct": _to_decimal(_field(fields, 32)),
            "high": _to_decimal(_field(fields, 33)),
            "low": _to_decimal(_field(fields, 34)),
            "turnover_amt_wan": _to_decimal(_field(fields, 37)),
            "turnover_rate": _to_decimal(_field(fields, 38)),
            "spread": spread,
            "spread_pct": spread_pct,
        }
    except Exception:
        return None


def fetch_price(etf_code):
    """从腾讯行情获取实时价格，返回旧命令需要的最小字段或 None。"""
    quote = fetch_qq_quote(etf_code)
    if not quote or quote.get("price") is None:
        return None
    return {
        "price": quote["price"],
        "name": quote.get("name"),
        "change_pct": str(quote.get("change_pct")) if quote.get("change_pct") is not None else "",
        "prev_close": quote.get("prev_close"),
    }


def fetch_em_etf_quote(etf_code):
    """从东方财富 ETF 列表接口尝试获取 IOPV 和折溢价率。"""
    try:
        base_params = {
            "pz": "100",
            "po": "0",
            "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2",
            "invt": "2",
            "fid": "f12",
            "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827",
            "fields": "f12,f14,f2,f3,f4,f15,f16,f17,f402,f441,f13",
        }
        for page in range(1, 21):
            params = {**base_params, "pn": str(page)}
            url = "https://push2delay.eastmoney.com/api/qt/clist/get?" + urlencode(params)
            raw = _curl_text(url, timeout=15, encoding="utf-8")
            if not raw:
                continue
            data = json.loads(raw).get("data") or {}
            diff = data.get("diff") or []
            if not diff:
                break
            for item in diff:
                if str(item.get("f12")) == str(etf_code):
                    return {
                        "source": "东方财富",
                        "price_source": "东方财富",
                        "name": item.get("f14"),
                        "market": item.get("f13"),
                        "price": _to_decimal(item.get("f2")),
                        "change_pct": _to_decimal(item.get("f3")),
                        "change_amt": _to_decimal(item.get("f4")),
                        "high": _to_decimal(item.get("f15")),
                        "low": _to_decimal(item.get("f16")),
                        "open": _to_decimal(item.get("f17")),
                        "iopv": _to_decimal(item.get("f441")),
                        "discount_pct": _to_decimal(item.get("f402")),
                    }
    except Exception:
        return None
    return None


def fetch_etf_realtime(etf_code):
    """合并腾讯主行情和东方财富 ETF 补充字段。"""
    quote = fetch_qq_quote(etf_code) or {}
    em_quote = fetch_em_etf_quote(etf_code) or {}
    if not quote and not em_quote:
        return None

    quote.setdefault("code", etf_code)
    quote.setdefault("name", em_quote.get("name") or CONFIGS.get(etf_code, {}).get("name", etf_code))
    for key in ("price", "change_pct", "change_amt", "high", "low", "open"):
        if quote.get(key) is None and em_quote.get(key) is not None:
            quote[key] = em_quote[key]
    if quote.get("price_source") is None and quote.get("price") is not None:
        quote["price_source"] = em_quote.get("price_source")
    quote["iopv"] = em_quote.get("iopv")
    quote["discount_pct"] = em_quote.get("discount_pct")
    quote["premium_pct"] = None
    quote["premium_source"] = None

    if quote.get("price") and quote.get("iopv"):
        quote["premium_pct"] = (quote["price"] - quote["iopv"]) / quote["iopv"] * 100
        quote["premium_source"] = f"估算：价格={quote.get('price_source') or '-'} / IOPV=东方财富"

    sources = []
    if quote.get("source"):
        sources.append(quote["source"])
    if em_quote.get("source"):
        sources.append(em_quote["source"])
    quote["sources"] = " + ".join(sources) if sources else "-"
    return quote


def get_price_or_input(etf_code, price_str=None):
    """获取价格：优先用命令行参数，其次从行情 API 拉，最后交互输入"""
    if price_str:
        return D(price_str), "manual"

    # 尝试实时行情
    quote = fetch_price(etf_code)
    if quote and quote["price"] > 0:
        tag = f"实时" if quote["change_pct"] else "api"
        return quote["price"], tag

    # 兜底
    try:
        return D(input("当前价格: ").strip()), "manual"
    except (EOFError, KeyboardInterrupt):
        print("\n已取消")
        sys.exit(0)

BOX_W = 62


def _header(title):
    print(f"╔{'═' * BOX_W}╗")
    print(f"║  {title}" + " " * (BOX_W - len(title) - 3) + "║")


def _sep():
    print(f"╠{'═' * BOX_W}╣")


def _thin():
    print(f"╟{'─' * BOX_W}╢")


def _footer():
    print(f"╚{'═' * BOX_W}╝")


def _row(left, right=""):
    text = f"║  {left}"
    if right:
        text += " " * max(1, BOX_W - len(left) - len(right) - 4) + right
    text += " " * (BOX_W - len(text) + 2) + "║"
    print(text)


def _empty():
    print(f"║{' ' * BOX_W}║")


def _fmt_decimal(value, digits=3, prefix=""):
    if value is None:
        return "-"
    q = Decimal("1").scaleb(-digits)
    return f"{prefix}{value.quantize(q, rounding=ROUND_HALF_UP)}"


def _fmt_pct_value(value):
    if value is None:
        return "-"
    return f"{value:+.2f}%"


def _fmt_amount_wan(value):
    if value is None:
        return "-"
    if abs(value) >= Decimal("10000"):
        return f"{value / Decimal('10000'):.2f}亿"
    return f"{value:.2f}万"


def _fmt_quote_time(raw):
    if not raw or len(raw) < 14:
        return raw or "-"
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]} {raw[8:10]}:{raw[10:12]}:{raw[12:14]}"


def _quote_warning(quote):
    warnings = []
    premium = quote.get("premium_pct")
    spread = quote.get("spread_pct")
    if premium is not None:
        if premium > PREMIUM_HIGH_PCT:
            warnings.append("估算溢价率较高，需以券商APP复核")
        elif premium > PREMIUM_WARN_PCT:
            warnings.append("估算溢价率偏高，买入前复核IOPV")
    if spread is not None and spread > SPREAD_WARN_PCT:
        warnings.append("盘口价差偏大，避免市价单/对手三档")
    if quote.get("iopv") is None:
        warnings.append("IOPV不可用，溢价率需在券商APP复核")
    return warnings


# --- 命令实现 ---

def _print_quote_block(code, quote):
    if not quote:
        print(f"\n  {code}: 获取公开行情失败，请在东方财富APP核对。\n")
        return

    name = quote.get("name") or CONFIGS.get(code, {}).get("name", code)
    bid_levels = quote.get("bid_levels") or []
    ask_levels = quote.get("ask_levels") or []
    warnings = _quote_warning(quote)

    print()
    _header(f"{name} ({code}) 下单前行情快照")
    _row(
        f"当前价: {_fmt_decimal(quote.get('price'), 3, '¥')}  │  "
        f"涨跌幅: {_fmt_pct_value(quote.get('change_pct'))}  │  "
        f"涨跌额: {_fmt_decimal(quote.get('change_amt'), 3)}"
    )
    _row(
        f"IOPV: {_fmt_decimal(quote.get('iopv'), 4, '¥')}  │  "
        f"估算溢价率: {_fmt_pct_value(quote.get('premium_pct'))}  │  "
        f"来源: {quote.get('premium_source') or '-'}"
    )
    _row(
        f"昨收: {_fmt_decimal(quote.get('prev_close'), 3, '¥')}  │  "
        f"今开: {_fmt_decimal(quote.get('open'), 3, '¥')}  │  "
        f"最高: {_fmt_decimal(quote.get('high'), 3, '¥')}  │  "
        f"最低: {_fmt_decimal(quote.get('low'), 3, '¥')}"
    )
    _row(
        f"成交量: {quote.get('volume') or '-'}（腾讯口径）  │  "
        f"成交额: {_fmt_amount_wan(quote.get('turnover_amt_wan'))}"
    )
    _row(f"行情时间: {_fmt_quote_time(quote.get('quote_time'))}  │  数据源: {quote.get('sources') or '-'}")
    _thin()

    for i in range(5):
        bid = bid_levels[i] if i < len(bid_levels) else {"price": None, "volume": "-"}
        ask = ask_levels[i] if i < len(ask_levels) else {"price": None, "volume": "-"}
        _row(
            f"买{i + 1}: {_fmt_decimal(bid.get('price'), 3, '¥')} x {bid.get('volume') or '-'}手",
            f"卖{i + 1}: {_fmt_decimal(ask.get('price'), 3, '¥')} x {ask.get('volume') or '-'}手"
        )

    _thin()
    _row(
        f"买卖价差: {_fmt_decimal(quote.get('spread'), 3, '¥')}  │  "
        f"价差率: {_fmt_pct_value(quote.get('spread_pct'))}"
    )
    if warnings:
        for warning in warnings[:3]:
            _row(f"⚠️ {warning}")
    else:
        _row("✅ 未发现明显溢价/盘口价差风险")
    _row("提示: 公开行情可能延迟，最终以券商交易端为准")
    _footer()
    print()


def cmd_quote(etf_code=None):
    codes = [etf_code] if etf_code else list(CONFIGS.keys())
    for code in codes:
        _print_quote_block(code, fetch_etf_realtime(code))


def cmd_table(etf_code=None):
    codes = [etf_code] if etf_code else list(CONFIGS.keys())
    for code in codes:
        cfg = CONFIGS.get(code)
        if not cfg:
            print(f"未找到配置: {code}")
            continue

        sp_up = _sp_up(cfg)
        sp_down = _sp_down(cfg)
        if sp_up == 0 and sp_down == 0:
            print(f"\n  {cfg['name']} ({code}) — 仅持有，无网格\n")
            continue

        levels, bp = calc_grid_levels(cfg)
        spg = cfg["shares_per_grid"]
        bp_shares = cfg["base_position"]
        g_shares = cfg["grid_position"]

        print()
        _header(f"{cfg['name']} ({code}) 网格价格表")
        _row(f"基准价 ¥{bp}  |  间距 {_sp_label(cfg)}  |  每格 {spg}股")
        _row(f"底仓 {bp_shares}股（锁定）  |  网格仓 {g_shares}股")
        _sep()
        print(f"║  {'层':>4}  │ {'触发价':>8} │ {'操作':^4} │ {'股数':>5} │ {'网格仓':>6} │ {'总持仓':>6} │ {'累计成本':>10} ║")
        _thin()

        for lv in levels:
            a = {"BUY": "买入", "SELL": "卖出", "HOLD": "基准"}[lv.action]
            mark = " ▲" if lv.action == "SELL" else (" ▼" if lv.action == "BUY" else " ◆")
            print(f"║  {lv.idx:>+4d} │ {lv.price:>8.4f} │ {a:^4} │ {lv.shares:>5d} │ {lv.cum_grid:>6d} │ {lv.cum_total:>6d} │ ¥{lv.cum_cost:>9.2f} ║{mark}")

        _footer()

        lo = levels[-1].price
        hi = levels[0].price
        max_cost = max(lv.cum_cost for lv in levels if lv.action == "BUY")
        min_grid = min(lv.cum_grid for lv in levels)
        max_grid = max(lv.cum_grid for lv in levels)
        round_trip = spg * bp * (sp_up + sp_down) / Decimal("100")

        print(f"  区间: ¥{lo:.4f} ~ ¥{hi:.4f}")
        print(f"  网格仓: {min_grid} ~ {max_grid} 股  │  总持仓: {bp_shares + min_grid} ~ {bp_shares + max_grid} 股")
        print(f"  最大资金: ¥{max_cost:.2f}  │  单格往返利润: ¥{round_trip:.2f}")
        print()


def cmd_status(etf_code=None, price_str=None):
    if not etf_code:
        etf_code = list(CONFIGS.keys())[0]

    cfg = CONFIGS.get(etf_code)
    if not cfg:
        print(f"未找到配置: {etf_code}")
        return

    # 获取当前价格
    if price_str:
        cp = D(price_str)
        price_src = "manual"
    else:
        cp, price_src = get_price_or_input(etf_code)
    sp_up = _sp_up(cfg)
    sp_down = _sp_down(cfg)
    bp_shares = cfg["base_position"]

    # 仅持有模式
    if sp_up == 0 and sp_down == 0:
        bp = D(cfg.get("cost_price", cfg["base_price"]))
        shares = bp_shares
        mkt_val = cp * Decimal(str(shares))
        cost_val = bp * Decimal(str(shares))
        pnl = mkt_val - cost_val
        pnl_pct = pnl / cost_val * 100 if cost_val > 0 else Decimal("0")

        print()
        _header(f"{cfg['name']} ({etf_code}) — 仅持有")
        _row(f"持仓: {shares} 股  │  成本: ¥{bp}  │  现价: ¥{cp}")
        _row(f"市值: ¥{mkt_val:.2f}  │  浮盈: {pnl_pct:+.2f}%  (¥{pnl:+.2f})")
        _footer()
        print()
        return

    triggers = get_triggers_for(etf_code)
    current_bp = get_dynamic_bp(cfg, triggers)
    grid_shares, total_shares = get_current_positions(cfg, triggers)

    # 基于动态基准价 + 当前网格仓重算层级
    dyn_cfg = {**cfg, "base_price": str(current_bp), "grid_position": grid_shares}
    dyn_levels, _ = calc_grid_levels(dyn_cfg)

    # 找下一买卖触发价（取最近的一层）
    sell_candidates = [lv for lv in dyn_levels if lv.action == "SELL" and lv.price > cp]
    next_sell = min(sell_candidates, key=lambda lv: lv.price) if sell_candidates else None
    buy_candidates = [lv for lv in dyn_levels if lv.action == "BUY" and lv.price < cp]
    next_buy = max(buy_candidates, key=lambda lv: lv.price) if buy_candidates else None

    # 动态成本
    total_cost = calc_dynamic_cost(cfg, triggers)
    avg_cost = total_cost / D(str(total_shares)) if total_shares > 0 else Decimal("0")

    mkt_val = cp * D(str(total_shares))
    unreal_pct = (cp - avg_cost) / avg_cost * 100 if avg_cost > 0 else Decimal("0")

    buy_count = sum(1 for t in triggers if t["action"] == "buy")
    sell_count = sum(1 for t in triggers if t["action"] == "sell")

    print()
    _header(f"{cfg['name']} ({etf_code}) 网格状态")
    _row(f"📍 当前价: ¥{cp}  │  动态基准价: ¥{current_bp}")

    if next_sell:
        d = (next_sell.price - cp) / cp * 100
        _row(f"📈 下一卖出: ¥{next_sell.price:.4f}  (+{d:.1f}%)")
    else:
        _row(f"📈 下一卖出: 无（已超网格上限）")

    if next_buy:
        d = (cp - next_buy.price) / cp * 100
        _row(f"📉 下一买入: ¥{next_buy.price:.4f}  (-{d:.1f}%)")
    else:
        _row(f"📉 下一买入: 无（已超网格下限）")

    _sep()
    _row(f"📊 持仓概览")
    _row(f"底仓（锁定）: {bp_shares} 股")
    _row(f"网格仓位: {grid_shares} 股  │  网格上限: {cfg['max_position'] - bp_shares} 股")
    _row(f"总持仓: {total_shares} 股  │  最大: {cfg['max_position']} 股")
    _row(f"持仓市值: ¥{mkt_val:.2f}  │  浮盈: {unreal_pct:+.2f}%")
    _row(f"已触发: 买入 {buy_count} 次  │  卖出 {sell_count} 次")
    _footer()
    print()


def cmd_trigger(etf_code=None, force_direction=None):
    """录入网格成交。force_direction: None=自动判断, 'buy'=强制买入, 'sell'=强制卖出"""
    if not etf_code:
        etf_code = list(CONFIGS.keys())[0]

    cfg = CONFIGS.get(etf_code)
    if not cfg:
        print(f"未找到配置: {etf_code}")
        return

    sp_up = _sp_up(cfg)
    sp_down = _sp_down(cfg)
    if sp_up == 0 and sp_down == 0:
        print(f"{etf_code} 未开启网格交易，无需录入触发")
        return

    triggers = get_triggers_for(etf_code)
    current_bp = get_dynamic_bp(cfg, triggers)
    spg = cfg["shares_per_grid"]

    print()
    print(f"  {cfg['name']} ({etf_code})")
    print(f"  动态基准价: ¥{current_bp}")
    print(f"  买入触发价: ¥{current_bp * (Decimal('1') - sp_down / 100):.4f}")
    print(f"  卖出触发价: ¥{current_bp * (Decimal('1') + sp_up / 100):.4f}")
    if force_direction:
        print(f"  方向: {'卖出' if force_direction == 'sell' else '买入'} (手动指定)")
    print()

    try:
        price_str = input("  成交价: ").strip()
        price = D(price_str)
        date_str = input("  日期 (回车=今天): ").strip()
        if not date_str:
            date_str = date.today().isoformat()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消")
        return

    sell_trigger = current_bp * (Decimal("1") + sp_up / 100)
    buy_trigger = current_bp * (Decimal("1") - sp_down / 100)

    # 方向判定：手动指定 > 自动判断
    if force_direction:
        action = force_direction
        direction = "卖出" if force_direction == "sell" else "买入"
        if force_direction == "sell":
            step_mult = Decimal("1") + sp_up / 100
            step_price = sell_trigger
        else:
            step_mult = Decimal("1") - sp_down / 100
            step_price = buy_trigger
        # 计算跨越层数
        triggered_count = 1
        for _ in range(50):  # 安全上限
            next_step = step_price * step_mult
            if force_direction == "sell":
                if price >= next_step:
                    triggered_count += 1
                    step_price = next_step
                else:
                    break
            else:
                if price <= next_step:
                    triggered_count += 1
                    step_price = next_step
                else:
                    break
        # 安全上限：跨层超过20层提示异常
        if triggered_count > 20:
            print(f"  ⚠️ 计算跨越 {triggered_count} 层，可能异常，请检查输入价格")
            return
    elif price >= sell_trigger:
        action = "sell"
        direction = "卖出"
        step_price = sell_trigger
        step_mult = Decimal("1") + sp_up / 100
        triggered_count = 1
        for _ in range(50):
            next_step = step_price * step_mult
            if price >= next_step:
                triggered_count += 1
                step_price = next_step
            else:
                break
        if triggered_count > 20:
            print(f"  ⚠️ 计算跨越 {triggered_count} 层，可能异常，请检查输入价格")
            return
    elif price <= buy_trigger:
        action = "buy"
        direction = "买入"
        step_price = buy_trigger
        step_mult = Decimal("1") - sp_down / 100
        triggered_count = 1
        for _ in range(50):
            next_step = step_price * step_mult
            if price <= next_step:
                triggered_count += 1
                step_price = next_step
            else:
                break
        if triggered_count > 20:
            print(f"  ⚠️ 计算跨越 {triggered_count} 层，可能异常，请检查输入价格")
            return
    else:
        # 价格在网格区间内 — 补录历史成交，手动指定方向
        print(f"  ⚠️ 价格 ¥{price} 在网格区间内 (¥{buy_trigger:.4f} ~ ¥{sell_trigger:.4f})")
        print(f"     买入触发价: ¥{buy_trigger:.4f}  卖出触发价: ¥{sell_trigger:.4f}")
        try:
            d = input("  请手动指定方向 (b=买入 / s=卖出): ").strip().lower()
            if d in ("s", "sell"):
                action = "sell"
                direction = "卖出"
            elif d in ("b", "buy"):
                action = "buy"
                direction = "买入"
            else:
                print("已取消")
                return
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return
        triggered_count = 1  # 区间内不跨越，固定1层

    print()
    if triggered_count > 1:
        print(f"  ⚡ 价格跳空跨越 {triggered_count} 层，将执行倍数委托")
    total_spg = spg * triggered_count
    print(f"  → {direction} {total_spg} 股 @ ¥{price}")
    confirm = input("  确认录入? (y/n): ").strip().lower()
    if confirm != "y":
        print("已取消")
        return

    # 保存
    all_data = load_triggers()
    if etf_code not in all_data:
        all_data[etf_code] = []

    new_bp = current_bp
    for i in range(triggered_count):
        record = {
            "date": date_str,
            "action": action,
            "price": str(price),
            "shares": spg,
            "base_price_before": str(new_bp),
            "base_price_after": str(price),
        }
        all_data[etf_code].append(record)
        new_bp = price

    save_triggers(all_data)
    print(f"  ✅ 已记录 {triggered_count} 笔成交，新基准价: ¥{price}")
    print()


def cmd_pnl(etf_code=None, price_str=None):
    if not etf_code:
        etf_code = list(CONFIGS.keys())[0]

    cfg = CONFIGS.get(etf_code)
    if not cfg:
        print(f"未找到配置: {etf_code}")
        return

    if price_str:
        cp = D(price_str)
    else:
        cp, _ = get_price_or_input(etf_code)

    triggers = get_triggers_for(etf_code)
    bp = D(cfg["base_price"])
    bp_shares = cfg["base_position"]
    g_shares = cfg["grid_position"]

    # 初始成本价（FIFO 种子用）
    seed_price = D(cfg.get("cost_price", cfg["base_price"]))

    # FIFO 匹配
    # 初始网格仓位视为以 cost_price（或 base_price）买入
    buy_queue = []
    if g_shares > 0:
        buy_queue.append([seed_price, g_shares])
    realized = Decimal("0")
    round_trips = 0

    for tr in triggers:
        if tr["action"] == "buy":
            buy_queue.append([D(tr["price"]), tr["shares"]])
        elif tr["action"] == "sell":
            sell_price = D(tr["price"])
            remaining = tr["shares"]
            while remaining > 0 and buy_queue:
                buy_price, buy_qty = buy_queue[0]
                matched = min(buy_qty, remaining)
                realized += (sell_price - buy_price) * D(str(matched))
                remaining -= matched
                if buy_qty <= matched:
                    buy_queue.pop(0)
                    round_trips += 1
                else:
                    buy_queue[0] = [buy_price, buy_qty - matched]

    # 当前持仓
    grid_shares, total_shares = get_current_positions(cfg, triggers)

    # 动态成本
    total_cost = calc_dynamic_cost(cfg, triggers)
    avg_cost = total_cost / D(str(total_shares)) if total_shares > 0 else Decimal("0")

    mkt_val = cp * D(str(total_shares))
    unrealized = mkt_val - total_cost
    total_pnl = realized + unrealized
    pnl_pct = total_pnl / total_cost * 100 if total_cost > 0 else Decimal("0")

    print()
    _header(f"{cfg['name']} ({etf_code}) 盈亏统计")
    _row(f"当前价: ¥{cp}  │  总持仓: {total_shares} 股  │  动态成本: ¥{avg_cost:.4f}")
    _thin()
    _row(f"📈 已实现盈亏: ¥{realized:>+.2f}")
    _row(f"   完成往返: {round_trips} 对")
    _row(f"📉 未实现盈亏: ¥{unrealized:>+.2f}")
    _row(f"💰 总盈亏: ¥{total_pnl:>+.2f}  ({pnl_pct:>+.2f}%)")
    _thin()
    _row(f"📋 成交记录: {len(triggers)} 笔")
    if triggers:
        for tr in triggers[-5:]:
            a = "买入" if tr["action"] == "buy" else "卖出"
            _row(f"  {tr['date']} {a} {tr['shares']}股 @ ¥{tr['price']}")
    _footer()
    print()


def cmd_risk(etf_code=None, price_str=None):
    if not etf_code:
        etf_code = list(CONFIGS.keys())[0]

    cfg = CONFIGS.get(etf_code)
    if not cfg:
        print(f"未找到配置: {etf_code}")
        return

    if price_str:
        cp = D(price_str)
    else:
        cp, _ = get_price_or_input(etf_code)

    sp_up = _sp_up(cfg)
    sp_down = _sp_down(cfg)
    bp = D(cfg["base_price"])
    bp_shares = cfg["base_position"]
    g_shares = cfg["grid_position"]
    max_pos = cfg["max_position"]
    stop_loss = D(cfg["stop_loss_price"])

    triggers = get_triggers_for(etf_code)
    grid_shares, total_shares = get_current_positions(cfg, triggers)

    alerts = []
    lowest_buy = None

    if sp_up > 0 or sp_down > 0:
        levels, _ = calc_grid_levels(cfg)
        buy_levels = [lv for lv in levels if lv.action == "BUY"]
        if buy_levels:
            lowest_buy = min(lv.price for lv in buy_levels)

        # 网格耗尽
        if lowest_buy and cp < lowest_buy:
            alerts.append(("WARN", f"价格 ¥{cp} 已跌破最低买入层 ¥{lowest_buy}，网格耗尽"))

        # 仓位上限
        pos_pct = D(str(total_shares)) / D(str(max_pos)) * 100
        if pos_pct > 90:
            alerts.append(("WARN", f"持仓 {total_shares}/{max_pos} ({pos_pct:.0f}%)，接近上限"))

    # 止损
    if cp <= stop_loss:
        alerts.append(("CRIT", f"价格 ¥{cp} 已触及止损价 ¥{stop_loss}"))
    elif stop_loss > 0:
        dist = (cp - stop_loss) / cp * 100
        if dist < 10:
            alerts.append(("WARN", f"距止损价仅 {dist:.1f}%（止损 ¥{stop_loss}）"))

    # 浮动盈亏（券商成本，对齐东方财富 App 显示）
    if "cost_price" in cfg:
        avg_cost = D(cfg["cost_price"])
    else:
        total_cost_dyn = calc_dynamic_cost(cfg, triggers)
        avg_cost = total_cost_dyn / D(str(total_shares)) if total_shares > 0 else Decimal("0")
    mkt_val = cp * D(str(total_shares))
    float_pnl_pct = (cp - avg_cost) / avg_cost * 100 if avg_cost > 0 else Decimal("0")

    if float_pnl_pct <= -RISK_TOTAL_LOSS_EXIT:
        alerts.append(("CRIT", f"总亏损 {float_pnl_pct:+.1f}%，已达清仓线 {RISK_TOTAL_LOSS_EXIT}%"))
    elif float_pnl_pct <= -RISK_TOTAL_LOSS_WARN:
        alerts.append(("WARN", f"总亏损 {float_pnl_pct:+.1f}%，接近警戒线 {RISK_TOTAL_LOSS_WARN}%"))

    # 输出
    print()
    _header(f"{cfg['name']} ({etf_code}) 风险检查")
    _row(f"当前价: ¥{cp}  │  持仓: {total_shares}/{max_pos} 股")
    _row(f"持仓市值: ¥{mkt_val:.2f}  │  券商成本: ¥{avg_cost:.4f}")
    _row(f"浮动盈亏: {float_pnl_pct:+.2f}%  │  警戒 {RISK_TOTAL_LOSS_WARN}% / 清仓 {RISK_TOTAL_LOSS_EXIT}%")
    if lowest_buy:
        _row(f"最低买入层: ¥{lowest_buy:.4f}")
    _row(f"止损价: ¥{stop_loss}")
    _sep()

    if not alerts:
        _row("✅ 无风险信号")
    else:
        for level, msg in alerts:
            icon = {"CRIT": "🔴", "WARN": "🟡"}.get(level, "⚪")
            _row(f"{icon} [{level}] {msg}")

    crit_count = sum(1 for l, _ in alerts if l == "CRIT")
    warn_count = sum(1 for l, _ in alerts if l == "WARN")

    if crit_count > 0:
        score = "🔴 高风险 — 需要立即处理"
    elif warn_count >= 2:
        score = "🟡 中风险 — 密切关注"
    elif warn_count == 1:
        score = "🟢 低风险 — 持续监控"
    else:
        score = "✅ 正常"

    _sep()
    _row(f"综合评分: {score}")
    _footer()
    print()


def cmd_signal(etf_code=None):
    """量价信号：折溢价率 + 成交量突变 + 触发距离 → 操作建议"""
    codes = [etf_code] if etf_code else list(CONFIGS.keys())

    for code in codes:
        cfg = CONFIGS.get(code)
        if not cfg:
            print(f"未找到配置: {code}")
            continue

        sp_up = _sp_up(cfg)
        sp_down = _sp_down(cfg)
        if sp_up == 0 and sp_down == 0:
            print(f"\n  {cfg['name']} ({code}) — 仅持有，无网格\n")
            continue

        quote = fetch_etf_realtime(code)
        cp = quote.get("price") if quote else None
        premium = quote.get("premium_pct") if quote else None
        volume_today = _estimate_daily_volume(quote) if quote else None

        triggers = get_triggers_for(code)
        current_bp = get_dynamic_bp(cfg, triggers)
        grid_shares, total_shares = get_current_positions(cfg, triggers)

        dyn_cfg = {**cfg, "base_price": str(current_bp), "grid_position": grid_shares}
        dyn_levels, _ = calc_grid_levels(dyn_cfg)

        sell_candidates = [lv for lv in dyn_levels if lv.action == "SELL" and cp and lv.price > cp]
        next_sell = min(sell_candidates, key=lambda lv: lv.price) if sell_candidates else None
        buy_candidates = [lv for lv in dyn_levels if lv.action == "BUY" and cp and lv.price < cp]
        next_buy = max(buy_candidates, key=lambda lv: lv.price) if buy_candidates else None

        # 量信号
        vol_label, vol_ratio, vol_type = detect_volume_signal(code, volume_today) if volume_today else ("无数据", None, "normal")

        # 综合评分
        buy_score = 0
        sell_score = 0
        signals = []

        # 1. 折溢价
        if premium is not None:
            if premium > PREMIUM_EXPENSIVE:
                signals.append(f"🟡 溢价 {premium:+.2f}% → 偏贵，不利买入")
                sell_score += 1
            elif premium < PREMIUM_CHEAP:
                signals.append(f"🟢 折价 {premium:+.2f}% → 便宜，有利买入")
                buy_score += 2
            elif abs(premium) <= D("0.5"):
                signals.append(f"✅ 折溢价 {premium:+.2f}% → 合理")
            else:
                signals.append(f"⚪ 折溢价 {premium:+.2f}% → 正常范围")
        else:
            signals.append("⚪ IOPV 不可用，无法判断折溢价")

        # 2. 成交量信号
        if vol_type == "spike":
            signals.append(f"🔥 放量 {vol_ratio:.1f}x → 关注！可能触发或假突破")
            sell_score += 1
            buy_score += 1  # 放量本身不偏多空，但增加交易价值
        elif vol_type == "shrink":
            signals.append(f"💤 缩量 {vol_ratio:.1f}x → 清淡，条件单触发概率低")
        else:
            signals.append(f"✅ {vol_label} → 正常")

        # 3. 触发距离
        if next_sell:
            sell_dist = (next_sell.price - cp) / cp * 100
            if sell_dist < 1:
                signals.append(f"🔔 距卖出 {sell_dist:.1f}%，即将触发")
                sell_score += 2
            elif sell_dist < 3:
                signals.append(f"⚡ 距卖出 {sell_dist:.1f}%")
                sell_score += 1
        if next_buy:
            buy_dist = (cp - next_buy.price) / cp * 100
            if buy_dist < 1:
                signals.append(f"🔔 距买入 {buy_dist:.1f}%，即将触发")
                buy_score += 2
            elif buy_dist < 3:
                signals.append(f"⚡ 距买入 {buy_dist:.1f}%")
                buy_score += 1

        # 输出
        print()
        _header(f"{cfg['name']} ({code}) 量价信号")
        _row(f"现价: ¥{cp:.4f}  │  基准: ¥{current_bp}  │  网格仓: {grid_shares}股")
        _thin()
        for s in signals:
            _row(s)
        _thin()

        # 综合建议
        if buy_score >= 3:
            suggestion = "🟢 买入条件有利（折价+量价共振），如触发可放心执行"
        elif buy_score >= 2:
            suggestion = "🟢 买入条件偏有利"
        elif sell_score >= 3:
            suggestion = "🔴 卖出条件有利（溢价+接近触发），可关注成交"
        elif sell_score >= 2:
            suggestion = "🟡 卖出条件偏有利"
        else:
            suggestion = "✅ 信号中性，按网格正常执行"

        _row(suggestion)
        _row(f"评分: 买入{buy_score}分 / 卖出{sell_score}分  (≥3=强信号)")
        _footer()
        print()


def cmd_watch(etf_code=None, interval=30):
    """持续监控模式，每隔 N 秒刷新"""
    if not etf_code:
        etf_code = list(CONFIGS.keys())[0]

    cfg = CONFIGS.get(etf_code)
    if not cfg:
        print(f"未找到配置: {etf_code}")
        return

    sp_up = _sp_up(cfg)
    sp_down = _sp_down(cfg)
    if sp_up == 0 and sp_down == 0:
        print(f"{etf_code} 未开启网格交易，无需监控")
        return

    bp_shares = cfg["base_position"]
    alert_threshold_up = sp_up / Decimal("100") * Decimal("0.3")  # 间距的 30% 为预警区
    alert_threshold_down = sp_down / Decimal("100") * Decimal("0.3")
    last_alerts = set()

    print(f"\n  🔍 开始监控 {cfg['name']} ({etf_code})  —  {interval}s 刷新  —  Ctrl+C 退出\n", flush=True)

    try:
        while True:
            quote = fetch_price(etf_code)
            if not quote or quote["price"] <= 0:
                print(f"  [{_now()}] ⚠️ 获取行情失败，60s 后重试...")
                time.sleep(60)
                continue

            cp = quote["price"]
            change = quote.get("change_pct", "-")
            triggers = get_triggers_for(etf_code)
            current_bp = get_dynamic_bp(cfg, triggers)
            grid_shares, total_shares = get_current_positions(cfg, triggers)

            # 下一触发价
            dyn_cfg = {**cfg, "base_price": str(current_bp), "grid_position": grid_shares}
            dyn_levels, _ = calc_grid_levels(dyn_cfg)
            sell_candidates = [lv for lv in dyn_levels if lv.action == "SELL" and lv.price > cp]
            next_sell = min(sell_candidates, key=lambda lv: lv.price) if sell_candidates else None
            buy_candidates = [lv for lv in dyn_levels if lv.action == "BUY" and lv.price < cp]
            next_buy = max(buy_candidates, key=lambda lv: lv.price) if buy_candidates else None

            # 构建状态行
            status_parts = [f"[{_now()}] {etf_code} ¥{cp:.3f}"]
            alerts_this = set()

            if next_sell:
                dist = (next_sell.price - cp) / cp
                if dist < alert_threshold_up:
                    status_parts.append(f"🔥 距卖出 ¥{next_sell.price:.4f} 仅 {dist*100:.1f}%")
                    alerts_this.add("sell_close")
                elif dist < alert_threshold_up * 2:
                    status_parts.append(f"⚡ 距卖出 ¥{next_sell.price:.4f} 差 {dist*100:.1f}%")
                else:
                    status_parts.append(f"卖 ¥{next_sell.price:.4f} ({dist*100:.1f}%)")
            else:
                status_parts.append("卖: 已超上限")

            if next_buy:
                dist = (cp - next_buy.price) / cp
                if dist < alert_threshold_down:
                    status_parts.append(f"🔥 距买入 ¥{next_buy.price:.4f} 仅 {dist*100:.1f}%")
                    alerts_this.add("buy_close")
                elif dist < alert_threshold_down * 2:
                    status_parts.append(f"⚡ 距买入 ¥{next_buy.price:.4f} 差 {dist*100:.1f}%")
                else:
                    status_parts.append(f"买 ¥{next_buy.price:.4f} ({dist*100:.1f}%)")
            else:
                status_parts.append("买: 已超下限")

            # 已触发价穿越
            all_data = load_triggers()
            etf_triggers = all_data.get(etf_code, [])
            if next_sell and cp >= next_sell.price:
                status_parts.append("🔔 已触及卖出触发价！检查条件单")
                alerts_this.add("triggered")
            if next_buy and cp <= next_buy.price:
                status_parts.append("🔔 已触及买入触发价！检查条件单")
                alerts_this.add("triggered")

            # 风险简报
            risk_alerts = []
            lowest_buy = min((lv.price for lv in dyn_levels if lv.action == "BUY"), default=None)
            if lowest_buy and cp < lowest_buy:
                risk_alerts.append("网格耗尽")
            if D(str(total_shares)) / D(str(cfg["max_position"])) > Decimal("0.9"):
                risk_alerts.append("接近仓位上限")
            if risk_alerts:
                status_parts.append("⚠️ " + ", ".join(risk_alerts))

            # 变化标记
            status_parts.append(f"│ {change}%")

            # 折溢价 + 量信号（每 10 次才拉一次完整数据，节省请求）
            if not hasattr(cmd_watch, "_counter"):
                cmd_watch._counter = 0
            cmd_watch._counter += 1

            if cmd_watch._counter % 10 == 1:
                full_quote = fetch_etf_realtime(etf_code)
                if full_quote:
                    premium = full_quote.get("premium_pct")
                    if premium is not None:
                        pct_str = f"{premium:+.2f}%"
                        if premium > PREMIUM_EXPENSIVE:
                            status_parts.append(f"⚠️ 溢价 {pct_str}")
                        elif premium < PREMIUM_CHEAP:
                            status_parts.append(f"✅ 折价 {pct_str}")
                        else:
                            status_parts.append(f"│ {pct_str}")

                    vol_now = _estimate_daily_volume(full_quote)
                    if vol_now:
                        vol_label, vol_ratio, vol_type = detect_volume_signal(etf_code, vol_now)
                        if vol_type == "spike":
                            status_parts.append(f"🔥 放量 {vol_ratio:.1f}x")
                        elif vol_type == "shrink":
                            status_parts.append(f"💤 缩量 {vol_ratio:.1f}x")

            # 仅在状态变化时打印（首次或告警变化时额外换行）
            if alerts_this != last_alerts:
                print(flush=True)
                last_alerts = alerts_this

            print("  " + "  ".join(status_parts), flush=True)

            if "triggered" in alerts_this:
                print(f"  {'─' * 60}", flush=True)
                print(f"  ⚠️  以上价格已触发！请在东方财富确认条件单是否已自动成交", flush=True)
                print(f"      若已成交，运行: python3 grid_trading.py trigger {etf_code}", flush=True)
                print(f"  {'─' * 60}", flush=True)

            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n\n  已退出监控。共监控 {cfg['name']} ({etf_code})")
        print()


def _now():
    return datetime.now().strftime("%H:%M:%S")


# ============================================================
# 高级分析命令（集成 GitHub 开源项目最佳实践）
# ============================================================

def _fetch_kline_for_ma(etf_code, count=120):
    """获取 K 线数据用于 MA 和 ATR 计算。优先用 ashare_data.py，失败则用腾讯。"""
    # 尝试调用同目录的 ashare_data.py
    script = os.path.join(SCRIPT_DIR, "..", "scripts", "fetch_data.py")
    if os.path.exists(script):
        try:
            result = subprocess.run(
                ["python3", script, "kline", etf_code, "--count", str(count)],
                capture_output=True, text=True, timeout=30)
            data = json.loads(result.stdout)
            closes = []
            for item in data.get("kline_data", data.get("data", [])):
                c = item.get("close") or item.get("Close")
                if c:
                    closes.append(float(c))
            if len(closes) >= 20:
                return closes
        except Exception:
            pass
    # 备用：腾讯日K
    try:
        code = etf_code.strip()
        prefix = "sh" if code.startswith(("6", "9", "5")) else "sz"
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,{count},qfq"
        raw = _curl_text(url, timeout=10)
        data = json.loads(raw)
        days = data.get("data", {}).get(f"{prefix}{code}", {}).get("qfqday", []) or \
               data.get("data", {}).get(f"{prefix}{code}", {}).get("day", [])
        closes = [float(d[2]) for d in days if len(d) > 2]
        if len(closes) >= 20:
            return closes
    except Exception:
        pass
    return []


def _calc_atr(closes, highs=None, lows=None, period=14):
    """简化的 ATR(14) 计算（仅用收盘价估算真实波幅）。"""
    if len(closes) < period + 1:
        return None
    tr_list = []
    for i in range(1, len(closes)):
        tr = abs(closes[i] - closes[i-1])
        if highs and lows:
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
    if len(tr_list) < period:
        return None
    # Wilder's smoothing (simplified as SMA)
    return sum(tr_list[-period:]) / period


def _calc_ma(closes, period):
    """简单移动平均。"""
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _calc_bollinger(closes, period=20, std_mult=2.0):
    """布林带计算。返回 (middle, upper, lower, width_pct)。"""
    if len(closes) < period:
        return None, None, None, None
    ma = _calc_ma(closes, period)
    recent = closes[-period:]
    variance = sum((x - ma) ** 2 for x in recent) / period
    std = variance ** 0.5
    upper = ma + std_mult * std
    lower = ma - std_mult * std
    width_pct = (upper - lower) / ma * 100 if ma > 0 else 0
    return ma, upper, lower, width_pct


def cmd_performance(etf_code=None):
    """高级绩效分析：Sharpe/Sortino/Calmar/MaxDD/胜率/盈亏比。

    对标 ptulluri/BP_Grid_Trading_Bot 的 20+ 指标精简版。
    """
    if not etf_code:
        etf_code = list(CONFIGS.keys())[0]

    cfg = CONFIGS.get(etf_code)
    if not cfg:
        print(f"未找到配置: {etf_code}")
        return

    triggers = get_triggers_for(etf_code)
    if len(triggers) < 4:
        print(f"\n  {cfg['name']} ({etf_code}): 成交记录不足（需 ≥4 笔），无法计算绩效\n")
        return

    # 分离买卖，计算每笔盈亏（FIFO）
    price = D(cfg["base_price"])
    shares_per = cfg["shares_per_grid"]

    # 按日期排序
    sorted_triggers = sorted(triggers, key=lambda t: (t["date"], triggers.index(t)))

    # FIFO 匹配：累积买卖对，计算每对盈亏
    buy_queue = []  # [(price, shares)]
    trades = []     # [(buy_price, sell_price, shares, profit)]

    for tr in sorted_triggers:
        p = D(tr["price"])
        s = tr["shares"]
        if tr["action"] == "buy":
            buy_queue.append((p, s))
        elif tr["action"] == "sell":
            remaining = s
            while remaining > 0 and buy_queue:
                bp, bq = buy_queue[0]
                matched = min(bq, remaining)
                profit = (p - bp) * D(str(matched))
                trades.append((float(bp), float(p), matched, float(profit)))
                remaining -= matched
                if bq <= matched:
                    buy_queue.pop(0)
                else:
                    buy_queue[0] = (bp, bq - matched)

    if not trades:
        print(f"\n  {cfg['name']} ({etf_code}): 无完整往返交易对\n")
        return

    profits = [t[3] for t in trades]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p < 0]

    total_profit = sum(profits)
    win_rate = len(wins) / len(profits) * 100 if profits else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float('inf')

    # 最大回撤（基于累计盈亏）
    cumsum = 0
    peak = 0
    max_dd = 0
    for p in profits:
        cumsum += p
        if cumsum > peak:
            peak = cumsum
        dd = peak - cumsum
        if dd > max_dd:
            max_dd = dd

    # 年化指标
    if trades:
        dates = sorted(set(tr["date"] for tr in sorted_triggers))
        first_date = dates[0]
        last_date = dates[-1]
        try:
            d1 = date.fromisoformat(first_date)
            d2 = date.fromisoformat(last_date)
            days = (d2 - d1).days or 1
        except Exception:
            days = len(dates)
    else:
        days = 30

    annual_factor = 365 / days
    grid_val = float(D(str(cfg["grid_position"])) * price)
    annual_return = total_profit / grid_val * 100 * annual_factor if grid_val > 0 else 0

    # Sharpe ratio (risk-free = 2%)
    if len(profits) >= 3:
        mean_ret = sum(profits) / len(profits)
        variance = sum((p - mean_ret) ** 2 for p in profits) / (len(profits) - 1)
        std_ret = variance ** 0.5
        sharpe = (mean_ret - 0.02 * mean_ret * days / 365) / std_ret * (365 / days) ** 0.5 if std_ret > 0 else 0
        sortino_denom = (sum((p - mean_ret) ** 2 for p in profits if p < mean_ret) / max(len([p for p in profits if p < mean_ret]), 1)) ** 0.5
        sortino = (mean_ret - 0.02 * mean_ret * days / 365) / sortino_denom * (365 / days) ** 0.5 if sortino_denom > 0 else 0
    else:
        sharpe = 0
        sortino = 0

    calmar = annual_return / (max_dd / abs(cumsum) * 100) if max_dd > 0 and cumsum != 0 else 0

    print()
    _header(f"{cfg['name']} ({etf_code}) 绩效分析 (对标 BP_Grid_Trading_Bot)")
    _thin()
    _row(f"📊 交易统计")
    _row(f"  总交易对: {len(trades)}  |  胜率: {win_rate:.1f}%  |  盈亏比: {profit_factor:.2f}")
    _row(f"  平均盈利: ¥{avg_win:+.2f}  |  平均亏损: ¥{avg_loss:+.2f}")
    _thin()
    _row(f"📈 收益指标")
    _row(f"  总盈亏: ¥{total_profit:+.2f}  |  年化收益: {annual_return:.1f}%")
    _thin()
    _row(f"📉 风险指标")
    _row(f"  最大回撤: ¥{max_dd:.2f}  |  Sharpe: {sharpe:.2f}  |  Sortino: {sortino:.2f}")
    _row(f"  Calmar: {calmar:.2f}  |  统计天数: {days}")
    _thin()
    # 评分
    score = 0
    if sharpe > 2.0: score += 3
    elif sharpe > 1.0: score += 2
    elif sharpe > 0.5: score += 1
    if win_rate > 60: score += 2
    elif win_rate > 50: score += 1
    if profit_factor > 2: score += 2
    elif profit_factor > 1.5: score += 1
    grade = {5: 'A+', 4: 'A', 3: 'B', 2: 'C', 1: 'D', 0: 'F'}.get(min(score, 5), 'F')
    _row(f"🏆 综合评分: {grade}  (Sharpe≥2→A+, 胜率≥60%→+2, 盈亏比≥2→+2)")
    _row(f"  评分对标 ptulluri/BP_Grid_Trading_Bot 机构级标准")
    _footer()
    print()


def cmd_tune(etf_code=None):
    """参数网格搜索优化：枚举间距×层数，最大化年化收益或 Sharpe。

    对标 EasyXT 的一键参数优化 + penny-vault 的贝叶斯搜索思路（简化版）。
    """
    if not etf_code:
        etf_code = list(CONFIGS.keys())[0]

    cfg = CONFIGS.get(etf_code)
    if not cfg:
        print(f"未找到配置: {etf_code}")
        return

    bp = D(cfg["base_price"])
    # 估算波动率：从触发频率反推
    triggers = get_triggers_for(etf_code)
    if len(triggers) >= 5:
        d1 = date.fromisoformat(triggers[0]["date"])
        d2 = date.fromisoformat(triggers[-1]["date"])
        days = max((d2 - d1).days, 1)
        rt_count = sum(1 for t in triggers if t["action"] == "sell")  # 用卖出来估往返
        observed_vol = (rt_count / days * 365) ** 0.5 * float(bp) * 0.1  # 粗略估计
        vol_est = max(min(observed_vol, 0.50), 0.15)  # 钳制 [15%, 50%]
    else:
        # 默认波动率估计
        vol_defaults = {"513180": 0.38, "159915": 0.28, "510300": 0.20, "159920": 0.22}
        vol_est = vol_defaults.get(etf_code, 0.25)

    spg = cfg["shares_per_grid"]
    k = 0.22  # 效率系数（开源项目经验值）

    print()
    _header(f"{cfg['name']} ({etf_code}) 参数优化 (Grid Search)")
    _row(f"  波动率估计: {vol_est*100:.0f}%  |  效率系数 k={k}")
    _row(f"  搜索范围: 间距 ±1.5%~±5.0%  |  层数 3~8")
    _sep()

    best = {"sharpe": -999, "profit": 0, "spacing": 2.5, "layers": 5}
    print(f"║  {'间距':>6} │ {'层数':>4} │ {'年往返':>6} │ {'单次利润':>8} │ {'年利润':>9} │ {'网格资金':>9} │ {'ROI':>6} │ {'Sharpe*':>7} ║")
    _thin()

    results_list = []
    for spacing in [s/10 for s in range(15, 51, 2)]:  # 1.5% ~ 5.0%
        for layers in range(3, 9):  # 3 ~ 8
            annual_rt = k * (vol_est ** 2) / ((spacing/100) ** 2)
            rt_profit = D(str(spg)) * bp * D(str(spacing * 2)) / 100
            annual_profit = rt_profit * D(str(round(annual_rt, 1)))

            # 网格资金需求
            buy_extra = D(0)
            for i in range(1, layers + 1):
                buy_price = bp * (1 - D(str(spacing))/100) ** i
                buy_extra += D(str(spg)) * buy_price
            grid_capital = D(str(spg * layers)) * bp + buy_extra

            roi = float(annual_profit) / float(grid_capital) * 100 if float(grid_capital) > 0 else 0
            # 近似 Sharpe: ROI / (vol_est * 100) 简化
            approx_sharpe = roi / (vol_est * 100) if vol_est > 0 else 0

            results_list.append({
                "spacing": spacing, "layers": layers,
                "annual_rt": annual_rt, "rt_profit": float(rt_profit),
                "annual_profit": float(annual_profit), "grid_capital": float(grid_capital),
                "roi": roi, "sharpe": approx_sharpe,
            })

            mark = ""
            if roi > best["profit"]:
                mark += "💰"
            if approx_sharpe > best["sharpe"]:
                best["sharpe"] = approx_sharpe
                best["spacing"] = spacing
                best["layers"] = layers
                best["profit"] = float(annual_profit)
                mark += "⭐"

            print(f"║  ±{spacing:.1f}% │ {layers:>3}  │ {annual_rt:>6.0f} │ ¥{float(rt_profit):>7.1f} │ ¥{float(annual_profit):>8.0f} │ ¥{float(grid_capital):>8.0f} │ {roi:>5.1f}% │ {approx_sharpe:>6.2f} {mark}")

    _footer()

    # 最优推荐
    print(f"\n  ⭐ 最优参数 (最高 ROI+Sharpe): ±{best['spacing']:.1f}%  |  {best['layers']}+{best['layers']} 层")
    print(f"     预期年利润: ¥{best['profit']:,.0f}")

    current_sp = float(_sp_up(cfg) + _sp_down(cfg)) / 200
    current_la = cfg.get("levels_above", 5)
    if abs(best["spacing"] - current_sp) > 0.003 or best["layers"] != current_la:
        print(f"     当前设置: ±{current_sp*100:.1f}%  |  {current_la}+{current_la} 层")
        print(f"     🟡 建议调整 → 更新 CONFIGS 中的 grid_spacing_pct 和 levels_above/below")
    else:
        print(f"     ✅ 当前参数已是最优")
    print()


def _execute_triggers_at_price(bp, price, cash, pos, fifo_queue, trades, date_str,
                                max_position, base_shares, spg, sp_up, sp_down,
                                la, lb, board_lot, comm_rate, slip_rate,
                                triggered_buy, triggered_sell):
    """在一个价位点逐层执行所有网格触发。

    每触发一层立即更新 bp，重新计算档次后继续判断。
    返回 (bp, cash, pos, fifo_queue, triggered_buy, triggered_sell)。
    """
    while True:
        any_triggered = False

        # --- 卖出方向：每次只执行离当前基准最近的一层 ---
        trig = bp * (1 + sp_up / 100)
        if price >= trig and pos > base_shares:
            sell_shares = min(spg, pos - base_shares)
            sell_shares = (sell_shares // board_lot) * board_lot
            if sell_shares >= board_lot:
                sell_fill = trig * (1 - slip_rate)
                sell_gross = sell_fill * sell_shares
                sell_comm = sell_gross * comm_rate
                sell_net = sell_gross - sell_comm
                sell_slip_amount = (trig - sell_fill) * sell_shares

                remaining = sell_shares
                cost_of_sold = 0.0
                while remaining > 0 and fifo_queue:
                    lot = fifo_queue[0]
                    matched = min(remaining, lot["shares"])
                    unit_cost = lot["cost"] / lot["shares"]
                    cost_of_sold += unit_cost * matched
                    lot["shares"] -= matched
                    lot["cost"] -= unit_cost * matched
                    remaining -= matched
                    if lot["shares"] == 0:
                        fifo_queue.pop(0)

                cash += sell_net
                pos -= sell_shares
                bp = trig
                triggered_sell += 1
                trades.append({
                    "date": date_str, "action": "sell",
                    "price": trig, "fill_price": sell_fill,
                    "shares": sell_shares,
                    "commission": sell_comm, "slippage": sell_slip_amount,
                    "realized_pnl": sell_net - cost_of_sold,
                    "cash_after": cash, "pos_after": pos,
                })
                any_triggered = True

        if any_triggered:
            continue

        # --- 买入方向：每次只执行离当前基准最近的一层 ---
        trig = bp * (1 - sp_down / 100)
        if price <= trig and cash > 0 and pos < max_position:
            buy_shares = min(spg, max_position - pos)
            buy_shares = (buy_shares // board_lot) * board_lot
            if buy_shares >= board_lot:
                buy_fill = trig * (1 + slip_rate)
                buy_gross = buy_fill * buy_shares
                buy_comm = buy_gross * comm_rate
                buy_cost = buy_gross + buy_comm
                buy_slip_amount = (buy_fill - trig) * buy_shares

                if buy_cost > cash:
                    affordable_raw = int(cash / (buy_fill * (1 + comm_rate)))
                    buy_shares = (affordable_raw // board_lot) * board_lot
                    if buy_shares >= board_lot:
                        buy_gross = buy_fill * buy_shares
                        buy_comm = buy_gross * comm_rate
                        buy_cost = buy_gross + buy_comm
                        buy_slip_amount = (buy_fill - trig) * buy_shares

                if buy_shares >= board_lot and buy_cost <= cash:
                    cash -= buy_cost
                    pos += buy_shares
                    bp = trig
                    triggered_buy += 1
                    fifo_queue.append({
                        "price": buy_fill, "shares": buy_shares,
                        "cost": buy_cost,
                    })
                    trades.append({
                        "date": date_str, "action": "buy",
                        "price": trig, "fill_price": buy_fill,
                        "shares": buy_shares,
                        "commission": buy_comm, "slippage": buy_slip_amount,
                        "realized_pnl": 0.0,
                        "cash_after": cash, "pos_after": pos,
                    })
                    any_triggered = True

        if not any_triggered:
            break

    return bp, cash, pos, fifo_queue, triggered_buy, triggered_sell


def _run_intraday_path(bp_start, cash_start, pos_start, fifo_start,
                        path_sequence, date_str,
                        max_position, base_shares, spg, sp_up, sp_down,
                        la, lb, board_lot, comm_rate, slip_rate,
                        triggered_buy_start, triggered_sell_start):
    """模拟一条日内路径，返回该路径的最终状态和局部交易记录。

    path_sequence: [(label, price), ...]  如 [('open', 1.01), ('high', 1.02), ...]
    返回 (bp, cash, pos, fifo_queue, local_trades, triggered_buy, triggered_sell)
    """
    bp = bp_start
    cash = cash_start
    pos = pos_start
    fifo = [dict(f) for f in fifo_start]
    local_trades = []
    t_buy = triggered_buy_start
    t_sell = triggered_sell_start

    for _label, price in path_sequence:
        bp, cash, pos, fifo, t_buy, t_sell = _execute_triggers_at_price(
            bp, price, cash, pos, fifo, local_trades, date_str,
            max_position, base_shares, spg, sp_up, sp_down,
            la, lb, board_lot, comm_rate, slip_rate,
            t_buy, t_sell)

    return bp, cash, pos, fifo, local_trades, t_buy, t_sell


def run_grid_backtest(
    closes, dates,
    opens=None, highs=None, lows=None,
    *,
    spacing_up_pct=3.0, spacing_down_pct=3.0,
    levels_above=5, levels_below=5,
    shares_per_grid=1000,
    total_capital=100000.0,
    position_pct=0.6,
    base_ratio=0.6,
    stop_loss_ratio=0.75,
    execution=None,
):
    """历史 K 线回测核心函数 — 无 CLI 依赖，可被测试直接调用。

    参数:
        closes, dates: 收盘价和日期列表（必须）
        opens, highs, lows: OHLC 其他字段（可选，None 则 fallback 到 close）
        spacing_up_pct, spacing_down_pct: 网格间距（%）
        levels_above, levels_below: 网格层数
        shares_per_grid: 每格委托股数
        total_capital: 总资金
        position_pct: 初始仓位占比（0~1）
        base_ratio: 底仓占初始仓位比例（0~1）
        stop_loss_ratio: 止损比例（0 表示不启用止损）
        execution: ExecutionConfig 实例（None 则用默认）

    返回 dict，包含 equity_curve、trades、各项绩效指标。
    支持 OHLC 日内路径模拟：用两条路径 (open→high→low→close 和
    open→low→high→close) 模拟日内价格路径，双向歧义日取较低收盘权益。
    """
    if execution is None:
        execution = ExecutionConfig()

    board_lot = execution.board_lot
    comm_rate = float(execution.commission_rate)
    slip_rate = float(execution.slippage_rate)

    n = len(closes)
    if n < 2:
        raise ValueError(f"至少需要 2 个交易日，实际 {n}")

    # 使用传入的 OHLC 或 fallback 到 close
    _opens = opens if opens else closes
    _highs = highs if highs else closes
    _lows = lows if lows else closes

    # --- 初始建仓（含佣金+滑点，取整到 board_lot） ---
    initial_price = _opens[0]
    initial_bp = initial_price  # 止损基准价

    position_value = total_capital * position_pct
    fill_price_entry = initial_price * (1 + slip_rate)  # 买入：向上滑点
    cost_per_share = fill_price_entry * (1 + comm_rate)

    raw_shares = int(position_value / cost_per_share)
    initial_shares = (raw_shares // board_lot) * board_lot
    base_shares = (int(initial_shares * base_ratio) // board_lot) * board_lot
    grid_shares = initial_shares - base_shares

    initial_gross = fill_price_entry * initial_shares
    initial_commission = initial_gross * comm_rate
    initial_slippage = (fill_price_entry - initial_price) * initial_shares

    cash = total_capital - initial_gross - initial_commission

    # --- B&H（同费率建仓） ---
    bh_raw = int(total_capital / cost_per_share)
    bh_shares = (bh_raw // board_lot) * board_lot
    bh_gross = fill_price_entry * bh_shares
    bh_commission = bh_gross * comm_rate
    bh_remaining_cash = total_capital - bh_gross - bh_commission

    # --- 网格参数 ---
    la = levels_above
    lb = levels_below
    sp_up = spacing_up_pct
    sp_down = spacing_down_pct
    spg = shares_per_grid

    # 自动调整每格股数（如果比初始仓位还大）
    if spg * la > grid_shares and grid_shares > 0:
        spg = max(board_lot, (grid_shares // la // board_lot) * board_lot)

    max_position = base_shares + grid_shares + spg * lb

    # --- 止损 ---
    if stop_loss_ratio > 0:
        stop_loss_price = initial_bp * stop_loss_ratio
    else:
        stop_loss_price = 0.0

    # --- 模拟状态 ---
    bp = initial_price       # 动态基准价
    pos = initial_shares     # 当前持仓
    trades = []              # 交易记录
    equity_curve = []        # 权益曲线

    # FIFO 买入队列：种子为初始网格仓位
    initial_lot_cost = (initial_gross + initial_commission) * grid_shares / initial_shares
    fifo_queue = []
    if grid_shares > 0:
        fifo_queue.append({
            "price": fill_price_entry, "shares": grid_shares,
            "cost": initial_lot_cost
        })

    # 记录初始建仓
    trades.append({
        "date": dates[0], "action": "buy", "price": initial_price,
        "fill_price": fill_price_entry, "shares": initial_shares,
        "commission": initial_commission, "slippage": initial_slippage,
        "cash_after": cash, "pos_after": initial_shares,
    })

    stop_loss_triggered = False
    stop_date = None
    triggered_buy = 0
    triggered_sell = 0
    ambiguous_bar_count = 0

    # 首日已在 open 建仓，继续模拟 open 之后的盘中路径。
    first_path = [("high", _highs[0]), ("low", _lows[0]), ("close", closes[0])]
    bp, cash, pos, fifo_queue, first_trades, triggered_buy, triggered_sell = _run_intraday_path(
        bp, cash, pos, fifo_queue, first_path, dates[0],
        max_position, base_shares, spg, sp_up, sp_down,
        la, lb, board_lot, comm_rate, slip_rate,
        triggered_buy, triggered_sell,
    )
    trades.extend(first_trades)
    equity_curve.append({
        "date": dates[0], "equity": cash + pos * closes[0],
        "cash": cash, "position": pos, "close": closes[0],
    })

    # --- 后续逐日模拟（OHLC 日内路径） ---
    for i in range(1, n):
        date_str = dates[i]
        close = closes[i]
        day_open = _opens[i]
        day_high = _highs[i]
        day_low = _lows[i]

        # 止损检查（使用 low 判断）
        if stop_loss_price > 0 and day_low <= stop_loss_price:
            stop_loss_triggered = True
            stop_date = date_str

            # 止损成交价：开盘已低于止损 → 按开盘成交；否则按止损价
            if day_open < stop_loss_price:
                stop_fill = day_open
            else:
                stop_fill = stop_loss_price

            # 只清网格仓，保留底仓
            grid_current = max(0, pos - base_shares)
            if grid_current > 0:
                # 卖出滑点
                stop_actual = stop_fill * (1 - slip_rate)
                stop_gross = stop_actual * grid_current
                stop_comm = stop_gross * comm_rate
                stop_net = stop_gross - stop_comm
                stop_slip_amount = (stop_fill - stop_actual) * grid_current

                remaining = grid_current
                cost_of_sold = 0.0
                while remaining > 0 and fifo_queue:
                    lot = fifo_queue[0]
                    matched = min(remaining, lot["shares"])
                    unit_cost = lot["cost"] / lot["shares"]
                    cost_of_sold += unit_cost * matched
                    lot["shares"] -= matched
                    lot["cost"] -= unit_cost * matched
                    remaining -= matched
                    if lot["shares"] == 0:
                        fifo_queue.pop(0)

                cash += stop_net
                pos -= grid_current

                trades.append({
                    "date": date_str, "action": "stop_loss",
                    "price": stop_fill, "fill_price": stop_actual,
                    "shares": grid_current,
                    "commission": stop_comm, "slippage": stop_slip_amount,
                    "realized_pnl": stop_net - cost_of_sold,
                    "cash_after": cash, "pos_after": pos,
                })

            # 记录止损日权益（mark-to-market）
            equity = cash + pos * close
            equity_curve.append({
                "date": date_str, "equity": equity,
                "cash": cash, "position": pos, "close": close,
            })
            break

        # --- 检查是否有双向触发潜力 ---
        has_sell_potential = pos > base_shares and any(
            day_high >= bp * (1 + sp_up / 100) ** j for j in range(1, la + 1))
        has_buy_potential = cash > 0 and pos < max_position and any(
            day_low <= bp * (1 - sp_down / 100) ** j for j in range(1, lb + 1))
        needs_two_paths = has_sell_potential and has_buy_potential

        # --- 构建两条日内路径 ---
        path_a = [("open", day_open), ("high", day_high),
                   ("low", day_low), ("close", close)]
        path_b = [("open", day_open), ("low", day_low),
                   ("high", day_high), ("close", close)]

        # 快照当前状态
        bp_snap = bp
        cash_snap = cash
        pos_snap = pos
        fifo_snap = [dict(f) for f in fifo_queue]
        tb_snap = triggered_buy
        ts_snap = triggered_sell

        # --- 执行路径 A ---
        bp_a, cash_a, pos_a, fifo_a, trades_a, tb_a, ts_a = _run_intraday_path(
            bp_snap, cash_snap, pos_snap, fifo_snap,
            path_a, date_str,
            max_position, base_shares, spg, sp_up, sp_down,
            la, lb, board_lot, comm_rate, slip_rate,
            tb_snap, ts_snap)

        equity_a = cash_a + pos_a * close

        if needs_two_paths:
            # --- 执行路径 B ---
            bp_b, cash_b, pos_b, fifo_b, trades_b, tb_b, ts_b = _run_intraday_path(
                bp_snap, cash_snap, pos_snap, fifo_snap,
                path_b, date_str,
                max_position, base_shares, spg, sp_up, sp_down,
                la, lb, board_lot, comm_rate, slip_rate,
                tb_snap, ts_snap)

            equity_b = cash_b + pos_b * close

            # 双向歧义：两条路径结果不同 → 取较低期末权益
            if abs(equity_a - equity_b) > 0.01:
                ambiguous_bar_count += 1

            if equity_a <= equity_b:
                # 选择路径 A
                bp, cash, pos = bp_a, cash_a, pos_a
                fifo_queue = fifo_a
                triggered_buy, triggered_sell = tb_a, ts_a
                trades.extend(trades_a)
            else:
                # 选择路径 B
                bp, cash, pos = bp_b, cash_b, pos_b
                fifo_queue = fifo_b
                triggered_buy, triggered_sell = tb_b, ts_b
                trades.extend(trades_b)
        else:
            # 仅单方向触发或无触发，路径 A 即可
            bp, cash, pos = bp_a, cash_a, pos_a
            fifo_queue = fifo_a
            triggered_buy, triggered_sell = tb_a, ts_a
            trades.extend(trades_a)

        # 记录当日权益（mark-to-market）
        equity = cash + pos * close
        equity_curve.append({
            "date": date_str, "equity": equity,
            "cash": cash, "position": pos, "close": close,
        })

    # --- 事后指标 ---
    if not equity_curve:
        return {
            "equity_curve": [],
            "trades": [],
            "final_equity": total_capital,
            "final_cash": cash,
            "final_position": initial_shares,
            "grid_return_pct": 0.0, "bh_return_pct": 0.0,
            "grid_annual_pct": 0.0, "bh_annual_pct": 0.0,
            "sharpe": 0.0, "sortino": 0.0, "max_dd": 0.0, "max_dd_date": "",
            "win_rate": 0.0, "profit_factor": 0.0, "total_realized_pnl": 0.0,
            "stop_loss_triggered": stop_loss_triggered,
            "stop_date": stop_date, "stop_loss_price": stop_loss_price,
            "initial_shares": initial_shares, "base_shares": base_shares,
            "grid_shares": grid_shares,
            "initial_gross": initial_gross, "initial_commission": initial_commission,
            "initial_slippage": initial_slippage,
            "bh_shares": bh_shares, "bh_gross": bh_gross,
            "bh_commission": bh_commission, "bh_remaining_cash": bh_remaining_cash,
            "bh_final_equity": total_capital,
            "triggered_buy": triggered_buy, "triggered_sell": triggered_sell,
            "ambiguous_bar_count": ambiguous_bar_count,
            "total_bar_count": n,
            "calmar": 0.0, "grade": "F", "alpha_pct": 0.0,
            "total_trading_days": 0, "years": 0.0,
            "execution_config": execution,
        }

    final_equity = equity_curve[-1]["equity"]
    final_pos = equity_curve[-1]["position"]
    final_close = equity_curve[-1]["close"]
    final_cash_val = equity_curve[-1]["cash"]

    # B&H 期末 mark-to-market（无退出费）
    bh_final_equity = bh_shares * final_close + bh_remaining_cash

    # 收益率
    grid_return_pct = (final_equity - total_capital) / total_capital * 100
    bh_return_pct = (bh_final_equity - total_capital) / total_capital * 100

    # 年化
    total_days = len(equity_curve)
    years = max(total_days / 252.0, 1.0 / 252.0)
    if final_equity > 0:
        grid_annual_pct = ((final_equity / total_capital) ** (1.0 / years) - 1) * 100
    else:
        grid_annual_pct = -100.0
    if bh_final_equity > 0:
        bh_annual_pct = ((bh_final_equity / total_capital) ** (1.0 / years) - 1) * 100
    else:
        bh_annual_pct = -100.0

    # 日收益率序列
    daily_rets = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]["equity"]
        if prev > 0:
            daily_rets.append((equity_curve[i]["equity"] - prev) / prev)

    # Sharpe / Sortino
    if len(daily_rets) >= 20:
        mean_ret = sum(daily_rets) / len(daily_rets)
        variance = sum((r - mean_ret) ** 2 for r in daily_rets) / max(len(daily_rets) - 1, 1)
        std_ret = variance ** 0.5
        rf_daily = 0.02 / 252.0
        sharpe = (mean_ret - rf_daily) / std_ret * (252.0 ** 0.5) if std_ret > 0 else 0.0
        downside = [r for r in daily_rets if r < 0]
        if downside:
            down_std = (sum((r - mean_ret) ** 2 for r in downside) / len(downside)) ** 0.5
            sortino = (mean_ret - rf_daily) / down_std * (252.0 ** 0.5) if down_std > 0 else 0.0
        else:
            sortino = 0.0
    else:
        sharpe = 0.0
        sortino = 0.0

    # MaxDD
    peak = total_capital
    max_dd = 0.0
    max_dd_date = ""
    for pt in equity_curve:
        eq = pt["equity"]
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak
        if dd > max_dd:
            max_dd = dd
            max_dd_date = pt["date"]

    # 成交时已按完整 FIFO 成本（含买卖佣金和滑点）记录净已实现盈亏。
    realized_pnls = [
        float(trade["realized_pnl"])
        for trade in trades
        if trade["action"] in ("sell", "stop_loss")
        and "realized_pnl" in trade
    ]

    if realized_pnls:
        wins = [p for p in realized_pnls if p > 0]
        losses = [p for p in realized_pnls if p < 0]
        win_rate = len(wins) / len(realized_pnls) * 100
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float('inf')
        total_realized_pnl = sum(realized_pnls)
    else:
        win_rate = 0.0
        avg_win = 0.0
        avg_loss = 0.0
        profit_factor = 0.0
        total_realized_pnl = 0.0

    # Calmar
    calmar = grid_annual_pct / (max_dd * 100) if max_dd > 0 else 0.0

    # 评级
    grade_score = 0
    if sharpe > 2.0: grade_score += 3
    elif sharpe > 1.0: grade_score += 2
    elif sharpe > 0.5: grade_score += 1
    if win_rate > 60: grade_score += 2
    elif win_rate > 50: grade_score += 1
    if profit_factor > 2: grade_score += 2
    elif profit_factor > 1.5: grade_score += 1
    grade = {5: 'A+', 4: 'A', 3: 'B', 2: 'C', 1: 'D', 0: 'F'}.get(min(grade_score, 5), 'F')
    alpha_pct = grid_annual_pct - bh_annual_pct

    return {
        "equity_curve": equity_curve,
        "trades": trades,
        "final_equity": final_equity,
        "final_cash": final_cash_val,
        "final_position": final_pos,
        "grid_return_pct": grid_return_pct,
        "bh_return_pct": bh_return_pct,
        "grid_annual_pct": grid_annual_pct,
        "bh_annual_pct": bh_annual_pct,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_dd": max_dd,
        "max_dd_date": max_dd_date,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "total_realized_pnl": total_realized_pnl,
        "stop_loss_triggered": stop_loss_triggered,
        "stop_date": stop_date,
        "stop_loss_price": stop_loss_price,
        "initial_shares": initial_shares,
        "base_shares": base_shares,
        "grid_shares": grid_shares,
        "initial_gross": initial_gross,
        "initial_commission": initial_commission,
        "initial_slippage": initial_slippage,
        "bh_shares": bh_shares,
        "bh_gross": bh_gross,
        "bh_commission": bh_commission,
        "bh_remaining_cash": bh_remaining_cash,
        "bh_final_equity": bh_final_equity,
        "triggered_buy": triggered_buy,
        "triggered_sell": triggered_sell,
        "ambiguous_bar_count": ambiguous_bar_count,
        "total_bar_count": n,
        "calmar": calmar,
        "grade": grade,
        "alpha_pct": alpha_pct,
        "total_trading_days": total_days,
        "years": years,
        "execution_config": execution,
    }


def cmd_backtest(etf_code=None, start_date=None, end_date=None,
                 capital=None, override_spacing=None, override_levels=None,
                 override_shares=None, output_json=False):
    """历史 K 线回测：模拟网格交易，输出绩效指标 vs 买入持有。

    用法: python3 grid_trading.py backtest CODE [--start YYYY-MM-DD]
              [--end YYYY-MM-DD] [--capital N] [--spacing X] [--levels N] [--shares N]
              [--json]

    不指定起止日期则回测最近 2 年。
    不指定资金则默认 ¥100,000。
    不指定网格参数则从 CONFIGS 读取，无 CONFIGS 则用默认值。
    --json: 输出 JSON 格式（含 OHLC 歧义日统计等结构化字段）。
    """
    if not etf_code:
        etf_code = list(CONFIGS.keys())[0]

    cfg = CONFIGS.get(etf_code, {})
    name = cfg.get("name", etf_code)

    # --- 参数解析 ---
    sp_up = float(override_spacing if override_spacing else
                  cfg.get("grid_spacing_up_pct", cfg.get("grid_spacing_pct", "3.0")))
    sp_down = float(override_spacing if override_spacing else
                    cfg.get("grid_spacing_down_pct", cfg.get("grid_spacing_pct", "3.0")))
    la = override_levels or cfg.get("levels_above", 5)
    lb = override_levels or cfg.get("levels_below", 5)
    spg = override_shares or cfg.get("shares_per_grid", 1000)
    total_capital_val = float(capital or 100000)

    # --- 获取 OHLC ---
    closes, dates, opens, highs, lows = _fetch_ohlc_data(
        etf_code, 1500, as_of=end_date or date.today().isoformat()
    )
    if len(closes) < 60:
        print(f"\n  {name} ({etf_code}): K线数据不足（需要 ≥60 日），无法回测\n")
        return

    # --- 按日期过滤 ---
    if start_date or end_date:
        filtered = [[] for _ in range(5)]
        lists = [closes, dates, opens, highs, lows]
        for i in range(len(closes)):
            d = dates[i]
            if start_date and d < start_date:
                continue
            if end_date and d > end_date:
                break
            for j, lst in enumerate(lists):
                filtered[j].append(lst[i])
        closes, dates, opens, highs, lows = filtered

    if len(closes) < 60:
        print(f"\n  {name} ({etf_code}): 指定日期范围内K线不足（{len(closes)}日），无法回测\n")
        return

    # --- 运行回测 ---
    execution = ExecutionConfig()
    print(f"\n  🔄 正在回测 {name} ({etf_code})...")
    print(f"     区间: {dates[0]} ~ {dates[-1]} ({len(closes)} 个交易日)")
    print(f"     初始资金: ¥{total_capital_val:,.0f}  "
          f"成本率: {float(execution.commission_rate)*10000:.1f}‱  "
          f"滑点: {float(execution.slippage_rate)*10000:.1f}‱")
    print()

    result = run_grid_backtest(
        closes, dates,
        opens=opens, highs=highs, lows=lows,
        spacing_up_pct=sp_up,
        spacing_down_pct=sp_down,
        levels_above=la,
        levels_below=lb,
        shares_per_grid=spg,
        total_capital=total_capital_val,
        execution=execution,
    )

    # --- 输出 ---
    total_days = result["total_trading_days"]
    years = result["years"]
    grid_return = result["grid_return_pct"]
    bh_return = result["bh_return_pct"]
    grid_annual = result["grid_annual_pct"]
    bh_annual = result["bh_annual_pct"]
    sharpe = result["sharpe"]
    sortino = result["sortino"]
    max_dd = result["max_dd"]
    max_dd_date = result["max_dd_date"]
    win_rate = result["win_rate"]
    profit_factor = result["profit_factor"]
    total_realized = result["total_realized_pnl"]
    final_pos = result["final_position"]
    final_cash_val = result["final_cash"]
    calmar = result["calmar"]
    grade = result["grade"]
    alpha = result["alpha_pct"]
    triggered_buy = result["triggered_buy"]
    triggered_sell = result["triggered_sell"]
    trades = result["trades"]
    stop_triggered = result["stop_loss_triggered"]
    stop_date = result["stop_date"]
    stop_loss_price = result["stop_loss_price"]
    initial_shares = result["initial_shares"]
    base_shares = result["base_shares"]
    grid_shares = result["grid_shares"]
    comm = result["initial_commission"]
    slip = result["initial_slippage"]

    print(f"╔{'═' * 62}╗")
    print(f"║  {name} ({etf_code}) 网格回测报告"
          f"{' ' * (45 - len(name) - len(etf_code))}║")
    print(f"╠{'═' * 62}╣")
    print(f"║  回测区间: {dates[0]} ~ {dates[-1]} ({total_days}个交易日, "
          f"{years:.1f}年){' ' * max(0, 10)}║")
    print(f"║  网格参数: +{sp_up}%/-{sp_down}%  "
          f"{la}+{lb}层  {spg}股/格{' ' * max(0, 12)}║")
    print(f"║  初始建仓: {initial_shares}股（底仓{base_shares}+网格{grid_shares}）")
    print(f"║  建仓佣金: ¥{comm:.2f}  滑点: ¥{slip:.2f}")

    if stop_triggered:
        print(f"║  ⛔ 止损触发: {stop_date}  ¥{result['stop_loss_price']:.4f}"
              f"{' ' * max(0, 20)}║")

    print(f"╟{'─' * 62}╢")
    print(f"║  {'指标':<24s} {'网格':>12s} {'买入持有':>12s}   ║")
    print(f"╟{'─' * 62}╢")
    print(f"║  {'总收益率':<24s} {grid_return:>+11.2f}% "
          f"{bh_return:>+11.2f}%  ║")
    print(f"║  {'年化收益率':<24s} {grid_annual:>+11.2f}% "
          f"{bh_annual:>+11.2f}%  ║")
    print(f"║  {'最终权益':<24s} {'¥' + str(int(result['final_equity'])):>12s} "
          f"{'¥' + str(int(result['bh_final_equity'])):>12s}  ║")
    print(f"╟{'─' * 62}╢")
    print(f"║  {'最大回撤':<24s} {max_dd*100:>11.2f}%"
          f"{'':>13s}  ║")
    print(f"║  {'最大回撤日':<24s} {max_dd_date:>12s}{'':>13s}  ║")
    print(f"║  {'Sharpe Ratio':<24s} {sharpe:>12.2f}{'':>13s}  ║")
    print(f"║  {'Sortino Ratio':<24s} {sortino:>12.2f}{'':>13s}  ║")
    print(f"║  {'Calmar Ratio':<24s} {calmar:>12.2f}{'':>13s}  ║")
    print(f"╟{'─' * 62}╢")
    print(f"║  {'交易统计':<24s} {'':>24s}║")
    grid_only_trades = [t for t in trades if t["action"] != "buy" or t["date"] != dates[0]]
    print(f"║  {'  总成交笔数':<24s} {len(grid_only_trades):>12d}{'':>13s}  ║")
    print(f"║  {'  买入触发':<24s} {triggered_buy:>12d}{'':>13s}  ║")
    print(f"║  {'  卖出触发':<24s} {triggered_sell:>12d}{'':>13s}  ║")
    print(f"║  {'  胜率':<24s} {win_rate:>11.1f}%{'':>13s}  ║")
    print(f"║  {'  盈亏比':<24s} {profit_factor:>12.2f}{'':>13s}  ║")
    print(f"║  {'  已实现盈亏':<24s} {'¥':>11s}{total_realized:>+.0f}{'':>12s}  ║")
    print(f"║  {'  最终持仓':<24s} {final_pos:>12d} 股{'':>13s}  ║")
    print(f"║  {'  最终现金':<24s} {'¥' + str(int(final_cash_val)):>12s}{'':>13s}  ║")
    print(f"╟{'─' * 62}╢")

    print(f"║  综合评级: {grade}  │  超额收益(α): {alpha:+.1f}%  │  "
          f"{'网格优于持有' if alpha > 0 else '持有优于网格' if alpha < 0 else '持平'}"
          f"{' ' * max(0, 6)}║")
    print(f"╚{'═' * 62}╝")
    print()

    # --- OHLC 歧义日统计 ---
    ambiguous_count = result.get("ambiguous_bar_count", 0)
    total_bars = result.get("total_bar_count", 0)
    if ambiguous_count > 0:
        print(f"  → OHLC 日内路径模拟：{total_bars} 个交易日中，{ambiguous_count} 日为双向歧义日"
              f"（同日 high 触卖 + low 触买），取较低期末权益路径。")
    else:
        print(f"  → OHLC 日内路径模拟：{total_bars} 个交易日，无双向歧义日。")

    if abs(alpha) < 2:
        print(f"  → 网格与买入持有收益接近。网格的优势在于降低回撤和提供现金流。")
    elif alpha > 0:
        print(f"  → 网格显著跑赢买入持有（+{alpha:.1f}%年化超额）。震荡市是网格的优势环境。")
    else:
        print(f"  → 网格跑输买入持有（{alpha:.1f}%年化差额）。趋势市中网格会因过早卖出而踏空。")
    if stop_triggered:
        print(f"  → 止损于 {stop_date} 触发（¥{result['stop_loss_price']:.4f}），"
              f"底仓 {base_shares} 股保留。")
    print()

    # --- JSON 输出 ---
    if output_json:
        import json as _json_mod
        json_out = {
            "meta": {
                "etf_code": etf_code,
                "name": name,
                "start_date": dates[0],
                "end_date": dates[-1],
                "total_trading_days": total_days,
                "years": round(years, 2),
                "ohlc_mode": True,
                "ambiguous_bar_count": ambiguous_count,
                "total_bar_count": total_bars,
                "grid_params": {
                    "spacing_up_pct": sp_up,
                    "spacing_down_pct": sp_down,
                    "levels_above": la,
                    "levels_below": lb,
                    "shares_per_grid": spg,
                },
                "execution_config": {
                    "commission_rate": float(execution.commission_rate),
                    "slippage_rate": float(execution.slippage_rate),
                    "board_lot": execution.board_lot,
                },
            },
            "performance": {
                "grid": {
                    "total_return_pct": round(grid_return, 2),
                    "annual_return_pct": round(grid_annual, 2),
                    "final_equity": round(result["final_equity"], 2),
                    "sharpe": round(sharpe, 2),
                    "sortino": round(sortino, 2),
                    "max_dd_pct": round(max_dd * 100, 2),
                    "max_dd_date": max_dd_date,
                    "calmar": round(calmar, 2),
                    "win_rate_pct": round(win_rate, 1),
                    "profit_factor": round(profit_factor, 2),
                    "total_realized_pnl": round(total_realized, 2),
                    "grade": grade,
                    "triggered_buy": triggered_buy,
                    "triggered_sell": triggered_sell,
                },
                "buy_and_hold": {
                    "total_return_pct": round(bh_return, 2),
                    "annual_return_pct": round(bh_annual, 2),
                    "final_equity": round(result["bh_final_equity"], 2),
                },
                "alpha_pct": round(alpha, 2),
            },
            "position": {
                "initial_shares": initial_shares,
                "base_shares": base_shares,
                "grid_shares": grid_shares,
                "final_shares": final_pos,
                "final_cash": round(final_cash_val, 2),
            },
            "stop_loss": {
                "triggered": stop_triggered,
                "date": stop_date,
                "price": float(stop_loss_price) if stop_loss_price else None,
            },
            "trades": [
                {
                    "date": t["date"],
                    "action": t["action"],
                    "price": t["price"],
                    "fill_price": t["fill_price"],
                    "shares": t["shares"],
                    "commission": round(t["commission"], 4),
                    "slippage": round(t["slippage"], 4),
                    "cash_after": round(t["cash_after"], 2),
                    "pos_after": t["pos_after"],
                }
                for t in trades
            ],
        }
        print(_json_mod.dumps(json_out, ensure_ascii=False, indent=2))
        print()


def _fetch_kline_for_ma_with_dates(etf_code, count=120):
    """获取 K 线数据，返回 (closes, dates) 两个列表。"""
    code = etf_code.strip()
    prefix = "sh" if code.startswith(("6", "9", "5")) else "sz"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,{count},qfq"
    try:
        import json as _json
        raw = _curl_text(url, timeout=15)
        data = _json.loads(raw)
        days = (data.get("data", {}).get(f"{prefix}{code}", {}).get("qfqday", []) or
                data.get("data", {}).get(f"{prefix}{code}", {}).get("day", []))
        closes = []
        dates = []
        for d in days:
            if len(d) > 2:
                try:
                    closes.append(float(d[2]))
                    dates.append(d[0])  # 日期字段
                except (ValueError, IndexError):
                    continue
        return closes, dates
    except Exception:
        return [], []


def _fetch_ohlc_data(etf_code, count=1500, as_of=None):
    """通过统一 qfq 数据契约加载 OHLC；不允许静默降级到原始 day。"""
    series = load_etf_series(
        etf_code, count=count, adjustment="qfq", as_of=as_of
    )
    bars = series.bars
    return (
        [bar["close"] for bar in bars],
        [bar["date"] for bar in bars],
        [bar["open"] for bar in bars],
        [bar["high"] for bar in bars],
        [bar["low"] for bar in bars],
    )


def cmd_trend(etf_code=None):
    """趋势过滤器：MA 均线 + 布林带宽度，判断是否适合网格运行。

    对标 Grid-Bot-2 的布林带区间检测 + Dynamic-Grid-Trading 的趋势自适应。
    核心理念：布林带收窄→震荡市→网格活跃；布林带扩张→趋势市→网格暂停。
    """
    if not etf_code:
        etf_code = list(CONFIGS.keys())[0]

    cfg = CONFIGS.get(etf_code)
    if not cfg:
        print(f"未找到配置: {etf_code}")
        return

    closes = _fetch_kline_for_ma(etf_code, 120)
    if len(closes) < 60:
        # 备用：腾讯实时行情
        quote = fetch_qq_quote(etf_code)
        if quote and quote.get("price"):
            print(f"\n  {cfg['name']} ({etf_code}): K线数据不足，仅显示实时价格 ¥{quote['price']}\n")
        else:
            print(f"\n  {cfg['name']} ({etf_code}): 无法获取 K 线数据\n")
        return

    current_price = closes[-1]
    ma20 = _calc_ma(closes, 20)
    ma60 = _calc_ma(closes, 60)
    ma200 = _calc_ma(closes, 200) if len(closes) >= 200 else None
    bb_ma, bb_upper, bb_lower, bb_width = _calc_bollinger(closes, 20)
    atr14 = _calc_atr(closes, period=14)

    # 推荐 ATR 动态间距
    if atr14 and current_price > 0:
        atr_pct = atr14 / current_price * 100
        recommended_spacing = atr_pct * 2.5  # ATR × 2.5 (EasyXT 推荐日线参数)
    else:
        atr_pct = None
        recommended_spacing = None

    # 判断市场状态
    signals = []
    trend_score = 0  # 正=震荡有利, 负=趋势不利

    # 1. MA 方向判断
    if ma20 and ma60:
        near_ma20 = abs(current_price - ma20) / current_price < 0.01  # 价格在 MA20 ±1% 内

        if near_ma20:
            # 价格贴着 MA20: 优先按附近震荡处理，不按多头/空头排列
            # 避免价格在 MA20 上下 1 厘钱波动导致评分在 +2 和 -2 之间跳跃
            if bb_width and bb_width < 8:
                signals.append(("✅", "价格在 MA20 附近 → 震荡，网格活跃"))
                trend_score += 2
            else:
                signals.append(("🟡", f"价格在 MA20 附近但 BB 未收窄({bb_width:.1f}%) → 温和偏震荡"))
                trend_score += 1
        elif current_price > ma20 > ma60:
            signals.append(("🟢", "多头排列 (价格>MA20>MA60) → 上涨趋势，优先做卖出网格"))
            trend_score -= 1
        elif current_price < ma20 < ma60:
            signals.append(("🔴", "空头排列 (价格<MA20<MA60) → 下跌趋势，暂停买入网格"))
            trend_score -= 2
        elif abs(current_price - ma20) / current_price < 0.02:
            signals.append(("✅", "价格在 MA20 附近 → 震荡，网格活跃"))
            trend_score += 2
        else:
            signals.append(("🟡", "均线缠绕 → 方向不明，网格正常"))
            trend_score += 1

    # 2. 布林带宽度（Grid-Bot-2 核心指标）
    if bb_width:
        if bb_width < 4:
            signals.append(("✅", f"布林带收窄 (宽度 {bb_width:.1f}%) → 区间震荡，网格最理想"))
            trend_score += 3
        elif bb_width < 8:
            signals.append(("🟡", f"布林带正常 (宽度 {bb_width:.1f}%) → 正常波动"))
            trend_score += 1
        else:
            signals.append(("🔴", f"布林带扩张 (宽度 {bb_width:.1f}%) → 趋势市，网格应暂停"))
            trend_score -= 2

    # 3. ATR 间距建议
    if atr_pct:
        cfg_sp = float(_sp_up(cfg) + _sp_down(cfg)) / 2
        if recommended_spacing:
            diff = recommended_spacing - cfg_sp
            if abs(diff) > 0.5:
                direction = "放宽" if diff > 0 else "收窄"
                signals.append(("💡", f"ATR({atr_pct:.1f}%) → 建议间距 ±{recommended_spacing:.1f}% (当前 ±{cfg_sp:.1f}%，建议{direction})"))

    # 输出
    print()
    _header(f"{cfg['name']} ({etf_code}) 趋势过滤器")
    _row(f"现价: ¥{current_price:.4f}  |  MA20: ¥{ma20:.3f}  |  MA60: ¥{ma60:.3f}" if ma60 else f"现价: ¥{current_price:.4f}")
    if ma200:
        _row(f"MA200: ¥{ma200:.3f}  |  {'价格在 MA200 之上' if current_price > ma200 else '价格在 MA200 之下'}")
    _row(f"布林带: 中轨 ¥{bb_ma:.3f}  上轨 ¥{bb_upper:.3f}  下轨 ¥{bb_lower:.3f}  宽度 {bb_width:.1f}%" if bb_width else "")
    if atr_pct:
        _row(f"ATR(14): ¥{atr14:.4f} ({atr_pct:.2f}%)  |  建议网格间距: ±{recommended_spacing:.1f}% (ATR×2.5)")
    _thin()
    for icon, msg in signals:
        _row(f"{icon} {msg}")
    _thin()

    if trend_score >= 4:
        verdict = "✅ 震荡市 — 网格最佳环境，全力运行"
    elif trend_score >= 1:
        verdict = "🟢 偏震荡 — 网格正常运行"
    elif trend_score >= -1:
        verdict = "🟡 偏趋势 — 网格谨慎运行，关注方向"
    elif trend_score >= -3:
        verdict = "🔴 趋势市 — 建议暂停反向网格，仅保留顺势方向"
    else:
        verdict = "⛔ 强趋势 — 暂停所有网格，等待回归震荡"

    _row(f"综合评分: {trend_score:+d}  →  {verdict}")
    _row(f"参考: Grid-Bot-2 布林带区间检测 + Dynamic-Grid-Trading 趋势自适应")
    _footer()
    print()


def print_help():
    print("""
网格交易管理工具 — 独立脚本，零外部依赖

用法: python3 grid_trading.py <命令> [参数]

命令:
  table                生成所有 ETF 网格价格表
  table <etf>          查看指定 ETF
  quote [etf]          下单前行情快照（价格/IOPV/溢价率/盘口/成交额）
  signal [etf]         量价信号（折溢价+成交量突变+触发距离→操作建议）
  status [etf] [价格]  查看网格状态（不输价格自动拉行情）
  trigger [etf] [--buy|--sell] 录入成交（自动判断方向，--buy/--sell 强制指定）
  pnl [etf] [价格]     盈亏统计（不输价格自动拉行情）
  risk [etf] [价格]    风险检查（不输价格自动拉行情）
  watch [etf] [秒]     实时监控+量价信号，默认 30s 刷新（Ctrl+C 退出）
  perf [etf]           绩效分析（Sharpe/Sortino/MaxDD/胜率/盈亏比，对标机构标准）
  tune [etf]           参数优化（网格搜索最优间距+层数，对标 EasyXT）
  trend [etf]          趋势过滤器（MA均线+布林带+ATR，判断是否适合跑网格）
  backtest [etf]       历史回测（模拟网格交易，对比买入持有，含Sharpe/MaxDD/胜率）
                        --start/--end DATE  --capital N  --spacing X  --levels N
                        --shares N  --json

示例:
  python3 grid_trading.py table 513180
  python3 grid_trading.py perf 513180             # 绩效分析（Sharpe/胜率等）
  python3 grid_trading.py tune 513180             # 参数优化搜索
  python3 grid_trading.py trend 513180            # 趋势过滤（布林带+ATR）
  python3 grid_trading.py backtest 513180         # 历史回测（模拟网格 vs 持有）
  python3 grid_trading.py backtest 513180 --start 2024-01-01 --end 2025-12-31
  python3 grid_trading.py quote 513180            # 下单前行情快照
  python3 grid_trading.py signal 513180           # 量价信号
  python3 grid_trading.py status 513180           # 自动获取实时价
  python3 grid_trading.py trigger 513180
  python3 grid_trading.py pnl 513180
  python3 grid_trading.py watch 513180            # 30s 刷新监控

首次使用：编辑脚本顶部 CONFIGS 字典，修改为你的持仓参数。
触发记录保存在 data/grid_triggers.json
成交量历史保存在 data/grid_volume_history.json
""")


# --- 入口 ---

def main():
    args = sys.argv[1:]

    if not args or args[0] in ("help", "-h", "--help"):
        print_help()
        return

    cmd = args[0]

    # 解析 ETF 代码、方向标志和价格参数
    etf = None
    price = None
    force_direction = None  # "buy" / "sell" / None
    for a in args[1:]:
        if a in ("--buy", "-b"):
            force_direction = "buy"
        elif a in ("--sell", "-s"):
            force_direction = "sell"
        elif a in CONFIGS or (cmd == "quote" and len(a) == 6 and a.isdigit()):
            etf = a
        elif len(a) == 6 and a.isdigit():
            # 6 位数字但不在 CONFIGS 中，非 quote 命令 → 报错
            print(f"未找到配置: {a}")
            print(f"已配置的 ETF: {', '.join(CONFIGS.keys())}")
            print(f"如需添加，请编辑 tools/grid_trading.py 顶部的 CONFIGS 字典")
            sys.exit(1)
        else:
            try:
                D(a)
                price = a
            except Exception:
                pass

    if cmd == "table":
        cmd_table(etf)
    elif cmd == "quote":
        cmd_quote(etf)
    elif cmd == "signal":
        cmd_signal(etf)
    elif cmd == "status":
        cmd_status(etf, price)
    elif cmd == "trigger":
        cmd_trigger(etf, force_direction)
    elif cmd == "pnl":
        cmd_pnl(etf, price)
    elif cmd == "risk":
        cmd_risk(etf, price)
    elif cmd == "watch":
        interval = 30
        if len(args) >= 3:
            try:
                interval = int(args[2])
            except ValueError:
                pass
        cmd_watch(etf, interval)
    elif cmd == "perf":
        cmd_performance(etf)
    elif cmd == "tune":
        cmd_tune(etf)
    elif cmd == "trend":
        cmd_trend(etf)
    elif cmd == "backtest":
        # 解析回测参数
        bt_start = None
        bt_end = None
        bt_capital = None
        bt_spacing = None
        bt_levels = None
        bt_shares = None
        bt_json = False
        i = 1
        while i < len(args):
            if args[i] == "--start" and i + 1 < len(args):
                bt_start = args[i + 1]
                i += 2
            elif args[i] == "--end" and i + 1 < len(args):
                bt_end = args[i + 1]
                i += 2
            elif args[i] == "--capital" and i + 1 < len(args):
                bt_capital = int(args[i + 1])
                i += 2
            elif args[i] == "--spacing" and i + 1 < len(args):
                bt_spacing = float(args[i + 1])
                i += 2
            elif args[i] == "--levels" and i + 1 < len(args):
                bt_levels = int(args[i + 1])
                i += 2
            elif args[i] == "--shares" and i + 1 < len(args):
                bt_shares = int(args[i + 1])
                i += 2
            elif args[i] == "--json":
                bt_json = True
                i += 1
            else:
                i += 1
        cmd_backtest(etf, bt_start, bt_end, bt_capital, bt_spacing, bt_levels, bt_shares,
                     output_json=bt_json)
    else:
        print(f"未知命令: {cmd}")
        print_help()


if __name__ == "__main__":
    main()
