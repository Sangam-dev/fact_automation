"""Attach to FACT by screen, never to a remembered window handle.

FACT ERP.NG swaps top-level windows as it moves between screens: the Login form
is its own window, the main menu is another, and each Sale Order form is a third.
A :class:`~desktop.types.WindowInfo` captured once is therefore stale the moment
the user logs in - clicking the old handle raises ``WindowNotFoundError`` even
though FACT is still running and showing the next screen.

This module is the one place that answers *which window should I touch now?*
It asks :mod:`desktop.state.recognizer` (the learned successor of
``scripts/fact_state.py``) what screen FACT is showing, takes the handle from
that answer, and reads the window afresh. Every capture, inspection, click and
save in the automation paths goes through it, so a screen change is a normal
step instead of a crash.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from core.config import AppConfig
from desktop.screenshots.capture import Capture, CaptureError, capture_window
from desktop.state.recognizer import StateMatch, detect
from desktop.state.states import StateTable, load_state_table
from desktop.types import WindowInfo
from desktop.uia.uia import UIAError, UIAInspection, inspect_window
from desktop.windows.window import (
    WindowError,
    focus_window,
    get_window,
    keep_windows_pinned,
)

#: The only process this automation is allowed to drive.
FACT_PROCESS = "FACTERPNG.exe"
#: A title prefix every FACT screen shares; used only for readable messages.
FACT_TITLE = "FACT ERP.NG"

#: How long a screen change (Login -> Main, Add -> Sale Order) may take.
DEFAULT_WAIT_SECONDS = 10.0
DEFAULT_INTERVAL_SECONDS = 0.25

#: How long one screen read may take before it is given up on.
#:
#: Windows UI Automation answers a busy or modal application late - a real
#: session showed one read taking eighty seconds while a message box was open.
#: Waiting forever is worse than a clear "I could not read the screen", so every
#: read runs on its own thread and this is the budget it gets.
DETECT_SECONDS = 15.0


class FactError(RuntimeError):
    """FACT is not running, or is showing a screen the state table cannot name."""


@dataclass(frozen=True)
class FactScreen:
    """One capture of FACT, taken from the window that is showing *now*."""

    window: WindowInfo
    capture: Capture
    inspection: UIAInspection
    match: StateMatch


def load_table(path: str | None = None) -> StateTable:
    """The learned FACT state table, with a helpful error when it is missing."""
    try:
        return load_state_table(path)
    except Exception as exc:  # StateTableError, but keep this layer's message
        raise FactError(f"the FACT state table cannot be read: {exc}") from exc


def _com_ready() -> Any:
    """Initialise COM for a worker thread; returns what has to be uninitialised."""
    try:
        import pythoncom

        pythoncom.CoInitialize()
        return pythoncom
    except Exception:
        try:
            import comtypes

            comtypes.CoInitialize()
            return comtypes
        except Exception:
            return None


def _com_done(module: Any) -> None:
    if module is None:
        return
    try:
        module.CoUninitialize()
    except Exception:
        pass


def detect_state(
    table: StateTable | None = None,
    *,
    seconds: float = DETECT_SECONDS,
    process: str = FACT_PROCESS,
) -> StateMatch:
    """Ask which FACT screen is on top, and never wait on it forever.

    The answer is the recogniser's own: this only puts a time budget around the
    call, so a modal dialog that does not answer Windows UI Automation turns
    into a clear ``UNKNOWN`` with a reason instead of a frozen terminal.
    """
    from desktop.state.recognizer import UNKNOWN

    table = table if table is not None else load_table()
    box: dict[str, Any] = {}

    def work() -> None:
        com = _com_ready()
        try:
            box["match"] = detect(table, process=process)
        except BaseException as exc:  # noqa: BLE001 - reported by the caller
            box["error"] = exc
        finally:
            _com_done(com)

    thread = threading.Thread(target=work, name="fact-screen-read", daemon=True)
    thread.start()
    thread.join(max(0.05, seconds))
    if thread.is_alive():
        return StateMatch(
            state_id=UNKNOWN,
            detail=(
                f"FACT did not answer Windows UI Automation within {seconds:.0f}s; "
                "a dialog may be open or the window is busy - close it by hand, "
                "then try again"
            ),
        )
    if "error" in box:
        raise FactError(f"the screen could not be read: {box['error']}")
    return box["match"]


def window_for(match: StateMatch) -> WindowInfo:
    """The live ``WindowInfo`` for the window the recogniser chose."""
    if match.window is None or not match.window.handle:
        raise FactError(match.message())
    try:
        return get_window(int(match.window.handle))
    except WindowError as exc:
        raise FactError(
            f"the FACT screen changed while it was being read; try again ({exc})"
        ) from exc


def wait_for_screen(
    table: StateTable | None = None,
    *,
    config: AppConfig | None = None,
    seconds: float = DEFAULT_WAIT_SECONDS,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    output: Callable[[str], None] | None = None,
    expected_state: str | None = None,
    expected_handle: int | None = None,
) -> tuple[WindowInfo, StateMatch]:
    """Wait until FACT shows a screen the state table knows, then attach.

    A screen transition is not instantaneous: after Login the old window is
    destroyed and the main window is still being drawn. During that moment the
    recogniser may answer ``STARTING``/``POPUP``; waiting is the correct
    response, not an error and certainly not a click on the old handle.
    """
    deadline = time.monotonic() + max(0.0, seconds)
    table = table if table is not None else load_table()
    last: StateMatch | None = None
    while True:
        match = detect_state(table)
        last = match

        handle = expected_handle
        if (
            handle is None
            and match.known
            and match.window is not None
            and (expected_state is None or match.state_id == expected_state)
        ):
            handle = int(match.window.handle or 0) or None

        window = None
        if handle is not None:
            try:
                window = get_window(handle)
            except WindowError:
                window = None

        if window is not None:
            if config is not None and config.require_foreground:
                focus_window(window)
                try:
                    window = get_window(window.handle)
                except WindowError:
                    window = None

            if window is not None and (
                expected_state is not None or expected_handle is not None
            ):
                # Focusing or repinning an application can reorder its sibling
                # windows. Confirm the requested state/handle after focus and
                # before returning a window that could receive input.
                confirmed = detect_state(table)
                confirmed_handle = (
                    int(confirmed.window.handle or 0)
                    if confirmed.window is not None
                    else 0
                )
                matches_state = (
                    expected_state is None or confirmed.state_id == expected_state
                )
                matches_handle = (
                    expected_handle is None or confirmed_handle == expected_handle
                )
                if confirmed.known and matches_state and matches_handle:
                    match = confirmed
                    try:
                        window = get_window(confirmed_handle)
                    except WindowError:
                        window = None
                else:
                    last = confirmed
                    window = None

            if window is not None:
                # Keep the whole app pinned, not just this window: a form FACT
                # opens later is lifted with it instead of appearing behind it.
                if config is None or config.pin_window:
                    keep_windows_pinned(window.process_name)
                return window, match

        if time.monotonic() >= deadline:
            break
        if output is not None and last is not None:
            output(f"  waiting for a known FACT screen (now {last.state_id})")
        sleep(interval)

    message = last.message() if last is not None else "FACT is not running"
    if expected_state is not None or expected_handle is not None:
        expected = []
        if expected_state is not None:
            expected.append(f"screen {expected_state!r}")
        if expected_handle is not None:
            expected.append(f"window {expected_handle}")
        raise FactError(f"expected {' on '.join(expected)}, but {message}")
    raise FactError(message)


def attach(
    config: AppConfig | None = None,
    *,
    table: StateTable | None = None,
    seconds: float = DEFAULT_WAIT_SECONDS,
    expected_state: str | None = None,
    expected_handle: int | None = None,
) -> WindowInfo:
    """Attach to the current FACT window, optionally requiring a known screen."""
    window, _match = wait_for_screen(
        table,
        config=config,
        seconds=seconds,
        expected_state=expected_state,
        expected_handle=expected_handle,
    )
    return window


def is_fact(config: AppConfig) -> bool:
    """True when this config targets FACT, however it was written."""
    process = (config.process_name or "").strip().casefold()
    title = (config.title_contains or "").strip().casefold()
    return process in {"facterpng", "facterpng.exe"} or "fact erp.ng" in title


def attach_target(config: AppConfig) -> WindowInfo:
    """Attach to FACT by screen, or to another app by its configured window.

    The generic commands (``inspect``, ``ground``, ``form run``) accept a config
    for any application. A config that names FACT gets the state-aware attach;
    everything else keeps the original title/process lookup.
    """
    if is_fact(config):
        return attach(config)
    from desktop.windows.window import attach_window

    return attach_window(config)


def _read(
    *,
    table: StateTable | None,
    seconds: float,
    interval: float,
    sleep: Callable[[float], None],
    with_capture: bool,
) -> tuple[WindowInfo, StateMatch, Capture | None, UIAInspection]:
    deadline = time.monotonic() + max(0.0, seconds)
    table = table if table is not None else load_table()
    last_error = ""
    while True:
        try:
            window, match = wait_for_screen(
                table, seconds=0.0, interval=interval, sleep=sleep
            )
            # Inspect first: it restores a minimized window, which capture
            # refuses to take. The geometry it returns is the one to shoot.
            inspection = inspect_window(window)
            window = inspection.window
            capture = capture_window(window) if with_capture else None
            return window, match, capture, inspection
        except (FactError, CaptureError, UIAError, WindowError) as exc:
            last_error = str(exc)
        if time.monotonic() >= deadline:
            break
        sleep(interval)
    raise FactError(last_error or "could not read the current FACT screen")


def read_screen(
    table: StateTable | None = None,
    *,
    seconds: float = DEFAULT_WAIT_SECONDS,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> FactScreen:
    """Capture and inspect the screen FACT is showing now, waiting if it is busy."""
    window, match, capture, inspection = _read(
        table=table,
        seconds=seconds,
        interval=interval,
        sleep=sleep,
        with_capture=True,
    )
    assert capture is not None  # ``with_capture=True``
    return FactScreen(
        window=window, capture=capture, inspection=inspection, match=match
    )


def read_current(
    table: StateTable | None = None,
    *,
    seconds: float = DEFAULT_WAIT_SECONDS,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[WindowInfo, UIAInspection, StateMatch]:
    """Re-resolve the window after a click, without taking a screenshot."""
    window, match, _capture, inspection = _read(
        table=table,
        seconds=seconds,
        interval=interval,
        sleep=sleep,
        with_capture=False,
    )
    return window, inspection, match


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_WAIT_SECONDS",
    "FACT_PROCESS",
    "FACT_TITLE",
    "FactError",
    "FactScreen",
    "attach",
    "attach_target",
    "detect_state",
    "is_fact",
    "load_table",
    "read_current",
    "read_screen",
    "wait_for_screen",
    "window_for",
]
