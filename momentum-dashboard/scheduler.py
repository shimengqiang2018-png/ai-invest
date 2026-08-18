#!/usr/bin/env python3
"""交易时段定时调度器（threading.Timer 链式）。

规则：每个交易日（周一至周五）
  上午 09:07 ~ 11:57、下午 13:07 ~ 14:27，每 10 分钟触发一次（分钟个位为 7）；
  14:30 ~ 15:27，每 5 分钟触发一次（分钟个位为 2 或 7）。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo


CN = ZoneInfo("Asia/Shanghai")

MORNING = (9 * 60 + 7, 12 * 60)          # 09:07 ~ 12:00
AFTERNOON = (13 * 60, 15 * 60 + 30)      # 13:00 ~ 15:30
PHASE_MINUTE = 7                          # 每 10 分钟一个刻度，落在 :07


def in_window(hour_minute: int) -> bool:
    return (MORNING[0] <= hour_minute < MORNING[1]) or (
        AFTERNOON[0] <= hour_minute < AFTERNOON[1]
    )


def next_tick(now: datetime | None = None) -> datetime:
    """返回下一个调度时刻（严格晚于 now）。

    14:30 前每 10 分钟（:07/:17/:27...），14:30 起每 5 分钟（:32/:37/:42...）。
    """
    now = now or datetime.now(CN)
    for ahead in range(1, 60 * 24 * 8):
        candidate = now + timedelta(minutes=ahead)
        if candidate.weekday() >= 5:
            continue
        hour_minute = candidate.hour * 60 + candidate.minute
        if not in_window(hour_minute):
            continue
        # 14:30 之后每 5 分钟，之前每 10 分钟
        if hour_minute >= 14 * 60 + 30:
            if candidate.minute % 5 == 2:
                return candidate
        elif candidate.minute % 10 == PHASE_MINUTE:
            return candidate
    # 兜底：下一个交易日的 09:07
    day = now.date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, dtime(9, PHASE_MINUTE), tzinfo=CN)


class Scheduler:
    """基于 threading.Timer 的链式调度：每次触发后计算下一次。"""

    def __init__(self, job, log, name: str = "sched"):
        self._job = job
        self._log = log
        self._name = name
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._running = False
        self.last_run: str | None = None
        self.last_result: str | None = None
        self.next_run: datetime | None = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self):
        with self._lock:
            if self._running:
                return
            self._running = True
        self._schedule()

    def stop(self):
        with self._lock:
            self._running = False
            if self._timer:
                self._timer.cancel()
                self._timer = None

    def _schedule(self):
        tick = next_tick()
        self.next_run = tick
        delay = max(1.0, (tick - datetime.now(CN)).total_seconds())
        self._log(
            f"{self._name.upper()} 下次调度: {tick:%Y-%m-%d %H:%M:%S} "
            f"（{delay / 60:.1f} 分钟后）"
        )
        with self._lock:
            if not self._running:
                return
            self._timer = threading.Timer(delay, self._tick)
            self._timer.daemon = True
            self._timer.start()

    def _tick(self):
        try:
            self.last_run = datetime.now(CN).isoformat(timespec="seconds")
            self.last_result = "running"
            try:
                self._job()
                self.last_result = "ok"
            except Exception as exc:  # noqa: BLE001 - 定时任务异常不中断调度
                self.last_result = f"error: {exc}"
                self._log(f"{self._name.upper()} 任务异常: {exc}", "ERROR")
        finally:
            self._schedule()

    def run_now(self):
        """立即执行一次（手动触发，不影响既定调度）。"""
        self.last_run = datetime.now(CN).isoformat(timespec="seconds")
        self.last_result = "running"
        try:
            self._job()
            self.last_result = "ok"
            return "ok"
        except Exception as exc:
            self.last_result = f"error: {exc}"
            raise

    def status(self) -> dict:
        return {
            "enabled": self._running,
            "next_run": (
                self.next_run.isoformat(timespec="seconds") if self.next_run else None
            ),
            "last_run": self.last_run,
            "last_result": self.last_result,
            "windows": {
                "morning": "09:07-11:57 (每10分钟)",
                "afternoon_early": "13:07-14:27 (每10分钟)",
                "afternoon_late": "14:32-15:27 (每5分钟)",
            },
            "timezone": "Asia/Shanghai",
        }


def seconds_until_next_tick() -> float:
    return max(0.0, (next_tick() - datetime.now(CN)).total_seconds())
