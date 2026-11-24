"""Inspect and change Linux memory compression (zram / zswap).

These are low-level kernel interfaces: every mutating operation needs root and
temporarily swaps the affected device out and back in. Unsupported platforms
get a clear message instead of a crash.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass

from .swap import get_swap_devices
from .utils import get_platform, privilege_wrap, run_command

ZRAM_GLOB = "/sys/block/zram*"
ZSWAP_PARAMETERS = "/sys/module/zswap/parameters"


@dataclass(frozen=True)
class ZramDevice:
    name: str
    disksize: int
    algorithm: str
    algorithms: tuple[str, ...]
    orig: int
    compr: int
    mem_used: int

    @property
    def ratio(self) -> float:
        """Compression ratio (higher is better); 0 when nothing is compressed."""
        if self.compr == 0:
            return 0.0
        return self.orig / self.compr

    @property
    def device(self) -> str:
        return f"/dev/{self.name}"


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _linux_only() -> tuple[bool, str] | None:
    if get_platform() != "linux":
        return False, "Memory compression controls are only available on Linux."
    return None


# --------------------------------------------------------------------------- #
# Inspection
# --------------------------------------------------------------------------- #


def get_zram_devices() -> list[ZramDevice]:
    """Return all zram devices and their compression statistics."""
    devices: list[ZramDevice] = []
    for path in sorted(glob.glob(ZRAM_GLOB)):
        name = os.path.basename(path)
        raw = _read(os.path.join(path, "comp_algorithm"))
        algorithms = tuple(token.strip("[]") for token in raw.split())
        match = re.search(r"\[([^\]]+)\]", raw)
        algorithm = match.group(1) if match else (algorithms[0] if algorithms else "unknown")

        mm_stat = _read(os.path.join(path, "mm_stat")).split()
        numbers = []
        for item in mm_stat[:3]:
            try:
                numbers.append(int(item))
            except ValueError:
                numbers.append(0)
        while len(numbers) < 3:
            numbers.append(0)

        try:
            disksize = int(_read(os.path.join(path, "disksize")) or 0)
        except ValueError:
            disksize = 0

        devices.append(
            ZramDevice(
                name=name,
                disksize=disksize,
                algorithm=algorithm,
                algorithms=algorithms,
                orig=numbers[0],
                compr=numbers[1],
                mem_used=numbers[2],
            )
        )
    return devices


def get_zswap_info() -> dict[str, str]:
    """Return zswap parameter values, or an empty dict when unavailable."""
    keys = ("enabled", "compressor", "max_compression_ratio", "same_filled_pages_enabled")
    info = {}
    for key in keys:
        value = _read(os.path.join(ZSWAP_PARAMETERS, key))
        if value:
            info[key] = value
    return info


def compression_status() -> tuple[bool, str]:
    """Human-readable report of zram and zswap state."""
    unavailable = _linux_only()
    if unavailable:
        return unavailable

    lines: list[str] = []
    devices = get_zram_devices()
    if devices:
        active = {dev.name for dev in get_swap_devices()}
        for dev in devices:
            lines.append(
                f"{dev.name}: algorithm={dev.algorithm}, "
                f"size={dev.disksize // 1024**2} MiB, "
                f"ratio={dev.ratio:.2f}x, "
                f"compressed={dev.compr // 1024**2} MiB / {dev.orig // 1024**2} MiB, "
                f"swap={'on' if dev.name in active or dev.device in active else 'off'}"
            )
            lines.append(f"  available algorithms: {', '.join(dev.algorithms) or 'unknown'}")
    else:
        lines.append("No zram devices found.")

    zswap = get_zswap_info()
    if zswap:
        lines.append("zswap: " + ", ".join(f"{key}={value}" for key, value in zswap.items()))
    else:
        lines.append("zswap: not available (module not loaded?)")
    return True, "\n".join(lines)


# --------------------------------------------------------------------------- #
# Mutation
# --------------------------------------------------------------------------- #


def _run_as_root(script: str) -> tuple[int, str, str]:
    return run_command(privilege_wrap(["sh", "-c", script]), timeout=60)


def _toggle_device_swap(device: str, enable: bool) -> tuple[bool, str]:
    action = "swapon" if enable else "swapoff"
    rc, _, err = _run_as_root(f"{action} {device}")
    return (True, "") if rc == 0 else (False, err or f"{action} failed")


def set_zram_algorithm(name: str, algorithm: str) -> tuple[bool, str]:
    """Change a zram device's compression algorithm (swap cycles briefly)."""
    unavailable = _linux_only()
    if unavailable:
        return unavailable

    sysfs = f"/sys/block/{name}/comp_algorithm"
    if not os.path.isdir(f"/sys/block/{name}"):
        return False, f"No such zram device: {name}"
    raw = _read(sysfs)
    available = [token.strip("[]") for token in raw.split()]
    if algorithm not in available:
        return False, f"Unsupported algorithm '{algorithm}'. Available: {', '.join(available)}"

    device = f"/dev/{name}"
    was_active = any(dev.name in (device, name) for dev in get_swap_devices())
    if was_active:
        ok, err = _toggle_device_swap(device, False)
        if not ok:
            return False, f"Could not disable {device}: {err}"

    rc, _, err = _run_as_root(f"echo {algorithm} > {sysfs}")
    if rc != 0:
        if was_active:
            _toggle_device_swap(device, True)
        return False, f"Could not set algorithm: {err or 'root privileges required?'}"

    if was_active:
        ok, err = _toggle_device_swap(device, True)
        if not ok:
            return False, f"Algorithm changed but could not re-enable swap: {err}"

    return True, f"{name} now uses {algorithm} compression."


def set_zram_size(name: str, size: int) -> tuple[bool, str]:
    """Resize a zram device's backing disk (swap cycles and is reformatted)."""
    unavailable = _linux_only()
    if unavailable:
        return unavailable

    if not os.path.isdir(f"/sys/block/{name}"):
        return False, f"No such zram device: {name}"
    if size < 16 * 1024**2:
        return False, "zram size must be at least 16 MiB."

    device = f"/dev/{name}"
    was_active = any(dev.name in (device, name) for dev in get_swap_devices())
    if was_active:
        ok, err = _toggle_device_swap(device, False)
        if not ok:
            return False, f"Could not disable {device}: {err}"

    rc, _, err = _run_as_root(f"echo {size} > /sys/block/{name}/disksize")
    if rc != 0:
        if was_active:
            _toggle_device_swap(device, True)
        return False, f"Could not set disksize: {err or 'root privileges required?'}"

    if was_active:
        rc, _, err = _run_as_root(f"mkswap {device} && swapon {device}")
        if rc != 0:
            return False, f"Resized but could not restore swap: {err or 'unknown error'}"

    return True, f"{name} resized to {size // 1024**2} MiB."


def set_zswap(enabled: bool) -> tuple[bool, str]:
    """Enable or disable zswap at runtime."""
    unavailable = _linux_only()
    if unavailable:
        return unavailable

    path = os.path.join(ZSWAP_PARAMETERS, "enabled")
    if not os.path.isfile(path):
        return False, "zswap is not available on this kernel."

    rc, _, err = _run_as_root(f"echo {'Y' if enabled else 'N'} > {path}")
    if rc != 0:
        return False, f"Could not change zswap: {err or 'root privileges required?'}"
    return True, f"zswap {'enabled' if enabled else 'disabled'}."


def set_zswap_compressor(compressor: str) -> tuple[bool, str]:
    """Change the zswap compressor (requires zswap support)."""
    unavailable = _linux_only()
    if unavailable:
        return unavailable

    path = os.path.join(ZSWAP_PARAMETERS, "compressor")
    if not os.path.isfile(path):
        return False, "zswap compressor setting is not available."

    # The kernel validates the value itself; the sysfs file only exposes the
    # current compressor, so there is no reliable list to pre-check against.
    rc, _, err = _run_as_root(f"echo {compressor} > {path}")
    if rc != 0:
        return False, f"Could not set compressor: {err or 'root privileges required?'}"
    return True, f"zswap compressor set to {compressor}."
