"""Open Global UDF and fill its first page, leaving the dialog open."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from decimal import Decimal, InvalidOperation
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
SMS_ITEM_DESCRIPTIONS = {
    "FG-0001": "Jagdamba Ultra Premium OPC 43 Grade(T)",
    "FG-0002": "Jagdamba Ultra Premium OPC 53 Grade(T)",
    "FG-0003": "Jagdamba PPC Cement(T)",
    "FG-0007": "Jagdamba PPC Cement(M)",
    "FG-0009": "Jagdamba Ultra Premium OPC 43 Grade(M)",
}
SMS_UOM_OPTIONS = {"BAG": "Bags", "MT": "MT"}
SMS_FIELDS = {"F10": "Edit", "F11": "Edit", "F12": "Edit"}
PICKER_TIMEOUT_SECONDS = 15.0


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


def _sms_entry(document: dict[str, Any]) -> dict[str, str]:
    if "lines" in document and "items" in document:
        raise Stop("provide either 'lines' or 'items', not both")
    lines = document.get("lines", document.get("items"))
    if not isinstance(lines, list) or not lines:
        raise Stop("'lines' or 'items' must be a non-empty list for Entry For SMS")

    ranked: list[tuple[Decimal, int, dict[str, Any]]] = []
    for index, line in enumerate(lines):
        if not isinstance(line, dict):
            raise Stop(f"line {index + 1}: expected an object")
        try:
            quantity = Decimal(str(line.get("quantity", "")).replace(",", "").strip())
        except (InvalidOperation, ValueError):
            quantity = Decimal("NaN")
        if not quantity.is_finite() or quantity <= 0:
            raise Stop(f"line {index + 1}: quantity must be a number > 0")
        ranked.append((quantity, index, line))

    # max() is stable for equal quantities because the original index is the tie-break.
    quantity, _, line = max(ranked, key=lambda item: (item[0], -item[1]))
    product_code = str(line.get("product_code", "")).strip().upper()
    if product_code not in SMS_ITEM_DESCRIPTIONS:
        raise Stop(
            f"unsupported SMS product_code {product_code!r}; expected one of "
            f"{sorted(SMS_ITEM_DESCRIPTIONS)}"
        )
    unit_code = str(line.get("unitcode", "")).strip().upper()
    if unit_code not in SMS_UOM_OPTIONS:
        raise Stop(f"unsupported SMS unitcode {unit_code!r}; expected one of {sorted(SMS_UOM_OPTIONS)}")
    return {
        "product_code": product_code,
        "description": SMS_ITEM_DESCRIPTIONS[product_code],
        "quantity": format(quantity.normalize(), "f"),
        "uom": SMS_UOM_OPTIONS[unit_code],
    }


def read_udf_values(
    document: Any,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Map JSON values to the first-page UDFs and the single SMS entry."""
    if not isinstance(document, dict):
        raise Stop("the JSON root must be an object")
    text_values = {field_id: _text_value(document, key) for field_id, key in TEXT_FIELD_IDS.items()}
    dropdown_values = {
        "F19": _dropdown_value(document, "paymentMethod", SALES_TYPE_OPTIONS),
        "F18": _dropdown_value(document, "plant", LOADING_POINT_OPTIONS),
    }
    return text_values, dropdown_values, _sms_entry(document)


def load_udf_values(
    path: str | Path,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
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


def _visible_controls(window, automation_id: str, control_type: str | None = None):
    matches = []
    for control in window.descendants():
        try:
            if (control.element_info.automation_id or "") != automation_id:
                continue
            if control_type and control.element_info.control_type != control_type:
                continue
            if control.is_visible() and control.is_enabled():
                matches.append(control)
        except Exception:
            continue
    if len(matches) != 1:
        raise Stop(
            f"visible enabled control {automation_id!r}"
            f"{f' ({control_type})' if control_type else ''} "
            f"matched {len(matches)} controls; expected exactly one"
        )
    return matches[0]


def _select_sms_tab(dialog) -> None:
    candidates = []
    for control in dialog.descendants():
        try:
            if control.element_info.control_type != "TabItem":
                continue
            if _normalise(control.element_info.name) != "ENTRY FOR SMS":
                continue
            if not control.is_visible() or not control.is_enabled():
                continue
            rect = control.rectangle()
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if 0 < width <= 260 and 0 < height <= 60:
                candidates.append((width * height, control))
        except Exception:
            continue
    if not candidates:
        raise Stop("could not find the small visible Entry For SMS tab header")
    candidates.sort(key=lambda candidate: candidate[0])
    smallest_area = candidates[0][0]
    smallest = [control for area, control in candidates if area == smallest_area]
    if len(smallest) != 1:
        raise Stop("Entry For SMS tab header is ambiguous in the Global UDF dialog")
    try:
        smallest[0].iface_selection_item.Select()
    except Exception as exc:
        raise Stop(f"could not select the Entry For SMS tab: {exc}") from exc

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        obey_hotkeys("opening Entry For SMS")
        try:
            for field_id in SMS_FIELDS:
                _visible_controls(dialog, field_id)
            return
        except Stop:
            time.sleep(0.1)
    raise Stop("Entry For SMS fields did not appear after selecting the tab")


def _find_picker(desktop, title: str, process_id: int):
    deadline = time.monotonic() + PICKER_TIMEOUT_SECONDS
    last_candidates = []
    while time.monotonic() < deadline:
        obey_hotkeys(f"waiting for {title}")
        matches = []
        last_candidates = []
        for hwnd, window_title, class_name, _, _ in _top_windows():
            if window_title.strip() != title:
                continue
            try:
                window_pid = _window_process_id(hwnd)
                details = (
                    f"title={window_title!r}, class={class_name!r}, "
                    f"pid={window_pid}, hwnd={int(hwnd)}"
                )
                if window_pid != process_id:
                    last_candidates.append(f"{details}; rejected: PID mismatch")
                    continue
                window = desktop.window(handle=int(hwnd)).wrapper_object()
                automation_id = window.element_info.automation_id or ""
                if automation_id != "frmHelp":
                    last_candidates.append(
                        f"{details}, automation_id={automation_id!r}; "
                        "rejected: Automation ID is not 'frmHelp'"
                    )
                    continue
                if not window.is_visible():
                    last_candidates.append(f"{details}; rejected: UIA reports not visible")
                    continue
                matches.append(window)
                last_candidates.append(f"{details}, automation_id='frmHelp'; accepted")
            except Exception as exc:
                last_candidates.append(
                    f"title={window_title!r}, class={class_name!r}, "
                    f"hwnd={int(hwnd)}; UIA attach failed "
                    f"({type(exc).__name__}: {exc})"
                )
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise Stop(f"more than one {title!r} picker is open in the Sale Order process")
        time.sleep(0.1)
    diagnostic = "; ".join(last_candidates) or "no matching native top-level window found"
    raise Stop(
        f"the {title!r} picker did not appear in the Sale Order process; "
        f"last candidate(s): {diagnostic}"
    )


def _click_picker_button(dialog, field_id: str) -> None:
    field = _visible_controls(dialog, field_id)
    buttons = []
    for button in field.descendants(control_type="Button"):
        try:
            if button.is_visible() and button.is_enabled():
                buttons.append(button)
        except Exception:
            continue
    if len(buttons) != 1:
        raise Stop(
            f"SMS field {field_id} has {len(buttons)} visible enabled picker buttons; expected one"
        )
    agent_click(buttons[0], dialog, f"opening the picker for SMS field {field_id}")


def _cell_value(cell) -> str:
    try:
        value = cell.iface_value.CurrentValue
        if value is not None and str(value).strip():
            return str(value).strip()
    except Exception:
        pass
    return str(cell.window_text() or "").strip()


def _grid_filter_row(grid, column_name: str):
    expected = _normalise(f"{column_name} row -999997")
    matches = []
    for cell in grid.descendants(control_type="DataItem"):
        try:
            if _normalise(cell.window_text()) == expected:
                matches.append(cell)
        except Exception:
            continue
    if len(matches) != 1:
        raise Stop(
            f"picker filter row {column_name!r} matched {len(matches)} cells; expected one"
        )
    return matches[0]


def _filter_picker(grid, picker, column_name: str, text: str) -> None:
    filter_cell = _grid_filter_row(grid, column_name)
    agent_click(filter_cell, picker, f"focusing the {column_name} picker filter")
    agent_replace_text(text, f"filtering picker by {column_name}")


def _find_grid_match(grid, expected: str, *, first: bool = False):
    matches = []
    for cell in grid.descendants(control_type="DataItem"):
        try:
            # FACT exposes the editable filter as a DataItem too. Its value
            # matches the search text, but row -999997 is not a result row.
            cell_name = _normalise(cell.window_text())
            if re.search(r"\bROW\s+-999997$", cell_name):
                continue
            if _normalise(_cell_value(cell)) == _normalise(expected):
                matches.append(cell)
        except Exception:
            continue
    if not matches:
        return None
    if first:
        return matches[0]
    if len(matches) != 1:
        raise Stop(f"picker result {expected!r} matched {len(matches)} cells; expected one")
    return matches[0]


def _select_picker_result(grid, picker, expected: str, *, first: bool = False):
    deadline = time.monotonic() + PICKER_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        obey_hotkeys(f"finding picker result {expected}")
        match = _find_grid_match(grid, expected, first=first)
        if match is not None:
            agent_click(match, picker, f"selecting picker result {expected}")
            return
        time.sleep(0.1)
    raise Stop(f"picker result {expected!r} was not found")


def _confirm_picker(picker) -> None:
    # FACT's OK control can be exposed under a non-Button UIA control type;
    # its picker-local Automation ID is the stable selector.
    button = _visible_controls(picker, "btnMok")
    agent_click(button, picker, "confirming picker selection")
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        obey_hotkeys("closing picker")
        try:
            if not picker.is_visible():
                return
        except Exception:
            return
        time.sleep(0.1)
    raise Stop("picker remained open after clicking its OK button")


def _verify_numeric_value(control, expected: str, field_name: str) -> None:
    def decimal_value(value: Any) -> Decimal | None:
        try:
            return Decimal(str(value).replace(",", "").strip())
        except (InvalidOperation, ValueError):
            return None

    expected_number = decimal_value(expected)
    deadline = time.monotonic() + 3.0
    actual = control_value(control)
    while decimal_value(actual) != expected_number and time.monotonic() < deadline:
        obey_hotkeys(f"verifying {field_name}")
        time.sleep(0.1)
        actual = control_value(control)
    if expected_number is None or decimal_value(actual) != expected_number:
        raise Stop(f"{field_name} read back as {actual!r}; expected {expected!r}")


def _fill_sms_entry(desktop, dialog, sms_entry: dict[str, str]) -> None:
    _select_sms_tab(dialog)
    process_id = int(dialog.element_info.process_id)

    _click_picker_button(dialog, "F10")
    item_picker = _find_picker(desktop, "Item Ordered(SMS)", process_id)
    item_grid = _visible_controls(item_picker, "grdSearchFound")
    _filter_picker(item_grid, item_picker, "Product Description", sms_entry["description"])
    _select_picker_result(item_grid, item_picker, sms_entry["description"])
    _confirm_picker(item_picker)

    quantity = _visible_controls(dialog, "F11", "Edit")
    agent_click(quantity, dialog, "focusing SMS quantity")
    agent_replace_text(sms_entry["quantity"], "entering SMS quantity")
    _verify_numeric_value(quantity, sms_entry["quantity"], "SMS quantity")

    _click_picker_button(dialog, "F12")
    uom_picker = _find_picker(desktop, "UOM (SMS)", process_id)
    uom_grid = _visible_controls(uom_picker, "grdSearchFound")
    _filter_picker(uom_grid, uom_picker, "Product Master Sale UOM", sms_entry["uom"])
    _select_picker_result(uom_grid, uom_picker, sms_entry["uom"], first=True)
    _confirm_picker(uom_picker)
    _verify_value(_visible_controls(dialog, "F12"), sms_entry["uom"], "SMS UOM")


def _click_udf_ok(dialog) -> None:
    # As with the picker OK control, FACT may expose this ID under a non-Button
    # UIA type. The search remains scoped to the Global UDF dialog.
    button = _visible_controls(dialog, "btnSave")
    try:
        dialog_handle = int(dialog.handle)
    except Exception as exc:
        raise Stop(f"could not read the Global UDF dialog handle: {exc}") from exc

    agent_click(button, dialog, "confirming Global UDF details")
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        obey_hotkeys("waiting for Global UDF confirmation")
        if all(int(hwnd) != dialog_handle for hwnd, *_ in _top_windows()):
            return
        time.sleep(0.1)
    raise Stop("Global UDF dialog remained open after clicking its OK button")


def fill_udf_page(
    text_values: dict[str, str],
    dropdown_values: dict[str, str],
    sms_entry: dict[str, str],
) -> None:
    from pywinauto import Desktop

    desktop = Desktop(backend="uia")
    form = _find_sale_order(desktop)
    if not form.is_enabled():
        raise Stop("the Sale Order Add form is disabled; another dialog may be open")
    form = _focus_sale_order(desktop, form)
    process_id = int(form.element_info.process_id)

    _click_udf_opener(form)
    dialog = _find_udf_dialog(desktop, process_id)

    for field_id, value in text_values.items():
        _fill_text_field(dialog, field_id, value)
    for field_id, value in dropdown_values.items():
        _select_dropdown(dialog, field_id, value)

    _fill_sms_entry(desktop, dialog, sms_entry)
    _click_udf_ok(dialog)
    print("  global udf: first page and SMS entry entered; UDF dialog confirmed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("values_file", help="JSON file containing Global UDF values")
    parser.add_argument("--check", action="store_true", help="validate JSON only")
    args = parser.parse_args()
    try:
        text_values, dropdown_values, sms_entry = load_udf_values(args.values_file)
        if args.check:
            print(
                "OK: Global UDF values present "
                f"({len(text_values)} text fields, {len(dropdown_values)} dropdowns, "
                f"SMS item {sms_entry['product_code']} x {sms_entry['quantity']})"
            )
            return 0
        if sys.platform != "win32":
            print("Windows only.", file=sys.stderr)
            return 1
        from desktop.input.input import InputError

        try:
            fill_udf_page(text_values, dropdown_values, sms_entry)
        except InputError as exc:
            raise Stop(f"a checked UI click was refused: {exc}") from exc
    except Stop as exc:
        print("STOPPED:", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
