import unittest
from collections import deque
from datetime import datetime, timedelta

from tests.support.path_setup import LOGGER_ROOT  # noqa: F401
from temp_humidity_logger.smoothing import append_and_average


class SmoothingTests(unittest.TestCase):
    def test_arduino_uses_sample_window(self):
        history = deque()
        now = datetime(2026, 4, 21, 12, 0, 0)

        values = [
            append_and_average(history, now + timedelta(seconds=i), value, is_satellite=False, sample_window=3, time_window_s=120)
            for i, value in enumerate([10, 20, 30, 40])
        ]

        self.assertEqual(values[-1], 30.0)

    def test_satellite_uses_time_window(self):
        history = deque()
        now = datetime(2026, 4, 21, 12, 0, 0)

        append_and_average(history, now, 10, is_satellite=True, sample_window=3, time_window_s=60)
        result = append_and_average(history, now + timedelta(seconds=61), 30, is_satellite=True, sample_window=3, time_window_s=60)

        self.assertEqual(result, 30.0)
        self.assertEqual(len(history), 1)

    def test_satellite_zero_window_disables_smoothing(self):
        history = deque()
        now = datetime(2026, 4, 21, 12, 0, 0)

        append_and_average(history, now, 10, is_satellite=True, sample_window=3, time_window_s=0)
        result = append_and_average(history, now + timedelta(seconds=1), 30, is_satellite=True, sample_window=3, time_window_s=0)

        self.assertEqual(result, 30.0)


if __name__ == "__main__":
    unittest.main()
