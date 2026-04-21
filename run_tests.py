"""Run local app and firmware tests."""

import argparse
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def run_unittest(pattern):
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=pattern)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def compile_logger():
    files = [ROOT / "Temp_and_HumidityLogger" / "arduino_logger_v72.py"]
    files.extend((ROOT / "Temp_and_HumidityLogger" / "temp_humidity_logger").glob("*.py"))
    result = subprocess.run([sys.executable, "-m", "py_compile", *map(str, files)], cwd=str(ROOT))
    return result.returncode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--suite",
        choices=("all", "app", "firmware", "fast"),
        default="all",
        help="fast excludes PlatformIO build tests",
    )
    args = parser.parse_args()

    rc = compile_logger()
    if rc != 0:
        return rc

    if args.suite == "app":
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests" / "app"), pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    if args.suite == "firmware":
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests" / "firmware"), pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    if args.suite == "fast":
        suite = unittest.TestSuite()
        for test_dir in (ROOT / "tests" / "app", ROOT / "tests" / "firmware"):
            pattern = "test_static_contracts.py" if test_dir.name == "firmware" else "test_*.py"
            suite.addTests(unittest.defaultTestLoader.discover(str(test_dir), pattern=pattern))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    return run_unittest("test_*.py")


if __name__ == "__main__":
    raise SystemExit(main())
