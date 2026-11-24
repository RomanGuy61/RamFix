"""Background automation: clean on a RAM threshold and on app startup.

Configuration lives in ``~/.config/ramfix/config.json``. The guard polls
process names and memory usage; it never kills anything and honours a cooldown
so repeated cleanups cannot pile up.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import psutil

from .cleaner import CACHE, clean_action
from .utils import privilege_wrap, run_command

EventCallback = Callable[[str], None]


@dataclass
class GuardConfig:
    threshold: float = 85.0
    cooldown: float = 60.0
    watch_apps: list[str] = field(default_factory=list)
    clean_on_threshold: bool = True
    clean_on_app_start: bool = True


def config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "ramfix" / "config.json"


def load_config() -> GuardConfig:
    """Load config, falling back to defaults for missing/invalid files."""
    defaults = GuardConfig()
    try:
        with open(config_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return defaults
        return GuardConfig(
            threshold=float(data.get("threshold", defaults.threshold)),
            cooldown=float(data.get("cooldown", defaults.cooldown)),
            watch_apps=[str(item) for item in data.get("watch_apps", [])],
            clean_on_threshold=bool(data.get("clean_on_threshold", True)),
            clean_on_app_start=bool(data.get("clean_on_app_start", True)),
        )
    except (OSError, ValueError, TypeError):
        return defaults


def save_config(config: GuardConfig) -> tuple[bool, str]:
    """Write config to disk."""
    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(config), indent=2) + "\n", encoding="utf-8")
        return True, f"Configuration saved to {path}"
    except OSError as exc:
        return False, f"Could not save configuration: {exc}"


# --------------------------------------------------------------------------- #
# Config mutators
# --------------------------------------------------------------------------- #


def _update(**changes) -> tuple[bool, str]:
    config = load_config()
    for key, value in changes.items():
        setattr(config, key, value)
    return save_config(config)


def set_threshold(value: float) -> tuple[bool, str]:
    if not 1 <= value <= 100:
        return False, "Threshold must be between 1 and 100 percent."
    return _update(threshold=float(value))


def set_cooldown(value: float) -> tuple[bool, str]:
    if value < 0:
        return False, "Cooldown cannot be negative."
    return _update(cooldown=float(value))


def add_watch(name: str) -> tuple[bool, str]:
    name = name.strip()
    if not name:
        return False, "App name cannot be empty."
    config = load_config()
    if any(item.lower() == name.lower() for item in config.watch_apps):
        return False, f"'{name}' is already being watched."
    config.watch_apps.append(name)
    return save_config(config)


def remove_watch(name: str) -> tuple[bool, str]:
    config = load_config()
    keep = [item for item in config.watch_apps if item.lower() != name.lower()]
    if len(keep) == len(config.watch_apps):
        return False, f"'{name}' is not being watched."
    config.watch_apps = keep
    return save_config(config)


def set_flag(flag: str, value: bool) -> tuple[bool, str]:
    if flag not in ("clean_on_threshold", "clean_on_app_start"):
        return False, f"Unknown flag: {flag}"
    return _update(**{flag: bool(value)})


def config_summary() -> str:
    config = load_config()
    watched = ", ".join(config.watch_apps) if config.watch_apps else "none"
    return (
        f"RAM threshold: {config.threshold:.0f}% "
        f"(clean_on_threshold={'on' if config.clean_on_threshold else 'off'})\n"
        f"Cooldown: {config.cooldown:.0f}s\n"
        f"Watched apps: {watched} "
        f"(clean_on_app_start={'on' if config.clean_on_app_start else 'off'})\n"
        f"Config: {config_path()}"
    )


# --------------------------------------------------------------------------- #
# Guard daemon
# --------------------------------------------------------------------------- #


class Guard:
    """Poll memory and process names, cleaning when configured conditions hit."""

    def __init__(
        self,
        config: GuardConfig | None = None,
        interval: float = 2.0,
        on_event: EventCallback | None = None,
    ) -> None:
        self.config = config or load_config()
        self.interval = max(0.5, float(interval))
        self.on_event = on_event or (lambda _message: None)
        self.stop_event = threading.Event()

    def _emit(self, message: str) -> None:
        self.on_event(message)

    def stop(self) -> None:
        self.stop_event.set()

    def _process_names(self) -> set[str]:
        names: set[str] = set()
        try:
            for proc in psutil.process_iter(["name"]):
                name = (proc.info.get("name") or "").lower()
                if name:
                    names.add(name)
        except psutil.Error:
            pass
        return names

    def _clean(self, reason: str) -> None:
        result = clean_action(CACHE)
        prefix = "OK" if result.ok else "FAIL"
        self._emit(f"[{prefix}] Auto-clean ({reason}): {result.message}")

    def run(self) -> None:
        """Blocking monitoring loop. Call :meth:`stop` from another thread."""
        self._emit(
            f"Guard started: threshold={self.config.threshold:.0f}%, "
            f"cooldown={self.config.cooldown:.0f}s, "
            f"watched={len(self.config.watch_apps)} app(s)"
        )
        rc, _, err = run_command(privilege_wrap(["true"]))
        if rc != 0:
            self._emit(
                "Warning: passwordless sudo is unavailable; automatic cache "
                "cleaning will likely fail. Run 'sudo ramfix guard run' instead."
            )

        seen = self._process_names()
        watched = {name.lower() for name in self.config.watch_apps}
        last_clean = 0.0

        while not self.stop_event.is_set():
            now = time.monotonic()

            if self.config.clean_on_threshold:
                usage = psutil.virtual_memory().percent
                if usage >= self.config.threshold and now - last_clean >= self.config.cooldown:
                    self._clean(f"RAM at {usage:.1f}%")
                    last_clean = time.monotonic()

            current = self._process_names()
            new_names = current - seen
            matched = sorted(new_names & watched)
            if (
                matched
                and self.config.clean_on_app_start
                and time.monotonic() - last_clean >= self.config.cooldown
            ):
                self._clean("started " + ", ".join(matched))
                last_clean = time.monotonic()

            seen = current
            self.stop_event.wait(self.interval)

        self._emit("Guard stopped.")
