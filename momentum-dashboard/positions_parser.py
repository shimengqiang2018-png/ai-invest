#!/usr/bin/env python3
"""持仓图片解析 → 结构化 + 核验 → 更新系统。

流程：
  1. 调用视觉模型解析截图，输出 account_summary / holdings / trades
  2. verify_parsed() 逐字段核验（代码合法性、市值≈股数×现价、
     盈亏≈(现价-成本)×股数、汇总勾稽、与实时行情比对）
  3. 前端确认后 update_positions() 写入 data/positions_latest.json（先备份）
"""

from __future__ import annotations

import json
import os
import base64
import io
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from models import chat_json, load_config


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
POSITIONS_FILE = DATA_DIR / "positions_latest.json"
HISTORY_FILE = DATA_DIR / "ai_parse_history.json"
REPORTS_DIR = DATA_DIR.parent / "reports"

CODE_RE = re.compile(r"^\d{6}$")

PARSE_SYSTEM = """你是证券账户持仓识别助手，严格遵循 fund-screenshot-ocr skill 的规范。
从用户提供的截图（天天基金 / 东方财富 / 招商银行）中提取结构化 JSON。只输出 JSON，不要解释。

## 一、平台与页面类型识别（铁证信号优先，命中即停）
- 出现「银行卡尾号」或「在途资金」 → cmb（招商银行）；单只基金全屏且有「可用份额」「最新净值」「持仓成本价」→ cmb_detail，否则 cmb_list
- 出现「银证转账」或「总资产+证券市值」或「持仓/可用 + 现价/成本」复合列头 → eastmoney_position（东方财富持仓）
- 出现「证券买入」/「证券卖出」/「当日委托」 → eastmoney_transaction（东方财富成交记录）
- 出现「累计收益」（注意不是"持仓收益"）→ tiantianfund（天天基金持仓列表）
- 模糊时按布局特征打分：天天基金=「资产」+「累计收益」+基金诊断；东方财富=「当日盈亏」+「总资产」；招商银行=「金额(元)」+「持有XX天」+「产品详情」

## 二、各平台字段口径
- tiantianfund：code、name、market_value(持仓市值)、total_return(累计收益)、return_rate(收益率)、daily_return(昨日收益)
- eastmoney_position：账户概览 total_assets/securities_value/available_cash/withdrawable(可取)/daily_pnl(当日盈亏)；持仓 code、name、shares(持仓)、available(可用)、price(现价)、cost(成本)、pnl(浮动盈亏)、pnl_pct(盈亏率)
- eastmoney_transaction：date、time、code、name、action(证券买入→buy / 证券卖出→sell)、price、shares、amount
- cmb_list：code、name、market_value(金额(元))、total_return(持仓收益)、return_rate、daily_return(昨日收益)
- cmb_detail：code、name、market_value(金额(元))、hold_days(持有天数)、daily_return(昨日收益)、total_return(持仓收益)、return_rate、available_shares(可用份额)、nav(最新净值)、cost(持仓成本价)、card_tail(银行卡尾号)、in_transit(在途资金)、daily_change_pct(日涨幅)

## 三、OCR 规则（必须执行）
- 复合列拆分：「持仓/可用」→ shares/available 两值；「现价/成本」→ price/cost 两值；天天基金「+33.24 +0.51%」→ total_return/return_rate
- 名称与代码分行显示时，用 6 位数字特征在相邻行间关联
- 成交记录日期只出现一次时，向下关联到后续多笔交易
- 百分比必须验算：return_rate ≈ total_return / (market_value - total_return)，偏差>2%取验算值
- 招商银行代码缺失时禁止跨平台猜测，填 null；可通过名称查证
- 金额单位统一人民币元；ETF 数量为股，场外基金份额两位小数；看不清的字段给 null 不猜
- **精度要求**：金额类字段（总资产/证券市值/可用资金/市值/盈亏/成交金额）与盈亏率
  统一保留3位小数（如 45000.000、+5.882%）；价格/成本保留3位小数（ETF 常见，如 1.505）；
  占比（weight_pct）保留2位小数

## 四、输出 JSON 结构
{
  "detected_platforms": [{"image_index": 1, "platform": "eastmoney_position", "page_type": "持仓"}],
  "account_summary": {"total_assets": 数字, "securities_value": 数字, "available_cash": 数字,
                       "withdrawable": 数字, "total_pnl": 数字, "daily_pnl": 数字},
  "holdings": [{"code": "6位代码", "name": "名称", "source": "tiantianfund/eastmoney_position/cmb_list/cmb_detail",
                 "shares": 数字, "available": 数字, "price": 数字, "cost": 数字,
                 "market_value": 数字, "pnl": 数字, "pnl_pct": 数字,
                 "nav": 数字, "available_shares": 数字, "card_tail": "xxxx", "hold_days": 数字}],
  "trades": [{"date": "YYYY-MM-DD", "time": "HH:MM:SS", "action": "buy/sell", "code": "6位代码",
               "name": "名称", "price": 数字, "shares": 数字, "amount": 数字,
               "source": "eastmoney_transaction"}]
}
多张截图合并：按 code 合并持仓（保留各字段最清晰的值）；按 日期+代码+动作 去重交易。"""

PARSE_USER = """请按 fund-screenshot-ocr skill 规范解析以下持仓/成交截图：
1) 先识别每张图的平台与页面类型（detected_platforms）；
2) 提取 account_summary（有账户概览时）；
3) 提取 holdings（每项带 source 平台标记，招行详情页补 nav/available_shares/card_tail 等）；
4) 提取 trades（东方财富成交记录，action 用 buy/sell，带 time）。
输出严格 JSON。"""

OCR_USER = (
    "以下是从券商/银行持仓截图 OCR 出的文本（可能有多张图，每张以「图N」开头），"
    "OCR 可能存在识别误差（复合列被拆成两行、名称与代码分行、百分号丢首位数字、"
    "金额千分位等）。请按 fund-screenshot-ocr skill 规范纠错并提取 JSON。\n\n"
    "{ocr_text}"
)


def ocr_images(images: list[dict]) -> str:
    """本地 tesseract OCR：放大2倍 + 增强对比度 + 锐化（对齐 fund-screenshot-ocr）。"""
    try:
        import pytesseract
        from PIL import Image, ImageEnhance, ImageFilter
    except ImportError as exc:
        raise RuntimeError(
            f"本地 OCR 需要 pytesseract/PIL: {exc}（或改用支持视觉的模型厂商）"
        ) from exc
    blocks = []
    for index, image in enumerate(images, 1):
        try:
            raw = base64.b64decode(image.get("data_b64") or "")
            img = Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception as exc:
            raise RuntimeError(f"图{index} 解码失败: {exc}") from exc
        width, height = img.size
        img = img.resize((width * 2, height * 2), Image.LANCZOS)
        img = ImageEnhance.Contrast(img).enhance(2.0)
        img = img.filter(ImageFilter.SHARPEN)
        text = pytesseract.image_to_string(
            img, lang="chi_sim+eng", config="--psm 3"
        )
        blocks.append(
            f"===== 图{index}（{image.get('name', '')}）=====\n{text.strip()}"
        )
    return "\n\n".join(blocks)


def _normalize_precision(parsed: dict) -> dict:
    """金额/价格/盈亏率统一保留3位小数（识别结果归一，不依赖模型自觉）。"""
    def r3(value):
        if value is None:
            return None
        try:
            return round(float(value), 3)
        except (TypeError, ValueError):
            return None

    summary = parsed.get("account_summary") or {}
    for key in (
        "total_assets", "securities_value", "available_cash",
        "withdrawable", "total_pnl", "daily_pnl",
    ):
        if key in summary:
            summary[key] = r3(summary.get(key))
    for holding in parsed.get("holdings") or []:
        for key in ("market_value", "pnl", "price", "cost", "nav"):
            if key in holding:
                holding[key] = r3(holding.get(key))
        if "pnl_pct" in holding:
            holding["pnl_pct"] = r3(holding.get("pnl_pct"))
    for trade in parsed.get("trades") or []:
        for key in ("price", "amount"):
            if key in trade:
                trade[key] = r3(trade.get(key))
    return parsed


def _num(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round3(value):
    return round(value, 3) if value is not None else None


def parse_images(provider: str, images: list[dict], log=None) -> dict:
    """解析图片：视觉模型直接看图；文本模型走 本地OCR→结构化。

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
            f"POS-PARSE 通道=视觉模型 {spec.get('model', name)} 直接看图 "
            f"({len(images)} 张)"
        )
        user_text = PARSE_USER
        pipeline = "vision"
    else:
        ocr_text = ocr_images(images)
        _log(
            f"POS-PARSE 通道=本地OCR+文本模型 {spec.get('model', name)}，"
            f"OCR 文本 {len(ocr_text)} 字符"
        )
        _log(
            f"POS-PARSE OCR 文本片段: "
            f"{ocr_text[:400].replace(chr(10), ' ⏎ ')}"
        )
        user_text = OCR_USER.format(ocr_text=ocr_text)
        pipeline = "local_ocr"
    parsed, text = chat_json(
        name,
        PARSE_SYSTEM,
        user_text,
        images=images if vision else None,
        max_tokens=6000,
        log=_log,
    )
    _log(f"POS-PARSE 模型返回 {len(text)} 字符")
    parsed["parse_pipeline"] = pipeline
    _log(
        f"POS-PARSE 结构化完成 holdings="
        f"{len(parsed.get('holdings') or [])} "
        f"trades={len(parsed.get('trades') or [])}"
    )
    return _normalize_precision(parsed)


def _load_etf_codes() -> set[str]:
    meta_path = DATA_DIR / "etf_meta.json"
    codes = set()
    try:
        with open(meta_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        for item in payload.get("etfs") or []:
            code = str(item.get("code", ""))
            if CODE_RE.match(code):
                codes.add(code)
    except (OSError, json.JSONDecodeError):
        pass
    return codes


def _holding_pnl_calc(holding: dict) -> dict:
    """计算持仓内部勾稽：市值≈股数×现价、盈亏≈(现价-成本)×股数。"""
    shares = _num(holding.get("shares"))
    price = _num(holding.get("price"))
    cost = _num(holding.get("cost"))
    market_value = _num(holding.get("market_value"))
    pnl = _num(holding.get("pnl"))
    pnl_pct = _num(holding.get("pnl_pct"))
    issues = []

    mv_calc = shares * price if shares is not None and price is not None else None
    if market_value is not None and mv_calc is not None and abs(market_value - mv_calc) > max(1.0, mv_calc * 0.02):
        issues.append(f"市值与 股数×现价 不符（{market_value:.2f} vs {mv_calc:.2f}）")
    pnl_calc = (price - cost) * shares if shares is not None and price is not None and cost is not None else None
    if pnl is not None and pnl_calc is not None:
        # 现价/成本均为 3 位小数舍入，而券商盈亏由未舍入值计算；
        # 大持仓下价格舍入误差会按股数放大（现价+成本合计最多 2×股数×0.001），须纳入容差。
        rounding_allowance = 2 * shares * 0.001 if shares else 0.0
        tolerance = max(1.0, abs(pnl_calc) * 0.03, rounding_allowance)
        if abs(pnl - pnl_calc) > tolerance:
            issues.append(f"盈亏与 (现价-成本)×股数 不符（{pnl:.2f} vs {pnl_calc:.2f}）")
    if pnl_pct is not None and cost and shares:
        pnl_pct_calc = (price - cost) / cost * 100 if price is not None else None
        if pnl_pct_calc is not None and abs(pnl_pct - pnl_pct_calc) > 0.5:
            issues.append(f"盈亏率不符（{pnl_pct:.2f}% vs {pnl_pct_calc:.2f}%）")
    return {
        "market_value_calc": _round3(mv_calc),
        "pnl_calc": _round3(pnl_calc),
        "issues": issues,
    }


def verify_parsed(parsed: dict, realtime: dict | None = None) -> dict:
    """核验解析结果，返回 {summary, holdings:[...], trades:[...]} + 每项状态。"""
    known_codes = _load_etf_codes()
    summary = parsed.get("account_summary") or {}
    holdings = parsed.get("holdings") or []
    trades = parsed.get("trades") or []

    summary_issues = []
    sec_value = _num(summary.get("securities_value"))
    cash = _num(summary.get("available_cash"))
    total = _num(summary.get("total_assets"))
    sum_mv = sum((_num(h.get("market_value")) or 0) for h in holdings)
    if sec_value is not None and sum_mv and abs(sec_value - sum_mv) > max(1.0, sum_mv * 0.02):
        summary_issues.append(
            f"证券市值 {sec_value:.2f} 与持仓市值合计 {sum_mv:.2f} 不一致"
        )
    if sec_value is not None and cash is not None and total is not None:
        calc_total = sec_value + cash
        if abs(total - calc_total) > max(1.0, calc_total * 0.02):
            summary_issues.append(
                f"总资产 {total:.2f} 与 证券市值+资金 {calc_total:.2f} 不一致"
            )

    verified_holdings = []
    total_mv = sum((_num(h.get("market_value")) or 0) for h in holdings)
    total_assets = _num(summary.get("total_assets")) or total_mv
    for holding in holdings:
        code = str(holding.get("code") or "").strip()
        errors = []
        warnings = []
        if not CODE_RE.match(code):
            errors.append(f"代码格式非法: {code!r}")
        elif known_codes and code not in known_codes:
            warnings.append(f"代码 {code} 不在已知 ETF 列表，请人工确认")
        calc = _holding_pnl_calc(holding)
        errors.extend(calc["issues"])

        rt = (realtime or {}).get("quotes", {}).get(code) if realtime else None
        rt_note = None
        if rt and rt.get("source") == "tencent_realtime":
            price = _num(holding.get("price"))
            rt_price = _num(rt.get("price"))
            if price is not None and rt_price:
                diff = abs(price - rt_price) / rt_price
                if diff > 0.03:
                    warnings.append(
                        f"现价与实时行情差异 {diff * 100:.1f}%（截图 {price} vs 实时 {rt_price}）"
                    )
                elif diff > 0.005:
                    rt_note = f"实时价 {rt_price}（截图价 {price}）"

        status = "error" if errors else "warn" if warnings else "ok"
        mv = _num(holding.get("market_value"))
        weight_pct = (
            round(mv / total_assets * 100, 2)
            if mv is not None and total_assets
            else None
        )
        verified_holdings.append({
            **holding,
            "code": code,
            "status": status,
            "errors": errors,
            "warnings": warnings,
            "issues": errors + warnings,
            "realtime_note": rt_note,
            "weight_pct": weight_pct,
            "calc": calc,
        })

    verified_trades = []
    for trade in trades:
        code = str(trade.get("code") or "").strip()
        errors = []
        if not CODE_RE.match(code):
            errors.append(f"代码格式非法: {code!r}")
        if _num(trade.get("price")) is None or _num(trade.get("price")) <= 0:
            errors.append("价格缺失或非法")
        if _num(trade.get("shares")) is None or _num(trade.get("shares")) <= 0:
            errors.append("数量缺失或非法")
        verified_trades.append({
            **trade,
            "code": code,
            "status": "error" if errors else "ok",
            "errors": errors,
            "warnings": [],
            "issues": errors,
        })

    # 成交记录反推交叉校验（skill 第四/六步）
    trade_by_code: dict[str, dict] = {}
    for trade in trades:
        code = str(trade.get("code") or "").strip()
        if not CODE_RE.match(code):
            continue
        entry = trade_by_code.setdefault(
            code, {"buy_shares": 0.0, "sell_shares": 0.0, "buy_amount": 0.0, "count": 0}
        )
        action = str(trade.get("action") or "")
        shares = _num(trade.get("shares")) or 0
        amount = _num(trade.get("amount")) or 0
        if "buy" in action.lower() or "买" in action:
            entry["buy_shares"] += shares
            entry["buy_amount"] += amount
        elif "sell" in action.lower() or "卖" in action:
            entry["sell_shares"] += shares
        entry["count"] += 1

    cross_validation = []
    for holding in verified_holdings:
        code = holding["code"]
        info = trade_by_code.get(code)
        if not info:
            continue
        calc_shares = info["buy_shares"] - info["sell_shares"]
        calc_cost = (
            round(info["buy_amount"] / info["buy_shares"], 4) if info["buy_shares"] else None
        )
        errors = holding.setdefault("errors", [])
        warnings = holding.setdefault("warnings", [])
        reported_shares = _num(holding.get("shares"))
        reported_cost = _num(holding.get("cost"))
        if reported_shares is not None and abs(calc_shares) > 0:
            diff = abs(reported_shares - calc_shares) / max(abs(calc_shares), 1.0)
            if diff > 0.03:
                warnings.append(
                    f"成交反推持仓 {calc_shares:.0f} 股 vs 截图 {reported_shares:.0f} 股"
                    f"（偏差 {diff * 100:.1f}%）"
                )
                holding["status"] = "warn" if holding["status"] != "error" else "error"
        if calc_cost and reported_cost:
            diff = abs(reported_cost - calc_cost) / calc_cost
            if diff > 0.03:
                warnings.append(
                    f"成交反推成本 {calc_cost} vs 截图 {reported_cost}"
                    f"（偏差 {diff * 100:.1f}%）"
                )
                holding["status"] = "warn" if holding["status"] != "error" else "error"
        holding["issues"] = errors + warnings
        cross_validation.append({
            "code": code,
            "name": holding.get("name"),
            "calc_shares": round(calc_shares, 2),
            "reported_shares": holding.get("shares"),
            "calc_cost": calc_cost,
            "reported_cost": holding.get("cost"),
            "trades": info["count"],
        })

    error_count = sum(1 for h in verified_holdings if h["status"] == "error")
    warn_count = sum(1 for h in verified_holdings if h["status"] == "warn")
    trade_errors = sum(1 for t in verified_trades if t["status"] == "error")
    overall = (
        "error"
        if (error_count or trade_errors or summary_issues)
        else "warn"
        if warn_count
        else "ok"
    )
    return {
        "account_summary": {
            **summary,
            "total_assets": _round3(total),
            "securities_value": _round3(sec_value),
            "available_cash": _round3(cash),
            "issues": summary_issues,
        },
        "holdings": verified_holdings,
        "trades": verified_trades,
        "cross_validation": cross_validation,
        "status": overall,
        "counts": {
            "holdings": len(verified_holdings),
            "trades": len(verified_trades),
            "holdings_ok": sum(1 for h in verified_holdings if h["status"] == "ok"),
            "holdings_warn": warn_count,
            "holdings_error": error_count,
            "trades_error": trade_errors,
        },
        "verified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def update_positions(verified: dict, source_note: str = "AI 图片解析") -> dict:
    """核验通过后写入系统。先备份旧文件，再写新快照并追加解析历史。"""
    holdings = []
    for holding in verified.get("holdings") or []:
        if holding.get("status") == "error":
            raise ValueError(
                f"存在核验错误项（{holding.get('code')}）: "
                f"{'; '.join(holding.get('issues', []))}，请修正后再更新"
            )
        holdings.append({
            "code": holding.get("code"),
            "name": holding.get("name"),
            "shares": holding.get("shares"),
            "available": holding.get("available"),
            "price": holding.get("price"),
            "cost": holding.get("cost"),
            "market_value": holding.get("market_value"),
            "pnl": holding.get("pnl"),
            "pnl_pct": holding.get("pnl_pct"),
            "weight_pct": holding.get("weight_pct"),
            "daily_pnl": holding.get("daily_pnl"),
            "strategy": holding.get("strategy"),
            "source": holding.get("source"),
            "verified": holding.get("verified"),
        })

    summary = verified.get("account_summary") or {}
    snapshot = {
        "date": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": source_note,
        "account_summary": {
            "total_assets": summary.get("total_assets"),
            "securities_value": summary.get("securities_value"),
            "available_cash": summary.get("available_cash"),
            "withdrawable": summary.get("withdrawable"),
            "position_ratio": (
                round(summary.get("securities_value", 0) / summary.get("total_assets", 1) * 100, 2)
                if summary.get("total_assets")
                else None
            ),
            "daily_pnl": summary.get("daily_pnl"),
            "total_pnl": summary.get("total_pnl"),
        },
        "holdings": holdings,
        "cross_validation": [
            {
                "code": item.get("code"),
                "name": item.get("name"),
                "calc_shares": item.get("calc_shares"),
                "reported_shares": item.get("reported_shares"),
                "calc_cost": item.get("calc_cost"),
                "reported_cost": item.get("reported_cost"),
                "trades": item.get("trades"),
            }
            for item in (verified.get("cross_validation") or [])
        ],
        "trades": [
            {
                "date": t.get("date"),
                "action": t.get("action"),
                "code": t.get("code"),
                "name": t.get("name"),
                "price": t.get("price"),
                "shares": t.get("shares"),
                "amount": t.get("amount"),
            }
            for t in (verified.get("trades") or [])
        ],
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if POSITIONS_FILE.exists():
        backup = POSITIONS_FILE.with_name(
            f"positions_latest.bak-{time.strftime('%Y%m%d%H%M%S')}.json"
        )
        shutil.copy2(POSITIONS_FILE, backup)

    with open(POSITIONS_FILE, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)
    _write_portfolio_markdown(snapshot)
    excel_file = _write_excel_snapshot(snapshot)
    if excel_file:
        snapshot["excel_file"] = excel_file

    history = []
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if not isinstance(history, list):
                history = []
        except (OSError, json.JSONDecodeError):
            history = []
    history.append({
        "updated_at": snapshot["date"],
        "source": source_note,
        "holdings_count": len(holdings),
        "trades_count": len(snapshot["trades"]),
        "account_summary": snapshot["account_summary"],
    })
    HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # 同步写入 SQLite 数据库
    try:
        from db import append_parse_history, save_positions_snapshot
        save_positions_snapshot(snapshot)
        append_parse_history(
            snapshot["date"],
            source_note,
            len(holdings),
            len(snapshot["trades"]),
            snapshot,
        )
    except Exception:
        pass  # 数据库写入失败不影响文件落盘
    return snapshot


def _write_portfolio_markdown(snapshot: dict) -> Path:
    """按 portfolio-review skill 规范保存组合文件 reports/portfolio-latest.md。"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / "portfolio-latest.md"

    summary = snapshot.get("account_summary") or {}
    total_assets = summary.get("total_assets")
    lines = [
        "# 组合持仓（AI 解析更新）",
        "",
        f"> 更新日期: {snapshot.get('date')} ｜ 来源: {snapshot.get('source')}",
        "",
        "## 最新持仓",
        "",
        "| 代码 | 名称 | 持仓量 | 成本价 | 现价 | 市值 | 占比 | 盈亏 | 盈亏率 |",
        "|------|------|-------:|------:|-----:|-----:|-----:|-----:|-------:|",
    ]
    for holding in snapshot.get("holdings") or []:
        weight = holding.get("weight_pct")
        weight_text = f"{weight:.2f}%" if weight is not None else "—"
        lines.append(
            f"| {holding.get('code', '—')} | {holding.get('name', '—')} "
            f"| {holding.get('shares', '—')} | {holding.get('cost', '—')} "
            f"| {holding.get('price', '—')} | {holding.get('market_value', '—')} "
            f"| {weight_text} | {holding.get('pnl', '—')} | {holding.get('pnl_pct', '—')}% |"
        )
    lines.extend([
        "",
        "## 账户汇总",
        "",
        f"- 总资产: {total_assets}",
        f"- 证券市值: {summary.get('securities_value')}",
        f"- 可用资金: {summary.get('available_cash')}",
        f"- 持仓盈亏: {summary.get('total_pnl')}",
        f"- 当日盈亏: {summary.get('daily_pnl')}",
        f"- 仓位: {summary.get('position_ratio')}%",
        "",
        "## 交易记录（本次解析）",
        "",
    ])
    trades = snapshot.get("trades") or []
    if trades:
        lines.append("| 日期 | 方向 | 代码 | 名称 | 价格 | 数量 | 金额 |")
        lines.append("|------|------|------|------|-----:|-----:|-----:|")
        for trade in trades:
            lines.append(
                f"| {trade.get('date', '—')} | {trade.get('action', '—')} "
                f"| {trade.get('code', '—')} | {trade.get('name', '—')} "
                f"| {trade.get('price', '—')} | {trade.get('shares', '—')} "
                f"| {trade.get('amount', '—')} |"
            )
    else:
        lines.append("无")
    lines.extend([
        "",
        "## 下次审视提醒",
        "",
        "- 下次更新持仓后，按 portfolio-review skill 审视组合集中度、相关性与机会成本。",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_excel_snapshot(snapshot: dict) -> str | None:
    """按 fund-screenshot-ocr skill 输出规范生成 Excel 快照。"""
    try:
        tools_dir = DATA_DIR.parent / "tools"
        sys.path.insert(0, str(tools_dir))
        from generate_fund_excel import generate_fund_excel
    except Exception:
        return None
    funds = []
    for holding in snapshot.get("holdings") or []:
        if not holding.get("code"):
            continue
        funds.append({
            "code": holding["code"],
            "name": holding.get("name", ""),
            "market_value": holding.get("market_value"),
            "total_return": holding.get("pnl"),
            "return_rate": (holding.get("pnl_pct") or 0) / 100,
            "daily_return": holding.get("daily_pnl"),
            "source_img": holding.get("source", ""),
            "note": holding.get("source", ""),
        })
    if not funds:
        return None
    etf_dir = REPORTS_DIR / "ETF"
    etf_dir.mkdir(parents=True, exist_ok=True)
    excel_path = etf_dir / f"持仓数据-{time.strftime('%Y%m%d')}.xlsx"
    try:
        generate_fund_excel(
            funds,
            str(excel_path),
            screenshot_date=datetime.now().strftime("%Y-%m-%d"),
        )
        return str(excel_path)
    except Exception:
        return None
