import tempfile
import unittest
from pathlib import Path

from tests.support.path_setup import LOGGER_ROOT  # noqa: F401


class UiSmokeTests(unittest.TestCase):
    def test_tk_app_starts_and_closes(self):
        try:
            import tkinter as tk
        except Exception as ex:
            self.skipTest("tkinter unavailable: {0}".format(ex))

        with tempfile.TemporaryDirectory() as tmp:
            try:
                from temp_humidity_logger.app import ArduinoLoggerApp
            except ModuleNotFoundError as ex:
                self.skipTest("GUI dependency unavailable: {0}".format(ex))

            class TestApp(ArduinoLoggerApp):
                def resolve_runtime_dir(self, install_dir):
                    return str(Path(tmp))

            try:
                root = tk.Tk()
            except tk.TclError as ex:
                self.skipTest("Tk display unavailable: {0}".format(ex))
            root.withdraw()
            app = TestApp(root)
            root.update_idletasks()
            self.assertIn(app.APP_VERSION, app.app_title())
            self.assertTrue(hasattr(app, "tree"))
            app.on_close()


if __name__ == "__main__":
    unittest.main()
