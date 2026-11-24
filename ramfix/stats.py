"""Read memory statistics and process memory usage via psutil."""

from __future__ import annotations

from dataclasses import dataclass

import psutil

from .utils import human_bytes


@dataclass(frozen=True)
class MemoryStats:
    """A snapshot of system memory and swap usage."""

    total: int
    available: int
    used: int
    free: int
    cached: int
    buffers: int
    swap_total: int
    swap_used: int

    @property
    def percent(self) -> float:
        """Percentage of physical RAM currently in use."""
        return psutil.virtual_memory().percent

    @property
    def swap_percent(self) -> float:
        if self.swap_total == 0:
            return 0.0
        return self.swap_used / self.swap_total * 100


def get_memory_stats() -> MemoryStats:
    """Return a current :class:`MemoryStats` snapshot."""
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return MemoryStats(
        total=vm.total,
        available=vm.available,
        used=vm.used,
        free=getattr(vm, "free", 0),
        cached=getattr(vm, "cached", 0),
        buffers=getattr(vm, "buffers", 0),
        swap_total=sw.total,
        swap_used=sw.used,
    )


@dataclass(frozen=True)
class ProcessMem:
    """Resident memory usage for a single process."""

    pid: int
    name: str
    rss: int
    vms: int

    @property
    def rss_human(self) -> str:
        return human_bytes(self.rss)


def get_top_processes(limit: int = 10) -> list[ProcessMem]:
    """Return the *limit* processes with the largest resident set size."""
    processes: list[ProcessMem] = []
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            info = proc.info
            mem = info.get("memory_info")
            if mem is None:
                continue
            processes.append(
                ProcessMem(
                    pid=info["pid"],
                    name=info.get("name") or "<unknown>",
                    rss=mem.rss,
                    vms=mem.vms,
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    processes.sort(key=lambda p: p.rss, reverse=True)
    return processes[:limit]


def process_count() -> int:
    """Number of running processes."""
    return len(psutil.pids())
