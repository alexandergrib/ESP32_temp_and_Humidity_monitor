import unittest
from datetime import datetime
from unittest.mock import patch

from tests.support.path_setup import LOGGER_ROOT  # noqa: F401
from temp_humidity_logger.channels import ChannelUiMixin
from temp_humidity_logger.esp_controller import EspControllerMixin


class EspHarness(EspControllerMixin):
    ARDUINO_CHANNEL_COUNT = 6
    ESP_CHANNEL_COUNT = 2
    CHANNEL_COUNT = 8

    def __init__(self):
        class Root:
            def after(self, delay_ms, callback):
                return ("after", delay_ms, callback)

            def after_cancel(self, job):
                pass

        self.root = Root()
        self.esp_presence_job = None
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
        self.last_esp_event_monotonic = 0.0
        self.last_esp_stream_recover_at = 0.0
        self.scheduled_esp_init_delays = []
        self.markers = []
        self.saved = []
        self.row_updates = []
        self.redraws = 0
        self.console = []
        self.interval_ms = 30000

    def current_interval_ms(self):
        return self.interval_ms

    def update_channel_tree_row(self, *args, **kwargs):
        self.row_updates.append((args, kwargs))

    def refresh_legend(self):
        pass

    def append_console(self, text):
        self.console.append(text)

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

    def schedule_esp_init(self, delay_ms):
        self.scheduled_esp_init_delays.append(delay_ms)


class BoolVarStub:
    def __init__(self, value=False):
        self.value = bool(value)

    def get(self):
        return self.value

    def set(self, value):
        self.value = bool(value)


class ChannelHarness(ChannelUiMixin):
    ARDUINO_CHANNEL_COUNT = 6

    def __init__(self):
        class Tree:
            def __init__(self):
                self.column = "#2"
                self.row = "ch6"

            def identify_row(self, y):
                return self.row

            def identify_column(self, x):
                return self.column

        self.root = object()
        self.tree = Tree()
        self.esp_node_state = {2: {"sleep_enabled": True}}
        self.esp_slot_by_node_id = {2: 6}
        self.sleep_commands = []

    def find_esp_node_id_by_slot(self, ch_idx):
        for node_id, slot_idx in self.esp_slot_by_node_id.items():
            if slot_idx == ch_idx:
                return node_id
        return None

    def send_satellite_sleep(self, ch_idx, enabled, parent=None):
        self.sleep_commands.append((ch_idx, enabled, parent))
        return True


class EspControllerTests(unittest.TestCase):
    def test_presence_timeout_scales_with_interval(self):
        harness = EspHarness()
        self.assertGreater(harness.esp_presence_timeout_seconds(20000), harness.esp_presence_timeout_seconds(1000))
        self.assertGreater(harness.esp_presence_timeout_seconds(60000), 200)
        self.assertAlmostEqual(harness.esp_expected_reading_gap_seconds(60000), 72.5)

    def test_interval_reduction_adds_grace(self):
        harness = EspHarness()
        state = {"online": True, "last_seen_monotonic": 100.0}
        with patch("temp_humidity_logger.esp_controller.time.monotonic", return_value=101.0):
            harness.apply_esp_interval_change_grace(state, 20000, 1000)
        self.assertGreater(state["presence_grace_until_monotonic"], 101.0)

    def test_set_all_satellite_sleep_sends_controller_command(self):
        harness = EspHarness()
        harness.sleep_all_var = BoolVarStub(False)
        state = harness.ensure_esp_node_state(2)
        state["slot_idx"] = harness.ARDUINO_CHANNEL_COUNT

        self.assertTrue(harness.set_all_satellite_sleep(True))

        self.assertIn(("cmd", "SLEEP ALL ON"), harness.saved)
        self.assertIn(("cmd", "NODES"), harness.saved)
        self.assertTrue(state["sleep_enabled"])
        self.assertTrue(harness.sleep_all_var.get())

    def test_set_all_satellite_sleep_requires_esp_connection(self):
        harness = EspHarness()
        harness.source_connected["esp"] = False

        with patch("temp_humidity_logger.esp_controller.messagebox.showwarning") as showwarning:
            self.assertFalse(harness.set_all_satellite_sleep(True))

        showwarning.assert_called_once()
        self.assertFalse(any(item == ("cmd", "SLEEP ALL ON") for item in harness.saved))

    def test_set_all_satellite_sleep_rejects_short_interval(self):
        harness = EspHarness()
        harness.interval_ms = 1000

        with patch("temp_humidity_logger.esp_controller.messagebox.showwarning") as showwarning:
            self.assertFalse(harness.set_all_satellite_sleep(True))

        showwarning.assert_called_once()
        self.assertFalse(any(item == ("cmd", "SLEEP ALL ON") for item in harness.saved))

    def test_short_interval_clears_known_sleep_state(self):
        harness = EspHarness()
        harness.sleep_all_var = BoolVarStub(True)
        state = harness.ensure_esp_node_state(2)
        state["slot_idx"] = harness.ARDUINO_CHANNEL_COUNT
        state["sleep_enabled"] = True

        self.assertTrue(harness.apply_esp_interval(1000))

        self.assertFalse(state["sleep_enabled"])
        self.assertFalse(harness.sleep_all_var.get())
        self.assertIn("ESP sleep mode disabled because interval is below 30s", harness.console)

    def test_sleep_menu_state_reflects_known_satellite_states(self):
        harness = EspHarness()
        harness.sleep_all_var = BoolVarStub(False)
        one = harness.ensure_esp_node_state(1)
        one["slot_idx"] = harness.ARDUINO_CHANNEL_COUNT
        one["sleep_enabled"] = True
        two = harness.ensure_esp_node_state(2)
        two["slot_idx"] = harness.ARDUINO_CHANNEL_COUNT + 1
        two["sleep_enabled"] = False

        harness.refresh_sleep_all_menu_state()
        self.assertFalse(harness.sleep_all_var.get())

        two["sleep_enabled"] = True
        harness.refresh_sleep_all_menu_state()
        self.assertTrue(harness.sleep_all_var.get())

    def test_channel_sleep_cell_reflects_satellite_state(self):
        harness = ChannelHarness()

        self.assertEqual(harness.channel_sleep_cell(0), "")
        self.assertEqual(harness.channel_sleep_cell(6), "\u2611")
        self.assertEqual(harness.channel_sleep_cell(7), "-")

        harness.esp_node_state[2]["sleep_enabled"] = False
        self.assertEqual(harness.channel_sleep_cell(6), "\u2610")

    def test_channel_sleep_column_click_toggles_satellite_sleep(self):
        harness = ChannelHarness()

        result = harness.on_tree_click(type("Event", (), {"x": 1, "y": 1})())

        self.assertEqual(result, "break")
        self.assertEqual(harness.sleep_commands, [(6, False, harness.root)])

    def test_node_bound_counts_as_online_without_offline_marker(self):
        harness = EspHarness()
        harness.process_esp_packet_line(
            '{"event":"node_bound","node_id":2,"name":"satellite","controller_time":"2026-04-21T08:15:43Z"}'
        )
        state = harness.esp_node_state[2]
        self.assertTrue(state["online"])
        self.assertEqual(harness.markers, [])

    def test_controller_offline_event_marks_satellite_offline(self):
        harness = EspHarness()
        harness.process_esp_packet_line(
            '{"event":"node_online","node_id":2,"name":"satellite","controller_time":"2026-04-21T08:15:43Z"}'
        )
        harness.process_esp_packet_line(
            '{"event":"node_offline","node_id":2,"name":"satellite","controller_time":"2026-04-21T08:20:43Z"}'
        )
        slot = harness.esp_slot_by_node_id[2]

        self.assertFalse(harness.esp_node_state[2]["online"])
        self.assertEqual(harness.current_signals[slot], "offline")
        self.assertTrue(any("lost connection" in item[0] for item in harness.markers))

    def test_app_presence_timer_does_not_mark_satellite_offline(self):
        harness = EspHarness()
        harness.process_esp_packet_line(
            '{"event":"reading","node_id":2,"name":"satellite","temperature_c":22.5,'
            '"humidity_pct":40.5,"sensor_ok":true,"signal_pct":95,"rssi_dbm":-52,'
            '"report_interval_ms":60000,"next_report_delay_ms":59000,'
            '"controller_time":"2026-04-21T08:15:43Z"}'
        )

        with patch("temp_humidity_logger.esp_controller.time.monotonic", return_value=100000.0):
            harness.check_esp_presence()

        self.assertTrue(harness.esp_node_state[2]["online"])
        self.assertFalse(any("lost connection" in item[0] for item in harness.markers))

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
        self.assertIn(((slot, "sensor error", "sensor error", "95%"), {}), harness.row_updates)
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

    def test_reading_updates_presence_schedule_for_long_interval(self):
        harness = EspHarness()
        with patch("temp_humidity_logger.esp_controller.time.monotonic", return_value=100.0):
            harness.process_esp_packet_line(
                '{"event":"reading","node_id":2,"name":"satellite","temperature_c":22.5,'
                '"humidity_pct":40.5,"sensor_ok":true,"signal_pct":95,"rssi_dbm":-52,'
                '"report_interval_ms":60000,"next_report_delay_ms":59000,'
                '"controller_time":"2026-04-21T08:15:43Z"}'
            )
        state = harness.esp_node_state[2]
        self.assertEqual(state["report_interval_ms"], 60000)
        self.assertEqual(state["next_report_delay_ms"], 59000)

        with patch("temp_humidity_logger.esp_controller.time.monotonic", return_value=165.0):
            harness.check_esp_presence()
        self.assertTrue(harness.esp_node_state[2]["online"])
        self.assertEqual(harness.markers, [])

    def test_controller_ready_restarts_stream_initialization_after_reset(self):
        harness = EspHarness()
        harness.esp_stream_confirmed = True
        harness.esp_time_synced = True

        harness.process_esp_packet_line('{"event":"controller_ready","channel":6}')

        self.assertFalse(harness.esp_stream_confirmed)
        self.assertFalse(harness.esp_time_synced)
        self.assertEqual(harness.scheduled_esp_init_delays, [200])

    def test_stream_watchdog_reenables_stream_after_quiet_period(self):
        harness = EspHarness()
        harness.esp_stream_confirmed = True
        harness.last_esp_event_monotonic = 100.0
        state = harness.ensure_esp_node_state(2)
        state["slot_idx"] = harness.ARDUINO_CHANNEL_COUNT
        state["report_interval_ms"] = 30000

        with patch("temp_humidity_logger.esp_controller.time.monotonic", return_value=200.0):
            self.assertTrue(harness.recover_esp_stream_if_stale())

        self.assertIn(("cmd", "STREAM ON"), harness.saved)
        self.assertIn(("cmd", "NODES"), harness.saved)
        self.assertIn(">>> [ESP] stream watchdog: STREAM ON / NODES", harness.console)


if __name__ == "__main__":
    unittest.main()
