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
SETTLE = 0.6          # seconds without any pop-up before a lookup field counts as done
MIN_WAIT = 0.8        # always give a wrong-code pop-up this long to appear after Tab
LOOKUP_TIMEOUT = 60   # product lookups can take 13 s on a good day


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


def check(order):
    errs = []
    header = order.get("header")
    if header is None:
        header = {k: v for k, v in order.items() if k != "lines"}
    known = {h[0] for h in HEADER}
    for k in header:
        if k not in known:
            errs.append(f"header: unknown field '{k}' (known: {sorted(known)})")
    lines = order.get("lines")
    if not isinstance(lines, list) or not lines:
        errs.append("'lines' must be a non-empty list")
        lines = []
    for i, ln in enumerate(lines, 1):
        if not str(ln.get("product_code", "")).strip():
            errs.append(f"line {i}: product_code missing")
        if num(ln.get("quantity")) is None or num(ln["quantity"]) <= 0:
            errs.append(f"line {i}: quantity must be a number > 0")
        if "rate" in ln and num(ln["rate"]) is None:
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
        # 2) the rest by KEYBOARD only: the grid scrolls sideways, so Quantity/Rate may be off-screen
        #    and cannot be clicked. Focus is on Product Description now.
        #    Tab -> Quantity, Tab -> Pre-close, Tab -> Rate
        agent_press("Tab", f"moving to Quantity row {i}")  # description -> quantity
        time.sleep(0.2)
        keyed(f, i, "Quantity", "quantity", ln["quantity"])
        if "rate" in ln and str(ln["rate"]).strip() != "":
            agent_press("Tab", f"moving to Rate row {i}")  # pre-close -> rate
            time.sleep(0.2)
            keyed(f, i, "Rate", "rate", ln["rate"])
        if i < len(lines) - 1:                          # not after the last line: no stray empty row
            next_row(f, i + 1, 7 if "rate" in ln and str(ln["rate"]).strip() != "" else 9)
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