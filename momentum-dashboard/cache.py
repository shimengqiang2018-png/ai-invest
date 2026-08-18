#!/usr/bin/env python3
"""缓存抽象层（零第三方依赖）。

独立于业务代码的缓存层：业务代码（server.py 等）只允许调用本模块的
`get / set / cached / delete_expired / flush`，不直接接触具体缓存工具。

后端通过适配器实现，当前提供：
  - db     默认后端，复用 db.py 的 cache 表（生产为 MySQL，持久化、带 TTL）
  - memory 进程内字典（仅测试用，不跨进程、不持久化）

后续切换缓存工具（如 Redis / Memcached）时，只需新增一个适配器并修改
`configure()` 的默认后端，业务代码无需改动。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime


_CONFIG_LOCK = threading.Lock()
_backend_name = "db"
_logger = None

# 按 key 独立的生产锁：不同缓存 key 的重算互不阻塞，同 key 并发去重（double-check）
_KEY_LOCKS: dict[str, threading.Lock] = {}
_KEY_LOCKS_GUARD = threading.Lock()


def _key_lock(key: str) -> threading.Lock:
    with _KEY_LOCKS_GUARD:
        lock = _KEY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _KEY_LOCKS[key] = lock
        return lock


class _MemoryBackend:
    """进程内内存缓存（带 TTL），用于测试或 DB 不可用时的降级。"""

    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def get(self, key, ttl=None):
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            payload, updated_at = item
            if ttl is not None and time.time() - updated_at > ttl:
                return None
            return payload

    def set(self, key, payload):
        with self._lock:
            self._data[key] = (payload, time.time())

    def delete_expired(self):
        cutoff = time.time() - 7 * 86400
        with self._lock:
            expired = [k for k, (_, ts) in self._data.items() if ts < cutoff]
            for key in expired:
                self._data.pop(key, None)
            return len(expired)

    def flush(self):
        with self._lock:
            size = len(self._data)
            self._data.clear()
            return size


class _DbBackend:
    """持久化后端：复用 db.py 的 cache 表（当前为 MySQL，跨进程、持久化）。"""

    def get(self, key, ttl=None):
        from db import cache_get
        return cache_get(key, ttl)

    def set(self, key, payload):
        from db import cache_set
        cache_set(key, payload)

    def delete_expired(self):
        from db import cache_delete_expired
        return cache_delete_expired()

    def flush(self):
        from db import cache_flush
        return cache_flush()


_BACKENDS = {
    "memory": _MemoryBackend(),
    "db": _DbBackend(),
}


def configure(backend: str = "db") -> str:
    """切换缓存后端（"db" 持久化 / "memory" 内存）。"""
    if backend not in _BACKENDS:
        raise ValueError(f"未知缓存后端: {backend}（可选 {sorted(_BACKENDS)}）")
    with _CONFIG_LOCK:
        global _backend_name
        _backend_name = backend
    return backend


def backends() -> list:
    """返回可用后端列表。"""
    return sorted(_BACKENDS)


def backend_name() -> str:
    """返回当前生效的后端名。"""
    return _backend_name


def set_logger(fn):
    """注入日志回调 fn(message, level)，用于 cached() 输出缓存事件。"""
    global _logger
    _logger = fn


def _log(message, level="INFO"):
    if _logger is not None:
        try:
            _logger(message, level)
        except Exception:
            pass


def _backend():
    return _BACKENDS[_backend_name]


def get(key: str, ttl: float | None = None):
    """按 TTL 读取缓存；未命中 / 过期返回 None。ttl=None 忽略过期时间。"""
    return _backend().get(key, ttl)


def set(key: str, payload: dict) -> None:
    """写入缓存（覆盖旧值，刷新 updated_at）。"""
    _backend().set(key, payload)


def delete_expired() -> int:
    """清理过期缓存，返回删除条数。"""
    return _backend().delete_expired()


def flush() -> int:
    """清空缓存，返回删除条数。"""
    return _backend().flush()


def cached(key, ttl, refresh, producer):
    """带 TTL 的结果缓存：命中直接返回；refresh=True 强制重算；

    重算失败时自动回退旧缓存（标记 stale），无缓存则原样抛出异常。
    返回 (payload, from_cache: bool, is_stale: bool)。
    """
    if not refresh:
        payload = get(key, ttl)
        if payload is not None:
            payload["cached"] = True
            _log(f"CACHE 命中 [{key}] ttl={ttl}s", "INFO")
            return payload, True, False
    if refresh:
        _log(f"CACHE 强制重算 [{key}] (refresh=1)", "INFO")
    started = time.time()
    with _key_lock(key):
        if not refresh:
            # double-check：等待期间其他线程可能已生成该 key
            payload = get(key, ttl)
            if payload is not None:
                payload["cached"] = True
                _log(f"CACHE 命中 [{key}]（等待期间已生成）", "INFO")
                return payload, True, False
        try:
            data = producer()
            payload = {
                "ok": True,
                "cached": False,
                "stale": False,
                "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "data": data,
            }
            set(key, payload)
            _log(f"CACHE 重算完成 [{key}] ({time.time() - started:.1f}s)", "INFO")
            return payload, False, False
        except Exception as exc:
            stale = get(key, ttl=None)  # 忽略 TTL 取旧缓存
            if stale is not None:
                stale["cached"] = True
                stale["stale"] = True
                stale["stale_error"] = str(exc)
                _log(f"CACHE 重算失败，回退旧缓存 [{key}]: {exc}", "WARN")
                return stale, True, True
            _log(f"CACHE 重算失败且无缓存 [{key}]: {exc}", "ERROR")
            # 保留原始异常类型（ValueError→400，RuntimeError→500）
            raise
