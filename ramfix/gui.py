"""Tkinter desktop GUI for ramfix.

Long-running or privileged operations run in worker threads and report back
through ``after()`` so the interface never freezes.
"""

from __future__ import annotations

import datetime as _dt
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter import scrolledtext

from . import __version__
from .automation import (
    Guard,
    add_watch,
    config_summary,
    load_config,
    remove_watch,
    set_cooldown,
    set_flag,
    set_threshold,
)
from .cleaner import CACHE, GC, SWAP, clean_action
from .compression import (
    compression_status,
    get_zram_devices,
    get_zswap_info,
    set_zram_algorithm,
    set_zram_size,
    set_zswap,
    set_zswap_compressor,
)
from .limits import clear_all_limits, limits_status, list_limits, remove_limit, set_limit
from .stats import get_memory_stats, get_top_processes, process_count
from .swap import (
    create_swapfile,
    get_swap_devices,
    get_swappiness,
    parse_size,
    resize_swapfile,
    set_swappiness,
    toggle_swap,
)
from .utils import get_platform, human_bytes


def _timestamp() -> str:
    return _dt.datetime.now().strftime("%H:%M:%S")


class RamFixGUI(tk.Tk):
    """Main application window."""

    def __init__(self) -> None:
        super().__init__()
        self.title(f"RamFix {__version__} - Memory Housekeeping")
        self.geometry("1000x780")
        self.minsize(820, 620)

        self._auto_var = tk.BooleanVar(value=True)
        self._interval_var = tk.DoubleVar(value=2.0)
        self._swappiness_var = tk.IntVar(value=10)
        self._persist_var = tk.BooleanVar(value=False)
        self._timer_id: str | None = None
        self._busy = False

        # V2.0 state ------------------------------------------------------
        config = load_config()
        self._threshold_var = tk.DoubleVar(value=config.threshold)
        self._cooldown_var = tk.DoubleVar(value=config.cooldown)
        self._watch_var = tk.StringVar()
        self._watch_list: tk.Listbox | None = None
        self._guard: Guard | None = None
        self._guard_thread: threading.Thread | None = None
        self._guard_status = tk.StringVar(value="Guard: stopped")
        self._guard_button: ttk.Button | None = None

        self._build_styles()
        self._build_widgets()
        self.refresh()
        self._schedule_refresh()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ #
    # Widget construction
    # ------------------------------------------------------------------ #

    def _build_styles(self) -> None:
        style = ttk.Style()
        for name, colour in (
            ("RAM.Green", "#4caf50"),
            ("RAM.Yellow", "#ffb300"),
            ("RAM.Red", "#e53935"),
        ):
            style.configure(
                f"{name}.Horizontal.TProgressbar",
                background=colour,
                troughcolor="#2b2b2b",
            )

    def _build_widgets(self) -> None:
        # Toolbar -------------------------------------------------------
        toolbar = ttk.Frame(self, padding=(10, 8))
        toolbar.pack(fill="x")

        ttk.Label(
            toolbar, text="RamFix", font=("TkDefaultFont", 15, "bold")
        ).pack(side="left")
        ttk.Separator(toolbar, orient="vertical").pack(side="left", padx=12, fill="y")

        ttk.Button(toolbar, text="Refresh", command=self.refresh).pack(side="left", padx=3)
        ttk.Button(toolbar, text="Clean caches", command=self.clean_caches).pack(side="left", padx=3)
        ttk.Button(toolbar, text="Cycle swap", command=self.cycle_swap).pack(side="left", padx=3)
        ttk.Button(toolbar, text="Doctor", command=self.doctor).pack(side="left", padx=3)

        right = ttk.Frame(toolbar)
        right.pack(side="right")
        ttk.Checkbutton(
            right, text="Auto", variable=self._auto_var, command=self._toggle_auto
        ).pack(side="left")
        ttk.Spinbox(
            right,
            from_=1.0,
            to=30.0,
            increment=1.0,
            width=4,
            textvariable=self._interval_var,
            command=self._toggle_auto,
        ).pack(side="left", padx=4)
        ttk.Label(right, text="s").pack(side="left")

        # Notebook ------------------------------------------------------
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        self.overview = ttk.Frame(self.notebook, padding=12)
        self.processes = ttk.Frame(self.notebook, padding=12)
        self.swap_tab = ttk.Frame(self.notebook, padding=12)
        self.compression_tab = ttk.Frame(self.notebook, padding=12)
        self.limits_tab = ttk.Frame(self.notebook, padding=12)
        self.automation_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.overview, text="Overview")
        self.notebook.add(self.processes, text="Processes")
        self.notebook.add(self.swap_tab, text="Swap")
        self.notebook.add(self.compression_tab, text="Compression")
        self.notebook.add(self.limits_tab, text="App limits")
        self.notebook.add(self.automation_tab, text="Automation")

        self._build_overview()
        self._build_processes()
        self._build_swap_tab()
        self._build_compression_tab()
        self._build_limits_tab()
        self._build_automation_tab()

        # Log -----------------------------------------------------------
        log_frame = ttk.LabelFrame(self, text="Activity", padding=(8, 4))
        log_frame.pack(fill="both", expand=False, padx=10, pady=(0, 6))
        self.log = scrolledtext.ScrolledText(
            log_frame, height=6, wrap="word", state="disabled"
        )
        self.log.pack(fill="both", expand=True)

        # Status bar ----------------------------------------------------
        self.status = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status, anchor="w", padding=(10, 4)).pack(
            fill="x"
        )

    def _build_overview(self) -> None:
        mem = ttk.LabelFrame(self.overview, text="Physical memory", padding=12)
        mem.pack(fill="x")

        self.ram_label = ttk.Label(mem, text="—")
        self.ram_label.pack(anchor="w")
        self.ram_bar = ttk.Progressbar(
            mem, mode="determinate", maximum=100, style="RAM.Green.Horizontal.TProgressbar"
        )
        self.ram_bar.pack(fill="x", pady=6)

        self.mem_details = ttk.Label(mem, text="", justify="left")
        self.mem_details.pack(anchor="w")

        sw = ttk.LabelFrame(self.overview, text="Swap", padding=12)
        sw.pack(fill="x", pady=(12, 0))
        self.swap_label = ttk.Label(sw, text="—")
        self.swap_label.pack(anchor="w")
        self.swap_bar = ttk.Progressbar(
            sw, mode="determinate", maximum=100, style="RAM.Green.Horizontal.TProgressbar"
        )
        self.swap_bar.pack(fill="x", pady=6)
        self.swap_details = ttk.Label(sw, text="", justify="left")
        self.swap_details.pack(anchor="w")

        system = ttk.LabelFrame(self.overview, text="System", padding=12)
        system.pack(fill="x", pady=(12, 0))
        self.system_label = ttk.Label(system, text="", justify="left")
        self.system_label.pack(anchor="w")

        note = ttk.Label(
            self.overview,
            text=(
                "Note: the OS already manages memory well. Dropping caches frees "
                "RAM briefly but can slow things down until the cache is rebuilt."
            ),
            foreground="#666666",
            wraplength=820,
            justify="left",
        )
        note.pack(anchor="w", pady=(16, 0))

    def _build_processes(self) -> None:
        columns = ("pid", "name", "rss", "vms")
        self.tree = ttk.Treeview(
            self.processes, columns=columns, show="headings", height=18
        )
        for key, heading, width in (
            ("pid", "PID", 90),
            ("name", "Name", 360),
            ("rss", "Resident", 150),
            ("vms", "Virtual", 150),
        ):
            self.tree.heading(key, text=heading)
            self.tree.column(key, width=width, anchor="w")
        scrollbar = ttk.Scrollbar(
            self.processes, orient="vertical", command=self.tree.yview
        )
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _build_swap_tab(self) -> None:
        if get_platform() != "linux":
            ttk.Label(
                self.swap_tab,
                text="Swap configuration is only available on Linux.",
                foreground="#666666",
            ).pack(anchor="w")
            return

        # Swappiness ----------------------------------------------------
        sw_frame = ttk.LabelFrame(self.swap_tab, text="Swappiness", padding=12)
        sw_frame.pack(fill="x")
        ttk.Label(
            sw_frame,
            text="Lower values keep data in RAM longer; higher values swap sooner.",
            wraplength=800,
        ).pack(anchor="w")
        scale_frame = ttk.Frame(sw_frame)
        scale_frame.pack(fill="x", pady=8)
        self.swappiness_scale = ttk.Scale(
            scale_frame,
            from_=0,
            to=200,
            orient="horizontal",
            variable=self._swappiness_var,
            command=self._on_scale_change,
        )
        self.swappiness_scale.pack(side="left", fill="x", expand=True)
        self.swappiness_value = ttk.Label(scale_frame, text="0", width=4)
        self.swappiness_value.pack(side="left", padx=8)
        ttk.Button(
            sw_frame, text="Apply swappiness", command=self.apply_swappiness
        ).pack(anchor="w")

        # Devices -------------------------------------------------------
        dev_frame = ttk.LabelFrame(self.swap_tab, text="Active swap devices", padding=12)
        dev_frame.pack(fill="x", pady=(12, 0))
        self.swap_tree = ttk.Treeview(
            dev_frame,
            columns=("name", "kind", "size", "used"),
            show="headings",
            height=5,
        )
        for key, heading, width in (
            ("name", "Device", 280),
            ("kind", "Type", 120),
            ("size", "Size", 140),
            ("used", "Used", 140),
        ):
            self.swap_tree.heading(key, text=heading)
            self.swap_tree.column(key, width=width, anchor="w")
        self.swap_tree.pack(fill="x")
        toggle_frame = ttk.Frame(dev_frame)
        toggle_frame.pack(fill="x", pady=(8, 0))
        ttk.Button(toggle_frame, text="Enable all", command=self.swap_on).pack(side="left", padx=(0, 6))
        ttk.Button(toggle_frame, text="Disable all", command=self.swap_off).pack(side="left")

        # Swap file -----------------------------------------------------
        file_frame = ttk.LabelFrame(self.swap_tab, text="Swap file", padding=12)
        file_frame.pack(fill="x", pady=(12, 0))
        entries = ttk.Frame(file_frame)
        entries.pack(fill="x")
        ttk.Label(entries, text="Path:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        self.swap_path_var = tk.StringVar(value="/swapfile")
        ttk.Entry(entries, textvariable=self.swap_path_var, width=40).grid(
            row=0, column=1, sticky="ew", pady=4
        )
        ttk.Label(entries, text="Size:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=4)
        self.swap_size_var = tk.StringVar(value="2G")
        ttk.Entry(entries, textvariable=self.swap_size_var, width=12).grid(
            row=1, column=1, sticky="w", pady=4
        )
        entries.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            file_frame,
            text="Update /etc/fstab (backed up first)",
            variable=self._persist_var,
        ).pack(anchor="w", pady=(8, 4))
        buttons = ttk.Frame(file_frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Create", command=self.create_swap).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Resize", command=self.resize_swap).pack(side="left")

    # ------------------------------------------------------------------ #
    # V2.0 tabs
    # ------------------------------------------------------------------ #

    def _build_compression_tab(self) -> None:
        if get_platform() != "linux":
            ttk.Label(
                self.compression_tab,
                text="Memory compression controls are only available on Linux.",
                foreground="#666666",
            ).pack(anchor="w")
            return

        report = ttk.LabelFrame(self.compression_tab, text="Compression status", padding=10)
        report.pack(fill="x")
        self.compression_text = tk.Text(report, height=5, wrap="word", state="disabled")
        self.compression_text.pack(fill="x")

        zram = ttk.LabelFrame(self.compression_tab, text="zram device", padding=12)
        zram.pack(fill="x", pady=(12, 0))
        self.zram_device_var = tk.StringVar(value="zram0")
        self.zram_algo_var = tk.StringVar()
        self.zram_size_var = tk.StringVar(value="2G")

        grid = ttk.Frame(zram)
        grid.pack(fill="x")
        ttk.Label(grid, text="Device:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        self.zram_device_combo = ttk.Combobox(
            grid, textvariable=self.zram_device_var, width=16, state="readonly"
        )
        self.zram_device_combo.grid(row=0, column=1, sticky="w", pady=4)
        ttk.Label(grid, text="Algorithm:").grid(row=0, column=2, sticky="w", padx=(12, 6), pady=4)
        self.zram_algo_combo = ttk.Combobox(grid, textvariable=self.zram_algo_var, width=14)
        self.zram_algo_combo.grid(row=0, column=3, sticky="w", pady=4)
        ttk.Label(grid, text="Size:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=4)
        ttk.Entry(grid, textvariable=self.zram_size_var, width=12).grid(row=1, column=1, sticky="w", pady=4)

        actions = ttk.Frame(zram)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="Apply algorithm", command=self.apply_zram_algorithm).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="Resize zram", command=self.apply_zram_size).pack(side="left")
        ttk.Label(
            zram,
            text="Changing an algorithm or size briefly disables swap on that device (root required).",
            foreground="#666666",
        ).pack(anchor="w", pady=(8, 0))

        zswap = ttk.LabelFrame(self.compression_tab, text="zswap", padding=12)
        zswap.pack(fill="x", pady=(12, 0))
        self.zswap_label = ttk.Label(zswap, text="")
        self.zswap_label.pack(anchor="w")
        zswap_actions = ttk.Frame(zswap)
        zswap_actions.pack(fill="x", pady=(8, 0))
        ttk.Button(zswap_actions, text="Enable", command=lambda: self.apply_zswap(True)).pack(side="left", padx=(0, 6))
        ttk.Button(zswap_actions, text="Disable", command=lambda: self.apply_zswap(False)).pack(side="left", padx=(0, 12))
        ttk.Label(zswap_actions, text="Compressor:").pack(side="left", padx=(0, 6))
        self.zswap_compressor_var = tk.StringVar(value="lzo")
        ttk.Entry(zswap_actions, textvariable=self.zswap_compressor_var, width=12).pack(side="left", padx=(0, 6))
        ttk.Button(zswap_actions, text="Set", command=self.apply_zswap_compressor).pack(side="left")

    def _build_limits_tab(self) -> None:
        if get_platform() != "linux":
            ttk.Label(
                self.limits_tab,
                text="Per-app RAM limits are only available on Linux (cgroup v2).",
                foreground="#666666",
            ).pack(anchor="w")
            return

        tree_frame = ttk.LabelFrame(self.limits_tab, text="Active limits", padding=10)
        tree_frame.pack(fill="both", expand=True)
        self.limits_tree = ttk.Treeview(
            tree_frame, columns=("name", "max", "pids"), show="headings", height=8
        )
        for key, heading, width in (
            ("name", "Name", 200),
            ("max", "RAM limit", 180),
            ("pids", "PIDs", 220),
        ):
            self.limits_tree.heading(key, text=heading)
            self.limits_tree.column(key, width=width, anchor="w")
        self.limits_tree.pack(fill="both", expand=True)

        controls = ttk.Frame(tree_frame)
        controls.pack(fill="x", pady=(8, 0))
        ttk.Button(controls, text="Refresh", command=self.refresh_limits).pack(side="left", padx=(0, 6))
        ttk.Button(controls, text="Remove selected", command=self.remove_selected_limit).pack(side="left", padx=(0, 6))
        ttk.Button(controls, text="Remove all", command=self.clear_limits).pack(side="left")

        form = ttk.LabelFrame(self.limits_tab, text="Add a limit", padding=12)
        form.pack(fill="x", pady=(12, 0))
        grid = ttk.Frame(form)
        grid.pack(fill="x")
        ttk.Label(grid, text="PID:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        self.limit_pid_var = tk.StringVar()
        ttk.Entry(grid, textvariable=self.limit_pid_var, width=10).grid(row=0, column=1, sticky="w", pady=4)
        ttk.Label(grid, text="RAM limit:").grid(row=0, column=2, sticky="w", padx=(12, 6), pady=4)
        self.limit_size_var = tk.StringVar(value="512M")
        ttk.Entry(grid, textvariable=self.limit_size_var, width=12).grid(row=0, column=3, sticky="w", pady=4)
        ttk.Label(grid, text="Name:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=4)
        self.limit_name_var = tk.StringVar(value="app")
        ttk.Entry(grid, textvariable=self.limit_name_var, width=16).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Button(grid, text="Apply limit", command=self.apply_limit).grid(
            row=1, column=2, columnspan=2, sticky="w", padx=(12, 0), pady=4
        )
        ttk.Label(
            form,
            text="Warning: a limit that is too low can cause the kernel to kill the app.",
            foreground="#b71c1c",
        ).pack(anchor="w", pady=(8, 0))

    def _build_automation_tab(self) -> None:
        settings = ttk.LabelFrame(self.automation_tab, text="Automatic cleaning", padding=12)
        settings.pack(fill="x")

        row = ttk.Frame(settings)
        row.pack(fill="x")
        ttk.Label(row, text="RAM threshold:").pack(side="left", padx=(0, 6))
        ttk.Spinbox(
            row, from_=1, to=100, increment=1, width=6, textvariable=self._threshold_var
        ).pack(side="left", padx=(0, 4))
        ttk.Label(row, text="%").pack(side="left", padx=(0, 12))
        ttk.Label(row, text="Cooldown:").pack(side="left", padx=(0, 6))
        ttk.Spinbox(
            row, from_=0, to=3600, increment=10, width=6, textvariable=self._cooldown_var
        ).pack(side="left", padx=(0, 4))
        ttk.Label(row, text="s").pack(side="left", padx=(0, 12))
        ttk.Button(row, text="Apply", command=self.apply_guard_settings).pack(side="left")

        flags = ttk.Frame(settings)
        flags.pack(fill="x", pady=(10, 0))
        cfg = load_config()
        self._flag_threshold_var = tk.BooleanVar(value=cfg.clean_on_threshold)
        self._flag_app_var = tk.BooleanVar(value=cfg.clean_on_app_start)
        ttk.Checkbutton(
            flags,
            text="Clean when RAM threshold is reached",
            variable=self._flag_threshold_var,
            command=self._toggle_threshold_flag,
        ).pack(anchor="w")
        ttk.Checkbutton(
            flags,
            text="Clean when a watched app starts",
            variable=self._flag_app_var,
            command=self._toggle_app_flag,
        ).pack(anchor="w")

        watch = ttk.LabelFrame(self.automation_tab, text="Watched applications", padding=12)
        watch.pack(fill="both", expand=True, pady=(12, 0))
        self._watch_list = tk.Listbox(watch, height=6)
        self._watch_list.pack(fill="both", expand=True)
        entry_row = ttk.Frame(watch)
        entry_row.pack(fill="x", pady=(8, 0))
        ttk.Entry(entry_row, textvariable=self._watch_var, width=30).pack(side="left", padx=(0, 6))
        ttk.Button(entry_row, text="Add", command=self.add_watched_app).pack(side="left", padx=(0, 6))
        ttk.Button(entry_row, text="Remove", command=self.remove_watched_app).pack(side="left")

        runner = ttk.LabelFrame(self.automation_tab, text="Guard daemon", padding=12)
        runner.pack(fill="x", pady=(12, 0))
        ttk.Label(runner, textvariable=self._guard_status).pack(anchor="w")
        ttk.Label(
            runner,
            text="Cleaning needs root: run this app with sudo, or configure passwordless sudo.",
            foreground="#666666",
        ).pack(anchor="w", pady=(4, 0))
        self._guard_button = ttk.Button(runner, text="Start guard", command=self.toggle_guard)
        self._guard_button.pack(anchor="w", pady=(8, 0))
        self._refresh_watch_list()

    @staticmethod
    def _set_readonly_text(widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    # ------------------------------------------------------------------ #
    # Logging / async helpers
    # ------------------------------------------------------------------ #

    def _log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"[{_timestamp()}] {message}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _status(self, message: str) -> None:
        self.status.set(message)

    def _run(self, function, on_done, *args, **kwargs) -> None:
        """Run *function* in a worker thread, then call *on_done* on the UI thread."""
        if self._busy:
            self._log("Another operation is still running.")
            return
        self._busy = True
        self._status("Working...")

        def worker() -> None:
            try:
                result = function(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - report any failure to the user
                result = exc
            self.after(0, lambda: self._finish(on_done, result))

        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, on_done, result) -> None:
        self._busy = False
        self._status("Ready")
        on_done(result)

    @staticmethod
    def _set_bar(bar_widget: ttk.Progressbar, percent: float) -> None:
        style = "RAM.Green" if percent < 60 else "RAM.Yellow" if percent < 85 else "RAM.Red"
        bar_widget.configure(style=f"{style}.Horizontal.TProgressbar", value=percent)

    # ------------------------------------------------------------------ #
    # Refresh
    # ------------------------------------------------------------------ #

    def refresh(self) -> None:
        """Refresh the overview, process list and swap tab immediately."""
        try:
            stats = get_memory_stats()
        except Exception as exc:  # noqa: BLE001
            self._log(f"Could not read memory stats: {exc}")
            return

        self.ram_label.configure(
            text=f"RAM in use: {human_bytes(stats.used)} of {human_bytes(stats.total)}  ({stats.percent:.1f}%)"
        )
        self._set_bar(self.ram_bar, stats.percent)
        self.mem_details.configure(
            text=(
                f"Available: {human_bytes(stats.available)}    "
                f"Free: {human_bytes(stats.free)}    "
                f"Cached: {human_bytes(stats.cached)}    "
                f"Buffers: {human_bytes(stats.buffers)}"
            )
        )

        if stats.swap_total:
            self.swap_label.configure(
                text=f"Swap in use: {human_bytes(stats.swap_used)} of {human_bytes(stats.swap_total)}  ({stats.swap_percent:.1f}%)"
            )
            self._set_bar(self.swap_bar, stats.swap_percent)
        else:
            self.swap_label.configure(text="Swap: none configured")
            self._set_bar(self.swap_bar, 0)
        self.swap_details.configure(text=f"swappiness: {get_swappiness() if get_platform() == 'linux' else 'n/a'}")

        self.system_label.configure(
            text=f"Platform: {get_platform()}    Processes: {process_count()}"
        )

        try:
            processes = get_top_processes(15)
        except Exception as exc:  # noqa: BLE001
            self._log(f"Could not list processes: {exc}")
            processes = []
        self.tree.delete(*self.tree.get_children())
        for proc in processes:
            self.tree.insert(
                "",
                "end",
                values=(proc.pid, proc.name, proc.rss_human, human_bytes(proc.vms)),
            )

        if get_platform() == "linux":
            self._refresh_swap_devices()
            self.refresh_compression()
            self.refresh_limits()

    def _refresh_swap_devices(self) -> None:
        self.swap_tree.delete(*self.swap_tree.get_children())
        for dev in get_swap_devices():
            self.swap_tree.insert(
                "",
                "end",
                values=(
                    dev.name,
                    dev.kind,
                    human_bytes(dev.size_kb * 1024),
                    human_bytes(dev.used_kb * 1024),
                ),
            )
        current = get_swappiness()
        if current is not None:
            self._swappiness_var.set(current)
            self.swappiness_value.configure(text=str(current))

    def _schedule_refresh(self) -> None:
        if self._timer_id is not None:
            self.after_cancel(self._timer_id)
            self._timer_id = None
        if self._auto_var.get():
            delay = max(1, int(float(self._interval_var.get()) * 1000))
            self._timer_id = self.after(delay, self._auto_tick)

    def _auto_tick(self) -> None:
        self._timer_id = None
        if self._auto_var.get():
            self.refresh()
            self._schedule_refresh()

    def _toggle_auto(self) -> None:
        self._schedule_refresh()

    def _on_scale_change(self, _value=None) -> None:
        self.swappiness_value.configure(text=str(int(float(self._swappiness_var.get()))))

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #

    def clean_caches(self) -> None:
        self._log("Cleaning caches...")
        self._run(lambda: clean_action(CACHE), self._on_clean_done)

    def cycle_swap(self) -> None:
        if not messagebox.askyesno(
            "Cycle swap",
            "Disable and re-enable all swap? This can briefly slow the system "
            "and needs root privileges.",
        ):
            return
        self._log("Cycling swap...")
        self._run(lambda: clean_action(SWAP), self._on_clean_done)

    def doctor(self) -> None:
        def work():
            return [clean_action(CACHE), clean_action(GC)]

        self._log("Running doctor (cache + garbage collection)...")
        self._run(work, self._on_doctor_done)

    def _on_clean_done(self, result) -> None:
        self._log_result(result)
        self.refresh()

    def _on_doctor_done(self, results) -> None:
        if isinstance(results, Exception):
            self._log(f"Doctor failed: {results}")
        else:
            for result in results:
                self._log_result(result)
        self.refresh()

    def _log_result(self, result) -> None:
        if isinstance(result, Exception):
            self._log(f"Error: {result}")
            return
        prefix = "OK" if result.ok else "FAIL"
        self._log(f"[{prefix}] {result.label}: {result.message}")

    def apply_swappiness(self) -> None:
        value = int(float(self._swappiness_var.get()))
        self._log(f"Setting swappiness to {value}...")
        self._run(lambda: set_swappiness(value), self._on_swap_done)

    def swap_on(self) -> None:
        self._log("Enabling all swap...")
        self._run(lambda: toggle_swap(True), self._on_swap_done)

    def swap_off(self) -> None:
        if not messagebox.askyesno(
            "Disable swap", "Disable all swap devices? Memory pressure may rise."
        ):
            return
        self._log("Disabling all swap...")
        self._run(lambda: toggle_swap(False), self._on_swap_done)

    def create_swap(self) -> None:
        try:
            size = parse_size(self.swap_size_var.get())
        except ValueError as exc:
            messagebox.showerror("Invalid size", str(exc))
            return
        path = self.swap_path_var.get().strip() or "/swapfile"
        if not messagebox.askyesno(
            "Create swap file", f"Create a {self.swap_size_var.get()} swap file at {path}?"
        ):
            return
        persist = self._persist_var.get()
        self._log(f"Creating swap file {path}...")
        self._run(
            lambda: create_swapfile(path, size, persist=persist),
            self._on_swap_done,
        )

    def resize_swap(self) -> None:
        try:
            size = parse_size(self.swap_size_var.get())
        except ValueError as exc:
            messagebox.showerror("Invalid size", str(exc))
            return
        path = self.swap_path_var.get().strip() or "/swapfile"
        if not messagebox.askyesno(
            "Resize swap file",
            f"Resize {path} to {self.swap_size_var.get()}? Swap will be briefly disabled.",
        ):
            return
        persist = self._persist_var.get()
        self._log(f"Resizing swap file {path}...")
        self._run(
            lambda: resize_swapfile(path, size, persist=persist),
            self._on_swap_done,
        )

    def _on_swap_done(self, result) -> None:
        if isinstance(result, tuple) and len(result) == 2:
            success, message = result
            self._log(f"[{'OK' if success else 'FAIL'}] {message}")
        elif isinstance(result, Exception):
            self._log(f"Error: {result}")
        elif isinstance(result, list):
            for item in result:
                self._log_result(item)
        else:
            self._log(str(result))
        self.refresh()

    # ------------------------------------------------------------------ #
    # V2.0 actions
    # ------------------------------------------------------------------ #

    def refresh_compression(self) -> None:
        if not hasattr(self, "compression_text"):
            return
        success, message = compression_status()
        self._set_readonly_text(self.compression_text, message if success else message)
        devices = get_zram_devices()
        names = [dev.name for dev in devices]
        self.zram_device_combo.configure(values=names)
        if names and self.zram_device_var.get() not in names:
            self.zram_device_var.set(names[0])
        current = next((dev for dev in devices if dev.name == self.zram_device_var.get()), None)
        if current:
            self.zram_algo_combo.configure(values=list(current.algorithms))
            self.zram_algo_var.set(current.algorithm)
        zswap = get_zswap_info()
        self.zswap_label.configure(
            text="Global zswap: " + (", ".join(f"{k}={v}" for k, v in zswap.items()) or "not available")
        )

    def apply_zram_algorithm(self) -> None:
        device = self.zram_device_var.get().strip() or "zram0"
        algorithm = self.zram_algo_var.get().strip()
        if not algorithm:
            messagebox.showerror("Missing value", "Choose a compression algorithm.")
            return
        self._log(f"Changing {device} algorithm to {algorithm}...")
        self._run(
            lambda: set_zram_algorithm(device, algorithm),
            self._on_swap_done,
        )

    def apply_zram_size(self) -> None:
        try:
            size = parse_size(self.zram_size_var.get())
        except ValueError as exc:
            messagebox.showerror("Invalid size", str(exc))
            return
        device = self.zram_device_var.get().strip() or "zram0"
        if not messagebox.askyesno(
            "Resize zram", f"Resize {device} to {self.zram_size_var.get()}? Swap will be briefly disabled."
        ):
            return
        self._log(f"Resizing {device} to {self.zram_size_var.get()}...")
        self._run(lambda: set_zram_size(device, size), self._on_swap_done)

    def apply_zswap(self, enabled: bool) -> None:
        self._log(f"{'Enabling' if enabled else 'Disabling'} zswap...")
        self._run(lambda: set_zswap(enabled), self._on_swap_done)

    def apply_zswap_compressor(self) -> None:
        compressor = self.zswap_compressor_var.get().strip()
        if not compressor:
            messagebox.showerror("Missing value", "Enter a compressor name.")
            return
        self._log(f"Setting zswap compressor to {compressor}...")
        self._run(lambda: set_zswap_compressor(compressor), self._on_swap_done)

    def refresh_limits(self) -> None:
        if not hasattr(self, "limits_tree"):
            return
        self.limits_tree.delete(*self.limits_tree.get_children())
        success, message = limits_status()
        if not success:
            self._log(message)
            return
        from .utils import human_bytes

        for limit in list_limits():
            amount = "unlimited" if limit.memory_max is None else human_bytes(limit.memory_max)
            self.limits_tree.insert(
                "", "end", values=(limit.name, amount, ", ".join(map(str, limit.pids)) or "none")
            )

    def apply_limit(self) -> None:
        try:
            pid = int(self.limit_pid_var.get().strip())
            size = parse_size(self.limit_size_var.get())
        except (ValueError, TypeError) as exc:
            messagebox.showerror("Invalid input", f"PID must be a number and size valid: {exc}")
            return
        name = self.limit_name_var.get().strip() or "app"
        if not messagebox.askyesno(
            "Apply RAM limit",
            f"Limit PID {pid} to {self.limit_size_var.get()}? The kernel may kill it if it exceeds this.",
        ):
            return
        self._log(f"Limiting PID {pid} to {self.limit_size_var.get()}...")
        self._run(lambda: set_limit(pid, size, name), self._on_swap_done)

    def remove_selected_limit(self) -> None:
        selection = self.limits_tree.selection()
        if not selection:
            messagebox.showinfo("No selection", "Select a limit in the list first.")
            return
        name = self.limits_tree.item(selection[0], "values")[0]
        self._run(lambda: remove_limit(name), self._on_swap_done)

    def clear_limits(self) -> None:
        if not messagebox.askyesno("Remove all", "Remove every per-app RAM limit?"):
            return
        self._run(clear_all_limits, self._on_swap_done)

    def apply_guard_settings(self) -> None:
        threshold = self._threshold_var.get()
        cooldown = self._cooldown_var.get()
        ok1, msg1 = set_threshold(threshold)
        ok2, msg2 = set_cooldown(cooldown)
        self._log(msg1)
        self._log(msg2)
        if ok1 and ok2:
            messagebox.showinfo("Saved", "Automation settings saved.")

    def _toggle_threshold_flag(self) -> None:
        ok, message = set_flag("clean_on_threshold", self._flag_threshold_var.get())
        self._log(message if ok else f"Error: {message}")

    def _toggle_app_flag(self) -> None:
        ok, message = set_flag("clean_on_app_start", self._flag_app_var.get())
        self._log(message if ok else f"Error: {message}")

    def _refresh_watch_list(self) -> None:
        if self._watch_list is None:
            return
        self._watch_list.delete(0, "end")
        for name in load_config().watch_apps:
            self._watch_list.insert("end", name)

    def add_watched_app(self) -> None:
        name = self._watch_var.get().strip()
        ok, message = add_watch(name)
        self._log(message if ok else f"Error: {message}")
        if ok:
            self._watch_var.set("")
            self._refresh_watch_list()

    def remove_watched_app(self) -> None:
        selection = self._watch_list.curselection() if self._watch_list else ()
        if not selection:
            messagebox.showinfo("No selection", "Select an app in the list first.")
            return
        name = self._watch_list.get(selection[0])
        ok, message = remove_watch(name)
        self._log(message if ok else f"Error: {message}")
        if ok:
            self._refresh_watch_list()

    def toggle_guard(self) -> None:
        if self._guard is not None:
            self._guard.stop()
            self._guard = None
            self._guard_status.set("Guard: stopping...")
            if self._guard_button:
                self._guard_button.configure(text="Start guard", state="disabled")
            return

        guard = Guard(on_event=lambda message: self.after(0, lambda m=message: self._log(m)))
        self._guard = guard
        self._guard_status.set("Guard: running")
        if self._guard_button:
            self._guard_button.configure(text="Stop guard")

        def loop() -> None:
            guard.run()
            self.after(0, self._guard_finished)

        self._guard_thread = threading.Thread(target=loop, daemon=True)
        self._guard_thread.start()

    def _guard_finished(self) -> None:
        self._guard = None
        self._guard_status.set("Guard: stopped")
        if self._guard_button:
            self._guard_button.configure(state="normal")

    def _on_close(self) -> None:
        if self._timer_id is not None:
            self.after_cancel(self._timer_id)
        if self._guard is not None:
            self._guard.stop()
            self._guard = None
        self.destroy()


def launch_gui() -> int:
    """Start the GUI, returning a shell exit code."""
    try:
        app = RamFixGUI()
    except tk.TclError as exc:
        print(f"Cannot start the GUI: {exc}", file=sys.stderr)
        print("Check that a display is available (DISPLAY on Linux).", file=sys.stderr)
        return 1
    app.mainloop()
    return 0
