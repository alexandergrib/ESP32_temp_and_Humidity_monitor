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


if __name__ == "__main__":
    unittest.main()
