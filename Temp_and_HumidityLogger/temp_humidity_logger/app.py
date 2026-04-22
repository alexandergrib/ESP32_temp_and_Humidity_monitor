import ctypes
import math
import os
import queue
import sys
import threading
from collections import deque
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.dates import AutoDateLocator, ConciseDateFormatter
from matplotlib.figure import Figure

from .calibration import CalibrationMixin
from .channels import ChannelUiMixin
from .config_store import ConfigMixin
from .database import DatabaseMixin
from .esp_controller import EspControllerMixin
from .graph_interaction import GraphInteractionMixin
from .layout import LayoutMixin
from .serial_io import SerialIoMixin
from .settings_ui import SettingsUiMixin
from .sessions import SessionUiMixin
from .version import APP_DATA_DIR_NAME as PACKAGE_APP_DATA_DIR_NAME
from .version import GITHUB_URL as PACKAGE_GITHUB_URL
from .version import APP_NAME as PACKAGE_APP_NAME
from .version import APP_VERSION as PACKAGE_APP_VERSION


class ArduinoLoggerApp(
    ConfigMixin, DatabaseMixin, EspControllerMixin, CalibrationMixin, ChannelUiMixin,
    SessionUiMixin, GraphInteractionMixin, LayoutMixin, SettingsUiMixin, SerialIoMixin,
):
    APP_NAME = PACKAGE_APP_NAME
    APP_VERSION = PACKAGE_APP_VERSION
    GITHUB_URL = PACKAGE_GITHUB_URL
    ARDUINO_CHANNEL_COUNT = 6
    ESP_CHANNEL_COUNT = 8
    CHANNEL_COUNT = ARDUINO_CHANNEL_COUNT + ESP_CHANNEL_COUNT
    DEFAULT_INTERVAL_TEXT = "1s"
    SMOOTHING_WINDOW = 5
    SATELLITE_SMOOTHING_SECONDS = 120
    ARDUINO_BAUD_RATE = 9600
    ESP_BAUD_RATE = 460800
    DB_FILE_NAME = "logger.db"
    TEMP_DATA_FILE_NAME = "data_temperature.csv"
    HUM_DATA_FILE_NAME = "data_humidity.csv"
    CONFIG_FILE_NAME = "config.ini"
    SATELLITE_NAME_MAX_LEN = 15
    PLOT_HISTORY_SECONDS = 7 * 24 * 60 * 60
    MAX_RENDER_POINTS = 2500
    DEFAULT_COLORS = [
        "red", "blue", "green", "orange", "purple", "brown", "magenta", "teal",
        "#e74c3c", "#3498db", "#2ecc71", "#f1c40f", "#8e44ad", "#16a085", "#d35400", "#7f8c8d"
    ]
    ZOOM_FACTOR = 0.70
    DEFAULT_GRAPH_SPLIT_RATIO = 0.75
    APP_DATA_DIR_NAME = PACKAGE_APP_DATA_DIR_NAME

    def __init__(self, root):
        self.root = root
        self.root.title(self.app_title())
        self.root.geometry("1024x680")

        if getattr(sys, "frozen", False):
            self.install_dir = os.path.abspath(os.path.dirname(sys.executable))
        else:
            self.install_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
        self.base_dir = self.resolve_runtime_dir(self.install_dir)
        self.resource_dir = getattr(sys, "_MEIPASS", self.install_dir)
        self.config_path = os.path.join(self.base_dir, self.CONFIG_FILE_NAME)
        self.runtime_settings = self.runtime_settings_defaults()
        self.load_runtime_settings()
        self.temp_data_csv_path = os.path.join(self.base_dir, self.TEMP_DATA_FILE_NAME)
        self.hum_data_csv_path = os.path.join(self.base_dir, self.HUM_DATA_FILE_NAME)
        self.temp_data_csv_initialized = False
        self.hum_data_csv_initialized = False
        self.saved_interval_text = self.DEFAULT_INTERVAL_TEXT
        self.saved_arduino_port = ""
        self.saved_esp_port = ""
        self.saved_column_widths = {}
        self.saved_live_split_x = None
        self.saved_graph_split_x = None
        self.saved_graph_split_ratio = self.DEFAULT_GRAPH_SPLIT_RATIO
        self.minor_grid_enabled = True
        self.markers_visible = True
        self.markers_floating = False
        self.terminal_mode = "docked"
        self.terminal_visible = True
        self.terminal_window = None
        self.floating_console_text = None
        self.terminal_command_var = tk.StringVar(value="")
        self.terminal_command_target_var = tk.StringVar(value="ESP")
        self.terminal_command_entry = None
        self.floating_terminal_command_entry = None
        self.markers_window = None
        self.floating_markers_listbox = None
        self.floating_markers_scrollbar = None

        self.serial_ports = {"arduino": None, "esp": None}
        self.source_connected = {"arduino": False, "esp": False}
        self.read_threads = {"arduino": None, "esp": None}
        self.stop_events = {"arduino": threading.Event(), "esp": threading.Event()}
        self.ui_queue = queue.Queue()

        self.receive_buffers = {"arduino": "", "esp": ""}
        self.arduino_polling_started = False
        self.arduino_info = {"board": "", "fw_version": "", "protocol": "", "channel_count": None}
        self.esp_slot_by_node_id = {}
        self.esp_node_state = {}
        self.esp_init_job = None
        self.esp_init_attempts_remaining = 0
        self.esp_stream_confirmed = False
        self.esp_time_synced = False
        self.esp_presence_job = None
        self.last_esp_event_monotonic = 0.0
        self.last_esp_stream_recover_at = 0.0

        self.current_temps = ["NaN"] * self.CHANNEL_COUNT
        self.current_hums = ["NaN"] * self.CHANNEL_COUNT
        self.current_signals = ["-"] * self.CHANNEL_COUNT
        smoothing_history_max = max(self.SMOOTHING_WINDOW, 7200, 32)
        self.temp_history = [deque(maxlen=smoothing_history_max) for _ in range(self.CHANNEL_COUNT)]
        self.hum_history = [deque(maxlen=smoothing_history_max) for _ in range(self.CHANNEL_COUNT)]

        self.series_times = [deque(maxlen=self.PLOT_HISTORY_SECONDS) for _ in range(self.CHANNEL_COUNT)]
        self.temp_series_values = [deque(maxlen=self.PLOT_HISTORY_SECONDS) for _ in range(self.CHANNEL_COUNT)]
        self.hum_series_values = [deque(maxlen=self.PLOT_HISTORY_SECONDS) for _ in range(self.CHANNEL_COUNT)]
        self.last_plot_second = [None] * self.CHANNEL_COUNT
        self._render_cache = self._build_empty_render_cache()

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
        self._drag_graph_kind = None
        self.graph_contexts = {}
        self.active_graph_kind = "temp"

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
        self.schedule_esp_presence_check()

    def app_title(self):
        return "{0} v{1}".format(self.APP_NAME, self.APP_VERSION)

    def default_channel_name(self, ch_idx):
        if ch_idx < self.ARDUINO_CHANNEL_COUNT:
            return "Arduino CH{0}".format(ch_idx)
        return "ESP Slot {0}".format(ch_idx - self.ARDUINO_CHANNEL_COUNT + 1)

    def resolve_runtime_dir(self, install_dir):
        if not getattr(sys, "frozen", False):
            return install_dir
        root_dir = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or install_dir
        runtime_dir = os.path.join(root_dir, self.APP_DATA_DIR_NAME)
        try:
            os.makedirs(runtime_dir, exist_ok=True)
        except Exception:
            return install_dir
        self.migrate_legacy_runtime_files(install_dir, runtime_dir)
        return runtime_dir

    def migrate_legacy_runtime_files(self, install_dir, runtime_dir):
        if not install_dir or not runtime_dir or os.path.normcase(install_dir) == os.path.normcase(runtime_dir):
            return
        legacy_names = [
            self.CONFIG_FILE_NAME,
            self.DB_FILE_NAME,
            self.DB_FILE_NAME + "-wal",
            self.DB_FILE_NAME + "-shm",
            self.TEMP_DATA_FILE_NAME,
            self.HUM_DATA_FILE_NAME,
        ]
        for file_name in legacy_names:
            src_path = os.path.join(install_dir, file_name)
            dst_path = os.path.join(runtime_dir, file_name)
            if not os.path.exists(src_path) or os.path.exists(dst_path):
                continue
            try:
                with open(src_path, "rb") as src_file:
                    data = src_file.read()
                with open(dst_path, "wb") as dst_file:
                    dst_file.write(data)
            except Exception:
                pass

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

    def sanitize_satellite_name(self, raw_name):
        sanitized = []
        for ch in str(raw_name or "").strip():
            if len(sanitized) >= self.SATELLITE_NAME_MAX_LEN:
                break
            if ch.isascii() and (ch.isalnum() or ch in "_- "):
                sanitized.append(ch)
        safe_name = "".join(sanitized).strip()
        return safe_name or "satellite"

    def satellite_display_name(self, node_id):
        state = self.esp_node_state.get(node_id) or {}
        slot_idx = state.get("slot_idx")
        try:
            slot_idx = int(slot_idx)
        except Exception:
            slot_idx = None
        if slot_idx is not None and 0 <= slot_idx < self.CHANNEL_COUNT:
            name = str(self.channel_names[slot_idx] or "").strip()
            if name:
                return name
        name = str(state.get("name") or "satellite").strip()
        return name or "satellite"

    def satellite_editor_initial_name(self, ch_idx):
        current_name = str(self.channel_names[ch_idx] or "").strip()
        node_id = self.find_esp_node_id_by_slot(ch_idx)
        if node_id is None:
            return current_name
        state = self.esp_node_state.get(node_id) or {}
        remote_name = str(state.get("name") or "").strip()
        auto_prefix = "ESP Node {0} - ".format(node_id)
        if current_name.startswith(auto_prefix) and remote_name:
            return remote_name
        return current_name

    def apply_channel_editor_changes(self, ch_idx, new_name, new_color, record_enabled):
        display_name = str(new_name or "").strip() or self.default_channel_name(ch_idx)
        self.channel_names[ch_idx] = display_name
        self.channel_colors[ch_idx] = new_color
        self.set_channel_recording(ch_idx, bool(record_enabled))
        tag = "ch_color_{0}".format(ch_idx)
        self.tree.tag_configure(tag, foreground=new_color)
        self.update_channel_tree_row(ch_idx)
        node_id = self.find_esp_node_id_by_slot(ch_idx)
        if node_id is not None:
            state = self.ensure_esp_node_state(node_id)
            state["slot_idx"] = ch_idx
        for ctx in self.graph_contexts.values():
            ctx["lines"][ch_idx].set_label(display_name)
            ctx["lines"][ch_idx].set_color(new_color)
        self.refresh_legend()
        for ctx in self.graph_contexts.values():
            ctx["canvas"].draw_idle()
        return display_name

    def send_satellite_rename(self, ch_idx, requested_name, parent=None):
        node_id = self.find_esp_node_id_by_slot(ch_idx)
        if node_id is None:
            messagebox.showwarning(
                "Send to Satellite",
                "This channel is not currently mapped to a connected satellite.",
                parent=parent,
            )
            return False, None
        if not self.source_connected["esp"]:
            messagebox.showwarning(
                "Send to Satellite",
                "ESP controller is not connected.",
                parent=parent,
            )
            return False, None
        safe_name = self.sanitize_satellite_name(requested_name)
        if self.send_esp_command("RENAME {0} {1}".format(node_id, safe_name)):
            self.append_console(">>> [ESP] RENAME {0} {1}".format(node_id, safe_name))
            return True, safe_name
        messagebox.showwarning(
            "Send to Satellite",
            "Failed to send rename command to the ESP controller.",
            parent=parent,
        )
        return False, None

    def send_satellite_sleep(self, ch_idx, enabled, parent=None):
        node_id = self.find_esp_node_id_by_slot(ch_idx)
        if node_id is None:
            messagebox.showwarning(
                "Sleep Control",
                "This channel is not currently mapped to a connected satellite.",
                parent=parent,
            )
            return False
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
        command = "SLEEP {0} {1}".format(node_id, command_value)
        if self.send_esp_command(command):
            self.append_console(">>> [ESP] {0}".format(command))
            state = self.ensure_esp_node_state(node_id)
            state["sleep_enabled"] = bool(enabled)
            slot_idx = state.get("slot_idx")
            if slot_idx is not None:
                self.update_channel_tree_row(slot_idx, signal_display=self.current_signals[slot_idx])
            self.refresh_sleep_all_menu_state()
            self.send_esp_command("NODES")
            return True
        messagebox.showwarning(
            "Sleep Control",
            "Failed to send sleep command to the ESP controller.",
            parent=parent,
        )
        return False

    def channel_has_data(self, ch_idx):
        if self.current_temps[ch_idx] not in ("NaN", "", None):
            return True
        if self.current_hums[ch_idx] not in ("NaN", "", None):
            return True
        if (
            self.series_times[ch_idx]
            or self.temp_series_values[ch_idx]
            or self.hum_series_values[ch_idx]
        ):
            return True
        return False

    def channel_is_visible_in_ui(self, ch_idx):
        if self.loaded_session_id is not None and not self.any_source_connected():
            return self.channel_has_data(ch_idx)
        if ch_idx < self.ARDUINO_CHANNEL_COUNT:
            return self.source_connected["arduino"]
        if not self.source_connected["esp"]:
            return False
        return self.find_esp_node_id_by_slot(ch_idx) is not None or self.channel_has_data(ch_idx)

    def visible_channel_indices(self):
        return [i for i in range(self.CHANNEL_COUNT) if self.channel_is_visible_in_ui(i)]

    def channel_row_values(self, ch_idx):
        temp_display = "-"
        hum_display = "-"
        signal_display = self.current_signals[ch_idx] if self.current_signals[ch_idx] not in ("", None) else "-"
        if self.current_temps[ch_idx] not in ("NaN", "", None):
            temp_display = "{0} \u00b0C".format(self.current_temps[ch_idx])
        if self.current_hums[ch_idx] not in ("NaN", "", None):
            hum_display = "{0} %".format(self.current_hums[ch_idx])
        return (
            self.channel_record_cell(ch_idx),
            self.channel_sleep_cell(ch_idx),
            self.channel_display_id(ch_idx),
            self.channel_tree_name(ch_idx),
            temp_display,
            hum_display,
            signal_display,
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
                self.current_signals[slot_idx] = "-"
                self.rebuild_channel_tree()
                return slot_idx
        self.append_console("ESP slot limit reached, dropping node {0}".format(node_id))
        return None

    def any_source_connected(self):
        return any(self.source_connected.values())

    @staticmethod
    def format_board_name(board_name):
        raw = str(board_name or "").strip()
        if not raw:
            return "Arduino"
        return raw.replace("_", " ").title()

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

    def format_signal_display(self, signal_pct=None, rssi_dbm=None):
        pct_text = ""
        dbm_text = ""
        try:
            if signal_pct is not None:
                pct_text = "{0}%".format(int(round(float(signal_pct))))
        except Exception:
            pct_text = ""
        try:
            if rssi_dbm is not None:
                dbm_text = "{0} dBm".format(int(round(float(rssi_dbm))))
        except Exception:
            dbm_text = ""
        if pct_text and dbm_text:
            return "{0} ({1})".format(pct_text, dbm_text)
        return pct_text or dbm_text or "-"

    def channel_legend_label(self, ch_idx):
        return self.channel_legend_label_for_kind(self.active_graph_kind, ch_idx)

    def channel_legend_label_for_kind(self, kind, ch_idx):
        return self.expanded_channel_legend_label_for_kind(kind, ch_idx)

    def compact_channel_legend_label_for_kind(self, kind, ch_idx):
        parts = [self.channel_display_id(ch_idx)]
        if kind == "temp":
            if self.current_temps[ch_idx] not in ("NaN", "", None):
                parts.append("{0}\u00b0C".format(self.current_temps[ch_idx]))
        else:
            if self.current_hums[ch_idx] not in ("NaN", "", None):
                parts.append("{0}%".format(self.current_hums[ch_idx]))
        return " ".join(parts)

    def expanded_channel_legend_label_for_kind(self, kind, ch_idx):
        parts = [self.channel_names[ch_idx]]
        if kind == "temp":
            if self.current_temps[ch_idx] not in ("NaN", "", None):
                parts.append("{0} \u00b0C".format(self.current_temps[ch_idx]))
        else:
            if self.current_hums[ch_idx] not in ("NaN", "", None):
                parts.append("{0} %".format(self.current_hums[ch_idx]))
        return " | ".join(parts)

    def series_values_for_kind(self, kind):
        return self.temp_series_values if kind == "temp" else self.hum_series_values

    def _build_empty_render_cache(self):
        return {kind: [None] * self.CHANNEL_COUNT for kind in ("temp", "hum")}

    def _invalidate_render_cache(self, kind=None, channel_index=None):
        kinds = (kind,) if kind in ("temp", "hum") else ("temp", "hum")
        for cache_kind in kinds:
            cache_entries = self._render_cache.setdefault(cache_kind, [None] * self.CHANNEL_COUNT)
            if channel_index is None:
                for idx in range(self.CHANNEL_COUNT):
                    cache_entries[idx] = None
                continue
            if 0 <= channel_index < self.CHANNEL_COUNT:
                cache_entries[channel_index] = None

    def _sample_render_series(self, times_source, values_source):
        times = list(times_source)
        values = list(values_source)
        if len(times) <= self.MAX_RENDER_POINTS:
            return times, values
        step = int(math.ceil(len(times) / float(self.MAX_RENDER_POINTS)))
        return times[::step], values[::step]

    def _render_series_for_channel(self, kind, channel_index):
        cache_entries = self._render_cache.setdefault(kind, [None] * self.CHANNEL_COUNT)
        cached = cache_entries[channel_index]
        if cached is not None:
            return cached
        sampled = self._sample_render_series(
            self.series_times[channel_index],
            self.series_values_for_kind(kind)[channel_index],
        )
        cache_entries[channel_index] = sampled
        return sampled

    def graph_title_text(self, kind):
        return "Temperature" if kind == "temp" else "Humidity"

    def graph_y_label(self, kind):
        if kind == "temp":
            return "Temperature (\u00b0C)"
        return "Humidity (%)"

    def graph_tab_for_kind(self, kind):
        return self.tab_graph if kind == "temp" else self.tab_humidity_graph

    def set_active_graph(self, kind, select_tab=False):
        ctx = self.graph_contexts.get(kind)
        if ctx is None:
            return
        self.active_graph_kind = kind
        self.figure = ctx["figure"]
        self.ax = ctx["ax"]
        self.canvas = ctx["canvas"]
        self.toolbar = ctx["toolbar"]
        self.h_scroll = ctx["h_scroll"]
        self.lines = ctx["lines"]
        self.graph_split = ctx["graph_split"]
        self.markers_panel = ctx["markers_panel"]
        self.markers_listbox = ctx["markers_listbox"]
        if select_tab and hasattr(self, "notebook"):
            self.notebook.select(self.graph_tab_for_kind(kind))

    def refresh_graph_titles(self):
        session_title = ""
        if self.loaded_session_id is not None:
            session_title = self.get_session_name(self.loaded_session_id)
        elif self.db_session_id is not None:
            session_title = ""
        for kind, ctx in self.graph_contexts.items():
            ctx["ax"].set_title(self.graph_title_text(kind))
            ctx["ax"].set_title(session_title, loc="left")

    def add_auto_marker(self, note, dt=None):
        marker_dt = dt if dt is not None else datetime.now()
        self._place_marker(marker_dt, note, save_to_db=True)
        self.append_console("[MARKER] {0}".format(note))

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

    def build_ui(self):
        style = ttk.Style()
        style.configure("TNotebook.Tab", font=("Segoe UI", 12, "bold"), padding=[22, 8])

        top = tk.Frame(self.root, bg="#e6e6e6", bd=1, relief="solid", height=70)
        top.pack(side=tk.TOP, fill=tk.X)
        top.pack_propagate(False)

        tk.Label(top, text="Interval in s:", bg="#e6e6e6").place(x=10, y=25)
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
        settings_menu.add_command(label="Application settings...", command=self.open_application_settings)
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
        self.markers_floating_var = tk.BooleanVar(value=self.markers_floating)
        settings_menu.add_checkbutton(
            label="Undock markers",
            variable=self.markers_floating_var,
            command=self.on_markers_floating_toggle
        )
        self.terminal_floating_var = tk.BooleanVar(value=self.terminal_mode == "floating")
        settings_menu.add_checkbutton(
            label="Undock terminal",
            variable=self.terminal_floating_var,
            command=self.on_terminal_floating_toggle
        )
        self.sleep_all_var = tk.BooleanVar(value=False)
        settings_menu.add_checkbutton(
            label="Satellite sleep mode ALL on/off",
            variable=self.sleep_all_var,
            command=self.on_sleep_all_toggle
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
        settings_menu.add_separator()
        settings_menu.add_command(label="Open logs folder...", command=self.open_logs_folder)
        settings_menu.add_command(label="App info...", command=self.show_app_info)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        tab_live = ttk.Frame(self.notebook)
        self.tab_graph = ttk.Frame(self.notebook)
        self.tab_humidity_graph = ttk.Frame(self.notebook)
        self.tab_sessions = ttk.Frame(self.notebook)
        self.notebook.add(tab_live, text="Live View")
        self.notebook.add(self.tab_graph, text="Temperature Graph")
        self.notebook.add(self.tab_humidity_graph, text="Humidity Graph")
        self.notebook.add(self.tab_sessions, text="Sessions")
        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_changed)

        #  Live View Tab
        self.live_split = tk.PanedWindow(tab_live, orient=tk.HORIZONTAL, sashrelief="raised")
        self.live_split.pack(fill=tk.BOTH, expand=True)
        self.live_left = tk.Frame(self.live_split)
        self.live_right = tk.Frame(self.live_split)
        self.live_split.add(self.live_left, minsize=560)
        self.live_split.add(self.live_right, minsize=280)
        self.root.after(10, self._apply_initial_live_layout)

        self.tree = ttk.Treeview(
            self.live_left,
            columns=("rec", "sleep", "id", "name", "temp", "hum", "signal"),
            show="headings",
        )
        self.tree.heading("rec", text="Active")
        self.tree.heading("sleep", text="Sleep")
        self.tree.heading("id", text="ID")
        self.tree.heading("name", text="Name  (double-click to edit)")
        self.tree.heading("temp", text="Temp")
        self.tree.heading("hum", text="Hum")
        self.tree.heading("signal", text="Signal")
        self.tree.column("rec", width=64, anchor="center")
        self.tree.column("sleep", width=58, anchor="center")
        self.tree.column("id", width=60, anchor="center")
        self.tree.column("name", width=220, anchor="w")
        self.tree.column("temp", width=120, anchor="center")
        self.tree.column("hum", width=120, anchor="center")
        self.tree.column("signal", width=120, anchor="center")
        for col_id, width_value in self.saved_column_widths.items():
            if col_id in ("rec", "sleep", "id", "name", "temp", "hum", "signal"):
                self.tree.column(col_id, width=width_value)
        self.tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<Double-1>", self.on_tree_double_click)

        for i in range(self.CHANNEL_COUNT):
            tag = "ch_color_{0}".format(i)
            self.tree.tag_configure(tag, foreground=self.channel_colors[i])
        self.rebuild_channel_tree()

        self.txt_console = self._build_terminal_panel(
            self.live_right,
            entry_attr_name="terminal_command_entry"
        )

        self.build_graph_tab(self.tab_graph, "temp")
        self.build_graph_tab(self.tab_humidity_graph, "hum")
        self.set_active_graph("temp")
        self.apply_markers_visibility()

        self.build_sessions_tab(self.tab_sessions)
        self.refresh_sessions_list()



    # Graph scroll / zoom

    def build_graph_tab(self, parent, kind):
        content_frame = tk.Frame(parent)
        toolbar_row = tk.Frame(parent, bg="#f4f4f4", bd=1, relief="flat")
        toolbar_row.pack(side=tk.TOP, fill=tk.X)

        tk.Label(
            toolbar_row,
            text="Left-drag -> pan     Double-click -> add marker     Double-click marker -> edit",
            font=("Segoe UI", 8), fg="#666666", bg="#f4f4f4"
        ).pack(side=tk.LEFT, padx=(8, 6), pady=5)

        ttk.Separator(toolbar_row, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=4, pady=3)

        btn_kw = dict(font=("Segoe UI", 9, "bold"), relief="groove", bd=1, padx=6, pady=2)
        tk.Button(
            toolbar_row, text="Reset View",
            command=lambda graph_kind=kind: self._reset_view(graph_kind=graph_kind),
            bg="#e8e8e8", **btn_kw
        ).pack(side=tk.LEFT, padx=(0, 2), pady=4)
        tk.Button(
            toolbar_row, text=" - ",
            command=lambda graph_kind=kind: self._zoom_out(graph_kind=graph_kind),
            bg="#e8e8e8", **btn_kw
        ).pack(side=tk.LEFT, padx=1, pady=4)
        tk.Button(
            toolbar_row, text=" + ",
            command=lambda graph_kind=kind: self._zoom_in(graph_kind=graph_kind),
            bg="#e8e8e8", **btn_kw
        ).pack(side=tk.LEFT, padx=(1, 0), pady=4)

        ttk.Separator(toolbar_row, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=3)

        content_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        graph_split = tk.PanedWindow(content_frame, orient=tk.HORIZONTAL, sashrelief="raised")
        graph_split.pack(fill=tk.BOTH, expand=True)
        chart_frame = tk.Frame(graph_split)
        graph_split.add(chart_frame, minsize=560)

        figure = Figure(figsize=(8, 5), dpi=100)
        ax = figure.add_subplot(111)
        ax.set_title(self.graph_title_text(kind))
        ax.set_title("", loc="left")
        ax.set_xlabel("Time")
        ax.set_ylabel(self.graph_y_label(kind))

        locator = AutoDateLocator()
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(ConciseDateFormatter(locator))
        figure.autofmt_xdate(rotation=30)

        lines = []
        for i in range(self.CHANNEL_COUNT):
            line, = ax.plot([], [], label=self.channel_names[i], linewidth=2, color=self.channel_colors[i])
            lines.append(line)

        canvas = FigureCanvasTkAgg(figure, master=chart_frame)
        canvas.mpl_connect("button_press_event", lambda event, graph_kind=kind: self._on_mouse_press(event, graph_kind=graph_kind))
        canvas.mpl_connect("motion_notify_event", lambda event, graph_kind=kind: self._on_mouse_drag(event, graph_kind=graph_kind))
        canvas.mpl_connect("button_release_event", lambda event, graph_kind=kind: self._on_mouse_release(event, graph_kind=graph_kind))
        canvas.mpl_connect("scroll_event", lambda event, graph_kind=kind: self._on_mouse_wheel(event, graph_kind=graph_kind))
        canvas.mpl_connect("figure_leave_event", lambda event, graph_kind=kind: self._on_graph_leave(graph_kind))
        ax.callbacks.connect("xlim_changed", lambda changed_ax, graph_kind=kind: self._on_xlim_changed(changed_ax, graph_kind=graph_kind))

        nav_frame = tk.Frame(toolbar_row, bg="#f4f4f4")
        nav_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        toolbar = NavigationToolbar2Tk(canvas, nav_frame)
        toolbar.update()
        if hasattr(toolbar, "_message_label"):
            try:
                toolbar._message_label.pack_forget()
            except Exception:
                pass
        toolbar.set_message = lambda _s: None
        ttk.Separator(toolbar_row, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=3)

        if not hasattr(self, "markers_visible_var"):
            self.markers_visible_var = tk.BooleanVar(value=self.markers_visible)
        tk.Checkbutton(
            toolbar_row,
            text="Markers Panel",
            variable=self.markers_visible_var,
            onvalue=True,
            offvalue=False,
            bg="#f4f4f4",
            command=self.on_markers_visibility_toggle
        ).pack(side=tk.RIGHT, padx=(4, 8), pady=4)

        h_scroll = ttk.Scrollbar(
            chart_frame, orient=tk.HORIZONTAL,
            command=lambda action, *args, graph_kind=kind: self._on_xscroll(action, *args, graph_kind=graph_kind)
        )
        h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        markers_panel = tk.Frame(graph_split, width=265, bg="#f0f0f0", bd=1, relief="sunken")
        markers_panel.pack_propagate(False)
        tk.Label(markers_panel, text="Markers", bg="#f0f0f0",
                 font=("Segoe UI", 11, "bold"), pady=6).pack(fill=tk.X, padx=8)
        ttk.Separator(markers_panel, orient="horizontal").pack(fill=tk.X)

        markers_list_frame = tk.Frame(markers_panel, bg="#f0f0f0")
        markers_list_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        sb = ttk.Scrollbar(markers_list_frame, orient=tk.VERTICAL)

        markers_listbox = tk.Listbox(
            markers_list_frame,
            font=("Consolas", 8), selectmode=tk.SINGLE,
            bg="#ffffff", activestyle="none", bd=0, relief="flat"
        )
        markers_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.config(command=markers_listbox.yview)
        markers_listbox.configure(
            yscrollcommand=lambda first, last, scrollbar=sb: self._set_listbox_scrollbar(
                scrollbar, first, last
            )
        )
        markers_listbox.bind(
            "<Double-1>",
            lambda event, graph_kind=kind: self.on_listbox_double_click(event, graph_kind=graph_kind)
        )

        tk.Button(
            markers_panel, text="Delete Selected",
            command=lambda graph_kind=kind: self.delete_marker(graph_kind=graph_kind),
            bg="#c0392b", fg="white", font=("Segoe UI", 9), pady=4
        ).pack(fill=tk.X, padx=8, pady=(0, 8))

        self.graph_contexts[kind] = {
            "content_frame": content_frame,
            "graph_split": graph_split,
            "figure": figure,
            "ax": ax,
            "canvas": canvas,
            "toolbar": toolbar,
            "h_scroll": h_scroll,
            "lines": lines,
            "markers_panel": markers_panel,
            "markers_listbox": markers_listbox,
            "markers_scrollbar": sb,
            "legend_expanded": False,
        }
        graph_split.bind("<ButtonRelease-1>", lambda _event, graph_kind=kind: self._on_graph_split_released(graph_kind))
        self.apply_grid_settings()
        self.refresh_legend(kind)
        self._refresh_listbox_scrollbar(markers_listbox, sb)

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


    # Channel rename / color

    # Calibration


    # Markers

    # Mouse drag-pan

    _DRAG_THRESHOLD = 4   # pixels of movement before drag activates


    _CONSOLE_MAX_LINES = 500


