import unittest
from datetime import datetime
from unittest.mock import patch

from tests.support.path_setup import LOGGER_ROOT  # noqa: F401
from temp_humidity_logger.esp_controller import EspControllerMixin


class EspHarness(EspControllerMixin):
    ARDUINO_CHANNEL_COUNT = 6
    ESP_CHANNEL_COUNT = 2
    CHANNEL_COUNT = 8

    def __init__(self):
        self.esp_node_state = {}
        self.esp_slot_by_node_id = {}
        self.current_signals = ["-"] * self.CHANNEL_COUNT
        self.current_temps = ["NaN"] * self.CHANNEL_COUNT
        self.current_hums = ["NaN"] * self.CHANNEL_COUNT
        self.channel_names = ["Arduino"] * self.ARDUINO_CHANNEL_COUNT + ["ESP Slot 1", "ESP Slot 2"]
        self.graph_contexts = {}
        self.source_connected = {"esp": True}
        self.esp_stream_confirmed = False
        self.esp_time_synced = True
        self.esp_init_job = None
        self.markers = []
        self.saved = []
        self.redraws = 0

    def current_interval_ms(self):
        return 1000

    def update_channel_tree_row(self, *args, **kwargs):
        pass

    def refresh_legend(self):
        pass

    def add_auto_marker(self, note, dt=None):
        self.markers.append((note, dt))

    def satellite_display_name(self, node_id):
        return "satellite"

    def allocate_esp_slot(self, node_id):
        if node_id not in self.esp_slot_by_node_id:
            self.esp_slot_by_node_id[node_id] = self.ARDUINO_CHANNEL_COUNT + len(self.esp_slot_by_node_id)
        return self.esp_slot_by_node_id[node_id]

    def format_signal_display(self, signal_pct=None, rssi_dbm=None):
        return "{0}%".format(signal_pct)

    def apply_calibration(self, kind, ch_idx, raw_value):
        return raw_value

    def _format_number(self, value):
        return str(round(float(value), 3)).rstrip("0").rstrip(".")

    def add_smoothed_point(self, channel_index, timestamp, temp_raw=None, hum_raw=None):
        self.saved.append(("smooth", channel_index, temp_raw, hum_raw))

    def save_to_db(self, timestamp):
        self.saved.append(("db", timestamp))

    def _schedule_redraw(self):
        self.redraws += 1

    def sync_esp_time(self, log_to_console=True):
        return True

    def send_esp_command(self, command):
        self.saved.append(("cmd", command))
        return True


class EspControllerTests(unittest.TestCase):
    def test_presence_timeout_scales_with_interval(self):
        harness = EspHarness()
        self.assertGreater(harness.esp_presence_timeout_seconds(20000), harness.esp_presence_timeout_seconds(1000))

    def test_interval_reduction_adds_grace(self):
        harness = EspHarness()
        state = {"online": True, "last_seen_monotonic": 100.0}
        with patch("temp_humidity_logger.esp_controller.time.monotonic", return_value=101.0):
            harness.apply_esp_interval_change_grace(state, 20000, 1000)
        self.assertGreater(state["presence_grace_until_monotonic"], 101.0)

    def test_node_bound_counts_as_online_without_offline_marker(self):
        harness = EspHarness()
        harness.process_esp_packet_line(
            '{"event":"node_bound","node_id":2,"name":"satellite","controller_time":"2026-04-21T08:15:43Z"}'
        )
        state = harness.esp_node_state[2]
        self.assertTrue(state["online"])
        self.assertEqual(harness.markers, [])

    def test_nan_sensor_reading_keeps_node_online_but_does_not_save_data(self):
        harness = EspHarness()
        harness.process_esp_packet_line(
            '{"event":"reading","node_id":2,"name":"satellite","temperature_c":nan,'
            '"humidity_pct":nan,"sensor_ok":false,"signal_pct":95,"rssi_dbm":-52,'
            '"controller_time":"2026-04-21T08:15:43Z"}'
        )
        slot = harness.esp_slot_by_node_id[2]
        self.assertTrue(harness.esp_node_state[2]["online"])
        self.assertEqual(harness.current_temps[slot], "NaN")
        self.assertFalse(any(item[0] == "db" for item in harness.saved))

    def test_valid_reading_saves_and_schedules_redraw(self):
        harness = EspHarness()
        harness.process_esp_packet_line(
            '{"event":"reading","node_id":2,"name":"satellite","temperature_c":22.5,'
            '"humidity_pct":40.5,"sensor_ok":true,"signal_pct":95,"rssi_dbm":-52,'
            '"controller_time":"2026-04-21T08:15:43Z"}'
        )
        self.assertTrue(any(item[0] == "db" for item in harness.saved))
        self.assertEqual(harness.redraws, 1)


if __name__ == "__main__":
    unittest.main()
