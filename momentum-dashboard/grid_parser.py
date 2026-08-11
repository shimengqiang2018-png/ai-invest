#!/usr/bin/env python3
"""网格触发记录识别：OCR + 多厂商模型解析（复用持仓解析的本地 OCR 与模型层）。

两条通道（与持仓解析一致）：
  - 视觉模型（qwen-vl-max / glm-4v-plus / Claude 等）直接看图；
  - 文本模型（如 DeepSeek）走 本地 tesseract OCR → 结构化 JSON。

输出 records 每项：
  date / code / name / action(buy|sell) / price / shares / amount(可空)
  base_price_before / base_price_after（条件单基准价变动，可空）
"""

from __future__ import annotations

import re

from models import chat_json, load_config
from positions_parser import ocr_images


CODE_RE = re.compile(r"^\d{6}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$")
TRIGGER_TYPES = ("grid", "add", "reduce", "momentum")


GRID_PARSE_SYSTEM = """你是证券账户「网格交易/成交记录」识别助手。
从券商 App 截图（东方财富成交记录、网格条件单记录、持仓/委托记录等）中提取
网格买卖成交，只输出 JSON，不要解释。

## 字段口径
- 每笔成交：date(YYYY-MM-DD)、time(HH:MM:SS，可空)、code(6位)、name、action
  （证券买入→buy / 证券卖出→sell）、price(成交价)、shares(数量)、amount(金额，可空)
- 若截图含条件单基准价变动信息，提取 base_price_before / base_price_after
  （触发前/后基准价，可空）；否则置 null
- 金额/价格保留3位小数；看不清的字段填 null，禁止猜测

## OCR 规则
- 复合列拆分：「证券买入」→ buy、「证券卖出」→ sell；成交价/数量/金额按列对齐
- 日期只出现一次时，向下关联到后续多笔成交
- 金额千分位、小数点、百分号首位丢失等 OCR 误差需纠错
- 名称与代码分行显示时，用 6 位数字特征在相邻行关联

## 输出 JSON 结构
{
  "detected_platforms": [{"image_index": 1, "platform": "eastmoney_transaction"}],
  "trades": [
    {"date": "YYYY-MM-DD", "time": "HH:MM:SS", "code": "6位代码", "name": "名称",
     "action": "buy/sell", "price": 数字, "shares": 数字, "amount": 数字,
     "base_price_before": 数字|null, "base_price_after": 数字|null}
  ]
}
多张截图合并：按 日期+代码+动作+价格+数量 去重。"""


GRID_PARSE_USER = """请识别以下网格交易/成交截图：
1) 标记每张图的平台与页面类型（detected_platforms）；
2) 提取所有买卖成交 trades（action 用 buy/sell，带 date，有 time 时带上）；
3) 有基准价变动信息时补 base_price_before / base_price_after。
输出严格 JSON。"""


GRID_OCR_USER = (
    "以下是从券商 App 截图 OCR 出的文本（可能有多张图，每张以「图N」开头），"
    "OCR 可能存在识别误差（复合列被拆成两行、名称与代码分行、金额千分位、"
    "小数点丢失等）。请纠错并提取网格买卖成交 JSON。\n\n{ocr_text}"
)


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round3(value):
    return round(_num(value), 3) if _num(value) is not None else None


def parse_grid_images(provider: str, images: list[dict], log=None) -> dict:
    """解析截图：视觉模型直接看图；文本模型走 本地OCR→结构化。

    log 为可选日志回调 log(message, level)，用于打印识别过程。
    """
    def _log(message, level="INFO"):
        if log is None:
            return
        try:
            log(message, level)
        except Exception:
            pass

    cfg = load_config()
    providers = cfg.get("providers") or {}
    name = provider or cfg.get("default_provider", "")
    spec = providers.get(name) or {}
    vision = bool(spec.get("vision"))
    if vision:
        _log(
            f"GRID-PARSE 通道=视觉模型 {spec.get('model', name)} 直接看图 "
            f"({len(images)} 张)"
        )
        user_text = GRID_PARSE_USER
        pipeline = "vision"
    else:
        ocr_text = ocr_images(images)
        _log(
            f"GRID-PARSE 通道=本地OCR+文本模型 {spec.get('model', name)}，"
            f"OCR 文本 {len(ocr_text)} 字符"
        )
        _log(
            f"GRID-PARSE OCR 文本片段: "
            f"{ocr_text[:400].replace(chr(10), ' ⏎ ')}"
        )
        user_text = GRID_OCR_USER.format(ocr_text=ocr_text)
        pipeline = "local_ocr"
    parsed, text = chat_json(
        name,
        GRID_PARSE_SYSTEM,
        user_text,
        images=images if vision else None,
        max_tokens=4000,
        log=_log,
    )
    _log(f"GRID-PARSE 模型返回 {len(text)} 字符")
    parsed["parse_pipeline"] = pipeline
    _log(
        f"GRID-PARSE 结构化完成 trades="
        f"{len(parsed.get('trades') or [])} "
        f"platforms={len(parsed.get('detected_platforms') or [])}"
    )
    for trade in parsed.get("trades") or []:
        for key in ("price", "amount", "base_price_before", "base_price_after"):
            if key in trade:
                trade[key] = _round3(trade.get(key))
        if trade.get("action"):
            trade["action"] = str(trade["action"]).strip().lower()
    return parsed


def verify_grid_records(
    records: list[dict],
    known_codes: set | None = None,
    grid_sizes: dict | None = None,
) -> list[dict]:
    """三级核验：通过(ok) / ⚠核实(warn) / ✗错误(error)。"""
    known_codes = known_codes or set()
    grid_sizes = grid_sizes or {}
    for record in records:
        issues = []
        warns = []
        code = str(record.get("code") or "").strip()
        action = str(record.get("action") or "").strip().lower()
        trigger_type = str(
            record.get("trigger_type") or record.get("type") or ""
        ).strip().lower()
        date = str(record.get("date") or "").strip()
        trade_time = str(record.get("time") or "").strip()
        price = _num(record.get("price"))
        shares = record.get("shares")
        try:
            shares_int = int(float(shares))
        except (TypeError, ValueError):
            shares_int = None

        if not CODE_RE.match(code):
            issues.append("代码需为 6 位数字")
        elif known_codes and code not in known_codes:
            warns.append(f"{code} 不在网格标的中（请确认是否为网格交易）")
        if action not in ("buy", "sell"):
            issues.append("动作需为 buy/sell")
        if trigger_type and trigger_type not in TRIGGER_TYPES:
            issues.append("类型需为 grid/add/reduce/momentum")
        elif not trigger_type:
            trigger_type = "grid"
        if not DATE_RE.match(date):
            issues.append("日期格式应为 YYYY-MM-DD")
        if trade_time and not TIME_RE.match(trade_time):
            issues.append("时间格式应为 HH:MM:SS")
        elif trade_time and len(trade_time) == 5:
            trade_time = trade_time + ":00"
        if price is None or price <= 0:
            issues.append("价格需为正数")
        if shares_int is None or shares_int <= 0:
            issues.append("数量需为正整数")

        for key in ("base_price_before", "base_price_after"):
            value = _num(record.get(key))
            if value is not None and value <= 0:
                issues.append(f"{key} 需为正数")

        per_grid = grid_sizes.get(code)
        if (
            shares_int
            and per_grid
            and shares_int % int(per_grid) != 0
        ):
            warns.append(f"数量 {shares_int} 不是每格 {int(per_grid)} 股的整数倍")

        record["code"] = code
        record["action"] = action
        record["trigger_type"] = trigger_type
        record["date"] = date
        record["time"] = trade_time
        if price is not None:
            record["price"] = _round3(price)
        if shares_int is not None:
            record["shares"] = shares_int

        if issues:
            record["status"] = "error"
        elif warns:
            record["status"] = "warn"
        else:
            record["status"] = "ok"
        record["issues"] = issues
        record["warns"] = warns
    return records
