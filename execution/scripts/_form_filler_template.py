"""Starter for a form-specific FACT filler; copy and customize before use.

This file is deliberately hidden from the automation wizard while its name
starts with ``_``. Copy it to a new name such as ``form_filler_invoice.py``
after configuring the form title, required controls, and field mappings.

It fills ordinary header Edit controls only. Grid navigation is intentionally
left form-specific: implement ``fill_line_items`` after inspecting that form's
grid rather than assuming another form uses the Sale Order tab sequence.

Run ``python execution/scripts/_form_filler_template.py <values.json> --check``
to validate JSON paths and configuration without touching FACT.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class Stop(Exception):
    """The form or input did not meet a safe precondition."""


@dataclass(frozen=True)
class HeaderField:
    """One source JSON path mapped to one FACT Edit control."""

    json_path: str
    automation_id: str
    index: int = 0
    required: bool = True
    description_id: str | None = None
    input_date_format: str | None = None
    fact_date_format: str | None = None


# ------------------------------------------------------------------ edit here --
# Use an identifying suffix from the actual FACT form title, not a generic title
# such as "FACT ERP.NG".
FORM_TITLE_SUFFIX = "REPLACE WITH FORM TITLE SUFFIX"

# Include controls that distinguish this form from sibling FACT windows.
REQUIRED_CONTROL_IDS = ("REPLACE_WITH_REQUIRED_AUTOMATION_ID",)

# JSON path -> FACT AutomationId. The index is zero-based among matching Edit
# controls, sorted top-to-bottom then left-to-right.
#
# Examples based on execution/scripts/sample.json (confirm every business
# mapping and AutomationId before enabling it):
#   HeaderField("header.quotation_no", "FACT_PROFORMA_AUTOMATION_ID")
#   HeaderField(
#       "header.order_date",
#       "FACT_DATE_AUTOMATION_ID",
#       input_date_format="%Y-%m-%d",
#       fact_date_format="%d/%m/%Y",
#   )
#
# Date formatting is configured by setting both formats, for example
# input_date_format="%Y-%m-%d" and fact_date_format="%d/%m/%Y". Leave both unset
# only after confirming FACT accepts the incoming date text unchanged.
HEADER_FIELDS: tuple[HeaderField, ...] = (
    # Replace this example with fields observed on the target form.
    HeaderField(
        json_path="header.REPLACE_WITH_JSON_KEY",
        automation_id="REPLACE_WITH_FACT_AUTOMATION_ID",
    ),
)

# Path to the line list in the source JSON. Change if the payload uses another
# key. A non-empty list will be refused until fill_line_items is implemented.
LINE_ITEMS_PATH = "items"

# The script never saves. Review the completed form and save explicitly.
# -----------------------------------------------------------------------------


_MISSING = object()
_SAFETY_STATE = PROJECT_ROOT / "data" / "state" / "safety.json"


def _get_path(document: Any, path: str) -> Any:
    value = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _format_value(field: HeaderField, value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if field.input_date_format and field.fact_date_format:
        try:
            text = datetime.strptime(text, field.input_date_format).strftime(
                field.fact_date_format
            )
        except ValueError as exc:
            raise Stop(
                f"{field.json_path}: date {text!r} does not match "
                f"{field.input_date_format!r}"
            ) from exc
    return text


def _validate_configuration() -> None:
    if not FORM_TITLE_SUFFIX or FORM_TITLE_SUFFIX.startswith("REPLACE"):
        raise Stop("Set FORM_TITLE_SUFFIX to the target FACT form's title suffix")
    if not REQUIRED_CONTROL_IDS or any(
        not control_id or control_id.startswith("REPLACE")
        for control_id in REQUIRED_CONTROL_IDS
    ):
        raise Stop("Set REQUIRED_CONTROL_IDS to controls unique to the target form")
    if not HEADER_FIELDS or any(
        field.json_path.startswith("header.REPLACE")
        or not field.automation_id
        or field.automation_id.startswith("REPLACE")
        for field in HEADER_FIELDS
    ):
        raise Stop("Replace the example entries in HEADER_FIELDS before using the template")

    paths = [field.json_path for field in HEADER_FIELDS]
    if len(paths) != len(set(paths)):
        raise Stop("HEADER_FIELDS contains a duplicate JSON path")
    if any(field.index < 0 for field in HEADER_FIELDS):
        raise Stop("Edit-control indexes must be zero or greater")
    for field in HEADER_FIELDS:
        if bool(field.input_date_format) != bool(field.fact_date_format):
            raise Stop(
                f"{field.json_path}: configure both date formats or leave both unset"
            )


def _validate_payload(document: Any) -> tuple[dict[str, str], list[dict[str, Any]]]:
    if not isinstance(document, dict):
        raise Stop("The values file must contain a JSON object")

    values: dict[str, str] = {}
    errors: list[str] = []
    for field in HEADER_FIELDS:
        raw = _get_path(document, field.json_path)
        if raw is _MISSING or raw is None or str(raw).strip() == "":
            if field.required:
                errors.append(f"required value missing: {field.json_path}")
            continue
        try:
            values[field.json_path] = _format_value(field, raw)
        except Stop as exc:
            errors.append(str(exc))

    raw_rows = _get_path(document, LINE_ITEMS_PATH) if LINE_ITEMS_PATH else []
    if raw_rows is _MISSING or raw_rows is None:
        rows: list[dict[str, Any]] = []
    elif not isinstance(raw_rows, list):
        errors.append(f"{LINE_ITEMS_PATH} must be a JSON list")
        rows = []
    elif any(not isinstance(row, dict) for row in raw_rows):
        errors.append(f"every item in {LINE_ITEMS_PATH} must be a JSON object")
        rows = []
    else:
        rows = raw_rows

    if errors:
        raise Stop("Invalid input:\n - " + "\n - ".join(errors))
    return values, rows


def _control_ids(window: Any) -> set[str]:
    ids = {
        str(getattr(element.element_info, "automation_id", "") or "").strip()
        for element in window.descendants()
    }
    ids.discard("")
    return ids


def _foreground_handle() -> int:
    import win32gui

    return int(win32gui.GetForegroundWindow())


class Form:
    """Attach only to a uniquely identified, visible target form."""

    def __init__(self, expected_handle: int | None = None) -> None:
        from pywinauto import Desktop

        self.desktop = Desktop(backend="uia")
        candidates = []
        for window in self.desktop.windows():
            if not window.is_visible():
                continue
            title = (window.window_text() or "").strip()
            if not title.endswith(FORM_TITLE_SUFFIX):
                continue
            if not set(REQUIRED_CONTROL_IDS).issubset(_control_ids(window)):
                continue
            candidates.append(window)

        if expected_handle is not None:
            candidates = [
                window for window in candidates if int(window.handle) == expected_handle
            ]
        if not candidates:
            raise Stop(
                "The target form was not found with the configured title and "
                "required controls. Nothing was typed."
            )

        foreground = _foreground_handle()
        active = [window for window in candidates if int(window.handle) == foreground]
        if len(active) == 1:
            self.win = active[0]
        elif len(candidates) == 1:
            self.win = candidates[0]
        else:
            raise Stop(
                "More than one target form matches and none is uniquely in front. "
                "Nothing was typed."
            )

        self.handle = int(self.win.handle)
        self.win.set_focus()
        self.assert_active()

    def assert_active(self) -> None:
        if _foreground_handle() != self.handle:
            raise Stop(
                "The selected FACT form lost foreground focus. Nothing further was typed."
            )

    def edit(self, automation_id: str, index: int = 0) -> Any:
        elements = [
            element
            for element in self.win.descendants(control_type="Edit")
            if (element.element_info.automation_id or "") == automation_id
        ]
        elements.sort(
            key=lambda element: (
                round(element.rectangle().top / 12),
                element.rectangle().left,
            )
        )
        if index >= len(elements):
            raise Stop(f"field {automation_id}[{index}] was not found on the form")
        return elements[index]

    @staticmethod
    def read(control: Any) -> str:
        try:
            value = control.iface_value.CurrentValue
            if value is not None:
                return str(value)
        except Exception:
            pass
        return str(control.window_text() or "")


def _safety_state() -> str | None:
    try:
        state = json.loads(_SAFETY_STATE.read_text(encoding="utf-8"))
        return state.get("state")
    except (OSError, ValueError):
        return None


def _obey_safety(where: str) -> None:
    while _safety_state() == "paused":
        time.sleep(0.2)
    if _safety_state() == "stopped":
        raise Stop(f"stopped by the automation safety control during {where}")


def fill_header(form: Form, values: dict[str, str]) -> None:
    from desktop.input.input import (
        VK_A,
        VK_CONTROL,
        click_control,
        press_chord,
        press_keys,
        type_text,
    )

    for field in HEADER_FIELDS:
        text = values.get(field.json_path)
        if text is None:
            continue
        _obey_safety(field.json_path)
        form.assert_active()
        control = form.edit(field.automation_id, field.index)
        if not control.is_enabled():
            raise Stop(f"{field.json_path}: the FACT control is not editable")

        click_control(control, form.handle, require_foreground=True)
        form.assert_active()
        press_chord(VK_CONTROL, VK_A)
        type_text(text)
        press_keys("Tab")
        time.sleep(0.15)
        form.assert_active()

        actual = re.sub(r"\s+", " ", form.read(form.edit(field.automation_id, field.index))).strip()
        expected = re.sub(r"\s+", " ", text).strip()
        if actual.casefold() != expected.casefold():
            raise Stop(
                f"{field.json_path}: typed the configured value, but FACT read back "
                f"{actual!r}; nothing else was typed"
            )

        if field.description_id:
            description = form.read(form.edit(field.description_id)).strip()
            if not description:
                raise Stop(
                    f"{field.json_path}: FACT did not populate its description; "
                    "check the lookup value"
                )


def fill_line_items(_form: Form, rows: list[dict[str, Any]]) -> None:
    """Add the form's observed grid sequence here before enabling line entry."""
    if rows:
        raise Stop(
            f"This template received {len(rows)} line item(s), but no grid-entry "
            "sequence is configured. Nothing was typed. Inspect this form's grid "
            "and implement fill_line_items first."
        )


def run(values_path: Path, *, check_only: bool, window_handle: int | None) -> int:
    _validate_configuration()
    try:
        document = json.loads(values_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Stop(f"cannot read JSON values from {values_path}: {exc}") from exc

    values, rows = _validate_payload(document)
    if check_only:
        print(f"OK: {len(values)} configured header value(s), {len(rows)} line item(s)")
        if rows:
            print("NOTICE: implement fill_line_items before running this template")
        return 0

    # Refuse a payload with rows before touching the form unless its grid logic
    # has been customized.
    if rows:
        raise Stop(
            f"This template received {len(rows)} line item(s), but its grid "
            "filler is not configured. Implement fill_line_items before running."
        )
    form = Form(expected_handle=window_handle)
    fill_header(form, values)
    fill_line_items(form, rows)
    print("DONE: configured header fields were read back. Review and Save by hand.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("values_file", type=Path)
    parser.add_argument("--check", action="store_true", help="validate mappings only")
    parser.add_argument(
        "--window-handle",
        type=int,
        default=None,
        help="attach to this exact FACT window handle when supplied by a runner",
    )
    args = parser.parse_args(argv)
    if sys.platform != "win32" and not args.check:
        parser.error("live form filling is Windows-only")
    try:
        return run(
            args.values_file,
            check_only=args.check,
            window_handle=args.window_handle,
        )
    except Stop as exc:
        print(f"STOPPED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
