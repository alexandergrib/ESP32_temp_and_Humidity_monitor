import tkinter as tk
from tkinter import simpledialog

import matplotlib.dates as mdates
from matplotlib.ticker import AutoMinorLocator, NullFormatter, NullLocator


class GraphInteractionMixin:
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

    def _on_xlim_changed(self, ax, graph_kind=None):
        if graph_kind is not None:
            self.set_active_graph(graph_kind)
        if self._ignore_xlim_changes > 0:
            self._ignore_xlim_changes -= 1
            self._update_scrollbar()
            return
        if not self._in_redraw:
            self._auto_view = False
        self._update_scrollbar()

    def _update_scrollbar(self, graph_kind=None):
        if graph_kind is not None:
            self.set_active_graph(graph_kind)
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

    def _on_xscroll(self, action, *args, **kwargs):
        graph_kind = kwargs.get("graph_kind")
        if graph_kind is not None:
            self.set_active_graph(graph_kind)
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

    def _apply_zoom(self, scale, x_focus=None, y_focus=None, graph_kind=None):
        if graph_kind is not None:
            self.set_active_graph(graph_kind)
        x_lo, x_hi = self.ax.get_xlim()
        y_lo, y_hi = self.ax.get_ylim()
        new_x_lo, new_x_hi = self._zoom_axis(x_lo, x_hi, x_focus, scale)
        new_y_lo, new_y_hi = self._zoom_axis(y_lo, y_hi, y_focus, scale)
        self.ax.set_xlim(new_x_lo, new_x_hi)
        self.ax.set_ylim(new_y_lo, new_y_hi)
        self.canvas.draw_idle()

    def _zoom_in(self, x_focus=None, y_focus=None, graph_kind=None):
        self._apply_zoom(self.ZOOM_FACTOR, x_focus=x_focus, y_focus=y_focus, graph_kind=graph_kind)

    def _zoom_out(self, x_focus=None, y_focus=None, graph_kind=None):
        self._apply_zoom(1.0 / self.ZOOM_FACTOR, x_focus=x_focus, y_focus=y_focus, graph_kind=graph_kind)

    def _reset_view(self, graph_kind=None):
        if graph_kind is not None:
            self.set_active_graph(graph_kind)
        self._auto_view = True
        self._ignore_xlim_changes += 1
        self._in_redraw = True
        self.ax.autoscale()
        self._in_redraw = False
        self._update_scrollbar()
        self.canvas.draw_idle()

    def on_tab_changed(self, _event):
        selected = self.notebook.select()
        if selected == str(self.tab_graph):
            self.set_active_graph("temp")
            self._auto_view = True
            self._schedule_redraw()
        elif selected == str(self.tab_humidity_graph):
            self.set_active_graph("hum")
            self._auto_view = True
            self._schedule_redraw()
        elif hasattr(self, "tab_sessions") and self.notebook.select() == str(self.tab_sessions):
            self.refresh_sessions_list()

    def apply_grid_settings(self):
        if not self.graph_contexts:
            return
        for ctx in self.graph_contexts.values():
            ax = ctx["ax"]
            ax.set_axisbelow(True)
            ax.grid(True, which="major", color="gainsboro", linewidth=0.8)
            if self.minor_grid_enabled:
                ax.yaxis.set_minor_locator(AutoMinorLocator(2))
                ax.xaxis.set_minor_locator(mdates.AutoDateLocator(minticks=12, maxticks=36))
                ax.xaxis.set_minor_formatter(NullFormatter())
                ax.grid(True, which="minor", color="#ededed", linestyle=":", linewidth=0.6)
            else:
                ax.yaxis.set_minor_locator(NullLocator())
                ax.xaxis.set_minor_locator(NullLocator())
                ax.xaxis.set_minor_formatter(NullFormatter())
                ax.grid(False, which="minor")

    def on_minor_grid_toggle(self):
        self.minor_grid_enabled = bool(self.minor_grid_var.get())
        self.apply_grid_settings()
        for ctx in self.graph_contexts.values():
            ctx["canvas"].draw_idle()
        self.save_config()

    def apply_markers_visibility(self):
        self.apply_markers_mode(persist=False)

    def on_markers_visibility_toggle(self):
        self.markers_visible = bool(self.markers_visible_var.get())
        self.apply_markers_mode(persist=True)

    def on_markers_floating_toggle(self):
        self.markers_floating = bool(self.markers_floating_var.get())
        if self.markers_floating:
            self.markers_visible = True
        self.apply_markers_mode(persist=True)

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

    def _on_mouse_press(self, event, graph_kind=None):
        """Handle both drag-start (left-click) and double-click (marker actions)."""
        if graph_kind is not None:
            self.set_active_graph(graph_kind)
        if event.inaxes != self.ax:
            return

        # Double-click: add / edit marker
        if event.dblclick:
            if self.toolbar.mode != "" or event.xdata is None:
                return
            PIXEL_HIT = 9
            for idx, marker in enumerate(self.markers):
                try:
                    artists = marker.get("artists", {}).get(self.active_graph_kind)
                    if not artists:
                        continue
                    mx = self.ax.transData.transform((mdates.date2num(marker["datetime"]), 0))[0]
                    if abs(event.x - mx) <= PIXEL_HIT:
                        self.edit_marker(idx)
                        return
                except Exception:
                    continue
            clicked_dt = mdates.num2date(event.xdata).replace(tzinfo=None)
            self._prompt_and_place_marker(clicked_dt)
            return

        # Single left-click: begin drag tracking
        if event.button == 1 and self.toolbar.mode == "" and event.xdata is not None:
            self._drag_press_x = event.x
            self._drag_press_y = event.y
            self._drag_xlim = self.ax.get_xlim()
            self._drag_ylim = self.ax.get_ylim()
            # Snapshot the inverse transform at this moment so the mapping
            # stays consistent even as we shift xlim/ylim during the drag.
            self._drag_inv_tf = self.ax.transData.inverted()
            self._is_dragging = False
            self._drag_graph_kind = self.active_graph_kind
            self.canvas.get_tk_widget().config(cursor="fleur")

    def _on_mouse_drag(self, event, graph_kind=None):
        """Pan the axes while the left button is held and moved."""
        if graph_kind is not None:
            self.set_active_graph(graph_kind)
            self._update_legend_hover_state(event, graph_kind)
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

    def _on_mouse_release(self, event, graph_kind=None):
        """End drag."""
        if graph_kind is not None:
            self.set_active_graph(graph_kind)
        if self._drag_press_x is not None:
            self.canvas.get_tk_widget().config(cursor="")
        self._cancel_drag()

    def _on_mouse_wheel(self, event, graph_kind=None):
        """Zoom with mouse wheel in the graph area."""
        if graph_kind is not None:
            self.set_active_graph(graph_kind)
            self._update_legend_hover_state(event, graph_kind)
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
        self._drag_graph_kind = None

    def _on_graph_leave(self, graph_kind):
        self._set_legend_expanded(graph_kind, False)

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
        artists = {}
        for kind, ctx in self.graph_contexts.items():
            vline = ctx["ax"].axvline(x=dt, color="crimson", linestyle="--",
                                      linewidth=1.4, alpha=0.85, zorder=5)
            annotation = ctx["ax"].annotate(
                note,
                xy=(dt, 1.0),
                xycoords=("data", "axes fraction"),
                xytext=(3, -4), textcoords="offset points",
                fontsize=7, color="crimson",
                rotation=90, va="top", ha="left",
                clip_on=False, zorder=6
            )
            artists[kind] = {"vline": vline, "annotation": annotation}
        self.markers.append({"datetime": dt, "note": note, "artists": artists})
        self._rebuild_listbox()
        if save_to_db:
            self._save_marker_to_db("ADD", dt, note)
        for ctx in self.graph_contexts.values():
            ctx["canvas"].draw_idle()

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
        for artists in marker.get("artists", {}).values():
            artists["annotation"].set_text(new_note)
        self._rebuild_listbox()
        self._save_marker_to_db("EDIT", marker["datetime"], new_note)
        for ctx in self.graph_contexts.values():
            ctx["canvas"].draw_idle()

    def on_listbox_double_click(self, event, graph_kind=None, source_listbox=None):
        if graph_kind is not None:
            self.set_active_graph(graph_kind)
        listbox = source_listbox or self.markers_listbox
        sel = listbox.curselection()
        if not sel:
            return
        marker_idx = sel[0] // 3
        if marker_idx < len(self.markers):
            self.edit_marker(marker_idx)

    def delete_marker(self, graph_kind=None, source_listbox=None):
        if graph_kind is not None:
            self.set_active_graph(graph_kind)
        listbox = source_listbox or self.markers_listbox
        sel = listbox.curselection()
        if not sel:
            return
        marker_idx = sel[0] // 3
        if marker_idx >= len(self.markers):
            return
        marker = self.markers[marker_idx]
        for artists in marker.get("artists", {}).values():
            try:
                artists["vline"].remove()
            except Exception:
                pass
            try:
                artists["annotation"].remove()
            except Exception:
                pass
        self._save_marker_to_db("DEL", marker["datetime"], marker["note"])
        self.markers.pop(marker_idx)
        self._rebuild_listbox()
        for ctx in self.graph_contexts.values():
            ctx["canvas"].draw_idle()

    def _rebuild_listbox(self):
        for ctx in self.graph_contexts.values():
            listbox = ctx["markers_listbox"]
            listbox.delete(0, tk.END)
            for marker in self.markers:
                ts = marker["datetime"].strftime("%Y-%m-%d %H:%M:%S")
                listbox.insert(tk.END, u"\u25cf {0}".format(ts))
                listbox.insert(tk.END, "  {0}".format(marker["note"]))
                listbox.insert(tk.END, "")
            if self.markers:
                listbox.see(tk.END)
            self._refresh_listbox_scrollbar(listbox, ctx.get("markers_scrollbar"))
        if self.floating_markers_listbox is not None:
            self.floating_markers_listbox.delete(0, tk.END)
            for marker in self.markers:
                ts = marker["datetime"].strftime("%Y-%m-%d %H:%M:%S")
                self.floating_markers_listbox.insert(tk.END, u"\u25cf {0}".format(ts))
                self.floating_markers_listbox.insert(tk.END, "  {0}".format(marker["note"]))
                self.floating_markers_listbox.insert(tk.END, "")
            if self.markers:
                self.floating_markers_listbox.see(tk.END)
            self._refresh_listbox_scrollbar(
                self.floating_markers_listbox,
                self.floating_markers_scrollbar
            )
