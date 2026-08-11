#!/usr/bin/env python3
"""数据访问层（当前后端 MySQL，保留 SQLite 供测试/离线开发）。

本模块是整个系统**唯一**包含 SQL / 直接操作数据库的代码层：
业务代码（server.py / positions_parser.py / scheduler.py 等）只能通过
本模块的函数读写数据，不得出现 SQL 或数据库驱动调用。

后端切换：
  - MySQL（默认，生产）：依赖 PyMySQL（pip install PyMySQL），连接参数来自
    环境变量 DB_BACKEND=mysql / DB_HOST / DB_PORT / DB_USER / DB_PASSWORD /
    DB_NAME（可写入项目根目录 .env）。
  - SQLite（测试/离线）：DB_BACKEND=sqlite，使用 ROOT/dashboard.db。

表结构（MySQL DDL 见 schema_mysql.sql）：四张表均含
  id BIGINT 主键 / created_at 创建时间 / updated_at 更新时间 / 字段注释。
从 SQLite 迁移旧数据见 `migrate_from_sqlite()`。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "dashboard.db"


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


# 后端配置（默认 MySQL；环境变量可覆盖）
_backend_name = (os.environ.get("DB_BACKEND") or "mysql").strip().lower()
_MYSQL_HOST = os.environ.get("DB_HOST", "127.0.0.1")
_MYSQL_PORT = int(os.environ.get("DB_PORT", "3306"))
_MYSQL_USER = os.environ.get("DB_USER", "invest")
_MYSQL_PASSWORD = os.environ.get("DB_PASSWORD", "invest123")
_MYSQL_DB = os.environ.get("DB_NAME", "ai_invest")

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None          # SQLite 单连接
_local = threading.local()                        # MySQL 线程本地连接
_migrated = False

ALLOWED_TABLES = (
    "cache",
    "positions_snapshots",
    "parse_history",
    "api_logs",
    "signal_history",
    "grid_triggers",
    "backtest_results",
    "scheduler_runs",
)


def configure(backend: str) -> str:
    """切换后端（"mysql" / "sqlite"），返回生效后端。测试用。"""
    global _backend_name, _conn, _migrated
    backend = (backend or "").strip().lower()
    if backend not in ("mysql", "sqlite"):
        raise ValueError(f"未知数据后端: {backend}（可选 mysql/sqlite）")
    with _lock:
        _backend_name = backend
        _conn = None
        _local = threading.local()
        _migrated = False
    return backend


def db_backend() -> str:
    """返回当前数据后端标识。"""
    return _backend_name


def db_info() -> dict:
    """返回当前后端连接信息（不含密码）。"""
    if _backend_name == "mysql":
        return {
            "backend": "mysql",
            "host": _MYSQL_HOST,
            "port": _MYSQL_PORT,
            "database": _MYSQL_DB,
            "user": _MYSQL_USER,
        }
    return {"backend": "sqlite", "db_path": str(DB_PATH)}


# ---------------------------------------------------------------------------
# 连接管理
# ---------------------------------------------------------------------------

def _connect_mysql():
    import pymysql
    return pymysql.connect(
        host=_MYSQL_HOST,
        port=_MYSQL_PORT,
        user=_MYSQL_USER,
        password=_MYSQL_PASSWORD,
        database=_MYSQL_DB,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def get_conn():
    """返回当前后端连接（SQLite 单连接 / MySQL 每线程一连接）。"""
    if _backend_name == "sqlite":
        global _conn
        if _conn is None:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _init_schema(_conn)
            _maybe_migrate()
        return _conn
    conn = getattr(_local, "conn", None)
    if conn is None or not conn.open:
        conn = _connect_mysql()
        _local.conn = conn
        with _lock:
            _init_schema(conn)
        _maybe_migrate()
    return conn


def _exec(sql: str, params=()):
    """统一执行入口：MySQL 下把 ? 占位符翻译为 %s，写操作自动提交。"""
    if _backend_name == "mysql":
        mysql_sql = sql.replace("?", "%s")
        with _lock:
            conn = get_conn()
            cursor = conn.cursor()
            try:
                if params and isinstance(params, (list, tuple)) and isinstance(
                    params[0], (list, tuple)
                ):
                    cursor.executemany(mysql_sql, params)
                else:
                    cursor.execute(mysql_sql, params)
                conn.commit()
                return cursor
            except Exception:
                conn.rollback()
                raise
    with _lock:
        conn = get_conn()
        if params and isinstance(params, (list, tuple)) and isinstance(
            params[0], (list, tuple)
        ):
            cursor = conn.executemany(sql, params)
        else:
            cursor = conn.execute(sql, params)
        conn.commit()
        return cursor


def _exec_script(sql_script: str) -> None:
    """按分号切分执行多条 DDL（兼容 sqlite3.executescript 与 PyMySQL）。"""
    statements = [s.strip() for s in sql_script.split(";") if s.strip()]
    for statement in statements:
        if statement:
            _exec(statement)


def _count(table: str) -> int:
    row = _exec(f"SELECT COUNT(*) AS n FROM {_quote_ident(table)}").fetchone()
    return int(row["n"])


def _quote_ident(name: str) -> str:
    """按后端返回标识符引用：MySQL 用反引号，SQLite 用双引号。"""
    if _backend_name == "mysql":
        return f"`{name}`"
    return f'"{name}"'


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
# 建表（MySQL DDL 与 schema_mysql.sql 保持一致；SQLite 仅供测试/离线）
# ---------------------------------------------------------------------------

_MYSQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
  id         BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  cache_key  VARCHAR(255) NOT NULL                COMMENT '缓存键',
  payload    MEDIUMTEXT   NOT NULL                COMMENT '缓存内容(JSON)',
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_cache_key (cache_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='API结果缓存';

CREATE TABLE IF NOT EXISTS positions_snapshots (
  id         BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  date       VARCHAR(32) DEFAULT NULL            COMMENT '快照日期',
  source     VARCHAR(64) DEFAULT NULL            COMMENT '数据来源',
  payload    MEDIUMTEXT  NOT NULL                COMMENT '完整持仓快照(JSON)',
  created_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  KEY idx_snapshots_date (date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='持仓快照';

CREATE TABLE IF NOT EXISTS parse_history (
  id             BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  parse_updated_at VARCHAR(32) DEFAULT NULL          COMMENT '解析更新时间',
  source         VARCHAR(64) DEFAULT NULL            COMMENT '解析来源',
  holdings_count INT         DEFAULT NULL            COMMENT '持仓数量',
  trades_count   INT         DEFAULT NULL            COMMENT '交易笔数',
  payload        MEDIUMTEXT  DEFAULT NULL            COMMENT '解析结果(JSON)',
  created_at     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI持仓解析历史';

CREATE TABLE IF NOT EXISTS api_logs (
  id         BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  ts         VARCHAR(32) NOT NULL                COMMENT '日志时间(ISO8601)',
  level      VARCHAR(16) DEFAULT NULL            COMMENT '日志级别(INFO/WARN/ERROR)',
  message    VARCHAR(2000) DEFAULT NULL          COMMENT '日志内容',
  created_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  KEY idx_logs_ts (ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='业务日志';

CREATE TABLE IF NOT EXISTS signal_history (
  id            BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  as_of         VARCHAR(32) DEFAULT NULL            COMMENT '信号日期',
  pool          VARCHAR(64) DEFAULT NULL            COMMENT '信号池',
  momentum      INT         DEFAULT NULL            COMMENT 'RSRS动量周期(日)',
  status        VARCHAR(32) DEFAULT NULL            COMMENT '扫描状态',
  items         MEDIUMTEXT  DEFAULT NULL            COMMENT '各标的信号(JSON)',
  selected_code VARCHAR(16) DEFAULT NULL            COMMENT '目标标的代码',
  selected_name VARCHAR(64) DEFAULT NULL            COMMENT '目标标的名称',
  rotation      MEDIUMTEXT  DEFAULT NULL            COMMENT '轮动动作(JSON)',
  payload       MEDIUMTEXT  DEFAULT NULL            COMMENT '完整信号结果(JSON)',
  created_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_signal_day (as_of, pool, momentum),
  KEY idx_signal_pool (pool)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='信号扫描/轮动历史';

CREATE TABLE IF NOT EXISTS grid_triggers (
  id                BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  code              VARCHAR(16)  NOT NULL                COMMENT 'ETF代码',
  name              VARCHAR(64)  DEFAULT NULL            COMMENT 'ETF名称',
  trigger_date      VARCHAR(32)  NOT NULL DEFAULT ''     COMMENT '触发日期时间(YYYY-MM-DD HH:MM:SS)',
  action            VARCHAR(16)  DEFAULT NULL            COMMENT '买入/卖出',
  trigger_type      VARCHAR(16)  NOT NULL DEFAULT 'grid' COMMENT '触发类型(grid/add/reduce/momentum)',
  price             DECIMAL(12,4) DEFAULT NULL           COMMENT '成交价',
  amount            DECIMAL(16,4) NOT NULL DEFAULT 0     COMMENT '成交金额(价格×数量)',
  shares            INT          DEFAULT NULL            COMMENT '数量(份)',
  base_price_before DECIMAL(12,4) DEFAULT NULL           COMMENT '触发前基准价',
  base_price_after  DECIMAL(12,4) DEFAULT NULL           COMMENT '触发后基准价',
  source            VARCHAR(32)  DEFAULT NULL            COMMENT '来源(文件/手工/策略)',
  created_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_trigger (code, trigger_date, amount, shares),
  KEY idx_triggers_code_date (code, trigger_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='网格触发记录';

CREATE TABLE IF NOT EXISTS backtest_results (
  id         BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  kind       VARCHAR(32)  NOT NULL                COMMENT '类型(backtest/enum/screener/grid_opt)',
  params_key VARCHAR(255) NOT NULL                COMMENT '参数指纹(唯一)',
  params     MEDIUMTEXT   DEFAULT NULL            COMMENT '参数(JSON)',
  summary    MEDIUMTEXT   DEFAULT NULL            COMMENT '摘要指标(JSON)',
  payload    MEDIUMTEXT   DEFAULT NULL            COMMENT '完整结果(JSON)',
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_result_kind_key (kind, params_key),
  KEY idx_result_kind (kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='回测/寻优结果';

CREATE TABLE IF NOT EXISTS scheduler_runs (
  id          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  run_type    VARCHAR(32)  DEFAULT NULL            COMMENT '触发类型(schedule/manual)',
  started_at  VARCHAR(32)  DEFAULT NULL            COMMENT '开始时间',
  finished_at VARCHAR(32)  DEFAULT NULL            COMMENT '结束时间',
  duration_ms INT          DEFAULT NULL            COMMENT '耗时(毫秒)',
  result      VARCHAR(32)  DEFAULT NULL            COMMENT '结果(ok/error)',
  detail      MEDIUMTEXT   DEFAULT NULL            COMMENT '执行详情(JSON)',
  email_sent  TINYINT(1)   DEFAULT NULL            COMMENT '邮件是否发送',
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  KEY idx_runs_started (started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='定时任务执行历史';
"""

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  key        TEXT UNIQUE NOT NULL,
  payload    TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS positions_snapshots (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  date       TEXT,
  source     TEXT,
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parse_history (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  parse_updated_at TEXT,
  source         TEXT,
  holdings_count INTEGER,
  trades_count   INTEGER,
  payload        TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_logs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  level      TEXT,
  message    TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_history (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of         TEXT,
  pool          TEXT,
  momentum      INTEGER,
  status        TEXT,
  items         TEXT,
  selected_code TEXT,
  selected_name TEXT,
  rotation      TEXT,
  payload       TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (as_of, pool, momentum)
);

CREATE TABLE IF NOT EXISTS grid_triggers (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  code              TEXT NOT NULL,
  name              TEXT,
  trigger_date      TEXT NOT NULL DEFAULT '',
  action            TEXT,
  trigger_type      TEXT NOT NULL DEFAULT 'grid',
  price             REAL,
  amount            REAL NOT NULL DEFAULT 0,
  shares            INTEGER,
  base_price_before REAL,
  base_price_after  REAL,
  source            TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  UNIQUE (code, trigger_date, amount, shares)
);

CREATE TABLE IF NOT EXISTS backtest_results (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL,
  params_key TEXT NOT NULL,
  params     TEXT,
  summary    TEXT,
  payload    TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (kind, params_key)
);

CREATE TABLE IF NOT EXISTS scheduler_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  run_type    TEXT,
  started_at  TEXT,
  finished_at TEXT,
  duration_ms INTEGER,
  result      TEXT,
  detail      TEXT,
  email_sent  INTEGER,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_date ON positions_snapshots(date);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON api_logs(ts);
CREATE INDEX IF NOT EXISTS idx_signal_pool ON signal_history(pool);
CREATE INDEX IF NOT EXISTS idx_triggers_code_date ON grid_triggers(code, trigger_date);
CREATE INDEX IF NOT EXISTS idx_result_kind ON backtest_results(kind);
CREATE INDEX IF NOT EXISTS idx_runs_started ON scheduler_runs(started_at);
"""


def _init_schema(conn) -> None:
    if _backend_name == "mysql":
        _exec_script(_MYSQL_SCHEMA)
    else:
        conn.executescript(_SQLITE_SCHEMA)
        conn.commit()


def _maybe_migrate() -> None:
    global _migrated
    if _migrated:
        return
    with _lock:
        if _migrated:
            return
        try:
            migrate_from_sqlite()
        except Exception:
            pass  # 迁移失败不影响主流程
        _migrated = True


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

def cache_get(key: str, ttl: float | None = None):
    """按 TTL 读取缓存，命中返回 dict，未命中/过期返回 None。

    ttl=None 表示忽略过期时间（取旧缓存）；ttl=0 表示立即过期。
    """
    if _backend_name == "mysql":
        row = _exec(
            "SELECT payload, UNIX_TIMESTAMP(updated_at) AS updated_at "
            "FROM cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
    else:
        row = _exec(
            "SELECT payload, updated_at FROM cache WHERE key = ?", (key,)
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
    if _backend_name == "mysql":
        _exec(
            "INSERT INTO cache (cache_key, payload, created_at, updated_at) "
            "VALUES (?, ?, NOW(), NOW()) "
            "ON DUPLICATE KEY UPDATE payload = VALUES(payload), "
            "updated_at = NOW()",
            (key, blob),
        )
    else:
        now = time.time()
        _exec(
            "INSERT INTO cache (key, payload, created_at, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "payload = excluded.payload, updated_at = excluded.updated_at",
            (key, blob, now, now),
        )


def cache_delete_expired() -> int:
    """清理 7 天前的缓存行，返回删除条数。"""
    if _backend_name == "mysql":
        cutoff = (datetime.now() - timedelta(days=7)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return _exec(
            "DELETE FROM cache WHERE updated_at < ?", (cutoff,)
        ).rowcount
    cutoff = time.time() - 7 * 86400
    return _exec("DELETE FROM cache WHERE updated_at < ?", (cutoff,)).rowcount


def cache_flush() -> int:
    """清空缓存表（测试/维护用），返回删除条数。"""
    return _exec("DELETE FROM cache").rowcount


# ---------------------------------------------------------------------------
# positions / parse history
# ---------------------------------------------------------------------------

def save_positions_snapshot(snapshot: dict) -> int:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    if _backend_name == "mysql":
        cursor = _exec(
            "INSERT INTO positions_snapshots (date, source, payload, "
            "created_at, updated_at) VALUES (?, ?, ?, NOW(), NOW())",
            (
                snapshot.get("date"),
                snapshot.get("source"),
                json.dumps(snapshot, ensure_ascii=False),
            ),
        )
    else:
        cursor = _exec(
            "INSERT INTO positions_snapshots (date, source, payload, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                snapshot.get("date"),
                snapshot.get("source"),
                json.dumps(snapshot, ensure_ascii=False),
                now,
                now,
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


def append_parse_history(
    parse_updated_at: str,
    source: str,
    holdings_count: int,
    trades_count: int,
    payload: dict,
) -> int:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    if _backend_name == "mysql":
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
    else:
        cursor = _exec(
            "INSERT INTO parse_history "
            "(parse_updated_at, source, holdings_count, trades_count, payload, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                parse_updated_at,
                source,
                holdings_count,
                trades_count,
                json.dumps(payload, ensure_ascii=False),
                now,
                now,
            ),
        )
    return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------

def append_log(ts: str, level: str, message: str) -> None:
    try:
        if _backend_name == "mysql":
            _exec(
                "INSERT INTO api_logs (ts, level, message, created_at, "
                "updated_at) VALUES (?, ?, ?, NOW(), NOW())",
                (ts, level, message[:2000]),
            )
        else:
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            _exec(
                "INSERT INTO api_logs (ts, level, message, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?)",
                (ts, level, message[:2000], now, now),
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
    if _backend_name == "mysql":
        cutoff = (datetime.now() - timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return _exec(
            "DELETE FROM api_logs WHERE created_at < ?", (cutoff,)
        ).rowcount
    cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat(
        timespec="seconds"
    )
    return _exec(
        "DELETE FROM api_logs WHERE created_at < ?", (cutoff,)
    ).rowcount


# ---------------------------------------------------------------------------
# signal_history / grid_triggers / backtest_results / scheduler_runs
# ---------------------------------------------------------------------------

def _to_decimal(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
    if _backend_name == "mysql":
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
    else:
        cursor = _exec(
            "INSERT INTO signal_history "
            "(as_of, pool, momentum, status, items, selected_code, selected_name, "
            "rotation, payload, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(as_of, pool, momentum) DO UPDATE SET "
            "status = excluded.status, items = excluded.items, "
            "selected_code = excluded.selected_code, "
            "selected_name = excluded.selected_name, "
            "rotation = excluded.rotation, payload = excluded.payload, "
            "updated_at = excluded.updated_at",
            (
                as_of, pool, momentum, status, items_blob,
                selected_code, selected_name, rotation_blob, payload_blob,
                now, now,
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


def seed_grid_triggers_from_file(
    path, source: str = "file", name_map: dict | None = None
) -> int:
    """从 grid_triggers.json 幂等导入触发记录（按唯一键去重），返回写入条数。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(data, dict):
        return 0
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    rows = []
    for code, records in data.items():
        for record in records or []:
            if not isinstance(record, dict):
                continue
            name = (name_map or {}).get(code)
            price = _to_decimal(record.get("price"))
            shares = record.get("shares")
            try:
                shares_i = int(shares)
            except (TypeError, ValueError):
                shares_i = None
            amount = round(price * shares_i, 4) if price is not None and shares_i else 0
            date = str(record.get("date") or "").strip()
            trade_time = str(record.get("time") or "").strip()
            trigger_date = f"{date} {trade_time or '00:00:00'}" if date else ""
            rows.append(
                (
                    code,
                    name,
                    trigger_date,
                    record.get("action"),
                    record.get("type") or "grid",
                    price,
                    amount,
                    shares_i,
                    _to_decimal(record.get("base_price_before")),
                    _to_decimal(record.get("base_price_after")),
                    source,
                )
            )
    if not rows:
        return 0
    if _backend_name == "mysql":
        cursor = _exec(
            "INSERT IGNORE INTO grid_triggers "
            "(code, name, trigger_date, action, trigger_type, price, amount, shares, "
            "base_price_before, base_price_after, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NOW(), NOW())",
            rows,
        )
    else:
        rows_with_time = [tuple(row) + (now, now) for row in rows]
        cursor = _exec(
            "INSERT OR IGNORE INTO grid_triggers "
            "(code, name, trigger_date, action, trigger_type, price, amount, shares, "
            "base_price_before, base_price_after, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows_with_time,
        )
    return int(cursor.rowcount)


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
    if _backend_name == "mysql":
        _exec(
            "INSERT INTO grid_triggers "
            "(code, name, trigger_date, action, trigger_type, price, amount, shares, "
            "base_price_before, base_price_after, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NOW(), NOW())",
            (code, name, trigger_date, action, trigger_type, price_f, amount_f, shares_i,
             base_price_before, base_price_after, source),
        )
    else:
        _exec(
            "INSERT INTO grid_triggers "
            "(code, name, trigger_date, action, trigger_type, price, amount, shares, "
            "base_price_before, base_price_after, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (code, name, trigger_date, action, trigger_type, price_f, amount_f, shares_i,
             base_price_before, base_price_after, source, now, now),
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
    if _backend_name == "mysql":
        cursor = _exec(
            "INSERT INTO backtest_results "
            "(kind, params_key, params, summary, payload, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, NOW(), NOW()) "
            "ON DUPLICATE KEY UPDATE params = VALUES(params), "
            "summary = VALUES(summary), payload = VALUES(payload), "
            "updated_at = NOW()",
            (kind, params_key, params_blob, summary_blob, payload_blob),
        )
    else:
        cursor = _exec(
            "INSERT INTO backtest_results "
            "(kind, params_key, params, summary, payload, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(kind, params_key) DO UPDATE SET "
            "params = excluded.params, summary = excluded.summary, "
            "payload = excluded.payload, updated_at = excluded.updated_at",
            (kind, params_key, params_blob, summary_blob, payload_blob, now, now),
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
    if _backend_name == "mysql":
        cursor = _exec(
            "INSERT INTO scheduler_runs "
            "(run_type, started_at, finished_at, duration_ms, result, detail, "
            "email_sent, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NOW(), NOW())",
            (run_type, started_at, finished_at, duration_ms, result, detail_blob, email),
        )
    else:
        cursor = _exec(
            "INSERT INTO scheduler_runs "
            "(run_type, started_at, finished_at, duration_ms, result, detail, "
            "email_sent, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_type, started_at, finished_at, duration_ms, result, detail_blob,
             email, now, now),
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
    """返回列信息 [{name, type, comment}]（MySQL 有注释，SQLite 注释为空）。"""
    if _backend_name == "mysql":
        rows = _exec(
            "SELECT column_name AS name, column_type AS type, "
            "column_comment AS comment "
            "FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = ? "
            "ORDER BY ordinal_position",
            (table,),
        ).fetchall()
    else:
        rows = _exec(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
        return [
            {"name": row["name"], "type": row["type"] or "", "comment": ""}
            for row in rows
        ]
    return [dict(row) for row in rows]


def list_tables() -> list[dict]:
    """列出业务表：名称、行数、列名、列信息（含注释）。"""
    if _backend_name == "mysql":
        names = [
            row["name"]
            for row in _exec(
                "SELECT table_name AS name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name IN "
                "('cache','positions_snapshots','parse_history','api_logs') "
                "ORDER BY table_name"
            ).fetchall()
        ]
    else:
        conn = get_conn()
        names = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
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
    if _backend_name == "mysql":
        row = _exec(
            "SELECT COALESCE(SUM(data_length + index_length), 0) AS size "
            "FROM information_schema.tables WHERE table_schema = DATABASE()"
        ).fetchone()
        size = int(row["size"])
        info = db_info()
    else:
        size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        info = db_info()
    latest = latest_positions_snapshot()
    return {
        "db_info": info,
        "size_bytes": size,
        "tables": counts,
        "latest_snapshot": latest and latest.get("date"),
    }


# ---------------------------------------------------------------------------
# SQLite → MySQL 数据迁移（一次性、幂等）
# ---------------------------------------------------------------------------

def _to_mysql_dt(value) -> str | None:
    """把 ISO 时间字符串转 MySQL DATETIME；失败返回 None。"""
    if not value:
        return None
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def migrate_from_sqlite() -> dict:
    """后端为 MySQL 时，把旧 SQLite dashboard.db 的四张表数据迁入（幂等）。

    每张表仅在 MySQL 侧为空时迁移；返回 {表名: 迁移行数}。
    """
    if _backend_name != "mysql" or not DB_PATH.exists():
        return {}
    migrated: dict = {}
    src = sqlite3.connect(str(DB_PATH))
    src.row_factory = sqlite3.Row
    try:
        existing = {
            row["name"]
            for row in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table in ALLOWED_TABLES:
            if table not in existing:
                continue
            if _count(table) > 0:
                continue
            rows = src.execute(f'SELECT * FROM "{table}"').fetchall()
            if not rows:
                continue
            n = _copy_table(table, [dict(row) for row in rows])
            if n:
                migrated[table] = n
    finally:
        src.close()
    return migrated


def _copy_table(table: str, rows: list[dict]) -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if table == "cache":
        cursor = _exec(
            "INSERT INTO cache (cache_key, payload, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            [
                (row["key"], row["payload"], now, now)
                for row in rows
            ],
        )
    elif table == "positions_snapshots":
        cursor = _exec(
            "INSERT INTO positions_snapshots "
            "(id, date, source, payload, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    row["id"],
                    row.get("date"),
                    row.get("source"),
                    row["payload"],
                    _to_mysql_dt(row.get("created_at")) or now,
                    now,
                )
                for row in rows
            ],
        )
    elif table == "parse_history":
        cursor = _exec(
            "INSERT INTO parse_history "
            "(id, parse_updated_at, source, holdings_count, trades_count, payload, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row["id"],
                    row.get("updated_at") or row.get("parse_updated_at"),
                    row.get("source"),
                    row.get("holdings_count"),
                    row.get("trades_count"),
                    row.get("payload"),
                    _to_mysql_dt(row.get("created_at")) or now,
                    now,
                )
                for row in rows
            ],
        )
    elif table == "api_logs":
        cursor = _exec(
            "INSERT INTO api_logs "
            "(id, ts, level, message, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    row["id"],
                    row["ts"],
                    row.get("level"),
                    row.get("message"),
                    _to_mysql_dt(row.get("created_at")) or now,
                    now,
                )
                for row in rows
            ],
        )
    else:
        return 0
    return int(cursor.rowcount)
