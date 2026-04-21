import os

import serial.tools.list_ports
import tkinter as tk
from tkinter import ttk, messagebox


class SettingsUiMixin:
    def show_app_info(self):
        messagebox.showinfo(
            "App Info",
            "{0}\nVersion: {1}\n\nGitHub:\n{2}\n\nLogs data:\n{3}".format(
                self.APP_NAME,
                self.APP_VERSION,
                self.GITHUB_URL,
                # self.install_dir,
                self.base_dir,
            ),
            parent=self.root,
        )


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

    def open_application_settings(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Application Settings")
        dialog.geometry("520x420")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        notebook = ttk.Notebook(dialog)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 6))

        tabs = {
            "channels": ttk.Frame(notebook),
            "serial": ttk.Frame(notebook),
            "files": ttk.Frame(notebook),
            "plot": ttk.Frame(notebook),
        }
        notebook.add(tabs["channels"], text="Channels")
        notebook.add(tabs["serial"], text="Serial")
        notebook.add(tabs["files"], text="Files")
        notebook.add(tabs["plot"], text="Plot")

        vars_map = {
            "arduino_channel_count": tk.StringVar(value=str(self.runtime_settings["arduino_channel_count"])),
            "esp_channel_count": tk.StringVar(value=str(self.runtime_settings["esp_channel_count"])),
            "default_interval_text": tk.StringVar(value=str(self.runtime_settings["default_interval_text"])),
            "smoothing_window": tk.StringVar(value=str(self.runtime_settings["smoothing_window"])),
            "satellite_smoothing_seconds": tk.StringVar(value=str(self.runtime_settings["satellite_smoothing_seconds"])),
            "arduino_baud_rate": tk.StringVar(value=str(self.runtime_settings["arduino_baud_rate"])),
            "esp_baud_rate": tk.StringVar(value=str(self.runtime_settings["esp_baud_rate"])),
            "db_file_name": tk.StringVar(value=str(self.runtime_settings["db_file_name"])),
            "temp_data_file_name": tk.StringVar(value=str(self.runtime_settings["temp_data_file_name"])),
            "hum_data_file_name": tk.StringVar(value=str(self.runtime_settings["hum_data_file_name"])),
            "plot_history_seconds": tk.StringVar(value=str(self.runtime_settings["plot_history_seconds"])),
            "max_render_points": tk.StringVar(value=str(self.runtime_settings["max_render_points"])),
            "zoom_factor": tk.StringVar(value=str(self.runtime_settings["zoom_factor"])),
            "default_graph_split_ratio": tk.StringVar(value=str(self.runtime_settings["default_graph_split_ratio"])),
        }

        def add_entry(parent, row, label, key, width=18, note=""):
            tk.Label(parent, text=label, anchor="w", font=("Segoe UI", 10)).grid(
                row=row, column=0, sticky="w", padx=(12, 8), pady=7
            )
            tk.Entry(parent, textvariable=vars_map[key], width=width, font=("Segoe UI", 10)).grid(
                row=row, column=1, sticky="w", padx=(0, 12), pady=7
            )
            if note:
                tk.Label(parent, text=note, anchor="w", fg="#666666", font=("Segoe UI", 8)).grid(
                    row=row, column=2, sticky="w", padx=(0, 10), pady=7
                )

        add_entry(tabs["channels"], 0, "Arduino channels", "arduino_channel_count", note="Restart required")
        add_entry(tabs["channels"], 1, "ESP channels", "esp_channel_count", note="Restart required")
        add_entry(tabs["channels"], 2, "Default interval", "default_interval_text", note="e.g. 1s, 500ms")
        add_entry(tabs["channels"], 3, "Arduino smoothing samples", "smoothing_window", note="Restart required")
        add_entry(tabs["channels"], 4, "Satellite smoothing (s)", "satellite_smoothing_seconds", note="0 disables")

        add_entry(tabs["serial"], 0, "Arduino baud", "arduino_baud_rate", note="Next connection")
        add_entry(tabs["serial"], 1, "ESP baud", "esp_baud_rate", note="Next connection")

        add_entry(tabs["files"], 0, "Database filename", "db_file_name", note="Restart required")
        add_entry(tabs["files"], 1, "Temp output filename", "temp_data_file_name")
        add_entry(tabs["files"], 2, "Hum output filename", "hum_data_file_name")
        tk.Label(
            tabs["files"],
            text="Filenames are stored in the app folder. Do not enter directories.",
            fg="#666666",
            font=("Segoe UI", 8)
        ).grid(row=3, column=0, columnspan=3, sticky="w", padx=12, pady=(4, 0))

        add_entry(tabs["plot"], 0, "Plot history (sec)", "plot_history_seconds", note="Restart required")
        add_entry(tabs["plot"], 1, "Max render points", "max_render_points")
        add_entry(tabs["plot"], 2, "Zoom factor", "zoom_factor", note="0.10 - 0.95")
        add_entry(tabs["plot"], 3, "Default split ratio", "default_graph_split_ratio", note="0.50 - 0.90")

        tk.Label(
            dialog,
            text="Channel counts, Arduino smoothing, database filename, and plot history are applied on next restart.",
            fg="#666666",
            font=("Segoe UI", 8)
        ).pack(anchor="w", padx=14, pady=(0, 4))

        def apply_and_close():
            raw_settings = {key: var.get().strip() for key, var in vars_map.items()}
            if self.parse_interval_ms(raw_settings["default_interval_text"]) is None:
                messagebox.showwarning("Application Settings", "Default interval must use a format like 500ms, 1s, 2min, or 1h.", parent=dialog)
                return
            for file_key in ("db_file_name", "temp_data_file_name", "hum_data_file_name"):
                filename = raw_settings[file_key]
                if not filename or os.path.basename(filename) != filename:
                    messagebox.showwarning("Application Settings", "Filenames must be plain file names without folders.", parent=dialog)
                    return

            new_settings = self.sanitize_runtime_settings(raw_settings)
            restart_keys = {
                "arduino_channel_count",
                "esp_channel_count",
                "smoothing_window",
                "db_file_name",
                "plot_history_seconds",
            }
            old_settings = dict(self.runtime_settings)
            restart_required = any(new_settings[key] != old_settings.get(key) for key in restart_keys)
            self.runtime_settings = new_settings
            self.apply_live_runtime_settings()
            self.saved_interval_text = self.runtime_settings["default_interval_text"]
            if hasattr(self, "txt_interval") and not self.any_source_connected():
                self.txt_interval.delete(0, tk.END)
                self.txt_interval.insert(0, self.saved_interval_text)
            self.save_config()
            dialog.destroy()

            if restart_required:
                messagebox.showinfo(
                    "Application Settings",
                    "Settings saved.\n\nRestart the app to apply channel counts, smoothing window, database filename, and plot history changes."
                )

        btn_row = tk.Frame(dialog)
        btn_row.pack(pady=(0, 10))
        tk.Button(btn_row, text="Apply", command=apply_and_close, bg="#4a90d9", fg="white", padx=18).pack(side=tk.LEFT, padx=6)
        tk.Button(btn_row, text="Close", command=dialog.destroy, padx=18).pack(side=tk.LEFT, padx=6)

        dialog.wait_window()
