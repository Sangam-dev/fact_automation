"""Unit tests for keeping our own windows above other always-on-top windows."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import win32con
import win32gui

from desktop.windows.topmost import (
    forget_window,
    keep_above_topmost,
    raise_above_topmost,
    raise_our_windows,
)


@pytest.fixture(autouse=True)
def _start_with_no_windows_of_ours():
    import desktop.windows.topmost as topmost

    topmost._ours.clear()
    yield
    topmost._ours.clear()


def test_the_window_is_pushed_into_the_topmost_band_without_focus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(win32gui, "GetAncestor", lambda handle, _flag: handle + 1)
    monkeypatch.setattr(win32gui, "SetWindowPos", lambda *args: calls.append(args))
    widget = MagicMock()
    widget.winfo_id.return_value = 0x1234

    assert raise_above_topmost(widget) is True

    handle, insert_after, _x, _y, _width, _height, flags = calls[0]
    # The toplevel, not the client window tkinter hands out.
    assert handle == 0x1235
    assert insert_after == win32con.HWND_TOPMOST
    # Re-raising must not take the focus away from the app being automated.
    assert flags & win32con.SWP_NOACTIVATE
    assert flags & win32con.SWP_NOMOVE
    assert flags & win32con.SWP_NOSIZE


def test_a_widget_that_cannot_report_its_handle_is_left_alone() -> None:
    assert raise_above_topmost(object()) is False


def test_our_window_is_lifted_again_after_the_target_took_the_front(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(win32gui, "GetAncestor", lambda handle, _flag: handle)
    monkeypatch.setattr(win32gui, "SetWindowPos", lambda *args: calls.append(args))
    widget = MagicMock()
    widget.winfo_id.return_value = 0x77

    assert raise_above_topmost(widget) is True
    calls.clear()

    # This is what the input layer does after every click and keystroke: the
    # activation raised the target app, so our window has to come back up.
    assert raise_our_windows() == 1

    handle, insert_after, _x, _y, _width, _height, flags = calls[0]
    assert handle == 0x77
    assert insert_after == win32con.HWND_TOPMOST
    assert flags & win32con.SWP_NOACTIVATE


def test_a_closed_window_is_no_longer_lifted(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(win32gui, "GetAncestor", lambda handle, _flag: handle)
    monkeypatch.setattr(win32gui, "SetWindowPos", lambda *args: calls.append(args))
    widget = MagicMock()
    widget.winfo_id.return_value = 0x88

    assert raise_above_topmost(widget) is True
    forget_window(widget)
    calls.clear()

    assert raise_our_windows() == 0
    assert not calls


def test_the_window_kept_on_top_is_also_lifted_again_on_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(win32gui, "GetAncestor", lambda handle, _flag: handle)
    monkeypatch.setattr(win32gui, "SetWindowPos", lambda *args: None)
    widget = MagicMock()
    widget.winfo_id.return_value = 0x99
    widget.after.side_effect = lambda interval, callback: None

    assert keep_above_topmost(widget) is True

    assert raise_our_windows() == 1


def test_the_topmost_place_is_re_asserted_while_the_window_lives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raised: list[object] = []
    scheduled: list[tuple[int, object]] = []
    widget = MagicMock()
    widget.after.side_effect = lambda interval, callback: scheduled.append(
        (interval, callback)
    )
    monkeypatch.setattr(
        "desktop.windows.topmost.raise_above_topmost",
        lambda target: raised.append(target) or True,
    )

    assert keep_above_topmost(widget, interval_ms=250) is True
    assert raised == [widget]
    assert scheduled[0][0] == 250

    scheduled.pop(0)[1]()
    assert raised == [widget, widget]
    assert len(scheduled) == 1


def test_no_tick_is_scheduled_when_the_window_cannot_be_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "desktop.windows.topmost.raise_above_topmost", lambda _target: False
    )
    widget = MagicMock()

    assert keep_above_topmost(widget) is False
    assert not widget.after.called
