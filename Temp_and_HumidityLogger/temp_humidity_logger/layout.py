import tkinter as tk
from tkinter import ttk


class LayoutMixin:
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

    def _set_graph_split(self, x, graph_kind=None):
        ctx = self.graph_contexts.get(graph_kind or self.active_graph_kind)
        if ctx is None:
            return
        try:
            ctx["graph_split"].sash_place(0, int(x), 0)
        except Exception:
            pass

    def _graph_split_target_x(self, graph_kind=None):
        ctx = self.graph_contexts.get(graph_kind or self.active_graph_kind)
        if ctx is None:
            return None
        try:
            total_width = int(ctx["graph_split"].winfo_width())
        except Exception:
            total_width = 0
        if total_width <= 1:
            return self.saved_graph_split_x
        ratio = self.saved_graph_split_ratio
        if ratio is None:
            ratio = self.DEFAULT_GRAPH_SPLIT_RATIO
        return int(round(total_width * ratio))

    def _capture_graph_split_x(self, graph_kind=None):
        ctx = self.graph_contexts.get(graph_kind or self.active_graph_kind)
        if ctx is None or not self.markers_visible:
            return
        try:
            split_x = int(ctx["graph_split"].sash_coord(0)[0])
        except Exception:
            split_x = None
        if split_x is not None and split_x > 100:
            self.saved_graph_split_x = split_x
            try:
                total_width = int(ctx["graph_split"].winfo_width())
            except Exception:
                total_width = 0
            if total_width > 1:
                ratio = float(split_x) / float(total_width)
                self.saved_graph_split_ratio = max(0.50, min(0.90, ratio))

    def _graph_pane_names(self, graph_kind=None):
        ctx = self.graph_contexts.get(graph_kind or self.active_graph_kind)
        if ctx is None:
            return set()
        try:
            return {str(name) for name in ctx["graph_split"].panes()}
        except Exception:
            return set()

    def _attach_markers_panel(self, graph_kind=None):
        ctx = self.graph_contexts.get(graph_kind or self.active_graph_kind)
        if ctx is None:
            return
        markers_pane_name = str(ctx["markers_panel"])
        if markers_pane_name not in self._graph_pane_names(graph_kind):
            ctx["graph_split"].add(ctx["markers_panel"], minsize=220)
        self.root.after(
            10,
            lambda graph_kind=graph_kind or self.active_graph_kind: self._apply_graph_split_to_kind(graph_kind)
        )

    def _detach_markers_panel(self, graph_kind=None):
        ctx = self.graph_contexts.get(graph_kind or self.active_graph_kind)
        if ctx is None:
            return
        markers_pane_name = str(ctx["markers_panel"])
        if markers_pane_name in self._graph_pane_names(graph_kind):
            self._capture_graph_split_x(graph_kind)
            try:
                ctx["graph_split"].forget(ctx["markers_panel"])
            except Exception:
                pass

    def _apply_graph_split_to_kind(self, graph_kind):
        target_x = self._graph_split_target_x(graph_kind)
        if target_x is not None:
            self._set_graph_split(target_x, graph_kind=graph_kind)

    def _apply_graph_split_to_all(self, exclude_kind=None):
        for graph_kind in self.graph_contexts:
            if graph_kind == exclude_kind:
                continue
            self._apply_graph_split_to_kind(graph_kind)

    def _on_graph_split_released(self, graph_kind):
        if not self.markers_visible:
            return
        self._capture_graph_split_x(graph_kind)
        self._apply_graph_split_to_all(exclude_kind=graph_kind)
        self.save_config()

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

    def _build_terminal_panel(self, parent, entry_attr_name):
        tk.Label(parent, text="Terminal", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=8, pady=(8, 4)
        )
        console_text = tk.Text(
            parent, height=10, bg="black", fg="lime",
            font=("Consolas", 9), wrap="word"
        )
        console_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))

        command_row = tk.Frame(parent)
        command_row.pack(fill=tk.X, padx=8, pady=(0, 8))

        target_combo = ttk.Combobox(
            command_row,
            width=5,
            state="readonly",
            values=("ESP", "ARD"),
            textvariable=self.terminal_command_target_var
        )
        target_combo.pack(side=tk.LEFT, padx=(0, 6))

        entry = tk.Entry(command_row, textvariable=self.terminal_command_var)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        entry.bind("<Return>", self.send_terminal_command)

        tk.Button(command_row, text="Send", width=8, command=self.send_terminal_command).pack(side=tk.LEFT)

        setattr(self, entry_attr_name, entry)
        return console_text

    def _create_floating_terminal_window(self):
        if self.terminal_window is not None and self.terminal_window.winfo_exists():
            self.terminal_window.deiconify()
            self.terminal_window.lift()
            return

        self.terminal_window = tk.Toplevel(self.root)
        self.terminal_window.title("Terminal")
        self.terminal_window.geometry("680x320")
        self.terminal_window.transient(self.root)

        self.floating_console_text = self._build_terminal_panel(
            self.terminal_window,
            entry_attr_name="floating_terminal_command_entry"
        )
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
        self.floating_terminal_command_entry = None

    def _on_terminal_window_close(self):
        self.terminal_mode = "docked"
        self.apply_terminal_mode(persist=True)

    def _create_floating_markers_window(self):
        if self.markers_window is not None and self.markers_window.winfo_exists():
            self.markers_window.deiconify()
            self.markers_window.lift()
            return

        self.markers_window = tk.Toplevel(self.root)
        self.markers_window.title("Markers")
        self.markers_window.geometry("420x420")
        self.markers_window.transient(self.root)

        tk.Label(self.markers_window, text="Markers", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=8, pady=(8, 4)
        )
        list_frame = tk.Frame(self.markers_window, bg="#f0f0f0")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        sb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL)
        self.floating_markers_scrollbar = sb

        self.floating_markers_listbox = tk.Listbox(
            list_frame,
            font=("Consolas", 8),
            selectmode=tk.SINGLE,
            bg="#ffffff",
            activestyle="none",
            bd=0,
            relief="flat"
        )
        self.floating_markers_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.config(command=self.floating_markers_listbox.yview)
        self.floating_markers_listbox.configure(
            yscrollcommand=lambda first, last, scrollbar=sb: self._set_listbox_scrollbar(
                scrollbar, first, last
            )
        )
        self.floating_markers_listbox.bind(
            "<Double-1>",
            lambda event: self.on_listbox_double_click(event, source_listbox=self.floating_markers_listbox)
        )

        tk.Button(
            self.markers_window,
            text="Delete Selected",
            command=lambda: self.delete_marker(source_listbox=self.floating_markers_listbox),
            bg="#c0392b",
            fg="white",
            font=("Segoe UI", 9),
            pady=4
        ).pack(fill=tk.X, padx=8, pady=(0, 8))

        self._rebuild_listbox()
        self._refresh_listbox_scrollbar(self.floating_markers_listbox, sb)
        self.markers_window.protocol("WM_DELETE_WINDOW", self._on_markers_window_close)

    def _destroy_floating_markers_window(self):
        if self.markers_window is not None:
            try:
                if self.markers_window.winfo_exists():
                    self.markers_window.destroy()
            except Exception:
                pass
        self.markers_window = None
        self.floating_markers_listbox = None
        self.floating_markers_scrollbar = None

    def _on_markers_window_close(self):
        self.markers_floating = False
        self.apply_markers_mode(persist=True)

    def _set_listbox_scrollbar(self, scrollbar, first, last):
        if scrollbar is None:
            return
        try:
            first_val = float(first)
            last_val = float(last)
        except Exception:
            first_val = 0.0
            last_val = 1.0
        scrollbar.set(first, last)
        if first_val <= 0.0 and last_val >= 1.0:
            if scrollbar.winfo_manager():
                scrollbar.pack_forget()
        elif not scrollbar.winfo_manager():
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def _refresh_listbox_scrollbar(self, listbox, scrollbar):
        if listbox is None or scrollbar is None:
            return
        try:
            listbox.update_idletasks()
            first, last = listbox.yview()
        except Exception:
            first, last = (0.0, 1.0)
        self._set_listbox_scrollbar(scrollbar, first, last)

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

    def apply_markers_mode(self, persist=True):
        if self.markers_floating and self.markers_visible:
            for graph_kind in self.graph_contexts:
                self._detach_markers_panel(graph_kind)
            self._create_floating_markers_window()
        else:
            self._destroy_floating_markers_window()
            if self.markers_visible:
                for graph_kind in self.graph_contexts:
                    self._attach_markers_panel(graph_kind)
            else:
                for graph_kind in self.graph_contexts:
                    self._detach_markers_panel(graph_kind)

        if hasattr(self, "markers_visible_var"):
            self.markers_visible_var.set(self.markers_visible)
        if hasattr(self, "markers_floating_var"):
            self.markers_floating_var.set(self.markers_floating)
        if persist:
            self.save_config()
