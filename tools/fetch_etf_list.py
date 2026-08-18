#!/usr/bin/env python3
"""获取全市场 ETF 静态元信息（上市日期、分类、规模），存储到 data/etf_meta.json。

数据源: 东方财富 push2 API（主源，含上市日期） + 新浪财经（降级方案）
    - 东方财富字段: f12(code), f14(name), f26(listing_date YYYYMMDD), f20(total_mktcap 元)
    - 分类基于名称关键词 + 代码前缀推导

    - 全市场 1553 只 ETF，分页获取约需 3-5 分钟

存储策略:
    只存对回测筛选有用的静态/低频字段。实时价格、涨跌幅等一律不存。
    后续不必再调接口获取这些元信息。
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime

_TIMEOUT = 20
_RETRY_COUNT = 3
_PAGE_INTERVAL = (3.0, 5.0)  # 东方财富限流：每页 3-5s 间隔
_PAGE_SIZE = 100  # 东方财富每页最大 100 条

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
OUTPUT_FILE = os.path.join(DATA_DIR, "etf_meta.json")

# ==============================================================================
# ETF 分类规则（基于名称关键词匹配，按优先级从高到低）
# ==============================================================================

# 货币基金精确匹配（代码前缀 + 名称特征，避免误匹配"汇添富"等基金公司名）
_MONEY_FUND_CODES = {
    "159001", "159003", "159004", "159005",  # 保证金、招商快线等
    "511600", "511620", "511650", "511660", "511670", "511680", "511690",
    "511700", "511770", "511800", "511810", "511820", "511830", "511850",
    "511860", "511880", "511900", "511910", "511920", "511930", "511950",
    "511960", "511970", "511980", "511990",
}
_MONEY_FUND_NAMES = {"保证金", "华宝添益", "银华日利", "理财金", "货币", "现金添富",
                     "招商快线", "添富快钱", "闲钱", "日日鑫", "日鑫",
                     "易方达货币", "博时货币", "华安货币", "南方理财金"}

CATEGORY_RULES = [
    # (category, subcategory, keywords)
    # 货币基金 — 代码精确匹配 + 名称关键词
    ("货币基金", "货币", []),  # 特殊处理，见 _classify_etf
    # 债券
    ("债券ETF", "债券", [
        "债ETF", "债券ETF", "转债ETF", "国债", "地方债", "信用债",
        "国开债", "国开", "地债", "科创债", "政金债", "金融债",
    ]),
    # 商品
    ("商品ETF", "黄金", ["黄金", "金ETF", "上海金"]),
    ("商品ETF", "豆粕", ["豆粕"]),
    ("商品ETF", "有色", ["有色", "矿业"]),
    ("商品ETF", "能源", ["能源", "原油", "油气"]),
    ("商品ETF", "白银", ["白银"]),
    # 杠杆（排除）
    ("杠杆ETF", "杠杆", ["分级", "两倍", "三倍", "2X", "3X"]),
    # 跨境（海外/港股）
    ("跨境ETF", "纳指", ["纳指", "纳斯达克", "纳100"]),
    ("跨境ETF", "美股", ["美股", "美国50", "美国", "道琼斯", "罗素"]),
    ("跨境ETF", "标普", ["标普", "SP"]),
    ("跨境ETF", "港股", ["恒生", "港股通", "H股", "香港", "港股"]),
    ("跨境ETF", "德国", ["德国"]),
    ("跨境ETF", "日经", ["日经", "日本"]),
    ("跨境ETF", "法国", ["法国"]),
    ("跨境ETF", "东南亚", ["印度", "越南", "东南亚", "亚太"]),
    ("跨境ETF", "中概", ["中概", "海外中国", "海外互联网"]),
    ("跨境ETF", "其他", ["跨境", "境外", "全球", "海外", "QD"]),
    # A股宽基
    ("A股宽基", "双创", ["双创"]),
    ("A股宽基", "沪深300", ["沪深300", "HS300"]),
    ("A股宽基", "中证500", ["中证500"]),
    ("A股宽基", "中证1000", ["中证1000"]),
    ("A股宽基", "上证50", ["上证50"]),
    ("A股宽基", "创业板", ["创业板"]),
    ("A股宽基", "科创50", ["科创50"]),
    ("A股宽基", "科创100", ["科创100"]),
    ("A股宽基", "A50", ["A50"]),
    ("A股宽基", "A500", ["A500"]),
    ("A股宽基", "深证100", ["深证100"]),
    ("A股宽基", "科创综指", ["科创综指", "科创板"]),
    ("A股宽基", "北证50", ["北证50"]),
    ("A股宽基", "国证2000", ["国证2000"]),
    ("A股宽基", "中证2000", ["中证2000", "2000增强"]),
    # A股行业/主题
    ("A股行业", "证券", ["证券", "券商"]),
    ("A股行业", "银行", ["银行"]),
    ("A股行业", "保险", ["保险"]),
    ("A股行业", "房地产", ["房地产", "地产"]),
    ("A股行业", "医药", ["医药", "医疗", "生物医药", "中药", "创新药", "医械"]),
    ("A股行业", "消费", ["消费", "食品", "饮料", "酒", "家电", "汽车", "旅游"]),
    ("A股行业", "科技", ["科技", "芯片", "半导体", "集成电路"]),
    ("A股行业", "新能源", ["新能源", "光伏", "电池", "锂电", "储能", "碳中和"]),
    ("A股行业", "军工", ["军工", "国防", "军民融合"]),
    ("A股行业", "农业", ["农业", "畜牧", "农牧", "养殖"]),
    ("A股行业", "基建", ["基建", "建筑", "建材"]),
    ("A股行业", "电力", ["电力", "电网"]),
    ("A股行业", "煤炭", ["煤炭"]),
    ("A股行业", "钢铁", ["钢铁"]),
    ("A股行业", "化工", ["化工"]),
    ("A股行业", "传媒", ["传媒", "影视", "游戏", "动漫"]),
    ("A股行业", "通信", ["通信", "5G", "电信"]),
    ("A股行业", "计算机", ["计算机", "云计算", "大数据", "人工智能", "AI", "软件", "信创"]),
    ("A股行业", "交通运输", ["交通运输", "物流", "航运"]),
    ("A股行业", "红利", ["红利", "股息", "高息"]),
    ("A股行业", "国企", ["央企", "国企", "国企改革"]),
    ("A股行业", "ESG", ["ESG", "绿色"]),
    ("A股行业", "高端制造", ["制造", "机器人", "机床", "工程机械"]),
    ("A股行业", "教育", ["教育"]),
    ("A股行业", "环保", ["环保"]),
    ("A股行业", "现金流", ["现金流"]),
]


def _classify_etf(code: str, name: str) -> tuple[str, str]:
    """根据名称关键词 + 代码前缀推导 ETF 分类。

    Returns:
        (category, subcategory)
    """
    # 货币基金优先：代码精确匹配或名称精确匹配
    if code in _MONEY_FUND_CODES:
        return "货币基金", "货币"
    for mf_name in _MONEY_FUND_NAMES:
        if mf_name in name:
            return "货币基金", "货币"

    # "新能源" 优先于商品能源：避免 "新能源车ETF" 被 "能源" 关键词误判为商品
    if "新能源" in name or "新能车" in name:
        return "A股行业", "新能源"

    for category, subcategory, keywords in CATEGORY_RULES:
        if category == "货币基金":
            continue  # 已在上面处理
        for kw in keywords:
            if kw in name:
                return category, subcategory

    # 代码前缀兜底：REITs（508/180 开头）与 LOF/场内基金（16/50 开头）保持独立分类，
    # 避免被默认塞进 A股行业。
    if code.startswith(("508", "180")):
        return "REITs", "基础设施"
    if code.startswith("16") or code[:3] in {
        "501", "502", "503", "504", "505", "506", "507",
    }:
        return "LOF", "其他"

    # 未匹配到任何关键词 → 默认 A股行业/主题
    return "A股行业", "其他"


def _fetch_page(node: str, page: int, page_size: int = _PAGE_SIZE) -> list[dict]:
    """获取东方财富 ETF 列表单页。

    Args:
        node: 市场板块代码，如 "b:MK0021,b:MK0022,b:MK0023,b:MK0024"
        page: 页码（1-based）
        page_size: 每页条数（最大100）

    Returns:
        list[dict]: 原始 API 响应中的 diff 列表
    """
    params = (
        f"pn={page}&pz={page_size}&po=1&np=1&fltt=2&invt=2"
        f"&fid=f3&fs={node}"
        f"&fields=f12,f14,f20,f21,f26"
    )
    url = f"https://push2.eastmoney.com/api/qt/clist/get?{params}"

    last_error = None
    for attempt in range(_RETRY_COUNT):
        try:
            result = subprocess.run(
                [
                    "/usr/bin/curl", "-sS", "--noproxy", "*",
                    "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    "-H", "Referer: https://quote.eastmoney.com/",
                    "--connect-timeout", "10", "--max-time", str(_TIMEOUT),
                    url,
                ],
                capture_output=True, timeout=_TIMEOUT + 5,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout.decode("utf-8"))
                if data.get("data") and data["data"].get("diff") is not None:
                    return data["data"]["diff"]
                # API 返回了响应但没有 diff 数据
                return []
            # 空响应或 curl 失败
            last_error = ConnectionError(
                f"curl rc={result.returncode}, "
                f"stdout_len={len(result.stdout)}, "
                f"stderr={result.stderr.decode('utf-8', errors='replace')[:200]}"
            )
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as e:
            last_error = e
        if attempt < _RETRY_COUNT - 1:
            time.sleep(2 + random.uniform(0, 2))
    raise ConnectionError(f"东方财富 API 请求失败（已重试）: {last_error}")


def fetch_eastmoney() -> list[dict]:
    """从东方财富 API 获取全市场 ETF 列表。

    Returns:
        list[dict]: 包含 code, name, listing_date, mktcap 的 ETF 列表

    Raises:
        ConnectionError: 无法获取第一页数据时抛出
    """
    # 东方财富 ETF 板块:
    # MK0021: 沪市 ETF
    # MK0022: 深市 ETF
    # MK0023: ?
    # MK0024: ?
    node = "b:MK0021,b:MK0022,b:MK0023,b:MK0024"

    all_items = []
    page = 1
    first_page_failed = False
    while True:
        # 间隔控制，避免触发限流
        delay = random.uniform(*_PAGE_INTERVAL)
        time.sleep(delay)

        try:
            items = _fetch_page(node, page)
        except Exception as e:
            print(f"  ⚠️ 第{page}页请求失败: {e}", file=sys.stderr)
            if page == 1:
                raise  # 第一页失败 → 向上抛出，触发降级
            break  # 后续页失败 → 停止分页，保留已有数据

        if not items:
            # 空页面：可能是正常分页结束，也可能是 API 异常
            if page == 1:
                first_page_failed = True
            break

        # 去重（部分 ETF 可能在多个板块重复出现）
        for item in items:
            code = str(item.get("f12", ""))
            if code and not any(i.get("f12") == code for i in all_items):
                all_items.append(item)

        total = len(all_items)
        print(f"  📄 第{page}页: {len(items)} 条 → 去重后累计 {total}")

        if len(items) < _PAGE_SIZE:
            break
        page += 1

    if first_page_failed and not all_items:
        raise ConnectionError("东方财富 API 返回空数据（可能 IP 被限流）")

    return all_items


def fetch_sina_fallback() -> list[dict]:
    """降级方案：从新浪财经获取 ETF 列表（无上市日期）。"""
    import random as _random

    node = "etf_hq_fund"
    all_items = []
    page = 1
    page_size = 100

    def _curl_json(url: str):
        for attempt in range(_RETRY_COUNT + 1):
            try:
                time.sleep(random.uniform(0.3, 0.6))
                result = subprocess.run(
                    [
                        "/usr/bin/curl", "-s", "--noproxy", "*",
                        "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                        "-H", "Referer: https://finance.sina.com.cn/",
                        "--connect-timeout", "10", "--max-time", "15",
                        url,
                    ],
                    capture_output=True, timeout=20,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return json.loads(result.stdout.decode("utf-8"))
            except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
                pass
            if attempt < _RETRY_COUNT:
                time.sleep(1 + random.uniform(0, 1))
        return None

    while True:
        url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData"
            f"?page={page}&num={page_size}&sort=symbol&asc=1&node={node}&_s_r_a=init"
        )
        data = _curl_json(url)
        if not isinstance(data, list) or not data:
            break
        all_items.extend(data)
        print(f"  📄 新浪 第{page}页: {len(data)} 条 (累计 {len(all_items)})")
        if len(data) < page_size:
            break
        page += 1

    return all_items


def _to_date(ymd_int) -> str | None:
    """将 YYYYMMDD 整数转为 YYYY-MM-DD 字符串。"""
    if not ymd_int or ymd_int == 0:
        return None
    s = str(int(ymd_int))
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def _try_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _derive_market(code: str) -> str:
    """根据代码前缀推导市场。"""
    if code.startswith(("5", "6", "9")):
        return "SH"
    elif code.startswith(("0", "2", "3")):
        return "SZ"
    elif code.startswith("8"):
        return "BJ"
    return "??"


def main():
    print("=" * 60)
    print("获取全市场 ETF 静态元信息")
    print("=" * 60)

    os.makedirs(DATA_DIR, exist_ok=True)

    # ── 主数据源：东方财富 ──
    use_sina_fallback = False
    raw_items = []
    total_from_api = 0

    # ── 主数据源：东方财富（优先获取上市日期）──
    eastmoney_failed = False
    try:
        print("\n📡 数据源: 东方财富 push2 API")
        print("   (每页间隔 3-5s，预计 3-5 分钟)...")
        raw_items = fetch_eastmoney()
        total_from_api = len(raw_items)
    except Exception as e:
        print(f"\n  ❌ 东方财富 API 不可用: {e}")
        eastmoney_failed = True

    # ── 降级：新浪财经 ──
    if eastmoney_failed or not raw_items:
        if eastmoney_failed:
            print("  ⬇️  降级到新浪财经（无上市日期，后续从 K 线数据推导）...")
        use_sina_fallback = True
        try:
            raw_items = fetch_sina_fallback()
            total_from_api = len(raw_items)
        except Exception as e2:
            print(f"  ❌ 新浪财经也失败了: {e2}")
            sys.exit(1)

    if not raw_items:
        print("  ❌ 未获取到任何 ETF 数据")
        sys.exit(1)

    print(f"\n📊 共获取 {total_from_api} 条原始数据")

    # ── 处理 & 去重 ──
    seen: dict[str, dict] = {}
    for item in raw_items:
        if use_sina_fallback:
            # 新浪字段映射
            code = str(item.get("code", ""))
            if not code or code in seen:
                continue
            name = item.get("name", "")
            market = (
                "SH" if item.get("symbol", "").startswith("sh") else
                "SZ" if item.get("symbol", "").startswith("sz") else
                "BJ" if item.get("symbol", "").startswith("bj") else
                _derive_market(code)
            )
            mktcap = _try_float(item.get("mktcap"))  # 新浪 mktcap 单位: 万元
            if mktcap:
                mktcap = mktcap * 10000  # 转元，与东方财富对齐
            seen[code] = {
                "code": code,
                "name": name,
                "market": market,
                "listing_date": None,  # 新浪无上市日期
                "mktcap": mktcap,
            }
        else:
            # 东方财富字段映射
            code = str(item.get("f12", ""))
            if not code or code in seen:
                continue
            name = item.get("f14", "")
            listing_date = _to_date(item.get("f26"))
            mktcap = _try_float(item.get("f20"))  # 单位: 元
            seen[code] = {
                "code": code,
                "name": name,
                "market": _derive_market(code),
                "listing_date": listing_date,
                "mktcap": mktcap,
            }

    # ── 组装输出 ──
    etf_list = []
    for code in sorted(seen):
        item = seen[code]
        category, subcategory = _classify_etf(code, item["name"])
        mktcap = item.get("mktcap")
        etf_list.append({
            "code": code,
            "name": item["name"],
            "market": item["market"],
            "listing_date": item.get("listing_date"),
            "category": category,
            "subcategory": subcategory,
            "fund_size": round(mktcap, 2) if mktcap else None,
        })

    # ── 写文件 ──
    output = {
        "version": 1,
        "fetched_at": datetime.now().isoformat(),
        "data_source": "新浪财经 vip.stock.finance.sina.com.cn" if use_sina_fallback
                       else "东方财富 push2.eastmoney.com",
        "listing_date_source": "derived_from_kline" if use_sina_fallback else "eastmoney",
        "total": len(etf_list),
        "etfs": etf_list,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False)

    file_size = os.path.getsize(OUTPUT_FILE)
    print(f"\n✅ 已保存 {len(etf_list)} 只 ETF 元信息到 {OUTPUT_FILE}")
    print(f"   文件大小: {file_size / 1024:.1f} KB")

    # ── 统计 ──
    sh_count = sum(1 for e in etf_list if e["market"] == "SH")
    sz_count = sum(1 for e in etf_list if e["market"] == "SZ")
    bj_count = sum(1 for e in etf_list if e["market"] == "BJ")
    has_listing_date = sum(1 for e in etf_list if e["listing_date"])
    print(f"   沪市: {sh_count} | 深市: {sz_count} | 北交所: {bj_count}")
    print(f"   有上市日期: {has_listing_date}/{len(etf_list)}")

    # 分类统计
    cat_counts: dict[str, int] = {}
    for e in etf_list:
        cat = e["category"]
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    print("\n📋 分类统计:")
    for cat in sorted(cat_counts, key=cat_counts.get, reverse=True):
        print(f"   {cat}: {cat_counts[cat]} 只")

    # 展示前 10 只
    print("\n📋 样本（前10只）:")
    for e in etf_list[:10]:
        date_str = e["listing_date"] or "未知"
        size_str = f"{e['fund_size']/1e8:.1f}亿" if e["fund_size"] else "-"
        print(f"  {e['code']}  {e['name']:<20s}  {e['market']}  {e['category']}/{e['subcategory']:<8s}  "
              f"上市:{date_str}  规模:{size_str}")


if __name__ == "__main__":
    main()
