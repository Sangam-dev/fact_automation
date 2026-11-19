"""Read one window's full control tree, for capture and for learning.

This is the *capture* scanner: it keeps everything a screen draws, including
hidden and disabled controls, plus the Win32 control list, because a snapshot
is evidence to be learned from later. The automation-facing scanner in
:mod:`desktop.uia.uia` is the opposite - a narrower, already-filtered view
meant for aiming clicks.

``scripts/snapshots.py`` drives these functions from its window; the learner in
:mod:`desktop.state.learner` reads what it produced.
"""

from __future__ import annotations

import re
from typing import Callable

EDIT_LIKE = re.compile(r"edit|memo|combo|mask|spin|date", re.I)

Tick = Callable[[int], None] | None


def g(fn: Callable, default=None):
    """Call something that may fail on a dying window; return a default."""
    try:
        return fn()
    except Exception:
        return default


def rect_list(rect) -> list[int] | None:
    """A rectangle as a plain list, so it survives a JSON round trip."""
    if rect is None:
        return None
    try:
        return [rect.left, rect.top, rect.right, rect.bottom]
    except Exception:
        return None


# ------------------------------------------------------------------------ UIA --
def describe_uia(element, depth: int) -> dict:
    """One UIA element as a flat, JSON-safe record."""
    return {
        "d": depth,
        "type": g(lambda: element.control_type),
        "name": g(lambda: element.name),
        "id": g(lambda: element.automation_id),
        "cls": g(lambda: element.class_name),
        "framework": g(lambda: element.framework_id),
        "hwnd": g(lambda: element.handle),
        "rect": rect_list(g(lambda: element.rectangle)),
        "enabled": g(lambda: element.enabled),
        "visible": g(lambda: element.visible),
    }


def walk_uia(element, depth: int, out: list, cap: int, tick: Tick = None) -> None:
    """Depth-first walk of a UIA subtree, capped so a runaway tree ends."""
    if len(out) >= cap:
        return
    for child in g(lambda: element.children, []) or []:
        if len(out) >= cap:
            return
        out.append(describe_uia(child, depth))
        if tick is not None and len(out) % 25 == 0:
            tick(len(out))
        walk_uia(child, depth + 1, out, cap, tick)


def scan_uia(hwnd: int, cap: int = 3000, tick: Tick = None) -> dict:
    """Read the UIA subtree below one window handle."""
    from pywinauto.uia_element_info import UIAElementInfo

    root = UIAElementInfo(handle_or_elem=hwnd)
    items: list[dict] = []
    walk_uia(root, 1, items, cap, tick)
    return {"count": len(items), "capped": len(items) >= cap, "controls": items}


# ---------------------------------------------------------------------- Win32 --
def scan_win32(hwnd: int, cap: int = 3000, tick: Tick = None) -> dict | None:
    """Read the classic Win32 child controls below one window handle.

    Text of edit-like controls is replaced: it is whatever was typed, which may
    be a password or somebody's data, and a snapshot is meant to be shareable.
    """
    import ctypes

    from pywinauto.controls.hwndwrapper import HwndWrapper

    user32 = ctypes.windll.user32

    items: list[dict] = []
    capped = False
    try:
        window = HwndWrapper(hwnd)
    except Exception:
        return None
    for child in g(lambda: window.descendants, []) or []:
        if len(items) >= cap:
            capped = True
            break
        class_name = g(lambda: child.class_name, "")
        text = g(lambda: child.window_text, "")
        if EDIT_LIKE.search(class_name or ""):
            text = "<redacted>"
        items.append(
            {
                "hwnd": int(g(lambda: child.handle, 0) or 0),
                "parent": int(user32.GetParent(int(g(lambda: child.handle, 0) or 0)) or 0),
                "cls": class_name,
                "text": text,
                "ctrl_id": g(child.control_id),
                "rect": rect_list(g(lambda: child.rectangle)),
                "enabled": g(child.is_enabled),
                "visible": g(child.is_visible),
            }
        )
        if tick is not None and len(items) % 25 == 0:
            tick(len(items))
    return {"count": len(items), "capped": capped, "controls": items}


# -------------------------------------------------------------------- outline --
def outline_text(snapshot: dict) -> str:
    """A snapshot as the readable tree a person can check at a glance."""
    lines = [
        f"SNAPSHOT {snapshot['label']}  {snapshot['timestamp']}",
        f"process: {snapshot['process']}",
        "",
    ]
    for window in snapshot["windows"]:
        lines.append("=" * 78)
        lines.append(
            f"WINDOW #{window['z_order']} title={window['title']!r} "
            f"class={window['class']!r} hwnd={window['hwnd']} "
            f"foreground={window['is_foreground']} picked={window['is_picked']}"
        )
        uia = window.get("uia")
        if uia:
            lines.append(
                f"  -- UIA tree: {uia['count']} controls"
                f"{' (CAPPED)' if uia['capped'] else ''}"
            )
            for control in uia["controls"]:
                flags = ""
                if control["enabled"] is False:
                    flags += " [disabled]"
                if control["visible"] is False:
                    flags += " [hidden]"
                lines.append(
                    f"  {'  ' * control['d']}[{control['type']}] "
                    f"name={control['name']!r} id={control['id']!r} "
                    f"cls={control['cls']!r}{flags}"
                )
        win32 = window.get("win32")
        if win32:
            lines.append(
                f"  -- Win32 tree: {win32['count']} controls"
                f"{' (CAPPED)' if win32['capped'] else ''}"
            )
            for control in win32["controls"]:
                lines.append(
                    f"    hwnd={control['hwnd']} parent={control['parent']} "
                    f"cls={control['cls']!r} text={control['text']!r} "
                    f"ctrl_id={control['ctrl_id']}"
                )
        lines.append("")
    return "\n".join(lines)


def scan_window(
    hwnd: int,
    *,
    cap: int = 3000,
    with_win32: bool = True,
    tick: Tick = None,
) -> dict:
    """Both trees for one window, as a snapshot entry stores them."""
    entry: dict = {"uia": scan_uia(hwnd, cap, tick)}
    if with_win32:
        entry["win32"] = scan_win32(hwnd, cap, tick)
    return entry


__all__ = [
    "EDIT_LIKE",
    "describe_uia",
    "g",
    "outline_text",
    "rect_list",
    "scan_uia",
    "scan_window",
    "scan_win32",
    "walk_uia",
]
