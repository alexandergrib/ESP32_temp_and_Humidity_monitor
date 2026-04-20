from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field

import serial  # type: ignore


def send_command(ser: serial.Serial, command: str) -> None:
    ser.write((command + "\n").encode("ascii"))
    ser.flush()


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
                objects.append(raw[start : idx + 1])
                start = -1

    return objects


class EventReader:
    def __init__(self, ser: serial.Serial) -> None:
        self.ser = ser
        self.pending_events: list[dict] = []
        self.pending_text = ""

    def read_event(self, timeout: float) -> dict | None:
        if self.pending_events:
            return self.pending_events.pop(0)

        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = self.ser.read(self.ser.in_waiting or 1).decode("utf-8", errors="replace")
            if not chunk:
                continue
            self.pending_text += chunk
            consumed_upto = -1
            for blob in extract_json_objects(self.pending_text):
                try:
                    event = json.loads(blob)
                except json.JSONDecodeError:
                    continue
                self.pending_events.append(event)
                blob_end = self.pending_text.find(blob, consumed_upto + 1)
                if blob_end >= 0:
                    consumed_upto = blob_end + len(blob)
            if consumed_upto >= 0:
                self.pending_text = self.pending_text[consumed_upto:]
            elif len(self.pending_text) > 65536:
                self.pending_text = self.pending_text[-8192:]
            if self.pending_events:
                return self.pending_events.pop(0)
        return None


@dataclass
class NodeStats:
    readings: int = 0
    gaps_s: list[float] = field(default_factory=list)
    last_ts: float | None = None
    config_acks: int = 0
    tx_failures: int = 0
    last_seen_ms: int = 0


def wait_for_nodes_snapshot(reader: EventReader, ser: serial.Serial, timeout_s: float) -> list[dict]:
    deadline = time.time() + timeout_s
    next_probe_at = 0.0
    while time.time() < deadline:
        now = time.time()
        if now >= next_probe_at:
            send_command(ser, "NODES")
            next_probe_at = now + 1.0
        event = reader.read_event(max(0.1, deadline - now))
        if event and event.get("event") == "nodes":
            return list(event.get("items", []))
    return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=460800)
    parser.add_argument("--interval-ms", type=int, required=True)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--label", required=True)
    parser.add_argument("--sleep-on", action="store_true")
    args = parser.parse_args()

    timeout_s = max(args.interval_ms * (args.iterations + 3) / 1000.0, 45.0)
    start_ts = time.time()

    with serial.Serial(args.port, args.baud, timeout=0.5) as ser:
        ser.dtr = False
        ser.rts = False
        time.sleep(0.5)
        ser.reset_input_buffer()
        reader = EventReader(ser)

        for cmd in ("STREAM ON", "BIND"):
            send_command(ser, cmd)
            time.sleep(0.15)
        if args.sleep_on:
            send_command(ser, "SLEEP ALL ON")
            time.sleep(0.25)
        send_command(ser, f"SETINT ALL {args.interval_ms}")

        nodes = wait_for_nodes_snapshot(reader, ser, 10.0)
        target_nodes = sorted(int(item.get("node_id", 0)) for item in nodes if int(item.get("node_id", 0)) > 0)
        if not target_nodes:
            target_nodes = [1, 2]

        stats: dict[int, NodeStats] = defaultdict(NodeStats)
        tx_failures = 0
        deadline = time.time() + timeout_s

        while time.time() < deadline:
            event = reader.read_event(1.0)
            if event is None:
                continue

            kind = event.get("event")
            if kind == "tx_status" and event.get("ok") is False:
                tx_failures += 1
                continue

            if kind == "nodes":
                for item in event.get("items", []):
                    node_id = int(item.get("node_id", 0))
                    if node_id > 0:
                        stats[node_id].last_seen_ms = int(item.get("last_seen_ms", 0) or 0)
                continue

            if kind == "config_ack":
                node_id = int(event.get("node_id", 0))
                if node_id > 0:
                    stats[node_id].config_acks += 1
                continue

            if kind != "reading":
                continue

            node_id = int(event.get("node_id", 0))
            if node_id <= 0:
                continue

            now_ts = time.time()
            node = stats[node_id]
            if node.last_ts is not None:
                node.gaps_s.append(now_ts - node.last_ts)
            node.last_ts = now_ts
            node.readings += 1

            if all(stats[node_id].readings >= args.iterations for node_id in target_nodes):
                break

        summary = {
            "label": args.label,
            "interval_ms": args.interval_ms,
            "iterations_target": args.iterations,
            "elapsed_s": round(time.time() - start_ts, 2),
            "timeout_s": round(timeout_s, 2),
            "tx_failures": tx_failures,
            "nodes": {},
        }
        for node_id in sorted(target_nodes):
            node = stats[node_id]
            summary["nodes"][str(node_id)] = {
                "readings": node.readings,
                "config_acks": node.config_acks,
                "last_seen_ms": node.last_seen_ms,
                "avg_gap_s": round(sum(node.gaps_s) / len(node.gaps_s), 3) if node.gaps_s else None,
                "max_gap_s": round(max(node.gaps_s), 3) if node.gaps_s else None,
                "min_gap_s": round(min(node.gaps_s), 3) if node.gaps_s else None,
                "passed": node.readings >= args.iterations,
            }

        print(json.dumps(summary))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
