"""Shared test path setup."""

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
LOGGER_ROOT = REPO_ROOT / "Temp_and_HumidityLogger"

for path in (str(REPO_ROOT), str(LOGGER_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)
