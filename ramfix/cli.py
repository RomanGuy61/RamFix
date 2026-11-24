"""Command-line interface for ramfix."""

from __future__ import annotations

import argparse
import sys
import time
from typing import Sequence

import psutil

from . import __version__
from .cleaner import ACTION_LABELS, CACHE, GC, SWAP, clean
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
