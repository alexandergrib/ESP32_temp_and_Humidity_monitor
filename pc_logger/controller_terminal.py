"""Simple serial terminal for the controller with automatic time sync.

Usage:
    python controller_terminal.py --port COM6
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

try:
    import serial  # type: ignore
except ImportError:
    print("Missing dependency: pyserial\nInstall with: pip install pyserial", file=sys.stderr)
    raise


def send_line(ser: serial.Serial, line: str) -> None:
    ser.write((line + "\n").encode("utf-8"))
    ser.flush()


def sync_time(ser: serial.Serial) -> None:
    send_line(ser, f"TIME SET {int(time.time())}")


def serial_reader(ser: serial.Serial, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            raw = ser.readline()
        except serial.SerialException:
            break
        if not raw:
            continue
        sys.stdout.write(raw.decode("utf-8", errors="replace"))
        sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True, help="Controller serial port, e.g. COM6")
    parser.add_argument("--baud", type=int, default=460800)
    args = parser.parse_args()

    with serial.Serial(args.port, args.baud, timeout=0.2) as ser:
        ser.dtr = False
        ser.rts = False
        time.sleep(0.3)
        ser.reset_input_buffer()
        sync_time(ser)

        stop_event = threading.Event()
        reader = threading.Thread(target=serial_reader, args=(ser, stop_event), daemon=True)
        reader.start()

        print(f"Connected to {args.port} @ {args.baud}. Controller time synced.")
        print("Type commands and press Enter. Type /sync to resend time, /quit to exit.")

        try:
            while True:
                try:
                    line = input()
                except EOFError:
                    break

                command = line.strip()
                if command == "/quit":
                    break
                if command == "/sync":
                    sync_time(ser)
                    continue

                send_line(ser, line)
        except KeyboardInterrupt:
            pass
        finally:
            stop_event.set()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
