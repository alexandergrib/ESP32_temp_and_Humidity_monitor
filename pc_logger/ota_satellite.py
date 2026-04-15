"""Upload satellite firmware through the controller over USB serial.

Usage:
    python ota_satellite.py --port COM6 --node-id 1 --firmware .pio/build/satellite_upload/firmware.bin
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import zlib
from pathlib import Path

try:
    import serial  # type: ignore
except ImportError:
    print("Missing dependency: pyserial\nInstall with: pip install pyserial", file=sys.stderr)
    raise


CHUNK_BYTES = 180
BEGIN_PHASE = 1
CHUNK_PHASE = 2
END_PHASE = 3


def send_command(ser: serial.Serial, command: str) -> None:
    ser.write((command + "\n").encode("ascii"))
    ser.flush()


def read_event(ser: serial.Serial, timeout: float) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = ser.readline().decode("utf-8", errors="replace").strip()
        if not raw:
            continue
        print(raw)
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end < start:
            continue
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            continue
    return None


def wait_for_ota_ack(ser: serial.Serial, phase: int, timeout: float) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = max(0.1, deadline - time.time())
        event = read_event(ser, remaining)
        if event is None:
            continue
        if event.get("event") == "ota_ack" and event.get("phase") == phase:
            return event
        if event.get("event") in {"ota_error", "ota_aborted"}:
            raise RuntimeError(json.dumps(event))
    raise TimeoutError(f"Timed out waiting for OTA ack phase {phase}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True, help="Controller serial port, e.g. COM6")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--node-id", type=int, required=True)
    parser.add_argument("--firmware", required=True, help="Path to satellite firmware .bin")
    args = parser.parse_args()

    firmware_path = Path(args.firmware)
    if not firmware_path.is_file():
        print(f"Firmware not found: {firmware_path}", file=sys.stderr)
        return 1

    payload = firmware_path.read_bytes()
    total_size = len(payload)
    crc32 = zlib.crc32(payload) & 0xFFFFFFFF

    print(f"Uploading {firmware_path} to node {args.node_id}")
    print(f"Size: {total_size} bytes  CRC32: {crc32:08x}")

    with serial.Serial(args.port, args.baud, timeout=0.5) as ser:
        ser.dtr = False
        ser.rts = False
        time.sleep(0.2)
        ser.reset_input_buffer()
        send_command(ser, f"SETINT {args.node_id} 30000")
        read_event(ser, 2.0)

        send_command(ser, f"OTA BEGIN {args.node_id} {total_size} {crc32:08x}")
        begin_ack = wait_for_ota_ack(ser, BEGIN_PHASE, 8.0)
        if begin_ack.get("status") not in {"ok", "busy"}:
            raise RuntimeError(json.dumps(begin_ack))

        offset = int(begin_ack.get("bytes_received", 0))
        while offset < total_size:
            chunk = payload[offset: offset + CHUNK_BYTES]
            send_command(ser, f"OTA CHUNK {offset} {chunk.hex()}")
            ack = wait_for_ota_ack(ser, CHUNK_PHASE, 8.0)
            status = ack.get("status")
            ack_offset = int(ack.get("bytes_received", offset))

            if status == "ok":
                offset = ack_offset
                print(f"Progress: {offset}/{total_size}")
                continue

            if status == "offset_mismatch":
                offset = ack_offset
                print(f"Resyncing offset to {offset}")
                continue

            raise RuntimeError(json.dumps(ack))

        send_command(ser, "OTA END")
        end_ack = wait_for_ota_ack(ser, END_PHASE, 12.0)
        if end_ack.get("status") != "ok":
            raise RuntimeError(json.dumps(end_ack))

    print("OTA upload complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
