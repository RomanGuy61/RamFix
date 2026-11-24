"""Linux swap inspection and configuration.

All mutating operations require root (or passwordless ``sudo -n``). Functions
return ``(ok, message)`` pairs so callers can report failures without having to
catch exceptions.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass

from .utils import command_exists, get_platform, privilege_wrap, run_command

PROC_SWAPS = "/proc/swaps"
SWAPPINESS = "/proc/sys/vm/swappiness"
FSTAB = "/etc/fstab"


@dataclass(frozen=True)
class SwapDevice:
    name: str
    kind: str
    size_kb: int
    used_kb: int
    priority: int


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _privileged(args: list[str], *, timeout: int = 60) -> tuple[int, str, str]:
    """Run *args* with elevated privileges (or a clear failure)."""
    return run_command(privilege_wrap(args), timeout=timeout)


def _linux_only() -> tuple[bool, str] | None:
    if get_platform() != "linux":
        return False, "Swap management is only available on Linux."
    return None


def parse_size(text: str) -> int:
    """Parse a human size like ``2G``, ``512M`` or ``1.5GiB`` into bytes.

    Plain numbers are interpreted as bytes. Raises ``ValueError`` on bad input.
    """
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*([kmgtp]?)(?:i?b)?\s*", text, re.IGNORECASE
    )
    if not match:
        raise ValueError(f"invalid size: {text!r}")
    number = float(match.group(1))
    unit = match.group(2).upper()
    multipliers = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
    return int(number * multipliers[unit])


def get_swap_devices() -> list[SwapDevice]:
    """Parse active swap devices from ``/proc/swaps``."""
    devices: list[SwapDevice] = []
    try:
        with open(PROC_SWAPS, encoding="utf-8") as handle:
            lines = handle.readlines()[1:]
    except OSError:
        return devices
    for line in lines:
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            devices.append(
                SwapDevice(
                    name=parts[0],
                    kind=parts[1],
                    size_kb=int(parts[2]),
                    used_kb=int(parts[3]),
                    priority=int(parts[4]),
                )
            )
        except ValueError:
            continue
    return devices


def get_swappiness() -> int | None:
    """Return the current ``vm.swappiness`` value, or ``None`` if unavailable."""
    try:
        with open(SWAPPINESS, encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def get_uuid(path: str) -> str:
    """Return the filesystem/swap UUID for *path*, or an empty string."""
    if not command_exists("blkid"):
        return ""
    rc, out, _ = run_command(["blkid", "-s", "UUID", "-o", "value", path])
    return out.strip() if rc == 0 else ""


# --------------------------------------------------------------------------- #
# Read-only status
# --------------------------------------------------------------------------- #


def swap_status() -> tuple[bool, str]:
    """Describe all active swap devices and the current swappiness."""
    unavailable = _linux_only()
    if unavailable:
        return unavailable

    devices = get_swap_devices()
    swappiness = get_swappiness()
    lines = []
    if devices:
        for dev in devices:
            used = (dev.used_kb / dev.size_kb * 100) if dev.size_kb else 0
            lines.append(
                f"{dev.name}  [{dev.kind}]  {dev.size_kb // 1024} MiB total, "
                f"{dev.used_kb // 1024} MiB used ({used:.1f}%), priority {dev.priority}"
            )
    else:
        lines.append("No active swap devices.")
    lines.append(f"swappiness: {swappiness if swappiness is not None else 'unknown'}")
    return True, "\n".join(lines)


# --------------------------------------------------------------------------- #
# Swappiness
# --------------------------------------------------------------------------- #


def set_swappiness(value: int) -> tuple[bool, str]:
    """Set ``vm.swappiness`` to *value* (0-200)."""
    unavailable = _linux_only()
    if unavailable:
        return unavailable
    if not 0 <= value <= 200:
        return False, "swappiness must be between 0 and 200."
    rc, _, stderr = _privileged(["sh", "-c", f"echo {value} > {SWAPPINESS}"])
    if rc == 0:
        return True, f"swappiness set to {value}."
    if "sudo" in stderr.lower() or rc == 127:
        return False, f"Root required. Run: sudo sh -c 'echo {value} > {SWAPPINESS}'"
    return False, f"Could not set swappiness: {stderr or 'unknown error'}"


# --------------------------------------------------------------------------- #
# Enable / disable
# --------------------------------------------------------------------------- #


def toggle_swap(enable: bool, target: str = "all") -> tuple[bool, str]:
    """Enable or disable swap globally, or for a single *target* device/file."""
    unavailable = _linux_only()
    if unavailable:
        return unavailable

    action = "swapon" if enable else "swapoff"
    if target == "all":
        args = [action, "-a"]
        description = "all swap devices"
    else:
        target = os.path.abspath(os.path.expanduser(target))
        if not os.path.exists(target):
            return False, f"No such swap target: {target}"
        args = [action, target]
        description = target

    rc, _, stderr = _privileged(args, timeout=90)
    if rc == 0:
        verb = "Enabled" if enable else "Disabled"
        return True, f"{verb} swap for {description}."
    if "sudo" in stderr.lower() or rc == 127:
        return False, f"Root required. Run: sudo {action} {'-a' if target == 'all' else target}"
    return False, f"{action} failed: {stderr or 'unknown error'}"


# --------------------------------------------------------------------------- #
# File allocation helpers
# --------------------------------------------------------------------------- #


def _allocate(path: str, size: int, *, grow: bool) -> tuple[bool, str]:
    """Size a swap file to exactly *size* bytes."""
    if grow and command_exists("fallocate"):
        rc, _, err = _privileged(["fallocate", "-l", str(size), path])
        if rc == 0:
            return True, ""
        # fallocate is unsupported on some filesystems; truncate still works.
    rc, _, err = _privileged(["truncate", "-s", str(size), path])
    if rc == 0:
        return True, ""
    return False, err or "could not resize file"


def create_swapfile(path: str, size: int, *, persist: bool = False) -> tuple[bool, str]:
    """Create, format and enable a new swap file at *path*."""
    unavailable = _linux_only()
    if unavailable:
        return unavailable

    path = os.path.abspath(os.path.expanduser(path))
    if os.path.exists(path):
        return False, f"Refusing to overwrite existing file: {path}"
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        return False, f"Parent directory does not exist: {parent}"
    if size < 1 * 1024**2:
        return False, "Swap file must be at least 1 MiB."

    ok, err = _allocate(path, size, grow=True)
    if not ok:
        return False, f"Could not create swap file: {err}"
    for args in (["chmod", "600", path], ["mkswap", path], ["swapon", path]):
        rc, _, stderr = _privileged(args)
        if rc != 0:
            # Best-effort cleanup of the file we just created.
            _privileged(["rm", "-f", path])
            return False, f"Failed during '{args[0]}': {stderr or 'unknown error'}"

    message = f"Created and enabled {path} ({size // 1024**2} MiB)."
    if persist:
        message += " " + _persist_new(path)
    return True, message


def resize_swapfile(path: str, size: int, *, persist: bool = False) -> tuple[bool, str]:
    """Resize an existing swap file, then reformat and re-enable it."""
    unavailable = _linux_only()
    if unavailable:
        return unavailable

    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        return False, f"Not a regular file: {path}"
    if size < 1 * 1024**2:
        return False, "Swap file must be at least 1 MiB."

    old_size = os.path.getsize(path)
    if old_size == size:
        return True, f"{path} is already that size; nothing to do."
    old_uuid = get_uuid(path)

    rc, _, stderr = _privileged(["swapoff", path])
    if rc != 0:
        return False, f"Could not disable swap on {path}: {stderr or 'unknown error'}"

    ok, err = _allocate(path, size, grow=size > old_size)
    if not ok:
        _privileged(["swapon", path])
        return False, f"Could not resize {path}: {err}. Tried to re-enable original swap."

    rc, _, stderr = _privileged(["mkswap", path])
    if rc != 0:
        _privileged(["swapon", path])
        return False, f"mkswap failed: {stderr or 'unknown error'}. Tried to re-enable original swap."

    rc, _, stderr = _privileged(["swapon", path])
    if rc != 0:
        return False, (
            f"Swap file resized but could not be re-enabled: {stderr or 'unknown error'}. "
            f"Run: sudo swapon {path}"
        )

    new_uuid = get_uuid(path)
    message = (
        f"Resized {path}: {old_size // 1024**2} MiB -> {size // 1024**2} MiB."
    )
    if persist and old_uuid and new_uuid and old_uuid != new_uuid:
        ok, detail = _update_fstab(path, old_uuid, new_uuid)
        message += f" {detail}"
    elif persist:
        message += " No /etc/fstab entry needed updating."
    elif old_uuid and new_uuid and old_uuid != new_uuid:
        message += (
            f" UUID changed ({old_uuid} -> {new_uuid}); if /etc/fstab references the "
            "old UUID, re-run with --persist or edit it manually."
        )
    return True, message


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def _fstab_backup() -> tuple[bool, str]:
    stamp = __import__("datetime").datetime.now().strftime("%Y%m%d%H%M%S")
    backup = f"{FSTAB}.ramfix-{stamp}.bak"
    rc, _, err = _privileged(["cp", "-p", FSTAB, backup])
    if rc == 0:
        return True, backup
    return False, err or "could not back up /etc/fstab"


def _write_root_file(source: str, destination: str) -> tuple[bool, str]:
    rc, _, err = _privileged(["cp", source, destination])
    return (True, "") if rc == 0 else (False, err or f"could not write {destination}")


def _persist_new(path: str) -> str:
    """Append a swap line for *path* to /etc/fstab."""
    if not os.path.isfile(FSTAB):
        return "Could not find /etc/fstab; add the swap entry manually."
    uuid = get_uuid(path)
    if not uuid:
        return "Could not read UUID; add the swap entry manually."
    ok, backup = _fstab_backup()
    if not ok:
        return f"Could not back up /etc/fstab ({backup}); entry not added."
    with open(FSTAB, encoding="utf-8") as handle:
        content = handle.read()
    line = f"UUID={uuid} none swap sw 0 0\n"
    if line in content:
        return "fstab entry already present."
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp:
        tmp.write(content if content.endswith("\n") or not content else content + "\n")
        tmp.write(line)
        tmp_path = tmp.name
    try:
        ok, err = _write_root_file(tmp_path, FSTAB)
    finally:
        os.unlink(tmp_path)
    if ok:
        return f"Added fstab entry (backup: {backup})."
    return f"Could not update /etc/fstab: {err}"


def _update_fstab(path: str, old_uuid: str, new_uuid: str) -> tuple[bool, str]:
    """Replace an old swap UUID in /etc/fstab with the new one."""
    if not os.path.isfile(FSTAB):
        return False, "Could not find /etc/fstab."
    with open(FSTAB, encoding="utf-8") as handle:
        lines = handle.readlines()

    changed = False
    updated = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            fields = stripped.split()
            is_swap = len(fields) >= 3 and fields[1] == "none" and "swap" in fields[2].split(",")
            references = path in fields or old_uuid in line
            if is_swap and references:
                line = line.replace(old_uuid, new_uuid)
                changed = True
        updated.append(line)

    if not changed:
        return False, "No matching /etc/fstab entry found."
    ok, backup = _fstab_backup()
    if not ok:
        return False, f"Could not back up /etc/fstab ({backup}); entry not updated."
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp:
        tmp.writelines(updated)
        tmp_path = tmp.name
    try:
        ok, err = _write_root_file(tmp_path, FSTAB)
    finally:
        os.unlink(tmp_path)
    if ok:
        return True, f"Updated /etc/fstab (backup: {backup})."
    return False, f"Could not update /etc/fstab: {err}"
