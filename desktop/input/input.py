"""Low-level mouse/keyboard input tagged as agent input (Segment 3).

Every event carries :data:`AGENT_TAG` in ``dwExtraInfo``. The input lock treats
tagged events as its own and lets them through, while blocking external input.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import time
from typing import Any

import pyperclip

from desktop.windows.points import point_owned_by
from desktop.windows.topmost import raise_our_windows
from desktop.windows.window import is_foreground

_user32 = ctypes.WinDLL("user32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
ULONG = wintypes.ULONG
DWORD = wintypes.DWORD
WORD = wintypes.WORD
LONG = wintypes.LONG

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010

VK_CONTROL = 0x11
VK_A = 0x41
VK_DELETE = 0x2E
VK_TAB = 0x09
VK_ESCAPE = 0x1B
VK_V = 0x56
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_MENU = 0x12  # Alt
VK_SPACE = 0x20
VK_BACK = 0x08
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_HOME = 0x24
VK_END = 0x23
VK_PRIOR = 0x21  # Page Up
VK_NEXT = 0x22  # Page Down

#: Key names a plan may ask for, written the way a person writes them.
#: Everything is case-insensitive; ``Ctrl+S`` is split into modifier + key.
NAMED_KEYS: dict[str, int] = {
    "enter": VK_RETURN,
    "return": VK_RETURN,
    "tab": VK_TAB,
    "esc": VK_ESCAPE,
    "escape": VK_ESCAPE,
    "space": VK_SPACE,
    "backspace": VK_BACK,
    "delete": VK_DELETE,
    "del": VK_DELETE,
    "left": VK_LEFT,
    "up": VK_UP,
    "right": VK_RIGHT,
    "down": VK_DOWN,
    "home": VK_HOME,
    "end": VK_END,
    "pageup": VK_PRIOR,
    "pagedown": VK_NEXT,
    "ctrl": VK_CONTROL,
    "control": VK_CONTROL,
    "shift": VK_SHIFT,
    "alt": VK_MENU,
}

for _digit in range(10):
    NAMED_KEYS[str(_digit)] = 0x30 + _digit
for _letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    NAMED_KEYS[_letter.casefold()] = ord(_letter)
for _number in range(1, 25):
    NAMED_KEYS[f"f{_number}"] = 0x6F + _number

# A private value that identifies events injected by this agent. Low-level
# hooks compare against it; nothing else in the system uses this value.
AGENT_TAG = 0x45584C41  # "EXLA"


class InputError(RuntimeError):
    """An input event could not be sent."""


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", LONG),
        ("dy", LONG),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", WORD),
        ("wScan", WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", DWORD),
        ("wParamL", WORD),
        ("wParamH", WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [
        ("type", DWORD),
        ("value", _INPUTUNION),
    ]


_user32.SendInput.argtypes = [
    wintypes.UINT,
    ctypes.POINTER(INPUT),
    ctypes.c_int,
]
_user32.SendInput.restype = wintypes.UINT
_user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_user32.SetCursorPos.restype = wintypes.BOOL


def _key_input(vk: int, scan: int, flags: int) -> INPUT:
    return INPUT(
        type=INPUT_KEYBOARD,
        ki=KEYBDINPUT(
            wVk=vk,
            wScan=scan,
            dwFlags=flags,
            time=0,
            dwExtraInfo=AGENT_TAG,
        ),
    )


def _mouse_input(flags: int) -> INPUT:
    return INPUT(
        type=INPUT_MOUSE,
        mi=MOUSEINPUT(
            dx=0,
            dy=0,
            mouseData=0,
            dwFlags=flags,
            time=0,
            dwExtraInfo=AGENT_TAG,
        ),
    )


def _utf16_units(text: str) -> list[int]:
    """Split text into UTF-16 code units (surrogate pairs for non-BMP)."""
    data = text.encode("utf-16-le")
    return [
        int.from_bytes(data[index : index + 2], "little")
        for index in range(0, len(data), 2)
    ]


def _unicode_inputs(text: str) -> list[INPUT]:
    inputs: list[INPUT] = []
    for unit in _utf16_units(text):
        inputs.append(_key_input(0, unit, KEYEVENTF_UNICODE))
        inputs.append(_key_input(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return inputs


def _send(inputs: list[INPUT]) -> None:
    if not inputs:
        return
    array = (INPUT * len(inputs))(*inputs)
    sent = _user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise InputError(
            f"SendInput sent {sent} of {len(inputs)} events "
            f"(error {ctypes.get_last_error()})"
        )
    # The click has just been delivered and the app we automate is now active,
    # which put it in front of our chat window. Take the front back, without
    # stealing the focus the click just gave it.
    raise_our_windows()


def type_text(text: str) -> None:
    """Type text as Unicode key events; the target window must have focus."""
    _send(_unicode_inputs(text))


def press_key(vk: int) -> None:
    """Press and release one virtual key."""
    _send([_key_input(vk, 0, 0), _key_input(vk, 0, KEYEVENTF_KEYUP)])


def press_chord(modifier_vk: int, key_vk: int) -> None:
    """Press modifier + key, then release both."""
    _send(
        [
            _key_input(modifier_vk, 0, 0),
            _key_input(key_vk, 0, 0),
            _key_input(key_vk, 0, KEYEVENTF_KEYUP),
            _key_input(modifier_vk, 0, KEYEVENTF_KEYUP),
        ]
    )


def paste_text(text: str) -> None:
    """Put text on the clipboard and send Ctrl+V.

    The clipboard is left with the pasted text; the next paste overwrites it.
    """
    try:
        pyperclip.copy(text)
    except Exception as exc:
        raise InputError(f"could not copy to the clipboard: {exc}") from exc
    # Give the clipboard a moment to settle before the target reads it.
    time.sleep(0.05)
    press_chord(VK_CONTROL, VK_V)


def click_checked(
    x: int,
    y: int,
    window: Any,
    *,
    require_foreground: bool = False,
    button: str = "left",
) -> None:
    """Click a point only while it still belongs to the target window.

    A coordinate read from a UI Automation snapshot goes stale as soon as the
    user moves, covers, or minimises the window - and the mouse event would be
    delivered to whatever is under the point instead. Nothing is sent when the
    check fails, so a stale coordinate can never click another application.
    """
    if not point_owned_by(window, x, y):
        raise InputError(
            f"nothing was clicked: ({x}, {y}) is not over {window.title!r} "
            "any more - the window moved, was covered, or was minimised"
        )
    if require_foreground and not is_foreground(window.handle):
        raise InputError(
            f"nothing was clicked: {window.title!r} is not in the foreground"
        )
    click(x, y, button)


def click(x: int, y: int, button: str = "left") -> None:
    """Move the cursor to a screen point and click it."""
    if not _user32.SetCursorPos(int(x), int(y)):
        raise InputError(f"could not move the cursor to ({x}, {y})")
    if button == "left":
        _send([_mouse_input(MOUSEEVENTF_LEFTDOWN), _mouse_input(MOUSEEVENTF_LEFTUP)])
    elif button == "right":
        _send([_mouse_input(MOUSEEVENTF_RIGHTDOWN), _mouse_input(MOUSEEVENTF_RIGHTUP)])
    else:
        raise ValueError(f"unsupported mouse button: {button!r}")


def double_click_checked(
    x: int,
    y: int,
    window: Any,
    *,
    require_foreground: bool = False,
) -> None:
    """Double-click a point only while it still belongs to the target window.

    Same rule as :func:`click_checked`: a stale coordinate must never send a
    double-click into whatever happens to be under it.
    """
    if not point_owned_by(window, x, y):
        raise InputError(
            f"nothing was double-clicked: ({x}, {y}) is not over {window.title!r} "
            "any more - the window moved, was covered, or was minimised"
        )
    if require_foreground and not is_foreground(window.handle):
        raise InputError(
            f"nothing was double-clicked: {window.title!r} is not in the foreground"
        )
    double_click(x, y)


def double_click(x: int, y: int, button: str = "left") -> None:
    """Move to a point and click it twice, in the same place.

    FACT opens records by double-clicking a list row, and it only opens when
    both clicks land on the same control with no pause that the app mistakes
    for two separate clicks, so the two press/release pairs are sent together.
    """
    if not _user32.SetCursorPos(int(x), int(y)):
        raise InputError(f"could not move the cursor to ({x}, {y})")
    if button != "left":
        raise ValueError(f"a double-click is only sent with the left button: {button!r}")
    down = _mouse_input(MOUSEEVENTF_LEFTDOWN)
    up = _mouse_input(MOUSEEVENTF_LEFTUP)
    _send([down, up, down, up])


def key_vk(name: str) -> int:
    """The virtual-key code for one key name (``Enter``, ``Tab``, ``F5``)."""
    key = (name or "").strip().casefold()
    if key not in NAMED_KEYS:
        raise InputError(f"unknown key {name!r}; use Enter, Tab, Esc, F1-F24 or a letter")
    return NAMED_KEYS[key]


def press_keys(spec: str) -> None:
    """Press one key, or a chord written like ``Ctrl+S`` / ``Shift+Tab``.

    Accepts at most one modifier: that is what a form needs (commit a value,
    move to the next field), and rejecting the rest keeps a typo from sending
    something surprising to the application.
    """
    parts = [part.strip() for part in str(spec or "").split("+") if part.strip()]
    if not parts:
        raise InputError("a key step must name a key, for example Enter or Ctrl+S")
    if len(parts) == 1:
        press_key(key_vk(parts[0]))
        return
    if len(parts) != 2:
        raise InputError(
            f"{spec!r} names more than one modifier; write it as Ctrl+S, Shift+Tab or Alt+F4"
        )
    modifier = key_vk(parts[0])
    if modifier not in (VK_CONTROL, VK_SHIFT, VK_MENU):
        raise InputError(f"{parts[0]!r} is not a modifier; use Ctrl, Shift or Alt")
    press_chord(modifier, key_vk(parts[1]))
