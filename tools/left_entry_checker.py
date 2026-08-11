#!/usr/bin/env python3
"""左侧交易买点条件检查器 (独立观察模块) v2.0

检查三重左侧信号（回测验证版）:
  ① 估值: PE/PB 处于近 5 年 P20 分位以下
  ② RSRS 反转: RSRS 从深度负值回升到近零（当前 > -0.1 且 14日前 < -0.3）
  ③ 双底确认: 60交易日内两个低点，第二个不创新低(≥95%)，反弹>5%

回测结论（11 ETF, ~641-2001 日）:
  - ⭐⭐⭐ 全窗口正超额（仅在 512880 证券ETF 触发 6 次）
  - ⭐⭐ 最佳实用信号，频率 3-7%，超额 +1-4pp
  - ⭐ 信号频率过高，不单独使用（作为"不割肉"参考）
  - 适用品种: 高波动 ETF（证券、恒生科技、酒）
  - 不适用: 低波动宽基（沪深300、中证500）、创业板、纳指

用法:
  python3 tools/left_entry_checker.py                    # 全量检查
  python3 tools/left_entry_checker.py --code 159915       # 单只 ETF
  python3 tools/left_entry_checker.py --json              # JSON 输出
  python3 tools/left_entry_checker.py --pool momentum     # 仅动量池
  python3 tools/left_entry_checker.py --pool grid          # 仅网格标的

独立模块 — 不影响 strategy_monitor.py、momentum_signal.py 等现有逻辑。
仅做观察使用，不生成任何交易指令。
"""

import argparse
import json
import math
import os
import subprocess
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# ── 项目根目录 ──────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "tools"))

# ── 复用现有基础设施 ────────────────────────────────────
try:
    from etf_screener import fetch_kline as fetch_etf_kline  # 腾讯K线（带缓存）
except ImportError:
    fetch_etf_kline = None

try:
    from monitor import fetch_batch_quotes  # 腾讯行情批量查询
except ImportError:
    fetch_batch_quotes = None

try:
    from momentum_core import compute_rsrs, DEFAULT_MOMENTUM_PERIOD
except ImportError:
    compute_rsrs = None
    DEFAULT_MOMENTUM_PERIOD = 25


# ══════════════════════════════════════════════════════════
# ETF → 指数映射（用于估值查询）
# ══════════════════════════════════════════════════════════

ETF_INDEX_MAP: dict[str, str] = {
    "512880": "399975",   # 证券ETF → 证券公司
    "159915": "399006",   # 创业板ETF → 创业板指
    "510300": "000300",   # 沪深300ETF → 沪深300
    "513180": "HSTECH",   # 恒生科技ETF → 恒生科技
    "159920": "HSI",      # 恒生ETF → 恒生指数
    "513100": "NDX",      # 纳指ETF → 纳指100
    "518880": None,       # 黄金ETF → 无PE/PB（用 RSRS+情绪替代）
    "512010": "399933",   # 医药ETF → 中证医药
    "512690": "399997",   # 酒ETF → 中证白酒
}

ETF_NAMES: dict[str, str] = {
    "512880": "证券ETF",
    "159915": "创业板ETF",
    "510300": "沪深300ETF",
    "513180": "恒生科技ETF",
    "159920": "恒生ETF",
    "513100": "纳指ETF",
    "518880": "黄金ETF",
    "512010": "医药ETF",
    "512690": "酒ETF",
}

# 关注池定义
MOMENTUM_POOL = ["518880", "513100", "159915", "159920"]
GRID_POOL = ["512880", "159915"]
BASE_POOL = ["510300", "513180"]
CLEANUP_POOL = ["512010", "512690"]
DEFAULT_POOL = MOMENTUM_POOL + ["512880", "510300", "513180", "512010", "512690"]


# ══════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════

@dataclass
class ValuationSignal:
    """估值信号"""
    pe: float | None = None
    pb: float | None = None
    pe_percentile: float | None = None  # 近 5 年 PE 分位, 0-100
    pb_percentile: float | None = None
    dividend_yield: float | None = None
    index_name: str | None = None
    index_price: float | None = None
    error: str | None = None

    @property
    def is_cheap(self) -> bool:
        """PE 或 PB 分位 < 20%"""
        if self.pe_percentile is not None and self.pe_percentile < 20:
            return True
        if self.pb_percentile is not None and self.pb_percentile < 20:
            return True
        return False

    @property
    def is_very_cheap(self) -> bool:
        """PE 或 PB 分位 < 10%"""
        if self.pe_percentile is not None and self.pe_percentile < 10:
            return True
        if self.pb_percentile is not None and self.pb_percentile < 10:
            return True
        return False


@dataclass
class SentimentSignal:
    """情绪/量能信号"""
    vol_5d_avg: float | None = None       # 近 5 日均量（手）
    vol_20d_avg: float | None = None      # 近 20 日均量
    vol_60d_median: float | None = None   # 近 60 日量中位
    vol_ratio_vs_60d: float | None = None # 20日均量 / 60日中位
    vol_shrinking: bool = False           # 成交量持续萎缩
    vol_extreme_low: bool = False         # 地量（< 50% 中位）
    price_5d_pct: float | None = None     # 近 5 日涨跌幅
    price_20d_pct: float | None = None    # 近 20 日涨跌幅
    rsi_14: float | None = None           # RSI(14)
    error: str | None = None

    @property
    def is_sentiment_washed_out(self) -> bool:
        """卖盘衰竭: 地量 + 价格企稳"""
        if not self.vol_extreme_low:
            return False
        # 价格不再大跌（5日跌幅 > -5% 或 RSI 不再新低）
        if self.price_5d_pct is not None and self.price_5d_pct < -8:
            return False
        if self.rsi_14 is not None and self.rsi_14 < 20:
            return False  # RSI 极低说明还在恐慌，未企稳
        return True


@dataclass
class MomentumSignal:
    """RSRS 动量恢复信号"""
    rsrs_score: float | None = None
    slope_annual_pct: float | None = None
    r_squared: float | None = None
    rsrs_7d_ago: float | None = None     # 7 日前 RSRS
    rsrs_14d_ago: float | None = None    # 14 日前 RSRS
    rsrs_trend: str = "unknown"          # "improving" / "worsening" / "stable"
    close: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    error: str | None = None

    @property
    def is_recovering(self) -> bool:
        """RSRS 从底部回升: 当前 > 7日前 > 14日前（虽然可能仍为负值）"""
        if self.rsrs_score is None:
            return False
        if self.rsrs_7d_ago is None or self.rsrs_14d_ago is None:
            return False
        return self.rsrs_score > self.rsrs_7d_ago > self.rsrs_14d_ago

    @property
    def is_stop_deteriorating(self) -> bool:
        """RSRS 停止恶化: 当前 >= 7日前（不再跌）"""
        if self.rsrs_score is None or self.rsrs_7d_ago is None:
            return False
        return self.rsrs_score >= self.rsrs_7d_ago


@dataclass
class LeftEntryCheck:
    """单只 ETF 的左侧检查结果"""
    code: str
    name: str
    valuation: ValuationSignal = field(default_factory=ValuationSignal)
    sentiment: SentimentSignal = field(default_factory=SentimentSignal)
    momentum: MomentumSignal = field(default_factory=MomentumSignal)
    # 综合判定
    score: int = 0                # 0-3（满足几个条件）
    verdict: str = ""             # 文字判定
    details: list[str] = field(default_factory=list)


# ══════════════════════════════════════════════════════════
# 估值数据获取（通过 ashare_data.py index 子进程）
# ══════════════════════════════════════════════════════════

def _fetch_index_valuation(index_code: str) -> ValuationSignal:
    """通过子进程调用 ashare_data.py index 获取指数估值。"""
    vs = ValuationSignal()

    if not index_code:
        vs.error = "无对应指数"
        return vs

    ashare = str(PROJECT_DIR / "tools" / "ashare_data.py")
    try:
        result = subprocess.run(
            [sys.executable, ashare, "index", index_code],
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_DIR),
        )
        if result.returncode != 0:
            vs.error = f"ashare_data.py 返回非零: {result.stderr.strip()[:120]}"
            return vs

        output = result.stdout
        # 解析输出格式:
        # 指数: 沪深300  价格: 4691.61  PE=14.3  PB=1.46
        #   估值: PE分位: 85.4%  |  PB分位: 51.0%  |  股息: 2.54%
        for line in output.split("\n"):
            line = line.strip()
            if line.startswith("指数:"):
                parts = line.split()
                vs.index_name = parts[1] if len(parts) > 1 else index_code
                for part in parts:
                    if part.startswith("价格:"):
                        try:
                            vs.index_price = float(part.split(":", 1)[1])
                        except (ValueError, IndexError):
                            pass
                    elif part.startswith("PE="):
                        try:
                            vs.pe = float(part.split("=", 1)[1])
                        except (ValueError, IndexError):
                            pass
                    elif part.startswith("PB="):
                        try:
                            vs.pb = float(part.split("=", 1)[1])
                        except (ValueError, IndexError):
                            pass
            elif "PE分位:" in line:
                parts = line.split("|")
                for part in parts:
                    part = part.strip()
                    if "PE分位:" in part:
                        try:
                            pct_str = part.split("PE分位:", 1)[1].strip().rstrip("%")
                            vs.pe_percentile = float(pct_str)
                        except (ValueError, IndexError):
                            pass
                    elif "PB分位:" in part:
                        try:
                            pct_str = part.split("PB分位:", 1)[1].strip().rstrip("%")
                            vs.pb_percentile = float(pct_str)
                        except (ValueError, IndexError):
                            pass
                    elif "股息:" in part:
                        try:
                            div_str = part.split("股息:", 1)[1].strip().rstrip("%")
                            vs.dividend_yield = float(div_str) / 100
                        except (ValueError, IndexError):
                            pass
    except subprocess.TimeoutExpired:
        vs.error = "ashare_data.py 超时"
    except Exception as e:
        vs.error = str(e)[:200]

    return vs


# ══════════════════════════════════════════════════════════
# K 线数据获取（优先复用现有缓存）
# ══════════════════════════════════════════════════════════

def _fetch_klines(code: str, count: int = 400) -> list[dict]:
    """获取 ETF K 线，优先复用 etf_screener 缓存，兜底直连腾讯 API。"""
    klines = []

    # 路径 1: etf_screener.fetch_kline（腾讯 API + 6h 缓存）
    if fetch_etf_kline:
        try:
            klines = fetch_etf_kline(code, count=count)
            if klines and len(klines) >= 100:
                return klines
        except Exception:
            pass

    # 路径 2: 直接读缓存文件（etf_screener 的缓存目录）
    cache_dir = PROJECT_DIR / "data" / "etf_kline_cache"
    cache_file = cache_dir / f"etf_kline_{code}_qfq_{count}.json"
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                cached = json.load(f)
            if cached.get("data") and len(cached["data"]) >= 100:
                return cached["data"]
        except Exception:
            pass

    # 路径 3: 直连腾讯 API
    klines = _fetch_tencent_kline(code, count)
    if klines and len(klines) >= 100:
        return klines

    return []


def _fetch_tencent_kline(code: str, count: int = 400) -> list[dict]:
    """直连腾讯 K 线 API。"""
    qq = _qq_code(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={qq},day,,,{count},qfq"
    try:
        result = subprocess.run(
            ["/usr/bin/curl", "-s", "--noproxy", "*",
             "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
             url],
            capture_output=True, timeout=15,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        raw = result.stdout
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk")
        resp = json.loads(text)
        data = resp.get("data", {}).get(qq, {})
        rows = data.get("qfqday") or data.get("day") or []
        klines = []
        for row in rows:
            if len(row) >= 6:
                klines.append({
                    "date": str(row[0]), "open": float(row[1]),
                    "close": float(row[2]), "high": float(row[3]),
                    "low": float(row[4]), "volume": float(row[5]),
                })
        return klines
    except Exception:
        return []


def _qq_code(code: str) -> str:
    """ETF 代码 → 腾讯行情前缀。"""
    code = code.strip().replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    if code.startswith(("6", "9", "5")):
        return "sh" + code
    elif code.startswith(("0", "3", "2", "1")):
        return "sz" + code
    elif code.startswith(("4", "8")):
        return "bj" + code
    return "sh" + code


# ══════════════════════════════════════════════════════════
# 情绪/量能信号计算
# ══════════════════════════════════════════════════════════

def _analyze_sentiment(code: str, klines: list[dict]) -> SentimentSignal:
    """从 K 线计算量能和情绪指标。"""
    ss = SentimentSignal()

    if not klines or len(klines) < 80:
        ss.error = f"K线不足（{len(klines)}根，需要≥80）"
        return ss

    # 成交量序列（手）
    volumes = []
    closes = []
    for k in klines:
        vol = k.get("volume", 0)
        close = k.get("close", 0)
        if vol > 0 and close > 0:
            volumes.append(vol)
            closes.append(close)

    if len(volumes) < 80:
        ss.error = "有效K线不足"
        return ss

    # 近 5 日均量
    ss.vol_5d_avg = sum(volumes[-5:]) / 5
    # 近 20 日均量
    ss.vol_20d_avg = sum(volumes[-20:]) / 20
    # 近 60 日中位量
    recent_60 = sorted(volumes[-60:])
    ss.vol_60d_median = recent_60[len(recent_60) // 2]

    if ss.vol_60d_median > 0:
        ss.vol_ratio_vs_60d = ss.vol_20d_avg / ss.vol_60d_median
        # 地量: 20日均量 < 60日中位的 50%
        if ss.vol_ratio_vs_60d < 0.50:
            ss.vol_extreme_low = True
        # 萎缩: 连续萎缩
        if ss.vol_ratio_vs_60d < 0.65:
            # 检查是否持续萎缩（近5日均量 < 近20日均量 < 60日中位）
            if ss.vol_5d_avg < ss.vol_20d_avg:
                ss.vol_shrinking = True

    # 价格涨跌
    if len(closes) >= 6:
        ss.price_5d_pct = (closes[-1] / closes[-6] - 1) * 100
    if len(closes) >= 21:
        ss.price_20d_pct = (closes[-1] / closes[-21] - 1) * 100

    # RSI(14)
    if len(closes) >= 15:
        gains, losses = [], []
        for i in range(-14, 0):
            diff = closes[i] - closes[i - 1]
            if diff > 0:
                gains.append(diff)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(diff))
        avg_gain = sum(gains) / 14
        avg_loss = sum(losses) / 14
        if avg_loss == 0:
            ss.rsi_14 = 100.0
        elif avg_gain == 0:
            ss.rsi_14 = 0.0
        else:
            rs = avg_gain / avg_loss
            ss.rsi_14 = 100 - (100 / (1 + rs))

    return ss


# ══════════════════════════════════════════════════════════
# RSRS 动量恢复信号计算
# ══════════════════════════════════════════════════════════

def _analyze_rsrs(code: str, klines: list[dict]) -> MomentumSignal:
    """计算 RSRS 当前值 + 历史值（用于判断恢复趋势）。"""
    ms = MomentumSignal()

    if not klines or len(klines) < 120:
        ms.error = f"K线不足（{len(klines)}根，需要≥120）"
        return ms

    closes = [k["close"] for k in klines if k.get("close", 0) > 0]
    if len(closes) < 120:
        ms.error = "有效收盘价不足"
        return ms

    ms.close = closes[-1]

    # 计算 MA
    if len(closes) >= 20:
        ms.ma20 = sum(closes[-20:]) / 20
    if len(closes) >= 60:
        ms.ma60 = sum(closes[-60:]) / 60

    # RSRS: 复用 momentum_core 或自行计算
    momentum_period = DEFAULT_MOMENTUM_PERIOD if compute_rsrs else 25
    rsrs_scores = []

    # 滑动计算近期 RSRS
    for offset in range(0, 21):  # 当前 + 前 20 日
        if len(closes) < momentum_period + offset + 1:
            break
        window_closes = closes[-(momentum_period + offset):(len(closes) - offset) if offset > 0 else None]
        if len(window_closes) < momentum_period:
            break

        try:
            if compute_rsrs:
                score = compute_rsrs(code, window_closes)
                if isinstance(score, (int, float)):
                    rsrs_scores.append(score)
                    continue
            # 备用: 自行计算 OLS
            score = _ols_rsrs(window_closes)
            rsrs_scores.append(score)
        except Exception:
            rsrs_scores.append(None)

    if not rsrs_scores or rsrs_scores[0] is None:
        ms.error = "RSRS 计算失败"
        return ms

    ms.rsrs_score = rsrs_scores[0]
    if len(rsrs_scores) >= 8:
        ms.rsrs_7d_ago = rsrs_scores[7]
    if len(rsrs_scores) >= 15:
        ms.rsrs_14d_ago = rsrs_scores[14]

    # 趋势判断
    if ms.is_recovering:
        ms.rsrs_trend = "improving"
    elif ms.is_stop_deteriorating:
        ms.rsrs_trend = "stabilizing"
    elif ms.rsrs_7d_ago is not None and ms.rsrs_score is not None and ms.rsrs_score < ms.rsrs_7d_ago:
        ms.rsrs_trend = "worsening"
    else:
        ms.rsrs_trend = "stable"

    return ms


def _ols_rsrs(closes: list[float]) -> float | None:
    """简易 OLS RSRS 计算（兜底用）。"""
    import math as _math

    n = len(closes)
    if n < 5:
        return None

    log_closes = [_math.log(c) for c in closes if c > 0]
    if len(log_closes) < n * 0.8:
        return None
    n = len(log_closes)

    x_mean = (n - 1) / 2.0
    y_mean = sum(log_closes) / n

    num = sum((i - x_mean) * (log_closes[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    if den == 0:
        return None

    slope = num / den
    annual_ret = _math.exp(slope * 250) - 1

    # R²
    ss_res = sum((log_closes[i] - (slope * i + (y_mean - slope * x_mean))) ** 2 for i in range(n))
    ss_tot = sum((lc - y_mean) ** 2 for lc in log_closes)
    r2 = max(0.0, 1 - ss_res / ss_tot) if ss_tot > 0 else 0

    return annual_ret * r2


# ══════════════════════════════════════════════════════════
# 综合检查
# ══════════════════════════════════════════════════════════

def check_left_entry(code: str) -> LeftEntryCheck:
    """对单只 ETF 执行完整的三条件左侧检查。"""
    name = ETF_NAMES.get(code, code)
    check = LeftEntryCheck(code=code, name=name)

    # ── 1. 估值 ──
    index_code = ETF_INDEX_MAP.get(code)
    if index_code:
        check.valuation = _fetch_index_valuation(index_code)
    else:
        check.valuation = ValuationSignal(error="无对应指数（商品类ETF，跳过估值）")

    # ── 2. K 线 ──
    klines = _fetch_klines(code)

    # ── 3. 情绪/量能 ──
    check.sentiment = _analyze_sentiment(code, klines)

    # ── 4. RSRS 恢复 ──
    check.momentum = _analyze_rsrs(code, klines)

    # ── 5. 综合评分 ──
    check.score = 0
    check.details = []

    # 条件①: 估值便宜（PE 或 PB 分位 < 20%）
    if check.valuation.is_cheap:
        check.score += 1
        level = "极低" if check.valuation.is_very_cheap else "偏低"
        check.details.append(
            f"✅ 估值{level}: PE分位{check.valuation.pe_percentile:.0f}% | "
            f"PB分位{check.valuation.pb_percentile:.0f}%"
        )
    elif check.valuation.error:
        check.details.append(f"⬜ 估值: {check.valuation.error}")
    else:
        pe_s = f"{check.valuation.pe_percentile:.0f}%" if check.valuation.pe_percentile else "-"
        pb_s = f"{check.valuation.pb_percentile:.0f}%" if check.valuation.pb_percentile else "-"
        check.details.append(f"❌ 估值不便宜: PE分位{pe_s} | PB分位{pb_s}")

    # 条件②: RSRS 反转（v2.0: 从深度负值回升到近零）
    rsrs_current = check.momentum.rsrs_score
    rsrs_14d = check.momentum.rsrs_14d_ago
    is_rsrs_reversal = (rsrs_current is not None and rsrs_14d is not None
                        and rsrs_current > -0.1 and rsrs_14d < -0.3)

    if is_rsrs_reversal:
        check.score += 1
        check.details.append(
            f"✅ RSRS 反转: 当前{rsrs_current:.3f}(>-0.1) | "
            f"14日前{rsrs_14d:.3f}(<-0.3) — 趋势从深度负值回升"
        )
    elif check.momentum.error:
        check.details.append(f"⬜ RSRS: {check.momentum.error}")
    else:
        cur_s = f"{rsrs_current:.3f}" if rsrs_current is not None else "-"
        d14_s = f"{rsrs_14d:.3f}" if rsrs_14d is not None else "-"
        if rsrs_current is not None and rsrs_current > -0.1:
            check.details.append(
                f"🟡 RSRS 近零({cur_s})但14日前不够低({d14_s}) — 不是反转，是平淡"
            )
        elif rsrs_current is not None and rsrs_14d is not None and rsrs_14d < -0.3:
            check.details.append(
                f"🟡 RSRS 14日前深度负值({d14_s})但当前仍低({cur_s}) — 仍在下行"
            )
        else:
            check.details.append(
                f"❌ RSRS 当前{cur_s} | 14日前{d14_s} — 不满足反转条件"
            )

    # 条件③: 双底确认（v2.0 新增，复用已有 K 线数据）
    has_double_bottom, db_detail = _check_double_bottom(klines)
    if has_double_bottom:
        check.score += 1
        check.details.append(f"✅ 双底确认: {db_detail}")
    else:
        check.details.append(f"❌ 双底: {db_detail}")

    # 辅助信息: 量能（仅供参考，不计分）
    if check.sentiment.vol_shrinking:
        check.details.append(
            f"ℹ️ 辅助-量能萎缩: 量比{check.sentiment.vol_ratio_vs_60d:.1%} | "
            f"5日{check.sentiment.price_5d_pct:+.1f}% | RSI={check.sentiment.rsi_14:.0f}"
        )

    # ── 6. 判定 ──
    if check.score == 3:
        check.verdict = "⭐⭐⭐ 三重条件满足 — 回测验证的左侧买点（稀有，超额+2-4pp）"
    elif check.score == 2:
        check.verdict = "⭐⭐ 双重条件满足 — 最佳实用信号（频率3-7%，超额+1-4pp）"
    elif check.score == 1:
        check.verdict = "⭐ 单一条件满足 — 不单独使用，等待第二条件加入"
    else:
        check.verdict = "无左侧条件触发 — 保持现金，耐心等待"

    return check


def _check_double_bottom(klines: list[dict]) -> tuple[bool, str]:
    """检查 60 日内是否有双底不创新低结构。

    返回 (是否双底, 描述信息)。
    """
    if not klines or len(klines) < 80:
        return False, "K线不足"

    closes = [k.get("close", 0) for k in klines if k.get("close", 0) > 0]
    if len(closes) < 80:
        return False, "有效K线不足"

    # 取最近 60 日
    lookback = closes[-60:]

    # 找局部低点（间隔 ≥ 10 日）
    lows = []
    for j in range(10, len(lookback) - 10):
        local_min = True
        for k in range(j - 10, j + 11):
            if k != j and k < len(lookback) and lookback[k] < lookback[j]:
                local_min = False
                break
        if local_min:
            lows.append((j, lookback[j]))

    if len(lows) < 2:
        return False, f"60日内仅{len(lows)}个局部低点，需≥2"

    lows.sort(key=lambda x: x[1])  # 按价格排序
    b1_idx, b1_price = lows[0]     # 最低点

    # 找次低点（在最低点之后）
    b2_candidates = [(idx, p) for idx, p in lows if idx > b1_idx]
    if not b2_candidates and len(lows) >= 3:
        b2_candidates = [(idx, p) for idx, p in lows[1:] if idx > b1_idx]
    if not b2_candidates:
        return False, "最低点之后无第二个局部低点"

    b2_idx, b2_price = min(b2_candidates, key=lambda x: x[1])

    # 检查不创新低
    if b2_price < b1_price * 0.95:
        return False, f"第二个低点(¥{b2_price:.3f})创新低(< ¥{b1_price*0.95:.3f})"

    # 检查当前反弹
    current = closes[-1]
    if current < b1_price * 1.05:
        return False, f"当前价(¥{current:.3f})尚未从底部(¥{b1_price:.3f})反弹>5%"

    rebound_pct = (current / b1_price - 1) * 100
    return True, (f"低点1=¥{b1_price:.3f} → 低点2=¥{b2_price:.3f}"
                  f"(不创新低✓) → 现价=¥{current:.3f}(反弹{rebound_pct:.1f}%)")


# ══════════════════════════════════════════════════════════
# 输出渲染
# ══════════════════════════════════════════════════════════

def _term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except Exception:
        return 100


def render(results: list[LeftEntryCheck], json_out: bool = False):
    """渲染检查结果。"""
    if json_out:
        output = {
            "as_of": date.today().isoformat(),
            "items": [
                {
                    "code": r.code,
                    "name": r.name,
                    "score": r.score,
                    "verdict": r.verdict,
                    "valuation": {
                        "pe": r.valuation.pe,
                        "pb": r.valuation.pb,
                        "pe_percentile": r.valuation.pe_percentile,
                        "pb_percentile": r.valuation.pb_percentile,
                        "is_cheap": r.valuation.is_cheap,
                        "error": r.valuation.error,
                    },
                    "sentiment": {
                        "vol_ratio_vs_60d": r.sentiment.vol_ratio_vs_60d,
                        "vol_extreme_low": r.sentiment.vol_extreme_low,
                        "vol_shrinking": r.sentiment.vol_shrinking,
                        "rsi_14": r.sentiment.rsi_14,
                        "price_5d_pct": r.sentiment.price_5d_pct,
                        "price_20d_pct": r.sentiment.price_20d_pct,
                        "washed_out": r.sentiment.is_sentiment_washed_out,
                        "error": r.sentiment.error,
                    },
                    "momentum": {
                        "rsrs_score": r.momentum.rsrs_score,
                        "rsrs_trend": r.momentum.rsrs_trend,
                        "rsrs_7d_ago": r.momentum.rsrs_7d_ago,
                        "rsrs_14d_ago": r.momentum.rsrs_14d_ago,
                        "is_recovering": r.momentum.is_recovering,
                        "close": r.momentum.close,
                        "ma20": r.momentum.ma20,
                        "ma60": r.momentum.ma60,
                        "error": r.momentum.error,
                    },
                    "details": r.details,
                }
                for r in results
            ],
            "summary": {
                "total": len(results),
                "score_3": sum(1 for r in results if r.score == 3),
                "score_2": sum(1 for r in results if r.score == 2),
                "score_1": sum(1 for r in results if r.score == 1),
                "score_0": sum(1 for r in results if r.score == 0),
            },
        }
        print(json.dumps(output, indent=2, ensure_ascii=False, default=str))
        return

    # ── 终端渲染 ──
    w = _term_width()
    print()
    print("╔" + "═" * (w - 2) + "╗")
    print("║" + "  🔍 左侧交易买点条件检查器 v2.0".ljust(w - 2) + "║")
    print("║" + f"  检查时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  "
          f"标的数: {len(results)}  |  回测验证版".ljust(w - 2) + "║")
    print("╚" + "═" * (w - 2) + "╝")
    print()
    print("  三重条件: ①估值(P20以下) ②RSRS反转(>-0.1且14日前<-0.3) ③双底不创新低")
    print("  辅助参考: 量能萎缩 | ⭐⭐-⭐⭐⭐信号已验证有效(超额+1-4pp)")
    print("  " + "─" * (min(w, 100) - 4))

    for r in results:
        # 得分星星
        stars = "⭐" * r.score + "☆" * (3 - r.score)
        # 颜色标记
        if r.score >= 2:
            tag = "🔔"
        elif r.score == 1:
            tag = "👁 "
        else:
            tag = "  "

        print(f"\n  {tag} [{r.code}] {r.name}  {stars}  →  {r.verdict}")

        for detail in r.details:
            print(f"     {detail}")

        # 补充信息
        extra_parts = []
        if r.momentum.close:
            extra_parts.append(f"现价¥{r.momentum.close:.3f}")
        if r.momentum.ma20:
            above = "↑" if r.momentum.close and r.momentum.close > r.momentum.ma20 else "↓"
            extra_parts.append(f"MA20={r.momentum.ma20:.3f}{above}")
        if r.momentum.ma60:
            above = "↑" if r.momentum.close and r.momentum.close > r.momentum.ma60 else "↓"
            extra_parts.append(f"MA60={r.momentum.ma60:.3f}{above}")
        if r.valuation.pe:
            extra_parts.append(f"PE={r.valuation.pe:.1f}")
        if r.valuation.pb:
            extra_parts.append(f"PB={r.valuation.pb:.2f}")
        if extra_parts:
            print(f"     {' | '.join(extra_parts)}")

    # 汇总
    print()
    print("  " + "─" * (min(w, 100) - 4))
    score3 = sum(1 for r in results if r.score == 3)
    score2 = sum(1 for r in results if r.score == 2)
    score1 = sum(1 for r in results if r.score == 1)
    score0 = sum(1 for r in results if r.score == 0)
    print(f"  汇总: ⭐⭐⭐×{score3}  ⭐⭐×{score2}  ⭐×{score1}  无信号×{score0}")
    if score3 > 0:
        names = [f"[{r.code}]{r.name}" for r in results if r.score == 3]
        print(f"  🔔 左侧买点候选: {', '.join(names)}")
    elif score2 > 0:
        names = [f"[{r.code}]{r.name}" for r in results if r.score == 2]
        print(f"  👁 接近左侧区域: {', '.join(names)}")
    else:
        # 展示哪些条件在改善
        improving = []
        for r in results:
            if r.momentum.is_stop_deteriorating:
                improving.append(f"[{r.code}]RSRS企稳")
            if r.sentiment.vol_shrinking:
                improving.append(f"[{r.code}]量缩")
        if improving:
            print(f"  ℹ️  改善迹象: {', '.join(improving[:8])}")
        else:
            print(f"  ℹ️  全池暂无左侧信号 — 保持现金，耐心等待")
    print()


# ══════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="左侧交易买点条件检查器 — 独立观察模块",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 tools/left_entry_checker.py                     # 全量检查（默认池）
  python3 tools/left_entry_checker.py --code 159915       # 单只 ETF
  python3 tools/left_entry_checker.py --pool momentum     # 仅动量池
  python3 tools/left_entry_checker.py --pool grid          # 仅网格标的
  python3 tools/left_entry_checker.py --json              # JSON 输出
        """,
    )
    parser.add_argument("--code", help="单只 ETF 代码")
    parser.add_argument("--pool", choices=["momentum", "grid", "base", "cleanup", "all"],
                        help="预设池: momentum/grid/base/cleanup/all")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    args = parser.parse_args()

    # 确定检查池
    if args.code:
        codes = [args.code]
    elif args.pool == "momentum":
        codes = MOMENTUM_POOL
    elif args.pool == "grid":
        codes = GRID_POOL
    elif args.pool == "base":
        codes = BASE_POOL
    elif args.pool == "cleanup":
        codes = CLEANUP_POOL
    else:
        codes = DEFAULT_POOL

    # 去重
    seen = set()
    codes = [c for c in codes if not (c in seen or seen.add(c))]

    results = []
    for code in codes:
        result = check_left_entry(code)
        results.append(result)

    render(results, json_out=args.json)


if __name__ == "__main__":
    main()
