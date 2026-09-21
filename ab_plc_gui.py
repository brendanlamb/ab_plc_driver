"""
Simple desktop UI for ab_plc_driver.py.

Connect to an Allen-Bradley Logix5000 PLC, browse its tags, check the ones
you want to monitor, watch live values update in a table and a real-time
trend graph, and optionally log every poll of the checked tags to a CSV
file.

Run:
    python ab_plc_gui.py
"""

# Developed by Brendan Lamb with Claude Code. Genuine and focused.

from __future__ import annotations

import itertools
import queue
import threading
import tkinter as tk
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk

import matplotlib

matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

from ab_plc_driver import PLCDriver, TagReading, csv_logger

APP_VERSION = "1.0.0"
POLL_INTERVAL_SEC = 1.0
CHECKED_MARK = "☑"
UNCHECKED_MARK = "☐"
TREND_HISTORY_LEN = 600  # points kept per tag (10 min at 1s poll interval)
DEFAULT_COLOR_CYCLE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]


class PLCApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Allen-Bradley PLC Reader")
        self.geometry("760x600")
        self.minsize(600, 440)

        self.driver: PLCDriver | None = None
        self.csv_path: Path | None = None
        self._csv_callback = None
        self._event_queue: queue.Queue = queue.Queue()

        self._all_tags: list[str] = []
        self._checked: set[str] = set()
        self._last_values: dict[str, tuple[str, str, str]] = {}  # tag -> (value, type, updated)
        self._row_ids: dict[str, str] = {}  # tag -> tree row id (only for currently visible/filtered rows)
        self._history: dict[str, deque] = {}  # tag -> deque[(datetime, float)], numeric tags only
        self._trend_dirty = False

        self._tag_colors: dict[str, str] = {}  # tag -> user-chosen hex color
        self._auto_colors: dict[str, str] = {}  # tag -> auto-assigned hex color (stable once assigned)
        self._color_cycle = itertools.cycle(DEFAULT_COLOR_CYCLE)
        self._color_swatch_buttons: dict[str, tk.Button] = {}

        self._build_widgets()
        self._build_menu()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._drain_queue)
        self.after(500, self._redraw_trend_if_dirty)

    # -- UI layout ------------------------------------------------------

    def _build_widgets(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="PLC IP:").grid(row=0, column=0, sticky="w")
        self.ip_var = tk.StringVar(value="192.168.13.100")
        ttk.Entry(top, textvariable=self.ip_var, width=20).grid(row=0, column=1, padx=(4, 12))

        self.connect_btn = ttk.Button(top, text="Connect", command=self._on_connect)
        self.connect_btn.grid(row=0, column=2)
        self.disconnect_btn = ttk.Button(top, text="Disconnect", command=self._on_disconnect, state="disabled")
        self.disconnect_btn.grid(row=0, column=3, padx=(4, 0))

        self.status_var = tk.StringVar(value="Not connected")
        ttk.Label(top, textvariable=self.status_var, foreground="#555").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )

        # search / filter bar
        filter_row = ttk.Frame(self, padding=(10, 0, 10, 0))
        filter_row.pack(fill="x")
        ttk.Label(filter_row, text="Filter tags:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._refresh_tree())
        ttk.Entry(filter_row, textvariable=self.filter_var, width=30).pack(side="left", padx=(4, 12))

        self.check_all_btn = ttk.Button(filter_row, text="Check all", command=self._check_all, state="disabled")
        self.check_all_btn.pack(side="left")
        self.check_none_btn = ttk.Button(filter_row, text="Uncheck all", command=self._check_none, state="disabled")
        self.check_none_btn.pack(side="left", padx=(4, 0))

        mid = ttk.Frame(self, padding=(10, 8, 10, 0))
        mid.pack(fill="x")
        self.poll_btn = ttk.Button(mid, text="Start monitoring", command=self._toggle_polling, state="disabled")
        self.poll_btn.pack(side="left")

        self.csv_btn = ttk.Button(mid, text="Start CSV logging...", command=self._toggle_csv, state="disabled")
        self.csv_btn.pack(side="left", padx=(6, 0))

        self.csv_status_var = tk.StringVar(value="")
        ttk.Label(mid, textvariable=self.csv_status_var, foreground="#555").pack(side="left", padx=(10, 0))

        # tabs: live table + trend graph
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)

        table_tab = ttk.Frame(self.notebook)
        self.notebook.add(table_tab, text="Tags")
        self._build_tag_table(table_tab)

        trend_tab = ttk.Frame(self.notebook)
        self.notebook.add(trend_tab, text="Trend")
        self._build_trend_chart(trend_tab)

        hint = ttk.Label(
            self,
            text="Click the checkbox to choose which tags are monitored, charted, and logged. All tags are checked by default.",
            foreground="#777",
            padding=(10, 0, 10, 10),
        )
        hint.pack(fill="x")

    def _build_menu(self):
        menubar = tk.Menu(self)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.config(menu=menubar)

    def _show_about(self):
        messagebox.showinfo(
            "About AB PLC Reader",
            "AB PLC Reader\n"
            f"Version {APP_VERSION}\n\n"
            "EtherNet/IP tag reader and live trend viewer for Allen-Bradley\n"
            "ControlLogix / CompactLogix (Logix5000 family) PLCs.\n\n"
            "Built on pycomm3 (github.com/ottowayi/pycomm3) and matplotlib.\n\n"
            "Security note: EtherNet/IP (CIP) has no built-in authentication\n"
            "or encryption. Use only on a trusted, segmented OT network.",
        )

    def _build_tag_table(self, parent):
        columns = ("monitor", "tag", "value", "type", "updated")
        self.tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("monitor", text="")
        self.tree.heading("tag", text="Tag")
        self.tree.heading("value", text="Value")
        self.tree.heading("type", text="Type")
        self.tree.heading("updated", text="Last updated")
        self.tree.column("monitor", width=36, anchor="center", stretch=False)
        self.tree.column("tag", width=260)
        self.tree.column("value", width=170)
        self.tree.column("type", width=90)
        self.tree.column("updated", width=100)
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_tree_click)

        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)

    def _build_trend_chart(self, parent):
        controls = ttk.Frame(parent, padding=(0, 0, 0, 8))
        controls.pack(fill="x")

        # Y axis controls
        y_frame = ttk.LabelFrame(controls, text="Y axis", padding=6)
        y_frame.pack(side="left", padx=(0, 8))
        self.y_auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            y_frame, text="Auto", variable=self.y_auto_var, command=self._on_axis_setting_changed
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(y_frame, text="Min:").grid(row=1, column=0, sticky="w")
        self.y_min_var = tk.StringVar()
        ttk.Entry(y_frame, textvariable=self.y_min_var, width=10).grid(row=1, column=1)
        ttk.Label(y_frame, text="Max:").grid(row=2, column=0, sticky="w")
        self.y_max_var = tk.StringVar()
        ttk.Entry(y_frame, textvariable=self.y_max_var, width=10).grid(row=2, column=1)

        # X axis controls (expressed as "seconds ago" from now, so it works for a live trend)
        x_frame = ttk.LabelFrame(controls, text="X axis (seconds ago)", padding=6)
        x_frame.pack(side="left", padx=(0, 8))
        self.x_auto_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            x_frame, text="Auto", variable=self.x_auto_var, command=self._on_axis_setting_changed
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(x_frame, text="Min (oldest):").grid(row=1, column=0, sticky="w")
        self.x_min_var = tk.StringVar(value="300")
        ttk.Entry(x_frame, textvariable=self.x_min_var, width=10).grid(row=1, column=1)
        ttk.Label(x_frame, text="Max (newest):").grid(row=2, column=0, sticky="w")
        self.x_max_var = tk.StringVar(value="0")
        ttk.Entry(x_frame, textvariable=self.x_max_var, width=10).grid(row=2, column=1)

        ttk.Button(controls, text="Apply axis settings", command=self._on_axis_setting_changed).pack(
            side="left", padx=(0, 8), anchor="s"
        )

        # per-tag color legend / picker, populated dynamically as tags are plotted
        self.color_legend_frame = ttk.LabelFrame(controls, text="Tag colors", padding=6)
        self.color_legend_frame.pack(side="left", fill="y")

        self.trend_figure = Figure(figsize=(5, 3.5), dpi=100)
        self.trend_ax = self.trend_figure.add_subplot(111)
        self.trend_ax.set_title("No numeric data yet")
        self.trend_ax.set_xlabel("Time")
        self.trend_ax.set_ylabel("Value")
        self.trend_figure.tight_layout()

        self.trend_canvas = FigureCanvasTkAgg(self.trend_figure, master=parent)
        self.trend_canvas.draw()
        self.trend_canvas.get_tk_widget().pack(fill="both", expand=True)

    def _on_axis_setting_changed(self):
        self._redraw_trend()

    def _color_for_tag(self, tag: str) -> str:
        if tag in self._tag_colors:
            return self._tag_colors[tag]
        if tag not in self._auto_colors:
            self._auto_colors[tag] = next(self._color_cycle)
        return self._auto_colors[tag]

    def _pick_tag_color(self, tag: str):
        initial = self._color_for_tag(tag)
        _, hex_color = colorchooser.askcolor(color=initial, title=f"Choose color for {tag}")
        if hex_color:
            self._tag_colors[tag] = hex_color
            self._redraw_trend()

    def _rebuild_color_legend(self, plotted_tags: list[str]):
        for child in self.color_legend_frame.winfo_children():
            child.destroy()
        self._color_swatch_buttons.clear()

        if not plotted_tags:
            ttk.Label(self.color_legend_frame, text="(no tags plotted)", foreground="#777").pack(anchor="w")
            return

        for tag in plotted_tags:
            row = ttk.Frame(self.color_legend_frame)
            row.pack(fill="x", anchor="w")
            swatch = tk.Button(
                row,
                bg=self._color_for_tag(tag),
                width=2,
                relief="raised",
                command=lambda t=tag: self._pick_tag_color(t),
            )
            swatch.pack(side="left")
            self._color_swatch_buttons[tag] = swatch
            ttk.Label(row, text=tag).pack(side="left", padx=(4, 0))

    # -- connect / disconnect --------------------------------------------------

    def _on_connect(self):
        ip = self.ip_var.get().strip()
        if not ip:
            messagebox.showerror("Missing IP", "Enter a PLC IP address first.")
            return

        self.connect_btn.configure(state="disabled")
        self.status_var.set(f"Connecting to {ip}...")

        def worker():
            driver = PLCDriver(ip, max_retries=2, retry_backoff=1)
            try:
                driver.connect()
                tags = driver.list_tags()
                self._event_queue.put(("connected", driver, tags))
            except Exception as exc:
                self._event_queue.put(("connect_failed", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_disconnect(self):
        self._stop_polling()
        self._stop_csv()
        if self.driver is not None:
            self.driver.disconnect()
            self.driver = None
        self.tree.delete(*self.tree.get_children())
        self._row_ids.clear()
        self._all_tags = []
        self._checked.clear()
        self._last_values.clear()
        self._history.clear()
        self._tag_colors.clear()
        self._auto_colors.clear()
        self._color_cycle = itertools.cycle(DEFAULT_COLOR_CYCLE)
        self._redraw_trend()
        self.filter_var.set("")
        self.status_var.set("Not connected")
        self.connect_btn.configure(state="normal")
        self.disconnect_btn.configure(state="disabled")
        self.check_all_btn.configure(state="disabled")
        self.check_none_btn.configure(state="disabled")
        self.poll_btn.configure(state="disabled")
        self.csv_btn.configure(state="disabled")

    def _on_close(self):
        self._on_disconnect()
        self.destroy()

    # -- tag table / browsing / checkbox selection ------------------------------------------------------

    def _populate_tags(self, tags: list[str]):
        self._all_tags = tags
        self._checked = set(tags)  # default: monitor everything
        self._last_values.clear()
        self._history.clear()
        self._refresh_tree()

    def _refresh_tree(self):
        """Rebuild the visible rows from _all_tags, applying the current filter text."""
        needle = self.filter_var.get().strip().lower()
        visible = [t for t in self._all_tags if needle in t.lower()] if needle else list(self._all_tags)

        self.tree.delete(*self.tree.get_children())
        self._row_ids.clear()
        for tag in visible:
            mark = CHECKED_MARK if tag in self._checked else UNCHECKED_MARK
            value, dtype, updated = self._last_values.get(tag, ("", "", ""))
            row_id = self.tree.insert("", "end", values=(mark, tag, value, dtype, updated))
            self._row_ids[tag] = row_id

    def _on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        col = self.tree.identify_column(event.x)
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        tag = self.tree.set(row_id, "tag")
        if col == "#1":  # monitor checkbox column
            if tag in self._checked:
                self._checked.discard(tag)
            else:
                self._checked.add(tag)
            self.tree.set(row_id, "monitor", CHECKED_MARK if tag in self._checked else UNCHECKED_MARK)
            self._trend_dirty = True

    def _check_all(self):
        self._checked = set(self._all_tags)
        self._refresh_tree()
        self._trend_dirty = True

    def _check_none(self):
        self._checked.clear()
        self._refresh_tree()
        self._trend_dirty = True

    def _selected_tags(self) -> list[str]:
        return [t for t in self._all_tags if t in self._checked]

    # -- polling ------------------------------------------------------

    def _toggle_polling(self):
        if self.driver is None:
            return
        if self.driver._poll_thread is not None:
            self._stop_polling()
        else:
            self._start_polling()

    def _start_polling(self):
        tags = self._selected_tags()
        if not tags:
            messagebox.showerror("No tags checked", "Check at least one tag to monitor.")
            return

        def on_readings(readings: list[TagReading]):
            self._event_queue.put(("readings", readings))
            if self._csv_callback:
                self._csv_callback(readings)

        self.driver.poll(tags, POLL_INTERVAL_SEC, on_readings, blocking=False)
        self.poll_btn.configure(text="Stop monitoring")
        self.status_var.set(f"Monitoring {len(tags)} tag(s) every {POLL_INTERVAL_SEC:g}s")

    def _stop_polling(self):
        if self.driver is not None:
            self.driver.stop_poll()
        self.poll_btn.configure(text="Start monitoring")

    # -- CSV logging ------------------------------------------------------

    def _toggle_csv(self):
        if self._csv_callback:
            self._stop_csv()
        else:
            path = filedialog.asksaveasfilename(
                title="Save CSV log as",
                defaultextension=".csv",
                filetypes=[("CSV files", "*.csv")],
                initialfile=f"plc_log_{datetime.now():%Y%m%d_%H%M%S}.csv",
            )
            if not path:
                return
            self.csv_path = Path(path)
            self._csv_callback = csv_logger(self.csv_path)
            self.csv_btn.configure(text="Stop CSV logging")
            self.csv_status_var.set(f"Logging to {self.csv_path.name}")

    def _stop_csv(self):
        self._csv_callback = None
        self.csv_btn.configure(text="Start CSV logging...")
        self.csv_status_var.set("")

    # -- trend graph ------------------------------------------------------

    @staticmethod
    def _numeric_value(value) -> float | None:
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        return None

    def _record_history(self, reading: TagReading):
        num = self._numeric_value(reading.value)
        if num is None:
            return
        history = self._history.setdefault(reading.tag, deque(maxlen=TREND_HISTORY_LEN))
        history.append((reading.timestamp, num))

    def _redraw_trend_if_dirty(self):
        if self._trend_dirty:
            self._redraw_trend()
            self._trend_dirty = False
        self.after(500, self._redraw_trend_if_dirty)

    def _redraw_trend(self):
        self.trend_ax.clear()
        plotted_tags = []
        for tag in self._selected_tags():
            history = self._history.get(tag)
            if not history:
                continue
            times = [t for t, _ in history]
            values = [v for _, v in history]
            self.trend_ax.plot(times, values, marker="", label=tag, color=self._color_for_tag(tag))
            plotted_tags.append(tag)

        if plotted_tags:
            self.trend_ax.set_title("Live tag trend")
            self.trend_ax.legend(loc="upper left", fontsize="small")
            self.trend_figure.autofmt_xdate()
        else:
            self.trend_ax.set_title("No numeric data yet")
        self.trend_ax.set_xlabel("Time")
        self.trend_ax.set_ylabel("Value")

        if not self.y_auto_var.get():
            y_min = self._parse_float(self.y_min_var.get())
            y_max = self._parse_float(self.y_max_var.get())
            if y_min is not None and y_max is not None and y_min < y_max:
                self.trend_ax.set_ylim(y_min, y_max)

        if not self.x_auto_var.get():
            x_min_ago = self._parse_float(self.x_min_var.get())
            x_max_ago = self._parse_float(self.x_max_var.get())
            if x_min_ago is not None and x_max_ago is not None and x_min_ago > x_max_ago:
                now = datetime.now(timezone.utc)
                self.trend_ax.set_xlim(now - timedelta(seconds=x_min_ago), now - timedelta(seconds=x_max_ago))

        self.trend_figure.tight_layout()
        self.trend_canvas.draw_idle()
        self._rebuild_color_legend(plotted_tags)

    @staticmethod
    def _parse_float(text: str) -> float | None:
        try:
            return float(text)
        except (TypeError, ValueError):
            return None

    # -- background event pump ------------------------------------------------------

    def _drain_queue(self):
        try:
            while True:
                event = self._event_queue.get_nowait()
                kind = event[0]
                if kind == "connected":
                    _, driver, tags = event
                    self.driver = driver
                    self._populate_tags(tags)
                    self.status_var.set(f"Connected to {driver.ip_address} — {len(tags)} tag(s) found")
                    self.connect_btn.configure(state="normal")
                    self.disconnect_btn.configure(state="normal")
                    self.check_all_btn.configure(state="normal")
                    self.check_none_btn.configure(state="normal")
                    self.poll_btn.configure(state="normal")
                    self.csv_btn.configure(state="normal")
                elif kind == "connect_failed":
                    _, err = event
                    self.status_var.set("Not connected")
                    self.connect_btn.configure(state="normal")
                    messagebox.showerror("Connection failed", err)
                elif kind == "readings":
                    _, readings = event
                    now_str = datetime.now().strftime("%H:%M:%S")
                    for r in readings:
                        value_str = r.error if r.error else str(r.value)
                        self._last_values[r.tag] = (value_str, r.type or "", now_str)
                        self._record_history(r)
                        row_id = self._row_ids.get(r.tag)
                        if row_id is not None:
                            self.tree.set(row_id, "value", value_str)
                            self.tree.set(row_id, "type", r.type or "")
                            self.tree.set(row_id, "updated", now_str)
                    self._trend_dirty = True
        except queue.Empty:
            pass
        self.after(150, self._drain_queue)


if __name__ == "__main__":
    app = PLCApp()
    app.mainloop()
