from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog


class SessionUiMixin:
    def _set_sessions_split(self, x):
        if not hasattr(self, "sessions_split"):
            return
        try:
            self.sessions_split.sash_place(0, int(x), 0)
        except Exception:
            pass

    def _apply_initial_sessions_layout(self):
        if not hasattr(self, "sessions_split"):
            return
        try:
            total_width = int(self.sessions_split.winfo_width())
        except Exception:
            total_width = 0
        if total_width <= 1:
            self.root.after(20, self._apply_initial_sessions_layout)
            return
        target_x = max(760, int(total_width * 0.74))
        self._set_sessions_split(target_x)

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

        self.sessions_split = tk.PanedWindow(parent, orient=tk.HORIZONTAL, sashrelief="raised")
        self.sessions_split.pack(fill=tk.BOTH, expand=True)

        left = tk.Frame(self.sessions_split)
        right = tk.Frame(self.sessions_split)
        self.sessions_split.add(left, minsize=720)
        self.sessions_split.add(right, minsize=220)
        self.root.after(10, self._apply_initial_sessions_layout)

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
        self.sessions_tree.column("id", width=45, anchor="center")
        self.sessions_tree.column("name", width=140, anchor="w")
        self.sessions_tree.column("started", width=145, anchor="w")
        self.sessions_tree.column("ended", width=145, anchor="w")
        self.sessions_tree.column("rows", width=80, anchor="e")
        self.sessions_tree.column("markers", width=120, anchor="e")
        self.sessions_tree.column("state", width=95, anchor="center")
        self.sessions_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.sessions_tree.bind("<<TreeviewSelect>>", self.on_session_select)
        self.sessions_tree.bind("<Double-1>", self.on_sessions_tree_double_click)

        sb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.sessions_tree.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        sb_x = ttk.Scrollbar(left, orient=tk.HORIZONTAL, command=self.sessions_tree.xview)
        sb_x.pack(side=tk.BOTTOM, fill=tk.X)
        self.sessions_tree.configure(yscrollcommand=sb.set, xscrollcommand=sb_x.set)

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
            for artists in marker.get("artists", {}).values():
                try:
                    artists["vline"].remove()
                except Exception:
                    pass
                try:
                    artists["annotation"].remove()
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
        self.esp_node_state.clear()
        self._invalidate_render_cache()
        for i in range(self.CHANNEL_COUNT):
            self.temp_history[i].clear()
            self.hum_history[i].clear()
            self.series_times[i].clear()
            self.temp_series_values[i].clear()
            self.hum_series_values[i].clear()
            self.last_plot_second[i] = None
            self.current_temps[i] = "NaN"
            self.current_hums[i] = "NaN"
            self.current_signals[i] = "-"

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
                temp_value = None
                hum_value = None
                if temp_text not in (None, ""):
                    self.current_temps[i] = str(temp_text)
                    try:
                        temp_value = float(temp_text)
                    except (TypeError, ValueError):
                        temp_value = None
                if hum_text not in (None, ""):
                    self.current_hums[i] = str(hum_text)
                    try:
                        hum_value = float(hum_text)
                    except (TypeError, ValueError):
                        hum_value = None
                if temp_value is not None or hum_value is not None:
                    self.add_smoothed_point(i, dt, temp_value, hum_value)

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
            self.update_channel_tree_row(i, temp_disp, hum_disp, self.current_signals[i])

        self.loaded_session_id = session_id
        self.refresh_graph_titles()
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
