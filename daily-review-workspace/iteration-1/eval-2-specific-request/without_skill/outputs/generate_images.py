#!/usr/bin/env python3
"""Generate Douyin-style market review images for 2026-07-27 A-share market."""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.patches import FancyBboxPatch
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import os
import sys
import textwrap

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Color palette (dark theme, Douyin style) ──
BG_DARK = "#1a1a2e"
BG_CARD = "#16213e"
RED_UP = "#ff4757"
GREEN_UP = "#ff6b81"  # A-share red = up
GREEN_DOWN = "#2ed573"
TEXT_PRIMARY = "#ffffff"
TEXT_SECONDARY = "#a4b0be"
ACCENT_GOLD = "#f9ca24"
ACCENT_BLUE = "#70a1ff"
ACCENT_CYAN = "#7bed9f"

# Set Chinese font
plt.rcParams['font.sans-serif'] = ['PingFang SC', 'Heiti SC', 'STHeiti', 'SimHei', 'Arial Unicode MS']
plt.rcParams['axes.unicode_minus'] = False

# ── Data ──
INDICES = [
    ("上证指数", 3858.25, 1.15),
    ("深证成指", 14148.73, 2.72),
    ("创业板指", 3590.79, 3.16),
    ("沪深300", 4702.43, 1.15),
    ("上证50", 2950.43, -0.20),
    ("中证500", 7720.60, 2.49),
    ("科创50", 1807.95, 1.16),
    ("中证1000", 7228.78, 3.33),
]

SECTORS_UP = [
    ("脑机接口", "🔥"),
    ("PCB/算力", "🔥🔥"),
    ("锂电产业链", "🔥🔥"),
    ("医药医疗", "🔥"),
    ("建筑材料", "🔥"),
    ("半导体", "🔥🔥"),
    ("电力", "🔥"),
    ("人形机器人", "🔥"),
]

SECTORS_DOWN = [
    ("石油天然气", "📉"),
    ("大金融(分化)", "➖"),
]

# Douyin standard sizes
PORTRAIT = (1080, 1920)
SQUARE = (1080, 1080)


def create_font(size):
    """Try to load a Chinese-capable font."""
    font_paths = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for fp in font_paths:
        if os.path.exists(fp):
            return ImageFont.truetype(fp, size)
    return ImageFont.load_default()


# ============================================================
# Image 1: Cover card (portrait 1080x1920)
# ============================================================
def create_cover():
    """Main cover: 7月27日A股市场回顾"""
    W, H = PORTRAIT
    img = Image.new('RGB', (W, H), BG_DARK)
    draw = ImageDraw.Draw(img)

    # Gradient background effect (top)
    for i in range(600):
        r = int(26 + (22 - 26) * i / 600)
        g = int(26 + (17 - 26) * i / 600)
        b = int(46 + (90 - 46) * i / 600)
        draw.rectangle([(0, i), (W, i+1)], fill=(r, g, b))

    font_large = create_font(96)
    font_date = create_font(48)
    font_key = create_font(56)
    font_val = create_font(140)
    font_sub = create_font(44)
    font_small = create_font(36)

    # Date
    draw.text((W//2, 120), "2026年7月27日 周一", fill=TEXT_SECONDARY, font=font_date, anchor="mt")

    # Title
    draw.text((W//2, 220), "A股市场回顾", fill=TEXT_PRIMARY, font=font_large, anchor="mt")

    # Divider line
    draw.rectangle([(140, 340), (W-140, 344)], fill=ACCENT_GOLD)

    # Key numbers - card style
    cards = [
        ("三大指数齐涨", "📈", 200),
        ("5190家上涨", "🟢", 520),
        ("成交2.08万亿", "💰", 840),
    ]
    for text, emoji, y_start in cards:
        # Card background
        card_y = y_start + 280
        draw.rounded_rectangle([(100, y_start), (W-100, card_y)], radius=20, fill=BG_CARD)
        draw.text((W//2, y_start+60), f"{emoji} {text}", fill=TEXT_PRIMARY, font=font_key, anchor="mt")

    # Bottom summary
    summary_y = 1350
    draw.rectangle([(100, summary_y), (W-100, summary_y+4)], fill=ACCENT_GOLD)
    draw.text((W//2, summary_y + 60), "创业板指 +3.16%  领涨全市场", fill=ACCENT_GOLD, font=font_sub, anchor="mt")
    draw.text((W//2, summary_y + 130), "中证1000 +3.33%  中小盘爆发", fill=ACCENT_GOLD, font=font_sub, anchor="mt")

    # Footer
    draw.text((W//2, H-180), "长鑫科技上市首日涨超500% · 成交1400亿创历史", fill=TEXT_SECONDARY, font=font_small, anchor="mt")
    draw.text((W//2, H-120), "数据来源: 腾讯行情/东方财富 | 仅供参考 不构成投资建议", fill=TEXT_SECONDARY, font=create_font(28), anchor="mt")
    draw.text((W//2, H-60), "#A股 #财经 #市场回顾 #投资", fill=TEXT_SECONDARY, font=create_font(28), anchor="mt")

    fp = os.path.join(OUTPUT_DIR, "douyin_01_cover.png")
    img.save(fp)
    print(f"Saved: {fp}")
    return fp


# ============================================================
# Image 2: Index performance bar chart (square 1080x1080)
# ============================================================
def create_index_chart():
    """Horizontal bar chart of index changes."""
    W, H = SQUARE
    fig, ax = plt.subplots(figsize=(9, 9), facecolor=BG_DARK)
    ax.set_facecolor(BG_DARK)

    names = [i[0] for i in reversed(INDICES)]
    changes = [i[2] for i in reversed(INDICES)]
    prices = [i[1] for i in reversed(INDICES)]

    colors = [RED_UP if c >= 0 else GREEN_DOWN for c in changes]
    bars = ax.barh(names, changes, color=colors, height=0.6, edgecolor='none')

    # Add value labels and price annotations
    for i, (bar, chg, price) in enumerate(zip(bars, changes, prices)):
        sign = "+" if chg >= 0 else ""
        label = f"{sign}{chg:.2f}%"
        ax.text(chg + 0.15 if chg >= 0 else chg - 0.15, bar.get_y() + bar.get_height()/2,
                f"{label}  ({price})", va='center',
                fontsize=13, color='white' if chg >= 0 else GREEN_DOWN,
                fontweight='bold')

    ax.axvline(x=0, color='#555', linewidth=1)
    ax.set_xlim(-1.5, 5.5)
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=12)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('#333')
    ax.spines['bottom'].set_color('#333')
    ax.xaxis.label.set_color(TEXT_SECONDARY)

    # Title
    ax.set_title("A股主要指数涨跌幅 (2026-07-27)", color=TEXT_PRIMARY, fontsize=20, fontweight='bold', pad=20)
    ax.set_xlabel("涨跌幅 (%)", color=TEXT_SECONDARY, fontsize=12)

    # Legend
    from matplotlib.patches import Patch
    legend_elements = [Patch(facecolor=RED_UP, label='上涨'),
                       Patch(facecolor=GREEN_DOWN, label='下跌')]
    ax.legend(handles=legend_elements, loc='lower right', facecolor=BG_CARD, edgecolor='#333',
              labelcolor=TEXT_PRIMARY, fontsize=10)

    plt.tight_layout()
    fp = os.path.join(OUTPUT_DIR, "douyin_02_index_chart.png")
    fig.savefig(fp, dpi=120, facecolor=BG_DARK, bbox_inches='tight')
    plt.close()
    print(f"Saved: {fp}")
    return fp


# ============================================================
# Image 3: Key highlights card (portrait 1080x1920)
# ============================================================
def create_highlights():
    """Today's key highlights: biggest gainers, champion stock, catalyst."""
    W, H = PORTRAIT
    img = Image.new('RGB', (W, H), BG_DARK)
    draw = ImageDraw.Draw(img)

    font_title = create_font(72)
    font_subtitle = create_font(48)
    font_body = create_font(40)
    font_num = create_font(120)
    font_big_num = create_font(160)
    font_small = create_font(32)

    # Title
    draw.text((W//2, 100), "今日焦点", fill=TEXT_PRIMARY, font=font_title, anchor="mt")
    draw.rectangle([(140, 180), (W-140, 184)], fill=ACCENT_GOLD)

    # ── Card 1: 长鑫科技 ──
    y1 = 240
    draw.rounded_rectangle([(60, y1), (W-60, y1+400)], radius=24, fill=BG_CARD)
    draw.text((W//2, y1+40), "长鑫科技 (688825) 科创板上市首日", fill=ACCENT_GOLD, font=font_subtitle, anchor="mt")
    draw.text((W//2, y1+130), "涨超 500%", fill=RED_UP, font=font_big_num, anchor="mt")
    draw.text((W//2, y1+270), "总市值突破 3.3 万亿", fill=TEXT_PRIMARY, font=font_subtitle, anchor="mt")
    draw.text((W//2, y1+340), "超越工商银行 · 登顶A股市值一哥", fill=TEXT_SECONDARY, font=font_body, anchor="mt")

    # ── Card 2: 成交额纪录 ──
    y2 = y1 + 430
    draw.rounded_rectangle([(60, y2), (W-60, y2+280)], radius=24, fill=BG_CARD)
    draw.text((W//2, y2+50), "单日成交超 1400 亿元", fill=TEXT_PRIMARY, font=font_subtitle, anchor="mt")
    draw.text((W//2, y2+140), "⏱ A股史上首只单日成交破千亿个股", fill=ACCENT_CYAN, font=font_body, anchor="mt")
    draw.text((W//2, y2+210), "多家券商交易系统一度卡顿", fill=TEXT_SECONDARY, font=font_small, anchor="mt")

    # ── Card 3: 宁德时代 ──
    y3 = y2 + 310
    draw.rounded_rectangle([(60, y3), (W-60, y3+280)], radius=24, fill=BG_CARD)
    draw.text((W//2, y3+50), "宁德时代 中报预增 42%", fill=TEXT_PRIMARY, font=font_subtitle, anchor="mt")
    draw.text((W//2, y3+140), "拟 200-400亿 回购股份", fill=ACCENT_GOLD, font=font_subtitle, anchor="mt")
    draw.text((W//2, y3+210), "创A股单次回购金额历史新高", fill=TEXT_SECONDARY, font=font_small, anchor="mt")

    # ── Card 4: 上涨催化剂 ──
    y4 = y3 + 310
    draw.rounded_rectangle([(60, y4), (W-60, y4+280)], radius=24, fill=BG_CARD)
    draw.text((W//2, y4+40), "午后拉升催化剂", fill=ACCENT_BLUE, font=font_subtitle, anchor="mt")
    draw.text((W//2, y4+110), "国际油价暴跌 WTI一度跌6.1%", fill=TEXT_PRIMARY, font=font_body, anchor="mt")
    draw.text((W//2, y4+170), "伊朗释放缓和信号 → 通胀预期降温", fill=TEXT_SECONDARY, font=font_body, anchor="mt")
    draw.text((W//2, y4+230), "A股午后全面拉升", fill=RED_UP, font=font_body, anchor="mt")

    # Footer
    draw.text((W//2, H-120), "数据来源: 腾讯行情/东方财富 | 仅供参考 不构成投资建议", fill=TEXT_SECONDARY, font=create_font(28), anchor="mt")
    draw.text((W//2, H-60), "#长鑫科技 #A股 #今日焦点", fill=TEXT_SECONDARY, font=create_font(28), anchor="mt")

    fp = os.path.join(OUTPUT_DIR, "douyin_03_highlights.png")
    img.save(fp)
    print(f"Saved: {fp}")
    return fp


# ============================================================
# Image 4: Sector hot map (portrait 1080x1920)
# ============================================================
def create_sectors():
    """Sector performance card."""
    W, H = PORTRAIT
    img = Image.new('RGB', (W, H), BG_DARK)
    draw = ImageDraw.Draw(img)

    font_title = create_font(72)
    font_sub = create_font(44)
    font_item = create_font(40)
    font_small = create_font(28)

    draw.text((W//2, 100), "板块热力图", fill=TEXT_PRIMARY, font=font_title, anchor="mt")
    draw.rectangle([(140, 180), (W-140, 184)], fill=ACCENT_GOLD)

    # 🔥 Hot sectors
    y_start = 240
    draw.text((100, y_start), "🔥 涨幅居前", fill=RED_UP, font=font_sub)
    y = y_start + 70
    for i, (name, fire) in enumerate(SECTORS_UP):
        col = i % 2
        row = i // 2
        x = 80 + col * 490
        yy = y + row * 130
        card_w = 440
        card_h = 110
        draw.rounded_rectangle([(x, yy), (x+card_w, yy+card_h)], radius=16, fill=BG_CARD)
        draw.text((x+30, yy+30), f"{fire} {name}", fill=TEXT_PRIMARY, font=font_item)
        # Sub-label
        subs = {
            "脑机接口": "多股20CM涨停",
            "PCB/算力": "国际复材涨停",
            "锂电产业链": "宁德时代催化",
            "医药医疗": "哈三联4天3板",
            "建筑材料": "+5.44% 6股涨停",
            "半导体": "主力净流入721亿",
            "电力": "立新能源8天7板",
            "人形机器人": "明新旭腾涨停",
        }
        draw.text((x+30, yy+65), subs.get(name, ""), fill=TEXT_SECONDARY, font=font_small)

    # 📉 Down sectors
    y_down = y + 4 * 130 + 50
    draw.text((100, y_down), "📉 逆势方向", fill=GREEN_DOWN, font=font_sub)
    y2 = y_down + 70
    for i, (name, sign) in enumerate(SECTORS_DOWN):
        x = 80 + i * 490
        draw.rounded_rectangle([(x, y2), (x+440, y2+110)], radius=16, fill=BG_CARD)
        draw.text((x+30, y2+30), f"{sign} {name}", fill=TEXT_PRIMARY, font=font_item)
        subs2 = {
            "石油天然气": "国际油价暴跌",
            "大金融(分化)": "上证50微跌0.20%",
        }
        draw.text((x+30, y2+65), subs2.get(name, ""), fill=TEXT_SECONDARY, font=font_small)

    # Key data cards at bottom
    y_stats = y2 + 170
    stats = [
        ("5190家", "上涨", RED_UP),
        ("286家", "下跌", GREEN_DOWN),
        ("100+家", "涨停", RED_UP),
        ("2.08万亿", "成交额", ACCENT_BLUE),
    ]
    for i, (num, label, color) in enumerate(stats):
        x = 60 + i * 250
        draw.rounded_rectangle([(x, y_stats), (x+220, y_stats+160)], radius=16, fill=BG_CARD)
        draw.text((x+110, y_stats+30), num, fill=color, font=create_font(60), anchor="mt")
        draw.text((x+110, y_stats+110), label, fill=TEXT_SECONDARY, font=font_small, anchor="mt")

    # Footer
    draw.text((W//2, H-120), "数据来源: 腾讯行情/东方财富 | 仅供参考 不构成投资建议", fill=TEXT_SECONDARY, font=create_font(28), anchor="mt")
    draw.text((W//2, H-60), "#A股 #板块 #涨停 #热点", fill=TEXT_SECONDARY, font=create_font(28), anchor="mt")

    fp = os.path.join(OUTPUT_DIR, "douyin_04_sectors.png")
    img.save(fp)
    print(f"Saved: {fp}")
    return fp


# ============================================================
# Image 5: Valuation thermometer (square 1080x1080)
# ============================================================
def create_valuation():
    """PE percentile thermometer chart."""
    W, H = SQUARE
    fig, ax = plt.subplots(figsize=(9, 9), facecolor=BG_DARK)
    ax.set_facecolor(BG_DARK)

    index_data = [
        ("科创50", 98.5, "#ff4757"),
        ("沪深300", 86.6, "#ff6b81"),
        ("中证500", 85.3, "#ffa502"),
        ("上证50", 84.9, "#ffa502"),
    ]
    names = [d[0] for d in reversed(index_data)]
    vals = [d[1] for d in reversed(index_data)]
    colors = [d[2] for d in reversed(index_data)]

    bars = ax.barh(names, vals, color=colors, height=0.5, edgecolor='none')

    for bar, val in zip(bars, vals):
        label = "高估" if val > 80 else ("偏贵" if val > 60 else "合理" if val > 30 else "低估")
        ax.text(val + 1.5, bar.get_y() + bar.get_height()/2,
                f"{val:.1f}% ({label})", va='center',
                fontsize=14, color=colors[-1] if val > 80 else 'white',
                fontweight='bold')

    # Reference zones
    ax.axvline(x=30, color=GREEN_DOWN, linewidth=1, linestyle='--', alpha=0.5)
    ax.axvline(x=70, color=ACCENT_GOLD, linewidth=1, linestyle='--', alpha=0.5)

    ax.text(15, -0.5, '低估区\n<30%', color=GREEN_DOWN, fontsize=10, ha='center')
    ax.text(50, -0.5, '合理区\n30-70%', color=ACCENT_GOLD, fontsize=10, ha='center')
    ax.text(85, -0.5, '高估区\n>70%', color=RED_UP, fontsize=10, ha='center')

    ax.set_xlim(0, 105)
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=12)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('#333')
    ax.spines['bottom'].set_color('#333')

    ax.set_title("估值温度计: PE历史分位", color=TEXT_PRIMARY, fontsize=20, fontweight='bold', pad=20)
    ax.set_xlabel("PE 分位 (%)", color=TEXT_SECONDARY, fontsize=12)

    plt.tight_layout()
    fp = os.path.join(OUTPUT_DIR, "douyin_05_valuation.png")
    fig.savefig(fp, dpi=120, facecolor=BG_DARK, bbox_inches='tight')
    plt.close()
    print(f"Saved: {fp}")
    return fp


# ============================================================
# Image 6: Summary info card (square 1080x1080) — text-rich
# ============================================================
def create_summary_card():
    """Text-rich summary suitable for sharing."""
    W, H = SQUARE
    img = Image.new('RGB', (W, H), BG_DARK)
    draw = ImageDraw.Draw(img)

    font_title = create_font(64)
    font_body = create_font(38)
    font_small = create_font(30)

    # Title
    draw.text((W//2, 60), "7月27日 A股收评", fill=TEXT_PRIMARY, font=font_title, anchor="mt")
    draw.rectangle([(140, 130), (W-140, 133)], fill=ACCENT_GOLD)

    # Main points
    points = [
        ("📊", "A股全线大涨，5190家个股飘红", "三大指数集体翻红，创业板指+3.16%领涨"),
        ("🚀", "长鑫科技上市首日，成交破纪录", "涨超500%，成交1400亿，超越工行登顶市值一哥"),
        ("💡", "宁德时代中报预增42%", "拟200-400亿回购，创A股史上最大回购计划"),
        ("⛽", "国际油价暴跌，午后全面拉升", "伊朗缓和信号→通胀预期降温，A股尾盘加速上涨"),
        ("🔥", "半导体主力资金净流入721亿", "脑机接口、PCB、锂电、医药多点开花"),
        ("⚠️", "石油天然气板块逆势重挫", "中曼石油、通源石油跌停，布油一度跌5.61%"),
    ]

    y = 180
    for emoji, title, desc in points:
        draw.rounded_rectangle([(60, y), (W-60, y+140)], radius=20, fill=BG_CARD)
        draw.text((100, y+20), f"{emoji} {title}", fill=TEXT_PRIMARY, font=font_body)
        draw.text((100, y+75), desc, fill=TEXT_SECONDARY, font=font_small)
        y += 165

    # Footer
    draw.text((W//2, H-100), "数据来源: 腾讯行情/东方财富/公开新闻", fill=TEXT_SECONDARY, font=font_small, anchor="mt")
    draw.text((W//2, H-50), "仅供参考 · 不构成投资建议", fill=TEXT_SECONDARY, font=create_font(26), anchor="mt")

    fp = os.path.join(OUTPUT_DIR, "douyin_06_summary.png")
    img.save(fp)
    print(f"Saved: {fp}")
    return fp


# ============================================================
# Main
# ============================================================
if __name__ == '__main__':
    print("Generating Douyin market review images...")
    print(f"Output directory: {OUTPUT_DIR}")

    files = []
    files.append(create_cover())
    files.append(create_index_chart())
    files.append(create_highlights())
    files.append(create_sectors())
    files.append(create_valuation())
    files.append(create_summary_card())

    print(f"\nDone! Generated {len(files)} images:")
    for f in files:
        size_kb = os.path.getsize(f) / 1024
        print(f"  {os.path.basename(f)} ({size_kb:.0f} KB)")
