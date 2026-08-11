#!/usr/bin/env python3
"""动量轮动策略仪表盘 — 本地 API 服务（Python 标准库，零第三方依赖）。

封装仓库内已有的动量轮动工具（信号扫描 / 策略监测 / 回测 / 审计 / 选品），
统一以 JSON API 提供给前端页面。数据统一走 db.py 数据访问层（SQLite）与
cache.py 缓存抽象层（默认 SQLite 后端），便于后续切换数据库 / 缓存工具。

用法:
    python3 momentum-dashboard/server.py
    python3 momentum-dashboard/server.py --port 8765 --host 127.0.0.1

打开 http://127.0.0.1:8765 访问仪表盘。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402 - 数据访问层（唯一含 SQL 的层，可整体切换数据库）
from cache import cached  # noqa: E402 - 缓存抽象层（后端默认 SQLite）
from cache import backend_name as cache_backend_name  # noqa: E402
from cache import get as cache_get  # noqa: E402
from cache import set as cache_set  # noqa: E402
from cache import set_logger as cache_set_logger  # noqa: E402

STATIC_DIR = ROOT / "static"
PROJECT_DIR = ROOT.parent
TOOLS_DIR = PROJECT_DIR / "tools"
DATA_DIR = PROJECT_DIR / "data"

LOG_FILE = ROOT / "server.log"

_LOGGER = logging.getLogger("momentum-dashboard")
_LOGGER.setLevel(logging.INFO)
_LOGGER.propagate = False
_LOG_FORMATTER = logging.Formatter(
    "[%(asctime)s] [%(levelname)-5s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_MAINT_STOP = threading.Event()
_DAY_LOG_RE = re.compile(r"^server-(\d{8})\.log$")


class _DailyDateFileHandler(logging.Handler):
    """按天命名的日志文件 server-YYYYMMDD.log：跨天自动切换，清理 7 天前旧文件。"""

    def __init__(self, log_dir, backup_days: int = 7, encoding: str = "utf-8"):
        super().__init__()
        self.log_dir = Path(log_dir)
        self.backup_days = max(1, int(backup_days))
        self.encoding = encoding
        self._current_date = None
        self._stream = None
        self._lock = threading.Lock()
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _filename_for(self, day):
        return self.log_dir / f"server-{day.strftime('%Y%m%d')}.log"

    def _cleanup_old(self):
        cutoff = (datetime.now() - timedelta(days=self.backup_days)).date()
        for path in self.log_dir.glob("server-*.log"):
            match = _DAY_LOG_RE.match(path.name)
            if not match:
                continue
            try:
                day = datetime.strptime(match.group(1), "%Y%m%d").date()
            except ValueError:
                continue
            if day < cutoff:
                try:
                    path.unlink()
                except OSError:
                    pass

    def _open(self, day):
        path = self._filename_for(day)
        self._stream = open(path, "a", encoding=self.encoding)
        self._current_date = day
        self._cleanup_old()

    def emit(self, record):
        today = datetime.now().date()
        with self._lock:
            if self._current_date != today:
                if self._stream:
                    try:
                        self._stream.close()
                    except OSError:
                        pass
                self._open(today)
            try:
                self._stream.write(self.format(record) + "\n")
                self._stream.flush()
            except Exception:
                pass

    def close(self):
        with self._lock:
            if self._stream:
                try:
                    self._stream.close()
                except OSError:
                    pass
                self._stream = None
        super().close()


def _init_logging():
    """初始化日志：终端 + server-YYYYMMDD.log（按天命名，保留 7 天自动删除）。"""
    if _LOGGER.handlers:
        return
    file_handler = _DailyDateFileHandler(ROOT, backup_days=7)
    file_handler.setFormatter(_LOG_FORMATTER)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(_LOG_FORMATTER)
    _LOGGER.addHandler(file_handler)
    _LOGGER.addHandler(stream_handler)


_init_logging()


def _load_env_file():
    """从项目根目录 .env 加载环境变量（模型 API Key 等，仅本地）。"""
    env_path = ROOT.parent / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


_load_env_file()


def _load_holdings_strategy() -> dict:
    """读取持仓策略归属配置（网格/动量双策略标的口径）。"""
    path = ROOT / "holdings_strategy.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# 池与预设配置（与 tools/ 保持一致，导入失败时使用内置兜底）
# ---------------------------------------------------------------------------

SIGNAL_POOLS = {
    "recommended": {
        "desc": "推荐 4-ETF（黄金+纳指+创业板+恒生）",
        "codes": ["518880", "513100", "159915", "159920"],
    },
    "full": {
        "desc": "全池 11 只（宽基/行业/跨境/防御）",
        "codes": [
            "510300", "510500", "159915", "588000",
            "512880", "512690", "512010",
            "513100", "513180", "159920", "518880",
        ],
    },
}

BACKTEST_PRESETS = {
    "default": ("518880,513100,159915", "黄金+纳指+创业板 (推荐3-ETF)"),
    "best3": ("518880,513100,159915", "黄金+纳指+创业板 (最优)"),
    "best4": ("518880,513100,159915,510300", "黄金+纳指+创业板+沪深300 (备选)"),
    "aggressive": ("518880,513100,159915,588000", "黄金+纳指+创业板+科创50 (激进)"),
    "full5": ("518880,513100,159915,510300,159920", "5只全明星"),
    "ashare": ("510300,159915,588000,510500", "A股纯宽基 (对比用)"),
    "original": ("159915,510300,512880,513180,512690,512010,159920,588000", "原始8-ETF池"),
    "all": ("518880,513100,159915,510300,588000,510050,512880,513180", "全品种大池"),
}

try:
    sys.path.insert(0, str(PROJECT_DIR))
    from tools.momentum_etf_backtest import PRESET_POOLS as _REAL_PRESETS  # noqa: E402
    BACKTEST_PRESETS = dict(_REAL_PRESETS)
    from tools.momentum_signal import POOL as _REAL_POOL  # noqa: E402
    SIGNAL_POOLS["full"] = {
        "desc": "全池 %d 只（宽基/行业/跨境/防御）" % (
            len([c for c in _REAL_POOL if c != "511880"]),
        ),
        "codes": [c for c in _REAL_POOL if c != "511880"],
    }
except Exception:  # pragma: no cover - 导入失败时使用内置配置
    pass

# 信号池与回测分析联动：把回测预设池注册为可选信号池（内容保持一致）
for _preset_key, (_preset_codes, _preset_desc) in BACKTEST_PRESETS.items():
    SIGNAL_POOLS[_preset_key] = {
        "desc": f"回测预设: {_preset_desc}",
        "codes": _preset_codes.split(","),
    }


CODE_RE = re.compile(r"^\d{6}(,\d{6})*$")

# 默认离线模式: 页面加载只读本地缓存，秒开不卡；点“刷新数据”才联网更新。
# 用 --online 启动则页面加载也允许联网刷新。
ALLOW_ONLINE = False


def _resolve_backtest_start(start_raw):
    """把回测区间参数解析为起始日期：full / 1y-20y / YYYY-MM-DD。"""
    if not start_raw or start_raw == "full":
        return "2013-01-01"
    if re.fullmatch(r"\d+y", start_raw):
        years = int(start_raw[:-1])
        if not 1 <= years <= 20:
            raise ValueError("回测区间年份需在 1-20 之间")
        today = datetime.now().date()
        try:
            target = today.replace(year=today.year - years)
        except ValueError:
            target = today.replace(year=today.year - years, day=28)
        return target.isoformat()
    try:
        datetime.strptime(start_raw, "%Y-%m-%d")
    except ValueError:
        raise ValueError(
            f"start 需为 full / 1y / 3y / 5y / 10y 或 YYYY-MM-DD，当前: {start_raw}"
        )
    return start_raw


def _log(message, level="INFO"):
    """输出到终端 + server.log（logging 框架，按天轮转保留 7 天）。"""
    level = (level or "INFO").upper()
    _LOGGER.log(
        {
            "INFO": logging.INFO,
            "WARN": logging.WARNING,
            "ERROR": logging.ERROR,
        }.get(level, logging.INFO),
        message,
    )
    try:
        db.append_log(
            datetime.now().astimezone().isoformat(timespec="seconds"),
            level,
            message,
        )
    except Exception:
        pass


cache_set_logger(_log)


def _maintenance_loop():
    """后台维护：每 6 小时自动删除 7 天前的日志与过期缓存行。"""
    while not _MAINT_STOP.is_set():
        try:
            removed_logs = db.cleanup_old_logs(7)
            removed_cache = db.cache_delete_expired()
            if removed_logs or removed_cache:
                _log(
                    f"MAINT 自动清理 7 天前日志 {removed_logs} 条 / "
                    f"过期缓存 {removed_cache} 条"
                )
        except Exception as exc:  # noqa: BLE001
            _log(f"MAINT 自动清理失败: {exc}", "WARN")
        _MAINT_STOP.wait(6 * 3600)


def _biz(tag, message):
    """业务日志：记录策略/组合/信号的业务摘要，统一 [BIZ] 前缀便于 grep。"""
    _log(f"[BIZ] {tag} {message}", "INFO")


# ---------------------------------------------------------------------------
# 子进程封装
# ---------------------------------------------------------------------------

def run_script(args, timeout=300, offline=True):
    """在项目根目录运行脚本，返回 stdout 文本。超时/失败抛 RuntimeError。

    offline=True 时设置 ETF_DATA_OFFLINE=1，脚本只读 data/cache 不联网，
    避免陈旧缓存触发慢速网络刷新导致页面长时间卡住。
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env["ETF_DATA_OFFLINE"] = "1" if offline else "0"
    cmd = [sys.executable, str(TOOLS_DIR / args[0]), *args[1:]]
    desc = " ".join(args)
    _log(f"RUN python3 {desc} (offline={'是' if offline else '否'}, timeout={timeout}s)")
    started = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(PROJECT_DIR),
            env=env,
        )
    except subprocess.TimeoutExpired:
        _log(f"RUN 超时 {desc} >{timeout}s", "ERROR")
        raise RuntimeError(f"脚本执行超时（>{timeout}s）: {desc}")
    duration = time.time() - started
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-800:]
        _log(
            f"RUN 失败 {desc} exit={result.returncode} ({duration:.1f}s): {detail}",
            "ERROR",
        )
        raise RuntimeError(f"脚本执行失败 (exit {result.returncode}): {detail}")
    _log(
        f"RUN 完成 {desc} exit=0 ({duration:.1f}s, stdout={len(result.stdout)}B)",
        "INFO",
    )
    return result.stdout


def parse_json_output(stdout):
    """从脚本 stdout 中提取 JSON。

    支持三种形式：
      1. 纯 JSON
      2. __JSON_START__ ... __JSON_END__ 标记包裹（回测/选品）
      3. 文本报告末尾的 JSON（审计）
    """
    start_marker = "__JSON_START__"
    end_marker = "__JSON_END__"
    if start_marker in stdout:
        chunk = stdout.split(start_marker, 1)[1]
        chunk = chunk.split(end_marker, 1)[0]
        return json.loads(chunk)
    stripped = stdout.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # 审计脚本: 人类报告之后输出 JSON，找第一个独立的 "{"
    lines = stdout.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() == "{":
            chunk = "\n".join(lines[idx:])
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                break
    raise RuntimeError("无法从脚本输出中解析 JSON")


# ---------------------------------------------------------------------------
# 结果缓存：统一走 cache.py 抽象层（当前后端 SQLite，可切换 memory/Redis 等）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 数据源读取（本地文件，不触发网络）
# ---------------------------------------------------------------------------

def _read_json_file(path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def latest_positions():
    """返回最新持仓快照（优先 positions_latest.json，其次按修改时间）。"""
    latest = DATA_DIR / "positions_latest.json"
    if latest.exists():
        return _read_json_file(latest, {})
    candidates = sorted(
        DATA_DIR.glob("positions_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        data = _read_json_file(path)
        if data:
            return data
    return {}


def _kline_from_cache(code, count):
    """从 data/cache 中读取 K 线，优先 v2 格式，其次旧格式。

    返回 (bars, meta)；找不到返回 (None, None)。
    """
    cache_dir = DATA_DIR / "cache"
    v2_hits = sorted(
        cache_dir.glob(f"etf_v2_*_{code}_qfq_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    old_hits = sorted(
        cache_dir.glob(f"etf_kline_{code}_qfq_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in v2_hits:
        data = _read_json_file(path)
        if not data or not data.get("bars"):
            continue
        bars = data["bars"]
        manifest = data.get("manifest", {})
        meta = {
            "source": manifest.get("source", "cache"),
            "start_date": manifest.get("start_date", bars[0].get("date")),
            "end_date": manifest.get("end_date", bars[-1].get("date")),
            "fetched_at": manifest.get("fetched_at"),
            "total_bars": len(bars),
        }
        return bars, meta
    for path in old_hits:
        data = _read_json_file(path)
        if not data:
            continue
        bars = data.get("data") or data.get("bars")
        if not bars:
            continue
        meta = {
            "source": data.get("source", "cache"),
            "start_date": bars[0].get("date"),
            "end_date": bars[-1].get("date"),
            "total_bars": len(bars),
        }
        return bars, meta
    return None, None


def load_kline(code, count=300):
    """读取 K 线；缓存不足时回退到 etf_market_data（可能触发网络刷新）。"""
    bars, meta = _kline_from_cache(code, count)
    if bars:
        view = bars[-count:]
        meta = dict(meta)
        meta["start_date"] = view[0]["date"]
        meta["total_bars"] = len(view)
        _log(
            f"DATA {code} 使用本地缓存 {meta.get('source', 'cache')} "
            f"{meta.get('total_bars', '?')} 根 ({meta.get('start_date')}~{meta.get('end_date')})",
            "INFO",
        )
        return view, meta
    try:
        sys.path.insert(0, str(PROJECT_DIR))
        from tools.etf_market_data import load_etf_series
        _log(f"DATA {code} 本地缓存不足，回退 load_etf_series (count={max(count, 300)})", "WARN")
        series = load_etf_series(code, count=max(count, 300))
        bars = list(series.bars)
        manifest = series.manifest
        meta = {
            "source": manifest.source,
            "start_date": manifest.start_date,
            "end_date": manifest.end_date,
            "fetched_at": manifest.fetched_at,
            "total_bars": len(bars),
        }
        return bars[-count:], meta
    except Exception as exc:
        _log(f"DATA {code} 加载失败: {exc}", "ERROR")
        return None, None


# ---------------------------------------------------------------------------
# API 处理
# ---------------------------------------------------------------------------

def api_pools(params=None):
    presets = {
        key: {"codes": codes, "desc": desc}
        for key, (codes, desc) in BACKTEST_PRESETS.items()
    }
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "signal_pools": SIGNAL_POOLS,
            "backtest_presets": presets,
        },
    }


def api_signals(params):
    pool = params.get("pool", ["recommended"])[0]
    refresh = params.get("refresh", ["0"])[0] == "1"
    momentum = int(params.get("momentum", ["25"])[0])
    if not 5 <= momentum <= 120:
        raise ValueError("动量周期需在 5-120 之间")
    if pool in SIGNAL_POOLS:
        codes = ",".join(SIGNAL_POOLS[pool]["codes"])
        pool_label = SIGNAL_POOLS[pool]["desc"]
    elif CODE_RE.match(pool):
        codes = pool
        pool_label = f"自定义池 {pool}"
    else:
        raise ValueError(f"未知信号池: {pool}")

    key = f"signals-v3|{momentum}|{codes}"

    def producer():
        stdout = run_script(
            ["momentum_signal.py", "--pool", codes, "--momentum", str(momentum), "--json"],
            timeout=420,
            offline=not (refresh or ALLOW_ONLINE),
        )
        data = parse_json_output(stdout)
        data["pool_label"] = pool_label
        data["momentum_period"] = momentum
        return data

    payload, _, _ = cached(key, 600, refresh, producer)
    data = payload.get("data") or {}
    items = data.get("items") or []
    _biz(
        "SIGNAL",
        f"pool={pool_label} momentum={momentum}日 status={data.get('status')} "
        f"as_of={data.get('as_of')} items={len(items)}",
    )
    for item in items:
        _biz(
            "SIGNAL",
            f"  {item.get('code')} {item.get('name')} "
            f"rsrs={item.get('rsrs_score')} slope={item.get('slope_annual_pct')} "
            f"pass={item.get('pass')} strength={item.get('signal_strength')}",
        )
    selected = data.get("selected")
    if selected:
        _biz(
            "SIGNAL",
            f"  >> 目标 {selected.get('code')} {selected.get('name')} "
            f"strength={selected.get('signal_strength')}",
        )
    rotation = data.get("rotation")
    if rotation:
        target = rotation.get("target") or {}
        _biz(
            "SIGNAL",
            f"  >> 轮动 action={rotation.get('action')} "
            f"target={target.get('code') or ''} {target.get('name') or ''}",
        )
    _record_signal_history(pool, momentum, data)
    return payload


def api_overview(params):
    refresh = params.get("refresh", ["0"])[0] == "1"
    key = "overview-v3|full"

    def producer():
        stdout = run_script(
            ["strategy_monitor.py", "--json"],
            timeout=600,
            offline=not (refresh or ALLOW_ONLINE),
        )
        return parse_json_output(stdout)

    payload, _, _ = cached(key, 900, refresh, producer)
    data = payload.get("data") or {}
    momentum = data.get("momentum") or {}
    selected = momentum.get("selected")
    _biz(
        "OVERVIEW",
        f"动量 status={momentum.get('status')} as_of={momentum.get('as_of')} "
        f"selected={selected and selected.get('code')} "
        f"pool_complete={momentum.get('pool_complete')}",
    )
    groups = data.get("grid_groups") or {}
    _biz(
        "OVERVIEW",
        f"网格 stop={[g.get('code') for g in groups.get('stop', [])]} "
        f"caution={[g.get('code') for g in groups.get('caution', [])]} "
        f"ok={len(groups.get('ok', []))} 只",
    )
    risk = data.get("risk") or {}
    _biz(
        "OVERVIEW",
        f"风险 status={risk.get('status')} sharpe={risk.get('sharpe')} "
        f"maxDD={risk.get('max_dd_pct')}% VaR95={risk.get('var_95_loss_pct')}% "
        f"IC10={risk.get('ic_10d')} IC20={risk.get('ic_20d')}",
    )
    advice = data.get("advice") or {}
    _biz(
        "OVERVIEW",
        f"建议 动量={advice.get('momentum_action')} | 网格={advice.get('grid_action')}",
    )
    return payload


def api_backtest(params):
    preset = (params.get("preset", [""])[0] or "").strip()
    pool_param = (params.get("pool", [""])[0] or "").strip()
    if pool_param and not preset:
        # 兼容 pool 参数：回测预设名 / 信号池名 / 逗号分隔代码
        preset = pool_param
    if not preset:
        preset = "best4"
    momentum = int(params.get("momentum", ["25"])[0])
    freq = params.get("freq", ["biweekly"])[0]
    start_raw = (params.get("start", [""])[0] or "").strip() or "full"
    commission = float(params.get("commission", ["0.00025"])[0])
    min_commission = float(params.get("min_commission", ["0"])[0])
    refresh = params.get("refresh", ["0"])[0] == "1"
    if not 5 <= momentum <= 120:
        raise ValueError("动量周期需在 5-120 之间")
    if freq not in {"weekly", "biweekly", "monthly"}:
        raise ValueError("freq 需为 weekly/biweekly/monthly")
    resolved_start = _resolve_backtest_start(start_raw)
    if not 0 <= commission <= 0.01:
        raise ValueError("佣金费率需在 0-0.01 之间（如 0.00025=万2.5）")
    if not 0 <= min_commission <= 100:
        raise ValueError("最低佣金需在 0-100 元之间（0=免5）")

    # 解析池：回测预设 / 信号池 / 自定义代码
    pool_codes = None
    if preset in BACKTEST_PRESETS:
        pool_label = BACKTEST_PRESETS[preset][1]
    elif preset in SIGNAL_POOLS:
        pool_codes = ",".join(SIGNAL_POOLS[preset]["codes"])
        pool_label = SIGNAL_POOLS[preset]["desc"]
    elif CODE_RE.match(preset):
        pool_codes = preset
        pool_label = f"自定义池 {preset}"
    else:
        signal_names = ", ".join(
            key for key in SIGNAL_POOLS if key not in BACKTEST_PRESETS
        )
        raise ValueError(
            f"未知回测池: {preset}（可用预设: {', '.join(BACKTEST_PRESETS)}；"
            f"可用信号池: {signal_names}；或逗号分隔的 ETF 代码）"
        )

    pool_spec = pool_codes or preset
    key = (
        f"backtest-v3|{pool_spec}|{momentum}|{freq}|{start_raw}"
        f"|{commission}|{min_commission}"
    )

    def producer():
        args = ["momentum_etf_backtest.py"]
        if pool_codes:
            args += ["--pool", pool_codes]
        else:
            args += ["--preset", preset]
        args += [
            "--momentum", str(momentum),
            "--freq", freq,
            "--start", resolved_start,
            "--commission-rate", str(commission),
            "--min-commission", str(min_commission),
            "--json",
        ]
        stdout = run_script(args, timeout=900, offline=not (refresh or ALLOW_ONLINE))
        data = parse_json_output(stdout)
        # market_data 是冻结的行情对象，序列化后体积过大，前端不需要
        data.pop("market_data", None)
        data.pop("data_manifest", None)
        # 日频 NAV 降采样到最多 1000 点供绘图
        daily_nav = data.get("daily_nav") or []
        if len(daily_nav) > 1000:
            step = len(daily_nav) / 1000
            data["daily_nav"] = [
                daily_nav[int(i * step)]
                for i in range(1000)
            ] + [daily_nav[-1]]
            data["daily_nav_sampled"] = True
        data["preset"] = preset
        data["pool_label"] = pool_label
        data["momentum"] = momentum
        data["freq"] = freq
        data["start_raw"] = start_raw
        data["commission"] = commission
        data["min_commission"] = min_commission
        return data

    payload, from_cache, _ = cached(key, 7200, refresh, producer)
    data = payload.get("data") or {}
    period = data.get("period") or {}
    perf = data.get("performance") or {}
    _biz(
        "BACKTEST",
        f"pool={data.get('pool_label') or preset} momentum={momentum}日 freq={freq} "
        f"start={start_raw}({resolved_start}) 佣金={commission}(最低{min_commission}元) "
        f"区间={period.get('start')}~{period.get('end')} ({period.get('years')}年)",
    )
    _biz(
        "BACKTEST",
        f"  total={perf.get('total_return_pct')}% annual={perf.get('annual_return_pct')}% "
        f"excess={perf.get('excess_return_pct')}pp maxDD={perf.get('max_dd_pct')}% "
        f"sharpe={perf.get('sharpe')} sortino={perf.get('sortino')} "
        f"calmar={perf.get('calmar')} trades={perf.get('num_trades')} "
        f"nav={perf.get('final_nav')}",
    )
    trades = data.get("trades") or []
    buy_count = sum(1 for t in trades if "买入" in str(t.get("action", "")))
    sell_count = sum(1 for t in trades if "卖出" in str(t.get("action", "")))
    _biz("BACKTEST", f"  交易 {len(trades)} 笔 (买入 {buy_count} / 卖出 {sell_count})")
    if not from_cache:
        try:
            db.upsert_backtest_result(
                "backtest",
                key,
                {
                    "preset": preset,
                    "pool": pool_spec,
                    "momentum": momentum,
                    "freq": freq,
                    "start": start_raw,
                    "commission": commission,
                    "min_commission": min_commission,
                },
                {
                    "total_return_pct": perf.get("total_return_pct"),
                    "annual_return_pct": perf.get("annual_return_pct"),
                    "max_dd_pct": perf.get("max_dd_pct"),
                    "sharpe": perf.get("sharpe"),
                    "sortino": perf.get("sortino"),
                    "calmar": perf.get("calmar"),
                    "num_trades": perf.get("num_trades"),
                    "final_nav": perf.get("final_nav"),
                    "period_start": period.get("start"),
                    "period_end": period.get("end"),
                    "period_years": period.get("years"),
                },
                data,
            )
        except Exception as exc:  # noqa: BLE001 - 落库失败不影响主流程
            _log(f"DB 回测结果写入失败: {exc}", "WARN")
    return payload


def api_audit(params):
    refresh = params.get("refresh", ["0"])[0] == "1"
    key = "audit-v3|full"

    def producer():
        stdout = run_script(
            ["strategy_audit.py", "--json"],
            timeout=900,
            offline=not (refresh or ALLOW_ONLINE),
        )
        return parse_json_output(stdout)

    payload, _, _ = cached(key, 7200, refresh, producer)
    data = payload.get("data") or {}
    daily = data.get("daily_metrics") or {}
    ic = data.get("ic_ir") or {}
    stress = data.get("stress_test") or {}
    _biz(
        "AUDIT",
        f"日频 annual={daily.get('annual_return_pct')}% sharpe={daily.get('sharpe')} "
        f"maxDD={daily.get('max_dd_pct')}% VaR95={daily.get('var_95_daily_pct')}% "
        f"win_rate={daily.get('win_rate_pct')}% samples={daily.get('count')}",
    )
    _biz(
        "AUDIT",
        f"  IC/IR 10日={ic.get('ic_10d')}/{ic.get('ir_10d')} "
        f"20日={ic.get('ic_20d')}/{ic.get('ir_20d')} "
        f"40日={ic.get('ic_40d')}/{ic.get('ir_40d')} n_dates={ic.get('n_dates')}",
    )
    _biz(
        "AUDIT",
        f"  压力情景 {len(stress.get('scenarios', []))} 个 "
        f"VaR95={stress.get('var_95_pct')}% VaR99={stress.get('var_99_pct')}% "
        f"CVaR95={stress.get('cvar_95_pct')}%",
    )
    return payload


def api_screener(params):
    refresh = params.get("refresh", ["0"])[0] == "1"
    key = "screener|full"

    def producer():
        stdout = run_script(
            ["etf_screener.py", "--json"],
            timeout=900,
            offline=not (refresh or ALLOW_ONLINE),
        )
        return parse_json_output(stdout)

    payload, from_cache, _ = cached(key, 86400, refresh, producer)
    data = payload.get("data") or {}
    results = data.get("results") or []
    top5 = [(r.get("code"), r.get("total")) for r in results[:5]]
    _biz(
        "SCREENER",
        f"候选 {data.get('candidates_count')} 只 top5={top5} "
        f"avg_corr={data.get('avg_selected_corr')}",
    )
    recommended = data.get("recommended") or []
    _biz(
        "SCREENER",
        f"  推荐组合 {[(r.get('code'), r.get('total')) for r in recommended]}",
    )
    if not from_cache:
        try:
            db.upsert_backtest_result(
                "screener",
                "full",
                {"top": len(results), "generated_at": data.get("generated_at")},
                {
                    "candidates_count": data.get("candidates_count"),
                    "avg_selected_corr": data.get("avg_selected_corr"),
                    "recommended": [
                        (r.get("code"), r.get("total")) for r in recommended
                    ],
                },
                data,
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"DB 选品结果写入失败: {exc}", "WARN")
    return payload


def api_positions(params=None):
    data = latest_positions()
    _log(
        f"FILE positions 快照 {data.get('date', '?')} "
        f"({len(data.get('holdings', []))} 只持仓)",
        "INFO",
    )
    summary = data.get("account_summary") or {}
    holdings = data.get("holdings") or []
    # 策略归属：按方案文档的网格/动量标的口径自动标注（优先保留已有标注）
    strategy_config = _load_holdings_strategy()
    by_code = strategy_config.get("by_code", {})
    default_cfg = strategy_config.get("default", {})
    for holding in holdings:
        code = str(holding.get("code") or "").strip()
        entry = by_code.get(code) or {}
        strategy = holding.get("strategy") or entry.get("strategy") or default_cfg.get("strategy", "其他")
        holding["strategy"] = strategy
        holding["bucket"] = holding.get("bucket") or entry.get("bucket") or default_cfg.get("bucket", "其他")
        holding["strategy_note"] = entry.get("note") or default_cfg.get("note", "")
    buckets: dict[str, dict] = {}
    for holding in holdings:
        bucket = holding.get("bucket") or "其他"
        item = buckets.setdefault(bucket, {"market_value": 0.0, "count": 0})
        item["market_value"] += holding.get("market_value") or 0
        item["count"] += 1
    total_mv = sum(item["market_value"] for item in buckets.values())
    for bucket, item in buckets.items():
        item["weight"] = (
            round(item["market_value"] / total_mv * 100, 2) if total_mv else 0
        )
        item["market_value"] = round(item["market_value"], 3)
    data["strategy_summary"] = buckets
    data["notes"] = [
        "两套策略资金物理隔离：网格子账户与动量子账户互相不救、不补、不挪用",
        "策略归属按「网格+动量双策略操作方案」标的口径自动标注，可在 holdings_strategy.json 调整",
    ]
    _biz(
        "POSITION",
        f"date={data.get('date')} total={summary.get('total_assets')} "
        f"sec={summary.get('securities_value')} cash={summary.get('available_cash')} "
        f"pos_ratio={summary.get('position_ratio')}% total_pnl={summary.get('total_pnl')} "
        f"daily_pnl={summary.get('daily_pnl')} holdings={len(holdings)}",
    )
    _biz(
        "POSITION",
        f"  子账户市值 { {k: round(v['market_value']) for k, v in buckets.items()} }",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": data,
    }


def api_kline(params):
    code = params.get("code", [""])[0]
    count = min(max(int(params.get("count", ["300"])[0]), 60), 2000)
    if not CODE_RE.match(code) or "," in code:
        raise ValueError("非法证券代码")
    key = f"kline-v2|{code}|{count}"

    def producer():
        bars, meta = load_kline(code, count)
        if not bars:
            raise ValueError(f"{code} 无可用 K 线数据（缓存缺失且网络不可用）")
        return {"code": code, "count": len(bars), "meta": meta, "bars": bars}

    payload, _, _ = cached(key, 3600, False, producer)
    data = payload.get("data") or {}
    meta = data.get("meta") or {}
    _biz(
        "KLINE",
        f"{code} bars={data.get('count')} 根 "
        f"({meta.get('start_date')}~{meta.get('end_date')}, 来源 {meta.get('source')})",
    )
    return payload


def api_stoploss(params):
    code = params.get("code", [""])[0]
    entry = params.get("entry", [""])[0]
    if not CODE_RE.match(code) or "," in code:
        raise ValueError("非法证券代码")
    try:
        price = float(entry)
    except ValueError:
        raise ValueError("entry 必须是数字价格")
    key = f"stoploss|{code}|{price}"

    def producer():
        stdout = run_script(
            ["momentum_signal.py", "--entry", code, str(price), "--json"],
            timeout=120,
            offline=True,
        )
        return parse_json_output(stdout)

    payload, _, _ = cached(key, 300, False, producer)
    data = payload.get("data") or {}
    _biz(
        "STOPLOSS",
        f"{code} entry={data.get('entry_price')} current={data.get('current_price')} "
        f"loss={data.get('loss_pct')}% stop_line={data.get('stop_loss_line')} "
        f"triggered={data.get('triggered')}",
    )
    return payload


def _qq_code(code):
    """ETF 代码转腾讯行情代码（sh/sz 前缀）。"""
    code = code.strip()
    if code.startswith(("6", "9", "5")):
        return "sh" + code
    elif code.startswith(("0", "3", "2", "1")):
        return "sz" + code
    return "sh" + code


def fetch_realtime_quotes(codes):
    """腾讯行情实时报价（批量），网络失败返回空 dict。"""
    symbols = ",".join(_qq_code(c) for c in codes)
    url = f"https://qt.gtimg.cn/q={symbols}"
    try:
        result = subprocess.run(
            [
                "/usr/bin/curl", "-s", "--noproxy", "*", "--max-time", "8",
                "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                url,
            ],
            capture_output=True,
            timeout=12,
        )
    except Exception as exc:
        _log(f"REALTIME 请求失败: {exc}", "WARN")
        return {}
    raw = result.stdout
    try:
        text = raw.decode("gbk", errors="replace")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    quotes = {}
    for line in text.splitlines():
        match = re.search(r'v_([a-z]+\d{6})="(.*)"', line)
        if not match:
            continue
        code = match.group(1)[-6:]
        if code not in codes:
            continue
        fields = match.group(2).split("~")
        if len(fields) < 34:
            continue
        try:
            price = float(fields[3])
        except ValueError:
            continue
        change_pct = None
        try:
            change_pct = float(fields[32])
        except (ValueError, IndexError):
            pass
        prev_close = None
        try:
            prev_close = float(fields[4]) if fields[4] not in ("", "0.00") else None
        except ValueError:
            pass
        quotes[code] = {
            "code": code,
            "name": fields[1],
            "price": price,
            "change_pct": change_pct,
            "prev_close": prev_close,
            "source": "tencent_realtime",
        }
    return quotes


def api_realtime(params):
    codes_raw = params.get("codes", [""])[0]
    codes = [c.strip() for c in codes_raw.split(",") if c.strip()]
    if not codes or len(codes) > 20 or not CODE_RE.match(",".join(codes)):
        raise ValueError("codes 需为逗号分隔的 6 位证券代码（最多 20 个）")
    key = f"realtime|{','.join(codes)}"

    def producer():
        quotes = fetch_realtime_quotes(codes)
        live = len(quotes) == len(codes)
        merged = dict(quotes)
        if not live:
            for code in codes:
                if code in merged:
                    continue
                bars, meta = load_kline(code, 120)
                if bars:
                    merged[code] = {
                        "code": code,
                        "name": code,
                        "price": bars[-1]["close"],
                        "change_pct": None,
                        "prev_close": None,
                        "source": "daily_close",
                    }
        _biz(
            "REALTIME",
            f"codes={','.join(codes)} live={live} "
            f"quotes={[(c, q.get('price')) for c, q in merged.items()]}",
        )
        return {
            "live": live,
            "quotes": merged,
            "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
            "note": None if live else "实时行情获取失败，展示最近收盘价",
        }

    payload, _, _ = cached(key, 20, False, producer)
    return payload


def api_enum(params=None):
    # 优先使用当前 v3.0 引擎生成的枚举结果，缺失时回退旧文件
    enum_path = DATA_DIR / "enum_backtest_veteran_c3_25d.json"
    if not enum_path.exists():
        enum_path = DATA_DIR / "enum_backtest_15c3.json"
    data = _read_json_file(enum_path, {})
    _log(
        f"FILE enum 组合枚举 {data.get('total_combos', '?')} 组 "
        f"配置={data.get('config', '?')} (生成于 {data.get('generated_at', '?')})"
    )
    raw_results = data.get("results") or []
    # 字段归一化：兼容新旧 schema（新: annual_pct/label/n_etf/win_rate/num_trades）
    results = []
    for r in raw_results:
        combo = r.get("combo", "")
        results.append({
            "combo": combo,
            "label": r.get("label", combo),
            "n": r.get("n", r.get("n_etf", len(combo.split("+")))),
            "ann": r.get("ann", r.get("annual_pct")),
            "total": r.get("total", r.get("total_pct")),
            "dd": r.get("dd", r.get("max_dd_pct")),
            "sharpe": r.get("sharpe"),
            "calmar": r.get("calmar"),
            "wr": r.get("wr", r.get("win_rate")),
            "trades": r.get("trades", r.get("num_trades")),
            "momentum": r.get("momentum"),
            "window_start": r.get("window_start"),
            "period_years": r.get("period_years"),
            "window_truncated": r.get("window_truncated"),
        })
    _biz(
        "ENUM",
        f"组合数 {data.get('total_combos')} 有效 {data.get('valid_results')} "
        f"配置={data.get('config')} 生成于 {data.get('generated_at')} "
        f"top5={[(r.get('combo'), r.get('ann')) for r in results[:5]]}",
    )
    try:
        db.upsert_backtest_result(
            "enum",
            str(data.get("config") or "default"),
            {
                "generated_at": data.get("generated_at"),
                "total_combos": data.get("total_combos"),
                "file": enum_path.name,
            },
            {
                "total_combos": data.get("total_combos"),
                "valid_results": data.get("valid_results"),
                "top5": [(r.get("combo"), r.get("ann")) for r in results[:5]],
            },
            {"generated_at": data.get("generated_at"), "results": results},
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"DB 枚举结果写入失败: {exc}", "WARN")
    return {
        "ok": True,
        "cached": True,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "generated_at": data.get("generated_at"),
            "total_combos": data.get("total_combos"),
            "valid_results": data.get("valid_results"),
            "config": data.get("config"),
            "results": results,
        },
    }


def api_etf_scan(params):
    top = min(max(int(params.get("top", ["100"])[0]), 1), 640)
    category = params.get("category", [""])[0]
    data = _read_json_file(DATA_DIR / "etf_backtest_results.json", {})
    results = data.get("results", [])
    if category:
        results = [r for r in results if category in (r.get("category") or "")]
    results = sorted(results, key=lambda r: r.get("composite_score", 0), reverse=True)
    _log(
        f"FILE etf-scan 全市场回测 top={top} category='{category}' "
        f"(共 {len(results)} 条, 生成于 {data.get('generated_at', '?')})"
    )
    _biz(
        "ETF-SCAN",
        f"top={top} category={category or '全部'} rows={len(results)} "
        f"best={[(r.get('code'), r.get('name'), r.get('composite_score')) for r in results[:5]]}",
    )
    return {
        "ok": True,
        "cached": True,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "generated_at": data.get("generated_at"),
            "config": data.get("config"),
            "total_tested": data.get("total_tested"),
            "results": results[:top],
        },
    }


ROUTES = {
    "/api/pools": api_pools,
    "/api/signals": api_signals,
    "/api/overview": api_overview,
    "/api/backtest": api_backtest,
    "/api/audit": api_audit,
    "/api/screener": api_screener,
    "/api/positions": api_positions,
    "/api/kline": api_kline,
    "/api/stoploss": api_stoploss,
    "/api/realtime": api_realtime,
    "/api/enum": api_enum,
    "/api/etf-scan": api_etf_scan,
    "/api/models": lambda params: _api_models(),
    "/api/db/stats": lambda params: _api_db_stats(params),
    "/api/db/tables": lambda params: _api_db_tables(params),
    "/api/db/table": lambda params: _api_db_table(params),
    "/api/logs": lambda params: _api_logs(params),
    "/api/scheduler": lambda params: _api_scheduler(params),
    "/api/grid": lambda params: api_grid(params),
    "/api/grid/optimize": lambda params: api_grid_optimize(params),
    "/api/grid/triggers/list": lambda params: api_grid_triggers_list(params),
}

MAX_BODY_BYTES = 12 * 1024 * 1024

_SCHEDULER = None


def _json_safe(value):
    """把 NaN / Infinity 等非标准 JSON 数值清洗为 null，保证浏览器可解析。"""
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _cache_signals_from_monitor(momentum: dict) -> None:
    """把策略监测里的动量信号写入信号页缓存，页面立即展示最新数据。"""
    codes = ",".join(SIGNAL_POOLS["recommended"]["codes"])
    key = f"signals-v3|25|{codes}"
    data = {
        "status": momentum.get("status"),
        "as_of": momentum.get("as_of"),
        "items": momentum.get("items", []),
        "errors": momentum.get("errors", []),
        "selected": momentum.get("selected"),
        "rotation": momentum.get("rotation"),
        "pool_complete": momentum.get("pool_complete"),
        "pool_label": SIGNAL_POOLS["recommended"]["desc"],
        "momentum_period": 25,
    }
    payload = {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": data,
    }
    try:
        cache_set(key, payload)
        _log(f"SCHED 信号缓存已更新 [{key}] as_of={momentum.get('as_of')}")
    except Exception as exc:
        _log(f"SCHED 信号缓存写入失败: {exc}", "WARN")


def _record_signal_history(pool: str, momentum: int, data: dict) -> None:
    """把一次信号扫描结果写入 signal_history（每天每池每周期 upsert）。"""
    try:
        selected = data.get("selected") or {}
        rotation = data.get("rotation") or {}
        db.append_signal_history(
            data.get("as_of") or "",
            pool,
            momentum,
            data.get("status") or "",
            data.get("items") or [],
            selected.get("code"),
            selected.get("name"),
            rotation,
            data,
        )
    except Exception as exc:  # noqa: BLE001 - 落库失败不影响主流程
        _log(f"DB 信号历史写入失败: {exc}", "WARN")


def _scheduled_job() -> None:
    """定时任务：刷新信号（策略监测）+ 发送邮件（复用 monitor_alert）。"""
    from tools import monitor_alert as ma

    started = time.time()
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    _log("SCHED 定时任务开始：刷新信号 + 发送邮件")
    result = "error"
    detail = {}
    email_sent = False
    try:
        stdout = run_script(
            ["strategy_monitor.py", "--json"],
            timeout=600,
            offline=False,
        )
        report = parse_json_output(stdout)
        momentum = report.get("momentum") or {}
        if momentum.get("items"):
            _cache_signals_from_monitor(momentum)
            _record_signal_history("recommended", 25, momentum)
        codes = [
            item.get("code")
            for item in (momentum.get("items") or [])
            if item.get("code")
        ]
        prices = fetch_realtime_quotes(codes) if codes else {}
        html = ma.format_email_body(report, prices)
        smtp = ma._load_env()
        ma.send_email(smtp, html)
        email_sent = True
        result = "ok"
        detail = {
            "as_of": momentum.get("as_of"),
            "items": len(momentum.get("items") or []),
            "selected": (momentum.get("selected") or {}).get("code"),
        }
        _biz(
            "SCHED",
            f"信号刷新 as_of={momentum.get('as_of')} 邮件已发送 "
            f"耗时 {time.time() - started:.1f}s",
        )
    except Exception as exc:  # noqa: BLE001 - 任务失败记录后不再向上抛
        detail = {"error": str(exc)}
        _log(f"SCHED 定时任务失败: {exc}", "ERROR")
    finally:
        try:
            db.append_scheduler_run(
                "schedule",
                started_at,
                datetime.now().astimezone().isoformat(timespec="seconds"),
                int((time.time() - started) * 1000),
                result,
                detail,
                email_sent,
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"DB 调度记录写入失败: {exc}", "WARN")


def _api_scheduler(params=None):
    if _SCHEDULER is None:
        return {
            "ok": True,
            "cached": False,
            "stale": False,
            "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "data": {"enabled": False, "reason": "未启用（--no-scheduler）"},
        }
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": _SCHEDULER.status(),
    }


def _api_scheduler_run(body=None):
    if _SCHEDULER is None:
        raise ValueError("调度器未启用（--no-scheduler）")
    started = time.time()
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    result = "error"
    detail = {}
    try:
        run_value = _SCHEDULER.run_now()
        result = "ok"
        detail = {"result": str(run_value)}
    except Exception as exc:
        detail = {"error": str(exc)}
        _log(f"SCHED 手动执行失败: {exc}", "ERROR")
        raise
    finally:
        try:
            db.append_scheduler_run(
                "manual",
                started_at,
                datetime.now().astimezone().isoformat(timespec="seconds"),
                int((time.time() - started) * 1000),
                result,
                detail,
                None,
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"DB 调度记录写入失败: {exc}", "WARN")
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"result": result, **_SCHEDULER.status()},
    }


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_param(params, key, default, lo, hi, label):
    raw = (params.get(key, [str(default)])[0] or str(default)).strip()
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        raise ValueError(f"{label} 需为整数")
    if not lo <= value <= hi:
        raise ValueError(f"{label} 需在 {lo}-{hi} 之间")
    return value


def api_grid(params=None):
    """网格策略总览：标的趋势分析 + 触发价格 + 触发记录。"""
    refresh = params.get("refresh", ["0"])[0] == "1" if params else False
    key = "grid|overview"

    def _normalize_db_triggers(rows):
        """把 grid_triggers 表记录转成 grid_trading 期望的触发记录格式。"""
        out = []
        for row in rows or []:
            trigger_date = str(row.get("trigger_date") or "")
            out.append({
                "date": trigger_date[:10],
                "time": trigger_date[11:19] if len(trigger_date) > 10 else "",
                "action": row.get("action"),
                "type": row.get("trigger_type") or "grid",
                "price": row.get("price"),
                "shares": row.get("shares"),
                "amount": row.get("amount"),
                "base_price_before": row.get("base_price_before"),
                "base_price_after": row.get("base_price_after"),
                "name": row.get("name"),
                "source": row.get("source"),
            })
        return out

    def _triggers_for(code):
        """从 MySQL grid_triggers 表读取（权威持久化，本地文件不再使用）。"""
        try:
            rows = db.grid_triggers_for_code(code)
            if rows:
                return _normalize_db_triggers(rows)
        except Exception:
            pass
        _log(f"GRID-TRIGGERS 表读取失败，按无触发处理: {code}", "WARN")
        return []

    def producer():
        from tools import grid_trading as gt

        configs = gt.CONFIGS
        codes = list(configs.keys())
        all_history = {code: _triggers_for(code) for code in codes}
        quotes = fetch_realtime_quotes(codes)
        items = []
        for code in codes:
            cfg = configs[code]
            try:
                trend = gt.analyze_trend(code)
            except Exception as exc:  # noqa: BLE001 - 单项失败不影响整体
                trend = {
                    "code": code,
                    "name": cfg.get("name", code),
                    "status": "unknown",
                    "error": str(exc)[:160],
                }
            history = all_history.get(code) or []
            # 用触发记录推导动态基准价；无触发时回退静态配置价
            try:
                base = float(gt.get_dynamic_bp(cfg, history))
            except Exception:
                base = _num(cfg.get("base_price"))
            up_pct = _num(
                cfg.get("grid_spacing_up_pct") or cfg.get("grid_spacing_pct") or 0
            )
            down_pct = _num(
                cfg.get("grid_spacing_down_pct") or cfg.get("grid_spacing_pct") or 0
            )
            quote = (quotes or {}).get(code) or {}
            last_trigger = history[-1] if history else None
            items.append({
                "code": code,
                "name": cfg.get("name", code),
                "base_price": round(base, 3) if base is not None else None,
                "spacing_up_pct": up_pct,
                "spacing_down_pct": down_pct,
                "buy_price": (
                    round(base * (1 - down_pct / 100), 3)
                    if base is not None and down_pct
                    else None
                ),
                "sell_price": (
                    round(base * (1 + up_pct / 100), 3)
                    if base is not None and up_pct
                    else None
                ),
                "current_price": quote.get("price"),
                "change_pct": quote.get("change_pct"),
                "score": trend.get("score"),
                "status": trend.get("status"),
                "bb_width": trend.get("bb_width"),
                "ma_state": trend.get("ma_state"),
                "verdict": trend.get("verdict"),
                "error": trend.get("error"),
                "trigger_count": len(history),
                "last_trigger": last_trigger,
            })

        def _ok(item):
            return item.get("status") == "ok" and item.get("score") is not None

        groups = {
            "stop": [i for i in items if _ok(i) and i["score"] <= -4],
            "caution": [i for i in items if _ok(i) and -3 <= i["score"] <= -2],
            "ok": [i for i in items if _ok(i) and i["score"] >= -1],
            "unknown": [i for i in items if not _ok(i)],
        }
        recent_triggers = []
        for code, history in all_history.items():
            for record in history:
                recent_triggers.append({
                    **record,
                    "code": code,
                    "name": configs.get(code, {}).get("name", code),
                })
        recent_triggers.sort(
            key=lambda r: str(r.get("date", "")), reverse=True
        )
        # 网格持仓详情（口径：底仓=策略配置 base_position，网格仓=总持仓-底仓）
        strategy_cfg = _load_holdings_strategy()
        by_code_strategy = strategy_cfg.get("by_code", {}) or {}
        holdings_map = {
            str(h.get("code") or ""): h
            for h in (latest_positions().get("holdings") or [])
        }
        positions = []
        for code in codes:
            cfg = configs.get(code) or {}
            holding = holdings_map.get(code) or {}
            triggers = db.grid_triggers_for_code(code)
            total_shares = _num(holding.get("shares"))
            entry = by_code_strategy.get(code) or {}
            base_position = _num(cfg.get("base_position")) or 0
            cost = _num(holding.get("cost"))
            if cost is None:
                cost = _num(cfg.get("cost_price"))
            price = _num((quotes or {}).get(code, {}).get("price"))
            base_now = _num(cfg.get("base_price"))
            if triggers:
                last_base = _num(triggers[-1].get("base_price_after"))
                if last_base is not None:
                    base_now = last_base
            note = None
            if total_shares is None or total_shares <= 0:
                # 快照中无该标的持仓：不拿 CONFIGS 兜底臆造持仓
                grid_shares = 0
                base_position = 0
                total_shares = 0
                note = "无持仓"
            else:
                cfg_grid = _num(cfg.get("grid_position")) or 0
                if base_position > total_shares:
                    note = "配置底仓超出实际持仓，请核对 CONFIGS base_position"
                elif abs((base_position + cfg_grid) - total_shares) / max(total_shares, 1) > 0.05:
                    note = "配置(base+grid)与实盘持仓不一致，底仓/网格仓为估算"
                base_position = min(base_position, total_shares)
                grid_shares = max(total_shares - base_position, 0)
            market_value = round(total_shares * price, 3) if total_shares is not None and price else None
            pnl = (
                round((price - cost) * total_shares, 3)
                if price is not None and cost is not None and total_shares is not None
                else None
            )
            pnl_pct = (
                round((price - cost) / cost * 100, 2)
                if price is not None and cost
                else None
            )
            positions.append({
                "code": code,
                "name": cfg.get("name", code),
                "strategy": holding.get("strategy") or entry.get("strategy") or "—",
                "bucket": holding.get("bucket") or entry.get("bucket") or "—",
                "base_position": int(base_position),
                "grid_position": int(grid_shares),
                "total_shares": int(total_shares),
                "price": price,
                "cost": cost,
                "market_value": market_value,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "base_price": base_now,
                "note": note,
            })
        positions.sort(key=lambda p: -(p.get("market_value") or 0))
        return {
            "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
            "codes": codes,
            "items": items,
            "groups": {k: [i["code"] for i in v] for k, v in groups.items()},
            "recent_triggers": recent_triggers[:40],
            "positions": positions,
        }

    payload, _, _ = cached(key, 300, refresh, producer)
    data = payload.get("data") or {}
    groups = data.get("groups") or {}
    _biz(
        "GRID",
        f"标的 {len(data.get('items') or [])} 只 "
        f"stop={groups.get('stop', [])} caution={groups.get('caution', [])} "
        f"ok={len(groups.get('ok', []))} 最近触发 {len(data.get('recent_triggers') or [])} 条",
    )
    positions = data.get("positions") or []
    if positions:
        total_mv = sum(p.get("market_value") or 0 for p in positions)
        total_pnl = sum(p.get("pnl") or 0 for p in positions)
        _biz(
            "GRID-POSITION",
            f"网格持仓 {len(positions)} 只 市值={total_mv:.2f} "
            f"盈亏={total_pnl:+.2f} "
            f"top={[(p['code'], p['total_shares'], p.get('base_price')) for p in positions[:3]]}",
        )
    return payload


def api_grid_triggers_list(params=None):
    """按标的/日期区间查询网格触发记录（读 MySQL，可搜索）。"""
    params = params or {}
    code = (params.get("code", [""])[0] or "").strip() or None
    start = (params.get("start", [""])[0] or "").strip() or None
    end = (params.get("end", [""])[0] or "").strip() or None
    trigger_type = (params.get("trigger_type", [""])[0] or "").strip() or None
    try:
        limit = min(max(int(params.get("limit", ["500"])[0]), 1), 2000)
    except (TypeError, ValueError):
        limit = 500
    records = db.query_grid_triggers(code, start, end, trigger_type, limit)
    _biz(
        "GRID-TRIGGERS",
        f"查询 code={code or '全部'} type={trigger_type or '全部'} "
        f"{start or '…'}~{end or '…'} rows={len(records)}",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"records": records, "total": len(records)},
    }


def _num_list(params, key, default, lo, hi, label, integer=False):
    if default is None:
        values = params.get(key)
        if not values:
            return []
        raw = (values[0] or "").strip()
    else:
        raw = (params.get(key, [default])[0] or default).strip()
    parts = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
    if not parts and default is not None:
        parts = [p.strip() for p in default.split(",")]
    out = []
    for part in parts:
        try:
            value = float(part)
        except ValueError:
            raise ValueError(f"{label} 列表含非法值: {part}")
        if not lo <= value <= hi:
            raise ValueError(f"{label} 需在 {lo}-{hi} 之间")
        if integer:
            value = int(value)
        if value not in out:
            out.append(value)
    return out


def _grid_config_summary(code: str, meta: dict) -> dict:
    """根据回测实际生效参数生成网格配置摘要（触发条件/价格区间/委托/持仓区间）。"""
    from tools import grid_trading as gt

    cfg = gt.CONFIGS.get(code) or {}
    gp = meta.get("grid_params") or {}
    base = _num(cfg.get("base_price"))
    up = _num(
        gp.get("spacing_up_pct") or cfg.get("grid_spacing_up_pct") or cfg.get("grid_spacing_pct")
    )
    down = _num(
        gp.get("spacing_down_pct") or cfg.get("grid_spacing_down_pct") or cfg.get("grid_spacing_pct")
    )
    levels_above = int(gp.get("levels_above") or cfg.get("levels_above") or 5)
    levels_below = int(gp.get("levels_below") or cfg.get("levels_below") or 5)
    order_size = int(gp.get("shares_per_grid") or cfg.get("shares_per_grid") or 1000)
    base_position = int(cfg.get("base_position") or 0)

    price_min = (
        round(base * (1 - down / 100) ** levels_below, 3)
        if base is not None and down
        else None
    )
    price_max = (
        round(base * (1 + up / 100) ** levels_above, 3)
        if base is not None and up
        else None
    )
    return {
        "base_price": round(base, 3) if base is not None else None,
        "sell_spacing_pct": up,    # 上涨多少卖
        "buy_spacing_pct": down,   # 下跌多少买
        "levels_above": levels_above,
        "levels_below": levels_below,
        "order_size": order_size,  # 委托数量（每笔）
        "price_range": {"min": price_min, "max": price_max},
        "position_range": {
            "min": base_position,
            "max": base_position + levels_below * order_size,
        },
        "execution_mode": "限价即时买一价卖出",
        "multiples": "已开启",
        "trigger_desc": (
            f"以 {base} 元为基准价，每上涨 +{up}% 卖出 / 每下跌 -{down}% 买入"
            if base is not None
            else "基准价未配置"
        ),
    }


GRID_OPT_SPACINGS = [1.0, 2.0, 3.0, 4.0, 5.0]     # 内置间距候选 %
GRID_OPT_LEVELS = [3, 5, 8]                        # 内置层数候选
GRID_OPT_VALUES = [200, 500, 1000, 2000, 5000]     # 内置每格金额候选（元）


def _grid_shares_candidates(base_price):
    """按基准价把「每格金额」折算成股数候选（100 股取整）。"""
    candidates = []
    for value in GRID_OPT_VALUES:
        if base_price:
            shares = max(100, int(round(value / base_price / 100)) * 100)
        else:
            shares = 500
        if shares not in candidates:
            candidates.append(shares)
    return candidates


def api_grid_optimize(params=None):
    """网格参数自动寻优：内置间距/层数/每格金额候选，多标的多线程并行扫描，
    每个标的输出最优配置（按年化收益）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    params = params or {}
    codes_raw = (params.get("codes", [""])[0] or "").strip()
    if codes_raw in ("", "all"):
        from tools import grid_trading as gt
        codes = list(gt.CONFIGS.keys())
    else:
        codes = [c.strip() for c in codes_raw.replace("，", ",").split(",") if c.strip()]
        if not codes:
            raise ValueError("请选择至少一个标的")
        for code in codes:
            if not CODE_RE.match(code):
                raise ValueError(f"非法证券代码: {code}")
        if len(codes) > 20:
            raise ValueError("标的最多 20 个")

    capital = _int_param(params, "capital", 100000, 1000, 10000000, "回测资金")
    start_raw = (params.get("start", ["2y"])[0] or "2y").strip()
    if start_raw == "full":
        start_arg = "2018-01-01"
    else:
        start_arg = _resolve_backtest_start(start_raw)
    # 可选覆盖（不传则用内置候选，按每格金额自动折算股数）
    spacings = _num_list(params, "spacings", None, 0.1, 20, "间距") or GRID_OPT_SPACINGS
    levels = _num_list(params, "levels", None, 1, 50, "层数", integer=True) or GRID_OPT_LEVELS
    explicit_shares = _num_list(params, "shares", None, 100, 1000000, "每格股数", integer=True)
    refresh = params.get("refresh", ["0"])[0] == "1" if params else False

    def scan_code(code):
        from tools import grid_trading as gt

        cfg = gt.CONFIGS.get(code) or {}
        base = _num(cfg.get("base_price"))
        shares_candidates = explicit_shares or _grid_shares_candidates(base)
        code_results = []
        for spacing in spacings:
            for level in levels:
                for share in shares_candidates:
                    key = (
                        f"grid-opt|{code}|{start_arg}|{capital}"
                        f"|{spacing}|{level}|{share}"
                    )

                    def producer(code=code, spacing=spacing, level=level, share=share):
                        args = [
                            "grid_trading.py", "backtest", code,
                            "--capital", str(capital),
                            "--spacing", str(spacing),
                            "--levels", str(level),
                            "--shares", str(share),
                            "--json",
                        ]
                        if start_arg:
                            args += ["--start", start_arg]
                        stdout = run_script(args, timeout=120, offline=True)
                        return parse_json_output(stdout)

                    payload, _, _ = cached(key, 86400, refresh, producer)
                    data = payload.get("data") or {}
                    perf = (data.get("performance") or {}).get("grid") or {}
                    meta = data.get("meta") or {}
                    code_results.append({
                        "code": code,
                        "name": meta.get("name", code),
                        "spacing": spacing,
                        "levels": level,
                        "shares": share,
                        "grid_value": round(share * base, 0) if base else None,
                        "annual_return_pct": perf.get("annual_return_pct"),
                        "total_return_pct": perf.get("total_return_pct"),
                        "max_dd_pct": perf.get("max_dd_pct"),
                        "sharpe": perf.get("sharpe"),
                        "win_rate_pct": perf.get("win_rate_pct"),
                        "profit_factor": perf.get("profit_factor"),
                        "triggered_buy": perf.get("triggered_buy"),
                        "triggered_sell": perf.get("triggered_sell"),
                        "trades": len(data.get("trades") or []),
                        "final_equity": perf.get("final_equity"),
                        "grade": perf.get("grade"),
                        "grid_config": _grid_config_summary(code, meta),
                    })
        return code_results

    # 多标的多线程并行
    results = []
    total = len(codes) * len(spacings) * len(levels) * (
        len(explicit_shares) if explicit_shares else len(GRID_OPT_VALUES)
    )
    if total > 2000:
        raise ValueError(f"组合数 {total} 超过上限 2000，请缩小标的范围")
    _log(f"GRID-OPT 开始并行扫描: 标的 {len(codes)} × 间距{len(spacings)} × 层数{len(levels)} × 每格金额{len(GRID_OPT_VALUES) if not explicit_shares else len(explicit_shares)} = 约{total} 组")
    workers = min(12, len(codes))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(scan_code, code): code for code in codes}
        for future in as_completed(futures):
            code = futures[future]
            code_results = future.result()
            results.extend(code_results)
            _log(f"GRID-OPT {code} 完成 {len(code_results)} 组（并行度 {workers}）")

    results.sort(key=lambda r: (r["code"], -(r["annual_return_pct"] or 0)))
    best_per_code = {}
    for result in results:
        best_per_code.setdefault(result["code"], result)
    _biz(
        "GRID-OPT",
        f"标的 {len(codes)} 并行扫描完成（{total} 组合）| 各标的最优: "
        + "；".join(
            f"{code} {r['spacing']}%/{r['levels']}层/{r['shares']}股"
            f"（每格约{r['grid_value'] or '?'}元）年化{r['annual_return_pct'] or 0:.1f}%"
            for code, r in best_per_code.items()
        ),
    )
    try:
        db.upsert_backtest_result(
            "grid_opt",
            f"{','.join(codes)}|{capital}|{start_raw}",
            {"codes": codes, "capital": capital, "start": start_raw},
            {
                code: {
                    key: item.get(key)
                    for key in (
                        "spacing", "levels", "shares", "grid_value",
                        "annual_return_pct", "total_return_pct", "max_dd_pct",
                        "sharpe", "win_rate_pct", "trades",
                    )
                }
                for code, item in best_per_code.items()
            },
            {"best_per_code": best_per_code, "results": results},
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"DB 网格寻优结果写入失败: {exc}", "WARN")
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "params": {
                "codes": codes,
                "capital": capital,
                "start": start_raw,
                "spacings": spacings,
                "levels": levels,
                "grid_values": GRID_OPT_VALUES if not explicit_shares else [],
                "shares": explicit_shares,
            },
            "best_per_code": best_per_code,
            "results": results,
        },
    }


def _api_models():
    from models import list_providers, load_config
    providers = list_providers()
    _biz("MODEL", f"厂商列表 {len(providers)} 个: "
                  f"{[(p['name'], p['model'], '已配置' if p['configured'] else '未配置Key') for p in providers]}")
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "default_provider": (load_config() or {}).get("default_provider", ""),
            "providers": providers,
        },
    }


def _api_db_stats(params=None):
    s = db.stats()
    _biz(
        "DB",
        f"统计 tables={s['tables']} 大小={s['size_bytes']}B "
        f"最新快照={s.get('latest_snapshot')}",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": s,
    }


def _api_logs(params=None):
    params = params or {}
    try:
        limit = min(max(int(params.get("limit", ["100"])[0]), 1), 500)
    except (TypeError, ValueError):
        limit = 100
    level = (params.get("level", [""])[0] or "").strip() or None
    if level is not None and level not in ("INFO", "WARN", "ERROR"):
        raise ValueError("level 需为 INFO/WARN/ERROR")
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"logs": db.recent_logs(limit, level)},
    }


def _api_db_tables(params=None):
    s = db.stats()
    s["tables_detail"] = db.list_tables()
    _biz("DB", f"表清单 {[(t['name'], t['count']) for t in s['tables_detail']]}")
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": s,
    }


def _api_db_table(params=None):
    params = params or {}
    name = (params.get("name", [""])[0] or "").strip()
    try:
        limit = int(params.get("limit", ["100"])[0] or "100")
        offset = int(params.get("offset", ["0"])[0] or "0")
    except (TypeError, ValueError):
        raise ValueError("limit/offset 需为整数")
    data = db.table_rows(name, limit, offset)
    _biz("DB", f"读表 {name} rows={len(data['rows'])} offset={offset}")
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": data,
    }


def _api_model_chat(body):
    from models import chat, ModelError
    provider = body.get("provider") or ""
    messages = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages 不能为空")
    system = "\n".join(
        str(m.get("content", "")) for m in messages if m.get("role") == "system"
    )
    user_text = "\n".join(
        str(m.get("content", "")) for m in messages if m.get("role") == "user"
    )
    text = chat(provider, system, user_text)
    _biz("MODEL", f"provider={provider or '(默认)'} 返回 {len(text)} 字")
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"provider": provider, "text": text},
    }


def _api_positions_parse(body):
    from positions_parser import parse_images, verify_parsed
    from models import ModelError

    provider = body.get("provider") or ""
    images = body.get("images") or []
    if not isinstance(images, list) or not 1 <= len(images) <= 6:
        raise ValueError("需要上传 1-6 张图片")
    for image in images:
        data_b64 = image.get("data_b64") or ""
        if not data_b64 or len(data_b64) > 6 * 1024 * 1024:
            raise ValueError("图片数据缺失或超过 6MB")
    _biz("POSITION-PARSE", f"provider={provider or '(默认)'} images={len(images)} 开始解析")
    parsed = parse_images(provider, images, log=_log)
    for index, holding in enumerate(parsed.get("holdings") or [], 1):
        _biz(
            "POSITION-PARSE",
            f"[持仓 {index}] {holding.get('code')} {holding.get('name')} "
            f"市值={holding.get('market_value')} 份额={holding.get('shares')} "
            f"来源={holding.get('source')}",
        )
    for index, trade in enumerate(parsed.get("trades") or [], 1):
        _biz(
            "POSITION-PARSE",
            f"[交易 {index}] {trade.get('date')} {trade.get('action')} "
            f"{trade.get('code')} {trade.get('name')} "
            f"价格={trade.get('price')} 数量={trade.get('shares')}",
        )
    _biz(
        "POSITION-PARSE",
        f"pipeline={parsed.get('parse_pipeline')} holdings={len(parsed.get('holdings') or [])} "
        f"trades={len(parsed.get('trades') or [])}",
    )

    codes = [
        str(h.get("code") or "").strip()
        for h in (parsed.get("holdings") or [])
        if h.get("code")
    ]
    realtime = None
    if codes:
        try:
            realtime = {"quotes": fetch_realtime_quotes(codes)}
        except Exception:
            realtime = None
    verified = verify_parsed(parsed, realtime)
    counts = verified.get("counts", {})
    _biz(
        "POSITION-PARSE",
        f"核验 status={verified.get('status')} 持仓 {counts.get('holdings_ok')}/{counts.get('holdings')} 通过 "
        f"错误 {counts.get('holdings_error')} 交易 {counts.get('trades')} 笔",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"provider": provider, "parsed": parsed, "verification": verified},
    }


def _api_positions_update(body):
    from positions_parser import update_positions
    verified = body.get("verified") or {}
    source = (body.get("source") or "AI 图片解析").strip()[:80]
    if not isinstance(verified, dict) or not verified.get("holdings"):
        raise ValueError("缺少核验后的持仓数据（请先解析并核验）")
    snapshot = update_positions(verified, source)
    _biz(
        "POSITION-UPDATE",
        f"持仓已更新: {len(snapshot['holdings'])} 只 / 交易 {len(snapshot['trades'])} 笔 "
        f"总资产 {snapshot['account_summary'].get('total_assets')}",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": snapshot,
    }


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "MomentumDashboard/1.0"

    def _request_started(self):
        self._req_t0 = time.time()
        self._resp_size = 0

    def log_message(self, fmt, *args):  # 标准请求行 + 耗时
        if self.path.startswith("/api/"):
            return  # API 请求已由 START / PARAMS / OUT 完整记录
        duration = ""
        if getattr(self, "_req_t0", None):
            duration = f" ({(time.time() - self._req_t0) * 1000:.0f}ms)"
        _log(fmt % args + duration)

    def _send_json(self, obj, status=200):
        body = json.dumps(
            _json_safe(obj), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        self._resp_size = len(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status, message):
        self._send_json(
            {
                "ok": False,
                "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "error": message,
            },
            status=status,
        )

    def _send_static(self, path):
        rel = path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self._send_error_json(404, "静态文件不存在")
            return
        ext = target.suffix.lower()
        content_type = MIME_TYPES.get(ext, "application/octet-stream")
        body = target.read_bytes()
        self._resp_size = len(body)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _summarize_response(result):
        """压缩展示响应结果，便于一眼看出“出场”内容。"""
        if not isinstance(result, dict):
            return f"type={type(result).__name__}"
        data = result.get("data")
        parts = []
        if isinstance(data, dict):
            keys = list(data.keys())
            parts.append(f"keys={keys[:8]}")
            for key, value in data.items():
                if isinstance(value, (list, dict)) and value:
                    parts.append(f"{key}=x{len(value)}")
                elif key in ("live", "status") and not isinstance(value, (dict, list)):
                    parts.append(f"{key}={value}")
        return " ".join(parts[:6]) or "data=empty"

    def do_GET(self):  # noqa: N802
        self._request_started()
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        cache_flag = ""
        if path == "/api/trace":
            msg = params.get("msg", [""])[0]
            _log(f"TRACE {msg[:300]}")
            self._send_json({"ok": True})
            return
        if path.startswith("/api/"):
            _log(f"START GET {self.path}")
            param_view = {key: value[0] for key, value in params.items()}
            _log(f"PARAMS {path} {json.dumps(param_view, ensure_ascii=False)}")
        try:
            if path == "/" or path == "/index.html":
                self._send_static("index.html")
                _log(f"END {path} (static) {self._resp_size}B")
                return
            if path.startswith("/static/"):
                self._send_static(path[len("/static/"):])
                _log(f"END {path} (static) {self._resp_size}B")
                return
            handler = ROUTES.get(path)
            if handler is None:
                _log(f"404 {path}", "WARN")
                self._send_error_json(404, f"未知接口: {path}")
                return
            result = handler(params)
            if isinstance(result, dict) and "cached" in result:
                if result.get("cached"):
                    cache_flag = " [cache]" + (" [stale]" if result.get("stale") else "")
                else:
                    cache_flag = " [fresh]"
            self._send_json(result)
            duration = (time.time() - self._req_t0) * 1000
            _log(
                f"OUT {path} 200{cache_flag} {duration:.0f}ms "
                f"{self._resp_size}B {self._summarize_response(result)}"
            )
        except ValueError as exc:
            _log(f"400 {path}: {exc}", "WARN")
            self._send_error_json(400, str(exc))
            _log(f"OUT {path} 400 {self._resp_size}B error={exc}")
        except Exception as exc:  # noqa: BLE001
            _log(f"500 {path}: {exc}", "ERROR")
            traceback.print_exc()
            self._send_error_json(500, str(exc))
            _log(f"OUT {path} 500 {self._resp_size}B error={exc}")

    def do_POST(self):  # noqa: N802
        self._request_started()
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            _log(f"404 {path}", "WARN")
            self._send_error_json(404, f"未知接口: {path}")
            return
        _log(f"START POST {self.path}")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > MAX_BODY_BYTES:
            _log(f"413 {path} 请求体 {length}B 超限", "WARN")
            self._send_error_json(413, f"请求体过大（上限 {MAX_BODY_BYTES} 字节）")
            return
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("请求体必须是 JSON 对象")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _log(f"400 {path}: JSON 解析失败 {exc}", "WARN")
            self._send_error_json(400, "请求体必须为 UTF-8 JSON")
            return
        except ValueError as exc:
            _log(f"400 {path}: {exc}", "WARN")
            self._send_error_json(400, str(exc))
            return

        handler = POST_ROUTES.get(path)
        if handler is None:
            _log(f"404 {path}", "WARN")
            self._send_error_json(404, f"未知接口: {path}")
            return
        try:
            result = handler(body)
            self._send_json(result)
            duration = (time.time() - self._req_t0) * 1000
            _log(
                f"OUT {path} 200 {duration:.0f}ms "
                f"{self._resp_size}B {self._summarize_response(result)}"
            )
        except ValueError as exc:
            _log(f"400 {path}: {exc}", "WARN")
            self._send_error_json(400, str(exc))
            _log(f"OUT {path} 400 {self._resp_size}B error={exc}")
        except ImportError as exc:
            _log(f"500 {path}: {exc}", "ERROR")
            traceback.print_exc()
            self._send_error_json(500, f"依赖模块加载失败: {exc}")
        except Exception as exc:  # noqa: BLE001 - 模型/上游错误统一 502
            _log(f"502 {path}: {exc}", "ERROR")
            traceback.print_exc()
            self._send_error_json(502, str(exc))
            _log(f"OUT {path} 502 {self._resp_size}B error={exc}")


def main():
    parser = argparse.ArgumentParser(description="动量轮动策略仪表盘")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--online",
        action="store_true",
        help="允许页面加载时联网刷新数据（默认离线缓存模式，刷新按钮才会联网）",
    )
    parser.add_argument(
        "--no-scheduler",
        action="store_true",
        help="禁用交易时段定时任务（默认启用：交易日 09:07-11:57 / 13:07-15:27 每 10 分钟）",
    )
    args = parser.parse_args()

    global ALLOW_ONLINE, _SCHEDULER
    ALLOW_ONLINE = args.online
    os.environ["ETF_DATA_OFFLINE"] = "0" if args.online else "1"

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    _log("=" * 64)
    _log(f"动量轮动策略仪表盘启动: http://{args.host}:{args.port}")
    _log(f"数据模式: {'联网(--online)' if args.online else '离线缓存（点刷新数据才联网）'}")
    _log(f"项目根目录: {PROJECT_DIR}")
    _log(f"静态资源: {STATIC_DIR}")
    _log(f"数据存储: db.py 后端={db.db_info()}")
    _log(f"缓存层: cache.py 后端={cache_backend_name()} (持久化/可切换 Redis 等)")
    today_log = ROOT / f"server-{datetime.now().strftime('%Y%m%d')}.log"
    _log(f"日志文件: {today_log}（按天命名，保留 7 天自动删除）")
    _log(f"信号池: {len(SIGNAL_POOLS)} 个预设, 回测预设: {len(BACKTEST_PRESETS)} 个")
    scheduler_enabled = not args.no_scheduler and os.environ.get(
        "MOMENTUM_SCHEDULER", "1"
    ) != "0"
    if scheduler_enabled:
        from scheduler import Scheduler
        _SCHEDULER = Scheduler(job=_scheduled_job, log=_log, name="sched")
        _SCHEDULER.start()
        _log("定时任务: 已启用（交易日 09:07-11:57 / 13:07-15:27 每 10 分钟）")
    else:
        _log("定时任务: 已禁用（--no-scheduler）")
    _seed_db_from_files()
    _seed_grid_triggers()
    maintenance = threading.Thread(
        target=_maintenance_loop, name="db-maint", daemon=True
    )
    maintenance.start()
    _log("日志保留策略: server-YYYYMMDD.log 按天命名保留 7 天；api_logs 自动清理 7 天前数据（每 6 小时）")
    _log("=" * 64)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("收到 Ctrl-C，服务停止", "WARN")
    finally:
        if _SCHEDULER is not None:
            _SCHEDULER.stop()
        _MAINT_STOP.set()
        server.server_close()


def _seed_db_from_files():
    """数据库为空时，把当前持仓文件作为首条快照导入（幂等）。"""
    try:
        current = db.stats()
        if current["tables"]["positions_snapshots"] == 0:
            snapshot = latest_positions()
            if snapshot:
                db.save_positions_snapshot(snapshot)
                _log(f"DB 已导入当前持仓快照: {snapshot.get('date')}")
        if current["tables"]["parse_history"] == 0:
            history_path = DATA_DIR / "ai_parse_history.json"
            try:
                history = json.loads(history_path.read_text(encoding="utf-8"))
                if isinstance(history, list):
                    for entry in history:
                        db.append_parse_history(
                            entry.get("updated_at") or entry.get("date") or "",
                            entry.get("source") or "",
                            entry.get("holdings_count") or 0,
                            entry.get("trades_count") or 0,
                            entry,
                        )
                    if history:
                        _log(f"DB 已导入解析历史 {len(history)} 条")
            except (OSError, json.JSONDecodeError):
                pass
    except Exception as exc:  # noqa: BLE001 - 种子导入失败不影响启动
        _log(f"DB 初始快照导入失败: {exc}", "WARN")


def _seed_grid_triggers():
    """把 data/grid_triggers.json 幂等导入 MySQL（按唯一键去重）。"""
    try:
        name_map = {}
        try:
            from tools import grid_trading as gt
            name_map = {
                code: (cfg or {}).get("name")
                for code, cfg in gt.CONFIGS.items()
            }
        except Exception:
            pass
        count = db.seed_grid_triggers_from_file(
            DATA_DIR / "grid_triggers.json", name_map=name_map
        )
        if count:
            _log(f"DB 已导入网格触发记录 {count} 条")
    except Exception as exc:  # noqa: BLE001
        _log(f"DB 网格触发导入失败: {exc}", "WARN")


def _grid_configs():
    """返回 (CONFIGS, code->name, code->shares_per_grid)。"""
    from tools import grid_trading as gt
    configs = gt.CONFIGS
    names = {code: (cfg or {}).get("name") for code, cfg in configs.items()}
    sizes = {
        code: (cfg or {}).get("shares_per_grid")
        for code, cfg in configs.items()
    }
    return configs, names, sizes


def _grid_base_chain(records: list[dict], configs: dict) -> list[dict]:
    """按时间顺序给记录计算基准价变化：
    网格类型每成交一次，基准价即变为成交价（before=当前基准，after=成交价）；
    加仓/减仓/动量不改变网格基准价（before=after=当前基准）。
    """
    base_cache: dict[str, float] = {}
    ordered = sorted(
        records,
        key=lambda r: (str(r.get("date") or ""), str(r.get("time") or "")),
    )
    for record in ordered:
        code = str(record.get("code") or "")
        if code not in base_cache:
            base = _num((configs.get(code) or {}).get("base_price"))
            existing = db.grid_triggers_for_code(code)
            if existing:
                last_base = _num(existing[-1].get("base_price_after"))
                if last_base is not None:
                    base = last_base
            base_cache[code] = base if base is not None else 0.0
        current = base_cache[code]
        trigger_type = (record.get("trigger_type") or "grid").strip() or "grid"
        price = _num(record.get("price"))
        if trigger_type == "grid" and price:
            record["base_price_before"] = current
            record["base_price_after"] = price
            base_cache[code] = price
        else:
            record["base_price_before"] = current
            record["base_price_after"] = current
    return ordered


def _rebuild_grid_triggers_file() -> int:
    """按 DB 中网格/加仓/减仓记录重建 data/grid_triggers.json（动量不入文件）。"""
    from tools import grid_trading as gt
    records = db.query_grid_triggers(limit=2000)
    triggers: dict[str, list] = {}
    for record in records:
        if (record.get("trigger_type") or "grid") == "momentum":
            continue
        trigger_date = str(record.get("trigger_date") or "")
        date_part, _, time_part = trigger_date.partition(" ")
        triggers.setdefault(record["code"], []).append({
            "date": date_part,
            "time": time_part,
            "action": record.get("action"),
            "type": record.get("trigger_type") or "grid",
            "price": str(record.get("price")),
            "shares": record.get("shares"),
            "base_price_before": (
                str(record.get("base_price_before"))
                if record.get("base_price_before") is not None else None
            ),
            "base_price_after": (
                str(record.get("base_price_after"))
                if record.get("base_price_after") is not None else None
            ),
        })
    for code in triggers:
        triggers[code].sort(
            key=lambda t: (str(t.get("date") or ""), str(t.get("time") or ""))
        )
    gt.save_triggers(triggers)
    return sum(len(items) for items in triggers.values())


def _recalc_grid_base_prices() -> int:
    """按新逻辑重算全部网格记录的基准价变化，并重建文件（启动时调用）。"""
    configs, _, _ = _grid_configs()
    codes = {row["code"] for row in db.query_grid_triggers(limit=2000)}
    changed = 0
    for code in codes:
        base = _num((configs.get(code) or {}).get("base_price")) or 0.0
        for row in db.grid_triggers_for_code(code):
            trigger_type = row.get("trigger_type") or "grid"
            price = _num(row.get("price"))
            before, after = base, base
            if trigger_type == "grid" and price:
                after = price
                base = price
            db.update_grid_trigger_base_prices(row["id"], before, after)
            changed += 1
    file_count = _rebuild_grid_triggers_file()
    _log(f"GRID-BASE 已重算 {changed} 条记录基准价变化，重建文件 {file_count} 条")
    return changed


def _sync_grid_triggers_file(records: list[dict]) -> bool:
    """追加触发记录到 data/grid_triggers.json（工具读取该文件），返回是否有新增。"""
    from tools import grid_trading as gt
    configs, _, _ = _grid_configs()
    path = DATA_DIR / "grid_triggers.json"
    triggers = {}
    try:
        triggers = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    if not isinstance(triggers, dict):
        triggers = {}
    changed = False
    for record in records:
        code = str(record.get("code") or "").strip()
        trigger_type = (record.get("trigger_type") or "grid").strip() or "grid"
        if trigger_type == "momentum":
            # 动量轮动成交不参与网格仓位/基准价计算，仅入库，不同步文件
            continue
        cfg = configs.get(code) or {}
        base = _num(cfg.get("base_price"))
        price = _num(record.get("price"))
        bb = _num(record.get("base_price_before"))
        ba = _num(record.get("base_price_after"))
        if bb is None:
            bb = base if base is not None else price
        if ba is None:
            ba = base if base is not None else price
        entry = {
            "date": record.get("date"),
            "action": record.get("action"),
            "type": trigger_type,
            "price": str(price),
            "shares": record.get("shares"),
            "base_price_before": str(bb),
            "base_price_after": str(ba),
        }
        trade_time = str(record.get("time") or "").strip()
        if trade_time:
            entry["time"] = trade_time
        history = triggers.setdefault(code, [])
        exists = any(
            t.get("date") == entry["date"]
            and t.get("action") == entry["action"]
            and str(t.get("price")) == entry["price"]
            and t.get("shares") == entry["shares"]
            and str(t.get("time") or "") == trade_time
            for t in history
        )
        if not exists:
            history.append(entry)
            changed = True
    if changed:
        for code in triggers:
            triggers[code] = sorted(
                triggers[code],
                key=lambda t: (
                    str(t.get("date") or ""),
                    str(t.get("time") or ""),
                ),
            )
        gt.save_triggers(triggers)
    return changed


def _api_grid_trigger_add(body):
    """手动录入一条网格触发记录（写 DB + 同步文件）。"""
    from grid_parser import verify_grid_records
    configs, names, _ = _grid_configs()
    code = str((body or {}).get("code") or "").strip()
    date = str((body or {}).get("date") or "").strip()
    trade_time = str((body or {}).get("time") or "").strip()
    action = str((body or {}).get("action") or "").strip().lower()
    trigger_type = str((body or {}).get("trigger_type") or "").strip().lower()
    price = (body or {}).get("price")
    shares = (body or {}).get("shares")
    records = verify_grid_records(
        [{
            "code": code,
            "date": date,
            "time": trade_time,
            "action": action,
            "trigger_type": trigger_type,
            "price": price,
            "shares": shares,
            "base_price_before": (body or {}).get("base_price_before"),
            "base_price_after": (body or {}).get("base_price_after"),
        }],
        known_codes=set(names),
    )
    record = records[0]
    if record["status"] == "error":
        raise ValueError("录入信息有误：" + "；".join(record["issues"]))
    if record["code"] not in names:
        raise ValueError(
            f"{record['code']} 不在网格标的中，无法手动录入（请从下拉框选择）"
        )
    record = _grid_base_chain([record], configs)[0]
    db_status = db.append_grid_trigger(
        record["code"],
        names.get(record["code"]),
        record["date"],
        record.get("time") or "",
        record["action"],
        record["price"],
        record["shares"],
        trigger_type=record.get("trigger_type") or "grid",
        base_price_before=record.get("base_price_before"),
        base_price_after=record.get("base_price_after"),
        source="manual",
    )
    _sync_grid_triggers_file([record])
    _biz(
        "GRID-TRIGGER",
        f"手动录入 {record['code']} {record['date']} {record['action']} "
        f"{record.get('time') or ''} {record['price']}×{record['shares']} → {db_status}",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"db_status": db_status, "record": record},
    }


def _api_grid_trigger_parse(body):
    """截图识别网格触发记录（视觉模型 或 OCR+文本模型），返回待核验记录。"""
    from grid_parser import parse_grid_images, verify_grid_records
    from models import ModelError
    _, names, sizes = _grid_configs()
    provider = (body or {}).get("provider") or ""
    images = (body or {}).get("images") or []
    if not isinstance(images, list) or not 1 <= len(images) <= 6:
        raise ValueError("需要上传 1-6 张图片")
    for image in images:
        data_b64 = image.get("data_b64") or ""
        if not data_b64 or len(data_b64) > 6 * 1024 * 1024:
            raise ValueError("图片数据缺失或超过 6MB")
    _biz("GRID-PARSE", f"provider={provider or '(默认)'} images={len(images)} 开始识别")
    parsed = parse_grid_images(provider, images, log=_log)
    records = verify_grid_records(
        parsed.get("trades") or [],
        known_codes=set(names),
        grid_sizes=sizes,
    )
    records = _mark_grid_duplicates(records)
    for index, record in enumerate(records, 1):
        _biz(
            "GRID-PARSE",
            f"[{index}/{len(records)}] {record.get('code')} "
            f"{record.get('date')} {record.get('action')} "
            f"{record.get('price')}×{record.get('shares')} "
            f"status={record.get('status')}"
            f"{' 重复' if record.get('duplicate') else ''}"
            f"{(' 问题=' + '；'.join(record.get('issues') or [])) if record.get('issues') else ''}"
            f"{(' 提示=' + '；'.join(record.get('warns') or [])) if record.get('warns') else ''}",
        )
    _biz(
        "GRID-PARSE",
        f"pipeline={parsed.get('parse_pipeline')} records={len(records)} "
        f"ok={sum(1 for r in records if r['status'] == 'ok')} "
        f"warn={sum(1 for r in records if r['status'] == 'warn')} "
        f"error={sum(1 for r in records if r['status'] == 'error')} "
        f"duplicate={sum(1 for r in records if r.get('duplicate'))}",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {
            "pipeline": parsed.get("parse_pipeline"),
            "provider": provider,
            "detected_platforms": parsed.get("detected_platforms") or [],
            "records": records,
        },
    }


def _mark_grid_duplicates(records: list[dict]) -> list[dict]:
    """与历史触发记录比对，标记 duplicate=True（同 code+date+time+金额+shares）。"""
    try:
        existing = db.query_grid_triggers(limit=2000)
        seen = {
            (
                str(row.get("code") or ""),
                str(row.get("trigger_date") or ""),
                round(
                    float(row.get("amount") or 0)
                    or (float(row.get("price") or 0) * int(row.get("shares") or 0)),
                    4,
                ),
                int(row.get("shares") or 0),
            )
            for row in existing
        }
        for record in records:
            price = _num(record.get("price")) or 0
            shares = int(record.get("shares") or 0)
            date = str(record.get("date") or "").strip()
            trade_time = str(record.get("time") or "").strip()
            combined = f"{date} {trade_time or '00:00:00'}" if date else ""
            key = (
                str(record.get("code") or ""),
                combined,
                round(price * shares, 4),
                shares,
            )
            record["duplicate"] = key in seen
    except Exception as exc:  # noqa: BLE001 - 比对失败不阻断识别
        _log(f"GRID-PARSE 历史比对失败: {exc}", "WARN")
        for record in records:
            record["duplicate"] = False
    return records


def _api_grid_trigger_confirm(body):
    """确认录入识别/手动编辑后的触发记录（写 DB + 同步文件）。"""
    from grid_parser import verify_grid_records
    configs, names, _ = _grid_configs()
    records = (body or {}).get("records") or []
    if not isinstance(records, list) or not records:
        raise ValueError("缺少待确认的触发记录")
    verified = verify_grid_records(records, known_codes=set(names))
    errors = [r for r in verified if r["status"] == "error"]
    if errors:
        detail = "；".join(
            f"{r.get('code')} {r.get('date')}: " + "，".join(r["issues"])
            for r in errors
        )
        raise ValueError(
            f"存在错误记录，请修正后重试（或点「移除需核对」删除不需要的记录）：{detail}"
        )
    # 按 日期+时间 升序录入，保证记录按时间顺序入库
    verified.sort(
        key=lambda r: (str(r.get("date") or ""), str(r.get("time") or ""))
    )
    verified = _grid_base_chain(verified, configs)
    added = []
    for record in verified:
        db_status = db.append_grid_trigger(
            record["code"],
            names.get(record["code"]),
            record["date"],
            record.get("time") or "",
            record["action"],
            record["price"],
            record["shares"],
            trigger_type=record.get("trigger_type") or "grid",
            base_price_before=record.get("base_price_before"),
            base_price_after=record.get("base_price_after"),
            source="ocr",
        )
        _biz(
            "GRID-TRIGGER",
            f"确认 {record['code']} {record['date']} {record['action']} "
            f"{record.get('time') or ''} {record['price']}×{record['shares']} → {db_status}",
        )
        added.append({**record, "db_status": db_status})
    _sync_grid_triggers_file(verified)
    _biz(
        "GRID-TRIGGER",
        f"确认录入 {len(added)} 条（新增 "
        f"{sum(1 for r in added if r['db_status'] == 'inserted')} / "
        f"重复 {sum(1 for r in added if r['db_status'] == 'duplicate')}）",
    )
    return {
        "ok": True,
        "cached": False,
        "stale": False,
        "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": {"added": len(added), "records": added},
    }


POST_ROUTES = {
    "/api/model/chat": _api_model_chat,
    "/api/positions/parse": _api_positions_parse,
    "/api/positions/update": _api_positions_update,
    "/api/scheduler/run": _api_scheduler_run,
    "/api/grid/triggers": _api_grid_trigger_add,
    "/api/grid/triggers/parse": _api_grid_trigger_parse,
    "/api/grid/triggers/confirm": _api_grid_trigger_confirm,
}


if __name__ == "__main__":
    main()
