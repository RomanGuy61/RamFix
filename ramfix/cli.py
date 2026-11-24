"""Command-line interface for ramfix."""

from __future__ import annotations

import argparse
import sys
import time
from typing import Sequence

import psutil

from . import __version__
from .automation import (
    Guard,
    add_watch,
    config_summary,
    remove_watch,
    set_cooldown,
    set_flag,
    set_threshold,
)
from .cleaner import ACTION_LABELS, CACHE, GC, SWAP, clean
from .compression import (
    compression_status,
    set_zram_algorithm,
    set_zram_size,
    set_zswap,
    set_zswap_compressor,
)
from .limits import clear_all_limits, limits_status, remove_limit, set_limit
from .stats import get_memory_stats, get_top_processes, process_count
from .swap import (
    create_swapfile,
    get_swap_devices,
    get_swappiness,
    parse_size,
    resize_swapfile,
    set_swappiness,
    swap_status,
    toggle_swap,
)
from .utils import (
    bar,
    confirm,
    fail,
    get_platform,
    heading,
    human_bytes,
    ok,
    style,
    warn,
)


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #


def _memory_line(label: str, value: int, total: int, colour: str = "") -> str:
    percent = (value / total * 100) if total else 0.0
    filled = bar(percent)
    text = f"{label:<10} {human_bytes(value):>10} / {human_bytes(total):<10} {filled} {percent:5.1f}%"
    return style(text, colour) if colour else text


def print_memory() -> None:
    """Print a full memory report."""
    s = get_memory_stats()
    print(heading("Memory"))
    print(_memory_line("RAM:", s.used, s.total, "cyan"))
    print(f"{'available':<10} {human_bytes(s.available):>10}")
    print(f"{'free':<10} {human_bytes(s.free):>10}")
    print(f"{'cached':<10} {human_bytes(s.cached):>10}")
    print(f"{'buffers':<10} {human_bytes(s.buffers):>10}")
    print()
    if s.swap_total:
        print(_memory_line("Swap:", s.swap_used, s.swap_total, "yellow"))
    else:
        print(f"Swap:      {warn('none configured')}")
    print(f"Processes: {process_count()}")
    print(f"Platform:  {get_platform()}")


def print_top_processes(limit: int = 10) -> None:
    """Print the processes using the most resident memory."""
    print(heading(f"Top {limit} processes by resident memory"))
    print(f"{'PID':>7}  {'RSS':>10}  {'VMS':>10}  NAME")
    for proc in get_top_processes(limit):
        print(
            f"{proc.pid:>7}  {human_bytes(proc.rss):>10}  "
            f"{human_bytes(proc.vms):>10}  {proc.name}"
        )


def print_results(results, *, before=None, after=None) -> None:
    """Print cleanup outcomes and an approximate before/after comparison."""
    print(heading("Cleanup results"))
    for result in results:
        mark = ok("[ OK ]") if result.ok else fail("[FAIL]")
        print(f"{mark} {result.label}: {result.message}")

    if before is not None and after is not None:
        delta = after.available - before.available
        sign = "+" if delta >= 0 else "-"
        colour = "green" if delta >= 0 else "yellow"
        print()
        print(
            style(
                f"Available RAM: {human_bytes(before.available)} -> "
                f"{human_bytes(after.available)}  "
                f"({sign}{human_bytes(abs(delta))})",
                colour,
            )
        )
        print(
            style(
                "Note: memory usage fluctuates constantly; the change above is "
                "approximate and not all of it is caused by cleanup.",
                "dim",
            )
        )


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_status(_args: argparse.Namespace) -> int:
    print_memory()
    return 0


def cmd_top(args: argparse.Namespace) -> int:
    print_top_processes(args.number)
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    actions: list[str] = []
    if args.all:
        actions = [CACHE, SWAP, GC]
    else:
        if args.cache:
            actions.append(CACHE)
        if args.swap:
            actions.append(SWAP)
        if args.gc:
            actions.append(GC)
    if not actions:
        actions = [CACHE]  # sensible default

    before = get_memory_stats()
    results = clean(actions, assume_yes=args.yes, dry_run=args.dry_run)
    after = get_memory_stats()
    print_results(results, before=before, after=after)

    return 0 if all(r.ok for r in results) else 1


def cmd_monitor(args: argparse.Namespace) -> int:
    """Continuously refresh a compact memory summary."""
    print("Monitoring memory. Press Ctrl-C to stop.")
    try:
        while True:
            s = get_memory_stats()
            line = (
                f"RAM {s.percent:5.1f}%  "
                f"used {human_bytes(s.used):>9} / {human_bytes(s.total)}  "
                f"avail {human_bytes(s.available):>9}  "
                f"swap {s.swap_percent:5.1f}%  "
                f"procs {process_count()}"
            )
            sys.stdout.write("\r" + line + "   ")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        print("Stopped.")
    return 0


def _swap_result(success: bool, message: str) -> int:
    print(ok(message) if success else fail(message))
    return 0 if success else 1


def cmd_swap_status(_args: argparse.Namespace) -> int:
    ok, message = swap_status()
    print(heading("Swap status"))
    print(message)
    return 0 if ok else 1


def cmd_swap_swappiness(args: argparse.Namespace) -> int:
    if args.value is None:
        current = get_swappiness()
        if current is None:
            return _swap_result(False, "Could not read vm.swappiness.")
        return _swap_result(True, f"vm.swappiness is {current}.")
    return _swap_result(*set_swappiness(args.value))


def cmd_swap_toggle(args: argparse.Namespace) -> int:
    return _swap_result(*toggle_swap(args.enable, args.target))


def cmd_swap_create(args: argparse.Namespace) -> int:
    try:
        size = parse_size(args.size)
    except ValueError as exc:
        return _swap_result(False, str(exc))
    if not args.yes and not confirm(
        f"Create a {args.size} swap file at {args.path}? ", default=False
    ):
        return _swap_result(False, "Cancelled.")
    return _swap_result(*create_swapfile(args.path, size, persist=args.persist))


def cmd_swap_resize(args: argparse.Namespace) -> int:
    try:
        size = parse_size(args.size)
    except ValueError as exc:
        return _swap_result(False, str(exc))
    path = args.path
    if path is None:
        devices = [d for d in get_swap_devices() if d.kind == "file"]
        if len(devices) == 1:
            path = devices[0].name
        elif not devices:
            return _swap_result(False, "No file-backed swap device found; specify PATH.")
        else:
            return _swap_result(
                False,
                "Multiple file-backed swap devices found; specify PATH explicitly.",
            )
    if not args.yes and not confirm(
        f"Resize swap file {path} to {args.size}? Swap will be briefly disabled. ",
        default=False,
    ):
        return _swap_result(False, "Cancelled.")
    return _swap_result(*resize_swapfile(path, size, persist=args.persist))


# --------------------------------------------------------------------------- #
# V2.0: compression, per-app limits, automation
# --------------------------------------------------------------------------- #


def cmd_compression_status(_args: argparse.Namespace) -> int:
    success, message = compression_status()
    print(heading("Memory compression"))
    print(message)
    return 0 if success else 1


def cmd_compression_algorithm(args: argparse.Namespace) -> int:
    return _swap_result(*set_zram_algorithm(args.device, args.algorithm))


def cmd_compression_size(args: argparse.Namespace) -> int:
    try:
        size = parse_size(args.size)
    except ValueError as exc:
        return _swap_result(False, str(exc))
    if not args.yes and not confirm(
        f"Resize {args.device} to {args.size}? Its swap will be briefly disabled. ",
        default=False,
    ):
        return _swap_result(False, "Cancelled.")
    return _swap_result(*set_zram_size(args.device, size))


def cmd_compression_zswap(args: argparse.Namespace) -> int:
    return _swap_result(*set_zswap(args.state == "on"))


def cmd_compression_compressor(args: argparse.Namespace) -> int:
    return _swap_result(*set_zswap_compressor(args.compressor))


def cmd_limit_list(_args: argparse.Namespace) -> int:
    success, message = limits_status()
    print(heading("Per-app RAM limits"))
    print(message)
    return 0 if success else 1


def cmd_limit_set(args: argparse.Namespace) -> int:
    try:
        size = parse_size(args.size)
    except ValueError as exc:
        return _swap_result(False, str(exc))
    if not args.yes and not confirm(
        f"Limit PID {args.pid} to {args.size} of RAM? "
        "The kernel may kill the app if it exceeds this. ",
        default=False,
    ):
        return _swap_result(False, "Cancelled.")
    return _swap_result(*set_limit(args.pid, size, args.name))


def cmd_limit_remove(args: argparse.Namespace) -> int:
    return _swap_result(*remove_limit(args.name))


def cmd_limit_clear(args: argparse.Namespace) -> int:
    if not args.yes and not confirm("Remove all per-app RAM limits? ", default=False):
        return _swap_result(False, "Cancelled.")
    return _swap_result(*clear_all_limits())


def cmd_guard_show(_args: argparse.Namespace) -> int:
    print(heading("Automation configuration"))
    print(config_summary())
    return 0


def cmd_guard_threshold(args: argparse.Namespace) -> int:
    return _swap_result(*set_threshold(args.value))


def cmd_guard_cooldown(args: argparse.Namespace) -> int:
    return _swap_result(*set_cooldown(args.value))


def cmd_guard_watch_add(args: argparse.Namespace) -> int:
    return _swap_result(*add_watch(args.name))


def cmd_guard_watch_remove(args: argparse.Namespace) -> int:
    return _swap_result(*remove_watch(args.name))


def cmd_guard_watch_list(_args: argparse.Namespace) -> int:
    return _swap_result(True, config_summary())


def cmd_guard_flag(args: argparse.Namespace) -> int:
    return _swap_result(*set_flag(args.flag, args.value))


def cmd_guard_run(args: argparse.Namespace) -> int:
    guard = Guard(
        interval=args.interval,
        on_event=lambda message: print(f"[guard] {message}", flush=True),
    )
    print("Guard running. Press Ctrl-C to stop.", flush=True)
    try:
        guard.run()
    except KeyboardInterrupt:
        guard.stop()
        print("\nGuard stopped.", flush=True)
    return 0


def cmd_gui(_args: argparse.Namespace) -> int:
    from .gui import launch_gui

    return launch_gui()


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report memory, show top processes, and run safe cleanups."""
    print(heading("ramfix doctor"))
    print()
    print_memory()
    print()
    print_top_processes(5)
    print()

    before = get_memory_stats()
    results = clean([CACHE, GC], assume_yes=args.yes, dry_run=args.dry_run)
    after = get_memory_stats()
    print_results(results, before=before, after=after)

    print()
    print(
        warn(
            "Swap was not cycled. To also clear swap, run: "
            "sudo ramfix clean --swap"
        )
    )
    return 0 if all(r.ok for r in results) else 1


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ramfix",
        description=(
            "Cross-platform memory housekeeping: inspect RAM usage, find "
            "memory-hungry processes, and ask the OS to release cached memory."
        ),
        epilog=(
            "Note: modern operating systems manage memory very well. Dropping "
            "caches frees RAM momentarily but can slow things down until the "
            "cache is rebuilt. Use this tool deliberately, not as a background "
            "tuner."
        ),
    )
    parser.add_argument("--version", action="version", version=f"ramfix {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="show a memory usage report")
    p_status.set_defaults(func=cmd_status)

    p_top = sub.add_parser("top", help="list processes using the most RAM")
    p_top.add_argument("-n", "--number", type=int, default=10, help="how many processes (default: 10)")
    p_top.set_defaults(func=cmd_top)

    p_clean = sub.add_parser("clean", help="release cached / stale memory")
    p_clean.add_argument("--cache", action="store_true", help="drop caches / trim working sets (default)")
    p_clean.add_argument("--swap", action="store_true", help="cycle swap (Linux, needs root)")
    p_clean.add_argument("--gc", action="store_true", help="run Python garbage collection")
    p_clean.add_argument("--all", action="store_true", help="run every cleanup action")
    p_clean.add_argument("--dry-run", action="store_true", help="show what would be done")
    p_clean.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompts")
    p_clean.set_defaults(func=cmd_clean)

    p_monitor = sub.add_parser("monitor", help="live-updating memory summary")
    p_monitor.add_argument("-i", "--interval", type=float, default=2.0, help="refresh seconds (default: 2)")
    p_monitor.set_defaults(func=cmd_monitor)

    p_gui = sub.add_parser("gui", help="launch the Tkinter desktop interface")
    p_gui.set_defaults(func=cmd_gui)
    p_ui = sub.add_parser("ui", help="alias for 'gui'")
    p_ui.set_defaults(func=cmd_gui)

    p_swap = sub.add_parser("swap", help="inspect and change Linux swap")
    swap_sub = p_swap.add_subparsers(dest="swap_command", required=True)

    p_swap_status = swap_sub.add_parser("status", help="show swap devices and swappiness")
    p_swap_status.set_defaults(func=cmd_swap_status)

    p_swap_swappiness = swap_sub.add_parser(
        "swappiness", help="show or set vm.swappiness (0-200)"
    )
    p_swap_swappiness.add_argument(
        "value", nargs="?", type=int, help="new value; omit to read the current one"
    )
    p_swap_swappiness.set_defaults(func=cmd_swap_swappiness)

    p_swap_on = swap_sub.add_parser("on", help="enable swap")
    p_swap_on.add_argument("target", nargs="?", default="all", help="device/file, or 'all'")
    p_swap_on.set_defaults(func=cmd_swap_toggle, enable=True)

    p_swap_off = swap_sub.add_parser("off", help="disable swap")
    p_swap_off.add_argument("target", nargs="?", default="all", help="device/file, or 'all'")
    p_swap_off.set_defaults(func=cmd_swap_toggle, enable=False)

    p_swap_create = swap_sub.add_parser("create", help="create and enable a swap file")
    p_swap_create.add_argument("size", help="size, e.g. 2G, 512M, 1.5GiB")
    p_swap_create.add_argument("path", nargs="?", default="/swapfile", help="file path (default: /swapfile)")
    p_swap_create.add_argument("--persist", action="store_true", help="add an /etc/fstab entry")
    p_swap_create.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    p_swap_create.set_defaults(func=cmd_swap_create)

    p_swap_resize = swap_sub.add_parser("resize", help="resize an existing swap file")
    p_swap_resize.add_argument("size", help="new size, e.g. 4G")
    p_swap_resize.add_argument(
        "path", nargs="?",
        help="file path (default: auto-detect the single file-backed swap device)",
    )
    p_swap_resize.add_argument("--persist", action="store_true", help="update /etc/fstab for the new UUID")
    p_swap_resize.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    p_swap_resize.set_defaults(func=cmd_swap_resize)

    # V2.0 command groups ------------------------------------------------- #
    p_comp = sub.add_parser(
        "compression", help="inspect and change RAM compression (Linux)"
    )
    comp_sub = p_comp.add_subparsers(dest="compression_command", required=True)

    p_comp_status = comp_sub.add_parser("status", help="show zram/zswap state")
    p_comp_status.set_defaults(func=cmd_compression_status)

    p_comp_algo = comp_sub.add_parser("algorithm", help="change a zram compressor")
    p_comp_algo.add_argument("algorithm", help="e.g. zstd, lz4, lzo")
    p_comp_algo.add_argument("-d", "--device", default="zram0", help="zram device (default: zram0)")
    p_comp_algo.set_defaults(func=cmd_compression_algorithm)

    p_comp_size = comp_sub.add_parser("size", help="resize a zram device")
    p_comp_size.add_argument("size", help="new size, e.g. 2G")
    p_comp_size.add_argument("-d", "--device", default="zram0", help="zram device (default: zram0)")
    p_comp_size.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    p_comp_size.set_defaults(func=cmd_compression_size)

    p_comp_zswap = comp_sub.add_parser("zswap", help="enable or disable zswap")
    p_comp_zswap.add_argument("state", choices=("on", "off"))
    p_comp_zswap.set_defaults(func=cmd_compression_zswap)

    p_comp_compressor = comp_sub.add_parser("compressor", help="change the zswap compressor")
    p_comp_compressor.add_argument("compressor", help="e.g. zstd, lz4, lzo")
    p_comp_compressor.set_defaults(func=cmd_compression_compressor)

    p_limit = sub.add_parser("limit", help="cap RAM usage per application (Linux)")
    limit_sub = p_limit.add_subparsers(dest="limit_command", required=True)

    p_limit_list = limit_sub.add_parser("list", help="show configured limits")
    p_limit_list.set_defaults(func=cmd_limit_list)

    p_limit_set = limit_sub.add_parser("set", help="limit a process to an amount of RAM")
    p_limit_set.add_argument("pid", type=int, help="process ID")
    p_limit_set.add_argument("size", help="limit, e.g. 512M")
    p_limit_set.add_argument("--name", default="app", help="friendly name (default: app)")
    p_limit_set.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    p_limit_set.set_defaults(func=cmd_limit_set)

    p_limit_remove = limit_sub.add_parser("remove", help="remove one limit")
    p_limit_remove.add_argument("name", help="limit name")
    p_limit_remove.set_defaults(func=cmd_limit_remove)

    p_limit_clear = limit_sub.add_parser("clear", help="remove all limits")
    p_limit_clear.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    p_limit_clear.set_defaults(func=cmd_limit_clear)

    p_guard = sub.add_parser("guard", help="auto-clean on RAM threshold or app start")
    guard_sub = p_guard.add_subparsers(dest="guard_command", required=True)

    p_guard_show = guard_sub.add_parser("show", help="show automation settings")
    p_guard_show.set_defaults(func=cmd_guard_show)

    p_guard_threshold = guard_sub.add_parser("threshold", help="set RAM threshold (percent)")
    p_guard_threshold.add_argument("value", type=float)
    p_guard_threshold.set_defaults(func=cmd_guard_threshold)

    p_guard_cooldown = guard_sub.add_parser("cooldown", help="set minimum seconds between cleans")
    p_guard_cooldown.add_argument("value", type=float)
    p_guard_cooldown.set_defaults(func=cmd_guard_cooldown)

    p_guard_watch = guard_sub.add_parser("watch", help="manage apps that trigger a clean")
    watch_sub = p_guard_watch.add_subparsers(dest="watch_command", required=True)
    p_watch_add = watch_sub.add_parser("add", help="watch an app")
    p_watch_add.add_argument("name", help="process name, e.g. firefox")
    p_watch_add.set_defaults(func=cmd_guard_watch_add)
    p_watch_remove = watch_sub.add_parser("remove", help="stop watching an app")
    p_watch_remove.add_argument("name")
    p_watch_remove.set_defaults(func=cmd_guard_watch_remove)
    p_watch_list = watch_sub.add_parser("list", help="list watched apps")
    p_watch_list.set_defaults(func=cmd_guard_watch_list)

    p_guard_flag = guard_sub.add_parser("flag", help="enable/disable a trigger")
    p_guard_flag.add_argument("flag", choices=("clean_on_threshold", "clean_on_app_start"))
    p_guard_flag.add_argument(
        "value", type=lambda text: text.lower() in ("on", "1", "true", "yes"),
        help="on or off",
    )
    p_guard_flag.set_defaults(func=cmd_guard_flag)

    p_guard_run = guard_sub.add_parser("run", help="start the monitoring daemon")
    p_guard_run.add_argument("-i", "--interval", type=float, default=2.0, help="poll seconds (default: 2)")
    p_guard_run.set_defaults(func=cmd_guard_run)

    p_doctor = sub.add_parser("doctor", help="full check-up plus safe cleanup")
    p_doctor.add_argument("--dry-run", action="store_true", help="show what would be done")
    p_doctor.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompts")
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except psutil.Error as exc:
        print(fail(f"Error reading process information: {exc}"), file=sys.stderr)
        return 1
