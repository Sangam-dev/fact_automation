"""Open Global UDF and fill its first page, leaving the dialog open."""

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
)

SALE_ORDER_TITLE = "Sale Order - Add"
UDF_TITLE = "Enter Global Level UDF Details"
UDF_DIALOG_ID = "frmGlobalUDF"
GLOBAL_UDF_BUTTON_ID = "btnUDF"
DIALOG_TIMEOUT_SECONDS = 20.0
TEXT_FIELD_IDS = {
    "F1": "note",
    "F2": "deliveryContact",
    "F3": "deliveryContactNumber",
    "F4": "deliveryAddress",
    "F7": "order_remarks",
    "F16": "lcNumber",
}
SALES_TYPE_OPTIONS = {
    "AGAINST PDC": "Against PDC",
    "PDC": "Against PDC",
    "AGAINST LC": "Against LC",
    "LC": "Against LC",
    "AGAINST CASH": "Against Cash",
    "CASH": "Against Cash",
    "AGAINST BG": "Against BG",
    "BG": "Against BG",
}
LOADING_POINT_OPTIONS = {
    "BHAIRAHAWA PLANT": "Bhairahawa Plant",
    "BHAIRAHAWA": "Bhairahawa Plant",
    "SUNWOL PLANT": "Sunwol Plant",
    "SUNWOL": "Sunwol Plant",
}


def _text_value(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if value is None or not str(value).strip():
        return "."
    return str(value).strip()


def _normalise(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().upper()


def _dropdown_value(document: dict[str, Any], key: str, options: dict[str, str]) -> str:
    value = document.get(key)
    if value is None or not str(value).strip():
        raise Stop(f"missing required dropdown value {key!r}")
    token = _normalise(value)
    if token not in options:
        raise Stop(
            f"unrecognized {key} value {value!r}; expected one of "
            f"{sorted(options)}"
        )
    return options[token]


def read_udf_values(document: Any) -> tuple[dict[str, str], dict[str, str]]:
    """Map the supported sales-order JSON keys to first-page UDF values."""
    if not isinstance(document, dict):
        raise Stop("the JSON root must be an object")
    text_values = {field_id: _text_value(document, key) for field_id, key in TEXT_FIELD_IDS.items()}
    dropdown_values = {
        "F19": _dropdown_value(document, "paymentMethod", SALES_TYPE_OPTIONS),
        "F18": _dropdown_value(document, "plant", LOADING_POINT_OPTIONS),
    }
    return text_values, dropdown_values


def load_udf_values(path: str | Path) -> tuple[dict[str, str], dict[str, str]]:
    try:
        with open(path, encoding="utf-8") as source:
            document = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise Stop(f"could not read the values JSON: {exc}") from exc
    return read_udf_values(document)


def _find_sale_order(desktop):
    matches = []
    for window in desktop.windows():
        try:
            title = (window.window_text() or "").strip()
            if title.endswith(SALE_ORDER_TITLE) and window.is_visible():
                matches.append(window)
        except Exception:
            continue
    if len(matches) != 1:
        raise Stop(f"expected one visible Sale Order Add form, found {len(matches)}")
    return matches[0]


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


def _click_udf_opener(form) -> None:
    matches = []
    id_matches = []
    controls = form.descendants()
    for control in controls:
        try:
            if (control.element_info.automation_id or "") != GLOBAL_UDF_BUTTON_ID:
                continue
            name = control.element_info.name or ""
            visible = control.is_visible()
            enabled = control.is_enabled()
            id_matches.append((name, visible, enabled))
            if visible and enabled:
                matches.append(control)
        except Exception as exc:
            id_matches.append(("<state unavailable>", False, False, str(exc)))

    if len(matches) != 1:
        details = "; ".join(repr(item) for item in id_matches) or "no such ID in the form tree"
        raise Stop(
            f"Global UDF opener {GLOBAL_UDF_BUTTON_ID!r} matched "
            f"{len(matches)} visible, enabled controls; expected exactly one. "
            f"ID candidates: {details}."
        )
    agent_click(matches[0], form, "opening Global UDF")


def _find_udf_dialog(desktop, process_id: int):
    deadline = time.monotonic() + DIALOG_TIMEOUT_SECONDS
    last_candidate = "none"
    while time.monotonic() < deadline:
        obey_hotkeys("waiting for the Global UDF dialog")
        matches = []
        for hwnd, title, class_name, _, _ in _top_windows():
            if title.strip() != UDF_TITLE:
                continue
            try:
                window_pid = _window_process_id(hwnd)
                last_candidate = f"title={title!r}, class={class_name!r}, pid={window_pid}"
                if window_pid != process_id:
                    continue
                window = desktop.window(handle=int(hwnd)).wrapper_object()
                if (
                    (window.element_info.automation_id or "") == UDF_DIALOG_ID
                    and window.is_visible()
                ):
                    matches.append(window)
            except Exception as exc:
                last_candidate = f"UIA attach failed ({type(exc).__name__}: {exc})"
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise Stop("more than one Global UDF dialog is open in the Sale Order process")
        time.sleep(0.15)
    raise Stop(
        "Global UDF dialog did not appear in the Sale Order process; "
        f"last candidate: {last_candidate}"
    )


def _verify_value(control, expected: str, field_name: str) -> None:
    deadline = time.monotonic() + 3.0
    actual = control_value(control)
    while _normalise(actual) != _normalise(expected) and time.monotonic() < deadline:
        obey_hotkeys(f"verifying {field_name}")
        time.sleep(0.1)
        actual = control_value(control)
    if _normalise(actual) != _normalise(expected):
        raise Stop(f"{field_name} read back as {actual!r}; expected {expected!r}")


def _fill_text_field(dialog, field_id: str, value: str) -> None:
    field = dialog_control(dialog, field_id, "Edit")
    if not field.is_enabled() or not field.is_visible():
        raise Stop(f"Global UDF field {field_id} is not editable and visible")
    agent_click(field, dialog, f"focusing Global UDF field {field_id}")
    time.sleep(0.1)
    agent_replace_text(value, f"entering Global UDF field {field_id}")
    _verify_value(field, value, f"Global UDF field {field_id}")


def _select_dropdown(dialog, field_id: str, expected: str) -> None:
    combo = dialog_control(dialog, field_id, "ComboBox")
    if not combo.is_enabled() or not combo.is_visible():
        raise Stop(f"Global UDF dropdown {field_id} is not enabled and visible")

    selected = _normalise(control_value(combo))
    if selected != _normalise(expected):
        try:
            combo.expand()
        except Exception as exc:
            raise Stop(f"could not expand Global UDF dropdown {field_id}: {exc}") from exc
        time.sleep(0.15)
        choices = [
            item for item in combo.descendants()
            if item.element_info.control_type == "ListItem"
            and _normalise(item.window_text()) == _normalise(expected)
        ]
        if len(choices) != 1:
            raise Stop(
                f"dropdown {field_id} option {expected!r} matched {len(choices)} "
                "list items; expected exactly one"
            )
        try:
            choices[0].iface_selection_item.Select()
        except Exception as exc:
            raise Stop(
                f"could not select {expected!r} in Global UDF dropdown {field_id}: {exc}"
            ) from exc

    _verify_value(combo, expected, f"Global UDF dropdown {field_id}")


def fill_udf_page(text_values: dict[str, str], dropdown_values: dict[str, str]) -> None:
    from pywinauto import Desktop

    desktop = Desktop(backend="uia")
    form = _find_sale_order(desktop)
    if not form.is_enabled():
        raise Stop("the Sale Order Add form is disabled; another dialog may be open")
    form = _focus_sale_order(desktop, form)
    process_id = int(form.element_info.process_id)

    _click_udf_opener(form)
    dialog = _find_udf_dialog(desktop, process_id)

    # This deterministic filler intentionally handles only the No Group page.
    for field_id, value in text_values.items():
        _fill_text_field(dialog, field_id, value)
    for field_id, value in dropdown_values.items():
        _select_dropdown(dialog, field_id, value)

    if not dialog.is_visible():
        raise Stop("the Global UDF dialog closed unexpectedly after filling")
    print("  global udf: first page entered and verified; dialog left open")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("values_file", help="JSON file containing Global UDF values")
    parser.add_argument("--check", action="store_true", help="validate JSON only")
    args = parser.parse_args()
    try:
        text_values, dropdown_values = load_udf_values(args.values_file)
        if args.check:
            print(
                "OK: Global UDF values present "
                f"({len(text_values)} text fields, {len(dropdown_values)} dropdowns)"
            )
            return 0
        if sys.platform != "win32":
            print("Windows only.", file=sys.stderr)
            return 1
        from desktop.input.input import InputError

        try:
            fill_udf_page(text_values, dropdown_values)
        except InputError as exc:
            raise Stop(f"a checked UI click was refused: {exc}") from exc
    except Stop as exc:
        print("STOPPED:", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
