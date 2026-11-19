"""Keep our own windows above other always-on-top windows.

Windows keeps every topmost window in one band. An app that is itself "always
on top" - the browser we automate, say - moves to the front of that band each
time it is activated, so a window that only set the topmost flag once ends up
underneath it. Re-asserting the position with ``SetWindowPos`` and
``SWP_NOACTIVATE`` keeps us in front without taking the focus away from the
app that is being automated.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# Often enough to win the band back after a click activates the target app,
# rare enough to cost nothing.
INTERVAL_MS = 400

_FLAGS = (
    "SWP_NOMOVE",
    "SWP_NOSIZE",
    "SWP_NOACTIVATE",
)

# Handles of our own always-on-top windows (the chat window, the panic overlay).
# A handle is plain data, so a worker thread can lift these windows without
# touching tkinter, which only its own thread may call.
_ours: set[int] = set()
_ours_lock = threading.Lock()


def _flags() -> int:
    import win32con

    flags = 0
    for name in _FLAGS:
        flags |= getattr(win32con, name)
    return flags


def _raise(handle: int) -> bool:
    """Put one window at the front of the topmost band, without focus."""
    try:
        import win32con
        import win32gui

        win32gui.SetWindowPos(handle, win32con.HWND_TOPMOST, 0, 0, 0, 0, _flags())
        return True
    except Exception:
        logger.debug("could not raise a window above the topmost band", exc_info=True)
        return False


def raise_above_topmost(widget: object) -> bool:
    """Move a tkinter toplevel to the front of the topmost band.

    The window is remembered, so it can be lifted again later from any thread
    with :func:`raise_our_windows`. Returns ``False`` when the platform or the
    widget cannot do it, so callers can stay silent about it.
    """
    try:
        import win32con
        import win32gui

        handle = widget.winfo_id()  # type: ignore[attr-defined]
        top = win32gui.GetAncestor(handle, win32con.GA_ROOT) or handle
    except Exception:
        logger.debug("could not find the window to raise", exc_info=True)
        return False
    with _ours_lock:
        _ours.add(top)
    return _raise(top)


def raise_our_windows() -> int:
    """Lift every window of ours back above a target app that just took focus.

    Activating a topmost window - which every click on the app we automate does
    - moves it to the front of the topmost band, above ours. Calling this right
    after the input that caused it keeps our window on top during a run, and
    costs one ``SetWindowPos`` per window of ours.
    """
    with _ours_lock:
        handles = list(_ours)
    return sum(1 for handle in handles if _raise(handle))


def forget_window(widget: object) -> None:
    """Stop lifting a window that has been closed."""
    try:
        import win32con
        import win32gui

        handle = widget.winfo_id()  # type: ignore[attr-defined]
        top = win32gui.GetAncestor(handle, win32con.GA_ROOT) or handle
    except Exception:
        return
    with _ours_lock:
        _ours.discard(top)


def keep_above_topmost(widget: object, interval_ms: int = INTERVAL_MS) -> bool:
    """Re-assert a tkinter window's topmost place for as long as it lives.

    Returns ``True`` when the repeating tick was scheduled.
    """
    if not raise_above_topmost(widget):
        return False

    def tick() -> None:
        raise_above_topmost(widget)
        try:
            widget.after(interval_ms, tick)  # type: ignore[attr-defined]
        except Exception:  # the window was destroyed while we waited
            logger.debug("stopped keeping a window on top", exc_info=True)

    try:
        widget.after(interval_ms, tick)  # type: ignore[attr-defined]
    except Exception:
        logger.debug("could not schedule keeping a window on top", exc_info=True)
        return False
    return True
