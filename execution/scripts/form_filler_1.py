"""
fill_form.py - fills the OPEN "Sale Order - Add" form in FACT from a JSON file. Any number of lines.
Nothing else: no login, no navigation, no Save. You open the Add form, run this, then check and save by hand.

    pip install pywinauto
    python fill_form.py sales_order.json            # fill
    python fill_form.py sales_order.json --check    # only validate the file

Ctrl+Alt+X aborts at any time.  Do not touch mouse/keyboard while it runs.
It stops at the first problem (unknown code, Message Box, value that did not stick) and tells you which one.
"""
import ctypes
import ctypes.wintypes as wt
import json
import re
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# json key -> (AutomationId, index among same-id fields top-to-bottom/left-to-right, description field or None)
HEADER = [
    ("order_date",             "NepDateEdit",  0, None),
    ("proforma_no",            "pePro_no",     0, None),
    ("proforma_date",          "NepDateEdit",  1, None),
    ("customer_order_no",      "peCustOrd_no", 0, None),
    ("customer_order_date",    "NepDateEdit",  2, None),
    ("currency",               "peCurr_desc",  0, "TxtCurrdescript"),
    ("customer_code",          "peCustcode",   0, "txtCustname"),
    ("salesman_code",          "peAgentcode",  0, "txtAgentname"),
    ("document_class",         "peClass",      0, "txtClassDesc"),
    ("expected_delivery_date", "NepDateEdit",  3, None),
]

# Website/sample payload keys -> the stable keys used by this form filler.
# Confirm these meanings against the source system before changing the mapping.
HEADER_ALIASES = {
    "quotation_no": "proforma_no",
    "order_no": "customer_order_no",
    "due_date": "expected_delivery_date",
    "customer_id": "customer_code",
    "salesman_id": "salesman_code",
}

# These are the UOM codes present in the sample and the exact labels in FACT.
UOM_OPTIONS = {"BAG": "Bags", "MT": "MT"}
SETTLE = 0.6          # seconds without any pop-up before a lookup field counts as done
MIN_WAIT = 0.8        # always give a wrong-code pop-up this long to appear after Tab
LOOKUP_TIMEOUT = 60   # product lookups can take 13 s on a good day
QUANTITY_DIALOG_TIMEOUT = 20


class Stop(Exception):
    pass


abort = threading.Event()
SAFETY_STATE = PROJECT_ROOT / "data" / "state" / "safety.json"


def safety_state():
    """Read the runner's pause/stop state when this filler is a child process."""
    try:
        import json
        return json.loads(SAFETY_STATE.read_text(encoding="utf-8")).get("state")
    except (OSError, ValueError):
        return None


def obey_hotkeys(where=""):
    """Honor both the runner's pause/stop hotkeys and local abort shortcut."""
    while safety_state() == "paused":
        if abort.is_set():
            raise Stop("aborted (Ctrl+Alt+X)")
        time.sleep(0.2)
    if abort.is_set():
        raise Stop("aborted (Ctrl+Alt+X)")
    if safety_state() == "stopped":
        suffix = f" during {where}" if where else ""
        raise Stop("stopped from the automation hotkey" + suffix)


def agent_press(keys, where=""):
    from desktop.input.input import press_keys
    obey_hotkeys(where)
    press_keys(keys)


def agent_replace_text(text, where=""):
    from desktop.input.input import VK_A, VK_CONTROL, press_chord, type_text
    obey_hotkeys(where)
    press_chord(VK_CONTROL, VK_A)
    obey_hotkeys(where)
    type_text(str(text))


def agent_click(control, owner, where=""):
    from desktop.input.input import click_control
    obey_hotkeys(where)
    click_control(control, owner)


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip().upper()


def num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return None


def normalize(order):
    """Convert supported payload shapes into the filler's header/lines shape."""
    if not isinstance(order, dict):
        raise Stop("the JSON root must be an object")

    source_header = order.get("header")
    if source_header is None:
        source_header = {
            key: value
            for key, value in order.items()
            if key not in {"lines", "items"}
        }
    if not isinstance(source_header, dict):
        raise Stop("'header' must be a JSON object")

    header = {}
    for source_key, value in source_header.items():
        key = HEADER_ALIASES.get(source_key, source_key)
        if key in header:
            raise Stop(
                f"multiple input fields map to header field {key!r}; "
                "resolve the duplicate before filling"
            )
        header[key] = value

    if "lines" in order and "items" in order:
        raise Stop("provide either 'lines' or 'items', not both")
    lines = order["lines"] if "lines" in order else order.get("items")

    return {"header": header, "lines": lines}


def check(order):
    errs = []
    try:
        order = normalize(order)
    except Stop as exc:
        return {}, [], [str(exc)]

    header = order["header"]
    known = {h[0] for h in HEADER}
    for key in header:
        if key not in known:
            errs.append(f"header: unknown field '{key}' (known: {sorted(known)})")

    lines = order.get("lines")
    if not isinstance(lines, list) or not lines:
        errs.append("'lines' or 'items' must be a non-empty list")
        lines = []
    for i, line in enumerate(lines, 1):
        if not isinstance(line, dict):
            errs.append(f"line {i}: expected an object")
            continue
        if not str(line.get("product_code", "")).strip():
            errs.append(f"line {i}: product_code missing")
        quantity = num(line.get("quantity"))
        if quantity is None or quantity <= 0:
            errs.append(f"line {i}: quantity must be a number > 0")
        unit_code = str(line.get("unitcode", "")).strip().upper()
        if unit_code not in UOM_OPTIONS:
            errs.append(
                f"line {i}: unitcode must be one of {sorted(UOM_OPTIONS)}"
            )
        if "rate" in line and num(line["rate"]) is None:
            errs.append(f"line {i}: rate is not a number")
    return header, lines, errs


# ------------------------------------------------------------------ windows --
user32 = ctypes.windll.user32 if sys.platform == "win32" else None


def _top_windows():
    """(hwnd, title, class) of every visible top-level window."""
    out = []
    cb_t = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(h, _):
        if user32.IsWindowVisible(h):
            t, c = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(h, t, 256)
            user32.GetClassNameW(h, c, 256)
            r = wt.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            out.append((h, t.value, c.value, r.right - r.left, r.bottom - r.top))
        return True
    user32.EnumWindows(cb_t(cb), 0)
    return out


def _window_process_id(hwnd):
    process_id = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    return int(process_id.value)


def message_box_open():
    return any(t == "Message Box" for _, t, _, _, _ in _top_windows())


BASELINE = set()


def refresh_baseline():
    """Remember every window that exists right now (e.g. the small clear/cross button), so only NEW ones count."""
    BASELINE.clear()
    BASELINE.update(h for h, *_ in _top_windows())


def lookup_list_open():
    """True only for a NEW untitled list-sized window. The small cross button is too small to count."""
    for h, t, c, w, ht in _top_windows():
        m = re.match(r"WindowsForms10\.Window\.(\d+)", c)
        if h not in BASELINE and not t and m and m.group(1) != "8" and w >= 120 and ht >= 60:
            return True
    return False


class Form:
    def __init__(self):
        from pywinauto import Desktop
        self.desk = Desktop(backend="uia")
        self.find_window()

    def find_window(self):
        for w in self.desk.windows():
            if re.fullmatch(r".*Sale Order - Add", w.window_text().strip()):
                self.win = w
                return
        raise Stop("Open the Sale Order 'Add' form first (title must end with 'Sale Order - Add').")

    # --- reading / typing
    def edits(self, auto_id):
        els = [e for e in self.win.descendants(control_type="Edit")
               if (e.element_info.automation_id or "") == auto_id]
        els.sort(key=lambda e: (round(e.rectangle().top / 12), e.rectangle().left))
        return els

    def edit(self, auto_id, index=0):
        els = self.edits(auto_id)
        if len(els) <= index:
            raise Stop(f"field {auto_id}[{index}] not found on the form")
        return els[index]

    @staticmethod
    def type_text(text):
        agent_replace_text(text, "typing in the FACT form")

    def cell(self, col, row):
        rx = re.compile(rf"^{re.escape(col)}\s*row {row}$")
        for e in self.win.descendants(control_type="DataItem"):
            if rx.match((e.window_text() or "").strip()):
                return e
        return None

    def cell_value(self, col, row):
        c = self.cell(col, row)
        if c is None:
            return None
        try:
            v = c.iface_value.CurrentValue
            if v is not None:
                return v
        except Exception:
            pass
        return c.window_text()

    def wait_for_quantity_dialog(self):
        """Find the modal by native HWND, then attach to it through UI Automation."""
        deadline = time.monotonic() + QUANTITY_DIALOG_TIMEOUT
        try:
            process_id = int(self.win.element_info.process_id)
            form_pid_detail = str(process_id)
        except Exception as exc:
            process_id = None
            form_pid_detail = f"unavailable ({type(exc).__name__}: {exc})"

        last_candidates = []
        enumeration_error = None

        while time.monotonic() < deadline:
            obey_hotkeys("waiting for the quantity/UOM window")
            matches = []
            last_candidates = []
            enumeration_error = None
            try:
                windows = _top_windows()
            except Exception as exc:
                windows = []
                enumeration_error = f"{type(exc).__name__}: {exc}"

            for hwnd, title, class_name, _, _ in windows:
                title = title.strip()
                window_pid = _window_process_id(hwnd)
                if "quantity" in title.casefold() or title == "Enter Quantity":
                    details = (
                        f"title={title!r}, class={class_name!r}, "
                        f"pid={window_pid}, hwnd={int(hwnd)}"
                    )
                    rejected = []
                    if title != "Enter Quantity":
                        rejected.append("title is not exactly 'Enter Quantity'")
                    if process_id is None:
                        rejected.append(f"Sale Order PID unavailable ({form_pid_detail})")
                    elif window_pid != process_id:
                        rejected.append(f"window PID does not match Sale Order PID {process_id}")

                    if rejected:
                        last_candidates.append(
                            f"{details}; rejected: {', '.join(rejected)}"
                        )
                        continue

                    try:
                        window = self.desk.window(handle=int(hwnd)).wrapper_object()
                        automation_id = window.element_info.automation_id or ""
                        visible = window.is_visible()
                    except Exception as exc:
                        last_candidates.append(
                            f"{details}; UIA attach/read failed: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        continue

                    if automation_id != "frmMultUOM":
                        last_candidates.append(
                            f"{details}, automation_id={automation_id!r}; "
                            "rejected: Automation ID is not 'frmMultUOM'"
                        )
                    elif not visible:
                        last_candidates.append(
                            f"{details}, automation_id={automation_id!r}; "
                            "rejected: UIA reports the window is not visible"
                        )
                    else:
                        matches.append(window)
                        last_candidates.append(
                            f"{details}, automation_id={automation_id!r}, "
                            "visible=True; all checks passed"
                        )

            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise Stop("more than one matching Enter Quantity window is open")
            time.sleep(0.15)

        diagnostic = [f"Sale Order PID: {form_pid_detail}."]
        if enumeration_error:
            diagnostic.append(f"Could not enumerate native windows: {enumeration_error}.")
        if last_candidates:
            diagnostic.append("Quantity-like native window(s): " + " | ".join(last_candidates))
        else:
            diagnostic.append(
                "No visible native top-level window with 'Quantity' in its title "
                "was found."
            )

        raise Stop(
            "the Enter Quantity window was not accepted; nothing was entered. "
            + " ".join(diagnostic)
        )


def dialog_control(window, automation_id, control_type=None):
    matches = [
        control
        for control in window.descendants()
        if (control.element_info.automation_id or "") == automation_id
        and (control_type is None or control.element_info.control_type == control_type)
    ]
    if len(matches) != 1:
        suffix = f" ({control_type})" if control_type else ""
        raise Stop(
            f"quantity window control {automation_id!r}{suffix} "
            f"matched {len(matches)} controls; expected exactly one"
        )
    return matches[0]


def control_value(control):
    try:
        value = control.iface_value.CurrentValue
        if value is not None:
            return str(value)
    except Exception:
        pass
    return str(control.window_text() or "")


def uom_label(value):
    """Extract the selected UOM from FACT's numbered combo/list label."""
    label = re.sub(r"^\s*\d+\.\s*", "", str(value or ""), count=1)
    return label.split("(", 1)[0].strip()


def settle(what, quiet=None, minwait=None):
    """Wait until FACT is quiet. A Message Box means FACT refused the value."""
    from pywinauto import Desktop
    quiet = SETTLE if quiet is None else quiet
    minwait = MIN_WAIT if minwait is None else minwait
    start = time.time()
    end, quiet_since = start + LOOKUP_TIMEOUT, None
    while time.time() < end:
        obey_hotkeys(what)
        if message_box_open():
            text = ""
            try:
                mb = Desktop(backend="uia").window(title="Message Box")
                text = mb.child_window(auto_id="lblDetail", found_index=0).window_text()
                for bid in ("btnNo2", "btnOk"):          # safest button, never Yes
                    b = mb.child_window(auto_id=bid, found_index=0)
                    if b.exists(timeout=0.3):
                        agent_click(b, mb.wrapper_object(), f"closing dialog for {what}")
                        break
            except Exception:
                pass
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text)).strip()
            raise Stop(f"FACT refused {what}: {text or '(Message Box, text not readable)'}")
        if lookup_list_open():
            quiet_since = None
        else:
            quiet_since = quiet_since or time.time()
            if time.time() - quiet_since >= quiet and time.time() - start >= minwait:
                return
        time.sleep(0.15)
    if lookup_list_open():
        agent_press("Esc", what)
        raise Stop(f"{what}: the lookup list stayed open (code not found or ambiguous)")
    raise Stop(f"{what}: FACT did not settle in {LOOKUP_TIMEOUT}s")


def focused_text():
    """Text of the control that has keyboard focus right now (the cell editor)."""
    from pywinauto.uia_defines import IUIA
    from pywinauto.uia_element_info import UIAElementInfo
    from pywinauto.controls.uiawrapper import UIAWrapper
    w = UIAWrapper(UIAElementInfo(IUIA().get_focused_element()))
    try:
        v = w.iface_value.CurrentValue
        if v is not None:
            return v
    except Exception:
        pass
    return w.window_text()


def fill_quantity_dialog(f, row, line):
    """Fill the quantity and UOM popup opened by the line grid."""
    unit_code = str(line["unitcode"]).strip().upper()
    expected_unit = UOM_OPTIONS.get(unit_code)
    if expected_unit is None:
        raise Stop(f"line {row + 1}: unsupported unitcode {unit_code!r}")

    dialog = f.wait_for_quantity_dialog()
    product_code = control_value(dialog_control(dialog, "TxtProdcode"))
    expected_product = str(line["product_code"]).strip()
    if norm(product_code) != norm(expected_product):
        raise Stop(
            f"line {row + 1}: quantity window is for product {product_code!r}, "
            f"not {expected_product!r}; nothing was entered"
        )

    quantity = str(line["quantity"]).strip()
    quantity_field = dialog_control(dialog, "TxtQuantity", "Edit")
    if not quantity_field.is_enabled():
        raise Stop(f"line {row + 1}: quantity field is not editable")
    agent_click(quantity_field, dialog, f"focusing quantity for line {row + 1}")
    time.sleep(0.1)
    agent_replace_text(quantity, f"entering quantity for line {row + 1}")
    agent_press("Tab", f"committing quantity for line {row + 1}")
    time.sleep(0.15)
    actual_quantity = control_value(quantity_field)
    if num(actual_quantity) != num(quantity):
        raise Stop(
            f"line {row + 1}: quantity window shows {actual_quantity!r}, "
            f"expected {quantity!r}"
        )

    options = []
    for item in dialog.descendants(control_type="ListItem"):
        raw_label = str(item.element_info.name or item.window_text() or "")
        if norm(uom_label(raw_label)) == norm(expected_unit):
            options.append(item)
    if len(options) != 1:
        raise Stop(
            f"line {row + 1}: could not uniquely find UOM {expected_unit!r} "
            f"in the quantity window ({len(options)} matches)"
        )

    try:
        options[0].iface_selection_item.Select()
    except Exception as exc:
        raise Stop(
            f"line {row + 1}: FACT did not accept UOM {expected_unit!r}: {exc}"
        ) from exc

    uom_control = dialog_control(dialog, "peUOM", "ComboBox")
    unit_deadline = time.monotonic() + 3.0
    selected_label = control_value(uom_control)
    actual_unit = uom_label(selected_label)
    while norm(actual_unit) != norm(expected_unit) and time.monotonic() < unit_deadline:
        obey_hotkeys(f"waiting for UOM on line {row + 1}")
        time.sleep(0.1)
        selected_label = control_value(uom_control)
        actual_unit = uom_label(selected_label)
    if norm(actual_unit) != norm(expected_unit):
        raise Stop(
            f"line {row + 1}: UOM selector shows {selected_label!r}, "
            f"expected a selection of {expected_unit!r}"
        )

    ok = dialog_control(dialog, "btnOK")
    agent_click(ok, dialog, f"confirming quantity for line {row + 1}")
    close_deadline = time.monotonic() + 10.0
    while time.monotonic() < close_deadline:
        obey_hotkeys(f"waiting for quantity window to close on line {row + 1}")
        try:
            if dialog.is_visible():
                time.sleep(0.1)
                continue
        except Exception:
            pass

        settle(f"line {row + 1} quantity")
        actual_grid_uom = f.cell_value("UOM", row)
        if actual_grid_uom is None:
            raise Stop(
                f"line {row + 1}: quantity window closed, but the committed "
                "UOM cell could not be read from the order grid"
            )
        if norm(str(actual_grid_uom)) != norm(expected_unit):
            raise Stop(
                f"line {row + 1}: quantity window closed, but the order grid "
                f"shows UOM {actual_grid_uom!r}, expected {expected_unit!r}"
            )
        print(f"  line {row + 1}: quantity {quantity} {expected_unit}")
        return

    raise Stop(
        f"line {row + 1}: quantity window did not close after OK; "
        "the line was not continued"
    )


def keyed(f, i, col, key, value):
    """Type into the cell that already has focus, check what was typed, Tab, then re-check the grid if visible."""
    obey_hotkeys(f"line {i + 1} {col}")
    val = str(value)
    refresh_baseline()
    f.type_text(val)
    time.sleep(0.2)
    typed = focused_text()
    if num(typed) != num(val):
        raise Stop(f"line {i + 1}: focus is not in the {col} cell (typed {key} '{val}', the focused box shows '{typed}')")
    agent_press("Tab", f"line {i + 1} {col}")
    settle(f"line {i + 1} {key}", quiet=0.25, minwait=0.3)
    cell = f.cell_value(col, i)
    if cell is None:
        print(f"    ({col} is scrolled out of view, checked while typing: '{typed}')")
    elif num(cell) != num(val):
        raise Stop(f"line {i + 1}: typed {key} '{val}' but the grid shows '{cell}'")


def next_row(f, row, tabs):
    """Walk remaining columns to the next row using tagged Tab input."""

    def row_ready(wait):
        end = time.time() + wait
        while time.time() < end:
            obey_hotkeys(f"moving to row {row + 1}")
            if message_box_open():
                settle(f"moving to row {row + 1}")      # reads + closes it, raises Stop with FACT's text
            if f.cell("Product Code", row) is not None:
                return True
            time.sleep(0.2)
        return False

    for _ in range(tabs):
        agent_press("Tab", f"moving to row {row + 1}")
        time.sleep(0.03)
    if row_ready(6):
        time.sleep(0.2)
        return
    for _ in range(8):                                   # fallback: slower, one Tab at a time
        agent_press("Tab", f"moving to row {row + 1}")
        if row_ready(1.5):
            time.sleep(0.2)
            return
    raise Stop(f"row {row + 1} did not appear")


# ------------------------------------------------------------------- filling --
def fill(order):
    header, lines, errs = check(order)
    if errs:
        raise Stop("The JSON has problems:\n - " + "\n - ".join(errs))
    f = Form()
    f.win.set_focus()

    for key, auto_id, idx, desc in HEADER:
        if key not in header or str(header[key]).strip() == "":
            continue                                    # no value in JSON: leave the field alone
        val = str(header[key])
        e = f.edit(auto_id, idx)
        if not e.is_enabled():
            raise Stop(f"{key}: the field is not editable right now")
        print(f"  {key} = {val}")
        refresh_baseline()
        agent_click(e, f.win, f"focusing {key}")
        time.sleep(0.2)
        f.type_text(val)
        agent_press("Tab", f"committing {key}")
        settle(key)
        got = f.edit(auto_id, idx).window_text()
        if norm(got) != norm(val):
            raise Stop(f"{key}: typed '{val}' but the field shows '{got}'")
        try:
            if desc and not norm(f.edit(desc).window_text()):
                print(f"  WARNING {key}: description is empty, check this field by eye")
        except Stop:
            pass

    for i, ln in enumerate(lines):
        end = time.time() + 20
        while f.cell("Product Code", i) is None:        # FACT adds the next empty row by itself
            if time.time() > end:
                raise Stop(f"grid row {i + 1} did not appear")
            time.sleep(0.5)
        print(f"  line {i + 1}: {ln['product_code']} x {ln['quantity']}" + (f" @ {ln['rate']}" if "rate" in ln else ""))
        # 1) product code: click the cell (always visible, grid is scrolled left on a new row), type, Tab
        code = str(ln["product_code"])
        c = f.cell("Product Code", i)
        if c is None:
            raise Stop(f"line {i + 1}: Product Code cell not found")
        refresh_baseline()
        agent_click(c, f.win, f"focusing Product Code row {i}")
        time.sleep(0.15)
        f.type_text(code)
        agent_press("Tab", f"committing Product Code row {i}")
        settle(f"line {i + 1} product_code")
        got = f.cell_value("Product Code", i)
        if norm(got) != norm(code):
            raise Stop(f"line {i + 1}: typed product_code '{code}' but the grid shows '{got}'")
        if not norm(f.cell_value("Product Description", i)):
            raise Stop(f"line {i + 1}: product '{code}' was not recognised (no description)")
        # Tab from Description opens the quantity/UOM popup. Confirming it
        # closes the popup and leaves focus directly on Rate.
        agent_press("Tab", f"opening quantity window for line {i + 1}")
        fill_quantity_dialog(f, i, ln)

        has_rate = "rate" in ln and str(ln["rate"]).strip() != ""
        if has_rate:
            keyed(f, i, "Rate", "rate", ln["rate"])
        if i < len(lines) - 1:
            if has_rate:
                next_row(f, i + 1, 7)
            else:
                # Preserve the old navigation starting point: after the modal,
                # focus is on the blank Rate cell instead of after Quantity.
                agent_press("Tab", f"skipping empty Rate on line {i + 1}")
                next_row(f, i + 1, 9)
    print(f"DONE: {len(lines)} line(s) filled and checked. Review the form, then Save by hand.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 0
    with open(sys.argv[1], encoding="utf-8") as source:
        order = json.load(source)
    if "--check" in sys.argv:
        _, lines, errs = check(order)
        print(("OK: %d line(s)" % len(lines)) if not errs else "PROBLEMS:\n - " + "\n - ".join(errs))
        return 0 if not errs else 1
    if sys.platform != "win32":
        print("Windows only.", file=sys.stderr)
        return 1
    from pynput import keyboard

    listener = keyboard.GlobalHotKeys({"<ctrl>+<alt>+x": abort.set})
    listener.start()
    try:
        fill(order)
    except Stop as e:
        print("STOPPED:", e)
        return 1
    finally:
        listener.stop()
        listener.join(timeout=2.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())