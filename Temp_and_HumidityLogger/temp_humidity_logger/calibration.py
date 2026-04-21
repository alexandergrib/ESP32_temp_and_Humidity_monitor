import tkinter as tk
from tkinter import ttk, messagebox


class CalibrationMixin:
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

        tk.Label(edit, text=("units: °C" if is_temp else "units: %RH"), fg="#555555").grid(
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
