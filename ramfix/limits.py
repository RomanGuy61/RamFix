"""Per-application RAM limits using Linux cgroup v2 ``memory.max``.

Limits are managed in dedicated cgroups named ``ramfix-<name>`` so they are
easy to list and remove. Setting a limit too low can cause the kernel to
OOM-kill the application, so callers should warn the user first.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import psutil

from .utils import get_platform, privilege_wrap, run_command

PREFIX = "ramfix-"


@dataclass(frozen=True)
class AppLimit:
    name: str
    path: str
    memory_max: int | None  # None means "max" (unlimited)
    pids: tuple[int, ...]


def _linux_only() -> tuple[bool, str] | None:
    if get_platform() != "linux":
        return False, "Per-app RAM limits are only available on Linux (cgroup v2)."
    return None


def cgroup_root() -> str | None:
    """Find the cgroup v2 mount point from ``/proc/mounts``."""
    try:
        with open("/proc/mounts", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 3 and parts[2] == "cgroup2":
                    return parts[1]
    except OSError:
        pass
    return None


def sanitize_name(name: str) -> str:
    """Turn *name* into a safe cgroup name component."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", name.strip()).strip("-")
    return cleaned[:48] or "app"


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _run(script: str) -> tuple[int, str, str]:
    return run_command(privilege_wrap(["sh", "-c", script]), timeout=30)


def list_limits() -> list[AppLimit]:
    """List all cgroups created by ramfix."""
    root = cgroup_root()
    if not root or not os.path.isdir(root):
        return []
    limits: list[AppLimit] = []
    for entry in sorted(os.listdir(root)):
        if not entry.startswith(PREFIX):
            continue
        path = os.path.join(root, entry)
        if not os.path.isdir(path):
            continue
        raw = _read(os.path.join(path, "memory.max"))
        try:
            memory_max = None if raw in ("", "max") else int(raw)
        except ValueError:
            memory_max = None
        pids: list[int] = []
        for token in _read(os.path.join(path, "cgroup.procs")).split():
            try:
                pids.append(int(token))
            except ValueError:
                continue
        limits.append(
            AppLimit(
                name=entry[len(PREFIX) :],
                path=path,
                memory_max=memory_max,
                pids=tuple(pids),
            )
        )
    return limits


def limits_status() -> tuple[bool, str]:
    """Human-readable list of active limits."""
    unavailable = _linux_only()
    if unavailable:
        return unavailable
    root = cgroup_root()
    if not root:
        return False, "cgroup v2 is not mounted on this system."
    limits = list_limits()
    if not limits:
        return True, "No per-app RAM limits are configured."
    from .utils import human_bytes

    lines = []
    for limit in limits:
        amount = "unlimited" if limit.memory_max is None else human_bytes(limit.memory_max)
        lines.append(
            f"{limit.name}: max={amount}, pids={', '.join(map(str, limit.pids)) or 'none'}"
        )
    return True, "\n".join(lines)


def set_limit(pid: int, size: int, name: str = "app") -> tuple[bool, str]:
    """Cap *pid* at *size* bytes of RAM in a cgroup named ``ramfix-<name>``."""
    unavailable = _linux_only()
    if unavailable:
        return unavailable
    root = cgroup_root()
    if not root:
        return False, "cgroup v2 is not mounted on this system."
    if size < 16 * 1024**2:
        return False, "Limit must be at least 16 MiB."
    if not psutil.pid_exists(pid):
        return False, f"No process with PID {pid}."

    safe = sanitize_name(name)
    path = os.path.join(root, PREFIX + safe)

    rc, _, err = _run(f"mkdir -p {path}")
    if rc != 0:
        return False, f"Could not create cgroup {path}: {err or 'root privileges required?'}"

    rc, _, err = _run(f"echo {size} > {path}/memory.max")
    if rc != 0:
        return False, f"Could not set memory.max: {err or 'root privileges required?'}"

    rc, _, err = _run(f"echo {pid} > {path}/cgroup.procs")
    if rc != 0:
        return False, (
            f"Limit created but could not move PID {pid} into it: {err or 'permission denied'}"
        )

    from .utils import human_bytes

    return True, f"PID {pid} ({safe}) limited to {human_bytes(size)} of RAM."


def remove_limit(name: str) -> tuple[bool, str]:
    """Remove a named ramfix cgroup, moving its processes back to the root."""
    unavailable = _linux_only()
    if unavailable:
        return unavailable
    root = cgroup_root()
    if not root:
        return False, "cgroup v2 is not mounted on this system."
    safe = sanitize_name(name)
    path = os.path.join(root, PREFIX + safe)
    if not os.path.isdir(path):
        return False, f"No such limit: {safe}"

    pids = []
    for token in _read(os.path.join(path, "cgroup.procs")).split():
        try:
            pids.append(str(int(token)))
        except ValueError:
            continue
    if pids:
        rc, _, err = _run(
            " && ".join(f"echo {pid} > {root}/cgroup.procs" for pid in pids)
        )
        if rc != 0:
            return False, f"Could not move processes back: {err or 'permission denied'}"

    rc, _, err = _run(f"rmdir {path}")
    if rc != 0:
        return False, f"Could not remove cgroup: {err or 'still in use?'}"
    return True, f"Removed limit '{safe}'."


def clear_all_limits() -> tuple[bool, str]:
    """Remove every ramfix-managed cgroup."""
    unavailable = _linux_only()
    if unavailable:
        return unavailable
    removed = 0
    failures = []
    for limit in list_limits():
        ok, message = remove_limit(limit.name)
        if ok:
            removed += 1
        else:
            failures.append(f"{limit.name}: {message}")
    if failures:
        return False, f"Removed {removed}; failures: " + "; ".join(failures)
    return True, f"Removed {removed} limit(s)."
