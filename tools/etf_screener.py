#!/usr/bin/env python3
"""
全市场 ETF 筛选工具 — 动量轮动策略选品

筛选标准（四维评分，每维 0-5 分，总分 20）:
  1. 长期方向 (0-5): 底层资产是否长期向上（看 3-5 年 CAGR）
  2. 流动性 (0-5): 日均成交额是否足够（百万级）
  3. 独立性 (0-5): 与其他 ETF 的相关性是否低（分散化价值）
  4. 波动率 (0-5): 是否有足够的波动供动量策略捕捉（年化 15-35% 最优）

数据源: 腾讯K线 + 腾讯行情（零限流）
用法: python3 tools/etf_screener.py
"""

import json, math, os, subprocess, sys, time
from datetime import datetime

_TIMEOUT = 15
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "cache")

# ==============================================================================
# 候选 ETF 池（全市场代表性品种，按类别分组）
# ==============================================================================

CANDIDATES = {
    # 宽基 A 股
    "510300": ("沪深300ETF", "宽基大盘"),
    "510500": ("中证500ETF", "宽基中盘"),
    "159915": ("创业板ETF", "宽基成长"),
    "588000": ("科创50ETF", "宽基科创"),
    "510050": ("上证50ETF", "宽基超大盘"),
    "159845": ("中证1000ETF", "宽基小盘"),
    # 跨境
    "513180": ("恒生科技ETF", "港股科技"),
    "513100": ("纳指ETF", "美股科技"),
    "159920": ("恒生ETF", "港股大盘"),
    "513500": ("标普500ETF", "美股大盘"),
    # 行业 A 股
    "512880": ("证券ETF", "行业金融"),
    "512760": ("芯片ETF", "行业科技"),
    "515030": ("新能源车ETF", "行业新能源"),
    "512010": ("医药ETF", "行业医药"),
    "512690": ("酒ETF", "行业消费"),
    "516510": ("云计算ETF", "行业科技"),
    "512660": ("军工ETF", "行业军工"),
    # 防御/另类
    "518880": ("黄金ETF", "另类贵金属"),
    "511010": ("国债ETF", "防御债券"),
}

# 排除: 低波动货币ETF、规模太小的主题ETF

# ==============================================================================
# 数据获取（复用 momentum_etf_backtest 的逻辑）
# ==============================================================================

def _qq_code(code: str) -> str:
    code = code.strip().replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    if code.startswith(("6", "9", "5")):
        return "sh" + code
    elif code.startswith(("0", "3", "2", "1")):
        return "sz" + code
    elif code.startswith(("4", "8")):
        return "bj" + code
    return "sh" + code


def _curl_json(url):
    result = subprocess.run(
        ["/usr/bin/curl", "-s", "--noproxy", "*",
         "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
         url],
        capture_output=True, timeout=_TIMEOUT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ConnectionError(f"请求失败: {url}")
    raw = result.stdout
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gbk")
    return json.loads(text)


def fetch_kline(code: str, count: int = 800) -> list:
    """从腾讯获取ETF日K线，带缓存。"""
    cache_path = os.path.join(_CACHE_DIR, f"etf_kline_{code}_qfq_{count}.json")
    if os.path.exists(cache_path):
        age_hours = (time.time() - os.path.getmtime(cache_path)) / 3600
        if age_hours < 6:
            try:
                with open(cache_path) as f:
                    cached = json.load(f)
                if cached.get("code") == code and cached.get("data"):
                    return cached["data"]
            except Exception:
                pass

    qq = _qq_code(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={qq},day,,,{count},qfq"
    resp = _curl_json(url)
    data = resp.get("data", {}).get(qq, {})
    klines = data.get("qfqday") or data.get("day") or []

    result = []
    for row in klines:
        if len(row) >= 6:
            result.append({
                "date": str(row[0]), "open": float(row[1]),
                "close": float(row[2]), "high": float(row[3]),
                "low": float(row[4]), "volume": float(row[5]),
            })
    if result:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        try:
            with open(cache_path, "w") as f:
                json.dump({"code": code, "count": len(result), "data": result}, f, ensure_ascii=False)
        except Exception:
            pass
    return result


def fetch_quote(code: str) -> dict:
    """获取实时行情（腾讯）。"""
    qq = _qq_code(code)
    result = subprocess.run(
        ["/usr/bin/curl", "-s", "--noproxy", "*",
         "-H", "User-Agent: Mozilla/5.0",
         f"https://qt.gtimg.cn/q={qq}"],
        capture_output=True, timeout=_TIMEOUT,
    )
    raw = result.stdout
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gbk")
    start = text.find('"')
    end = text.rfind('"')
    if start < 0 or end <= start:
        return {}
    fields = text[start+1:end].split("~")
    if len(fields) < 40:
        return {}
    return {
        "name": fields[1], "price": float(fields[3] or 0),
        "volume_shou": float(fields[6] or 0),
        "turnover_amt": float(fields[37] or 0) / 10000,  # 万元→亿元
        "pe": fields[39] if len(fields) > 39 else "-",
    }


# ==============================================================================
# 指标计算
# ==============================================================================

def calc_cagr(klines: list, min_days: int = 250) -> float:
    """年化复合增长率（基于全部可用数据）。"""
    if len(klines) < min_days:
        return None
    closes = [k["close"] for k in klines]
    start, end = closes[0], closes[-1]
    days = len(klines)
    years = days / 252
    if years < 0.5:
        return None
    return (end / start) ** (1 / years) - 1


def calc_annual_vol(klines: list) -> float:
    """年化波动率。"""
    closes = [k["close"] for k in klines]
    if len(closes) < 20:
        return 0
    returns = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            returns.append(math.log(closes[i] / closes[i-1]))
    if len(returns) < 2:
        return 0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(252)


def calc_max_drawdown(klines: list) -> float:
    """最大回撤（百分比）。"""
    closes = [k["close"] for k in klines]
    peak = closes[0]
    max_dd = 0
    for c in closes:
        if c > peak:
            peak = c
        dd = (c - peak) / peak
        if dd < max_dd:
            max_dd = dd
    return abs(max_dd) * 100


def calc_avg_daily_amount(klines: list) -> float:
    """日均成交额（亿元）。"""
    if not klines:
        return 0
    amounts = []
    for k in klines:
        vol = k.get("volume", 0)
        close = k.get("close", 0)
        # volume 是手数，ETF 1手=100份
        amounts.append(vol * 100 * close / 1e8)
    return sum(amounts) / len(amounts) if amounts else 0


def calc_rolling_returns(klines: list, window: int = 60) -> list:
    """滚动窗口收益率列表（用于计算相关性）。"""
    closes = [k["close"] for k in klines]
    result = {}
    for i in range(window, len(closes)):
        ret = (closes[i] - closes[i - window]) / closes[i - window]
        date = klines[i]["date"]
        result[date] = ret
    return result


def calc_correlation(ret1: dict, ret2: dict) -> float:
    """计算两个滚动收益率序列的相关性。"""
    common_dates = sorted(set(ret1.keys()) & set(ret2.keys()))
    if len(common_dates) < 30:
        return 1.0  # 数据不足，保守处理
    x = [ret1[d] for d in common_dates]
    y = [ret2[d] for d in common_dates]
    n = len(x)
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    cov = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n)) / n
    std_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x) / n)
    std_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y) / n)
    if std_x < 1e-10 or std_y < 1e-10:
        return 0
    return cov / (std_x * std_y)


# ==============================================================================
# 评分系统
# ==============================================================================

def score_direction(cagr: float) -> tuple:
    """长期方向评分 (0-5)。"""
    if cagr is None:
        return 1, "数据不足"
    if cagr > 0.15:
        return 5, f"CAGR {cagr*100:.0f}%"
    elif cagr > 0.08:
        return 4, f"CAGR {cagr*100:.0f}%"
    elif cagr > 0.03:
        return 3, f"CAGR {cagr*100:.0f}%"
    elif cagr > 0:
        return 2, f"CAGR {cagr*100:.0f}%"
    elif cagr > -0.05:
        return 1, f"CAGR {cagr*100:.0f}%"
    else:
        return 0, f"CAGR {cagr*100:.0f}%"


def score_liquidity(avg_amount: float) -> tuple:
    """流动性评分 (0-5)。日均成交额（亿元）"""
    if avg_amount > 20:
        return 5, f"日均{avg_amount:.0f}亿"
    elif avg_amount > 10:
        return 4, f"日均{avg_amount:.0f}亿"
    elif avg_amount > 3:
        return 3, f"日均{avg_amount:.0f}亿"
    elif avg_amount > 1:
        return 2, f"日均{avg_amount:.0f}亿"
    elif avg_amount > 0.3:
        return 1, f"日均{avg_amount:.1f}亿"
    else:
        return 0, f"日均{avg_amount:.2f}亿"


def score_volatility(vol: float) -> tuple:
    """波动率评分 (0-5)。年化 15-35% 最优（太高=赌，太低=没空间）"""
    if vol < 0.10:
        return 1, f"年化波动{vol*100:.0f}%（过低）"
    elif vol < 0.15:
        return 2, f"年化波动{vol*100:.0f}%"
    elif vol <= 0.25:
        return 5, f"年化波动{vol*100:.0f}%（最优）"
    elif vol <= 0.35:
        return 4, f"年化波动{vol*100:.0f}%"
    elif vol <= 0.45:
        return 3, f"年化波动{vol*100:.0f}%（偏高）"
    else:
        return 2, f"年化波动{vol*100:.0f}%（过高）"


# ==============================================================================
# 主流程
# ==============================================================================

def main():
    print("=" * 70)
    print("  全市场 ETF 筛选 — 动量轮动策略选品")
    print("=" * 70)
    print(f"  候选池: {len(CANDIDATES)} 只  |  评分维度: 方向/流动性/独立性/波动率")
    print()

    # ── 1. 获取所有候选ETF的K线和行情 ──
    print("[1/3] 获取数据...")
    etf_data = {}
    for code, (name, category) in CANDIDATES.items():
        try:
            klines = fetch_kline(code, count=800)
            quote = fetch_quote(code) if klines else {}
            if klines and len(klines) >= 250:
                cagr = calc_cagr(klines)
                vol = calc_annual_vol(klines)
                max_dd = calc_max_drawdown(klines)
                avg_amt = calc_avg_daily_amount(klines)
                roll_rets = calc_rolling_returns(klines, 60)
                etf_data[code] = {
                    "name": name, "category": category,
                    "klines": klines, "cagr": cagr, "vol": vol,
                    "max_dd": max_dd, "avg_amt": avg_amt,
                    "roll_rets": roll_rets,
                    "price": quote.get("price", 0),
                    "days": len(klines),
                    "first_date": klines[0]["date"],
                    "last_date": klines[-1]["date"],
                }
                print(f"  ✅ {code} {name:<12s} {len(klines):>4d}日  CAGR{cagr*100:+.0f}%  Vol{vol*100:.0f}%  DD{max_dd:.0f}%  日均{avg_amt:.0f}亿")
            else:
                print(f"  ⚠️ {code} {name:<12s} 数据不足（{len(klines)}日），跳过")
        except Exception as e:
            print(f"  ❌ {code} {name:<12s} 获取失败: {e}")

    print(f"\n  有效数据: {len(etf_data)}/{len(CANDIDATES)} 只")

    # ── 2. 计算相关性矩阵 ──
    print(f"\n[2/3] 计算相关性矩阵...")
    codes = sorted(etf_data.keys())
    corr_matrix = {}
    for c1 in codes:
        corr_matrix[c1] = {}
        for c2 in codes:
            if c1 == c2:
                corr_matrix[c1][c2] = 1.0
            elif c2 in corr_matrix and c1 in corr_matrix[c2]:
                corr_matrix[c1][c2] = corr_matrix[c2][c1]
            else:
                corr_matrix[c1][c2] = calc_correlation(
                    etf_data[c1]["roll_rets"], etf_data[c2]["roll_rets"]
                )

    # 计算每个ETF与其他ETF的平均相关性（作为独立性评分的基础）
    avg_corr = {}
    for c in codes:
        corrs = [corr_matrix[c][o] for o in codes if o != c]
        avg_corr[c] = sum(corrs) / len(corrs) if corrs else 1.0

    # ── 3. 评分 ──
    print(f"\n[3/3] 四维评分...\n")

    results = []
    for code in codes:
        d = etf_data[code]
        s_dir, dir_note = score_direction(d["cagr"])
        s_liq, liq_note = score_liquidity(d["avg_amt"])
        s_vol, vol_note = score_volatility(d["vol"])

        # 独立性评分: 平均相关性越低越好
        ac = avg_corr[code]
        if ac < 0.3:
            s_corr = 5
        elif ac < 0.45:
            s_corr = 4
        elif ac < 0.6:
            s_corr = 3
        elif ac < 0.75:
            s_corr = 2
        else:
            s_corr = 1
        corr_note = f"平均相关{ac:.2f}"

        total = s_dir + s_liq + s_corr + s_vol
        results.append({
            "code": code, "name": d["name"], "category": d["category"],
            "s_dir": s_dir, "s_liq": s_liq, "s_corr": s_corr, "s_vol": s_vol,
            "total": total,
            "cagr": d["cagr"], "vol": d["vol"], "max_dd": d["max_dd"],
            "avg_amt": d["avg_amt"], "avg_corr": ac,
            "price": d["price"], "days": d["days"],
            "dir_note": dir_note, "liq_note": liq_note,
            "corr_note": corr_note, "vol_note": vol_note,
        })

    # 按总分排序
    results.sort(key=lambda r: r["total"], reverse=True)

    # ── 输出表格 ──
    print(f"  {'代码':<8s} {'名称':<12s} {'类别':<10s} {'方向':>4s} {'流动':>4s} {'独立':>4s} {'波动':>4s} {'总分':>4s}  CAGR/Vol/DD")
    print(f"  {'-'*75}")
    for r in results:
        print(f"  {r['code']:<8s} {r['name']:<12s} {r['category']:<10s} "
              f"{r['s_dir']:>4d} {r['s_liq']:>4d} {r['s_corr']:>4d} {r['s_vol']:>4d} {r['total']:>4d}  "
              f"{r['cagr']*100:+.0f}%/{r['vol']*100:.0f}%/{r['max_dd']:.0f}%")

    # ── 相关性矩阵 ──
    print(f"\n{'='*70}")
    print(f"  相关性矩阵（60日滚动收益）")
    print(f"{'='*70}")
    # 表头
    header = "  " + "".join(f"{c:>8s}" for c in codes)
    print(header)
    for c1 in codes:
        row = f"  {c1:<6s}"
        for c2 in codes:
            cc = corr_matrix[c1][c2]
            row += f"{cc:>8.2f}"
        print(row)

    # ── Top 5 推荐 ──
    top5 = results[:6]  # 多取1个做备选
    print(f"\n{'='*70}")
    print(f"  🏆 Top 6 候选（按总分排序）")
    print(f"{'='*70}")
    for i, r in enumerate(top5):
        medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣"][i]
        print(f"\n  {medal} {r['code']} {r['name']} [{r['category']}]  — 总分 {r['total']}/20")
        print(f"     方向{r['s_dir']}/5: {r['dir_note']}")
        print(f"     流动{r['s_liq']}/5: {r['liq_note']}")
        print(f"     独立{r['s_corr']}/5: {r['corr_note']}")
        print(f"     波动{r['s_vol']}/5: {r['vol_note']}")

    # ── 组合建议 ──
    print(f"\n{'='*70}")
    print(f"  📋 推荐组合（4-5只）")
    print(f"{'='*70}")

    # 贪心选择：按总分排序，但确保类别多样化
    selected = []
    used_categories = set()
    for r in results:
        if len(selected) >= 5:
            break
        cat = r["category"]
        # 同类别最多选1只（宽基除外，可以选大盘+成长）
        if "宽基" in cat:
            # 宽基可以选2只（大盘+成长各一）
            broad_count = sum(1 for s in selected if "宽基" in s["category"])
            if broad_count >= 2:
                continue
        elif cat in used_categories:
            continue
        selected.append(r)
        used_categories.add(cat)

    print(f"\n  组合构成:")
    for r in selected:
        print(f"    {r['code']} {r['name']:<12s} [{r['category']}]  总分{r['total']}/20  CAGR{r['cagr']*100:+.0f}%  日均{r['avg_amt']:.0f}亿")

    # 计算组合内的平均相关性
    sel_codes = [s["code"] for s in selected]
    sel_corrs = []
    for i, c1 in enumerate(sel_codes):
        for c2 in sel_codes[i+1:]:
            sel_corrs.append(corr_matrix[c1][c2])
    avg_sel_corr = sum(sel_corrs) / len(sel_corrs) if sel_corrs else 0
    print(f"\n  组合内平均相关性: {avg_sel_corr:.2f}")
    if avg_sel_corr < 0.4:
        print(f"  ✅ 相关性低，轮动空间充足")
    elif avg_sel_corr < 0.6:
        print(f"  🟡 相关性中等，轮动有一定空间")
    else:
        print(f"  🔴 相关性偏高，轮动效果可能打折")

    print()


if __name__ == "__main__":
    main()
