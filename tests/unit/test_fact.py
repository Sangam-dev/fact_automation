"""FACT-only attachment: the current screen decides the window."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.config import AppConfig
from desktop.state import fact
from desktop.state.recognizer import POPUP, StateMatch, WindowView
from desktop.state.states import StateTable
from desktop.types import Rect, WindowInfo


def _window(handle: int = 7, title: str = "FACT ERP.NG") -> WindowInfo:
    return WindowInfo(
        handle=handle,
        title=title,
        process_name=fact.FACT_PROCESS,
        class_name="WindowsForms10.Window.8.app",
        rect=Rect(0, 0, 100, 100),
        client_rect=Rect(0, 0, 100, 100),
        dpi=96,
    )


def _known(state: str = "03_entry", handle: int = 7) -> StateMatch:
    return StateMatch(
        state_id=state,
        window=WindowView(title="FACT ERP.NG", handle=handle),
    )


def _table() -> StateTable:
    return StateTable.model_validate(
        {
            "process": fact.FACT_PROCESS,
            "states": [
                {"id": "03_entry", "match": {"auto_id": "frmMenu"}},
            ],
        }
    )


def test_wait_for_screen_waits_out_a_transition(monkeypatch) -> None:
    """Login closes, Main is still drawing: the old answer is not an error."""
    answers = iter([StateMatch(state_id=POPUP), _known()])
    monkeypatch.setattr(fact, "detect", lambda table, process: next(answers))
    monkeypatch.setattr(fact, "get_window", lambda handle: _window(handle))

    window, match = fact.wait_for_screen(
        _table(), seconds=1.0, interval=0.0, sleep=lambda seconds: None
    )

    assert window.handle == 7
    assert match.state_id == "03_entry"


def test_wait_for_screen_fails_with_the_recognisers_own_message(monkeypatch) -> None:
    monkeypatch.setattr(
        fact, "detect", lambda table, process: StateMatch(state_id=POPUP, detail="popup")
    )

    with pytest.raises(fact.FactError, match="does not know"):
        fact.wait_for_screen(_table(), seconds=0.0, interval=0.0)


def test_read_current_returns_the_live_window_and_inspection(monkeypatch) -> None:
    target = _window()
    match = _known()
    inspection = SimpleNamespace(window=target)
    monkeypatch.setattr(fact, "wait_for_screen", lambda *a, **k: (target, match))
    monkeypatch.setattr(fact, "inspect_window", lambda window: inspection)

    window, got_inspection, got_match = fact.read_current(
        _table(), seconds=0.0, interval=0.0
    )

    assert window is target
    assert got_inspection is inspection
    assert got_match is match


def test_read_screen_captures_and_inspects_the_same_window(monkeypatch) -> None:
    target = _window()
    match = _known()
    capture = object()
    inspection = SimpleNamespace(window=target)
    monkeypatch.setattr(fact, "wait_for_screen", lambda *a, **k: (target, match))
    monkeypatch.setattr(fact, "capture_window", lambda window: capture)
    monkeypatch.setattr(fact, "inspect_window", lambda window: inspection)

    screen = fact.read_screen(_table(), seconds=0.0, interval=0.0)

    assert screen.window is target
    assert screen.capture is capture
    assert screen.inspection is inspection
    assert screen.match is match


def test_a_fact_config_uses_the_state_aware_attach(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        fact, "attach", lambda config, **kwargs: calls.append("fact") or _window()
    )
    monkeypatch.setattr(
        "desktop.windows.window.attach_window",
        lambda config: calls.append("generic") or _window(),
    )

    fact.attach_target(AppConfig(process_name="FACTERPNG.exe"))
    fact.attach_target(AppConfig(title_contains="Invoice Manager"))

    assert calls == ["fact", "generic"]


def test_reading_a_screen_pins_whichever_windows_the_app_has(monkeypatch) -> None:
    # Pinning one handle left every window the app opened afterwards behind it,
    # so the keeper pins the whole app instead.
    monkeypatch.setattr(fact, "detect", lambda table, process: _known())
    monkeypatch.setattr(fact, "get_window", lambda handle: _window(handle))
    pinned: list[str] = []
    monkeypatch.setattr(
        fact, "keep_windows_pinned", lambda process, interval=0.3: pinned.append(process) or True
    )

    window, _match = fact.wait_for_screen(_table(), seconds=0.0, interval=0.0)

    assert window.handle == 7
    assert pinned == [fact.FACT_PROCESS]


def test_pin_window_false_cancels_the_pinning(monkeypatch) -> None:
    monkeypatch.setattr(fact, "detect", lambda table, process: _known())
    monkeypatch.setattr(fact, "get_window", lambda handle: _window(handle))
    monkeypatch.setattr(fact, "focus_window", lambda window: True)
    pinned: list[str] = []
    monkeypatch.setattr(
        fact, "keep_windows_pinned", lambda process, interval=0.3: pinned.append(process) or True
    )

    fact.wait_for_screen(
        _table(),
        config=AppConfig(
            process_name=fact.FACT_PROCESS,
            require_foreground=False,
            pin_window=False,
        ),
        seconds=0.0,
        interval=0.0,
    )

    assert pinned == []


    assert pinned == []

