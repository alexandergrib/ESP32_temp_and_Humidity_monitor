import unittest
import tempfile
from pathlib import Path

from tests.support.path_setup import LOGGER_ROOT  # noqa: F401
from temp_humidity_logger.serial_io import SerialIoMixin


class SerialHarness(SerialIoMixin):
    def __init__(self):
        self.base_dir = ""
        self.terminal_output_logging_enabled = False
        self.terminal_output_log_error_reported = False
        self.txt_console = None
        self.floating_console_text = None
        self.widget_output = []

    def _append_to_console_widget(self, widget, text, auto_newline=True):
        self.widget_output.append((widget, text, auto_newline))


class DisconnectHarness(SerialIoMixin):
    ARDUINO_CHANNEL_COUNT = 1
    CHANNEL_COUNT = 2

    def __init__(self):
        class Root:
            def after_cancel(self, _job):
                pass

        class Button:
            def __init__(self):
                self.config_calls = []

            def config(self, **kwargs):
                self.config_calls.append(kwargs)

        class SerialPort:
            port = "COM1"
            is_open = True

            def close(self):
                self.is_open = False

        self.root = Root()
        self.arduino_poll_job = None
        self.arduino_polling_started = False
        self.arduino_info = {}
        self.esp_init_job = None
        self.esp_init_attempts_remaining = 0
        self.esp_stream_confirmed = True
        self.esp_time_synced = True
        self.esp_node_state = {}
        self.current_signals = ["-", "-"]
        self.serial_ports = {"arduino": None, "esp": SerialPort()}
        self.source_connected = {"arduino": False, "esp": True}
        self.stop_events = {"arduino": type("Event", (), {"set": lambda _self: None})(),
                            "esp": type("Event", (), {"set": lambda _self: None})()}
        self.read_threads = {"arduino": None, "esp": object()}
        self.receive_buffers = {"arduino": "", "esp": "partial"}
        self.btn_connect_arduino = Button()
        self.btn_connect_esp = Button()
        self.db_session_id = 42
        self.last_session_id = None
        self.loaded_session_id = None
        self.calls = []

    def end_db_session(self):
        self.calls.append("end_db_session")

    def append_session_to_data_csv(self, session_id):
        self.calls.append(("append_session_to_data_csv", session_id))

    def refresh_graph_titles(self):
        self.calls.append("refresh_graph_titles")

    def refresh_sessions_list(self):
        self.calls.append("refresh_sessions_list")

    def update_status_label(self):
        self.calls.append("update_status_label")

    def update_sessions_controls(self):
        self.calls.append("update_sessions_controls")

    def rebuild_channel_tree(self):
        self.calls.append("rebuild_channel_tree")

    def _schedule_redraw(self):
        self.calls.append("_schedule_redraw")

    def update_channel_tree_row(self, *args, **kwargs):
        self.calls.append(("update_channel_tree_row", args, kwargs))

    def any_source_connected(self):
        return any(self.source_connected.values())


class SerialIoTests(unittest.TestCase):
    def setUp(self):
        self.harness = SerialHarness()

    def test_interval_parser(self):
        self.assertEqual(self.harness.parse_interval_ms("500ms"), 500)
        self.assertEqual(self.harness.parse_interval_ms("1s"), 1000)
        self.assertEqual(self.harness.parse_interval_ms("2min"), 120000)
        self.assertEqual(self.harness.parse_interval_ms("1h"), 3600000)
        self.assertIsNone(self.harness.parse_interval_ms("1"))
        self.assertIsNone(self.harness.parse_interval_ms("bad"))

    def test_extract_json_objects_handles_braces_in_strings(self):
        raw = 'noise {"event":"a","text":"brace } inside"} middle {"event":"b"} tail'
        objects = self.harness.extract_json_objects(raw)

        self.assertEqual(len(objects), 2)
        self.assertIn('"event":"a"', objects[0][2])
        self.assertIn('"event":"b"', objects[1][2])

    def test_terminal_output_logging_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.harness.base_dir = tmp
            self.harness.append_console("[ESP] one")

            self.assertFalse((Path(tmp) / "terminal_output.log").exists())

    def test_terminal_output_logging_appends_console_text_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.harness.base_dir = tmp
            self.harness.terminal_output_logging_enabled = True

            self.harness.append_console("[ESP] one")
            self.harness.append_console("[ESP] two", auto_newline=False)

            log_path = Path(tmp) / "terminal_output.log"
            self.assertEqual(log_path.read_text(encoding="utf-8"), "[ESP] one\n[ESP] two")

    def test_final_disconnect_keeps_finished_session_loaded_in_graph(self):
        harness = DisconnectHarness()

        harness.disconnect_source("esp")

        self.assertFalse(harness.source_connected["esp"])
        self.assertIsNone(harness.db_session_id)
        self.assertEqual(harness.last_session_id, 42)
        self.assertEqual(harness.loaded_session_id, 42)
        self.assertIn("refresh_graph_titles", harness.calls)
        self.assertIn("_schedule_redraw", harness.calls)


if __name__ == "__main__":
    unittest.main()
