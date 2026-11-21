"""Unit tests for screen models, window matching, capture, and annotation."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from desktop.screenshots.capture import Capture, _has_black_padding
from desktop.windows.window import (
    WindowAmbiguousError,
    WindowNotFoundError,
    _process_matches,
    find_window,
)
from desktop.types import Rect, UIACandidate, WindowInfo
from desktop.screenshots.images import annotate_candidates


def _window(title: str, process_name: str = "app.exe") -> WindowInfo:
    return WindowInfo(
        handle=1,
        title=title,
        process_name=process_name,
        class_name="TestWindow",
        rect=Rect(0, 0, 100, 100),
        client_rect=Rect(0, 0, 100, 100),
        dpi=96,
    )


def test_rect_geometry_and_edges() -> None:
    rect = Rect(10, 20, 110, 220)

    assert rect.width == 100
    assert rect.height == 200
    assert rect.center == (60, 120)
    assert rect.contains(10, 20)
    assert rect.contains(109, 219)
    assert not rect.contains(110, 120)
    assert not rect.contains(60, 220)


def test_rect_never_reports_negative_size() -> None:
    rect = Rect(100, 100, 40, 40)

    assert rect.width == 0
    assert rect.height == 0


def test_rect_intersects_uses_exclusive_edges() -> None:
    rect = Rect(0, 0, 100, 100)

    assert rect.intersects(Rect(50, 50, 150, 150))
    assert rect.intersects(Rect(99, 99, 200, 200))
    assert not rect.intersects(Rect(100, 0, 200, 100))
    assert not rect.intersects(Rect(0, 100, 100, 200))


def test_process_matches_accepts_name_with_or_without_exe() -> None:
    assert _process_matches("notepad.exe", "notepad")
    assert _process_matches("notepad", "notepad.exe")
    assert _process_matches("Notepad.EXE", "notepad")
    assert not _process_matches("notepad.exe", "calc")
    assert not _process_matches("", "notepad")


def test_find_window_requires_a_filter() -> None:
    with pytest.raises(ValueError, match="required"):
        find_window()


def test_find_window_returns_the_single_match(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _window("Excel Agent Test Form", "powershell.exe")
    monkeypatch.setattr(
        "desktop.windows.window.list_windows",
        lambda: [_window("Other"), target],
    )

    assert find_window("test form") is target


def test_find_window_reports_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "desktop.windows.window.list_windows",
        lambda: [_window("Other")],
    )

    with pytest.raises(WindowNotFoundError, match="no visible window"):
        find_window("missing")


def test_find_window_rejects_ambiguous_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "desktop.windows.window.list_windows",
        lambda: [_window("Test A"), _window("Test B")],
    )

    with pytest.raises(WindowAmbiguousError, match="2 windows matched"):
        find_window("test")


def test_black_padding_detects_dpi_virtualized_capture() -> None:
    image = Image.new("RGB", (200, 100), "black")
    ImageDraw.Draw(image).rectangle([0, 0, 159, 79], fill="white")

    assert _has_black_padding(image)


def test_full_content_is_not_black_padding() -> None:
    assert not _has_black_padding(Image.new("RGB", (200, 100), "white"))


def test_capture_maps_image_pixels_to_screen() -> None:
    window = _window("Test")
    capture = Capture(
        image=Image.new("RGB", (200, 100), "white"),
        origin=(100, 200),
        scale_x=0.5,
        scale_y=2.0,
        window=window,
    )

    assert capture.to_screen(0, 0) == (100, 200)
    assert capture.to_screen(10, 20) == (105, 240)


def test_an_image_button_takes_its_label_from_the_automation_id() -> None:
    candidate = UIACandidate(
        candidate_id="W1",
        name="System.Drawing.Bitmap",
        control_type="Pane",
        automation_id="btnLogin",
        class_name="WindowsForms10.Window.b.app",
        rect=Rect(10, 10, 110, 44),
        value="",
        enabled=True,
    )

    assert candidate.label == "btnLogin"


def test_a_captioned_button_keeps_its_caption() -> None:
    candidate = UIACandidate(
        candidate_id="W1",
        name="Save && New",
        control_type="Button",
        automation_id="hlblSave_New",
        class_name="WindowsForms10.Window.b.app",
        rect=Rect(10, 10, 110, 44),
        value="",
        enabled=True,
    )

    assert candidate.label == "Save && New"


def test_a_small_captioned_pane_counts_as_a_button() -> None:
    from desktop.uia.uia import _looks_like_button

    assert _looks_like_button(Rect(0, 0, 116, 33), "Login", "")
    assert _looks_like_button(
        Rect(0, 0, 900, 900), "System.Drawing.Bitmap", "btnLogin"
    )
    assert not _looks_like_button(Rect(0, 0, 600, 400), "Panel", "")
    assert not _looks_like_button(Rect(0, 0, 900, 900), "", "")


def test_annotate_candidates_draws_boxes_and_keeps_the_source(
    tmp_path: Path,
) -> None:
    image = Image.new("RGB", (200, 100), "white")
    candidate = UIACandidate(
        candidate_id="W1",
        name="Save",
        control_type="Button",
        automation_id="btnSave",
        class_name="Button",
        rect=Rect(60, 50, 120, 90),
        value="",
        enabled=True,
    )
    output = tmp_path / "annotated.png"

    annotate_candidates(image, [candidate], (50, 40), output)

    assert output.is_file()
    assert image.getpixel((10, 10)) == (255, 255, 255)
    assert Image.open(output).getpixel((10, 10)) == (255, 0, 0)


def test_annotate_candidates_skips_offscreen_boxes(tmp_path: Path) -> None:
    image = Image.new("RGB", (50, 50), "white")
    candidate = UIACandidate(
        candidate_id="W1",
        name="Hidden",
        control_type="Button",
        automation_id="",
        class_name="Button",
        rect=Rect(500, 500, 520, 520),
        value="",
        enabled=True,
    )
    output = tmp_path / "annotated.png"

    annotate_candidates(image, [candidate], (0, 0), output)

    assert output.is_file()
    assert Image.open(output).getpixel((0, 0)) == (255, 255, 255)


def test_pinning_an_app_lifts_new_windows_and_leaves_pinned_ones_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from desktop.windows import window as windows
    import win32con

    windows_found = [
        SimpleNamespace(handle=10, process_name="FACTERPNG.exe", title="main"),
        SimpleNamespace(handle=20, process_name="FACTERPNG.exe", title="form"),
        SimpleNamespace(handle=30, process_name="Code.exe", title="other"),
    ]
    already = {10}  # the window we pinned earlier
    calls: list[tuple] = []

    monkeypatch.setattr(windows, "list_windows", lambda: windows_found)
    monkeypatch.setattr(windows, "is_topmost", lambda handle: handle in already)
    monkeypatch.setattr(
        windows.win32gui,
        "SetWindowPos",
        lambda handle, insert_after, *args: calls.append((handle, insert_after)),
    )

    pinned = windows.pin_app_windows("FACTERPNG.exe")

    # Only the window that is not topmost yet is moved, so the keeper stops
    # pulling the target in front of our own chat window every tick.
    assert pinned == [20]
    assert calls == [(20, win32con.HWND_TOPMOST)]

    calls.clear()
    unpinned = windows.pin_app_windows("FACTERPNG.exe", pin=False)
    assert unpinned == [10]
    assert calls == [(10, win32con.HWND_NOTOPMOST)]


def test_release_pinned_windows_stops_keeper_before_unpinning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import Event

    from desktop.windows import window as windows

    calls: list[tuple] = []

    class Keeper:
        stopped = Event()

        def join(self) -> None:
            calls.append(("join", self.stopped.is_set()))

    keeper = Keeper()
    windows._keepers["FACTERP.NG.exe"] = keeper  # type: ignore[assignment]
    monkeypatch.setattr(
        windows,
        "pin_app_windows",
        lambda process_name, pin=True: calls.append(("unpin", process_name, pin)) or [10, 20],
    )

    result = windows.release_pinned_windows("FACTERP.NG")

    assert result == [10, 20]
    assert calls == [
        ("join", True),
        ("unpin", "FACTERP.NG", False),
    ]
    assert "FACTERP.NG.exe" not in windows._keepers


def test_release_pinned_windows_without_process_is_a_noop(monkeypatch) -> None:
    from desktop.windows import window as windows

    monkeypatch.setattr(
        windows,
        "pin_app_windows",
        lambda *args, **kwargs: pytest.fail("must not enumerate windows"),
    )

    assert windows.release_pinned_windows(None) == []


def test_pin_and_unpin_window(monkeypatch: pytest.MonkeyPatch) -> None:
    from desktop.windows.window import pin_window, pinned_window
    import win32con

    flags = (
        win32con.SWP_NOMOVE
        | win32con.SWP_NOSIZE
        | win32con.SWP_SHOWWINDOW
        | win32con.SWP_NOACTIVATE
    )
    calls = []

    def mock_set_window_pos(hwnd, insert_after, x, y, cx, cy, flags):
        calls.append((hwnd, insert_after, flags))
        return 1

    monkeypatch.setattr("win32gui.SetWindowPos", mock_set_window_pos)

    assert pin_window(100, pin=True) is True
    assert pin_window(100, pin=False) is True
    assert len(calls) == 2
    assert calls[0][1] == win32con.HWND_TOPMOST
    assert calls[1][1] == win32con.HWND_NOTOPMOST

    calls.clear()
    with pinned_window(200):
        assert len(calls) == 1
        assert calls[0] == (200, win32con.HWND_TOPMOST, flags)
    assert len(calls) == 2
    assert calls[1] == (200, win32con.HWND_NOTOPMOST, flags)

