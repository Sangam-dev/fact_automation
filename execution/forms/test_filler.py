"""Sale Order filler: a generic runner over an editable plan.

This module is the *how*: attach, type, dismiss dialogs, walk grid cells.
The *what* (which fields, in which order) lives in ``sale_order_plan.py`` —
edit that file to add or remove a step; nothing here needs to change.

Derived values (order number, descriptions, exchange rate, amounts) are only
read back, never typed. UOM, pre-close quantity, difference, export
documentation, additional description and the row EDD are tabbed past.

    myvenv/Scripts/python.exe -m execution.forms.test_filler --dry-run
    myvenv/Scripts/python.exe -m execution.forms.test_filler
    myvenv/Scripts/python.exe -m execution.forms.test_filler --lines 1 --force

The Sale Order Add form must already be open; the filler restores it if
minimized. It never saves.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from pywinauto import Application, Desktop
from pywinauto.keyboard import send_keys

from execution.forms.form_map import FormMapError, load_form_map
from execution.forms.sale_order_plan import SALE_ORDER, Plan

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FORM_MAP = PROJECT_ROOT / "config" / "forms" / "sale_order.json"
DEFAULT_PAYLOAD = PROJECT_ROOT / "config" / "forms" / "test_payload.json"

WINDOW_TITLE = "Sale Order"
GRID_AUTO_ID = "gcProduct"
TAB = "{TAB}"
COMMIT_DELAY = 0.25
LOOKUP_TIMEOUT = 4.0
SW_RESTORE = 9

#: FACT draws message boxes inside the form tree, not as top-level windows.
DIALOG_ID = "frmMessageBox"
DIALOG_DETAIL_ID = "lblDetail"
#: never clicked: "does not exist / Add New?" is always declined
DIALOG_NO_ID = "btnNo2"
DIALOG_OK_ID = "btnOk"  # informational warnings, e.g. "No previous Selling Rate Found"

TAG_RE = re.compile(r"<[^>]+>")
USER32 = ctypes.windll.user32


@dataclass
class Target:
    spec: object
    wrapper: object
    hwnd: int
    pid: int


@dataclass
class Check:
    key: str
    expected: str
    actual: str
    ok: bool
    note: str = ""

    def line(self) -> str:
        mark = "OK  " if self.ok else "FAIL"
        extra = f"  ({self.note})" if self.note else ""
        return (
            f"[{mark}] {self.key:<28} typed={self.expected!r:<30} "
            f"read={self.actual!r}{extra}"
        )


# --------------------------------------------------------------------------- #
# payload
# --------------------------------------------------------------------------- #


def load_payload(path: Path) -> tuple[dict, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    header = data.get("header") or {}
    lines = [
        line
        for line in data.get("lines") or []
        if str(line.get("product_code") or "").strip()
    ]
    return header, lines


def fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Y" if value else "N"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


# --------------------------------------------------------------------------- #
# window
# --------------------------------------------------------------------------- #


def find_window() -> Target:
    """Locate the Sale Order form, restoring it if it is minimized.

    A minimized window exposes no UIA children, so every lookup would fail
    until it is restored.
    """
    matches = []
    for window in Desktop(backend="uia").windows():
        try:
            if WINDOW_TITLE in window.window_text():
                matches.append(window)
        except Exception:
            continue

    if not matches:
        raise SystemExit(
            "Could not find the Sale Order window. Open Accounts Receivable > Sales > "
            "Entry > Sale Order > Add and run again."
        )

    # Several Sale Order forms can be open; the active one is the target.
    foreground = USER32.GetForegroundWindow()
    title_match = next((w for w in matches if w.handle == foreground), None)
    if title_match is None:
        title_match = next(
            (w for w in matches if w.is_visible() and w.rectangle().width() > 0),
            matches[0],
        )

    hwnd = title_match.handle
    if not title_match.is_visible() or title_match.rectangle().width() <= 0:
        print("window is minimized/hidden; restoring")
        USER32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.8)

    USER32.SetForegroundWindow(hwnd)
    time.sleep(0.6)

    app = Application(backend="uia").connect(handle=hwnd, timeout=15)
    spec = app.window(handle=hwnd)
    wrapper = spec.wrapper_object()

    if not spec.child_window(auto_id=GRID_AUTO_ID).exists():
        raise SystemExit(
            "Found the Sale Order window but not the product grid. Make sure the "
            "form is on the Add/Edit screen and not still loading."
        )

    return Target(spec=spec, wrapper=wrapper, hwnd=hwnd,
                  pid=wrapper.element_info.process_id)


def control_for(target: Target, auto_id: str):
    return target.spec.child_window(auto_id=auto_id).wrapper_object()


def read_text(control) -> str:
    """Text of a control, including the Edit nested in a wrapper pane."""
    candidates = [control]
    try:
        candidates.extend(control.children(control_type="Edit"))
    except Exception:
        pass

    for item in candidates:
        try:
            value = item.get_value()
            if value and str(value).strip():
                return str(value).strip()
        except Exception:
            pass
        try:
            legacy = item.legacy_properties()
            value = str(legacy.get("Value") or "").strip()
            if value:
                return value
        except Exception:
            pass
        try:
            value = item.window_text().strip()
            if value:
                return value
        except Exception:
            pass
    return ""


def type_into(control, value: str) -> None:
    try:
        control.set_focus()
    except Exception:
        control.click_input()
    send_keys("^a", pause=0.02)
    send_keys(value, with_spaces=True, pause=0.02)
    send_keys(TAB)
    time.sleep(COMMIT_DELAY)


# --------------------------------------------------------------------------- #
# popups
# --------------------------------------------------------------------------- #


def find_dialog(target: Target, timeout: float = 0.0):
    """The in-form FACT message box, if one is open."""
    deadline = time.time() + timeout
    while True:
        try:
            if target.spec.child_window(auto_id=DIALOG_ID).exists():
                return target.spec.child_window(auto_id=DIALOG_ID).wrapper_object()
        except Exception:
            pass
        if time.time() >= deadline:
            return None
        time.sleep(0.2)


def dialog_message(target: Target) -> str:
    try:
        raw = target.spec.child_window(auto_id=DIALOG_DETAIL_ID).wrapper_object()
        text = raw.window_text()
    except Exception:
        return ""
    text = TAG_RE.sub("", text)
    return " / ".join(part.strip() for part in text.splitlines() if part.strip())


def click_dialog_button(target: Target, auto_id: str) -> bool:
    # Some dialog buttons match twice: an unnamed wrapper Pane and the labelled
    # one share the same automation id (btnOk), so pin the first match.
    try:
        target.spec.child_window(
            auto_id=auto_id, found_index=0
        ).wrapper_object().click_input()
        return True
    except Exception:
        return False


def dialog_button_exists(target: Target, auto_id: str) -> bool:
    try:
        return target.spec.child_window(auto_id=auto_id, found_index=0).exists()
    except Exception:
        return False


def handle_dialog(target: Target, where: str) -> str:
    """Dismiss any FACT message box raised while filling ``where``.

    The dialog is a Window inside the form tree (``frmMessageBox``) with the
    message in ``lblDetail`` and Pane buttons ``btnYes2`` / ``btnNo2``.

    Policy: never answer Yes. A "does not exist / Add New?" prompt is declined
    (``btnNo2``) and the caller skips that field or line. Anything else is
    unrecognised and stops the run.
    """
    box = find_dialog(target, timeout=1.2)
    for _ in range(3):
        if box is None:
            return "none"

        message = dialog_message(target)
        print(f"  [popup] {where}: {message[:200]}")
        low = message.lower()

        if "does not exist" in low or "add new" in low:
            click_dialog_button(target, DIALOG_NO_ID)
            time.sleep(0.9)
            print("  [popup] declined; field skipped")
            return "skip"

        if dialog_button_exists(target, DIALOG_OK_ID):
            click_dialog_button(target, DIALOG_OK_ID)
            time.sleep(0.9)
            print("  [popup] acknowledged (OK)")
            return "ok"

        click_dialog_button(target, DIALOG_NO_ID)
        time.sleep(0.9)
        raise RuntimeError(
            f"Stopped on an unrecognised dialog while filling {where}: "
            f"{message[:200]}"
        )
    return "none"


# --------------------------------------------------------------------------- #
# header
# --------------------------------------------------------------------------- #


def fill_header(target: Target, form, plan: Plan, header: dict) -> list[Check]:
    checks: list[Check] = []

    for step in plan.header:
        key = step.key
        value = fmt(header.get(key))
        if not value:
            print(f"[skip] {key:<28} no value in payload")
            continue

        try:
            spec = form.field(key)
        except FormMapError:
            print(f"[skip] {key:<28} not in the form map")
            continue

        control = control_for(target, spec.automation_id)
        if not control.is_enabled():
            checks.append(Check(key, value, "", False, "control disabled"))
            print(f"[FAIL] {key:<28} disabled in the form")
            continue

        control.click_input()
        type_into(control, value)
        if handle_dialog(target, key) == "skip":
            checks.append(
                Check(key, value, read_text(control), True,
                      "skipped: code not in the database")
            )
            continue

        actual = read_text(control)
        # peDocScheme appends "[Date Range = ...]" to the scheme name.
        decorated = bool(actual) and actual != value and actual.startswith(value)
        checks.append(
            Check(
                key,
                value,
                actual,
                actual.strip() == value.strip() or decorated,
                "control appends decoration" if decorated else "",
            )
        )

        proof_key = step.proof
        if proof_key:
            proof = read_text(control_for(target, form.field(proof_key).automation_id))
            deadline = time.time() + LOOKUP_TIMEOUT
            while not proof and time.time() < deadline:
                time.sleep(0.25)
                proof = read_text(
                    control_for(target, form.field(proof_key).automation_id)
                )
            checks.append(
                Check(
                    f"{key} -> {proof_key}",
                    "(auto)",
                    proof,
                    bool(proof.strip()),
                    "lookup did not resolve",
                )
            )

    return checks


# --------------------------------------------------------------------------- #
# grid
# --------------------------------------------------------------------------- #


def cell_name(column: str, row: int) -> str:
    return f"{column} row {row}"


def clear_cell(target: Target, column: str, row: int) -> bool:
    """Empty one cell: F2 opens the in-place editor, Ctrl+A + Delete clears it.

    Escape must not be used here: on this form it raises the "Are you sure you
    wish to Exit?" prompt.
    """
    cell = find_cell(target, cell_name(column, row), timeout=0.8)
    if cell is None:
        return False
    cell.click_input()
    time.sleep(0.2)
    send_keys("{F2}")
    time.sleep(0.1)
    send_keys("^a")
    send_keys("{DELETE}")
    send_keys(TAB)
    time.sleep(COMMIT_DELAY)
    handle_dialog(target, f"clear {column} row {row + 1}")
    return not cell_value(find_cell(target, cell_name(column, row), 0.5))


def scroll_grid_left(target: Target, times: int = 4) -> None:
    """The grid auto-scrolls while tabbing; bring the left columns back to read them."""
    for _ in range(times):
        try:
            target.spec.child_window(
                title="Column Left", control_type="Button"
            ).wrapper_object().click_input()
            time.sleep(0.12)
        except Exception:
            return


def find_cell(target: Target, name: str, timeout: float = 1.5):
    deadline = time.time() + timeout
    wanted = " ".join(name.split())

    while True:
        try:
            spec = target.spec.child_window(title=name, control_type="DataItem")
            if spec.exists():
                return spec.wrapper_object()
        except Exception:
            pass

        try:
            for item in target.wrapper.descendants(control_type="DataItem"):
                if " ".join(item.window_text().split()) == wanted:
                    return item
        except Exception:
            pass

        if time.time() >= deadline:
            return None
        time.sleep(0.2)


def cell_value(cell) -> str:
    """Grid cell text via MSAA legacy properties; empty cells return "".

    UIA exposes only the cell *name* (``"Rate  row 0"``), and MSAA falls back to
    that same name for an empty cell, so a value equal to the name means blank.
    """
    if cell is None:
        return ""
    try:
        name = " ".join(cell.window_text().split())
    except Exception:
        name = ""
    try:
        legacy = cell.legacy_properties()
    except Exception:
        return ""

    value = legacy.get("Value")
    if value is None:
        return ""
    text = str(value).strip()
    if not text or " ".join(text.split()) == name:
        return ""
    return text


def focused_cell(target: Target):
    """The DataItem that currently has MSAA keyboard focus."""
    try:
        items = target.spec.descendants(control_type="DataItem")
    except Exception:
        return None
    for item in items:
        try:
            state = (item.legacy_properties() or {}).get("State", 0)
        except Exception:
            continue
        if state & 0x4:  # STATE_SYSTEM_FOCUSED
            return item
    return None


def focused_name(target: Target) -> str:
    cell = focused_cell(target)
    if cell is None:
        return ""
    try:
        return " ".join(cell.window_text().split())
    except Exception:
        return ""


def move_to_column(target: Target, column: str, row: int, limit: int = 15) -> bool:
    """Tab forward until the focused cell is ``<column> row <row>``.

    The grid scrolls itself horizontally while the focus moves, so the cells we
    type into are not always the ones that were on screen when the row started.
    """
    wanted = " ".join(cell_name(column, row).split())
    for _ in range(limit):
        name = focused_name(target)
        if name == wanted:
            return True
        if name and not name.endswith(f"row {row}"):
            return False
        send_keys(TAB)
        time.sleep(COMMIT_DELAY)
    return False


def focus_row_start(target: Target, row: int) -> bool:
    """Put the cursor on ``Product Code row N``."""
    wanted = " ".join(cell_name("Product Code", row).split())
    cell = find_cell(target, cell_name("Product Code", row), timeout=0.6)
    if cell is not None:
        cell.click_input()
        for _ in range(5):
            time.sleep(0.15)
            if focused_name(target) == wanted:
                return True

    for _ in range(15):
        name = focused_name(target)
        if name == " ".join(cell_name("Product Code", row).split()):
            return True
        if not name.endswith(f"row {row}"):
            return False
        send_keys("+{TAB}")  # shift+tab, pywinauto syntax
        time.sleep(COMMIT_DELAY)
    return False


def advance_row(target: Target, row: int, limit: int = 8) -> bool:
    """Tab past the end of ``row`` until the next row's Product Code has focus.

    On this grid the remaining columns after Document Class are Export
    Documentation, Additional Description and Expected Delivery Date; tabbing
    through them opens the next row.
    """
    wanted = " ".join(cell_name("Product Code", row + 1).split())
    for _ in range(limit):
        if focused_name(target) == wanted:
            return True
        send_keys(TAB)
        time.sleep(COMMIT_DELAY)
    return False


def fill_rows(target: Target, form, plan: Plan, lines: list[dict]) -> list[Check]:
    checks: list[Check] = []

    for row, line in enumerate(lines):
        values = {}
        for step in plan.columns:
            text = fmt(line.get(step.key))
            if text:
                values[step.column] = text

        if not values:
            print(f"[skip] row {row + 1:<2} no values")
            continue

        print(f"\nrow {row + 1}: {values}")
        if not focus_row_start(target, row):
            checks.append(
                Check(
                    f"row {row + 1} start",
                    "Product Code cell",
                    focused_name(target),
                    False,
                    "could not focus the row's first cell",
                )
            )
            continue

        for column, value in values.items():
            if not move_to_column(target, column, row):
                checks.append(
                    Check(
                        f"row {row + 1} {column}",
                        value,
                        focused_name(target),
                        False,
                        "focus never reached the cell; nothing typed",
                    )
                )
                break

            # Typing alone APPENDS to an existing cell value (observed:
            # "BRM-003" became "BRM-003BRM-003" on a rerun), so open the editor
            # and replace the text explicitly.
            send_keys("{F2}")
            time.sleep(0.1)
            send_keys("^a")
            send_keys(value, with_spaces=True, pause=0.02)
            send_keys(TAB)
            time.sleep(COMMIT_DELAY)
            if handle_dialog(target, f"row {row + 1} {column}") == "skip":
                checks.append(
                    Check(f"row {row + 1} {column}", value, "", True,
                          "skipped: code not in the database")
                )
                clear_cell(target, column, row)
                print(f"[skip] line {row + 2}+ not attempted; the row was left blank")
                return checks

        # Tabs, not Enter: the grid creates the next row at the end of the tab
        # chain, and only product code + description unlock forward movement.
        if row < len(lines) - 1 and not advance_row(target, row):
            checks.append(
                Check(
                    f"row {row + 1} -> row {row + 2}",
                    "Product Code cell",
                    focused_name(target),
                    False,
                    "tabbing did not open the next row",
                )
            )
            break

        scroll_grid_left(target)
        for column in plan.readback:
            actual = " ".join(
                cell_value(find_cell(target, cell_name(column, row))).split()
            )
            expected = values.get(column, "")
            checks.append(
                Check(
                    f"row {row + 1} {column}",
                    expected or "(derived)",
                    actual,
                    bool(actual),
                )
            )

    return checks


def refresh_derived(target: Target, form, checks: list[Check]) -> None:
    """Lookups can resolve after the field lost focus; re-read them once."""
    for check in checks:
        if check.ok or " -> " not in check.key:
            continue
        key = check.key.split(" -> ")[1]
        deadline = time.time() + LOOKUP_TIMEOUT
        proof = ""
        while not proof and time.time() < deadline:
            proof = read_text(control_for(target, form.field(key).automation_id))
            if not proof:
                time.sleep(0.25)
        if proof:
            check.actual = proof
            check.ok = True
            check.note = "resolved after the form settled"


# --------------------------------------------------------------------------- #
# plan / guards
# --------------------------------------------------------------------------- #


def resolve_plan(target: Target, form, plan: Plan, header: dict,
                 lines: list[dict]) -> None:
    """Dry run: prove every target resolves before touching the keyboard."""
    print("\n--- header targets ---")
    for step in plan.header:
        key = step.key
        value = fmt(header.get(key))
        try:
            spec = form.field(key)
            control = control_for(target, spec.automation_id)
            state = "enabled" if control.is_enabled() else "DISABLED"
            current = read_text(control)
            print(
                f"  {key:<24} {spec.automation_id:<16} {state:<9} "
                f"payload={value!r:<28} current={current!r}"
            )
        except Exception as error:
            print(f"  {key:<24} UNRESOLVED: {error}")

    print("\n--- grid row 0 ---")
    for column in form.grid.columns:
        cell = find_cell(target, cell_name(column.name, 0), timeout=0.3)
        value = cell_value(cell)
        print(
            f"  {column.name:<28} "
            f"{'cell found' if cell else 'not in view':<12} value={value!r}"
        )

    print(f"\n--- lines ({len(lines)}) ---")
    for index, line in enumerate(lines, start=1):
        print(f"  {index}: {line}")


def existing_data(target: Target, form, plan: Plan, header: dict,
                  lines: list[dict]) -> list[str]:
    """Values already on the form that differ from this payload."""
    conflicts = []
    for step in plan.header:
        key = step.key
        value = fmt(header.get(key))
        if not value:
            continue
        current = read_text(control_for(target, form.field(key).automation_id))
        if current and current != value:
            conflicts.append(f"{key}: form={current!r} payload={value!r}")

    first_product = cell_value(find_cell(target, cell_name("Product Code", 0), 0.3))
    expected = fmt(lines[0].get("product_code")) if lines else ""
    if first_product and first_product != expected:
        conflicts.append(f"row 1 already has product {first_product!r}")
    return conflicts


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, default=DEFAULT_PAYLOAD)
    parser.add_argument("--lines", type=int, default=0, help="fill only first N lines")
    parser.add_argument("--dry-run", action="store_true", help="resolve, type nothing")
    parser.add_argument("--force", action="store_true",
                        help="fill even when the form already holds other data")
    args = parser.parse_args(argv)

    form = load_form_map(FORM_MAP)
    plan = SALE_ORDER
    header, lines = load_payload(args.payload)
    if args.lines:
        lines = lines[: args.lines]

    print("=" * 68)
    print("SALE ORDER - TEST FILLER")
    print("=" * 68)
    print(f"payload: {args.payload}")
    print(f"plan   : {plan.name} - {len(plan.header)} header steps, "
          f"{len(plan.columns)} row steps, {len(lines)} lines")

    target = find_window()
    print(f"window : {target.wrapper.window_text().strip()!r} hwnd={target.hwnd}")

    if args.dry_run:
        resolve_plan(target, form, plan, header, lines)
        print("\ndry run: nothing typed.")
        return 0

    conflicts = existing_data(target, form, plan, header, lines)
    if conflicts and not args.force:
        print("\nThe open form already contains different data:")
        for conflict in conflicts:
            print(f"  - {conflict}")
        print(
            "\nOpen a fresh Sale Order (Save & New / Cancel) and run again, "
            "or pass --force to type over it."
        )
        return 2

    print("\nFilling header in 3 seconds (Ctrl+C to abort)...")
    time.sleep(3)

    checks = fill_header(target, form, plan, header)
    checks.extend(fill_rows(target, form, plan, lines))
    refresh_derived(target, form, checks)

    failed = [check for check in checks if not check.ok]
    print("\n" + "=" * 68)
    print(f"RESULT: {len(checks) - len(failed)}/{len(checks)} checks passed")
    for check in checks:
        print(check.line())
    print("=" * 68)
    print("Document NOT saved. Review the form, then save manually.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
