"""Open Remarks, fill the JSON remarks value, and confirm the dialog."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from execution.scripts.form_filler_1 import (
    Stop,
    _top_windows,
    _window_process_id,
    agent_click,
    agent_replace_text,
    control_value,
    dialog_control,
    obey_hotkeys,
    settle,
)

SALE_ORDER_TITLE = "Sale Order - Add"
REMARKS_TITLE = "Remarks, Internal Notes"
REMARKS_DIALOG_ID = "frmOthDetail"
REMARKS_BUTTON_ID = "btnREMARKS"
REMARKS_FIELD_ID = "meRemarks"
OK_BUTTON_ID = "btnOK"
DIALOG_TIMEOUT_SECONDS = 20.0


def read_remarks(document: Any) -> str:
    """Read a non-empty top-level ``remarks`` or ``header.remarks`` value."""
    if not isinstance(document, dict):
        raise Stop("the JSON root must be an object")

    candidates = []
    if "remarks" in document:
        candidates.append(("remarks", document["remarks"]))
    header = document.get("header")
    if isinstance(header, dict) and "remarks" in header:
        candidates.append(("header.remarks", header["remarks"]))
    if not candidates:
        raise Stop("missing required JSON value 'remarks' (or 'header.remarks')")

    values = [(path, str(value).strip()) for path, value in candidates]
    if any(not value for _, value in values):
        raise Stop("the remarks value must not be empty")
    if len({value for _, value in values}) > 1:
        raise Stop("'remarks' and 'header.remarks' contain different values")
    return values[0][1]


def load_remarks(path: str | Path) -> str:
    try:
        with open(path, encoding="utf-8") as source:
            document = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise Stop(f"could not read the values JSON: {exc}") from exc
    return read_remarks(document)


def _normalise_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _find_sale_order(desktop):
    candidates = []
    for window in desktop.windows():
        try:
            title = (window.window_text() or "").strip()
            if not title.endswith(SALE_ORDER_TITLE) or not window.is_visible():
                continue
            candidates.append(window)
        except Exception:
            continue

    if len(candidates) != 1:
        raise Stop(
            f"expected one visible Sale Order Add form, found {len(candidates)}"
        )
    return candidates[0]


def _find_dialog(desktop, process_id: int):
    deadline = time.monotonic() + DIALOG_TIMEOUT_SECONDS
    last_candidate = "none"
    while time.monotonic() < deadline:
        obey_hotkeys("waiting for the Remarks dialog")
        matches = []
        for hwnd, title, class_name, _, _ in _top_windows():
            if title.strip() != REMARKS_TITLE:
                continue
            try:
                window_pid = _window_process_id(hwnd)
            except Exception as exc:
                last_candidate = f"PID unavailable ({type(exc).__name__}: {exc})"
                continue
            last_candidate = (
                f"title={title!r}, class={class_name!r}, pid={window_pid}"
            )
            if window_pid != process_id:
                continue
            try:
                window = desktop.window(handle=int(hwnd)).wrapper_object()
                automation_id = window.element_info.automation_id or ""
                if automation_id == REMARKS_DIALOG_ID and window.is_visible():
                    matches.append(window)
                else:
                    last_candidate += f", automation_id={automation_id!r}"
            except Exception as exc:
                last_candidate += f", UIA attach failed ({type(exc).__name__}: {exc})"

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise Stop("more than one Remarks dialog is open in the Sale Order process")
        time.sleep(0.15)

    raise Stop(
        f"Remarks dialog did not appear in the Sale Order process; last candidate: "
        f"{last_candidate}"
    )


def _focus_sale_order(desktop, form):
    from desktop.windows.window import focus_window, get_window

    handle = int(form.handle)
    try:
        if not focus_window(get_window(handle)):
            raise Stop("could not bring the Sale Order Add form to the foreground")
        time.sleep(0.2)
        form = desktop.window(handle=handle).wrapper_object()
    except Stop:
        raise
    except Exception as exc:
        raise Stop(f"could not focus the Sale Order Add form: {exc}") from exc

    if not form.is_visible() or not form.is_enabled():
        raise Stop("the Sale Order Add form is not active after focusing it")
    return form


def _click_remarks_button(form) -> None:
    matches = [
        control
        for control in form.descendants()
        if (control.element_info.automation_id or "") == REMARKS_BUTTON_ID
    ]
    if len(matches) != 1:
        raise Stop(
            f"Remarks button {REMARKS_BUTTON_ID!r} matched {len(matches)} controls; "
            "expected exactly one"
        )
    if not matches[0].is_enabled() or not matches[0].is_visible():
        raise Stop("the Remarks button is not enabled and visible")
    agent_click(matches[0], form, "opening Remarks")


def fill_remarks(remarks: str) -> None:
    from pywinauto import Desktop

    desktop = Desktop(backend="uia")
    form = _find_sale_order(desktop)
    if not form.is_enabled():
        raise Stop("the Sale Order Add form is disabled; another dialog may be open")
    form = _focus_sale_order(desktop, form)
    process_id = int(form.element_info.process_id)

    _click_remarks_button(form)
    dialog = _find_dialog(desktop, process_id)

    remarks_field = dialog_control(dialog, REMARKS_FIELD_ID, "Edit")
    if not remarks_field.is_enabled():
        raise Stop("the Remarks field is not editable")
    agent_click(remarks_field, dialog, "focusing the Remarks field")
    time.sleep(0.1)
    agent_replace_text(remarks, "entering Remarks")

    deadline = time.monotonic() + 3.0
    actual = control_value(remarks_field)
    while _normalise_text(actual) != _normalise_text(remarks) and time.monotonic() < deadline:
        obey_hotkeys("verifying Remarks")
        time.sleep(0.1)
        actual = control_value(remarks_field)
    if _normalise_text(actual) != _normalise_text(remarks):
        raise Stop(
            f"Remarks field read back as {actual!r}, but the JSON value did not match"
        )

    ok_button = dialog_control(dialog, OK_BUTTON_ID)
    agent_click(ok_button, dialog, "confirming Remarks")

    close_deadline = time.monotonic() + 10.0
    while time.monotonic() < close_deadline:
        obey_hotkeys("waiting for the Remarks dialog to close")
        try:
            if dialog.is_visible():
                time.sleep(0.1)
                continue
        except Exception:
            pass
        settle("Remarks")
        break
    else:
        raise Stop("the Remarks dialog did not close after clicking OK")

    form_deadline = time.monotonic() + 5.0
    while time.monotonic() < form_deadline:
        try:
            if form.is_visible() and form.is_enabled():
                print("  remarks: entered, verified, and confirmed")
                return
        except Exception:
            pass
        obey_hotkeys("waiting for the Sale Order form after Remarks")
        time.sleep(0.1)
    raise Stop("Remarks closed, but the Sale Order Add form did not become active")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("values_file", help="JSON file containing a 'remarks' value")
    parser.add_argument(
        "--check", action="store_true", help="validate the JSON without touching FACT"
    )
    args = parser.parse_args()

    try:
        remarks = load_remarks(args.values_file)
        if args.check:
            print(f"OK: remarks value present ({len(remarks)} characters)")
            return 0
        if sys.platform != "win32":
            print("Windows only.", file=sys.stderr)
            return 1
        from desktop.input.input import InputError

        try:
            fill_remarks(remarks)
        except InputError as exc:
            raise Stop(f"a checked UI click was refused: {exc}") from exc
    except Stop as exc:
        print("STOPPED:", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
