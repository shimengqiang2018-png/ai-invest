#!/usr/bin/env python3
"""持仓服务层：当前持仓快照读取 + 策略归属分类标注。"""

from __future__ import annotations

import json
from pathlib import Path

import db  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent


def load_holdings_strategy() -> dict:
    """读取持仓策略归属配置（网格/动量双策略标的口径）。"""
    path = ROOT / "holdings_strategy.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def latest_positions():
    """返回最新持仓快照（权威来源：数据库 holdings_current，不再从文件兜底）。"""
    try:
        snap = db.load_holdings_current()
        if snap:
            return snap
    except Exception:
        pass
    return {
        "date": None,
        "source": "db",
        "holdings": [],
        "account_summary": {},
        "notes": ["数据库 holdings_current 为空，请先通过持仓解析导入"],
    }


def enrich_holdings_classification(holdings: list) -> list:
    """给持仓补充 网格/动量/共用/底仓/现金 分类与底仓股数（数据库权威字段）。"""
    try:
        from positions_parser import _classify_holding
    except Exception:
        return holdings
    enriched = []
    for holding in holdings:
        code = str(holding.get("code") or "").strip()
        strategy, bucket, base_shares = _classify_holding(
            code, int(holding.get("shares") or 0)
        )
        enriched.append({
            **holding,
            "strategy": strategy,
            "bucket": bucket,
            "base_shares": base_shares,
        })
    return enriched
