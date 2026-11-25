"""Decide which screen the target application is showing.

The recognizer answers one question, from data only: *given the app's visible
windows, which state is the app in?* It never guesses. When nothing in the
learned table fits it says so - ``POPUP`` when the topmost window is a real
window with an unfamiliar identity, ``UNKNOWN`` when it would have to read
controls it has not read yet - and every answer carries the evidence it used.

Two rules come straight out of the snapshots this was built against:

* The **topmost titled window decides**. The main menu window stays open behind
  the Sale Order window, so "the menu exists" says nothing about where you are.
* **Automation id beats name.** ``Edit|NepDateEdit`` is *named* ``2083/03/32``
  (a Bikram Sambat date) and a lookup shows its current value, so anything
  matched by name alone stops matching the next day.

``match`` is pure: hand it an :class:`Observation` built from a snapshot file
and it answers without touching Windows. Only :func:`observe` and
:func:`detect` talk to the live application.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from desktop.state.states import ControlNeed, StateSpec, StateTable

# The dark border drawn behind each form; it has no title and no controls.
IGNORED_CLASSES = frozenset({"ShadowWindow"})

#: How many windows on top are worth reading.
#:
#: FACT keeps dozens of forms alive (89 were counted on a live session), most of
#: them hidden. Only the top few can be the screen the user is looking at, and
#: asking UI Automation for every one's automation id is what used to turn a
#: state check into minutes of waiting.
MAX_LIVE_WINDOWS = 8

# What the recognizer reports besides a learned state id.
CLOSED = "CLOSED"
STARTING = "STARTING"
POPUP = "POPUP"
UNKNOWN = "UNKNOWN"
LOADING = "LOADING"

GENERIC_STATES = frozenset({CLOSED, STARTING, POPUP, UNKNOWN, LOADING})


class AppState:
    """The non-learned answers, as constants with one place to read them."""

    CLOSED = CLOSED
    STARTING = STARTING
    POPUP = POPUP
    UNKNOWN = UNKNOWN
    LOADING = LOADING


def normalise(text: str | None) -> str:
    """Compare control names the way the app labels them, not as typed.

    WinForms spells an accelerator as ``&`` (and a literal ampersand as
    ``&&``), so ``&Save && &New`` and ``Save & New`` are the same button.
    """
    if not text:
        return ""
    cleaned = text.replace("&", "").casefold()
    cleaned = " ".join(cleaned.split())
    return cleaned.rstrip(": ").strip()


@dataclass(frozen=True)
class ControlView:
    """One control as it was observed, snapshot or live."""

    type: str = ""
    name: str = ""
    auto_id: str = ""
    enabled: bool = True
    visible: bool = True

    @classmethod
    def from_snapshot(cls, control: dict) -> "ControlView":
        return cls(
            type=str(control.get("type") or ""),
            name=str(control.get("name") or ""),
            auto_id=str(control.get("id") or ""),
            enabled=control.get("enabled") is not False,
            visible=control.get("visible") is not False,
        )

    def __str__(self) -> str:
        label = self.auto_id or self.name
        return f"{self.type}|{label}" if self.type else label


@dataclass(frozen=True)
class WindowView:
    """One visible top-level window of the target process.

    ``controls`` is ``None`` when they have not been read: deciding a state
    that needs them then comes back as *pending* instead of as a wrong answer.
    """

    title: str = ""
    class_name: str = ""
    auto_id: str = ""
    z_order: int = 0
    is_foreground: bool = False
    controls: tuple[ControlView, ...] | None = None
    handle: int = 0

    @classmethod
    def from_snapshot(cls, window: dict) -> "WindowView":
        uia = window.get("uia") or {}
        controls = None
        if uia.get("count"):
            controls = tuple(
                ControlView.from_snapshot(control)
                for control in uia.get("controls") or []
            )
        return cls(
            title=str(window.get("title") or ""),
            class_name=str(window.get("class") or ""),
            auto_id=str(window.get("auto_id") or ""),
            z_order=int(window.get("z_order") or 0),
            is_foreground=bool(window.get("is_foreground")),
            controls=controls,
            handle=int(window.get("hwnd") or 0),
        )

    @property
    def titled(self) -> bool:
        return bool(self.title.strip())

    @property
    def ignorable(self) -> bool:
        return self.class_name in IGNORED_CLASSES or not self.titled

    def describe(self) -> str:
        return f"{self.title!r} (auto_id {self.auto_id!r})"


@dataclass(frozen=True)
class Observation:
    """Every visible window of one process, topmost first."""

    process: str = ""
    windows: tuple[WindowView, ...] = field(default_factory=tuple)

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> "Observation":
        process = snapshot.get("process") or {}
        windows = sorted(
            (WindowView.from_snapshot(window) for window in snapshot.get("windows") or []),
            key=lambda window: window.z_order,
        )
        return cls(
            process=str(process.get("name") or ""),
            windows=tuple(windows),
        )

    def deciding_windows(self) -> list[WindowView]:
        """The active titled window first, then other windows by z-order.

        Pinned sibling windows can appear above the active form in z-order.
        State recognition should follow the user's/automation's foreground
        window before considering those siblings.
        """
        windows = [window for window in self.windows if not window.ignorable]
        return sorted(
            windows,
            key=lambda window: (not window.is_foreground, window.z_order),
        )


@dataclass(frozen=True)
class StateMatch:
    """The answer, with the evidence that produced it."""

    state_id: str = ""
    window: WindowView | None = None
    evidence: tuple[str, ...] = ()
    stack: tuple[str, ...] = ()
    pending: StateSpec | None = None
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.state_id not in GENERIC_STATES and bool(self.state_id)

    @property
    def needs_controls(self) -> bool:
        return self.pending is not None

    def message(self) -> str:
        """One line a person can act on."""
        title = self.window.title if self.window else ""
        if self.state_id == CLOSED:
            return self.detail or "the application is not running"
        if self.state_id == STARTING:
            return "no titled window yet; the application may still be starting"
        if self.state_id == POPUP:
            return f"a window this table does not know is on top: {title!r}"
        if self.state_id == UNKNOWN:
            return self.detail or "could not recognise the screen"
        proved = ", ".join(self.evidence) if self.evidence else "title only"
        return f"{self.state_id} ({title!r}), proved by {proved}"


@dataclass(frozen=True)
class _Decision:
    """What one window says about the table."""

    state: StateSpec | None = None
    evidence: tuple[str, ...] = ()
    pending: StateSpec | None = None
    detail: str = ""


def _title_matches(spec: StateSpec, title: str) -> bool:
    title = title.strip()
    folded = title.casefold()
    match = spec.match
    if match.title_is:
        return folded == match.title_is.strip().casefold()
    if match.title_ends_with:
        return folded.endswith(match.title_ends_with.strip().casefold())
    if match.title_contains:
        return match.title_contains.strip().casefold() in folded
    return True


def _identity_matches(spec: StateSpec, window: WindowView) -> bool:
    if spec.match.auto_id:
        if window.auto_id.strip().casefold() != spec.match.auto_id.strip().casefold():
            return False
    return _title_matches(spec, window.title)


def _control_present(controls: Sequence[ControlView], need: ControlNeed) -> bool:
    wanted_id = (need.auto_id or "").strip().casefold()
    wanted_name = normalise(need.name)
    for control in controls:
        if not control.visible:
            continue
        if wanted_id and control.auto_id.strip().casefold() == wanted_id:
            return True
        if wanted_name and normalise(control.name) == wanted_name:
            return True
    return False


def _controls_match(spec: StateSpec, window: WindowView) -> tuple[bool, bool, list[str]]:
    """``(ok, pending, evidence)`` for one state's control needs."""
    if not spec.match.needs_controls:
        return True, False, []
    if window.controls is None:
        return False, True, []
    evidence: list[str] = []
    for need in spec.match.require:
        if not _control_present(window.controls, need):
            return False, False, []
        evidence.append(need.describe())
    for need in spec.match.forbid:
        if _control_present(window.controls, need):
            return False, False, []
    return True, False, evidence


def _decide(window: WindowView, table: StateTable) -> _Decision:
    pending: StateSpec | None = None
    for spec in table.states:
        if not _identity_matches(spec, window):
            continue
        ok, waiting, evidence = _controls_match(spec, window)
        if waiting:
            pending = pending or spec
            continue
        if ok:
            return _Decision(state=spec, evidence=tuple(evidence))
    return _Decision(pending=pending)


def _quick_state_id(window: WindowView, table: StateTable) -> str:
    """Identify a window by identity alone, for the stack behind the top one."""
    for spec in table.states:
        if _identity_matches(spec, window) and not spec.match.needs_controls:
            return spec.id
    return UNKNOWN


def match(observation: Observation, table: StateTable) -> StateMatch:
    """Decide the state from an observation. Pure: no I/O, no guessing."""
    windows = observation.deciding_windows()
    if not windows:
        return StateMatch(state_id=STARTING, detail="no titled window yet")

    top = windows[0]
    decided = _decide(top, table)
    stack = tuple(
        _quick_state_id(window, table) for window in reversed(windows[1:])
    )
    if decided.pending is not None:
        return StateMatch(
            state_id=UNKNOWN,
            window=top,
            stack=stack,
            pending=decided.pending,
            detail=(
                f"{top.describe()} needs the control list to be read before it "
                f"can be recognised"
            ),
        )
    if decided.state is not None:
        return StateMatch(
            state_id=decided.state.id,
            window=top,
            evidence=decided.evidence,
            stack=stack,
        )
    return StateMatch(
        state_id=POPUP,
        window=top,
        stack=stack,
        detail=(
            f"the topmost window {top.describe()} matches no state; "
            "a dialog may be open"
        ),
    )


# --------------------------------------------------------------- live (win32) --
def _process_matches(actual: str, wanted: str) -> bool:
    actual = (actual or "").strip().casefold()
    wanted = (wanted or "").strip().casefold()
    if not actual or not wanted:
        return False
    return (
        actual == wanted
        or actual == f"{wanted}.exe"
        or f"{actual}.exe" == wanted
    )


def _is_visible(handle: int) -> bool:
    """True when Windows draws this window right now (one cheap Win32 call)."""
    try:
        import win32gui

        return bool(win32gui.IsWindowVisible(int(handle)))
    except Exception:
        # Cannot tell: keep the window and let the title/id checks decide.
        return True


def _window_auto_id(handle: int) -> str:
    """The window's UIA automation id, which Win32 alone cannot report."""
    try:
        from pywinauto.uia_element_info import UIAElementInfo

        return str(getattr(UIAElementInfo(handle_or_elem=handle), "automation_id", "") or "")
    except Exception:
        return ""


def _scan_controls(handle: int) -> tuple[ControlView, ...] | None:
    """Read one window's control list through the automation-facing scanner."""
    from desktop.uia.uia import inspect_window
    from desktop.windows.window import get_window

    try:
        inspection = inspect_window(get_window(handle), max_elements=800)
    except Exception:
        return None
    return tuple(
        ControlView(
            type=candidate.control_type,
            name=candidate.name,
            auto_id=candidate.automation_id,
            enabled=candidate.enabled,
        )
        for candidate in inspection.candidates
    )


def observe(
    process: str,
    *,
    scan_handles: Iterable[int] = (),
) -> Observation:
    """Read the target process's visible windows from the live desktop.

    Only the windows listed in ``scan_handles`` get their control list read:
    scanning is the slow part, and a state whose identity already decides
    does not need it.
    """
    from desktop.windows.window import list_windows

    wanted = {int(handle) for handle in scan_handles}
    windows: list[WindowView] = []
    for z_order, info in enumerate(list_windows()):
        if not _process_matches(info.process_name, process):
            continue
        if not str(info.title or "").strip():
            continue
        if not _is_visible(info.handle):
            continue
        if len(windows) >= MAX_LIVE_WINDOWS:
            # Everything below the top few is not on screen, so it cannot be
            # the state; reading it costs a UIA round trip each time.
            break
        handle = int(info.handle)
        windows.append(
            WindowView(
                title=info.title,
                class_name=info.class_name,
                auto_id=_window_auto_id(handle),
                z_order=z_order,
                is_foreground=False,
                controls=_scan_controls(handle) if handle in wanted else None,
                handle=handle,
            )
        )
    foreground = 0
    try:
        import win32gui

        foreground = int(win32gui.GetForegroundWindow())
    except Exception:
        foreground = 0
    if foreground:
        windows = [
            WindowView(
                title=window.title,
                class_name=window.class_name,
                auto_id=window.auto_id,
                z_order=window.z_order,
                is_foreground=window.handle == foreground,
                controls=window.controls,
                handle=window.handle,
            )
            for window in windows
        ]
    return Observation(process=process, windows=tuple(windows))


def detect(
    table: StateTable | None = None,
    *,
    process: str | None = None,
    output=None,
) -> StateMatch:
    """Recognise the live screen, reading controls only when they decide it."""
    from desktop.state.states import load_state_table

    table = table if table is not None else load_state_table()
    process = process or table.process or "FACTERPNG.exe"
    observation = observe(process)
    if not observation.windows:
        return StateMatch(state_id=CLOSED, detail=f"{process} is not running")

    result = match(observation, table)
    if result.pending is not None and result.window is not None and result.window.handle:
        if output is not None:
            output(f"  reading controls of {result.window.title!r}")
        observation = observe(process, scan_handles=[result.window.handle])
        result = match(observation, table)
    return result


__all__ = [
    "CLOSED",
    "GENERIC_STATES",
    "IGNORED_CLASSES",
    "LOADING",
    "POPUP",
    "STARTING",
    "UNKNOWN",
    "AppState",
    "ControlView",
    "Observation",
    "StateMatch",
    "WindowView",
    "detect",
    "match",
    "normalise",
    "observe",
]
