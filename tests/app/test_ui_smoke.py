import tempfile
import unittest
from pathlib import Path

from tests.support.path_setup import LOGGER_ROOT  # noqa: F401


class UiSmokeTests(unittest.TestCase):
    def test_main_module_calls_main_when_executed(self):
        import runpy
        import sys
        import types
        from unittest import mock

        fake_app_module = types.ModuleType("temp_humidity_logger.app")
        fake_app_module.ArduinoLoggerApp = mock.Mock()

        with mock.patch.dict(sys.modules, {"temp_humidity_logger.app": fake_app_module}), mock.patch("tkinter.Tk") as tk_mock:
            root = tk_mock.return_value
            runpy.run_module("temp_humidity_logger.main", run_name="__main__")

        tk_mock.assert_called_once_with()
        fake_app_module.ArduinoLoggerApp.assert_called_once_with(root)
        root.protocol.assert_called_once()
        root.mainloop.assert_called_once_with()

    def test_app_info_includes_github_url(self):
        from temp_humidity_logger.settings_ui import SettingsUiMixin
        from temp_humidity_logger import version

        captured = {}

        class Dummy(SettingsUiMixin):
            APP_NAME = version.APP_NAME
            APP_VERSION = version.APP_VERSION
            GITHUB_URL = version.GITHUB_URL
            base_dir = "C:\\TempHumidityLogger"
            root = object()

        def fake_showinfo(title, message, parent=None):
            captured["title"] = title
            captured["message"] = message
            captured["parent"] = parent

        from temp_humidity_logger import settings_ui

        original_showinfo = settings_ui.messagebox.showinfo
        try:
            settings_ui.messagebox.showinfo = fake_showinfo
            Dummy().show_app_info()
        finally:
            settings_ui.messagebox.showinfo = original_showinfo

        self.assertEqual(captured["title"], "App Info")
        self.assertIn(version.APP_NAME, captured["message"])
        self.assertIn(version.APP_VERSION, captured["message"])
        self.assertIn(version.GITHUB_URL, captured["message"])

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
