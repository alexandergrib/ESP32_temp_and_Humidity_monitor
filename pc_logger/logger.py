"""Simple PC-side logger for the ESP32 controller.

Usage:
    python logger.py --port COM7
    python logger.py --port /dev/ttyUSB0 --csv readings.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime

try:
    import serial  # type: ignore
except ImportError:
    print("Missing dependency: pyserial\nInstall with: pip install pyserial", file=sys.stderr)
    raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True, help="Serial port, e.g. COM7 or /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--csv", default="readings.csv")
    args = parser.parse_args()

    with serial.Serial(args.port, args.baud, timeout=1) as ser, open(args.csv, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if f.tell() == 0:
            writer.writerow(["timestamp", "event", "node_id", "name", "temperature_c", "humidity_pct", "sensor_ok", "mac"])

        print(f"Listening on {args.port} @ {args.baud} baud")
        while True:
            raw = ser.readline().decode("utf-8", errors="replace").strip()
            if not raw:
                continue

            print(raw)
            if not raw.startswith("{"):
                continue

            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if obj.get("event") != "reading":
                continue

            writer.writerow([
                datetime.now().isoformat(timespec="seconds"),
                obj.get("event"),
                obj.get("node_id"),
                obj.get("name"),
                obj.get("temperature_c"),
                obj.get("humidity_pct"),
                obj.get("sensor_ok"),
                obj.get("mac"),
            ])
            f.flush()


if __name__ == "__main__":
    raise SystemExit(main())
