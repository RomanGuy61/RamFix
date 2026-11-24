"""Small shared helpers: platform detection, formatting, privileges, and UI."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from typing import Sequence

# --------------------------------------------------------------------------- #
# Platform / privileges
# --------------------------------------------------------------------------- #


def get_platform() -> str:
    """Return one of ``"linux"``, ``"windows"``, or ``"darwin"``."""
    system = platform.system().lower()
    if system.startswith("linux"):
        return "linux"
    if system.startswith("darwin"):
        return "darwin"
    if system.startswith("windows") or os.name == "nt":
        return "windows"
    return system  # unknown, but callers handle it gracefully


def is_root() -> bool:
    """True when running with administrative privileges."""
    if os.name == "nt":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def command_exists(name: str) -> bool:
    """True if *name* is on PATH."""
    return shutil.which(name) is not None


def privilege_wrap(command: Sequence[str]) -> list[str]:
    """Prefix *command* with ``sudo -n`` when not already privileged.

    ``-n`` keeps everything non-interactive; if sudo needs a password we
    report that to the user instead of hanging waiting for input.
    """
    command = list(command)
    if is_root() or get_platform() == "windows":
        return command
    return ["sudo", "-n", *command]


def run_command(
    command: Sequence[str], *, timeout: int = 30
) -> tuple[int, str, str]:
    """Run *command*, returning ``(returncode, stdout, stderr)``.

    Never raises for ordinary failures (missing binary, timeout, non-zero
    exit); callers inspect the returned tuple instead.
    """
    command = list(command)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, "", f"command not found: {command[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s: {command[0]}"
    except OSError as exc:  # pragma: no cover - unusual OS-level failure
        return 1, "", str(exc)
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


def human_bytes(num: float | int, *, precision: int = 1) -> str:
    """Format a byte count using binary (IEC) units, e.g. ``"1.5 GiB"``."""
    value = float(num)
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.{precision}f} {unit}"
        value /= 1024
    return f"{value:.{precision}f} PiB"  # unreachable, keeps type checkers happy


def bar(percent: float, width: int = 28) -> str:
    """Return a small text progress bar for a 0-100 *percent* value."""
    filled = max(0, min(width, round((percent / 100) * width)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


# --------------------------------------------------------------------------- #
# Interactive helpers
# --------------------------------------------------------------------------- #


def confirm(prompt: str, *, default: bool = False) -> bool:
    """Ask a yes/no question on the terminal."""
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(prompt + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default
    return answer in {"y", "yes"}


# --------------------------------------------------------------------------- #
# Minimal colour support (no third-party dependencies)
# --------------------------------------------------------------------------- #

_ENABLED = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
_CODES = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "cyan": "\033[36m",
}


def style(text: str, name: str) -> str:
    """Wrap *text* in an ANSI style when writing to a TTY."""
    if not _ENABLED or name not in _CODES:
        return text
    return f"{_CODES[name]}{text}{_CODES['reset']}"


def ok(text: str) -> str:
    return style(text, "green")


def warn(text: str) -> str:
    return style(text, "yellow")


def fail(text: str) -> str:
    return style(text, "red")


def heading(text: str) -> str:
    return style(text, "bold")
