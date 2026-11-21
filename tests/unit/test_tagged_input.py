"""The reusable UIA-control click adapter uses checked tagged input."""

from __future__ import annotations

from types import SimpleNamespace

from desktop.input import input as agent_input


class Control:
    def rectangle(self):
        return SimpleNamespace(left=10, top=20, right=30, bottom=40)


class Owner:
    handle = 1234


def test_click_control_resolves_owner_and_checks_center(monkeypatch) -> None:
    live_window = object()
    observed = {}

    def resolve_window(handle):
        observed["handle"] = handle
        return live_window

    monkeypatch.setattr(agent_input, "get_window", resolve_window)

    def checked(x, y, window, *, require_foreground, button):
        observed.update(
            x=x,
            y=y,
            window=window,
            require_foreground=require_foreground,
            button=button,
        )

    monkeypatch.setattr(agent_input, "click_checked", checked)

    agent_input.click_control(Control(), Owner())

    assert observed == {
        "handle": 1234,
        "x": 20,
        "y": 30,
        "window": live_window,
        "require_foreground": True,
        "button": "left",
    }


def test_click_control_accepts_raw_owner_handle(monkeypatch) -> None:
    observed = []
    monkeypatch.setattr(
        agent_input,
        "get_window",
        lambda handle: observed.append(handle) or object(),
    )
    monkeypatch.setattr(agent_input, "click_checked", lambda *args, **kwargs: None)

    agent_input.click_control(Control(), 5678, require_foreground=False)

    assert observed == [5678]


def test_injected_keyboard_and_mouse_events_carry_agent_tag() -> None:
    key = agent_input._key_input(agent_input.VK_A, 0, 0)
    mouse = agent_input._mouse_input(agent_input.MOUSEEVENTF_LEFTDOWN)

    assert key.ki.dwExtraInfo == agent_input.AGENT_TAG
    assert mouse.mi.dwExtraInfo == agent_input.AGENT_TAG
