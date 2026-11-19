"""Start FACT when it is not running, and never start a second copy.

Closing FACT between runs is normal - a machine restarts, a person logs out - and
a run that only works when the application is already open is a run that fails
for a boring reason. The executable is not guessed from the screen; it is a value
in ``config/app.json`` that a person sets once:

    {
      "schema": "app/1",
      "process": "FACTERPNG.exe",
      "exe_path": "C:/FACT/FACTERPNG.exe",
      "startup_seconds": 60
    }

Everything here is deliberately small: check whether the process is showing any
window, and if not, start the configured file and wait for the recogniser to
name a screen. It never kills, restarts or focuses a running FACT.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.config import project_root

APP_SCHEMA = "app/1"
APP_FILE = "app.json"


class LaunchError(RuntimeError):
    """FACT is not running and cannot be started from the configured path."""


@dataclass(frozen=True)
class AppPaths:
    """What ``config/app.json`` says about the application."""

    process: str = "FACTERPNG.exe"
    exe_path: str = ""
    startup_seconds: float = 60.0

    def exists(self) -> bool:
        """True when the configured executable is a real file."""
        return bool(self.exe_path) and Path(self.exe_path).is_file()


def app_config_path() -> Path:
    return project_root() / "config" / APP_FILE


def load_app_paths(path: str | Path | None = None) -> AppPaths:
    """Read the app configuration; a missing file is not an error.

    No file means "I have not told the automation where FACT is", which is a
    perfectly usable state: runs then require FACT to be open already, and the
    error that says so includes the file to create.
    """
    file_path = Path(path) if path is not None else app_config_path()
    if not file_path.is_absolute():
        file_path = project_root() / file_path
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return AppPaths()
    except (OSError, json.JSONDecodeError) as exc:
        raise LaunchError(f"cannot read {file_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LaunchError(f"{file_path} must hold one JSON object")
    if data.get("schema", APP_SCHEMA) != APP_SCHEMA:
        raise LaunchError(
            f"{file_path} uses schema {data.get('schema')!r}; expected {APP_SCHEMA!r}"
        )
    return AppPaths(
        process=str(data.get("process") or "FACTERPNG.exe"),
        exe_path=str(data.get("exe_path") or "").strip(),
        startup_seconds=float(data.get("startup_seconds") or 60.0),
    )


def is_running(process: str | None = None) -> bool:
    """True when a window of the target process is on the desktop.

    This is a plain Win32 window enumeration, not UI Automation: it answers in
    milliseconds and cannot stall on a busy application. Deciding *which* screen
    is showing is the recogniser's job, and runs do that when they start.
    """
    from desktop.windows.window import list_windows

    wanted = (process or "FACTERPNG.exe").strip().casefold()
    try:
        windows = list_windows()
    except Exception:  # a desktop that cannot be listed is "not running"
        return False
    return any((window.process_name or "").strip().casefold() == wanted for window in windows)


def ensure_running(
    paths: AppPaths | None = None,
    *,
    output: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Make sure FACT is open; returns ``True`` when it had to start it.

    Nothing is launched when a window is already there - not even a second copy
    in case the first is on the login screen - because two FACT instances
    sharing a data folder is not a state this automation can reason about.
    """
    paths = paths if paths is not None else load_app_paths()
    if is_running(paths.process):
        return False

    if not paths.exe_path:
        raise LaunchError(
            "FACT is not running, and no executable is configured. Create "
            f"{app_config_path()} with "
            '{"schema": "app/1", "process": "FACTERPNG.exe", '
            '"exe_path": "C:/path/to/FACTERPNG.exe", "startup_seconds": 60} '
            "or open FACT before running."
        )
    exe = Path(paths.exe_path)
    if not exe.is_file():
        raise LaunchError(
            f"FACT is not running, and the configured executable does not exist: "
            f"{exe}; fix exe_path in {app_config_path()}"
        )

    output(f"  FACT is not running; starting {exe.name}")
    try:
        subprocess.Popen([str(exe)], cwd=str(exe.parent))
    except OSError as exc:
        raise LaunchError(f"could not start {exe}: {exc}") from exc

    deadline = time.monotonic() + max(0.0, paths.startup_seconds)
    while time.monotonic() < deadline:
        if is_running(paths.process):
            output("  FACT is up; waiting for a screen the state table knows")
            return True
        sleep(1.0)
    raise LaunchError(
        f"{exe.name} was started but showed no window within "
        f"{paths.startup_seconds:.0f}s; log in by hand, then re-run"
    )


__all__ = [
    "APP_FILE",
    "APP_SCHEMA",
    "AppPaths",
    "LaunchError",
    "app_config_path",
    "ensure_running",
    "is_running",
    "load_app_paths",
]
