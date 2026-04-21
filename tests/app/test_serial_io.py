import unittest

from tests.support.path_setup import LOGGER_ROOT  # noqa: F401
from temp_humidity_logger.serial_io import SerialIoMixin


class SerialHarness(SerialIoMixin):
    pass


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


if __name__ == "__main__":
    unittest.main()
