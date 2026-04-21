import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_open_logs_folder_uses_runtime_data_dir(self):
        from temp_humidity_logger.settings_ui import SettingsUiMixin

        class Dummy(SettingsUiMixin):
            base_dir = "C:\\TempHumidityLogger"
            root = object()

        with mock.patch("temp_humidity_logger.settings_ui.os.makedirs") as makedirs:
            with mock.patch("temp_humidity_logger.settings_ui.sys.platform", "win32"):
                with mock.patch("temp_humidity_logger.settings_ui.os.startfile", create=True) as startfile:
                    Dummy().open_logs_folder()

        makedirs.assert_called_once_with(Dummy.base_dir, exist_ok=True)
        startfile.assert_called_once_with(Dummy.base_dir)

    def test_menu_labels_include_logs_and_sleep_all(self):
        source = (LOGGER_ROOT / "temp_humidity_logger" / "app.py").read_text(encoding="utf-8")

        self.assertIn('label="Open logs folder..."', source)
        self.assertIn('label="Satellite sleep mode ALL on/off"', source)

    def test_windows_build_uses_app_icon(self):
        build_script = (LOGGER_ROOT / "build_exe.ps1").read_text(encoding="utf-8")
        spec = (LOGGER_ROOT / "TempHumidityLogger.spec").read_text(encoding="utf-8")
        icon = LOGGER_ROOT / "icons" / "logo.ico"

        self.assertIn('--icon "icons\\logo.ico"', build_script)
        self.assertIn('--add-data "icons\\logo.ico;icons"', build_script)
        self.assertIn('--add-data "icons\\logo1.png;icons"', build_script)
        self.assertIn("icon=['icons\\\\logo.ico']", spec)
        with icon.open("rb") as fh:
            header = fh.read(6)
        self.assertEqual(header[:4], b"\x00\x00\x01\x00")
        self.assertGreaterEqual(int.from_bytes(header[4:6], "little"), 5)

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
