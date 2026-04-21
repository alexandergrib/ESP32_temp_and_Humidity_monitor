import shutil
import subprocess
import sys
import unittest

from tests.support.path_setup import REPO_ROOT


class PlatformIoBuildTests(unittest.TestCase):
    def pio_command(self):
        candidates = [
            [sys.executable, "-m", "platformio"],
            ["python", "-m", "platformio"],
            [str(REPO_ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "platformio"],
        ]
        if shutil.which("platformio") is not None:
            candidates.insert(0, ["platformio"])
        for command in candidates:
            if command[0].endswith("python.exe") and not (REPO_ROOT / ".venv" / "Scripts" / "python.exe").exists():
                continue
            try:
                subprocess.run(
                    [*command, "--version"],
                    cwd=str(REPO_ROOT),
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=30,
                )
                return command
            except Exception:
                continue
        self.skipTest("PlatformIO unavailable")

    def run_pio(self, env_name):
        command = self.pio_command()
        result = subprocess.run(
            [*command, "run", "-e", env_name],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stdout[-4000:])

    def test_controller_firmware_builds(self):
        self.run_pio("controller_upload")

    def test_satellite_firmware_builds(self):
        self.run_pio("satellite_upload")


if __name__ == "__main__":
    unittest.main()
