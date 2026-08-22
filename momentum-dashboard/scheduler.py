#!/usr/bin/env python3
"""交易时段定时调度器（短间隔轮询，可靠触发）。

规则：每个交易日（周一至周五）
  上午 09:07 ~ 11:57、下午 13:07 ~ 14:27，每 10 分钟触发一次（分钟个位为 7）；
  14:30 ~ 15:27，每 5 分钟触发一次（分钟个位为 2 或 7）。

实现：后台线程每 30 秒轮询一次，检查当前时刻是否命中调度刻度。
相比 threading.Timer 单次长延迟（可能 17+ 小时不触发），轮询方式更可靠。
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

POLL_INTERVAL = 30  # 轮询间隔（秒）


def in_window(hour_minute: int) -> bool:
    return (MORNING[0] <= hour_minute < MORNING[1]) or (
        AFTERNOON[0] <= hour_minute < AFTERNOON[1]
    )


def is_tick_minute(hour_minute: int, minute: int) -> bool:
    """判断某分钟是否为调度刻度（14:30 前每 10 分钟，之后每 5 分钟）。"""
    if not in_window(hour_minute):
        return False
    if hour_minute >= 14 * 60 + 30:
        return minute % 5 == 2
    return minute % 10 == PHASE_MINUTE


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
    """基于短间隔轮询的调度器：后台线程每 30 秒检查是否命中调度刻度。

    相比旧版 threading.Timer 单次长延迟（17+ 小时可能不触发），
    轮询方式确保即使进程长时间运行也不会错过调度窗口。
    """

    def __init__(self, job, log, name: str = "sched"):
        self._job = job
        self._log = log
        self._name = name
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        self.last_run: str | None = None
        self.last_result: str | None = None
        self.next_run: datetime | None = None
        self._last_fired_key: str | None = None  # "YYYY-MM-DD HH:MM" 防同分钟重复触发

    @property
    def running(self) -> bool:
        return self._running

    def start(self):
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stop_event.clear()
        self.next_run = next_tick()
        self._log(
            f"{self._name.upper()} 下次调度: {self.next_run:%Y-%m-%d %H:%M:%S} "
            f"（轮询模式，每 {POLL_INTERVAL}s 检查）"
        )
        self._thread = threading.Thread(
            target=self._poll_loop, name=f"{self._name}-poll", daemon=True
        )
        self._thread.start()

    def stop(self):
        with self._lock:
            self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _poll_loop(self):
        """轮询主循环：每 POLL_INTERVAL 秒检查一次是否命中调度刻度。"""
        while not self._stop_event.is_set():
            try:
                now = datetime.now(CN)
                hour_minute = now.hour * 60 + now.minute
                fire_key = now.strftime("%Y-%m-%d %H:%M")

                # 更新 next_run（每次轮询都刷新，确保 status 准确）
                self.next_run = next_tick(now)

                # 检查是否命中调度刻度且本分钟未触发过
                if (
                    now.weekday() < 5
                    and is_tick_minute(hour_minute, now.minute)
                    and fire_key != self._last_fired_key
                ):
                    self._last_fired_key = fire_key
                    self._fire(now)
            except Exception as exc:  # noqa: BLE001
                self._log(f"{self._name.upper()} 轮询异常: {exc}", "ERROR")

            self._stop_event.wait(POLL_INTERVAL)

    def _fire(self, now: datetime):
        """执行定时任务。"""
        self.last_run = now.isoformat(timespec="seconds")
        self.last_result = "running"
        self._log(f"{self._name.upper()} 定时任务触发: {now:%Y-%m-%d %H:%M:%S}")
        try:
            self._job()
            self.last_result = "ok"
        except Exception as exc:  # noqa: BLE001 - 定时任务异常不中断调度
            self.last_result = f"error: {exc}"
            self._log(f"{self._name.upper()} 任务异常: {exc}", "ERROR")

    def run_now(self):
        """立即执行一次（手动触发，不影响既定调度）。"""
        now = datetime.now(CN)
        self.last_run = now.isoformat(timespec="seconds")
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
