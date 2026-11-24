"""Platform-specific memory cleanup actions.

Every action returns a :class:`CleanResult` describing what happened. Nothing
here kills processes or changes persistent system settings: it only asks the
OS to release memory it is already holding onto.
"""

from __future__ import annotations

import gc
from dataclasses import dataclass

from .utils import (
    command_exists,
    confirm,
    get_platform,
    is_root,
    privilege_wrap,
    run_command,
)

CACHE = "cache"
SWAP = "swap"
GC = "gc"

ACTION_LABELS = {
    CACHE: "Drop filesystem caches / trim working sets",
    SWAP: "Cycle swap (swapoff + swapon)",
    GC: "Run Python garbage collection (this app only)",
}


@dataclass
class CleanResult:
    """Outcome of a single cleanup action."""

    action: str
    ok: bool
    message: str

    @property
    def label(self) -> str:
        return ACTION_LABELS.get(self.action, self.action)


# --------------------------------------------------------------------------- #
# Linux
# --------------------------------------------------------------------------- #


def _linux_drop_caches() -> CleanResult:
    """Ask the Linux kernel to drop pagecache, dentries and inodes."""
    script = "sync && echo 3 > /proc/sys/vm/drop_caches"
    command = privilege_wrap(["sh", "-c", script])
    rc, _, stderr = run_command(command)
    if rc == 0:
        return CleanResult(
            CACHE, True, "Dropped pagecache, dentries and inodes."
        )
    if rc == 127 or "sudo" in stderr.lower() or "not allowed" in stderr.lower():
        return CleanResult(
            CACHE,
            False,
            "Root privileges required. Re-run with: sudo ramfix clean --cache",
        )
    return CleanResult(CACHE, False, f"Failed to drop caches: {stderr or 'unknown error'}")


def _linux_cycle_swap() -> CleanResult:
    """Disable and re-enable all swap devices to clear stale swap contents."""
    if not command_exists("swapoff") or not command_exists("swapon"):
        return CleanResult(SWAP, False, "swapoff/swapon utilities not found.")
    command = privilege_wrap(["sh", "-c", "swapoff -a && swapon -a"])
    rc, _, stderr = run_command(command, timeout=60)
    if rc == 0:
        return CleanResult(SWAP, True, "Swap cleared and re-enabled.")
    if rc == 127 or "sudo" in stderr.lower():
        return CleanResult(
            SWAP,
            False,
            "Root privileges required. Re-run with: sudo ramfix clean --swap",
        )
    return CleanResult(
        SWAP,
        False,
        f"Could not cycle swap (free RAM may be too low): {stderr or 'unknown error'}",
    )


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #


def _windows_enable_debug_privilege() -> bool:
    """Best-effort enable of SeDebugPrivilege so other processes can be trimmed."""
    if not is_root():
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [
                ("PrivilegeCount", wintypes.DWORD),
                ("Privileges", LUID_AND_ATTRIBUTES * 1),
            ]

        TOKEN_ADJUST_PRIVILEGES = 0x0020
        TOKEN_QUERY = 0x0008
        SE_PRIVILEGE_ENABLED = 0x0002

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        token = wintypes.HANDLE()
        if not kernel32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
            ctypes.byref(token),
        ):
            return False

        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(luid)):
            kernel32.CloseHandle(token)
            return False

        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        result = advapi32.AdjustTokenPrivileges(
            token, False, ctypes.byref(tp), ctypes.sizeof(tp), None, None
        )
        kernel32.CloseHandle(token)
        return bool(result and ctypes.get_last_error() == 0)
    except Exception:
        return False


def _windows_trim_working_sets() -> CleanResult:
    """Call EmptyWorkingSet on every process we can open."""
    try:
        import ctypes
        from ctypes import wintypes

        import psutil

        _windows_enable_debug_privilege()

        PROCESS_QUERY_INFORMATION = 0x0400
        PROCESS_SET_QUOTA = 0x0100
        access = PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)

        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
        psapi.EmptyWorkingSet.restype = wintypes.BOOL

        trimmed = 0
        skipped = 0
        for proc in psutil.process_iter(["pid"]):
            handle = kernel32.OpenProcess(access, False, proc.info["pid"])
            if not handle:
                skipped += 1
                continue
            try:
                if psapi.EmptyWorkingSet(handle):
                    trimmed += 1
                else:
                    skipped += 1
            finally:
                kernel32.CloseHandle(handle)

        return CleanResult(
            CACHE,
            True,
            f"Trimmed working sets for {trimmed} processes ({skipped} skipped).",
        )
    except Exception as exc:
        return CleanResult(CACHE, False, f"Windows working-set trim failed: {exc}")


# --------------------------------------------------------------------------- #
# macOS
# --------------------------------------------------------------------------- #


def _macos_purge() -> CleanResult:
    """Free inactive memory using the built-in ``purge`` utility."""
    if not command_exists("purge"):
        return CleanResult(CACHE, False, "The 'purge' utility is not available.")
    command = privilege_wrap(["purge"])
    rc, _, stderr = run_command(command, timeout=120)
    if rc == 0:
        return CleanResult(CACHE, True, "Purged inactive memory.")
    if rc == 127 or "sudo" in stderr.lower():
        return CleanResult(
            CACHE,
            False,
            "Root privileges required. Re-run with: sudo ramfix clean --cache",
        )
    return CleanResult(CACHE, False, f"purge failed: {stderr or 'unknown error'}")


# --------------------------------------------------------------------------- #
# Python GC (all platforms)
# --------------------------------------------------------------------------- #


def _run_gc() -> CleanResult:
    collected = gc.collect()
    return CleanResult(
        GC,
        True,
        f"Collected {collected} unreachable Python objects in this process.",
    )


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


def _unsupported(action: str) -> CleanResult:
    system = get_platform()
    return CleanResult(
        action,
        False,
        f"Action '{action}' is not implemented for {system}.",
    )


def clean_action(action: str) -> CleanResult:
    """Run a single cleanup *action* on the current platform."""
    system = get_platform()

    if action == GC:
        return _run_gc()

    if action == CACHE:
        if system == "linux":
            return _linux_drop_caches()
        if system == "windows":
            return _windows_trim_working_sets()
        if system == "darwin":
            return _macos_purge()
        return _unsupported(action)

    if action == SWAP:
        if system == "linux":
            return _linux_cycle_swap()
        return _unsupported(action)

    return CleanResult(action, False, f"Unknown action: {action}")


def clean(actions: list[str], *, assume_yes: bool = False, dry_run: bool = False) -> list[CleanResult]:
    """Run several cleanup actions in order.

    Swap cycling is destructive enough to always ask for confirmation unless
    *assume_yes* is set.
    """
    results: list[CleanResult] = []
    for action in actions:
        if dry_run:
            results.append(
                CleanResult(action, True, f"[dry run] Would run: {ACTION_LABELS.get(action, action)}")
            )
            continue

        if action == SWAP and not assume_yes:
            if not confirm(
                "Cycling swap can briefly slow the system and may fail if RAM is full. Continue?"
            ):
                results.append(CleanResult(SWAP, False, "Skipped by user."))
                continue

        results.append(clean_action(action))
    return results
