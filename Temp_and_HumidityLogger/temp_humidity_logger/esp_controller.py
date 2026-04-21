"""ESP controller and satellite presence handling."""

import math
import time
from datetime import datetime
from tkinter import messagebox

from .esp_events import parse_esp_event_json


class EspControllerMixin:
    MIN_SLEEP_INTERVAL_MS = 30000

    def sleep_enable_interval_ok(self):
        try:
            return int(self.current_interval_ms()) >= int(self.MIN_SLEEP_INTERVAL_MS)
        except Exception:
            return False

    def warn_sleep_interval_too_short(self, parent=None):
        messagebox.showwarning(
            "Sleep Control",
            "Sleep mode requires a report interval of at least 30s. "
            "Increase the interval before enabling sleep mode.",
            parent=parent,
        )

    def default_esp_report_interval_ms(self):
        try:
            return max(250, int(self.current_interval_ms()))
        except Exception:
            return 1000


    def esp_presence_timeout_seconds(self, report_interval_ms):
        try:
            interval_ms = max(250, int(report_interval_ms))
        except Exception:
            interval_ms = self.default_esp_report_interval_ms()
        capture_window_ms = min(
            interval_ms,
            max(1, min((interval_ms * 20) // 100, 1000))
        )
        expected_reading_gap_ms = interval_ms + capture_window_ms
        slack_ms = max(capture_window_ms, interval_ms // 2)
        return ((expected_reading_gap_ms * 3) + slack_ms) / 1000.0


    def esp_expected_reading_gap_seconds(self, report_interval_ms):
        try:
            interval_ms = max(250, int(report_interval_ms))
        except Exception:
            interval_ms = self.default_esp_report_interval_ms()
        capture_window_ms = min(
            interval_ms,
            max(1, min((interval_ms * 20) // 100, 1000))
        )
        return (interval_ms + capture_window_ms) / 1000.0


    def apply_esp_interval_change_grace(self, state, old_interval_ms, new_interval_ms):
        try:
            old_interval_ms = max(250, int(old_interval_ms))
            new_interval_ms = max(250, int(new_interval_ms))
        except Exception:
            return
        if old_interval_ms <= new_interval_ms or not state.get("online"):
            return
        now_monotonic = time.monotonic()
        last_seen = float(state.get("last_seen_monotonic") or 0.0)
        old_gap_s = self.esp_expected_reading_gap_seconds(old_interval_ms)
        new_timeout_s = self.esp_presence_timeout_seconds(new_interval_ms)
        if last_seen > 0.0:
            grace_until = last_seen + old_gap_s + new_timeout_s
        else:
            grace_until = now_monotonic + old_gap_s + new_timeout_s
        if grace_until <= now_monotonic:
            grace_until = now_monotonic + new_timeout_s
        state["presence_grace_until_monotonic"] = max(
            float(state.get("presence_grace_until_monotonic") or 0.0),
            grace_until
        )


    def ensure_esp_node_state(self, node_id):
        state = self.esp_node_state.setdefault(node_id, {})
        defaults = {
            "slot_idx": None,
            "name": "satellite",
            "online": False,
            "last_seen_monotonic": 0.0,
            "last_seen_dt": None,
            "report_interval_ms": self.default_esp_report_interval_ms(),
            "signal_pct": None,
            "rssi_dbm": None,
            "next_report_delay_ms": 0,
            "schedule_seen_monotonic": 0.0,
            "has_announced_online": False,
            "presence_grace_until_monotonic": 0.0,
        }
        for key, value in defaults.items():
            state.setdefault(key, value)
        return state


    def update_esp_node_schedule_from_event(self, state, event, now_monotonic=None):
        if now_monotonic is None:
            now_monotonic = time.monotonic()
        try:
            if event.get("report_interval_ms") is not None:
                state["report_interval_ms"] = max(250, int(event.get("report_interval_ms")))
        except Exception:
            pass
        try:
            if event.get("next_report_delay_ms") is not None:
                state["next_report_delay_ms"] = max(0, int(event.get("next_report_delay_ms")))
                state["schedule_seen_monotonic"] = now_monotonic
        except Exception:
            pass


    def update_esp_node_presence(self, node_id, online, dt=None):
        state = self.ensure_esp_node_state(node_id)
        prev_online = bool(state.get("online"))
        if online:
            state["online"] = True
            state["last_seen_dt"] = dt if dt is not None else datetime.now()
            state["presence_grace_until_monotonic"] = 0.0
            slot_idx = state.get("slot_idx")
            if slot_idx is not None and 0 <= int(slot_idx) < self.CHANNEL_COUNT:
                self.update_channel_tree_row(int(slot_idx), signal_display=self.current_signals[int(slot_idx)])
                self.refresh_legend()
            if prev_online:
                return
            if state.get("has_announced_online"):
                name = self.satellite_display_name(node_id)
                self.add_auto_marker(
                    "Satellite {0} ({1}) back online".format(node_id, name),
                    dt=state["last_seen_dt"]
                )
            state["has_announced_online"] = True
            return

        if not prev_online:
            return
        state["online"] = False
        slot_idx = state.get("slot_idx")
        if slot_idx is not None and 0 <= int(slot_idx) < self.CHANNEL_COUNT:
            self.current_signals[int(slot_idx)] = "offline"
            self.update_channel_tree_row(int(slot_idx), signal_display="offline")
            self.refresh_legend()
        name = self.satellite_display_name(node_id)
        self.add_auto_marker(
            "Satellite {0} ({1}) lost connection".format(node_id, name),
            dt=dt if dt is not None else datetime.now()
        )


    def schedule_esp_presence_check(self):
        if self.esp_presence_job is not None:
            try:
                self.root.after_cancel(self.esp_presence_job)
            except Exception:
                pass
        self.esp_presence_job = self.root.after(1000, self.check_esp_presence)


    def check_esp_presence(self):
        self.esp_presence_job = None
        now_monotonic = time.monotonic()
        now_dt = datetime.now()
        if self.source_connected["esp"]:
            for node_id, state in list(self.esp_node_state.items()):
                if not state.get("online"):
                    continue
                report_interval_ms = state.get("report_interval_ms") or self.default_esp_report_interval_ms()
                try:
                    report_interval_ms = max(250, int(report_interval_ms))
                except Exception:
                    report_interval_ms = self.default_esp_report_interval_ms()
                stale_after_s = self.esp_presence_timeout_seconds(report_interval_ms)
                last_seen = float(state.get("last_seen_monotonic") or 0.0)
                next_report_delay_ms = 0
                try:
                    next_report_delay_ms = max(0, int(state.get("next_report_delay_ms") or 0))
                except Exception:
                    next_report_delay_ms = 0
                schedule_seen = float(state.get("schedule_seen_monotonic") or 0.0)
                if next_report_delay_ms > 0 and schedule_seen > 0.0:
                    scheduled_timeout_s = (
                        (schedule_seen - last_seen)
                        + (next_report_delay_ms / 1000.0)
                        + self.esp_presence_timeout_seconds(max(report_interval_ms, next_report_delay_ms))
                    )
                    stale_after_s = max(stale_after_s, scheduled_timeout_s)
                grace_until = float(state.get("presence_grace_until_monotonic") or 0.0)
                if grace_until > now_monotonic:
                    continue
                if last_seen > 0.0 and (now_monotonic - last_seen) > stale_after_s:
                    self.update_esp_node_presence(node_id, False, dt=now_dt)
        self.schedule_esp_presence_check()


    def apply_esp_interval(self, interval_ms, log_to_console=True):
        if not self.source_connected["esp"]:
            return False
        if self.send_esp_command("SETINT ALL {0}".format(interval_ms)):
            normalized_interval_ms = max(250, int(interval_ms))
            sleep_disabled = normalized_interval_ms < self.MIN_SLEEP_INTERVAL_MS
            for state in self.esp_node_state.values():
                old_interval_ms = state.get("report_interval_ms") or self.default_esp_report_interval_ms()
                self.apply_esp_interval_change_grace(state, old_interval_ms, normalized_interval_ms)
                state["report_interval_ms"] = normalized_interval_ms
                if sleep_disabled:
                    state["sleep_enabled"] = False
                    slot_idx = state.get("slot_idx")
                    if slot_idx is not None:
                        slot_idx = int(slot_idx)
                        self.update_channel_tree_row(slot_idx, signal_display=self.current_signals[slot_idx])
            if log_to_console:
                self.append_console(">>> [ESP] SETINT ALL {0}".format(interval_ms))
                if sleep_disabled:
                    self.append_console("ESP sleep mode disabled because interval is below 30s")
            if sleep_disabled:
                self.refresh_sleep_all_menu_state()
            return True
        return False


    def sync_esp_time(self, log_to_console=True):
        if not self.source_connected["esp"]:
            return False
        unix_time = int(time.time())
        if self.send_esp_command("TIME SET {0}".format(unix_time)):
            self.esp_time_synced = True
            if log_to_console:
                self.append_console(">>> [ESP] TIME SET {0}".format(unix_time))
            return True
        return False


    def set_all_satellite_sleep(self, enabled, parent=None):
        if not self.source_connected["esp"]:
            messagebox.showwarning(
                "Sleep Control",
                "ESP controller is not connected.",
                parent=parent,
            )
            return False
        if enabled and not self.sleep_enable_interval_ok():
            self.warn_sleep_interval_too_short(parent=parent)
            return False
        command_value = "ON" if enabled else "OFF"
        command = "SLEEP ALL {0}".format(command_value)
        if self.send_esp_command(command):
            self.append_console(">>> [ESP] {0}".format(command))
            for state in self.esp_node_state.values():
                state["sleep_enabled"] = bool(enabled)
                slot_idx = state.get("slot_idx")
                if slot_idx is not None:
                    slot_idx = int(slot_idx)
                    self.update_channel_tree_row(slot_idx, signal_display=self.current_signals[slot_idx])
            self.refresh_sleep_all_menu_state()
            self.refresh_legend()
            self.send_esp_command("NODES")
            return True
        messagebox.showwarning(
            "Sleep Control",
            "Failed to send sleep command to the ESP controller.",
            parent=parent,
        )
        return False


    def on_sleep_all_toggle(self):
        enabled = bool(self.sleep_all_var.get())
        if not self.set_all_satellite_sleep(enabled, parent=self.root):
            self.sleep_all_var.set(not enabled)


    def refresh_sleep_all_menu_state(self):
        if not hasattr(self, "sleep_all_var"):
            return
        sleep_states = [
            bool(state.get("sleep_enabled"))
            for state in self.esp_node_state.values()
            if state.get("slot_idx") is not None
        ]
        self.sleep_all_var.set(bool(sleep_states) and all(sleep_states))


    def parse_esp_timestamp(self, event):
        iso_text = str(event.get("controller_time", "") or "").strip()
        if iso_text:
            try:
                dt = datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    dt = dt.astimezone().replace(tzinfo=None)
                return dt
            except Exception:
                pass
        unix_value = event.get("controller_unix")
        try:
            if unix_value is not None:
                return datetime.fromtimestamp(int(unix_value))
        except Exception:
            pass
        return datetime.now()


    def update_esp_slot_metadata(self, slot_idx, node_id, node_name):
        current_name = self.channel_names[slot_idx]
        if (not current_name) or current_name.startswith("ESP Slot") or current_name.startswith("ESP Node"):
            safe_name = (node_name or "satellite").strip()
            self.channel_names[slot_idx] = "ESP Node {0} - {1}".format(node_id, safe_name)
            for ctx in self.graph_contexts.values():
                ctx["lines"][slot_idx].set_label(self.channel_names[slot_idx])
            self.refresh_legend()
        self.update_channel_tree_row(slot_idx, signal_display=self.current_signals[slot_idx])


    def parse_esp_event_json(self, line):
        return parse_esp_event_json(line)


    def process_esp_packet_line(self, line):
        event = self.parse_esp_event_json(line)
        if event is None:
            return
        event_name = str(event.get("event", "") or "")
        if event_name == "controller_ready":
            if not self.esp_stream_confirmed:
                self.schedule_esp_init(200)
            return
        if event_name == "node_bound":
            try:
                node_id = int(event.get("node_id"))
            except Exception:
                return
            slot_idx = self.allocate_esp_slot(node_id)
            if slot_idx is None:
                return
            node_name = str(event.get("name", "") or "satellite")
            self.update_esp_slot_metadata(slot_idx, node_id, node_name)
            state = self.ensure_esp_node_state(node_id)
            state["slot_idx"] = slot_idx
            state["name"] = node_name
            now = self.parse_esp_timestamp(event)
            state["last_seen_monotonic"] = time.monotonic()
            state["last_seen_dt"] = now
            self.update_esp_node_presence(node_id, True, dt=now)
            self.update_channel_tree_row(slot_idx, signal_display=self.current_signals[slot_idx])
        elif event_name == "reading":
            self.esp_stream_confirmed = True
            if self.esp_init_job is not None:
                try:
                    self.root.after_cancel(self.esp_init_job)
                except Exception:
                    pass
                self.esp_init_job = None
            if not self.esp_time_synced:
                self.sync_esp_time(log_to_console=True)
            try:
                node_id = int(event.get("node_id"))
            except Exception:
                return
            slot_idx = self.allocate_esp_slot(node_id)
            if slot_idx is None:
                return
            self.update_esp_slot_metadata(slot_idx, node_id, str(event.get("name", "") or "satellite"))
            signal_pct = event.get("signal_pct")
            rssi_dbm = event.get("rssi_dbm")
            self.current_signals[slot_idx] = self.format_signal_display(signal_pct, rssi_dbm)
            state = self.ensure_esp_node_state(node_id)
            state["slot_idx"] = slot_idx
            state["name"] = str(event.get("name", "") or "satellite")
            state["signal_pct"] = signal_pct
            state["rssi_dbm"] = rssi_dbm
            now = self.parse_esp_timestamp(event)
            now_monotonic = time.monotonic()
            self.update_esp_node_schedule_from_event(state, event, now_monotonic=now_monotonic)
            state["last_seen_monotonic"] = now_monotonic
            state["last_seen_dt"] = now
            self.update_esp_node_presence(node_id, True, dt=now)
            sensor_ok = bool(event.get("sensor_ok", True))
            try:
                temp_raw = float(event.get("temperature_c"))
                hum_raw = float(event.get("humidity_pct"))
            except Exception:
                sensor_ok = False
                temp_raw = math.nan
                hum_raw = math.nan
            if not sensor_ok or not math.isfinite(temp_raw) or not math.isfinite(hum_raw):
                self.current_temps[slot_idx] = "NaN"
                self.current_hums[slot_idx] = "NaN"
                self.update_channel_tree_row(slot_idx, "-", "-", self.current_signals[slot_idx])
                self.refresh_legend()
                return
            temp_cal = self.apply_calibration("temp", slot_idx, temp_raw)
            hum_cal = self.apply_calibration("hum", slot_idx, hum_raw)
            temp_text = self._format_number(temp_cal)
            hum_text = self._format_number(hum_cal)
            self.current_temps[slot_idx] = temp_text
            self.current_hums[slot_idx] = hum_text
            self.update_channel_tree_row(
                slot_idx,
                "{0} \u00b0C".format(temp_text),
                "{0} %".format(hum_text),
                self.current_signals[slot_idx]
            )
            self.add_smoothed_point(slot_idx, now, temp_cal, hum_cal)
            self.save_to_db(now)
            self._schedule_redraw()
        elif event_name == "config_ack":
            try:
                node_id = int(event.get("node_id"))
            except Exception:
                return
            state = self.ensure_esp_node_state(node_id)
            now_monotonic = time.monotonic()
            self.update_esp_node_schedule_from_event(state, event, now_monotonic=now_monotonic)
            try:
                state["sleep_enabled"] = bool(event.get("sleep_enabled"))
            except Exception:
                pass
            self.refresh_sleep_all_menu_state()
            now = self.parse_esp_timestamp(event)
            state["last_seen_monotonic"] = now_monotonic
            state["last_seen_dt"] = now
            self.update_esp_node_presence(node_id, True, dt=now)
        elif event_name == "rename_ack":
            try:
                node_id = int(event.get("node_id"))
            except Exception:
                return
            node_name = str(event.get("name", "") or "satellite")
            applied = bool(event.get("applied"))
            slot_idx = self.allocate_esp_slot(node_id)
            state = self.ensure_esp_node_state(node_id)
            state["name"] = node_name
            if slot_idx is not None:
                state["slot_idx"] = slot_idx
                if applied:
                    self.channel_names[slot_idx] = node_name
                    self.update_channel_tree_row(slot_idx, signal_display=self.current_signals[slot_idx])
                    for ctx in self.graph_contexts.values():
                        ctx["lines"][slot_idx].set_label(node_name)
                        ctx["canvas"].draw_idle()
                    self.refresh_legend()
                else:
                    self.update_channel_tree_row(slot_idx, signal_display=self.current_signals[slot_idx])
            if applied and self.source_connected["esp"]:
                self.send_esp_command("NODES")
        elif event_name == "nodes":
            self.esp_stream_confirmed = True
            if self.esp_init_job is not None:
                try:
                    self.root.after_cancel(self.esp_init_job)
                except Exception:
                    pass
                self.esp_init_job = None
            if not self.esp_time_synced:
                self.sync_esp_time(log_to_console=True)
            updated_any = False
            for item in event.get("items", []):
                try:
                    node_id = int(item.get("node_id"))
                except Exception:
                    continue
                slot_idx = self.allocate_esp_slot(node_id)
                if slot_idx is None:
                    continue
                node_name = str(item.get("name", "") or "satellite")
                self.update_esp_slot_metadata(slot_idx, node_id, node_name)
                signal_pct = item.get("signal_pct")
                rssi_dbm = item.get("rssi_dbm")
                self.current_signals[slot_idx] = self.format_signal_display(signal_pct, rssi_dbm)
                state = self.ensure_esp_node_state(node_id)
                state["slot_idx"] = slot_idx
                state["name"] = node_name
                state["signal_pct"] = signal_pct
                state["rssi_dbm"] = rssi_dbm
                if not state.get("online") and not float(state.get("last_seen_monotonic") or 0.0):
                    self.current_signals[slot_idx] = "waiting"
                self.update_esp_node_schedule_from_event(state, item)
                if not state.get("report_interval_ms"):
                    state["report_interval_ms"] = self.default_esp_report_interval_ms()
                try:
                    state["sleep_enabled"] = bool(item.get("sleep_enabled"))
                except Exception:
                    state["sleep_enabled"] = state.get("sleep_enabled", False)
                # NODES is controller metadata and can include satellites that have
                # not talked since before this app session. Start offline monitoring
                # only after a live reading/config ack from the satellite.
                self.update_channel_tree_row(slot_idx, signal_display=self.current_signals[slot_idx])
                updated_any = True
            if updated_any:
                self.refresh_sleep_all_menu_state()
                self.refresh_legend()

