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
BEGIN_TIMEOUT_S = 12.0
CHUNK_TIMEOUT_S = 12.0
END_TIMEOUT_S = 20.0
HOST_RETRIES = 3
INTER_CHUNK_DELAY_S = 0.0


def send_command(ser: serial.Serial, command: str) -> None:
    ser.write((command + "\n").encode("ascii"))
    ser.flush()


_pending_events: list[dict] = []
_pending_text = ""


def extract_json_objects(raw: str) -> list[str]:
    objects: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False

    for idx, ch in enumerate(raw):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                objects.append(raw[start:idx + 1])
                start = -1

    return objects


def read_event(ser: serial.Serial, timeout: float) -> dict | None:
    global _pending_text

    if _pending_events:
        return _pending_events.pop(0)

    deadline = time.time() + timeout
    while time.time() < deadline:
        chunk = ser.read(ser.in_waiting or 1).decode("utf-8", errors="replace")
        if not chunk:
            continue
        _pending_text += chunk
        consumed_upto = -1
        for blob in extract_json_objects(_pending_text):
            try:
                event = json.loads(blob)
                if event.get("event") != "ota_ack":
                    print(json.dumps(event))
                _pending_events.append(event)
                blob_end = _pending_text.find(blob, consumed_upto + 1)
                if blob_end >= 0:
                    consumed_upto = blob_end + len(blob)
            except json.JSONDecodeError:
                continue
        if consumed_upto >= 0:
            _pending_text = _pending_text[consumed_upto:]
        elif len(_pending_text) > 65536:
            _pending_text = _pending_text[-8192:]
        if _pending_events:
            return _pending_events.pop(0)
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
        if event.get("event") == "ota_error" and event.get("reason") == "chunk_rejected":
            return {
                "event": "ota_ack",
                "phase": phase,
                "status": "offset_mismatch",
                "bytes_received": int(event.get("expected_offset", 0)),
                "detail": 0,
            }
        if event.get("event") in {"ota_error", "ota_aborted"}:
            raise RuntimeError(json.dumps(event))
    raise TimeoutError(f"Timed out waiting for OTA ack phase {phase}")


def wait_for_node_ready(ser: serial.Serial, node_id: int, timeout: float) -> dict:
    deadline = time.time() + timeout
    next_probe_at = 0.0
    while time.time() < deadline:
        now = time.time()
        if now >= next_probe_at:
            send_command(ser, "NODES")
            next_probe_at = now + 1.0
        remaining = max(0.1, deadline - time.time())
        event = read_event(ser, remaining)
        if event is None:
            continue
        if event.get("node_id") == node_id and event.get("event") in {"config_ack", "reading"}:
            return event
        if event.get("event") == "nodes":
            for item in event.get("items", []):
                if int(item.get("node_id", 0)) == node_id:
                    return item
    raise TimeoutError(f"Timed out waiting for node {node_id} to become active")


def wait_for_nodes_snapshot(ser: serial.Serial, timeout: float) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = max(0.1, deadline - time.time())
        event = read_event(ser, remaining)
        if event is None:
            continue
        if event.get("event") == "nodes":
            return list(event.get("items", []))
    raise TimeoutError("Timed out waiting for node list")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True, help="Controller serial port, e.g. COM6")
    parser.add_argument("--baud", type=int, default=460800)
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
        send_command(ser, "STREAM ON")
        read_event(ser, 2.0)
        send_command(ser, "NODES")
        snapshot = wait_for_nodes_snapshot(ser, 4.0)
        saved_intervals: dict[int, int] = {}
        for item in snapshot:
            node_id = int(item.get("node_id", 0))
            interval = int(item.get("report_interval_ms", 30000))
            saved_intervals[node_id] = interval
            quiet_ms = 30000
            send_command(ser, f"SETINT {node_id} {quiet_ms}")
            read_event(ser, 2.0)
        send_command(ser, f"SETINT {args.node_id} 30000")
        wait_for_node_ready(ser, args.node_id, 20.0)

        begin_ack = None
        for attempt in range(HOST_RETRIES):
            send_command(ser, f"OTA BEGIN {args.node_id} {total_size} {crc32:08x}")
            try:
                begin_ack = wait_for_ota_ack(ser, BEGIN_PHASE, BEGIN_TIMEOUT_S)
                break
            except TimeoutError:
                if attempt + 1 == HOST_RETRIES:
                    raise
        assert begin_ack is not None
        if begin_ack.get("status") not in {"ok", "busy"}:
            raise RuntimeError(json.dumps(begin_ack))

        if begin_ack.get("status") == "busy":
            offset = int(begin_ack.get("bytes_received", 0))
            print(
                f"Controller will resync an existing OTA session for node {args.node_id} "
                f"from remote offset {offset}"
            )
        else:
            offset = int(begin_ack.get("bytes_received", 0))
        while offset < total_size:
            chunk = payload[offset: offset + CHUNK_BYTES]
            ack = None
            for attempt in range(HOST_RETRIES):
                send_command(ser, f"OTA CHUNK {offset} {chunk.hex()}")
                try:
                    ack = wait_for_ota_ack(ser, CHUNK_PHASE, CHUNK_TIMEOUT_S)
                    break
                except TimeoutError:
                    if attempt + 1 == HOST_RETRIES:
                        raise
            assert ack is not None
            status = ack.get("status")
            ack_offset = int(ack.get("bytes_received", offset))

            if status == "ok":
                offset = ack_offset
                print(f"Progress: {offset}/{total_size}")
                time.sleep(INTER_CHUNK_DELAY_S)
                continue

            if status == "offset_mismatch":
                offset = ack_offset
                print(f"Resyncing offset to {offset}")
                time.sleep(INTER_CHUNK_DELAY_S)
                continue

            raise RuntimeError(json.dumps(ack))

        end_ack = None
        for attempt in range(HOST_RETRIES):
            send_command(ser, "OTA END")
            try:
                end_ack = wait_for_ota_ack(ser, END_PHASE, END_TIMEOUT_S)
                break
            except TimeoutError:
                if attempt + 1 == HOST_RETRIES:
                    raise
        assert end_ack is not None
        if end_ack.get("status") != "ok":
            raise RuntimeError(json.dumps(end_ack))

        for node_id, interval in saved_intervals.items():
            if interval == 30000:
                continue
            send_command(ser, f"SETINT {node_id} {interval}")
            read_event(ser, 2.0)

    print("OTA upload complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
