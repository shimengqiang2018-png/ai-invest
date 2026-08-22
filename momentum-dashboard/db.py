#!/usr/bin/env python3
"""数据访问层（仅支持 MySQL）。

本模块是整个系统**唯一**包含 SQL / 直接操作数据库的代码层：
业务代码（server.py / positions_parser.py / scheduler.py 等）只能通过
本模块的函数读写数据，不得出现 SQL 或数据库驱动调用。

后端切换：
  - MySQL：依赖 PyMySQL + SQLAlchemy，连接参数来自
    环境变量 DB_BACKEND=mysql / DB_HOST / DB_PORT / DB_USER / DB_PASSWORD /
    DB_NAME（可写入项目根目录 .env）。

表结构（MySQL DDL 见 schema_mysql.sql）：全部表均含
  id BIGINT 主键 / created_at 创建时间 / updated_at 更新时间 / 字段注释。
代码不再自动建表或插入默认数据：建表与初始数据由 schema_mysql.sql / 人工维护。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote_plus

from sqlalchemy import create_engine


ROOT = Path(__file__).resolve().parent


def _load_env_file():
    """从项目根目录 .env 加载 DB_* 配置（已存在的环境变量优先）。"""
    env_path = ROOT.parent / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if key.startswith("DB_") and key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


_load_env_file()


# 后端配置（仅 MySQL；连接参数由环境变量可覆盖）
_backend_name = "mysql"
_MYSQL_HOST = os.environ.get("DB_HOST", "127.0.0.1")
_MYSQL_PORT = int(os.environ.get("DB_PORT", "3306"))
_MYSQL_USER = os.environ.get("DB_USER", "invest")
_MYSQL_PASSWORD = os.environ.get("DB_PASSWORD", "invest123")
_MYSQL_DB = os.environ.get("DB_NAME", "ai_invest")

# ── 业务 SQL 日志（全部打印，进 momentum-dashboard logger = server.log）──
_SQL_LOG_MAX_PARAM = 300      # 单个参数最大字符数（超长截断，避免日志爆炸）
_SQL_LOG_MAX_SQL = 4000       # SQL 语句最大字符数
_SQL_LOG_MAX_TOTAL = 8000     # 参数文本总量上限


def _sql_params_repr(params) -> str:
    """参数转可读文本；executemany 批量参数逐行展示，超长截断标注长度。"""
    if not params:
        return ""

    def _fmt(value) -> str:
        if value is None:
            return "NULL"
        text = str(value)
        if len(text) > _SQL_LOG_MAX_PARAM:
            text = text[:_SQL_LOG_MAX_PARAM] + f"...(len={len(text)})"
        return text

    if (
        isinstance(params, (list, tuple))
        and params
        and isinstance(params[0], (list, tuple))
    ):
        parts = [
            "(" + ", ".join(_fmt(v) for v in row) + ")"
            for row in params
        ]
        text = " ".join(parts)
    elif isinstance(params, (list, tuple)):
        text = ", ".join(_fmt(v) for v in params)
    else:
        text = _fmt(params)
    if len(text) > _SQL_LOG_MAX_TOTAL:
        text = text[:_SQL_LOG_MAX_TOTAL] + f"...(total_len={len(text)})"
    return text


def _sql_log(sql: str, params=()) -> None:
    """打印业务 SQL 完整日志（语句+参数），进 server.log；CLI 场景回退终端。"""
    try:
        sql_text = sql.strip()
        if len(sql_text) > _SQL_LOG_MAX_SQL:
            sql_text = sql_text[:_SQL_LOG_MAX_SQL] + f"...(len={len(sql)})"
        message = f"[SQL] {sql_text}"
        param_text = _sql_params_repr(params)
        if param_text:
            message += f" | params={param_text}"
        logger = logging.getLogger("momentum-dashboard")
        if logger.handlers:
            logger.info(message)
        else:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{stamp}] [INFO ] {message}", flush=True)
    except Exception:
        pass  # SQL 日志失败不影响业务执行


_lock = threading.RLock()
_engine = None  # SQLAlchemy 引擎（按当前后端惰性创建）

ALLOWED_TABLES = (
    "cache",
    "positions_snapshots",
    "holdings_current",
    "account_summary_current",
    "parse_history",
    "api_logs",
    "signal_history",
    "grid_triggers",
    "grid_configs",
    "momentum_pools",
    "backtest_results",
    "scheduler_runs",
)


def configure(backend: str) -> str:
    """重置连接（仅支持 mysql；返回生效后端）。测试用。"""
    global _backend_name, _engine
    if (backend or "").strip().lower() != "mysql":
        raise ValueError("仅支持 MySQL 后端（DB_BACKEND=mysql）")
    with _lock:
        _backend_name = "mysql"
        _engine = None
    return _backend_name


def db_backend() -> str:
    """返回当前数据后端标识。"""
    return _backend_name


def db_info() -> dict:
    """返回当前后端连接信息（不含密码）。"""
    return {
        "backend": "mysql",
        "host": _MYSQL_HOST,
        "port": _MYSQL_PORT,
        "database": _MYSQL_DB,
        "user": _MYSQL_USER,
    }


# ---------------------------------------------------------------------------
# SQLAlchemy 连接管理
# ---------------------------------------------------------------------------


def _make_engine():
    """创建 MySQL SQLAlchemy 引擎。"""
    url = (
        f"mysql+pymysql://{_MYSQL_USER}:{quote_plus(_MYSQL_PASSWORD)}"
        f"@{_MYSQL_HOST}:{_MYSQL_PORT}/{_MYSQL_DB}?charset=utf8mb4"
    )
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=3600,
        future=True,
    )


def _get_engine():
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = _make_engine()
    return _engine


def get_conn():
    """返回当前后端的一个 SQLAlchemy 连接（调用方负责 close/归还）。"""
    return _get_engine().connect()


class _ExecResult:
    """SQLAlchemy Result 的轻量兼容包装：fetchall→dict 列表，保留 cursor 语义。"""

    def __init__(self, result, rowcount=None, lastrowid=None):
        self._result = result
        self.rowcount = (
            rowcount
            if rowcount is not None
            else (int(result.rowcount) if result is not None and result.rowcount else 0)
        )
        self.lastrowid = lastrowid

    def fetchall(self):
        if self._result is None:
            return []
        return [dict(row._mapping) for row in self._result.fetchall()]

    def fetchone(self):
        if self._result is None:
            return None
        row = self._result.fetchone()
        return dict(row._mapping) if row is not None else None

    def close(self):
        if self._result is not None:
            try:
                self._result.close()
            except Exception:
                pass


def _result_lastrowid(result) -> int | None:
    """从 CursorResult 提取最后插入自增 id。"""
    try:
        value = result.lastrowid
        if value is not None:
            return int(value)
    except Exception:
        pass
    try:
        keys = result.insert_primary_key_rows
        if keys and keys[0]:
            return int(keys[0][0])
    except Exception:
        pass
    return None


def _exec(sql: str, params=()):
    """统一执行入口（SQLAlchemy Core + 驱动级 SQL，MySQL 占位符 %s），自动提交。"""
    sql = sql.replace("?", "%s")
    _sql_log(sql, params)
    engine = _get_engine()
    with engine.begin() as conn:
        if params and isinstance(params, (list, tuple)) and isinstance(
            params[0], (list, tuple, dict)
        ):
            result = conn.exec_driver_sql(sql, list(params))
        else:
            # 单条参数：list 标量需转 tuple，避免 exec_driver_sql 误判为 executemany
            single = tuple(params) if isinstance(params, list) else params
            result = conn.exec_driver_sql(sql, single)
        return _ExecResult(
            result,
            rowcount=result.rowcount,
            lastrowid=_result_lastrowid(result),
        )




def _count(table: str) -> int:
    row = _exec(f"SELECT COUNT(*) AS n FROM {_quote_ident(table)}").fetchone()
    return int(row["n"])


def _quote_ident(name: str) -> str:
    """返回 MySQL 标识符引用（反引号）。"""
    return f"`{name}`"


def _row_to_json(row: dict) -> dict:
    """把数据库行转成可 JSON 序列化的 dict（datetime/date/bytes 转字符串）。"""
    out = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat(sep=" ", timespec="seconds")
        elif isinstance(value, date):
            out[key] = value.isoformat()
        elif isinstance(value, Decimal):
            out[key] = float(value)
        elif isinstance(value, bytes):
            out[key] = value.decode("utf-8", "replace")
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# 建表（DDL 见 schema_mysql.sql，代码不自动建表）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def cache_get(key: str, ttl: float | None = None):
    """按 TTL 读取缓存，命中返回 dict，未命中/过期返回 None。

    ttl=None 表示忽略过期时间（取旧缓存）；ttl=0 表示立即过期。
    """
    row = _exec(
        "SELECT payload, UNIX_TIMESTAMP(updated_at) AS updated_at "
        "FROM cache WHERE cache_key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    if ttl is not None and time.time() - float(row["updated_at"]) > ttl:
        return None
    try:
        return json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        return None


def cache_set(key: str, payload: dict) -> None:
    blob = json.dumps(payload, ensure_ascii=False)
    _exec(
        "INSERT INTO cache (cache_key, payload, created_at, updated_at) "
        "VALUES (?, ?, NOW(), NOW()) "
        "ON DUPLICATE KEY UPDATE payload = VALUES(payload), "
        "updated_at = NOW()",
        (key, blob),
    )


def cache_delete_expired() -> int:
    """清理 7 天前的缓存行，返回删除条数。"""
    cutoff = (datetime.now() - timedelta(days=7)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return _exec("DELETE FROM cache WHERE updated_at < ?", (cutoff,)).rowcount


def cache_flush() -> int:
    """清空缓存表（测试/维护用），返回删除条数。"""
    return _exec("DELETE FROM cache").rowcount


# ---------------------------------------------------------------------------
# positions / parse history
# ---------------------------------------------------------------------------

def save_positions_snapshot(snapshot: dict) -> int:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    cursor = _exec(
        "INSERT INTO positions_snapshots (date, source, payload, "
        "created_at, updated_at) VALUES (?, ?, ?, NOW(), NOW())",
        (
            snapshot.get("date"),
            snapshot.get("source"),
            json.dumps(snapshot, ensure_ascii=False),
        ),
    )
    return int(cursor.lastrowid)


def latest_positions_snapshot() -> dict | None:
    row = _exec(
        "SELECT payload FROM positions_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload"])
    except (json.JSONDecodeError, TypeError):
        return None


def save_holdings_current(
    holdings: list[dict], account_summary: dict | None = None
) -> None:
    """把当前持仓+账户汇总写入数据库（先清空再写，保持单一权威快照）。

    holdings 每项字段: code/name/shares/available/price/cost/market_value/
    pnl/pnl_pct/daily_pnl/strategy/bucket/base_shares/source/verified
    """
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    _exec("DELETE FROM holdings_current")
    if account_summary:
        _exec("DELETE FROM account_summary_current")
        summary_cols = (
            "total_assets", "securities_value", "available_cash", "withdrawable",
            "position_ratio", "daily_pnl", "total_pnl", "source",
        )
        summary_vals = [account_summary.get(key) for key in summary_cols]
        _exec(
            "INSERT INTO account_summary_current "
            f"({', '.join(summary_cols)}, updated_at) VALUES "
            f"({', '.join('?' for _ in summary_cols)}, NOW())",
            summary_vals,
        )

    holding_cols = (
        "code", "name", "shares", "available", "price", "cost", "market_value",
        "pnl", "pnl_pct", "daily_pnl", "strategy", "bucket", "base_shares",
        "source", "verified",
    )
    rows = []
    for h in holdings:
        rows.append(tuple([
            h.get("code"),
            h.get("name"),
            int(h.get("shares") or 0),
            h.get("available"),
            h.get("price"),
            h.get("cost"),
            h.get("market_value"),
            h.get("pnl"),
            h.get("pnl_pct"),
            h.get("daily_pnl"),
            h.get("strategy"),
            h.get("bucket"),
            int(h.get("base_shares") or 0),
            h.get("source"),
            1 if h.get("verified") else 0,
        ]))
    if not rows:
        return
    placeholders = ", ".join("?" for _ in holding_cols)
    _exec(
        "INSERT INTO holdings_current "
        f"({', '.join(holding_cols)}, updated_at) VALUES ({placeholders}, NOW())",
        rows,
    )


def load_holdings_current() -> dict | None:
    """从数据库读取当前持仓快照（与旧 positions_latest.json 结构兼容）。"""
    rows = _exec(
        "SELECT code, name, shares, available, price, cost, market_value, pnl, "
        "pnl_pct, daily_pnl, strategy, bucket, base_shares, source, verified "
        "FROM holdings_current ORDER BY market_value DESC"
    ).fetchall()
    if not rows:
        return None
    holdings = []
    for row in rows:
        r = _row_to_json(dict(row))
        holdings.append({
            "code": r.get("code"),
            "name": r.get("name"),
            "shares": r.get("shares"),
            "available": r.get("available"),
            "price": r.get("price"),
            "cost": r.get("cost"),
            "market_value": r.get("market_value"),
            "pnl": r.get("pnl"),
            "pnl_pct": r.get("pnl_pct"),
            "daily_pnl": r.get("daily_pnl"),
            "strategy": r.get("strategy"),
            "bucket": r.get("bucket"),
            "base_shares": r.get("base_shares"),
            "source": r.get("source"),
            "verified": r.get("verified"),
        })
    summary_row = _exec(
        "SELECT total_assets, securities_value, available_cash, withdrawable, "
        "position_ratio, daily_pnl, total_pnl, source, updated_at "
        "FROM account_summary_current ORDER BY id DESC LIMIT 1"
    ).fetchone()
    summary = _row_to_json(dict(summary_row)) if summary_row else {}
    return {
        "date": summary.get("updated_at"),
        "source": summary.get("source"),
        "account_summary": summary,
        "holdings": holdings,
    }


def append_parse_history(
    parse_updated_at: str,
    source: str,
    holdings_count: int,
    trades_count: int,
    payload: dict,
) -> int:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    cursor = _exec(
        "INSERT INTO parse_history "
        "(parse_updated_at, source, holdings_count, trades_count, payload, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, NOW(), NOW())",
        (
            parse_updated_at,
            source,
            holdings_count,
            trades_count,
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------

def append_log(ts: str, level: str, message: str) -> None:
    try:
        _exec(
            "INSERT INTO api_logs (ts, level, message, created_at, "
            "updated_at) VALUES (?, ?, ?, NOW(), NOW())",
            (ts, level, message[:2000]),
        )
    except Exception:
        pass  # 日志入库失败不影响主流程


def recent_logs(limit: int = 100, level: str | None = None) -> list[dict]:
    if level:
        rows = _exec(
            "SELECT ts, level, message FROM api_logs "
            "WHERE level = ? ORDER BY id DESC LIMIT ?",
            (level, limit),
        ).fetchall()
    else:
        rows = _exec(
            "SELECT ts, level, message FROM api_logs "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def cleanup_old_logs(days: int = 7) -> int:
    """删除超过 days 天的业务日志（api_logs），返回删除条数。"""
    days = max(1, int(days))
    cutoff = (datetime.now() - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return _exec(
        "DELETE FROM api_logs WHERE created_at < ?", (cutoff,)
    ).rowcount


def append_signal_history(
    as_of: str,
    pool: str,
    momentum: int,
    status: str,
    items: list,
    selected_code: str | None,
    selected_name: str | None,
    rotation: dict | None,
    payload: dict,
) -> int:
    """写入/更新信号历史（每天每池每周期一条，upsert）。"""
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    items_blob = json.dumps(items, ensure_ascii=False)
    rotation_blob = json.dumps(rotation, ensure_ascii=False) if rotation else None
    payload_blob = json.dumps(payload, ensure_ascii=False)
    cursor = _exec(
        "INSERT INTO signal_history "
        "(as_of, pool, momentum, status, items, selected_code, selected_name, "
        "rotation, payload, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NOW(), NOW()) "
        "ON DUPLICATE KEY UPDATE status = VALUES(status), "
        "items = VALUES(items), selected_code = VALUES(selected_code), "
        "selected_name = VALUES(selected_name), rotation = VALUES(rotation), "
        "payload = VALUES(payload), updated_at = NOW()",
        (
            as_of, pool, momentum, status, items_blob,
            selected_code, selected_name, rotation_blob, payload_blob,
        ),
    )
    return int(cursor.lastrowid)


def recent_signal_history(limit: int = 50) -> list[dict]:
    rows = _exec(
        "SELECT * FROM signal_history ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for row in rows:
        item = _row_to_json(dict(row))
        for key in ("items", "rotation", "payload"):
            try:
                item[key] = json.loads(item[key])
            except (json.JSONDecodeError, TypeError):
                pass
        out.append(item)
    return out


def append_grid_trigger(
    code: str,
    name: str | None,
    trigger_date: str,
    trade_time: str,
    action: str,
    price: float,
    shares: int,
    trigger_type: str = "grid",
    amount: float | None = None,
    base_price_before: float | None = None,
    base_price_after: float | None = None,
    source: str = "manual",
) -> str:
    """写入一条网格触发记录（trigger_date 存储 YYYY-MM-DD HH:MM:SS，
    按 时间+标的+金额+数量 去重），返回 inserted/duplicate。"""
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    trigger_type = (trigger_type or "grid").strip() or "grid"
    trigger_date = (trigger_date or "").strip()
    trade_time = (trade_time or "").strip()
    if trigger_date and " " not in trigger_date:
        trigger_date = f"{trigger_date} {trade_time or '00:00:00'}"
    price_f = float(price)
    shares_i = int(shares)
    amount_f = round(price_f * shares_i, 4) if amount is None else round(float(amount), 4)
    exists = _exec(
        "SELECT id FROM grid_triggers WHERE code = ? AND trigger_date = ? "
        "AND amount = ? AND shares = ? LIMIT 1",
        (code, trigger_date, amount_f, shares_i),
    ).fetchone()
    if exists is not None:
        return "duplicate"
    _exec(
        "INSERT INTO grid_triggers "
        "(code, name, trigger_date, action, trigger_type, price, amount, shares, "
        "base_price_before, base_price_after, source, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NOW(), NOW())",
        (code, name, trigger_date, action, trigger_type, price_f, amount_f, shares_i,
         base_price_before, base_price_after, source),
    )
    # 持仓联动：新增触发记录后同步更新 holdings_current（买增卖减）
    try:
        _sync_holding_after_grid_trigger(code, action, price_f, shares_i)
    except Exception as exc:
        logging.getLogger("momentum-dashboard").warning(
            "GRID 触发后持仓联动失败 code=%s: %s", code, exc
        )
    # 配置联动：base_price 更新为触发后基准价，并按价格区间重算层数
    try:
        _sync_grid_config_after_trigger(code, base_price_after)
    except Exception as exc:
        logging.getLogger("momentum-dashboard").warning(
            "GRID 触发后配置联动失败 code=%s: %s", code, exc
        )
    return "inserted"


def recent_grid_triggers(code: str | None = None, limit: int = 200) -> list[dict]:
    if code:
        rows = _exec(
            "SELECT * FROM grid_triggers WHERE code = ? ORDER BY id DESC LIMIT ?",
            (code, limit),
        ).fetchall()
    else:
        rows = _exec(
            "SELECT * FROM grid_triggers ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_json(dict(row)) for row in rows]


def grid_triggers_for_code(code: str) -> list[dict]:
    """按时间升序返回某标的全部触发记录（用于基准价链式计算）。"""
    rows = _exec(
        "SELECT * FROM grid_triggers WHERE code = ? "
        "ORDER BY trigger_date ASC, id ASC",
        (code,),
    ).fetchall()
    return [_row_to_json(dict(row)) for row in rows]


_GRID_CONFIG_COLS = (
    "code", "name", "strategy_type", "base_price", "spacing_up_pct",
    "spacing_down_pct", "price_low", "price_high", "order_type_sell",
    "order_type_buy", "shares_per_grid", "base_position", "max_position",
    "levels_above", "levels_below", "status", "note", "source",
)


def save_grid_configs(configs: list[dict]) -> int:
    """全量 upsert 网格交易配置（按 code 唯一）。"""
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    rows = []
    for config in configs or []:
        rows.append(tuple(
            (config.get("status") or "active")
            if key == "status"
            else (config.get("strategy_type") or "网格交易")
            if key == "strategy_type"
            else config.get(key)
            for key in _GRID_CONFIG_COLS
        ))
    if not rows:
        return 0
    placeholders = ", ".join("?" for _ in _GRID_CONFIG_COLS)
    update_cols = [key for key in _GRID_CONFIG_COLS if key != "code"]
    updates = ", ".join(f"{key}=new.{key}" for key in update_cols)
    cursor = _exec(
        "INSERT INTO grid_configs "
        f"({', '.join(_GRID_CONFIG_COLS)}, created_at, updated_at) "
        f"VALUES ({placeholders}, NOW(), NOW()) AS new "
        f"ON DUPLICATE KEY UPDATE {updates}, updated_at=NOW()",
        rows,
    )
    return int(cursor.rowcount)


def load_grid_configs() -> list[dict]:
    """读取全部网格交易配置。"""
    rows = _exec(
        f"SELECT {', '.join(_GRID_CONFIG_COLS)} FROM grid_configs ORDER BY code"
    ).fetchall()
    return [_row_to_json(dict(row)) for row in rows]


def get_grid_config(code: str) -> dict | None:
    """读取单个标的的网格交易配置。"""
    row = _exec(
        f"SELECT {', '.join(_GRID_CONFIG_COLS)} FROM grid_configs WHERE code = ?",
        (code,),
    ).fetchone()
    return _row_to_json(dict(row)) if row else None


_MOMENTUM_POOL_COLS = (
    "pool_key", "pool_type", "description", "codes", "defensive_code",
    "is_recommended", "sort_order", "enabled",
)


def save_momentum_pools(pools: list[dict]) -> int:
    """全量 upsert 动量池配置（按 pool_key 唯一）。"""
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    rows = []
    for pool in pools or []:
        rows.append(tuple(
            (pool.get("pool_type") or "signal")
            if key == "pool_type"
            else (1 if pool.get(key) else 0)
            if key in ("is_recommended", "enabled")
            else int(pool.get(key) or 0)
            if key == "sort_order"
            else pool.get(key)
            for key in _MOMENTUM_POOL_COLS
        ))
    if not rows:
        return 0
    placeholders = ", ".join("?" for _ in _MOMENTUM_POOL_COLS)
    update_cols = [key for key in _MOMENTUM_POOL_COLS if key != "pool_key"]
    updates = ", ".join(f"{key}=new.{key}" for key in update_cols)
    cursor = _exec(
        "INSERT INTO momentum_pools "
        f"({', '.join(_MOMENTUM_POOL_COLS)}, created_at, updated_at) "
        f"VALUES ({placeholders}, NOW(), NOW()) AS new "
        f"ON DUPLICATE KEY UPDATE {updates}, updated_at=NOW()",
        rows,
    )
    return int(cursor.rowcount)


def load_momentum_pools() -> list[dict]:
    """读取全部动量池配置（按 sort_order 排序）。"""
    rows = _exec(
        f"SELECT {', '.join(_MOMENTUM_POOL_COLS)} FROM momentum_pools "
        "ORDER BY sort_order ASC, pool_key ASC"
    ).fetchall()
    return [_row_to_json(dict(row)) for row in rows]


def _sync_holding_after_grid_trigger(
    code: str, action: str, price: float, shares: int
) -> None:
    """触发记录新增后联动更新 holdings_current：买增卖减，
    按成交价刷新现价/加权成本/市值/浮动盈亏。"""
    row = _exec(
        "SELECT * FROM holdings_current WHERE code = ?", (code,)
    ).fetchone()
    if row is None:
        return
    h = dict(row)
    cur_shares = int(h.get("shares") or 0)
    cur_available = h.get("available")
    cur_cost = (
        float(h.get("cost")) if h.get("cost") is not None else None
    )
    is_buy = "buy" in str(action).lower() or "买" in str(action)
    shares_delta = int(shares)
    if is_buy:
        new_shares = cur_shares + shares_delta
        if cur_cost is not None and cur_shares > 0:
            new_cost = (cur_cost * cur_shares + float(price) * shares_delta) / new_shares
        else:
            new_cost = float(price)
        new_available = (
            cur_available + shares_delta
            if cur_available is not None
            else None
        )
    else:
        new_shares = max(cur_shares - shares_delta, 0)
        new_cost = cur_cost
        new_available = (
            max(cur_available - shares_delta, 0)
            if cur_available is not None
            else None
        )
    price_now = float(price)
    new_mv = round(new_shares * price_now, 3)
    if new_shares <= 0:
        # 已清仓：浮动盈亏与盈亏率归 0
        new_pnl = 0.0
        new_pnl_pct = 0.0
    elif new_cost and new_cost > 0:
        new_pnl = round((price_now - new_cost) * new_shares, 3)
        new_pnl_pct = round((price_now / new_cost - 1) * 100, 3)
    else:
        new_pnl = None
        new_pnl_pct = None
    _exec(
        "UPDATE holdings_current SET shares=?, available=?, price=?, cost=?, "
        "market_value=?, pnl=?, pnl_pct=?, updated_at=NOW() WHERE code=?",
        (
            new_shares, new_available, price_now, new_cost,
            new_mv, new_pnl, new_pnl_pct, code,
        ),
    )


def _sync_grid_config_after_trigger(code: str, base_price_after) -> None:
    """触发后同步网格配置：base_price 更新为触发后基准价，
    并按固定价格区间（price_low/price_high）反推重算 levels_above/levels_below。
    层级公式与 grid_trading.calc_grid_levels 一致（复利式）。"""
    if base_price_after is None:
        return
    config = get_grid_config(code)
    if not config:
        return
    import math

    base = float(base_price_after)
    up = config.get("spacing_up_pct")
    down = config.get("spacing_down_pct")
    low = config.get("price_low")
    high = config.get("price_high")
    levels_above = config.get("levels_above")
    levels_below = config.get("levels_below")

    if up and high is not None and base > 0:
        high_f = float(high)
        if high_f > base:
            ratio = math.log(high_f / base) / math.log(1 + float(up) / 100)
            levels_above = max(0, int(round(ratio)))
        else:
            levels_above = 0
    if down and low is not None and base > 0:
        low_f = float(low)
        if low_f < base:
            ratio = math.log(low_f / base) / math.log(1 - float(down) / 100)
            levels_below = max(0, int(round(ratio)))
        else:
            levels_below = 0

    _exec(
        "UPDATE grid_configs SET base_price=?, levels_above=?, levels_below=?, "
        "updated_at=NOW() WHERE code=?",
        (base, levels_above, levels_below, code),
    )


def update_grid_trigger_base_prices(
    record_id: int, base_before, base_after
) -> None:
    """更新单条记录的基准价变化字段。"""
    _exec(
        "UPDATE grid_triggers SET base_price_before = ?, base_price_after = ? "
        "WHERE id = ?",
        (base_before, base_after, record_id),
    )


def query_grid_triggers(
    code: str | None = None,
    start: str | None = None,
    end: str | None = None,
    trigger_type: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """按标的/日期区间/触发类型查询触发记录（按触发日期倒序）。"""
    sql = "SELECT * FROM grid_triggers WHERE 1 = 1"
    params = []
    if code:
        sql += " AND code = ?"
        params.append(code)
    if trigger_type:
        sql += " AND trigger_type = ?"
        params.append(trigger_type)
    if start:
        sql += " AND trigger_date >= ?"
        params.append(start)
    if end:
        end_value = end if " " in end else f"{end} 23:59:59"
        sql += " AND trigger_date <= ?"
        params.append(end_value)
    sql += " ORDER BY trigger_date DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 2000)))
    rows = _exec(sql, tuple(params)).fetchall()
    return [_row_to_json(dict(row)) for row in rows]


def upsert_backtest_result(
    kind: str,
    params_key: str,
    params: dict,
    summary: dict,
    payload: dict,
) -> int:
    """按 (kind, params_key) 幂等写入回测/寻优结果。"""
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    params_blob = json.dumps(params, ensure_ascii=False)
    summary_blob = json.dumps(summary, ensure_ascii=False)
    payload_blob = json.dumps(payload, ensure_ascii=False)
    cursor = _exec(
        "INSERT INTO backtest_results "
        "(kind, params_key, params, summary, payload, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, NOW(), NOW()) "
        "ON DUPLICATE KEY UPDATE params = VALUES(params), "
        "summary = VALUES(summary), payload = VALUES(payload), "
        "updated_at = NOW()",
        (kind, params_key, params_blob, summary_blob, payload_blob),
    )
    return int(cursor.lastrowid)


def get_backtest_result(kind: str, params_key: str) -> dict | None:
    row = _exec(
        "SELECT params, summary, payload FROM backtest_results "
        "WHERE kind = ? AND params_key = ?",
        (kind, params_key),
    ).fetchone()
    if row is None:
        return None
    try:
        return {
            "params": json.loads(row["params"]),
            "summary": json.loads(row["summary"]),
            "payload": json.loads(row["payload"]),
        }
    except (json.JSONDecodeError, TypeError):
        return None


def latest_backtest_result(kind: str) -> dict | None:
    """返回该 kind 最近一次写入的回测结果（按 updated_at 倒序）。"""
    rows = _exec(
        "SELECT params_key, params, summary, payload, updated_at "
        "FROM backtest_results WHERE kind = ? "
        "ORDER BY updated_at DESC LIMIT 1",
        (kind,),
    ).fetchall()
    if not rows:
        return None
    row = _row_to_json(dict(rows[0]))
    try:
        return {
            "params_key": row.get("params_key"),
            "params": json.loads(row.get("params") or "{}"),
            "summary": json.loads(row.get("summary") or "{}"),
            "payload": json.loads(row.get("payload") or "{}"),
            "updated_at": row.get("updated_at"),
        }
    except (json.JSONDecodeError, TypeError):
        return None


def list_backtest_results(
    kind: str = "backtest", limit: int = 10, offset: int = 0
) -> tuple[list[dict], int]:
    """分页返回最近的回测/寻优结果（不含 payload）+ 总条数。"""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    total_row = _exec(
        "SELECT COUNT(*) AS n FROM backtest_results WHERE kind = ?",
        (kind,),
    ).fetchone()
    total = int(total_row["n"]) if total_row else 0
    rows = _exec(
        "SELECT params_key, params, summary, updated_at FROM backtest_results "
        "WHERE kind = ? ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
        (kind, limit, offset),
    ).fetchall()
    items = []
    for row in rows:
        item = _row_to_json(dict(row))
        try:
            params = json.loads(item.get("params") or "{}")
            summary = json.loads(item.get("summary") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        items.append(
            {
                "params_key": item.get("params_key"),
                "params": params,
                "summary": summary,
                "updated_at": item.get("updated_at"),
            }
        )
    return items, total


def latest_grid_opt_result(code: str) -> dict | None:
    """返回该标的最新的网格寻优结果（kind='grid_opt'，含最优参数与时间）。"""
    rows = _exec(
        "SELECT params, summary, updated_at FROM backtest_results "
        "WHERE kind = 'grid_opt' AND params_key LIKE ? "
        "ORDER BY id DESC LIMIT 1",
        (f"{code}|%",),
    ).fetchall()
    if not rows:
        return None
    row = _row_to_json(dict(rows[0]))
    try:
        summary = json.loads(row.get("summary") or "{}")
    except (json.JSONDecodeError, TypeError):
        summary = {}
    best = (summary or {}).get(code)
    if not best:
        return None
    return {
        "best": best,
        "params": json.loads(row.get("params") or "{}"),
        "updated_at": row.get("updated_at"),
    }


def append_scheduler_run(
    run_type: str,
    started_at: str,
    finished_at: str,
    duration_ms: int,
    result: str,
    detail: dict,
    email_sent: bool | None,
) -> int:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    detail_blob = json.dumps(detail, ensure_ascii=False)
    email = 1 if email_sent else (0 if email_sent is False else None)
    cursor = _exec(
        "INSERT INTO scheduler_runs "
        "(run_type, started_at, finished_at, duration_ms, result, detail, "
        "email_sent, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NOW(), NOW())",
        (run_type, started_at, finished_at, duration_ms, result, detail_blob, email),
    )
    return int(cursor.lastrowid)


def recent_scheduler_runs(limit: int = 50) -> list[dict]:
    rows = _exec(
        "SELECT * FROM scheduler_runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for row in rows:
        item = _row_to_json(dict(row))
        try:
            item["detail"] = json.loads(item["detail"])
        except (json.JSONDecodeError, TypeError):
            pass
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# stats / 表浏览
# ---------------------------------------------------------------------------

def _table_columns(table: str) -> list[dict]:
    """返回列信息 [{name, type, comment}]。"""
    rows = _exec(
        "SELECT column_name AS name, column_type AS type, "
        "column_comment AS comment "
        "FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = ? "
        "ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [dict(row) for row in rows]


def list_tables() -> list[dict]:
    """列出业务表：名称、行数、列名、列信息（含注释）。"""
    names = [
        row["name"]
        for row in _exec(
            "SELECT table_name AS name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name IN "
            "('cache','positions_snapshots','parse_history','api_logs') "
            "ORDER BY table_name"
        ).fetchall()
    ]
    tables = []
    for name in names:
        count = _count(name)
        columns = _table_columns(name)
        tables.append(
            {
                "name": name,
                "count": count,
                "columns": [col["name"] for col in columns],
                "columns_detail": columns,
            }
        )
    return tables


def table_rows(table: str, limit: int = 100, offset: int = 0) -> dict:
    """读取指定表的分页数据（按 id 倒序，只允许白名单表）。"""
    if table not in ALLOWED_TABLES:
        raise ValueError(f"不允许访问表: {table}")
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    columns = _table_columns(table)
    rows = _exec(
        f"SELECT * FROM {_quote_ident(table)} ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    total = _count(table)
    return {
        "table": table,
        "columns": [col["name"] for col in columns],
        "columns_detail": columns,
        "rows": [_row_to_json(dict(row)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def stats() -> dict:
    counts = {}
    for table in ALLOWED_TABLES:
        counts[table] = _count(table)
    row = _exec(
        "SELECT COALESCE(SUM(data_length + index_length), 0) AS size "
        "FROM information_schema.tables WHERE table_schema = DATABASE()"
    ).fetchone()
    size = int(row["size"])
    info = db_info()
    latest = latest_positions_snapshot()
    return {
        "db_info": info,
        "size_bytes": size,
        "tables": counts,
        "latest_snapshot": latest and latest.get("date"),
    }
