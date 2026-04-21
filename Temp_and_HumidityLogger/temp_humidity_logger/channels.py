import tkinter as tk
from tkinter import messagebox, colorchooser


class ChannelUiMixin:
    def channel_record_cell(self, ch_idx):
        return "\u2611" if self.channel_record_enabled[ch_idx] else "\u2610"

    def update_channel_tree_row(self, ch_idx, temp_display=None, hum_display=None, signal_display=None):
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
        if signal_display is None:
            signal_display = cur[5] if len(cur) > 5 else "-"
        self.tree.item(
            row_id,
            values=(
                self.channel_record_cell(ch_idx),
                self.channel_display_id(ch_idx),
                self.channel_tree_name(ch_idx),
                temp_display,
                hum_display,
                signal_display,
            )
        )

    def set_channel_recording(self, ch_idx, enabled):
        self.channel_record_enabled[ch_idx] = bool(enabled)
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

    def refresh_legend(self, graph_kind=None):
        graph_kinds = [graph_kind] if graph_kind else list(self.graph_contexts.keys())
        for kind in graph_kinds:
            ctx = self.graph_contexts.get(kind)
            if ctx is None:
                continue
            visible_lines = [
                line for i, line in enumerate(ctx["lines"])
                if self.channel_record_enabled[i] and self.channel_is_visible_in_ui(i)
            ]
            if visible_lines:
                expanded = bool(ctx.get("legend_expanded"))
                labels = [
                    (
                        self.expanded_channel_legend_label_for_kind(kind, i)
                        if expanded else
                        self.compact_channel_legend_label_for_kind(kind, i)
                    )
                    for i, line in enumerate(ctx["lines"])
                    if self.channel_record_enabled[i] and self.channel_is_visible_in_ui(i)
                ]
                legend = ctx["ax"].legend(
                    visible_lines,
                    labels,
                    loc="upper left",
                    fontsize=8 if expanded else 7,
                    borderpad=0.8 if expanded else 0.35,
                    labelspacing=0.45 if expanded else 0.25,
                    handlelength=1.8 if expanded else 1.1,
                    handletextpad=0.7 if expanded else 0.45,
                    borderaxespad=0.5,
                    framealpha=0.90,
                )
                ctx["legend"] = legend
            else:
                legend = ctx["ax"].get_legend()
                if legend is not None:
                    legend.remove()
                ctx["legend"] = None

    def _set_legend_expanded(self, graph_kind, expanded):
        ctx = self.graph_contexts.get(graph_kind)
        if ctx is None:
            return
        expanded = bool(expanded)
        if bool(ctx.get("legend_expanded")) == expanded:
            return
        ctx["legend_expanded"] = expanded
        self.refresh_legend(graph_kind)
        ctx["canvas"].draw_idle()

    def _update_legend_hover_state(self, event, graph_kind):
        ctx = self.graph_contexts.get(graph_kind)
        if ctx is None:
            return
        legend = ctx.get("legend")
        if legend is None:
            self._set_legend_expanded(graph_kind, False)
            return
        contains = False
        if event is not None and getattr(event, "x", None) is not None and getattr(event, "y", None) is not None:
            try:
                renderer = ctx["canvas"].get_renderer()
            except Exception:
                renderer = None
            if renderer is not None:
                try:
                    contains = legend.get_window_extent(renderer=renderer).contains(event.x, event.y)
                except Exception:
                    contains = False
        self._set_legend_expanded(graph_kind, contains)

    def open_channel_editor(self, ch_idx):
        dialog = tk.Toplevel(self.root)
        dialog.title("Edit Channel {0}".format(ch_idx))
        dialog.geometry("430x345")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        node_id = self.find_esp_node_id_by_slot(ch_idx)
        is_satellite_channel = ch_idx >= self.ARDUINO_CHANNEL_COUNT
        satellite_state = self.esp_node_state.get(node_id, {}) if node_id is not None else {}
        pad = {"padx": 12, "pady": 8}
        tk.Label(dialog, text="Name:", font=("Segoe UI", 10)).grid(row=0, column=0, sticky="w", **pad)
        name_var = tk.StringVar(value=self.satellite_editor_initial_name(ch_idx))
        name_entry = tk.Entry(dialog, textvariable=name_var, width=28, font=("Segoe UI", 10))
        name_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(0, 12), pady=8)
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
        ).grid(row=2, column=0, columnspan=4, sticky="w", padx=12, pady=(2, 8))

        sleep_var = tk.BooleanVar(value=bool(satellite_state.get("sleep_enabled", False)))
        if is_satellite_channel:
            tk.Checkbutton(
                dialog,
                text="Enable sleep mode on this satellite",
                variable=sleep_var,
                onvalue=True,
                offvalue=False,
                font=("Segoe UI", 10)
            ).grid(row=3, column=0, columnspan=4, sticky="w", padx=12, pady=(0, 8))

        note_text = None
        if is_satellite_channel:
            if node_id is None:
                note_text = (
                    "Save/Close updates the app only. Connect the ESP controller to enable "
                    "Send to satellite and sleep control."
                )
            else:
                note_text = (
                    "Save/Close updates the app only. Send to satellite applies the current name and sleep mode. "
                    "Names use letters, numbers, space, _ or -, up to {0} characters.".format(
                        self.SATELLITE_NAME_MAX_LEN
                    )
                )
        if note_text:
            tk.Label(
                dialog,
                text=note_text,
                justify="left",
                wraplength=390,
                fg="#4c566a",
                font=("Segoe UI", 9),
            ).grid(row=4, column=0, columnspan=4, sticky="w", padx=12, pady=(0, 8))

        def apply_local_name():
            return self.apply_channel_editor_changes(
                ch_idx,
                name_var.get(),
                color_var.get(),
                bool(record_var.get()),
            )

        def save_only():
            apply_local_name()

        def close_and_apply():
            apply_local_name()
            dialog.destroy()

        def send_to_satellite():
            requested_name = name_var.get()
            sent, applied_name = self.send_satellite_rename(ch_idx, requested_name, parent=dialog)
            if not sent:
                return
            if not self.send_satellite_sleep(ch_idx, bool(sleep_var.get()), parent=dialog):
                return
            if applied_name is not None:
                if applied_name != requested_name.strip():
                    messagebox.showinfo(
                        "Send to Satellite",
                        'Satellite name was adjusted to "{0}" to match firmware limits.'.format(applied_name),
                        parent=dialog,
                    )
                name_var.set(applied_name)
            apply_local_name()
            dialog.destroy()

        btn_row = tk.Frame(dialog)
        btn_row.grid(row=5, column=0, columnspan=4, pady=(6, 12))
        tk.Button(
            btn_row,
            text="Save",
            command=save_only,
            bg="#4a90d9",
            fg="white",
            font=("Segoe UI", 10),
            padx=18,
        ).pack(side=tk.LEFT, padx=6)
        if is_satellite_channel:
            send_btn = tk.Button(
                btn_row,
                text="Send to satellite",
                command=send_to_satellite,
                font=("Segoe UI", 10),
                padx=12,
            )
            if node_id is None or not self.source_connected["esp"]:
                send_btn.config(state=tk.DISABLED)
            send_btn.pack(side=tk.LEFT, padx=6)
        tk.Button(
            btn_row,
            text="Close",
            command=close_and_apply,
            font=("Segoe UI", 10),
            padx=18,
        ).pack(side=tk.LEFT, padx=6)
        tk.Button(
            btn_row,
            text="Cancel",
            command=dialog.destroy,
            font=("Segoe UI", 10),
            padx=18,
        ).pack(side=tk.LEFT, padx=6)
        dialog.bind("<Return>", lambda e: save_only())
        dialog.protocol("WM_DELETE_WINDOW", close_and_apply)
        dialog.wait_window()
