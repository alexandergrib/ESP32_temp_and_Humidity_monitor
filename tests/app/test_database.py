import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from tests.support.path_setup import LOGGER_ROOT  # noqa: F401
from temp_humidity_logger.config_store import ConfigMixin
from temp_humidity_logger.database import DatabaseMixin
from temp_humidity_logger.sessions import SessionUiMixin


class DatabaseHarness(ConfigMixin, DatabaseMixin, SessionUiMixin):
    CHANNEL_COUNT = 2
    DB_FILE_NAME = "test_logger.db"
    TEMP_DATA_FILE_NAME = "temperature.csv"
    HUM_DATA_FILE_NAME = "humidity.csv"

    def __init__(self, base_dir):
        self.base_dir = str(base_dir)
        self.db_conn = None
        self.db_session_id = None
        self.last_session_id = None
        self.current_temps = ["", ""]
        self.current_hums = ["", ""]
        self.channel_record_enabled = [True, True]
        self.console = []

    def append_console(self, text):
        self.console.append(text)


class DatabaseTests(unittest.TestCase):
    def test_session_reading_marker_and_csv_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = DatabaseHarness(Path(tmp))
            harness.init_database()
            harness.start_db_session()
            session_id = harness.db_session_id
            harness.current_temps = ["22.1", "23.2"]
            harness.current_hums = ["40.1", "41.2"]
            harness.save_to_db(datetime(2026, 4, 21, 8, 0, 0))
            harness._save_marker_to_db("ADD", datetime(2026, 4, 21, 8, 0, 1), "test marker")
            harness.end_db_session()

            self.assertTrue(harness.session_has_data(session_id))
            rows = list(harness.iter_session_rows(session_id, "temp"))
            self.assertEqual(rows[0][3], "22.1")
            self.assertEqual(rows[1][1], "ADD")
            self.assertEqual(rows[1][2], "test marker")

            harness.append_session_to_data_csv(session_id)
            exported = list(Path(tmp).glob("*.csv"))
            self.assertEqual(len(exported), 2)
            self.assertTrue(any("temperature" in p.name for p in exported))
            harness.db_conn.close()

    def test_safe_filename_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = DatabaseHarness(Path(tmp))
            self.assertEqual(harness._safe_filename_part("Room 1 / Upstairs"), "Room_1_Upstairs")
            self.assertEqual(harness._safe_filename_part("..."), "")


if __name__ == "__main__":
    unittest.main()
