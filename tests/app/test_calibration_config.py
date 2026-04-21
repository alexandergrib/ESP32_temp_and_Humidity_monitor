import unittest

from tests.support.path_setup import LOGGER_ROOT  # noqa: F401
from temp_humidity_logger.calibration import CalibrationMixin
from temp_humidity_logger.config_store import ConfigMixin
from temp_humidity_logger.serial_io import SerialIoMixin


class CalibrationHarness(CalibrationMixin):
    CHANNEL_COUNT = 2

    def __init__(self):
        self.temp_calibration_points = [[(0, 1), (10, 11)], []]
        self.hum_calibration_points = [[], [(30, 35)]]


class ConfigHarness(ConfigMixin, SerialIoMixin):
    ARDUINO_CHANNEL_COUNT = 6
    ESP_CHANNEL_COUNT = 8
    CHANNEL_COUNT = 14
    DEFAULT_INTERVAL_TEXT = "1s"
    SMOOTHING_WINDOW = 5
    SATELLITE_SMOOTHING_SECONDS = 120
    ARDUINO_BAUD_RATE = 9600
    ESP_BAUD_RATE = 460800
    DB_FILE_NAME = "logger.db"
    TEMP_DATA_FILE_NAME = "data_temperature.csv"
    HUM_DATA_FILE_NAME = "data_humidity.csv"
    PLOT_HISTORY_SECONDS = 100
    MAX_RENDER_POINTS = 2500
    ZOOM_FACTOR = 0.70
    DEFAULT_GRAPH_SPLIT_RATIO = 0.75


class CalibrationAndConfigTests(unittest.TestCase):
    def test_calibration_deduplicates_and_sorts_points(self):
        harness = CalibrationHarness()
        points = harness._normalize_calibration_points([(10, 20), ("2", "3"), (10, 21), ("bad", 3)])
        self.assertEqual(points, [(2.0, 3.0), (10.0, 21.0)])

    def test_piecewise_linear_correction(self):
        harness = CalibrationHarness()
        self.assertEqual(harness.apply_calibration("temp", 0, 5), 6.0)
        self.assertEqual(harness.apply_calibration("hum", 1, 40), 45.0)

    def test_runtime_settings_sanitise_bad_values(self):
        harness = ConfigHarness()
        clean = harness.sanitize_runtime_settings({
            "arduino_channel_count": "-10",
            "esp_channel_count": "100",
            "default_interval_text": "bad",
            "smoothing_window": "0",
            "satellite_smoothing_seconds": "-1",
            "arduino_baud_rate": "1",
            "esp_baud_rate": "460800",
            "db_file_name": "../bad",
            "temp_data_file_name": "temp",
            "hum_data_file_name": "hum.csv",
            "plot_history_seconds": "10",
            "max_render_points": "0",
            "zoom_factor": "5",
            "default_graph_split_ratio": "0.1",
        })

        self.assertEqual(clean["arduino_channel_count"], 1)
        self.assertEqual(clean["esp_channel_count"], 32)
        self.assertEqual(clean["default_interval_text"], "1s")
        self.assertEqual(clean["db_file_name"], "bad.db")
        self.assertEqual(clean["temp_data_file_name"], "temp.csv")
        self.assertEqual(clean["plot_history_seconds"], 60)
        self.assertEqual(clean["max_render_points"], 100)
        self.assertEqual(clean["zoom_factor"], 0.95)
        self.assertEqual(clean["default_graph_split_ratio"], 0.50)


if __name__ == "__main__":
    unittest.main()
