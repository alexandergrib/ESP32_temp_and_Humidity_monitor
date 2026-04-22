import json
import math
import os
import queue
import re
import threading
import time
from datetime import datetime

import serial
import serial.tools.list_ports
from tkinter import messagebox

from .smoothing import append_and_average


class SerialIoMixin:
    def refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.cmb_arduino_port["values"] = ports
        self.cmb_esp_port["values"] = ports
        if not ports:
            return
        if self.saved_arduino_port and self.saved_arduino_port in ports and not self.cmb_arduino_port.get():
            self.cmb_arduino_port.set(self.saved_arduino_port)
        if self.saved_esp_port and self.saved_esp_port in ports and not self.cmb_esp_port.get():
            self.cmb_esp_port.set(self.saved_esp_port)
        if not self.cmb_arduino_port.get():
            self.cmb_arduino_port.current(0)
        if not self.cmb_esp_port.get():
            self.cmb_esp_port.current(0)

    def on_connect_arduino_click(self):
        if self.source_connected["arduino"]:
            self.disconnect_source("arduino")
            return
        port_name = self.cmb_arduino_port.get().strip()
        if not port_name:
            messagebox.showwarning("Port", "Select Arduino COM port.")
            return
        self.connect_source("arduino", port_name)

    def on_connect_esp_click(self):
        if self.source_connected["esp"]:
            self.disconnect_source("esp")
            return
        port_name = self.cmb_esp_port.get().strip()
        if not port_name:
            messagebox.showwarning("Port", "Select ESP COM port.")
            return
        self.connect_source("esp", port_name)

    def get_source_port_name(self, source_kind):
        ser = self.serial_ports.get(source_kind)
        if ser is None:
            return ""
        return getattr(ser, "port", "") or ""

    def connect_source(self, source_kind, port_name):
        other_kind = "esp" if source_kind == "arduino" else "arduino"
        if self.source_connected[other_kind] and port_name == self.get_source_port_name(other_kind):
            messagebox.showwarning("Port", "That COM port is already used by the other source.")
            return
        session_already_active = self.any_source_connected() and self.db_session_id is not None

        baud_rate = self.ARDUINO_BAUD_RATE if source_kind == "arduino" else self.ESP_BAUD_RATE
        try:
            ser = serial.Serial(port=port_name, baudrate=baud_rate, timeout=0.2, write_timeout=1)
            if source_kind == "arduino":
                ser.dtr = True
                ser.rts = True
            else:
                ser.dtr = False
                ser.rts = False
        except Exception as ex:
            messagebox.showerror("Serial error", str(ex))
            self.disconnect_source(source_kind)
            return

        if not self.any_source_connected():
            self.start_db_session()
            self.loaded_session_id = None
            self.refresh_graph_titles()
            self.refresh_sessions_list()

        self.serial_ports[source_kind] = ser
        self.source_connected[source_kind] = True
        self.stop_events[source_kind].clear()
        self.receive_buffers[source_kind] = ""

        if source_kind == "arduino":
            self.arduino_polling_started = False
            self.btn_connect_arduino.config(text="Stop ARD")
            self.append_console(">>> Arduino connected on {0} (session #{1})".format(port_name, self.db_session_id))
        else:
            self.esp_init_attempts_remaining = 6
            self.esp_stream_confirmed = False
            self.esp_time_synced = False
            self.last_esp_event_monotonic = time.monotonic()
            self.last_esp_stream_recover_at = 0.0
            self.btn_connect_esp.config(text="Stop ESP")
            self.append_console(">>> ESP controller connected on {0} (session #{1})".format(port_name, self.db_session_id))

        if session_already_active:
            if source_kind == "arduino":
                self.add_auto_marker("Arduino connected on {0}".format(port_name))
            else:
                self.add_auto_marker("ESP connected on {0}".format(port_name))

        self.update_status_label()
        self.update_sessions_controls()
        self.rebuild_channel_tree()
        self._schedule_redraw()
        self.read_threads[source_kind] = threading.Thread(
            target=self.serial_read_loop, args=(source_kind,), daemon=True
        )
        self.read_threads[source_kind].start()

        if source_kind == "arduino":
            self.root.after(2000, self.send_arduino_handshake)
        else:
            self.schedule_esp_init(1500)

    def send_arduino_handshake(self):
        try:
            ser = self.serial_ports.get("arduino")
            if ser and ser.is_open:
                ser.write(b"HANDSHAKE?\n")
                self.append_console(">>> [ARD] HANDSHAKE?")
        except Exception as ex:
            self.append_console("Arduino handshake error: {0}".format(ex))

    def current_interval_ms(self):
        interval_text = self.txt_interval.get().strip() if hasattr(self, "txt_interval") else self.saved_interval_text
        interval_ms = self.parse_interval_ms(interval_text)
        if interval_ms is None:
            return 1000
        return interval_ms

    def send_esp_command(self, command):
        ser = self.serial_ports.get("esp")
        if not ser or not ser.is_open:
            return False
        try:
            ser.write((command + "\n").encode("utf-8"))
            return True
        except Exception as ex:
            self.append_console("ESP command error: {0}".format(ex))
            return False

    def send_terminal_command(self, event=None):
        raw_command = self.terminal_command_var.get().strip()
        if not raw_command:
            return "break"

        command = " ".join(part for part in raw_command.splitlines() if part.strip()).strip()
        target = str(self.terminal_command_target_var.get() or "ESP").strip().upper()
        source_kind = "esp" if target == "ESP" else "arduino"
        ser = self.serial_ports.get(source_kind)
        if not ser or not ser.is_open:
            self.append_console("{0} terminal is not connected".format(target))
            return "break"

        try:
            ser.write((command + "\n").encode("utf-8"))
            self.append_console(">>> [{0}] {1}".format(target, command))
            self.terminal_command_var.set("")
            active_entry = self.floating_terminal_command_entry if self.terminal_mode == "floating" else self.terminal_command_entry
            if active_entry is not None:
                try:
                    active_entry.focus_set()
                except Exception:
                    pass
        except Exception as ex:
            self.append_console("{0} command error: {1}".format(target, ex))
        return "break"

    def on_interval_changed(self, event=None):
        interval_text = self.txt_interval.get().strip()
        interval_ms = self.parse_interval_ms(interval_text)
        if interval_ms is None:
            self.append_console("Interval ignored: use formats like 500ms, 1s, 2min, 1h")
            return
        previous_interval_ms = self.parse_interval_ms(self.saved_interval_text)
        if self.source_connected["arduino"] and self.arduino_polling_started:
            self.schedule_arduino_poll(interval_ms)
            self.append_console(">>> [ARD] interval set to {0} ms".format(interval_ms))
        if self.source_connected["esp"]:
            self.apply_esp_interval(interval_ms, log_to_console=True)
        if previous_interval_ms != interval_ms:
            self.add_auto_marker("Interval set to {0} ms".format(interval_ms))
        self.saved_interval_text = interval_text or self.DEFAULT_INTERVAL_TEXT
        self.save_config()

    def schedule_esp_init(self, delay_ms):
        if self.esp_init_job is not None:
            try:
                self.root.after_cancel(self.esp_init_job)
            except Exception:
                pass
        self.esp_init_job = self.root.after(delay_ms, self.initialize_esp_stream)

    def initialize_esp_stream(self):
        self.esp_init_job = None
        ser = self.serial_ports.get("esp")
        if not ser or not ser.is_open or self.esp_stream_confirmed:
            return
        attempt_no = 7 - max(self.esp_init_attempts_remaining, 0)
        interval_ms = self.current_interval_ms()
        for cmd in (
            "STREAM ON",
            "SETINT ALL {0}".format(interval_ms),
            "NODES",
        ):
            try:
                ser.write((cmd + "\n").encode("utf-8"))
            except Exception as ex:
                self.append_console("ESP init error: {0}".format(ex))
                break
        self.append_console(
            ">>> [ESP] init attempt {0}: STREAM ON / SETINT ALL {1} / NODES".format(
                attempt_no, interval_ms
            )
        )
        if self.esp_init_attempts_remaining > 0:
            self.esp_init_attempts_remaining -= 1
        if not self.esp_stream_confirmed and self.esp_init_attempts_remaining > 0:
            self.schedule_esp_init(1200)

    def serial_read_loop(self, source_kind):
        stop_event = self.stop_events[source_kind]
        while not stop_event.is_set():
            try:
                ser = self.serial_ports.get(source_kind)
                if not ser or not ser.is_open:
                    break
                waiting = ser.in_waiting
                if waiting > 0:
                    chunk = ser.read(waiting).decode("utf-8", errors="ignore")
                    if chunk:
                        self.ui_queue.put(("chunk", (source_kind, chunk)))
                else:
                    time.sleep(0.01)
            except Exception as ex:
                self.ui_queue.put(("console", "[{0}] Serial read error: {1}".format(source_kind.upper(), ex)))
                break

    def process_ui_queue(self):
        try:
            while True:
                msg_type, payload = self.ui_queue.get_nowait()
                if msg_type == "chunk":
                    source_kind, chunk = payload
                    self.handle_incoming_chunk(str(source_kind), str(chunk))
                elif msg_type == "console":
                    self.append_console(str(payload))
        except queue.Empty:
            pass
        self.root.after(20, self.process_ui_queue)

    def handle_incoming_chunk(self, source_kind, chunk):
        self.receive_buffers[source_kind] += chunk
        self.process_receive_buffer(source_kind)

    def process_receive_buffer(self, source_kind):
        if source_kind == "esp":
            self.process_esp_receive_buffer()
            return
        normalized = self.receive_buffers[source_kind].replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized.split("\n")
        if not lines:
            return
        complete_lines = lines[:-1]
        self.receive_buffers[source_kind] = lines[-1]
        for line in complete_lines:
            line = line.strip()
            if not line:
                continue
            self.process_packet_line(source_kind, line)
        if len(self.receive_buffers[source_kind]) > 10000:
            self.receive_buffers[source_kind] = self.receive_buffers[source_kind][-3000:]

    def extract_json_objects(self, raw_text):
        objects = []
        depth = 0
        start = -1
        in_string = False
        escaped = False

        for idx, ch in enumerate(raw_text):
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == "\"":
                    in_string = False
                continue

            if ch == "\"":
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
                    objects.append((start, idx + 1, raw_text[start:idx + 1]))
                    start = -1

        return objects

    def process_esp_receive_buffer(self):
        raw_text = self.receive_buffers["esp"]
        objects = self.extract_json_objects(raw_text)
        if not objects:
            if len(raw_text) > 10000:
                self.receive_buffers["esp"] = raw_text[-3000:]
            return

        consumed_upto = 0
        for start, end, obj_text in objects:
            consumed_upto = max(consumed_upto, end)
            self.process_packet_line("esp", obj_text.strip())

        self.receive_buffers["esp"] = raw_text[consumed_upto:]

    def process_packet_line(self, source_kind, line):
        prefix = "ARD" if source_kind == "arduino" else "ESP"
        self.append_console("[{0}] {1}".format(prefix, line))
        if source_kind == "arduino":
            self.process_arduino_packet_line(line)
        else:
            self.process_esp_packet_line(line)

    def process_arduino_packet_line(self, line):
        json_start = line.find("{")
        if json_start < 0:
            return
        try:
            event = json.loads(line[json_start:])
        except Exception:
            return
        event_name = str(event.get("event", "") or "")
        if event_name == "arduino_ready":
            board = str(event.get("board", "") or "").strip()
            fw_version = str(event.get("fw_version", "") or "").strip()
            protocol = str(event.get("protocol", "") or "").strip()
            channel_count = event.get("channel_count")
            try:
                channel_count = int(channel_count) if channel_count is not None else None
            except Exception:
                channel_count = None
            self.arduino_info = {
                "board": board,
                "fw_version": fw_version,
                "protocol": protocol,
                "channel_count": channel_count,
            }
            board_label = self.format_board_name(board)
            details = [board_label]
            if fw_version:
                details.append("fw {0}".format(fw_version))
            if protocol:
                details.append("protocol {0}".format(protocol))
            if channel_count is not None:
                details.append("{0} channels".format(channel_count))
            self.append_console(">>> [ARD] Ready: {0}".format(", ".join(details)))
            if channel_count is not None and channel_count != self.ARDUINO_CHANNEL_COUNT:
                self.append_console(
                    "[ARD] Channel count mismatch: app expects {0}, device reports {1}".format(
                        self.ARDUINO_CHANNEL_COUNT, channel_count
                    )
                )
            if not self.arduino_polling_started:
                interval_ms = self.current_interval_ms()
                self.arduino_polling_started = True
                self.append_console(">>> [ARD] Polling started")
                self.update_status_label()
                self.schedule_arduino_poll(interval_ms)
            else:
                self.update_status_label()
            return
        if event_name != "arduino_batch":
            return

        now = datetime.now()
        status = str(event.get("status", "") or "").strip().lower()
        message = str(event.get("message", "") or "").strip()
        items = event.get("items", [])
        if not isinstance(items, list):
            return

        got_any = False
        batch_errors = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                channel_idx = int(item.get("channel"))
            except Exception:
                continue
            if not (0 <= channel_idx < self.ARDUINO_CHANNEL_COUNT):
                continue

            if not bool(item.get("sensor_ok")):
                self.current_temps[channel_idx] = "NaN"
                self.current_hums[channel_idx] = "NaN"
                self.update_channel_tree_row(channel_idx, "-", "-", self.current_signals[channel_idx])
                error_text = str(item.get("error", "") or "").strip()
                if error_text:
                    batch_errors.append("CH{0}: {1}".format(channel_idx, error_text))
                continue

            try:
                temp_raw = float(item.get("temperature_c"))
                hum_raw = float(item.get("humidity_pct"))
            except Exception:
                continue

            temp_cal = self.apply_calibration("temp", channel_idx, temp_raw)
            hum_cal = self.apply_calibration("hum", channel_idx, hum_raw)
            temp_text = self._format_number(temp_cal)
            hum_text = self._format_number(hum_cal)
            self.current_temps[channel_idx] = temp_text
            self.current_hums[channel_idx] = hum_text
            self.update_channel_tree_row(
                channel_idx,
                "{0} \u00b0C".format(temp_text),
                "{0} %".format(hum_text),
                self.current_signals[channel_idx]
            )
            self.add_smoothed_point(channel_idx, now, temp_cal, hum_cal)
            got_any = True

        if status == "no_sensors":
            for channel_idx in range(self.ARDUINO_CHANNEL_COUNT):
                self.current_temps[channel_idx] = "NaN"
                self.current_hums[channel_idx] = "NaN"
                self.update_channel_tree_row(channel_idx, "-", "-", self.current_signals[channel_idx])
        elif status and status != "ok":
            status_text = "[ARD] Batch status: {0}".format(status)
            if message:
                status_text = "{0} ({1})".format(status_text, message)
            self.append_console(status_text)

        if batch_errors:
            self.append_console("[ARD] Read errors: {0}".format("; ".join(batch_errors)))

        if got_any:
            self.save_to_db(now)
            self._schedule_redraw()
        else:
            self.refresh_legend()

    def schedule_arduino_poll(self, interval_ms):
        if self.arduino_poll_job is not None:
            self.root.after_cancel(self.arduino_poll_job)
            self.arduino_poll_job = None
        self.arduino_poll_job = self.root.after(interval_ms, self.poll_arduino_once)

    def poll_arduino_once(self):
        self.arduino_poll_job = None
        ser = self.serial_ports.get("arduino")
        if ser and ser.is_open and self.source_connected["arduino"]:
            try:
                ser.write(b"READ\n")
            except Exception:
                pass
            interval_ms = self.current_interval_ms()
            self.schedule_arduino_poll(interval_ms)

    def add_smoothed_point(self, channel_index, timestamp, temp_raw=None, hum_raw=None):
        temp_smoothed = None
        hum_smoothed = None
        if temp_raw is not None:
            temp_history = self.temp_history[channel_index]
            temp_smoothed = append_and_average(
                temp_history,
                timestamp,
                temp_raw,
                is_satellite=channel_index >= self.ARDUINO_CHANNEL_COUNT,
                sample_window=self.SMOOTHING_WINDOW,
                time_window_s=self.SATELLITE_SMOOTHING_SECONDS,
            )
        if hum_raw is not None:
            hum_history = self.hum_history[channel_index]
            hum_smoothed = append_and_average(
                hum_history,
                timestamp,
                hum_raw,
                is_satellite=channel_index >= self.ARDUINO_CHANNEL_COUNT,
                sample_window=self.SMOOTHING_WINDOW,
                time_window_s=self.SATELLITE_SMOOTHING_SECONDS,
            )

        # Keep at most one plotted point per second per channel so 1 week is bounded.
        ts_sec = int(timestamp.timestamp())
        ts_plot = timestamp.replace(microsecond=0)

        if self.last_plot_second[channel_index] == ts_sec and self.series_times[channel_index]:
            self.series_times[channel_index][-1] = ts_plot
            if temp_smoothed is not None:
                self.temp_series_values[channel_index][-1] = temp_smoothed
            if hum_smoothed is not None:
                self.hum_series_values[channel_index][-1] = hum_smoothed
        else:
            self.series_times[channel_index].append(ts_plot)
            self.temp_series_values[channel_index].append(
                temp_smoothed if temp_smoothed is not None else math.nan
            )
            self.hum_series_values[channel_index].append(
                hum_smoothed if hum_smoothed is not None else math.nan
            )
            self.last_plot_second[channel_index] = ts_sec
        self._invalidate_render_cache(channel_index=channel_index)

    def _schedule_redraw(self):
        """Request a graph redraw on the next Tk idle cycle."""
        if not self._redraw_pending:
            self._redraw_pending = True
            self.root.after_idle(self._do_redraw)

    def _do_redraw(self):
        self._redraw_pending = False
        self.redraw_graph()

    def redraw_graph(self, graph_kind=None):
        graph_kinds = [graph_kind] if graph_kind else list(self.graph_contexts.keys())
        visible_channels = set(self.visible_channel_indices())
        previous_active_kind = self.active_graph_kind
        for kind in graph_kinds:
            ctx = self.graph_contexts.get(kind)
            if ctx is None:
                continue
            any_points = False
            for i in range(self.CHANNEL_COUNT):
                is_visible = self.channel_record_enabled[i] and i in visible_channels
                line = ctx["lines"][i]
                line.set_visible(is_visible)
                if not is_visible:
                    line.set_data([], [])
                    continue
                times, vals = self._render_series_for_channel(kind, i)
                line.set_data(times, vals)
                if times:
                    any_points = True

            if any_points and self._auto_view:
                self._ignore_xlim_changes += 1
                self._in_redraw = True
                ctx["ax"].relim()
                ctx["ax"].autoscale_view()
                self._in_redraw = False
                if kind == self.active_graph_kind:
                    self._update_scrollbar(kind)

            self.refresh_legend(kind)
            ctx["canvas"].draw_idle()
        if previous_active_kind in self.graph_contexts:
            self.set_active_graph(previous_active_kind)

    def schedule_poll(self, interval_ms):
        self.schedule_arduino_poll(interval_ms)

    def poll_once(self):
        self.poll_arduino_once()

    def disconnect_source(self, source_kind):
        if source_kind == "arduino" and self.arduino_poll_job is not None:
            try:
                self.root.after_cancel(self.arduino_poll_job)
            except Exception:
                pass
            self.arduino_poll_job = None
            self.arduino_polling_started = False
            self.arduino_info = {"board": "", "fw_version": "", "protocol": "", "channel_count": None}
        if source_kind == "esp":
            if self.esp_init_job is not None:
                try:
                    self.root.after_cancel(self.esp_init_job)
                except Exception:
                    pass
                self.esp_init_job = None
            self.esp_init_attempts_remaining = 0
            self.esp_stream_confirmed = False
            self.esp_time_synced = False
            self.esp_node_state.clear()
            for slot_idx in range(self.ARDUINO_CHANNEL_COUNT, self.CHANNEL_COUNT):
                self.current_signals[slot_idx] = "-"
                self.update_channel_tree_row(slot_idx, signal_display="-")

        self.stop_events[source_kind].set()
        ser = self.serial_ports.get(source_kind)
        if ser is not None:
            try:
                if ser.is_open:
                    ser.close()
            except Exception:
                pass
        self.serial_ports[source_kind] = None
        self.read_threads[source_kind] = None
        self.source_connected[source_kind] = False
        self.receive_buffers[source_kind] = ""

        if source_kind == "arduino":
            self.btn_connect_arduino.config(text="Connect ARD")
        else:
            self.btn_connect_esp.config(text="Connect ESP")

        if not self.any_source_connected():
            if self.db_session_id is not None:
                finished_session_id = self.db_session_id
                self.end_db_session()
                self.append_session_to_data_csv(finished_session_id)
                self.last_session_id = finished_session_id
                self.db_session_id = None
            self.refresh_sessions_list()
        self.update_status_label()
        self.update_sessions_controls()
        self.rebuild_channel_tree()
        self._schedule_redraw()

    def close_connection(self, source_kind=None):
        if source_kind is None:
            self.disconnect_source("arduino")
            self.disconnect_source("esp")
            return
        self.disconnect_source(source_kind)

    def parse_interval_ms(self, text):
        if not text:
            return None
        normalized = text.strip().lower()
        m = re.match(r"^(\d+)ms$", normalized)
        if m:
            return max(100, int(m.group(1)))
        m = re.match(r"^(\d+)s$", normalized)
        if m:
            return int(m.group(1)) * 1000
        m = re.match(r"^(\d+)min$", normalized)
        if m:
            return int(m.group(1)) * 60000
        m = re.match(r"^(\d+)h$", normalized)
        if m:
            return int(m.group(1)) * 3600000
        return None

    def terminal_output_log_path(self):
        return os.path.join(self.base_dir, "terminal_output.log")

    def write_terminal_output_log(self, text, auto_newline=True):
        if not getattr(self, "terminal_output_logging_enabled", False):
            return
        if not text:
            return
        try:
            os.makedirs(self.base_dir, exist_ok=True)
            with open(self.terminal_output_log_path(), "a", encoding="utf-8") as fh:
                fh.write(str(text))
                if auto_newline and not str(text).endswith("\n"):
                    fh.write("\n")
        except Exception as ex:
            self.terminal_output_logging_enabled = False
            if hasattr(self, "terminal_output_logging_var"):
                try:
                    self.terminal_output_logging_var.set(False)
                except Exception:
                    pass
            if not getattr(self, "terminal_output_log_error_reported", False):
                self.terminal_output_log_error_reported = True
                try:
                    self._append_to_console_widget(
                        self.txt_console,
                        "Terminal output log disabled: {0}".format(ex),
                    )
                except Exception:
                    pass

    def on_terminal_output_logging_toggle(self):
        self.terminal_output_logging_enabled = bool(self.terminal_output_logging_var.get())
        self.terminal_output_log_error_reported = False
        if self.terminal_output_logging_enabled:
            self.append_console(">>> Terminal output logging enabled: {0}".format(self.terminal_output_log_path()))
        else:
            self.append_console(">>> Terminal output logging disabled")
        self.save_config()

    def append_console(self, text, auto_newline=True):
        if not text:
            return
        self.write_terminal_output_log(text, auto_newline=auto_newline)
        self._append_to_console_widget(self.txt_console, text, auto_newline=auto_newline)
        if self.floating_console_text is not None:
            self._append_to_console_widget(self.floating_console_text, text, auto_newline=auto_newline)

    def on_close(self):
        self.save_config()
        if self.esp_presence_job is not None:
            try:
                self.root.after_cancel(self.esp_presence_job)
            except Exception:
                pass
            self.esp_presence_job = None
        self._destroy_floating_markers_window()
        self._destroy_floating_terminal_window()
        self.close_connection()
        if self.db_conn:
            self.db_conn.close()
        self.root.destroy()
