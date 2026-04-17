"""Simple serial terminal for the controller with automatic time sync.

Usage:
    python controller_terminal.py --port COM6
    python controller_terminal.py --port COM6 --rename 3 "Boiler Room"
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


SATELLITE_NAME_MAX_LEN = 15


def send_line(ser: serial.Serial, line: str) -> None:
    ser.write((line + "\n").encode("utf-8"))
    ser.flush()


def sync_time(ser: serial.Serial) -> None:
    send_line(ser, f"TIME SET {int(time.time())}")


def sanitize_satellite_name(name: str) -> str:
    sanitized: list[str] = []
    for ch in name.strip():
        if len(sanitized) >= SATELLITE_NAME_MAX_LEN:
            break
        if ch.isascii() and (ch.isalnum() or ch in "_- "):
            sanitized.append(ch)
    safe_name = "".join(sanitized).strip()
    return safe_name or "satellite"


def send_rename(ser: serial.Serial, node_id: int, requested_name: str) -> str:
    safe_name = sanitize_satellite_name(requested_name)
    send_line(ser, f"RENAME {node_id} {safe_name}")
    return safe_name


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
    parser.add_argument(
        "--rename",
        nargs=2,
        action="append",
        metavar=("NODE_ID", "NAME"),
        default=[],
        help="Rename a satellite and exit. Can be passed more than once.",
    )
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

        if args.rename:
            for node_id_text, requested_name in args.rename:
                try:
                    node_id = int(node_id_text)
                except ValueError:
                    print(f"Invalid node id for --rename: {node_id_text}", file=sys.stderr)
                    stop_event.set()
                    return 2
                sent_name = send_rename(ser, node_id, requested_name)
                print(f"Sent rename: node {node_id} -> {sent_name}")
            time.sleep(1.5)
            stop_event.set()
            return 0

        print(f"Connected to {args.port} @ {args.baud}. Controller time synced.")
        print("Type commands and press Enter. Type /sync to resend time, /rename <nodeId> <name> to rename a satellite, /quit to exit.")

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
                if command.startswith("/rename "):
                    parts = command.split(maxsplit=2)
                    if len(parts) < 3:
                        print("Usage: /rename <nodeId> <name>")
                        continue
                    try:
                        node_id = int(parts[1])
                    except ValueError:
                        print(f"Invalid node id: {parts[1]}")
                        continue
                    sent_name = send_rename(ser, node_id, parts[2])
                    print(f"Sent rename: node {node_id} -> {sent_name}")
                    continue

                send_line(ser, line)
        except KeyboardInterrupt:
            pass
        finally:
            stop_event.set()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
