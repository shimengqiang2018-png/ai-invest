#!/usr/bin/env python3
"""
抖音复盘视频 — 画面素材生成器
输出到 reports/ETF/video_frames/ 目录
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import os, sys, json, textwrap

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "reports", "ETF", "video_frames")
os.makedirs(OUT, exist_ok=True)

W, H = 1080, 1920  # 9:16 竖屏
BG_COLOR = "#0D1117"
FG_WHITE = "#FFFFFF"
FG_GRAY = "#8B949E"
ACCENT_RED = "#FF4757"
ACCENT_GREEN = "#2ED573"
ACCENT_GOLD = "#FFA502"
ACCENT_BLUE = "#1E90FF"

plt.rcParams.update({
    "figure.facecolor": BG_COLOR,
    "axes.facecolor": BG_COLOR,
    "axes.edgecolor": FG_GRAY,
    "axes.labelcolor": FG_WHITE,
    "axes.titlecolor": FG_WHITE,
    "text.color": FG_WHITE,
    "xtick.color": FG_GRAY,
    "ytick.color": FG_GRAY,
    "grid.color": "#21262D",
    "legend.facecolor": BG_COLOR,
    "legend.edgecolor": FG_GRAY,
    "legend.labelcolor": FG_WHITE,
    "font.sans-serif": ["PingFang SC", "Heiti SC", "Arial Unicode MS", "DejaVu Sans"],
})

# ── 中文字体探测 ──
def _find_cjk_font():
    from matplotlib.font_manager import fontManager
    for f in fontManager.ttflist:
        if "PingFang" in f.name or "Heiti" in f.name or "Songti" in f.name:
            return f.name
    # fallback
    for f in fontManager.ttflist:
        if any(k in f.name for k in ["CJK", "CN", "SC", "Chinese", "Noto Sans"]):
            return f.name
    return None

CJK_FONT = _find_cjk_font()
if CJK_FONT:
    plt.rcParams["font.family"] = CJK_FONT
    print(f"✅ 中文字体: {CJK_FONT}")
else:
    # 尝试 fallback
    plt.rcParams["font.family"] = "sans-serif"
    print("⚠️ 未找到CJK字体，中文可能显示异常")

def save(name):
    path = os.path.join(OUT, name)
    plt.tight_layout(pad=2)
    plt.savefig(path, dpi=120, facecolor=BG_COLOR, bbox_inches="tight")
    plt.close()
    print(f"  ✅ {path}")
    return path

# ══════════════════════════════════════════
# 帧 1: 三大指数收盘数据卡
# ══════════════════════════════════════════
def frame_01_index_cards():
    """标题: 今天 A 股，一只股票救了整个大盘"""
    fig, ax = plt.subplots(figsize=(W/120, H/120))
    ax.set_xlim(0, 1080)
    ax.set_ylim(0, 1920)
    ax.axis("off")

    # 标题
    ax.text(540, 1700, "2026.07.27  A股收盘", fontsize=32, color=FG_GRAY,
            ha="center", va="center")
    ax.text(540, 1580, "一只股票救了整个大盘", fontsize=80, color=FG_WHITE,
            ha="center", va="center", fontweight="bold")

    indices = [
        ("上证指数", "3,858.25", "+1.15%", ACCENT_GREEN),
        ("深证成指", "14,148.73", "+2.72%", ACCENT_GREEN),
        ("创业板指", "3,590.79", "+3.16%", ACCENT_RED),
        ("上证50", "2,950.43", "-0.20%", ACCENT_GREEN),  # 唯一绿
        ("中证1000", "7,228.78", "+3.33%", ACCENT_RED),
    ]

    # 上证50 标特殊色
    colors_map = {"上证50": "#FFA502"}  # 橙色高亮

    y_start = 1350
    for i, (name, price, chg, default_color) in enumerate(indices):
        y = y_start - i * 140
        color = colors_map.get(name, default_color)
        # 名称
        ax.text(120, y, name, fontsize=28, color=FG_GRAY, va="center")
        # 价格
        ax.text(500, y, price, fontsize=36, color=FG_WHITE, va="center", fontweight="bold")
        # 涨跌幅
        ax.text(850, y, chg, fontsize=44, color=color, va="center", fontweight="bold")
        # 分隔线
        if i < len(indices) - 1:
            ax.axhline(y - 60, xmin=0.1, xmax=0.9, color="#21262D", lw=1)

    ax.text(540, 500, "全市场 5,195 只上涨 | 286 只下跌 | 121 只涨停", fontsize=28, color=FG_GRAY, ha="center")
    ax.text(540, 380, "成交 2.08 万亿", fontsize=48, color=ACCENT_GOLD, ha="center", fontweight="bold")

    save("frame_01_index_cards.png")


# ══════════════════════════════════════════
# 帧 2: 长鑫科技数据卡
# ══════════════════════════════════════════
def frame_02_changxin_intro():
    """长鑫科技介绍卡"""
    fig, ax = plt.subplots(figsize=(W/120, H/120))
    ax.set_xlim(0, 1080)
    ax.set_ylim(0, 1920)
    ax.axis("off")

    ax.text(540, 1700, "688825  长鑫科技", fontsize=36, color=FG_GRAY, ha="center")
    ax.text(540, 1550, "国产 DRAM 芯片龙头", fontsize=56, color=ACCENT_BLUE, ha="center", fontweight="bold")
    ax.text(540, 1400, "今日科创板上市", fontsize=40, color=FG_WHITE, ha="center")

    # 核心数据大卡
    items = [
        ("首日涨幅", "+465.82%", ACCENT_RED),
        ("成交额", "1,411.87 亿", ACCENT_GOLD),
        ("市值", "3.28 万亿", ACCENT_BLUE),
    ]
    y = 1150
    for label, val, color in items:
        ax.text(540, y, label, fontsize=24, color=FG_GRAY, ha="center")
        ax.text(540, y - 90, val, fontsize=80, color=color, ha="center", fontweight="bold")
        y -= 220

    ax.text(540, 380, "A股史上首只单日成交破千亿个股", fontsize=30, color=ACCENT_GOLD, ha="center")
    ax.text(540, 280, "上市首日，加冕 A 股市值之王", fontsize=36, color=FG_WHITE, ha="center", fontweight="bold")

    save("frame_02_changxin_intro.png")


# ══════════════════════════════════════════
# 帧 3: 饼图 — 长鑫占全市场 7%
# ══════════════════════════════════════════
def frame_03_pie():
    fig, ax = plt.subplots(figsize=(W/120, H/120))
    ax.set_xlim(0, 1080)
    ax.set_ylim(0, 1920)
    ax.axis("off")

    ax.text(540, 1700, "长鑫占全市场成交比例", fontsize=36, color=FG_GRAY, ha="center")

    # 饼图
    sizes = [7, 93]
    colors_pie = [ACCENT_GOLD, "#21262D"]
    explode = (0.08, 0)
    wedges, texts = ax.pie(sizes, explode=explode, colors=colors_pie,
                            startangle=90, counterclock=False,
                            radius=0.55, center=(540, 1000),
                            wedgeprops={"linewidth": 3, "edgecolor": BG_COLOR})
    ax.text(540, 1000, "7%", fontsize=80, color=ACCENT_GOLD, ha="center", va="center", fontweight="bold")

    ax.text(540, 550, "长鑫科技 · 1,411.87 亿", fontsize=32, color=ACCENT_GOLD, ha="center", fontweight="bold")
    ax.text(540, 450, "其余 5340+ 只股票 · 19,475 亿", fontsize=24, color=FG_GRAY, ha="center")
    ax.text(540, 320, "一只股票，吃掉了全市场 7% 的钱", fontsize=40, color=FG_WHITE, ha="center", fontweight="bold")

    save("frame_03_pie.png")


# ══════════════════════════════════════════
# 帧 4: 市值对比柱状图
# ══════════════════════════════════════════
def frame_04_market_cap_compare():
    fig, ax = plt.subplots(figsize=(W/120, H/120))
    ax.set_xlim(0, 1080)
    ax.set_ylim(0, 1920)
    ax.axis("off")

    ax.text(540, 1750, "市值对比（万亿人民币）", fontsize=36, color=FG_GRAY, ha="center")

    companies = ["长鑫科技", "工商银行", "英特尔", "贵州茅台"]
    values = [3.28, 2.15, 1.85, 1.72]
    colors_bar = [ACCENT_GOLD, "#4A6572", "#5B7F95", "#E8A87C"]

    bar_ax = fig.add_axes([0.18, 0.25, 0.64, 0.55])
    bar_ax.set_facecolor(BG_COLOR)
    bar_ax.spines["top"].set_visible(False)
    bar_ax.spines["right"].set_visible(False)
    bar_ax.spines["left"].set_color(FG_GRAY)
    bar_ax.spines["bottom"].set_color(FG_GRAY)
    bar_ax.tick_params(colors=FG_GRAY, labelsize=22)
    bar_ax.set_xticks(range(len(companies)))
    bar_ax.set_xticklabels(companies, fontsize=26, color=FG_WHITE)
    bar_ax.set_ylabel("万亿人民币", fontsize=22, color=FG_GRAY)

    bars = bar_ax.bar(range(len(companies)), values, color=colors_bar, width=0.6, edgecolor=BG_COLOR, linewidth=2)
    for bar, v in zip(bars, values):
        bar_ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.06,
                    f"{v:.2f}万亿", ha="center", fontsize=30, color=FG_WHITE, fontweight="bold")

    ax.text(540, 320, "一只芯片股 = 两个茅台，比英特尔还大", fontsize=40, color=ACCENT_GOLD, ha="center", fontweight="bold")

    save("frame_04_market_cap.png")


# ══════════════════════════════════════════
# 帧 5: 涨跌比数字卡
# ══════════════════════════════════════════
def frame_05_advance_decline():
    fig, ax = plt.subplots(figsize=(W/120, H/120))
    ax.set_xlim(0, 1080)
    ax.set_ylim(0, 1920)
    ax.axis("off")

    ax.text(540, 1750, "表面上，今天的数据极其漂亮", fontsize=36, color=FG_GRAY, ha="center")

    # 涨跌比大数字
    ax.text(540, 1450, "上涨", fontsize=32, color=FG_GRAY, ha="center")
    ax.text(540, 1320, "5,195", fontsize=140, color=ACCENT_RED, ha="center", fontweight="bold")

    ax.text(540, 1120, "vs", fontsize=36, color=FG_GRAY, ha="center")

    ax.text(540, 980, "下跌", fontsize=32, color=FG_GRAY, ha="center")
    ax.text(540, 850, "286", fontsize=100, color=ACCENT_GREEN, ha="center", fontweight="bold")

    ax.text(540, 680, "涨跌比  18 : 1", fontsize=44, color=ACCENT_GOLD, ha="center", fontweight="bold")

    ax.text(300, 500, "涨停  121 只", fontsize=32, color=ACCENT_RED, ha="center", fontweight="bold")
    ax.text(780, 500, "跌停  6 只", fontsize=32, color=ACCENT_GREEN, ha="center", fontweight="bold")

    ax.text(540, 300, "前日仅 495 只上涨 vs 4,477 只下跌 → 极端反转", fontsize=24, color=FG_GRAY, ha="center")

    save("frame_05_advance_decline.png")


# ══════════════════════════════════════════
# 帧 6: 上证50 逆势下跌对比
# ══════════════════════════════════════════
def frame_06_sz50_divergence():
    fig, ax = plt.subplots(figsize=(W/120, H/120))
    ax.set_xlim(0, 1080)
    ax.set_ylim(0, 1920)
    ax.axis("off")

    ax.text(540, 1800, "但注意一个细节", fontsize=36, color=FG_GRAY, ha="center")

    # 模拟当日走势对比
    categories = ["上证50", "沪深300", "中证500", "中证1000", "创业板指"]
    values_chg = [-0.20, 1.15, 2.49, 3.33, 3.16]
    colors_chg = ["#FFA502" if v < 0 else ACCENT_RED for v in values_chg]

    bar_ax = fig.add_axes([0.15, 0.40, 0.70, 0.40])
    bar_ax.set_facecolor(BG_COLOR)
    bar_ax.spines["top"].set_visible(False)
    bar_ax.spines["right"].set_visible(False)
    bar_ax.spines["left"].set_color(FG_GRAY)
    bar_ax.spines["bottom"].set_color(FG_GRAY)
    bar_ax.tick_params(colors=FG_GRAY, labelsize=20)
    bar_ax.axhline(y=0, color=FG_GRAY, lw=1)
    bar_ax.set_xticks(range(len(categories)))
    bar_ax.set_xticklabels(categories, fontsize=28, color=FG_WHITE)
    bar_ax.set_ylabel("涨跌幅 %", fontsize=20, color=FG_GRAY)

    bars = bar_ax.bar(range(len(categories)), values_chg, color=colors_chg, width=0.6, edgecolor=BG_COLOR, linewidth=2)
    for bar, v in zip(bars, values_chg):
        va = "bottom" if v >= 0 else "top"
        off = 0.15 if v >= 0 else -0.35
        bar_ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + off,
                    f"{v:+.2f}%", ha="center", fontsize=24, color=FG_WHITE, fontweight="bold")

    ax.text(540, 580, "全市场飘红，唯独上证 50 收跌", fontsize=40, color=ACCENT_GOLD, ha="center", fontweight="bold")
    ax.text(540, 460, "50 只最大的股票，没涨。", fontsize=36, color=FG_WHITE, ha="center")
    ax.text(540, 340, "→ 大资金并未跟进", fontsize=32, color=FG_GRAY, ha="center")

    save("frame_06_sz50_divergence.png")


# ══════════════════════════════════════════
# 帧 7: 龙虎榜数据卡
# ══════════════════════════════════════════
def frame_07_dragon_tiger():
    fig, ax = plt.subplots(figsize=(W/120, H/120))
    ax.set_xlim(0, 1080)
    ax.set_ylim(0, 1920)
    ax.axis("off")

    ax.text(540, 1750, "再看龙虎榜", fontsize=40, color=FG_GRAY, ha="center")

    # 关键数据
    items = [
        ("上榜", "53 只", FG_WHITE),
        ("净买入", "29 只", ACCENT_RED),
        ("净卖出", "24 只", ACCENT_GREEN),
        ("总净买入额", "-1.61 亿", ACCENT_GREEN),
        ("买 / 卖比", "0.98", ACCENT_GOLD),
    ]
    y = 1480
    for label, val, color in items:
        ax.text(250, y, label, fontsize=32, color=FG_GRAY, va="center")
        ax.text(750, y, val, fontsize=48, color=color, va="center", fontweight="bold")
        y -= 160

    # 大结论
    ax.text(540, 600, "全榜净卖出", fontsize=56, color=ACCENT_GREEN, ha="center", fontweight="bold")
    ax.text(540, 450, "游资嘴上喊牛市，手上在跑路", fontsize=40, color=FG_WHITE, ha="center")

    # 细项
    ax.text(540, 300, "买方力量 < 卖方力量（比值 < 1 = 卖方市场）", fontsize=24, color=FG_GRAY, ha="center")

    save("frame_07_dragon_tiger.png")


# ══════════════════════════════════════════
# 帧 8: PE 分位仪表盘 — 科创50 98.5%
# ══════════════════════════════════════════
def frame_08_pe_gauge():
    fig, ax = plt.subplots(figsize=(W/120, H/120))
    ax.set_xlim(0, 1080)
    ax.set_ylim(0, 1920)
    ax.axis("off")

    ax.text(540, 1800, "再看估值", fontsize=40, color=FG_GRAY, ha="center")

    # 仪表盘
    gauge_ax = fig.add_axes([0.2, 0.42, 0.60, 0.35])
    gauge_ax.set_facecolor(BG_COLOR)
    gauge_ax.set_xlim(-1.3, 1.3)
    gauge_ax.set_ylim(-0.1, 1.4)
    gauge_ax.axis("off")

    # 半圆仪表
    theta = np.linspace(np.pi, 0, 180)
    r = 1.0
    # 背景弧 (灰)
    gauge_ax.plot(r * np.cos(theta), r * np.sin(theta), color="#21262D", lw=18, solid_capstyle="round")
    # 刻度弧 — 用渐变效果分段
    # 绿区 (0-33%): pi → pi*2/3
    for t_val, color in [
        (np.linspace(np.pi, 2*np.pi/3, 60), ACCENT_GREEN),
        (np.linspace(2*np.pi/3, np.pi/3, 60), ACCENT_GOLD),
        (np.linspace(np.pi/3, 0, 60), ACCENT_RED),
    ]:
        gauge_ax.plot(r * np.cos(t_val), r * np.sin(t_val), color=color, lw=12, alpha=0.3)

    # 指针位置: 98.5% → 角度从 pi 到 0，98.5% = pi * (1-0.985) = pi * 0.015
    needle_angle = np.pi * (1 - 0.985)
    needle_x = r * np.cos(needle_angle)
    needle_y = r * np.sin(needle_angle)
    gauge_ax.annotate("", xy=(needle_x, needle_y), xytext=(0, -0.3),
                      arrowprops=dict(arrowstyle="->", color=FG_WHITE, lw=4))
    gauge_ax.plot(needle_x, needle_y, 'o', color=FG_WHITE, markersize=14)

    # 百分比文字
    gauge_ax.text(0, -0.15, "PE 分位", fontsize=22, color=FG_GRAY, ha="center")
    gauge_ax.text(0, -0.45, "98.5%", fontsize=60, color=ACCENT_RED, ha="center", fontweight="bold")
    gauge_ax.text(0, -0.75, "科创 50 指数", fontsize=24, color=FG_GRAY, ha="center")

    # 标签
    gauge_ax.text(-1.1, -0.15, "0%", fontsize=16, color=FG_GRAY, ha="center")
    gauge_ax.text(1.1, -0.15, "100%", fontsize=16, color=FG_GRAY, ha="center")
    gauge_ax.text(0, 1.05, "PE=217.7", fontsize=20, color=ACCENT_RED, ha="center", fontweight="bold")

    ax.text(540, 600, "历史上只有 1.5% 的时间比现在更贵", fontsize=40, color=FG_WHITE, ha="center", fontweight="bold")
    ax.text(540, 460, "风险溢价极薄", fontsize=32, color=ACCENT_RED, ha="center")

    # 底部估值对比小表
    rows = [
        ("沪深300 PE 分位  86.6%", "上证50 PE 分位  84.9%"),
        ("科创50 PE 分位  98.5%", "中证1000 PB 分位  33.1%"),
        ("恒生科技 PE 分位  27.4%  ← 相对低估", ""),
    ]
    y = 320
    for r1, r2 in rows:
        ax.text(540, y, r1, fontsize=22, color=FG_GRAY, ha="center")
        if r2:
            y -= 38
            ax.text(540, y, r2, fontsize=22, color=FG_GRAY, ha="center")
        y -= 50

    save("frame_08_pe_gauge.png")


# ══════════════════════════════════════════
# 帧 9: 结论卡
# ══════════════════════════════════════════
def frame_09_conclusion():
    fig, ax = plt.subplots(figsize=(W/120, H/120))
    ax.set_xlim(0, 1080)
    ax.set_ylim(0, 1920)
    ax.axis("off")

    ax.text(540, 1700, "所以今天到底是什么？", fontsize=44, color=FG_GRAY, ha="center")

    # 核心结论框
    rect = plt.Rectangle((70, 1000), 940, 500, fill=True, facecolor="#161B22",
                         edgecolor=ACCENT_GOLD, linewidth=3)
    ax.add_patch(rect)

    ax.text(540, 1400, "长鑫上市带来的情绪脉冲", fontsize=48, color=FG_WHITE, ha="center")
    ax.text(540, 1280, "不是趋势反转", fontsize=64, color=ACCENT_RED, ha="center", fontweight="bold")
    ax.text(540, 1120, "IPO 事件驱动 ≠ 基本面改善", fontsize=28, color=FG_GRAY, ha="center")

    # 三个理由
    reasons = [
        ("●", "上证50 逆势下跌，大资金未跟进"),
        ("●", "龙虎榜全榜净卖出，游资在出货"),
        ("●", "扣除长鑫后量能未明显放大"),
    ]
    reason_colors = [ACCENT_RED, ACCENT_GOLD, ACCENT_BLUE]
    y = 850
    for (icon, text), clr in zip(reasons, reason_colors):
        ax.text(200, y, f"{icon}  {text}", fontsize=28, color=clr, va="center")
        y -= 80

    save("frame_09_conclusion.png")


# ══════════════════════════════════════════
# 帧 10: 周二三信号 + 关注
# ══════════════════════════════════════════
def frame_10_signals():
    fig, ax = plt.subplots(figsize=(W/120, H/120))
    ax.set_xlim(0, 1080)
    ax.set_ylim(0, 1920)
    ax.axis("off")

    ax.text(540, 1780, "周二（7/28）盯三个信号", fontsize=44, color=FG_WHITE, ha="center", fontweight="bold")

    signals = [
        ("1", "成交量能不能不靠长鑫\n      维持 2 万亿以上", ACCENT_RED),
        ("2", "上证 50 能不能转涨\n      （大资金是否回归）", ACCENT_GOLD),
        ("3", "北向资金能不能回流", ACCENT_BLUE),
    ]
    y = 1400
    for icon, text, color in signals:
        ax.text(150, y, icon, fontsize=48, color=color, va="top")
        ax.text(240, y, text, fontsize=32, color=FG_WHITE, va="top", linespacing=1.8)
        y -= 220

    # 底线
    rect = plt.Rectangle((120, 650), 840, 100, fill=True, facecolor="#161B22",
                         edgecolor=ACCENT_RED, linewidth=2)
    ax.add_patch(rect)
    ax.text(540, 700, "三缺一，都不算趋势确认", fontsize=40, color=ACCENT_RED, ha="center", va="center", fontweight="bold")

    # 关注
    ax.text(540, 450, "关注我，每天收盘，数据说话", fontsize=44, color=FG_WHITE, ha="center", fontweight="bold")
    ax.text(540, 300, ">>  点赞 · 收藏 · 转发", fontsize=32, color=FG_GRAY, ha="center")

    save("frame_10_signals.png")


# ══════════════════════════════════════════
# Main
# ══════════════════════════════════════════
if __name__ == "__main__":
    print("🎨 生成视频画面素材...")
    print(f"   输出目录: {OUT}\n")
    frame_01_index_cards()
    frame_02_changxin_intro()
    frame_03_pie()
    frame_04_market_cap_compare()
    frame_05_advance_decline()
    frame_06_sz50_divergence()
    frame_07_dragon_tiger()
    frame_08_pe_gauge()
    frame_09_conclusion()
    frame_10_signals()
    print(f"\n✅ 全部素材已生成 → {OUT}")
