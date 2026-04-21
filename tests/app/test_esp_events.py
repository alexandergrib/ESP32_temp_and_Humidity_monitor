import unittest

from tests.support.path_setup import LOGGER_ROOT  # noqa: F401
from temp_humidity_logger.esp_events import parse_esp_event_json


class EspEventParserTests(unittest.TestCase):
    def test_parses_prefixed_json(self):
        event = parse_esp_event_json('[ESP] {"event":"reading","node_id":2}')
        self.assertEqual(event["event"], "reading")
        self.assertEqual(event["node_id"], 2)

    def test_accepts_lowercase_bare_nan(self):
        event = parse_esp_event_json(
            '[ESP] {"event":"reading","temperature_c":nan,"humidity_pct":nan,"sensor_ok":false}'
        )
        self.assertIsNone(event["temperature_c"])
        self.assertIsNone(event["humidity_pct"])
        self.assertFalse(event["sensor_ok"])

    def test_invalid_or_missing_json_returns_none(self):
        self.assertIsNone(parse_esp_event_json("plain text"))
        self.assertIsNone(parse_esp_event_json('{"event":'))


if __name__ == "__main__":
    unittest.main()
