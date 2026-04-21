"""Configuration and runtime settings support."""

import configparser
import os


class ConfigMixin:
    @classmethod
    def reading_column_names(cls):
        cols = []
        for i in range(cls.CHANNEL_COUNT):
            cols.extend(["ch{0}_temp".format(i), "ch{0}_hum".format(i)])
        return cols


    @classmethod
    def runtime_settings_defaults(cls):
        return {
            "arduino_channel_count": cls.ARDUINO_CHANNEL_COUNT,
            "esp_channel_count": cls.ESP_CHANNEL_COUNT,
            "default_interval_text": cls.DEFAULT_INTERVAL_TEXT,
            "smoothing_window": cls.SMOOTHING_WINDOW,
            "satellite_smoothing_seconds": cls.SATELLITE_SMOOTHING_SECONDS,
            "arduino_baud_rate": cls.ARDUINO_BAUD_RATE,
            "esp_baud_rate": cls.ESP_BAUD_RATE,
            "db_file_name": cls.DB_FILE_NAME,
            "temp_data_file_name": cls.TEMP_DATA_FILE_NAME,
            "hum_data_file_name": cls.HUM_DATA_FILE_NAME,
            "plot_history_seconds": cls.PLOT_HISTORY_SECONDS,
            "max_render_points": cls.MAX_RENDER_POINTS,
            "zoom_factor": cls.ZOOM_FACTOR,
            "default_graph_split_ratio": cls.DEFAULT_GRAPH_SPLIT_RATIO,
        }


    @staticmethod
    def _sanitize_runtime_filename(value, default_name, required_ext=None):
        raw = os.path.basename(str(value or "").strip())
        if not raw:
            raw = default_name
        if required_ext and not raw.lower().endswith(required_ext.lower()):
            raw += required_ext
        return raw


    def sanitize_runtime_settings(self, settings):
        defaults = self.runtime_settings_defaults()
        clean = dict(defaults)

        def _to_int(key, minimum, maximum=None):
            try:
                value = int(settings.get(key, defaults[key]))
            except Exception:
                value = defaults[key]
            value = max(minimum, value)
            if maximum is not None:
                value = min(maximum, value)
            clean[key] = value

        def _to_float(key, minimum, maximum):
            try:
                value = float(settings.get(key, defaults[key]))
            except Exception:
                value = defaults[key]
            value = max(minimum, min(maximum, value))
            clean[key] = value

        _to_int("arduino_channel_count", 1, 32)
        _to_int("esp_channel_count", 0, 32)
        interval_text = str(settings.get("default_interval_text", defaults["default_interval_text"]) or "").strip()
        clean["default_interval_text"] = interval_text if self.parse_interval_ms(interval_text) is not None else defaults["default_interval_text"]
        _to_int("smoothing_window", 1, 120)
        _to_int("satellite_smoothing_seconds", 0, 3600)
        _to_int("arduino_baud_rate", 300)
        _to_int("esp_baud_rate", 300)
        clean["db_file_name"] = self._sanitize_runtime_filename(
            settings.get("db_file_name"), defaults["db_file_name"], required_ext=".db"
        )
        clean["temp_data_file_name"] = self._sanitize_runtime_filename(
            settings.get("temp_data_file_name"), defaults["temp_data_file_name"], required_ext=".csv"
        )
        clean["hum_data_file_name"] = self._sanitize_runtime_filename(
            settings.get("hum_data_file_name"), defaults["hum_data_file_name"], required_ext=".csv"
        )
        _to_int("plot_history_seconds", 60)
        _to_int("max_render_points", 100)
        _to_float("zoom_factor", 0.10, 0.95)
        _to_float("default_graph_split_ratio", 0.50, 0.90)
        return clean


    def apply_startup_runtime_settings(self):
        cls = self.__class__
        settings = self.runtime_settings
        cls.ARDUINO_CHANNEL_COUNT = int(settings["arduino_channel_count"])
        cls.ESP_CHANNEL_COUNT = int(settings["esp_channel_count"])
        cls.CHANNEL_COUNT = cls.ARDUINO_CHANNEL_COUNT + cls.ESP_CHANNEL_COUNT
        cls.DEFAULT_INTERVAL_TEXT = str(settings["default_interval_text"])
        cls.SMOOTHING_WINDOW = int(settings["smoothing_window"])
        cls.SATELLITE_SMOOTHING_SECONDS = int(settings["satellite_smoothing_seconds"])
        cls.ARDUINO_BAUD_RATE = int(settings["arduino_baud_rate"])
        cls.ESP_BAUD_RATE = int(settings["esp_baud_rate"])
        cls.DB_FILE_NAME = str(settings["db_file_name"])
        cls.TEMP_DATA_FILE_NAME = str(settings["temp_data_file_name"])
        cls.HUM_DATA_FILE_NAME = str(settings["hum_data_file_name"])
        cls.PLOT_HISTORY_SECONDS = int(settings["plot_history_seconds"])
        cls.MAX_RENDER_POINTS = int(settings["max_render_points"])
        cls.ZOOM_FACTOR = float(settings["zoom_factor"])
        cls.DEFAULT_GRAPH_SPLIT_RATIO = float(settings["default_graph_split_ratio"])


    def apply_live_runtime_settings(self):
        cls = self.__class__
        settings = self.runtime_settings
        cls.DEFAULT_INTERVAL_TEXT = str(settings["default_interval_text"])
        cls.SATELLITE_SMOOTHING_SECONDS = int(settings["satellite_smoothing_seconds"])
        cls.ARDUINO_BAUD_RATE = int(settings["arduino_baud_rate"])
        cls.ESP_BAUD_RATE = int(settings["esp_baud_rate"])
        cls.TEMP_DATA_FILE_NAME = str(settings["temp_data_file_name"])
        cls.HUM_DATA_FILE_NAME = str(settings["hum_data_file_name"])
        cls.MAX_RENDER_POINTS = int(settings["max_render_points"])
        cls.ZOOM_FACTOR = float(settings["zoom_factor"])
        cls.DEFAULT_GRAPH_SPLIT_RATIO = float(settings["default_graph_split_ratio"])
        self.temp_data_csv_path = os.path.join(self.base_dir, self.TEMP_DATA_FILE_NAME)
        self.hum_data_csv_path = os.path.join(self.base_dir, self.HUM_DATA_FILE_NAME)
        self._invalidate_render_cache()
        if self.graph_contexts:
            self._schedule_redraw()


    def load_runtime_settings(self):
        cfg = configparser.ConfigParser()
        if os.path.exists(self.config_path):
            try:
                cfg.read(self.config_path, encoding="utf-8")
            except Exception:
                pass
        settings = self.runtime_settings_defaults()
        if cfg.has_section("runtime"):
            for key in settings:
                settings[key] = cfg.get("runtime", key, fallback=settings[key])
        self.runtime_settings = self.sanitize_runtime_settings(settings)
        self.apply_startup_runtime_settings()


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
            markers_visible_text = cfg.get("app", "markers_visible", fallback="1").strip().lower()
            self.markers_visible = markers_visible_text in ("1", "true", "yes", "on")
            markers_floating_text = cfg.get("app", "markers_floating", fallback="0").strip().lower()
            self.markers_floating = markers_floating_text in ("1", "true", "yes", "on")
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
            for col_id in ("rec", "id", "name", "temp", "hum", "signal"):
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
            graph_split_text = cfg.get("layout", "graph_split_x", fallback="").strip()
            if graph_split_text:
                try:
                    graph_split_val = int(graph_split_text)
                except ValueError:
                    graph_split_val = None
                if graph_split_val is not None and graph_split_val > 100:
                    self.saved_graph_split_x = graph_split_val
            graph_split_ratio_text = cfg.get("layout", "graph_split_ratio", fallback="").strip()
            if graph_split_ratio_text:
                try:
                    graph_split_ratio = float(graph_split_ratio_text)
                except ValueError:
                    graph_split_ratio = None
                if graph_split_ratio is not None and 0.50 <= graph_split_ratio <= 0.90:
                    self.saved_graph_split_ratio = graph_split_ratio


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
            "markers_visible": "1" if self.markers_visible else "0",
            "markers_floating": "1" if self.markers_floating else "0",
            "terminal_mode": self.terminal_mode,
            "terminal_visible": "1" if self.terminal_visible else "0",
        }
        cfg["runtime"] = {
            "arduino_channel_count": str(self.runtime_settings["arduino_channel_count"]),
            "esp_channel_count": str(self.runtime_settings["esp_channel_count"]),
            "default_interval_text": str(self.runtime_settings["default_interval_text"]),
            "smoothing_window": str(self.runtime_settings["smoothing_window"]),
            "satellite_smoothing_seconds": str(self.runtime_settings["satellite_smoothing_seconds"]),
            "arduino_baud_rate": str(self.runtime_settings["arduino_baud_rate"]),
            "esp_baud_rate": str(self.runtime_settings["esp_baud_rate"]),
            "db_file_name": str(self.runtime_settings["db_file_name"]),
            "temp_data_file_name": str(self.runtime_settings["temp_data_file_name"]),
            "hum_data_file_name": str(self.runtime_settings["hum_data_file_name"]),
            "plot_history_seconds": str(self.runtime_settings["plot_history_seconds"]),
            "max_render_points": str(self.runtime_settings["max_render_points"]),
            "zoom_factor": "{0:.4f}".format(float(self.runtime_settings["zoom_factor"])),
            "default_graph_split_ratio": "{0:.4f}".format(float(self.runtime_settings["default_graph_split_ratio"])),
        }
        if hasattr(self, "tree"):
            cfg["columns"] = {
                "rec": str(int(self.tree.column("rec", "width"))),
                "id": str(int(self.tree.column("id", "width"))),
                "name": str(int(self.tree.column("name", "width"))),
                "temp": str(int(self.tree.column("temp", "width"))),
                "hum": str(int(self.tree.column("hum", "width"))),
                "signal": str(int(self.tree.column("signal", "width"))),
            }
        if hasattr(self, "live_split"):
            try:
                split_x = int(self.live_split.sash_coord(0)[0])
            except Exception:
                split_x = self.saved_live_split_x
            if split_x is not None:
                cfg["layout"] = {"live_split_x": str(split_x)}
        if hasattr(self, "graph_split"):
            try:
                graph_split_x = int(self.graph_split.sash_coord(0)[0])
            except Exception:
                graph_split_x = self.saved_graph_split_x
            if graph_split_x is not None:
                if "layout" not in cfg:
                    cfg["layout"] = {}
                cfg["layout"]["graph_split_x"] = str(graph_split_x)
                cfg["layout"]["graph_split_ratio"] = "{0:.6f}".format(self.saved_graph_split_ratio)
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

