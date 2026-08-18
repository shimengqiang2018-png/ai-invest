#!/usr/bin/env python3
"""业务日志与子进程执行器（从 server.py 拆出）。

- 日志：终端 + 按天轮转 server-YYYYMMDD.log + DB 镜像（api_logs）
- 子进程：统一运行 tools/ 脚本，超时自动 kill，解析脚本 JSON 输出
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402 - 数据访问层


PROJECT_DIR = ROOT.parent
TOOLS_DIR = PROJECT_DIR / "tools"

LOG_FILE = ROOT / "server.log"  # 兼容旧引用（实际日志走 server-YYYYMMDD.log）

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


def _log(message, level="INFO"):
    """输出到终端 + server-YYYYMMDD.log（logging 框架，按天轮转保留 7 天）。"""
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
        pass  # 日志入库失败不影响主流程


def _biz(tag, message):
    """业务日志：记录策略/组合/信号的业务摘要，统一 [BIZ] 前缀便于 grep。"""
    _log(f"[BIZ] {tag} {message}", "INFO")


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


def run_script_stream(args, timeout=300, offline=True, on_line=None):
    """运行脚本并逐行回调 stdout，用于长任务的进度展示。

    与 run_script 相同，offline=True 时只读缓存不联网；但 stdout 边产生边
    回调 on_line(line)，调用方可用它更新任务进度。stderr 单独收集，避免阻塞。
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env["ETF_DATA_OFFLINE"] = "1" if offline else "0"
    cmd = [sys.executable, str(TOOLS_DIR / args[0]), *args[1:]]
    desc = " ".join(args)
    _log(
        f"RUN python3 {desc} (stream, offline={'是' if offline else '否'}, timeout={timeout}s)"
    )
    started = time.time()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(PROJECT_DIR),
        env=env,
    )
    stdout_chunks = []
    stderr_chunks = []

    def _drain(stream, sink, callback):
        try:
            for line in iter(stream.readline, ""):
                sink.append(line)
                if callback:
                    callback(line)
        finally:
            stream.close()

    t_out = threading.Thread(
        target=_drain, args=(proc.stdout, stdout_chunks, on_line), daemon=True
    )
    t_err = threading.Thread(
        target=_drain, args=(proc.stderr, stderr_chunks, None), daemon=True
    )
    t_out.start()
    t_err.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise RuntimeError(f"脚本执行超时（>{timeout}s）: {desc}")
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    duration = time.time() - started
    if proc.returncode != 0:
        detail = ("".join(stderr_chunks) or "".join(stdout_chunks) or "").strip()[-800:]
        raise RuntimeError(f"脚本执行失败 (exit {proc.returncode}): {detail}")
    _log(f"RUN 完成 {desc} exit=0 ({duration:.1f}s)")
    return "".join(stdout_chunks)


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
