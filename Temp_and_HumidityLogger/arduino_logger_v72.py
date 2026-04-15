
import csv
import configparser
import ctypes
import json
import math
import os
import queue
import re
import sqlite3
import sys
import threading
import time
from collections import deque
from datetime import datetime

import serial
import serial.tools.list_ports
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, colorchooser, filedialog

import matplotlib.dates as mdates
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.dates import AutoDateLocator, ConciseDateFormatter
from matplotlib.figure import Figure
from matplotlib.ticker import AutoMinorLocator, MultipleLocator, NullLocator


class ArduinoLoggerApp:
    ARDUINO_CHANNEL_COUNT = 8
    ESP_CHANNEL_COUNT = 8
    CHANNEL_COUNT = ARDUINO_CHANNEL_COUNT + ESP_CHANNEL_COUNT
    DEFAULT_INTERVAL_TEXT = "1s"
    SMOOTHING_WINDOW = 5
    HANDSHAKE_MARKER = "STH85"
    ARDUINO_BAUD_RATE = 9600
    ESP_BAUD_RATE = 460800
    DB_FILE_NAME = "logger.db"
    DATA_FILE_NAME = "data.csv"
    CONFIG_FILE_NAME = "config.ini"
    PLOT_HISTORY_SECONDS = 7 * 24 * 60 * 60
    MAX_RENDER_POINTS = 2500
    PACKET_REGEX = re.compile(
        r"CH\s*(\d+)\s*,\s*Temp\s*,\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*,\s*C\s*,\s*RH\s*,\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*,\s*%",
        re.IGNORECASE
    )
    DEFAULT_COLORS = [
        "red", "blue", "green", "orange", "purple", "brown", "magenta", "teal",
        "#e74c3c", "#3498db", "#2ecc71", "#f1c40f", "#8e44ad", "#16a085", "#d35400", "#7f8c8d"
    ]
    ZOOM_FACTOR = 0.70

    def __init__(self, root):
        self.root = root
        self.root.title("Temperature and Humidity Logger - Arduino + ESP")
        self.root.geometry("1000x800")

        if getattr(sys, "frozen", False):
            self.base_dir = os.path.abspath(os.path.dirname(sys.executable))
        else:
            self.base_dir = os.path.abspath(os.path.dirname(__file__))
        self.resource_dir = getattr(sys, "_MEIPASS", self.base_dir)
        self.data_csv_path = os.path.join(self.base_dir, self.DATA_FILE_NAME)
        self.config_path = os.path.join(self.base_dir, self.CONFIG_FILE_NAME)
        self.data_csv_initialized = False
        self.saved_interval_text = self.DEFAULT_INTERVAL_TEXT
        self.saved_arduino_port = ""
        self.saved_esp_port = ""
        self.saved_column_widths = {}
        self.saved_live_split_x = None
        self.minor_grid_enabled = True
        self.terminal_mode = "docked"
        self.terminal_visible = True
        self.terminal_window = None
        self.floating_console_text = None

        self.serial_ports = {"arduino": None, "esp": None}
        self.source_connected = {"arduino": False, "esp": False}
        self.read_threads = {"arduino": None, "esp": None}
        self.stop_events = {"arduino": threading.Event(), "esp": threading.Event()}
        self.ui_queue = queue.Queue()

        self.receive_buffers = {"arduino": "", "esp": ""}
        self.arduino_polling_started = False
        self.esp_slot_by_node_id = {}
        self.esp_init_job = None
        self.esp_init_attempts_remaining = 0
        self.esp_stream_confirmed = False

        self.current_temps = ["NaN"] * self.CHANNEL_COUNT
        self.current_hums = ["NaN"] * self.CHANNEL_COUNT
        self.temp_history = [deque(maxlen=self.SMOOTHING_WINDOW) for _ in range(self.CHANNEL_COUNT)]

        self.series_times = [deque(maxlen=self.PLOT_HISTORY_SECONDS) for _ in range(self.CHANNEL_COUNT)]
        self.series_values = [deque(maxlen=self.PLOT_HISTORY_SECONDS) for _ in range(self.CHANNEL_COUNT)]
        self.last_plot_second = [None] * self.CHANNEL_COUNT

        # Graph redraw state
        self._redraw_pending = False

        self.channel_names = [self.default_channel_name(i) for i in range(self.CHANNEL_COUNT)]
        self.channel_colors = list(self.DEFAULT_COLORS)
        self.channel_record_enabled = [True] * self.CHANNEL_COUNT
        self.temp_calibration_points = [[] for _ in range(self.CHANNEL_COUNT)]
        self.hum_calibration_points = [[] for _ in range(self.CHANNEL_COUNT)]

        self.markers = []

        self._auto_view = True
        self._in_redraw = False
        self._ignore_xlim_changes = 0

        # Mouse drag-pan state
        self._drag_press_x = None    # pixel x at button-press
        self._drag_press_y = None    # pixel y at button-press
        self._drag_xlim = None       # xlim snapshot at press
        self._drag_ylim = None       # ylim snapshot at press
        self._drag_inv_tf = None     # inverse data transform snapshot at press
        self._is_dragging = False

        # SQLite state
        self.db_conn = None
        self.db_session_id = None
        self.last_session_id = None
        self.loaded_session_id = None

        self.arduino_poll_job = None

        self._app_icon_img = None
        self._app_icon_img_small = None
        self._app_icon_img_large = None
        self._top_logo_img = None
        self.apply_app_icon()
        self.init_database()
        self.load_config()
        self.build_ui()
        self.refresh_ports()
        self.process_ui_queue()

    def default_channel_name(self, ch_idx):
        if ch_idx < self.ARDUINO_CHANNEL_COUNT:
            return "Arduino CH{0}".format(ch_idx)
        return "ESP Slot {0}".format(ch_idx - self.ARDUINO_CHANNEL_COUNT + 1)

    def channel_display_id(self, ch_idx):
        if ch_idx < self.ARDUINO_CHANNEL_COUNT:
            return "ARD{0}".format(ch_idx)
        node_id = self.find_esp_node_id_by_slot(ch_idx)
        if node_id is None:
            return "ESP{0}".format(ch_idx - self.ARDUINO_CHANNEL_COUNT + 1)
        return "ESP{0}".format(node_id)

    def find_esp_node_id_by_slot(self, slot_idx):
        for node_id, mapped_slot in self.esp_slot_by_node_id.items():
            if mapped_slot == slot_idx:
                return node_id
        return None

    def channel_has_data(self, ch_idx):
        if self.current_temps[ch_idx] not in ("NaN", "", None):
            return True
        if self.current_hums[ch_idx] not in ("NaN", "", None):
            return True
        if self.series_times[ch_idx] or self.series_values[ch_idx]:
            return True
        return False

    def channel_is_visible_in_ui(self, ch_idx):
        if ch_idx < self.ARDUINO_CHANNEL_COUNT:
            return True
        return self.find_esp_node_id_by_slot(ch_idx) is not None or self.channel_has_data(ch_idx)

    def visible_channel_indices(self):
        return [i for i in range(self.CHANNEL_COUNT) if self.channel_is_visible_in_ui(i)]

    def channel_row_values(self, ch_idx):
        temp_display = "-"
        hum_display = "-"
        if self.current_temps[ch_idx] not in ("NaN", "", None):
            temp_display = "{0} \u00b0C".format(self.current_temps[ch_idx])
        if self.current_hums[ch_idx] not in ("NaN", "", None):
            hum_display = "{0} %".format(self.current_hums[ch_idx])
        return (
            self.channel_record_cell(ch_idx),
            self.channel_display_id(ch_idx),
            self.channel_tree_name(ch_idx),
            temp_display,
            hum_display,
        )

    def rebuild_channel_tree(self):
        if not hasattr(self, "tree"):
            return
        selected = tuple(self.tree.selection())
        focus = self.tree.focus()
        self.tree.delete(*self.tree.get_children())
        for ch_idx in self.visible_channel_indices():
            tag = "ch_color_{0}".format(ch_idx)
            self.tree.insert("", tk.END, iid="ch{0}".format(ch_idx), tags=(tag,), values=self.channel_row_values(ch_idx))
        for item_id in selected:
            if self.tree.exists(item_id):
                self.tree.selection_add(item_id)
        if focus and self.tree.exists(focus):
            self.tree.focus(focus)

    def allocate_esp_slot(self, node_id):
        if node_id in self.esp_slot_by_node_id:
            return self.esp_slot_by_node_id[node_id]
        for slot_idx in range(self.ARDUINO_CHANNEL_COUNT, self.CHANNEL_COUNT):
            if self.find_esp_node_id_by_slot(slot_idx) is None:
                self.esp_slot_by_node_id[node_id] = slot_idx
                self.rebuild_channel_tree()
                return slot_idx
        self.append_console("ESP slot limit reached, dropping node {0}".format(node_id))
        return None

    def any_source_connected(self):
        return any(self.source_connected.values())

    def update_status_label(self):
        states = []
        if self.source_connected["arduino"]:
            if self.arduino_polling_started:
                states.append("Arduino polling")
            else:
                states.append("Arduino handshake")
        if self.source_connected["esp"]:
            states.append("ESP streaming")
        self.lbl_status.config(text=" | ".join(states) if states else "Disconnected")

    @classmethod
    def reading_column_names(cls):
        cols = []
        for i in range(cls.CHANNEL_COUNT):
            cols.extend(["ch{0}_temp".format(i), "ch{0}_hum".format(i)])
        return cols

    def apply_app_icon(self):
        def _apply_win_icons(ico_file):
            try:
                user32 = ctypes.windll.user32
                hicon_big = user32.LoadImageW(
                    0, ico_file, 1, 64, 64, 0x00000010
                )
                hicon_small = user32.LoadImageW(
                    0, ico_file, 1, 16, 16, 0x00000010
                )
                hwnd = int(self.root.winfo_id())
                if hicon_big:
                    user32.SendMessageW(hwnd, 0x0080, 1, hicon_big)   # WM_SETICON, ICON_BIG
                if hicon_small:
                    user32.SendMessageW(hwnd, 0x0080, 0, hicon_small)  # WM_SETICON, ICON_SMALL
            except Exception:
                pass

        if os.name == "nt":
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "Agrib.TempHumidityLogger"
                )
            except Exception:
                pass
            ico_path = os.path.join(self.resource_dir, "icons", "logo.ico")
            if os.path.exists(ico_path):
                try:
                    self.root.iconbitmap(default=ico_path)
                except Exception:
                    pass
                try:
                    self.root.after(10, lambda p=ico_path: _apply_win_icons(p))
                    self.root.after(200, lambda p=ico_path: _apply_win_icons(p))
                except Exception:
                    pass
        small_png = os.path.join(self.resource_dir, "icons", "logo.png")
        large_png = os.path.join(self.resource_dir, "icons", "logo1.png")
        try:
            if os.path.exists(small_png):
                self._app_icon_img_small = tk.PhotoImage(file=small_png)
            if os.path.exists(large_png):
                self._app_icon_img_large = tk.PhotoImage(file=large_png)
        except Exception:
            self._app_icon_img_small = None
            self._app_icon_img_large = None

        if self._app_icon_img_large is not None and self._app_icon_img_small is not None:
            try:
                # Keep first arg False (Windows quirk): allows proper small+large icon selection.
                self.root.iconphoto(False, self._app_icon_img_large, self._app_icon_img_small)
                return
            except Exception:
                pass
        one_img = self._app_icon_img_large or self._app_icon_img_small
        if one_img is not None:
            try:
                self._app_icon_img = one_img
                self.root.iconphoto(False, one_img)
            except Exception:
                pass

    # â”€â”€ Database â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def init_database(self):
        db_path = os.path.join(self.base_dir, self.DB_FILE_NAME)
        self.db_conn = sqlite3.connect(db_path)
        self.db_conn.execute("PRAGMA journal_mode=WAL")
        reading_columns_sql = ",\n                ".join(
            "{0} TEXT".format(col_name) for col_name in self.reading_column_names()
        )
        self.db_conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                ended_at   TEXT,
                name       TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS readings (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                timestamp  TEXT NOT NULL,
                {0}
            );
            CREATE TABLE IF NOT EXISTS markers (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                timestamp  TEXT NOT NULL,
                action     TEXT NOT NULL,
                note       TEXT
            );
        """.format(reading_columns_sql))
        cols = [r[1] for r in self.db_conn.execute("PRAGMA table_info(sessions)").fetchall()]
        if "name" not in cols:
            self.db_conn.execute("ALTER TABLE sessions ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        reading_cols = {r[1] for r in self.db_conn.execute("PRAGMA table_info(readings)").fetchall()}
        for col_name in self.reading_column_names():
            if col_name not in reading_cols:
                self.db_conn.execute("ALTER TABLE readings ADD COLUMN {0} TEXT".format(col_name))
        self.db_conn.commit()

    # â”€â”€ Config â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def load_config(self):
        def _parse_points(text):
            points_map = {}
            raw = (text or "").strip()
            if not raw:
                return []
            for part in raw.split(";"):
                item = part.strip()
                if not item or ":" not in item:
                    continue
                lhs, rhs = item.split(":", 1)
                try:
                    raw_v = float(lhs.strip())
                    ref_v = float(rhs.strip())
                except ValueError:
                    continue
                points_map[raw_v] = ref_v
            return sorted(points_map.items(), key=lambda x: x[0])

        cfg = configparser.ConfigParser()
        if not os.path.exists(self.config_path):
            return
        try:
            cfg.read(self.config_path, encoding="utf-8")
        except Exception:
            return

        if cfg.has_section("app"):
            geometry = cfg.get("app", "window_geometry", fallback="").strip()
            if geometry:
                try:
                    self.root.geometry(geometry)
                except Exception:
                    pass
            interval_text = cfg.get("app", "interval", fallback=self.DEFAULT_INTERVAL_TEXT).strip()
            if interval_text:
                self.saved_interval_text = interval_text
            self.saved_arduino_port = cfg.get(
                "app", "last_arduino_port", fallback=cfg.get("app", "last_port", fallback="")
            ).strip()
            self.saved_esp_port = cfg.get("app", "last_esp_port", fallback="").strip()
            minor_grid_text = cfg.get("app", "minor_grid_enabled", fallback="1").strip().lower()
            self.minor_grid_enabled = minor_grid_text in ("1", "true", "yes", "on")
            terminal_mode = cfg.get("app", "terminal_mode", fallback="").strip().lower()
            terminal_visible_text = cfg.get("app", "terminal_visible", fallback="1").strip().lower()
            if terminal_mode in ("docked", "hidden", "floating"):
                self.terminal_mode = terminal_mode
            else:
                self.terminal_mode = "docked" if terminal_visible_text in ("1", "true", "yes", "on") else "hidden"
            self.terminal_visible = self.terminal_mode != "hidden"

        for i in range(self.CHANNEL_COUNT):
            section = "channel_{0}".format(i)
            if not cfg.has_section(section):
                continue
            name = cfg.get(section, "name", fallback=self.channel_names[i]).strip()
            color = cfg.get(section, "color", fallback=self.channel_colors[i]).strip()
            enabled = cfg.get(section, "enabled", fallback="true").strip().lower()
            if name:
                self.channel_names[i] = name
            if color:
                self.channel_colors[i] = color
            self.channel_record_enabled[i] = enabled in ("1", "true", "yes", "on")
            self.temp_calibration_points[i] = _parse_points(
                cfg.get(section, "temp_calibration", fallback="")
            )
            self.hum_calibration_points[i] = _parse_points(
                cfg.get(section, "hum_calibration", fallback="")
            )

        if cfg.has_section("columns"):
            for col_id in ("rec", "id", "name", "temp", "hum"):
                width_text = cfg.get("columns", col_id, fallback="").strip()
                if not width_text:
                    continue
                try:
                    width_value = int(width_text)
                except ValueError:
                    continue
                if width_value > 20:
                    self.saved_column_widths[col_id] = width_value

        if cfg.has_section("layout"):
            split_text = cfg.get("layout", "live_split_x", fallback="").strip()
            if split_text:
                try:
                    split_val = int(split_text)
                except ValueError:
                    split_val = None
                if split_val is not None and split_val > 100:
                    self.saved_live_split_x = split_val

    def save_config(self):
        def _dump_points(points):
            if not points:
                return ""
            return ";".join("{0:.6g}:{1:.6g}".format(p[0], p[1]) for p in points)

        cfg = configparser.ConfigParser()
        cfg["app"] = {
            "window_geometry": self.root.geometry(),
            "interval": (self.txt_interval.get().strip() or self.DEFAULT_INTERVAL_TEXT),
            "last_arduino_port": self.cmb_arduino_port.get().strip(),
            "last_esp_port": self.cmb_esp_port.get().strip(),
            "minor_grid_enabled": "1" if self.minor_grid_enabled else "0",
            "terminal_mode": self.terminal_mode,
            "terminal_visible": "1" if self.terminal_visible else "0",
        }
        if hasattr(self, "tree"):
            cfg["columns"] = {
                "rec": str(int(self.tree.column("rec", "width"))),
                "id": str(int(self.tree.column("id", "width"))),
                "name": str(int(self.tree.column("name", "width"))),
                "temp": str(int(self.tree.column("temp", "width"))),
                "hum": str(int(self.tree.column("hum", "width"))),
            }
        if hasattr(self, "live_split"):
            try:
                split_x = int(self.live_split.sash_coord(0)[0])
            except Exception:
                split_x = self.saved_live_split_x
            if split_x is not None:
                cfg["layout"] = {"live_split_x": str(split_x)}
        for i in range(self.CHANNEL_COUNT):
            section = "channel_{0}".format(i)
            cfg[section] = {
                "name": self.channel_names[i],
                "color": self.channel_colors[i],
                "enabled": "1" if self.channel_record_enabled[i] else "0",
                "temp_calibration": _dump_points(self.temp_calibration_points[i]),
                "hum_calibration": _dump_points(self.hum_calibration_points[i]),
            }
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                cfg.write(f)
        except Exception as ex:
            self.append_console("Config save error: {0}".format(ex))

    def start_db_session(self):
        cur = self.db_conn.execute(
            "INSERT INTO sessions (started_at) VALUES (?)",
            (datetime.now().isoformat(),)
        )
        self.db_conn.commit()
        self.db_session_id = cur.lastrowid
        self.last_session_id = self.db_session_id

    def end_db_session(self):
        if self.db_session_id is not None:
            self.db_conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (datetime.now().isoformat(), self.db_session_id)
            )
            self.db_conn.commit()

    def save_to_db(self, timestamp):
        if self.db_session_id is None:
            return
        try:
            t = []
            h = []
            for i in range(self.CHANNEL_COUNT):
                if self.channel_record_enabled[i]:
                    t.append(self.current_temps[i])
                    h.append(self.current_hums[i])
                else:
                    t.append("")
                    h.append("")
            reading_columns = self.reading_column_names()
            placeholders = ",".join("?" for _ in range(2 + len(reading_columns)))
            insert_columns = ", ".join(["session_id", "timestamp"] + reading_columns)
            reading_values = []
            for i in range(self.CHANNEL_COUNT):
                reading_values.extend([t[i], h[i]])
            self.db_conn.execute(
                "INSERT INTO readings ({0}) VALUES ({1})".format(insert_columns, placeholders),
                [self.db_session_id, timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")] + reading_values
            )
            self.db_conn.commit()
        except Exception as ex:
            self.append_console("DB write error: {0}".format(ex))

    def _save_marker_to_db(self, action, dt, note):
        if self.db_session_id is None:
            return
        try:
            self.db_conn.execute(
                "INSERT INTO markers (session_id, timestamp, action, note) VALUES (?,?,?,?)",
                (self.db_session_id, dt.strftime("%Y-%m-%dT%H:%M:%S.%f"), action, note)
            )
            self.db_conn.commit()
        except Exception as ex:
            self.append_console("Marker DB write error: {0}".format(ex))

    def _csv_header(self):
        header = ["Timestamp", "Marker", "Marker Text"]
        for i in range(self.CHANNEL_COUNT):
            header.extend(["CH{0}_T".format(i), "CH{0}_H".format(i)])
        return header

    def session_has_data(self, session_id):
        row = self.db_conn.execute(
            """SELECT
                   (SELECT COUNT(*) FROM readings WHERE session_id = ?) +
                   (SELECT COUNT(*) FROM markers  WHERE session_id = ?)""",
            (session_id, session_id)
        ).fetchone()
        return bool(row and row[0] > 0)

    def iter_session_rows(self, session_id):
        reading_columns_sql = ", ".join(self.reading_column_names())
        readings_cur = self.db_conn.execute(
            """SELECT timestamp, {0}
               FROM readings
               WHERE session_id = ?
               ORDER BY timestamp""".format(reading_columns_sql),
            (session_id,)
        )
        markers_cur = self.db_conn.execute(
            "SELECT timestamp, action, COALESCE(note, '') FROM markers WHERE session_id = ? ORDER BY timestamp",
            (session_id,)
        )

        r = readings_cur.fetchone()
        m = markers_cur.fetchone()
        empty_channels = tuple("" for _ in self.reading_column_names())

        while r is not None or m is not None:
            r_dt = self._parse_db_timestamp(r[0]) if r is not None else None
            m_dt = self._parse_db_timestamp(m[0]) if m is not None else None

            use_reading = False
            if r is not None and m is None:
                use_reading = True
            elif r is not None and m is not None:
                if r_dt is None:
                    use_reading = False
                elif m_dt is None:
                    use_reading = True
                else:
                    use_reading = r_dt <= m_dt

            if use_reading:
                yield (r[0], "", "") + tuple(r[1:])
                r = readings_cur.fetchone()
            else:
                marker_ts = m[0].replace("T", " ")
                yield (marker_ts, m[1], m[2]) + empty_channels
                m = markers_cur.fetchone()

    def export_session_csv(self, show_dialog=True):
        """Export current session from SQLite to a timestamped CSV file."""
        session_id = self.db_session_id if self.db_session_id is not None else self.last_session_id
        if session_id is None:
            if show_dialog:
                messagebox.showwarning("Export", "No session to export.")
            return
        self.export_session_csv_by_id(session_id, show_dialog=show_dialog)

    def export_session_csv_by_id(self, session_id, show_dialog=True):
        if session_id is None:
            if show_dialog:
                messagebox.showwarning("Export", "No session selected.")
            return

        if not self.session_has_data(session_id):
            if show_dialog:
                messagebox.showinfo("Export", "No data recorded in this session.")
            return

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        session_name = self.get_session_name(session_id)
        safe_name = self._safe_filename_part(session_name)
        if safe_name:
            filename = "log_{0}_{1}_{2}.csv".format(session_id, safe_name, ts)
        else:
            filename = "log_{0}_{1}.csv".format(session_id, ts)
        filepath = os.path.join(self.base_dir, filename)

        if show_dialog:
            chosen = filedialog.asksaveasfilename(
                title="Save Session CSV",
                initialdir=self.base_dir,
                initialfile=filename,
                defaultextension=".csv",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
            )
            if not chosen:
                return
            filepath = chosen
            filename = os.path.basename(filepath)

        try:
            with open(filepath, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(self._csv_header())
                for row in self.iter_session_rows(session_id):
                    w.writerow(row)

            self.append_console(">>> Exported to: {0}".format(filename))
            if show_dialog:
                messagebox.showinfo("Export Complete",
                                    "Session data saved to:\n{0}".format(filepath))
        except Exception as ex:
            self.append_console("Export error: {0}".format(ex))
            if show_dialog:
                messagebox.showerror("Export Error", str(ex))

    def append_session_to_data_csv(self, session_id):
        if not self.session_has_data(session_id):
            return
        mode = "a" if self.data_csv_initialized else "w"
        try:
            with open(self.data_csv_path, mode, newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if not self.data_csv_initialized:
                    w.writerow(self._csv_header())
                for row in self.iter_session_rows(session_id):
                    w.writerow(row)
            self.data_csv_initialized = True
            self.append_console(">>> Saved to: {0}".format(self.DATA_FILE_NAME))
        except Exception as ex:
            self.append_console("data.csv write error: {0}".format(ex))

    def get_sessions(self):
        return self.db_conn.execute(
            """SELECT s.id,
                      COALESCE(s.name, ''),
                      s.started_at,
                      COALESCE(s.ended_at, ''),
                      (SELECT COUNT(*) FROM readings r WHERE r.session_id = s.id) AS reading_count,
                      (SELECT COUNT(*) FROM markers m WHERE m.session_id = s.id) AS marker_event_count
               FROM sessions s
               ORDER BY s.id DESC"""
        ).fetchall()

    def get_session_name(self, session_id):
        row = self.db_conn.execute("SELECT COALESCE(name, '') FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return (row[0] if row else "").strip()

    def _safe_filename_part(self, text):
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (text or "").strip())
        return cleaned.strip("._")

    def sanity_check_session_counter(self):
        count_row = self.db_conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        if not count_row or count_row[0] != 0:
            return
        try:
            self.db_conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('sessions','readings','markers')")
            self.db_conn.commit()
        except Exception:
            pass

    # â”€â”€ UI â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def build_ui(self):
        style = ttk.Style()
        style.configure("TNotebook.Tab", font=("Segoe UI", 12, "bold"), padding=[22, 8])

        top = tk.Frame(self.root, bg="#e6e6e6", bd=1, relief="solid", height=70)
        top.pack(side=tk.TOP, fill=tk.X)
        top.pack_propagate(False)

        tk.Label(top, text="Interval:", bg="#e6e6e6").place(x=10, y=25)
        self.txt_interval = tk.Entry(top, width=8)
        self.txt_interval.place(x=80, y=23)
        self.txt_interval.insert(0, self.saved_interval_text)
        self.txt_interval.bind("<Return>", self.on_interval_changed)
        self.txt_interval.bind("<FocusOut>", self.on_interval_changed)

        tk.Label(top, text="Arduino:", bg="#e6e6e6").place(x=150, y=25)
        self.cmb_arduino_port = ttk.Combobox(top, width=10, state="readonly")
        self.cmb_arduino_port.place(x=212, y=23)
        self.btn_connect_arduino = tk.Button(
            top, text="Connect ARD", width=12, command=self.on_connect_arduino_click
        )
        self.btn_connect_arduino.place(x=315, y=20)

        tk.Label(top, text="ESP:", bg="#e6e6e6").place(x=430, y=25)
        self.cmb_esp_port = ttk.Combobox(top, width=10, state="readonly")
        self.cmb_esp_port.place(x=465, y=23)
        self.btn_connect_esp = tk.Button(
            top, text="Connect ESP", width=12, command=self.on_connect_esp_click
        )
        self.btn_connect_esp.place(x=568, y=20)

        self.btn_refresh = tk.Button(top, text="Refresh", width=8, command=self.refresh_ports)
        self.btn_refresh.place(x=690, y=20)

        # self.btn_export = tk.Button(
        #     top, text="Export CSV", width=11,
        #     command=lambda: self.export_session_csv(show_dialog=True),
        #     bg="#27ae60", fg="white", font=("Segoe UI", 9, "bold")
        # )
        # self.btn_export.place(x=490, y=20)

        self.lbl_status = tk.Label(top, text="Ready", bg="#e6e6e6", font=("Segoe UI", 10, "bold"))
        self.lbl_status.place(x=770, y=25)

        self.btn_settings = tk.Menubutton(top, text="Menu", font=("Segoe UI", 9, "bold"),
                                          bg="#dcdcdc", relief="raised", width=6)
        self.btn_settings.place(relx=0.985, y=8, anchor="ne")
        settings_menu = tk.Menu(self.btn_settings, tearoff=0)
        self.btn_settings.config(menu=settings_menu)
        settings_menu.add_command(label="COM settings...", command=self.open_com_settings)
        settings_menu.add_command(label="Refresh COM ports", command=self.refresh_ports)
        settings_menu.add_separator()
        self.minor_grid_var = tk.BooleanVar(value=self.minor_grid_enabled)
        settings_menu.add_checkbutton(
            label="Chart minor grid",
            variable=self.minor_grid_var,
            command=self.on_minor_grid_toggle
        )
        self.terminal_visible_var = tk.BooleanVar(value=self.terminal_visible)
        settings_menu.add_checkbutton(
            label="Show terminal",
            variable=self.terminal_visible_var,
            command=self.on_terminal_visibility_toggle
        )
        self.terminal_floating_var = tk.BooleanVar(value=self.terminal_mode == "floating")
        settings_menu.add_checkbutton(
            label="Undock terminal",
            variable=self.terminal_floating_var,
            command=self.on_terminal_floating_toggle
        )
        settings_menu.add_separator()
        settings_menu.add_command(
            label="Temperature calibration...",
            command=lambda: self.open_calibration_manager("temp")
        )
        settings_menu.add_command(
            label="Humidity calibration...",
            command=lambda: self.open_calibration_manager("hum")
        )

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        tab_live = ttk.Frame(self.notebook)
        self.tab_graph = ttk.Frame(self.notebook)
        self.tab_sessions = ttk.Frame(self.notebook)
        self.notebook.add(tab_live, text="Live View")
        self.notebook.add(self.tab_graph, text="Graphics")
        self.notebook.add(self.tab_sessions, text="Sessions")
        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_changed)

        # â”€â”€ Live View Tab â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        self.live_split = tk.PanedWindow(tab_live, orient=tk.HORIZONTAL, sashrelief="raised")
        self.live_split.pack(fill=tk.BOTH, expand=True)
        self.live_left = tk.Frame(self.live_split)
        self.live_right = tk.Frame(self.live_split)
        self.live_split.add(self.live_left, minsize=560)
        self.live_split.add(self.live_right, minsize=280)
        self.root.after(10, self._apply_initial_live_layout)

        self.tree = ttk.Treeview(self.live_left, columns=("rec", "id", "name", "temp", "hum"), show="headings")
        self.tree.heading("rec", text="Active")
        self.tree.heading("id", text="ID")
        self.tree.heading("name", text="Name  (double-click to edit)")
        self.tree.heading("temp", text="Temp")
        self.tree.heading("hum", text="Hum")
        self.tree.column("rec", width=64, anchor="center")
        self.tree.column("id", width=60, anchor="center")
        self.tree.column("name", width=220, anchor="w")
        self.tree.column("temp", width=120, anchor="center")
        self.tree.column("hum", width=120, anchor="center")
        for col_id, width_value in self.saved_column_widths.items():
            if col_id in ("rec", "id", "name", "temp", "hum"):
                self.tree.column(col_id, width=width_value)
        self.tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<Double-1>", self.on_tree_double_click)

        for i in range(self.CHANNEL_COUNT):
            tag = "ch_color_{0}".format(i)
            self.tree.tag_configure(tag, foreground=self.channel_colors[i])
        self.rebuild_channel_tree()

        tk.Label(self.live_right, text="Terminal", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=8, pady=(8, 4)
        )
        self.txt_console = tk.Text(
            self.live_right, height=10, bg="black", fg="lime",
            font=("Consolas", 9), wrap="word"
        )
        self.txt_console.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

    
        # â”€â”€ Graphics Tab â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        content_frame = tk.Frame(self.tab_graph)
        chart_frame = tk.Frame(content_frame)

        self.figure = Figure(figsize=(8, 5), dpi=100)
        self.ax = self.figure.add_subplot(111)
        self.ax.set_title("Temperature")
        self.ax.set_title("", loc="left")
        self.ax.set_xlabel("Time")
        self.ax.set_ylabel("Temperature (Â°C)")

        locator = AutoDateLocator()
        self.ax.xaxis.set_major_locator(locator)
        self.ax.xaxis.set_major_formatter(ConciseDateFormatter(locator))
        self.apply_grid_settings()
        self.figure.autofmt_xdate(rotation=30)

        self.lines = []
        for i in range(self.CHANNEL_COUNT):
            line, = self.ax.plot([], [], label=self.channel_names[i], linewidth=2,
                                 color=self.channel_colors[i])
            self.lines.append(line)
        self.refresh_legend()

        self.ax.callbacks.connect("xlim_changed", self._on_xlim_changed)

        self.canvas = FigureCanvasTkAgg(self.figure, master=chart_frame)
        self.canvas.mpl_connect("button_press_event", self._on_mouse_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_mouse_drag)
        self.canvas.mpl_connect("button_release_event", self._on_mouse_release)
        self.canvas.mpl_connect("scroll_event", self._on_mouse_wheel)

        # Toolbar row
        toolbar_row = tk.Frame(self.tab_graph, bg="#f4f4f4", bd=1, relief="flat")
        toolbar_row.pack(side=tk.TOP, fill=tk.X)

        tk.Label(
            toolbar_row,
            text="Left-drag â†’ pan     Double-click â†’ add marker     Double-click marker â†’ edit",
            font=("Segoe UI", 8), fg="#666666", bg="#f4f4f4"
        ).pack(side=tk.LEFT, padx=(8, 6), pady=5)

        ttk.Separator(toolbar_row, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=4, pady=3)

        btn_kw = dict(font=("Segoe UI", 9, "bold"), relief="groove", bd=1, padx=6, pady=2)
        tk.Button(toolbar_row, text="Reset View", command=self._reset_view,
                  bg="#e8e8e8", **btn_kw).pack(side=tk.LEFT, padx=(0, 2), pady=4)
        tk.Button(toolbar_row, text=" \u2212 ", command=self._zoom_out,
                  bg="#e8e8e8", **btn_kw).pack(side=tk.LEFT, padx=1, pady=4)
        tk.Button(toolbar_row, text=" + ", command=self._zoom_in,
                  bg="#e8e8e8", **btn_kw).pack(side=tk.LEFT, padx=(1, 0), pady=4)

        ttk.Separator(toolbar_row, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=3)

        nav_frame = tk.Frame(toolbar_row, bg="#f4f4f4")
        nav_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.toolbar = NavigationToolbar2Tk(self.canvas, nav_frame)
        self.toolbar.update()

        # Content area
        content_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        chart_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.h_scroll = ttk.Scrollbar(chart_frame, orient=tk.HORIZONTAL,
                                       command=self._on_xscroll)
        self.h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Markers panel
        markers_panel = tk.Frame(content_frame, width=265, bg="#f0f0f0", bd=1, relief="sunken")
        markers_panel.pack(side=tk.RIGHT, fill=tk.Y)
        markers_panel.pack_propagate(False)

        tk.Label(markers_panel, text="Markers", bg="#f0f0f0",
                 font=("Segoe UI", 11, "bold"), pady=6).pack(fill=tk.X, padx=8)
        ttk.Separator(markers_panel, orient="horizontal").pack(fill=tk.X)

        list_frame = tk.Frame(markers_panel, bg="#f0f0f0")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        sb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        self.markers_listbox = tk.Listbox(
            list_frame, yscrollcommand=sb.set,
            font=("Consolas", 8), selectmode=tk.SINGLE,
            bg="#ffffff", activestyle="none", bd=0, relief="flat"
        )
        self.markers_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.config(command=self.markers_listbox.yview)
        self.markers_listbox.bind("<Double-1>", self.on_listbox_double_click)

        tk.Button(
            markers_panel, text="Delete Selected", command=self.delete_marker,
            bg="#c0392b", fg="white", font=("Segoe UI", 9), pady=4
        ).pack(fill=tk.X, padx=8, pady=(0, 8))

        self.build_sessions_tab(self.tab_sessions)
        self.refresh_sessions_list()

    # â”€â”€ Graph scroll / zoom â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def place_top_logo(self, parent):
        icon_path = os.path.join(self.resource_dir, "icons", "logo.png")
        if not os.path.exists(icon_path):
            return
        try:
            src = tk.PhotoImage(file=icon_path)
            target = 28
            factor = max(1, max(src.width() // target, src.height() // target))
            self._top_logo_img = src.subsample(factor, factor)
            tk.Label(parent, image=self._top_logo_img, bg="#e6e6e6").place(relx=0.94, y=8, anchor="ne")
        except Exception:
            pass

    def _set_live_split(self, x):
        if not hasattr(self, "live_split"):
            return
        try:
            self.live_split.sash_place(0, int(x), 0)
        except Exception:
            pass

    def _capture_live_split_x(self):
        if not hasattr(self, "live_split") or self.terminal_mode != "docked":
            return
        try:
            split_x = int(self.live_split.sash_coord(0)[0])
        except Exception:
            split_x = None
        if split_x is not None and split_x > 100:
            self.saved_live_split_x = split_x

    def _terminal_pane_names(self):
        if not hasattr(self, "live_split"):
            return set()
        try:
            return {str(name) for name in self.live_split.panes()}
        except Exception:
            return set()

    def _attach_terminal_pane(self):
        if not hasattr(self, "live_split") or not hasattr(self, "live_right"):
            return
        right_pane_name = str(self.live_right)
        if right_pane_name not in self._terminal_pane_names():
            self.live_split.add(self.live_right, minsize=280)
        target_x = self.saved_live_split_x if self.saved_live_split_x is not None else 700
        self.root.after(10, lambda x=target_x: self._set_live_split(x))

    def _detach_terminal_pane(self):
        if not hasattr(self, "live_split") or not hasattr(self, "live_right"):
            return
        right_pane_name = str(self.live_right)
        if right_pane_name in self._terminal_pane_names():
            self._capture_live_split_x()
            try:
                self.live_split.forget(self.live_right)
            except Exception:
                pass

    def _append_to_console_widget(self, widget, text, auto_newline=True):
        if widget is None:
            return
        try:
            widget.insert(tk.END, text)
            if auto_newline and not text.endswith("\n"):
                widget.insert(tk.END, "\n")
            line_count = int(widget.index("end-1c").split(".")[0])
            if line_count > self._CONSOLE_MAX_LINES:
                widget.delete("1.0", "{0}.0".format(line_count - self._CONSOLE_MAX_LINES))
            widget.see(tk.END)
        except Exception:
            pass

    def _create_floating_terminal_window(self):
        if self.terminal_window is not None and self.terminal_window.winfo_exists():
            self.terminal_window.deiconify()
            self.terminal_window.lift()
            return

        self.terminal_window = tk.Toplevel(self.root)
        self.terminal_window.title("Terminal")
        self.terminal_window.geometry("680x320")
        self.terminal_window.transient(self.root)

        tk.Label(self.terminal_window, text="Terminal", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=8, pady=(8, 4)
        )
        self.floating_console_text = tk.Text(
            self.terminal_window, height=10, bg="black", fg="lime",
            font=("Consolas", 9), wrap="word"
        )
        self.floating_console_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        try:
            existing_text = self.txt_console.get("1.0", tk.END)
            if existing_text:
                self.floating_console_text.insert("1.0", existing_text)
                self.floating_console_text.see(tk.END)
        except Exception:
            pass
        self.terminal_window.protocol("WM_DELETE_WINDOW", self._on_terminal_window_close)

    def _destroy_floating_terminal_window(self):
        if self.terminal_window is not None:
            try:
                if self.terminal_window.winfo_exists():
                    self.terminal_window.destroy()
            except Exception:
                pass
        self.terminal_window = None
        self.floating_console_text = None

    def _on_terminal_window_close(self):
        self.terminal_mode = "docked"
        self.apply_terminal_mode(persist=True)

    def apply_terminal_mode(self, persist=True):
        if not hasattr(self, "live_split") or not hasattr(self, "live_right"):
            return

        if self.terminal_mode == "floating":
            self._detach_terminal_pane()
            self._create_floating_terminal_window()
            self.terminal_visible = True
        elif self.terminal_mode == "hidden":
            self._destroy_floating_terminal_window()
            self._detach_terminal_pane()
            self.terminal_visible = False
        else:
            self.terminal_mode = "docked"
            self._destroy_floating_terminal_window()
            self._attach_terminal_pane()
            self.terminal_visible = True

        if hasattr(self, "terminal_visible_var"):
            self.terminal_visible_var.set(self.terminal_mode != "hidden")
        if hasattr(self, "terminal_floating_var"):
            self.terminal_floating_var.set(self.terminal_mode == "floating")
        if persist:
            self.save_config()

    def _apply_initial_live_layout(self):
        self.apply_terminal_mode(persist=False)

    def build_sessions_tab(self, parent):
        top = tk.Frame(parent, bg="#f4f4f4", bd=1, relief="flat")
        top.pack(side=tk.TOP, fill=tk.X)

        tk.Button(top, text="Refresh", width=10, command=self.refresh_sessions_list).pack(
            side=tk.LEFT, padx=6, pady=6
        )
        self.btn_session_load = tk.Button(top, text="Load to Graph", width=12,
                                          command=self.load_selected_session_to_graph)
        self.btn_session_load.pack(side=tk.LEFT, padx=6, pady=6)
        tk.Button(top, text="Export CSV", width=10, command=self.export_selected_session).pack(
            side=tk.LEFT, padx=6, pady=6
        )
        tk.Button(top, text="Delete Session", width=12, command=self.delete_selected_session,
                  bg="#c0392b", fg="white").pack(side=tk.LEFT, padx=6, pady=6)

        split = tk.PanedWindow(parent, orient=tk.HORIZONTAL, sashrelief="raised")
        split.pack(fill=tk.BOTH, expand=True)

        left = tk.Frame(split)
        right = tk.Frame(split)
        split.add(left, minsize=560)
        split.add(right, minsize=280)

        self.sessions_tree = ttk.Treeview(
            left,
            columns=("id", "name", "started", "ended", "rows", "markers", "state"),
            show="headings"
        )
        self.sessions_tree.heading("id", text="ID")
        self.sessions_tree.heading("name", text="Name")
        self.sessions_tree.heading("started", text="Started")
        self.sessions_tree.heading("ended", text="Ended")
        self.sessions_tree.heading("rows", text="Readings")
        self.sessions_tree.heading("markers", text="Marker Events")
        self.sessions_tree.heading("state", text="State")
        self.sessions_tree.column("id", width=55, anchor="center")
        self.sessions_tree.column("name", width=150, anchor="w")
        self.sessions_tree.column("started", width=170, anchor="w")
        self.sessions_tree.column("ended", width=170, anchor="w")
        self.sessions_tree.column("rows", width=85, anchor="e")
        self.sessions_tree.column("markers", width=100, anchor="e")
        self.sessions_tree.column("state", width=75, anchor="center")
        self.sessions_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.sessions_tree.bind("<<TreeviewSelect>>", self.on_session_select)
        self.sessions_tree.bind("<Double-1>", self.on_sessions_tree_double_click)

        sb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.sessions_tree.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.sessions_tree.configure(yscrollcommand=sb.set)

        tk.Label(right, text="Session Preview", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=8, pady=(8, 4)
        )
        self.session_preview = tk.Text(right, bg="#111111", fg="#d8f7d8", font=("Consolas", 9), wrap="word")
        self.session_preview.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.update_sessions_controls()

    def update_sessions_controls(self):
        active_recording = self.any_source_connected() and self.db_session_id is not None
        if hasattr(self, "btn_session_load"):
            self.btn_session_load.config(state=(tk.DISABLED if active_recording else tk.NORMAL))

    def _parse_db_timestamp(self, text):
        if not text:
            return None
        txt = str(text).strip().replace("T", " ")
        try:
            return datetime.fromisoformat(txt)
        except ValueError:
            return None

    def _get_selected_session_id(self):
        if not hasattr(self, "sessions_tree"):
            return None
        sel = self.sessions_tree.selection()
        item = sel[0] if sel else self.sessions_tree.focus()
        if not item:
            return None
        try:
            return int(item)
        except ValueError:
            values = self.sessions_tree.item(item, "values")
            if not values:
                return None
            try:
                return int(values[0])
            except Exception:
                return None

    def refresh_sessions_list(self):
        if not hasattr(self, "sessions_tree"):
            return
        self.sanity_check_session_counter()
        self.update_sessions_controls()
        selected = self._get_selected_session_id()
        self.sessions_tree.delete(*self.sessions_tree.get_children())
        for session_id, session_name, started, ended, row_count, marker_count in self.get_sessions():
            state = ""
            if self.db_session_id == session_id:
                state = "ACTIVE"
            elif self.loaded_session_id == session_id:
                state = "LOADED"
            self.sessions_tree.insert(
                "",
                tk.END,
                iid=str(session_id),
                values=(session_id, session_name, started, ended or "-", row_count, marker_count, state)
            )
        if selected is not None and self.sessions_tree.exists(str(selected)):
            self.sessions_tree.selection_set(str(selected))
            self.sessions_tree.focus(str(selected))
            self.on_session_select(None)
        else:
            self._set_session_preview("Select a session to preview or load.")

    def _set_session_preview(self, text):
        if not hasattr(self, "session_preview"):
            return
        self.session_preview.delete("1.0", tk.END)
        self.session_preview.insert(tk.END, text)
        self.session_preview.see(tk.END)

    def on_session_select(self, _event):
        session_id = self._get_selected_session_id()
        if session_id is None:
            self._set_session_preview("Select a session to preview or load.")
            return
        info = self.db_conn.execute(
            "SELECT COALESCE(name, ''), started_at, COALESCE(ended_at, '') FROM sessions WHERE id = ?",
            (session_id,)
        ).fetchone()
        if info is None:
            self._set_session_preview("Session not found.")
            return
        session_name, started, ended = info
        readings = self.db_conn.execute(
            "SELECT timestamp, {0} FROM readings WHERE session_id = ? ORDER BY timestamp LIMIT 12".format(
                ", ".join(self.reading_column_names())
            ),
            (session_id,)
        ).fetchall()
        markers = self.db_conn.execute(
            "SELECT timestamp, action, note FROM markers WHERE session_id = ? ORDER BY id LIMIT 12",
            (session_id,)
        ).fetchall()

        lines = [
            "Session #{0}".format(session_id),
            "Name:    {0}".format(session_name or "(empty)"),
            "Started: {0}".format(started),
            "Ended:   {0}".format(ended or "(active/incomplete)"),
            "",
            "Sample readings (Timestamp | Slot | Temp | Hum):",
        ]
        if readings:
            for row in readings:
                appended = False
                for i in range(self.CHANNEL_COUNT):
                    temp_text = row[1 + 2 * i]
                    hum_text = row[2 + 2 * i]
                    if temp_text in (None, "") and hum_text in (None, ""):
                        continue
                    lines.append("  {0} | CH{1} | {2} | {3}".format(row[0], i, temp_text, hum_text))
                    appended = True
                    break
                if not appended:
                    lines.append("  {0} | (empty)".format(row[0]))
        else:
            lines.append("  (no readings)")
        lines.append("")
        lines.append("Marker events:")
        if markers:
            for ts, action, note in markers:
                lines.append("  {0} | {1} | {2}".format(ts, action, note or ""))
        else:
            lines.append("  (no marker events)")
        self._set_session_preview("\n".join(lines))

    def _clear_all_markers(self):
        for marker in self.markers:
            try:
                marker["vline"].remove()
            except Exception:
                pass
            try:
                marker["annotation"].remove()
            except Exception:
                pass
        self.markers = []
        self._rebuild_listbox()

    def _get_final_markers_for_session(self, session_id):
        events = self.db_conn.execute(
            "SELECT timestamp, action, note FROM markers WHERE session_id = ? ORDER BY id",
            (session_id,)
        ).fetchall()
        final_markers = []
        for ts_text, action, note in events:
            dt = self._parse_db_timestamp(ts_text)
            if dt is None:
                continue
            note_text = (note or "").strip() or "(no note)"
            if action == "ADD":
                final_markers.append({"datetime": dt, "note": note_text})
            elif action == "EDIT":
                for m in reversed(final_markers):
                    if m["datetime"] == dt:
                        m["note"] = note_text
                        break
            elif action == "DEL":
                for idx in range(len(final_markers) - 1, -1, -1):
                    if final_markers[idx]["datetime"] == dt:
                        final_markers.pop(idx)
                        break
        return final_markers

    def load_selected_session_to_graph(self):
        if self.any_source_connected() and self.db_session_id is not None:
            messagebox.showwarning("Sessions", "Stop recording before loading a previous session.")
            return
        session_id = self._get_selected_session_id()
        if session_id is None:
            messagebox.showwarning("Sessions", "Select a session first.")
            return
        self.load_session_to_graph(session_id)
        self.loaded_session_id = session_id
        self.refresh_sessions_list()
        self.notebook.select(self.tab_graph)
        self.append_console(">>> Loaded session #{0} into graph".format(session_id))

    def load_session_to_graph(self, session_id):
        self._clear_all_markers()
        self.esp_slot_by_node_id.clear()
        for i in range(self.CHANNEL_COUNT):
            self.temp_history[i].clear()
            self.series_times[i].clear()
            self.series_values[i].clear()
            self.last_plot_second[i] = None
            self.current_temps[i] = "NaN"
            self.current_hums[i] = "NaN"

        rows = self.db_conn.execute(
            "SELECT timestamp, {0} FROM readings WHERE session_id = ? ORDER BY timestamp".format(
                ", ".join(self.reading_column_names())
            ),
            (session_id,)
        )
        for row in rows:
            dt = self._parse_db_timestamp(row[0])
            if dt is None:
                continue
            for i in range(self.CHANNEL_COUNT):
                temp_text = row[1 + 2 * i]
                hum_text = row[2 + 2 * i]
                if temp_text not in (None, ""):
                    self.current_temps[i] = str(temp_text)
                    self.current_hums[i] = str(hum_text) if hum_text not in (None, "") else ""
                    try:
                        self.add_smoothed_point(i, dt, float(temp_text))
                    except (TypeError, ValueError):
                        pass

        for marker in self._get_final_markers_for_session(session_id):
            self._place_marker(marker["datetime"], marker["note"], save_to_db=False)

        self.rebuild_channel_tree()
        for i in range(self.CHANNEL_COUNT):
            temp_disp = "-"
            hum_disp = "-"
            if self.current_temps[i] not in ("NaN", "", None):
                temp_disp = "{0} \u00b0C".format(self.current_temps[i])
            if self.current_hums[i] not in ("NaN", "", None):
                hum_disp = "{0} %".format(self.current_hums[i])
            self.update_channel_tree_row(i, temp_disp, hum_disp)

        self.ax.set_title(self.get_session_name(session_id), loc="left")
        self._auto_view = True
        self._schedule_redraw()

    def export_selected_session(self):
        session_id = self._get_selected_session_id()
        if session_id is None:
            messagebox.showwarning("Sessions", "Select a session first.")
            return
        self.export_session_csv_by_id(session_id, show_dialog=True)

    def rename_selected_session(self, session_id=None):
        if session_id is None:
            session_id = self._get_selected_session_id()
        if session_id is None:
            messagebox.showwarning("Sessions", "Select a session first.")
            return
        current_name = self.get_session_name(session_id)
        new_name = simpledialog.askstring(
            "Session Name",
            "Enter session name (leave empty to clear):",
            initialvalue=current_name,
            parent=self.root
        )
        if new_name is None:
            return
        new_name = new_name.strip()
        try:
            self.db_conn.execute("UPDATE sessions SET name = ? WHERE id = ?", (new_name, session_id))
            self.db_conn.commit()
            self.refresh_sessions_list()
            self.sessions_tree.selection_set(str(session_id))
            self.sessions_tree.focus(str(session_id))
            self.on_session_select(None)
            self.append_console(">>> Session #{0} name set to: {1}".format(
                session_id, new_name if new_name else "(empty)"
            ))
        except Exception as ex:
            self.append_console("Session rename error: {0}".format(ex))
            messagebox.showerror("Session Name", str(ex))

    def on_sessions_tree_double_click(self, event):
        row_id = self.sessions_tree.identify_row(event.y)
        col = self.sessions_tree.identify_column(event.x)
        if not row_id or col != "#2":  # Name column
            return
        try:
            session_id = int(row_id)
        except ValueError:
            return
        self.rename_selected_session(session_id=session_id)
        return "break"

    def delete_selected_session(self):
        session_id = self._get_selected_session_id()
        if session_id is None:
            messagebox.showwarning("Sessions", "Select a session first.")
            return
        if session_id == self.db_session_id:
            messagebox.showwarning("Sessions", "Cannot delete the currently active session.")
            return
        if not messagebox.askyesno(
            "Delete Session",
            "Delete session #{0} and all its data?".format(session_id)
        ):
            return
        try:
            self.db_conn.execute("DELETE FROM markers WHERE session_id = ?", (session_id,))
            self.db_conn.execute("DELETE FROM readings WHERE session_id = ?", (session_id,))
            self.db_conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self.db_conn.commit()
            if self.last_session_id == session_id:
                self.last_session_id = None
            if self.loaded_session_id == session_id:
                self.loaded_session_id = None
            self.append_console(">>> Deleted session #{0}".format(session_id))
            self.refresh_sessions_list()
        except Exception as ex:
            self.append_console("Session delete error: {0}".format(ex))
            messagebox.showerror("Delete Session", str(ex))

    def _data_xlim(self):
        # Each series_times deque stores datetimes in insertion order.
        lo = None
        hi = None
        for dq in self.series_times:
            if dq:
                dq_lo = mdates.date2num(dq[0])
                dq_hi = mdates.date2num(dq[-1])
                lo = dq_lo if lo is None else min(lo, dq_lo)
                hi = dq_hi if hi is None else max(hi, dq_hi)
        if lo is None or hi is None or hi <= lo:
            return None
        return (lo, hi)

    def _on_xlim_changed(self, ax):
        if self._ignore_xlim_changes > 0:
            self._ignore_xlim_changes -= 1
            self._update_scrollbar()
            return
        if not self._in_redraw:
            self._auto_view = False
        self._update_scrollbar()

    def _update_scrollbar(self):
        lims = self._data_xlim()
        if lims is None:
            self.h_scroll.set(0, 1)
            return
        data_lo, data_hi = lims
        span = data_hi - data_lo
        view_lo, view_hi = self.ax.get_xlim()
        sb_lo = max(0.0, min(1.0, (view_lo - data_lo) / span))
        sb_hi = max(0.0, min(1.0, (view_hi - data_lo) / span))
        self.h_scroll.set(sb_lo, sb_hi)

    def _on_xscroll(self, action, *args):
        lims = self._data_xlim()
        if lims is None:
            return
        data_lo, data_hi = lims
        span = data_hi - data_lo
        view_lo, view_hi = self.ax.get_xlim()
        view_size = view_hi - view_lo

        if action == "moveto":
            frac = float(args[0])
            new_lo = data_lo + frac * span
            new_hi = new_lo + view_size
            if new_hi > data_hi:
                new_hi = data_hi
                new_lo = max(data_lo, new_hi - view_size)
        elif action == "scroll":
            n = int(args[0])
            step = view_size * (0.9 if args[1] == "pages" else 0.1)
            new_lo = max(data_lo, min(data_hi - view_size, view_lo + n * step))
            new_hi = new_lo + view_size
        else:
            return

        self.ax.set_xlim(new_lo, new_hi)
        self.canvas.draw_idle()

    def _zoom_axis(self, lo, hi, focus, scale):
        if hi <= lo:
            return lo, hi
        if focus is None or focus < lo or focus > hi:
            focus = (lo + hi) / 2.0
        new_lo = focus - (focus - lo) * scale
        new_hi = focus + (hi - focus) * scale
        if new_hi <= new_lo:
            mid = (lo + hi) / 2.0
            half = max((hi - lo) * 0.5 * scale, 1e-9)
            return mid - half, mid + half
        return new_lo, new_hi

    def _apply_zoom(self, scale, x_focus=None, y_focus=None):
        x_lo, x_hi = self.ax.get_xlim()
        y_lo, y_hi = self.ax.get_ylim()
        new_x_lo, new_x_hi = self._zoom_axis(x_lo, x_hi, x_focus, scale)
        new_y_lo, new_y_hi = self._zoom_axis(y_lo, y_hi, y_focus, scale)
        self.ax.set_xlim(new_x_lo, new_x_hi)
        self.ax.set_ylim(new_y_lo, new_y_hi)
        self.canvas.draw_idle()

    def _zoom_in(self, x_focus=None, y_focus=None):
        self._apply_zoom(self.ZOOM_FACTOR, x_focus=x_focus, y_focus=y_focus)

    def _zoom_out(self, x_focus=None, y_focus=None):
        self._apply_zoom(1.0 / self.ZOOM_FACTOR, x_focus=x_focus, y_focus=y_focus)

    def _reset_view(self):
        self._auto_view = True
        self._ignore_xlim_changes += 1
        self._in_redraw = True
        self.ax.autoscale()
        self._in_redraw = False
        self._update_scrollbar()
        self.canvas.draw_idle()

    def on_tab_changed(self, _event):
        if self.notebook.select() == str(self.tab_graph):
            self._auto_view = True
            self._schedule_redraw()
        elif hasattr(self, "tab_sessions") and self.notebook.select() == str(self.tab_sessions):
            self.refresh_sessions_list()

    def apply_grid_settings(self):
        if not hasattr(self, "ax"):
            return
        self.ax.grid(True, which="major", color="gainsboro", linewidth=0.8)
        if self.minor_grid_enabled:
            self.ax.yaxis.set_minor_locator(MultipleLocator(1.0))
            self.ax.xaxis.set_minor_locator(AutoMinorLocator(5))
            self.ax.grid(True, which="minor", color="#ededed", linestyle=":", linewidth=0.6)
        else:
            self.ax.yaxis.set_minor_locator(NullLocator())
            self.ax.xaxis.set_minor_locator(NullLocator())
            self.ax.grid(False, which="minor")

    def on_minor_grid_toggle(self):
        self.minor_grid_enabled = bool(self.minor_grid_var.get())
        self.apply_grid_settings()
        if hasattr(self, "canvas"):
            self.canvas.draw_idle()
        self.save_config()

    def on_terminal_visibility_toggle(self):
        if bool(self.terminal_visible_var.get()):
            self.terminal_mode = "floating" if bool(self.terminal_floating_var.get()) else "docked"
        else:
            self.terminal_mode = "hidden"
        self.apply_terminal_mode(persist=True)

    def on_terminal_floating_toggle(self):
        if bool(self.terminal_floating_var.get()):
            self.terminal_visible_var.set(True)
            self.terminal_mode = "floating"
        else:
            self.terminal_mode = "docked" if bool(self.terminal_visible_var.get()) else "hidden"
        self.apply_terminal_mode(persist=True)

    def open_com_settings(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("COM Settings")
        dialog.geometry("360x210")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        ports = [p.device for p in serial.tools.list_ports.comports()]

        tk.Label(dialog, text="Arduino port:", font=("Segoe UI", 10)).grid(row=0, column=0, sticky="w", padx=12, pady=(14, 8))
        arduino_var = tk.StringVar(value=self.cmb_arduino_port.get().strip())
        arduino_combo = ttk.Combobox(dialog, textvariable=arduino_var, values=ports, state="readonly", width=18)
        arduino_combo.grid(row=0, column=1, sticky="w", padx=(0, 12), pady=(14, 8))

        tk.Label(dialog, text="ESP port:", font=("Segoe UI", 10)).grid(row=1, column=0, sticky="w", padx=12, pady=8)
        esp_var = tk.StringVar(value=self.cmb_esp_port.get().strip())
        esp_combo = ttk.Combobox(dialog, textvariable=esp_var, values=ports, state="readonly", width=18)
        esp_combo.grid(row=1, column=1, sticky="w", padx=(0, 12), pady=8)

        tk.Label(dialog, text="Arduino baud: {0}".format(self.ARDUINO_BAUD_RATE), fg="#555555").grid(
            row=2, column=0, columnspan=2, sticky="w", padx=12, pady=(8, 2)
        )
        tk.Label(dialog, text="ESP baud: {0}".format(self.ESP_BAUD_RATE), fg="#555555").grid(
            row=3, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 12)
        )

        def refresh_dialog_ports():
            current_ports = [p.device for p in serial.tools.list_ports.comports()]
            arduino_combo["values"] = current_ports
            esp_combo["values"] = current_ports

        def apply_and_close():
            if arduino_var.get().strip():
                self.cmb_arduino_port.set(arduino_var.get().strip())
            if esp_var.get().strip():
                self.cmb_esp_port.set(esp_var.get().strip())
            self.save_config()
            dialog.destroy()

        btn_row = tk.Frame(dialog)
        btn_row.grid(row=4, column=0, columnspan=2, pady=(4, 0))
        tk.Button(btn_row, text="Refresh", command=refresh_dialog_ports, padx=14).pack(side=tk.LEFT, padx=6)
        tk.Button(btn_row, text="Apply", command=apply_and_close, bg="#4a90d9", fg="white", padx=18).pack(side=tk.LEFT, padx=6)
        tk.Button(btn_row, text="Close", command=dialog.destroy, padx=18).pack(side=tk.LEFT, padx=6)

        dialog.wait_window()

    # â”€â”€ Channel rename / color â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    # â”€â”€ Calibration â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _normalize_calibration_points(self, points):
        by_raw = {}
        for pair in points or []:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                continue
            try:
                raw_v = float(pair[0])
                ref_v = float(pair[1])
            except (TypeError, ValueError):
                continue
            by_raw[raw_v] = ref_v
        return sorted(by_raw.items(), key=lambda x: x[0])

    def _format_number(self, value):
        text = "{0:.3f}".format(float(value)).rstrip("0").rstrip(".")
        return text if text else "0"

    def _piecewise_linear_correct(self, raw_value, points):
        pts = self._normalize_calibration_points(points)
        if not pts:
            return raw_value
        if len(pts) == 1:
            raw0, ref0 = pts[0]
            return raw_value + (ref0 - raw0)

        if raw_value <= pts[0][0]:
            x1, y1 = pts[0]
            x2, y2 = pts[1]
        elif raw_value >= pts[-1][0]:
            x1, y1 = pts[-2]
            x2, y2 = pts[-1]
        else:
            x1, y1, x2, y2 = pts[0][0], pts[0][1], pts[1][0], pts[1][1]
            for i in range(1, len(pts)):
                left = pts[i - 1]
                right = pts[i]
                if left[0] <= raw_value <= right[0]:
                    x1, y1 = left
                    x2, y2 = right
                    break

        if x2 == x1:
            return y2
        return y1 + (raw_value - x1) * (y2 - y1) / (x2 - x1)

    def apply_calibration(self, kind, ch_idx, raw_value):
        if kind == "temp":
            points = self.temp_calibration_points[ch_idx]
        else:
            points = self.hum_calibration_points[ch_idx]
        return self._piecewise_linear_correct(raw_value, points)

    def open_calibration_manager(self, kind):
        if kind not in ("temp", "hum"):
            return

        is_temp = kind == "temp"
        dialog = tk.Toplevel(self.root)
        dialog.title("Temperature Calibration" if is_temp else "Humidity Calibration")
        dialog.geometry("560x460")
        dialog.minsize(560, 460)
        dialog.transient(self.root)
        dialog.grab_set()

        source = self.temp_calibration_points if is_temp else self.hum_calibration_points
        working = [self._normalize_calibration_points(source[i]) for i in range(self.CHANNEL_COUNT)]

        tk.Label(
            dialog,
            text=(
                "Define calibration points as Raw -> Reference. "
                "Two or more points enable non-linear piecewise correction."
            ),
            anchor="w",
            justify="left",
            wraplength=520
        ).pack(fill=tk.X, padx=12, pady=(12, 8))

        top = tk.Frame(dialog)
        top.pack(fill=tk.X, padx=12, pady=(0, 8))
        tk.Label(top, text="Channel:").pack(side=tk.LEFT)

        visible_indices = self.visible_channel_indices()
        channel_combo = ttk.Combobox(
            top,
            state="readonly",
            width=36,
            values=["{0} - {1}".format(self.channel_display_id(i), self.channel_names[i]) for i in visible_indices]
        )
        channel_combo.current(0)
        channel_combo.pack(side=tk.LEFT, padx=(8, 0))

        tree = ttk.Treeview(dialog, columns=("raw", "ref"), show="headings", height=12)
        tree.heading("raw", text="Raw")
        tree.heading("ref", text="Reference")
        tree.column("raw", width=220, anchor="center")
        tree.column("ref", width=220, anchor="center")
        tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 10))

        edit = tk.Frame(dialog)
        edit.pack(fill=tk.X, padx=12)

        tk.Label(edit, text="Raw:").grid(row=0, column=0, sticky="w")
        raw_var = tk.StringVar()
        raw_entry = tk.Entry(edit, textvariable=raw_var, width=14)
        raw_entry.grid(row=0, column=1, sticky="w", padx=(6, 16))

        tk.Label(edit, text="Reference:").grid(row=0, column=2, sticky="w")
        ref_var = tk.StringVar()
        ref_entry = tk.Entry(edit, textvariable=ref_var, width=14)
        ref_entry.grid(row=0, column=3, sticky="w", padx=(6, 16))

        tk.Label(edit, text=("units: Â°C" if is_temp else "units: %RH"), fg="#555555").grid(
            row=0, column=4, sticky="w"
        )

        def current_ch():
            idx = channel_combo.current()
            if idx < 0:
                return visible_indices[0]
            return visible_indices[idx]

        def refresh_tree():
            for iid in tree.get_children():
                tree.delete(iid)
            for idx, (raw_v, ref_v) in enumerate(working[current_ch()]):
                tree.insert(
                    "",
                    tk.END,
                    iid=str(idx),
                    values=(self._format_number(raw_v), self._format_number(ref_v))
                )

        def on_channel_change(_event=None):
            raw_var.set("")
            ref_var.set("")
            refresh_tree()

        def on_tree_select(_event=None):
            sel = tree.selection()
            if not sel:
                return
            try:
                idx = int(sel[0])
            except ValueError:
                return
            pts = working[current_ch()]
            if 0 <= idx < len(pts):
                raw_var.set(self._format_number(pts[idx][0]))
                ref_var.set(self._format_number(pts[idx][1]))

        def add_or_update():
            try:
                raw_v = float(raw_var.get().strip())
                ref_v = float(ref_var.get().strip())
            except ValueError:
                messagebox.showwarning("Calibration", "Raw and Reference must be numeric.", parent=dialog)
                return
            ch = current_ch()
            mp = {p[0]: p[1] for p in working[ch]}
            mp[raw_v] = ref_v
            working[ch] = sorted(mp.items(), key=lambda x: x[0])
            refresh_tree()

        def remove_selected():
            sel = tree.selection()
            if not sel:
                return
            try:
                idx = int(sel[0])
            except ValueError:
                return
            ch = current_ch()
            if 0 <= idx < len(working[ch]):
                working[ch].pop(idx)
                refresh_tree()

        def clear_channel():
            ch = current_ch()
            if not working[ch]:
                return
            if not messagebox.askyesno("Calibration", "Clear all calibration points for this channel?", parent=dialog):
                return
            working[ch] = []
            refresh_tree()

        btns = tk.Frame(dialog)
        btns.pack(fill=tk.X, padx=12, pady=(10, 0))
        tk.Button(btns, text="Add / Update Point", command=add_or_update).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(btns, text="Remove Selected", command=remove_selected).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(btns, text="Clear Channel", command=clear_channel).pack(side=tk.LEFT)

        bottom = tk.Frame(dialog)
        bottom.pack(fill=tk.X, padx=12, pady=12)

        def apply_and_close():
            for i in range(self.CHANNEL_COUNT):
                normalized = self._normalize_calibration_points(working[i])
                if is_temp:
                    self.temp_calibration_points[i] = normalized
                else:
                    self.hum_calibration_points[i] = normalized
            self.save_config()
            dialog.destroy()

        tk.Button(bottom, text="Apply", command=apply_and_close,
                  bg="#4a90d9", fg="white", padx=20).pack(side=tk.RIGHT)
        tk.Button(bottom, text="Cancel", command=dialog.destroy, padx=20).pack(side=tk.RIGHT, padx=(0, 8))

        channel_combo.bind("<<ComboboxSelected>>", on_channel_change)
        tree.bind("<<TreeviewSelect>>", on_tree_select)
        raw_entry.bind("<Return>", lambda _e: add_or_update())
        ref_entry.bind("<Return>", lambda _e: add_or_update())

        refresh_tree()
        raw_entry.focus_set()
        dialog.wait_window()

    def channel_record_cell(self, ch_idx):
        return "[x]" if self.channel_record_enabled[ch_idx] else "[ ]"

    def update_channel_tree_row(self, ch_idx, temp_display=None, hum_display=None):
        row_id = "ch{0}".format(ch_idx)
        if not self.tree.exists(row_id):
            if not self.channel_is_visible_in_ui(ch_idx):
                return
            self.rebuild_channel_tree()
            if not self.tree.exists(row_id):
                return
        cur = self.tree.item(row_id, "values")
        if temp_display is None:
            temp_display = cur[3] if len(cur) > 3 else "-"
        if hum_display is None:
            hum_display = cur[4] if len(cur) > 4 else "-"
        self.tree.item(
            row_id,
            values=(
                self.channel_record_cell(ch_idx),
                self.channel_display_id(ch_idx),
                self.channel_tree_name(ch_idx),
                temp_display,
                hum_display
            )
        )

    def set_channel_recording(self, ch_idx, enabled):
        self.channel_record_enabled[ch_idx] = bool(enabled)
        self.lines[ch_idx].set_visible(self.channel_record_enabled[ch_idx] and self.channel_is_visible_in_ui(ch_idx))
        self.update_channel_tree_row(ch_idx)
        self.refresh_legend()
        self._schedule_redraw()

    def on_tree_click(self, event):
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        col = self.tree.identify_column(event.x)
        if col != "#1":  # rec column
            return
        try:
            ch_idx = int(row_id.replace("ch", ""))
        except ValueError:
            return
        self.set_channel_recording(ch_idx, not self.channel_record_enabled[ch_idx])
        return "break"

    def on_tree_double_click(self, event):
        col = self.tree.identify_column(event.x)
        if col == "#1":
            return "break"
        item = self.tree.identify_row(event.y)
        if not item:
            return
        try:
            ch_idx = int(item.replace("ch", ""))
        except ValueError:
            return
        self.open_channel_editor(ch_idx)

    def channel_tree_name(self, ch_idx):
        if self.channel_record_enabled[ch_idx]:
            return self.channel_names[ch_idx]
        return "{0} [REC OFF]".format(self.channel_names[ch_idx])

    def refresh_legend(self):
        visible_lines = [
            line for i, line in enumerate(self.lines)
            if self.channel_record_enabled[i] and self.channel_is_visible_in_ui(i)
        ]
        if visible_lines:
            labels = [line.get_label() for line in visible_lines]
            self.ax.legend(visible_lines, labels, loc="upper left")
        else:
            legend = self.ax.get_legend()
            if legend is not None:
                legend.remove()

    def open_channel_editor(self, ch_idx):
        dialog = tk.Toplevel(self.root)
        dialog.title("Edit Channel {0}".format(ch_idx))
        dialog.geometry("330x210")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        pad = {"padx": 12, "pady": 8}
        tk.Label(dialog, text="Name:", font=("Segoe UI", 10)).grid(row=0, column=0, sticky="w", **pad)
        name_var = tk.StringVar(value=self.channel_names[ch_idx])
        name_entry = tk.Entry(dialog, textvariable=name_var, width=22, font=("Segoe UI", 10))
        name_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(0, 12), pady=8)
        name_entry.select_range(0, tk.END)
        name_entry.focus()

        color_var = tk.StringVar(value=self.channel_colors[ch_idx])
        color_preview = tk.Label(dialog, bg=self.channel_colors[ch_idx], width=3, relief="solid")

        def pick_color():
            result = colorchooser.askcolor(color=color_var.get(), parent=dialog, title="Choose color")
            if result and result[1]:
                color_var.set(result[1])
                color_preview.config(bg=result[1])

        tk.Label(dialog, text="Color:", font=("Segoe UI", 10)).grid(row=1, column=0, sticky="w", **pad)
        color_preview.grid(row=1, column=1, sticky="w", padx=(0, 6), pady=8)
        tk.Button(dialog, text="Choose...", command=pick_color).grid(row=1, column=2, sticky="w", padx=(0, 12), pady=8)

        record_var = tk.BooleanVar(value=self.channel_record_enabled[ch_idx])
        tk.Checkbutton(
            dialog,
            text="Enable recording for this channel",
            variable=record_var,
            onvalue=True,
            offvalue=False,
            font=("Segoe UI", 10)
        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=12, pady=(2, 8))

        def apply():
            new_name = name_var.get().strip() or self.default_channel_name(ch_idx)
            new_color = color_var.get()
            self.channel_names[ch_idx] = new_name
            self.channel_colors[ch_idx] = new_color
            self.set_channel_recording(ch_idx, bool(record_var.get()))
            tag = "ch_color_{0}".format(ch_idx)
            self.tree.tag_configure(tag, foreground=new_color)
            self.update_channel_tree_row(ch_idx)
            self.lines[ch_idx].set_label(new_name)
            self.lines[ch_idx].set_color(new_color)
            self.refresh_legend()
            self.canvas.draw_idle()
            dialog.destroy()

        btn_row = tk.Frame(dialog)
        btn_row.grid(row=3, column=0, columnspan=3, pady=(4, 12))
        tk.Button(btn_row, text="Apply", command=apply, bg="#4a90d9", fg="white",
                  font=("Segoe UI", 10), padx=18).pack(side=tk.LEFT, padx=8)
        tk.Button(btn_row, text="Cancel", command=dialog.destroy,
                  font=("Segoe UI", 10), padx=18).pack(side=tk.LEFT)
        dialog.bind("<Return>", lambda e: apply())
        dialog.wait_window()

    # â”€â”€ Markers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    # â”€â”€ Mouse drag-pan â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    _DRAG_THRESHOLD = 4   # pixels of movement before drag activates

    def _on_mouse_press(self, event):
        """Handle both drag-start (left-click) and double-click (marker actions)."""
        if event.inaxes != self.ax:
            return

        # â”€â”€ Double-click: add / edit marker â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if event.dblclick:
            if self.toolbar.mode != "" or event.xdata is None:
                return
            PIXEL_HIT = 9
            for idx, marker in enumerate(self.markers):
                try:
                    mx = self.ax.transData.transform(
                        (mdates.date2num(marker["datetime"]), 0))[0]
                    if abs(event.x - mx) <= PIXEL_HIT:
                        self.edit_marker(idx)
                        return
                except Exception:
                    continue
            clicked_dt = mdates.num2date(event.xdata).replace(tzinfo=None)
            self._prompt_and_place_marker(clicked_dt)
            return

        # â”€â”€ Single left-click: begin drag tracking â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if event.button == 1 and self.toolbar.mode == "" and event.xdata is not None:
            self._drag_press_x = event.x
            self._drag_press_y = event.y
            self._drag_xlim = self.ax.get_xlim()
            self._drag_ylim = self.ax.get_ylim()
            # Snapshot the inverse transform at this moment so the mapping
            # stays consistent even as we shift xlim/ylim during the drag.
            self._drag_inv_tf = self.ax.transData.inverted()
            self._is_dragging = False
            self.canvas.get_tk_widget().config(cursor="fleur")

    def _on_mouse_drag(self, event):
        """Pan the axes while the left button is held and moved."""
        if self._drag_press_x is None:
            return
        if self.toolbar.mode != "":
            self._cancel_drag()
            return

        dx_px = event.x - self._drag_press_x
        dy_px = event.y - self._drag_press_y

        # Only engage after moving past threshold (prevents micro-drags on click)
        if (not self._is_dragging and
                abs(dx_px) < self._DRAG_THRESHOLD and
                abs(dy_px) < self._DRAG_THRESHOLD):
            return

        self._is_dragging = True

        # Convert the press-pixel and current-pixel to data coords using the
        # ORIGINAL (snapshot) transform, then shift limits by the delta.
        press_data = self._drag_inv_tf.transform((self._drag_press_x, self._drag_press_y))
        curr_data  = self._drag_inv_tf.transform((event.x,            event.y))

        dx = curr_data[0] - press_data[0]
        dy = curr_data[1] - press_data[1]

        self.ax.set_xlim(self._drag_xlim[0] - dx, self._drag_xlim[1] - dx)
        self.ax.set_ylim(self._drag_ylim[0] - dy, self._drag_ylim[1] - dy)
        self.canvas.draw_idle()

    def _on_mouse_release(self, event):
        """End drag."""
        if self._drag_press_x is not None:
            self.canvas.get_tk_widget().config(cursor="")
        self._cancel_drag()

    def _on_mouse_wheel(self, event):
        """Zoom with mouse wheel in the graph area."""
        if event.inaxes != self.ax:
            return
        if self.toolbar.mode != "":
            return
        direction = getattr(event, "button", "")
        step = getattr(event, "step", 0)
        if direction == "up" or step > 0:
            self._zoom_in(x_focus=event.xdata, y_focus=event.ydata)
        elif direction == "down" or step < 0:
            self._zoom_out(x_focus=event.xdata, y_focus=event.ydata)

    def _cancel_drag(self):
        self._drag_press_x = None
        self._drag_press_y = None
        self._drag_xlim = None
        self._drag_ylim = None
        self._drag_inv_tf = None
        self._is_dragging = False

    def _prompt_and_place_marker(self, dt):
        note = simpledialog.askstring(
            "Add Marker",
            "Note for {0}:".format(dt.strftime("%Y-%m-%d %H:%M:%S")),
            parent=self.root
        )
        if note is None:
            return
        note = note.strip() or "(no note)"
        self._place_marker(dt, note, save_to_db=True)

    def _place_marker(self, dt, note, save_to_db=True):
        vline = self.ax.axvline(x=dt, color="crimson", linestyle="--",
                                linewidth=1.4, alpha=0.85, zorder=5)
        annotation = self.ax.annotate(
            note,
            xy=(dt, 1.0),
            xycoords=("data", "axes fraction"),
            xytext=(3, -4), textcoords="offset points",
            fontsize=7, color="crimson",
            rotation=90, va="top", ha="left",
            clip_on=False, zorder=6
        )
        self.markers.append({"datetime": dt, "note": note, "vline": vline, "annotation": annotation})
        self._rebuild_listbox()
        if save_to_db:
            self._save_marker_to_db("ADD", dt, note)
        self.canvas.draw_idle()

    def edit_marker(self, idx):
        marker = self.markers[idx]
        new_note = simpledialog.askstring(
            "Edit Marker",
            "Edit note for {0}:".format(marker["datetime"].strftime("%Y-%m-%d %H:%M:%S")),
            initialvalue=marker["note"],
            parent=self.root
        )
        if new_note is None:
            return
        new_note = new_note.strip() or "(no note)"
        marker["note"] = new_note
        marker["annotation"].set_text(new_note)
        self._rebuild_listbox()
        self._save_marker_to_db("EDIT", marker["datetime"], new_note)
        self.canvas.draw_idle()

    def on_listbox_double_click(self, event):
        sel = self.markers_listbox.curselection()
        if not sel:
            return
        marker_idx = sel[0] // 3
        if marker_idx < len(self.markers):
            self.edit_marker(marker_idx)

    def delete_marker(self):
        sel = self.markers_listbox.curselection()
        if not sel:
            return
        marker_idx = sel[0] // 3
        if marker_idx >= len(self.markers):
            return
        marker = self.markers[marker_idx]
        try:
            marker["vline"].remove()
        except Exception:
            pass
        try:
            marker["annotation"].remove()
        except Exception:
            pass
        self._save_marker_to_db("DEL", marker["datetime"], marker["note"])
        self.markers.pop(marker_idx)
        self._rebuild_listbox()
        self.canvas.draw_idle()

    def _rebuild_listbox(self):
        self.markers_listbox.delete(0, tk.END)
        for marker in self.markers:
            ts = marker["datetime"].strftime("%Y-%m-%d %H:%M:%S")
            self.markers_listbox.insert(tk.END, u"\u25cf {0}".format(ts))
            self.markers_listbox.insert(tk.END, "  {0}".format(marker["note"]))
            self.markers_listbox.insert(tk.END, "")
        if self.markers:
            self.markers_listbox.see(tk.END)

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
            self.ax.set_title("", loc="left")
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
            self.btn_connect_esp.config(text="Stop ESP")
            self.append_console(">>> ESP controller connected on {0} (session #{1})".format(port_name, self.db_session_id))

        self.update_status_label()
        self.update_sessions_controls()
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

    def apply_esp_interval(self, interval_ms, log_to_console=True):
        if not self.source_connected["esp"]:
            return False
        if self.send_esp_command("SETINT ALL {0}".format(interval_ms)):
            if log_to_console:
                self.append_console(">>> [ESP] SETINT ALL {0}".format(interval_ms))
            return True
        return False

    def on_interval_changed(self, event=None):
        interval_text = self.txt_interval.get().strip()
        interval_ms = self.parse_interval_ms(interval_text)
        if interval_ms is None:
            self.append_console("Interval ignored: use formats like 500ms, 1s, 2min, 1h")
            return
        if self.source_connected["arduino"] and self.arduino_polling_started:
            self.schedule_arduino_poll(interval_ms)
            self.append_console(">>> [ARD] interval set to {0} ms".format(interval_ms))
        if self.source_connected["esp"]:
            self.apply_esp_interval(interval_ms, log_to_console=True)

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
            "TIME SET {0}".format(int(time.time())),
            "SETINT ALL {0}".format(interval_ms),
            "NODES",
        ):
            try:
                ser.write((cmd + "\n").encode("utf-8"))
            except Exception as ex:
                self.append_console("ESP init error: {0}".format(ex))
                break
        self.append_console(
            ">>> [ESP] init attempt {0}: STREAM ON / TIME SET / SETINT ALL {1} / NODES".format(
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
        if source_kind == "arduino" and not self.arduino_polling_started and self.HANDSHAKE_MARKER in self.receive_buffers[source_kind]:
            interval_ms = self.current_interval_ms()
            self.arduino_polling_started = True
            self.append_console(">>> [ARD] Polling started")
            self.update_status_label()
            self.schedule_arduino_poll(interval_ms)
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
        matches = list(self.PACKET_REGEX.finditer(line))
        if not matches:
            return
        now = datetime.now()
        got_any = False
        for match in matches:
            channel_idx = int(match.group(1))
            if not (0 <= channel_idx < self.ARDUINO_CHANNEL_COUNT):
                continue
            try:
                temp_raw = float(match.group(2))
                hum_raw = float(match.group(3))
            except ValueError:
                continue
            temp_cal = self.apply_calibration("temp", channel_idx, temp_raw)
            hum_cal = self.apply_calibration("hum", channel_idx, hum_raw)
            temp_text = self._format_number(temp_cal)
            hum_text = self._format_number(hum_cal)
            self.current_temps[channel_idx] = temp_text
            self.current_hums[channel_idx] = hum_text
            self.update_channel_tree_row(channel_idx, "{0} \u00b0C".format(temp_text), "{0} %".format(hum_text))
            self.add_smoothed_point(channel_idx, now, temp_cal)
            got_any = True
        if got_any:
            self.save_to_db(now)
            self._schedule_redraw()

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
            self.lines[slot_idx].set_label(self.channel_names[slot_idx])
            self.refresh_legend()
        self.update_channel_tree_row(slot_idx)

    def process_esp_packet_line(self, line):
        json_start = line.find("{")
        if json_start < 0:
            return
        try:
            event = json.loads(line[json_start:])
        except Exception:
            return
        event_name = str(event.get("event", "") or "")
        if event_name == "controller_ready":
            if not self.esp_stream_confirmed:
                self.schedule_esp_init(200)
            return
        if event_name == "reading":
            self.esp_stream_confirmed = True
            if self.esp_init_job is not None:
                try:
                    self.root.after_cancel(self.esp_init_job)
                except Exception:
                    pass
                self.esp_init_job = None
            try:
                node_id = int(event.get("node_id"))
                temp_raw = float(event.get("temperature_c"))
                hum_raw = float(event.get("humidity_pct"))
            except Exception:
                return
            slot_idx = self.allocate_esp_slot(node_id)
            if slot_idx is None:
                return
            self.update_esp_slot_metadata(slot_idx, node_id, str(event.get("name", "") or "satellite"))
            temp_cal = self.apply_calibration("temp", slot_idx, temp_raw)
            hum_cal = self.apply_calibration("hum", slot_idx, hum_raw)
            temp_text = self._format_number(temp_cal)
            hum_text = self._format_number(hum_cal)
            self.current_temps[slot_idx] = temp_text
            self.current_hums[slot_idx] = hum_text
            self.update_channel_tree_row(slot_idx, "{0} \u00b0C".format(temp_text), "{0} %".format(hum_text))
            now = self.parse_esp_timestamp(event)
            self.add_smoothed_point(slot_idx, now, temp_cal)
            self.save_to_db(now)
            self._schedule_redraw()
        elif event_name == "nodes":
            self.esp_stream_confirmed = True
            if self.esp_init_job is not None:
                try:
                    self.root.after_cancel(self.esp_init_job)
                except Exception:
                    pass
                self.esp_init_job = None
            for item in event.get("items", []):
                try:
                    node_id = int(item.get("node_id"))
                except Exception:
                    continue
                slot_idx = self.allocate_esp_slot(node_id)
                if slot_idx is None:
                    continue
                self.update_esp_slot_metadata(slot_idx, node_id, str(item.get("name", "") or "satellite"))

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
                ser.write(b"Read\n")
            except Exception:
                pass
            interval_ms = self.current_interval_ms()
            self.schedule_arduino_poll(interval_ms)

    def add_smoothed_point(self, channel_index, timestamp, raw_value):
        history = self.temp_history[channel_index]
        history.append(raw_value)
        smoothed = sum(history) / float(len(history))

        # Keep at most one plotted point per second per channel so 1 week is bounded.
        ts_sec = int(timestamp.timestamp())
        ts_plot = timestamp.replace(microsecond=0)

        if self.last_plot_second[channel_index] == ts_sec and self.series_times[channel_index]:
            self.series_times[channel_index][-1] = ts_plot
            self.series_values[channel_index][-1] = smoothed
        else:
            self.series_times[channel_index].append(ts_plot)
            self.series_values[channel_index].append(smoothed)
            self.last_plot_second[channel_index] = ts_sec

    def _schedule_redraw(self):
        """Request a graph redraw on the next Tk idle cycle."""
        if not self._redraw_pending:
            self._redraw_pending = True
            self.root.after_idle(self._do_redraw)

    def _do_redraw(self):
        self._redraw_pending = False
        self.redraw_graph()

    def redraw_graph(self):
        any_points = False
        visible_channels = set(self.visible_channel_indices())
        for i in range(self.CHANNEL_COUNT):
            times = list(self.series_times[i])
            vals  = list(self.series_values[i])
            if len(times) > self.MAX_RENDER_POINTS:
                step = int(math.ceil(len(times) / float(self.MAX_RENDER_POINTS)))
                times = times[::step]
                vals = vals[::step]
            self.lines[i].set_data(times, vals)
            self.lines[i].set_visible(self.channel_record_enabled[i] and i in visible_channels)
            if times and self.channel_record_enabled[i] and i in visible_channels:
                any_points = True

        if any_points and self._auto_view:
            self._ignore_xlim_changes += 1
            self._in_redraw = True
            self.ax.relim()
            self.ax.autoscale_view()
            self._in_redraw = False
            self._update_scrollbar()

        self.canvas.draw_idle()

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
        if source_kind == "esp" and self.esp_init_job is not None:
            try:
                self.root.after_cancel(self.esp_init_job)
            except Exception:
                pass
            self.esp_init_job = None
            self.esp_init_attempts_remaining = 0
            self.esp_stream_confirmed = False

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

    _CONSOLE_MAX_LINES = 500

    def append_console(self, text, auto_newline=True):
        if not text:
            return
        self._append_to_console_widget(self.txt_console, text, auto_newline=auto_newline)
        if self.floating_console_text is not None:
            self._append_to_console_widget(self.floating_console_text, text, auto_newline=auto_newline)

    def on_close(self):
        self.save_config()
        self._destroy_floating_terminal_window()
        self.close_connection()
        if self.db_conn:
            self.db_conn.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = ArduinoLoggerApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
