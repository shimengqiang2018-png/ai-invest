import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "momentum-dashboard"))

from scheduler import CN, in_window, next_tick  # noqa: E402


def dt(text):
    return datetime.fromisoformat(text).replace(tzinfo=CN)


class SchedulerTests(unittest.TestCase):
    def test_morning_first_tick(self):
        # 09:05 → 当日 09:07
        tick = next_tick(dt("2026-08-10T09:05:00"))  # 周一
        self.assertEqual("2026-08-10T09:07:00", tick.strftime("%Y-%m-%dT%H:%M:%S"))

    def test_morning_tick_after_phase(self):
        # 09:08 → 09:17
        tick = next_tick(dt("2026-08-10T09:08:00"))
        self.assertEqual("2026-08-10T09:17:00", tick.strftime("%Y-%m-%dT%H:%M:%S"))

    def test_afternoon_transition(self):
        # 11:57 之后 → 下午 13:07
        tick = next_tick(dt("2026-08-10T11:58:00"))
        self.assertEqual("2026-08-10T13:07:00", tick.strftime("%Y-%m-%dT%H:%M:%S"))

    def test_after_window_rolls_to_next_day(self):
        # 15:30 后 → 次日 09:07
        tick = next_tick(dt("2026-08-10T15:30:00"))
        self.assertEqual("2026-08-11T09:07:00", tick.strftime("%Y-%m-%dT%H:%M:%S"))

    def test_weekend_rolls_to_monday(self):
        # 周六 10:00 → 下周一 09:07
        tick = next_tick(dt("2026-08-15T10:00:00"))  # 周六
        self.assertEqual("2026-08-17T09:07:00", tick.strftime("%Y-%m-%dT%H:%M:%S"))

    def test_in_window_boundaries(self):
        self.assertTrue(in_window(9 * 60 + 7))
        self.assertTrue(in_window(11 * 60 + 57))
        self.assertFalse(in_window(12 * 60))
        self.assertTrue(in_window(13 * 60 + 7))
        self.assertTrue(in_window(15 * 60 + 27))
        self.assertFalse(in_window(15 * 60 + 30))


if __name__ == "__main__":
    unittest.main()
