"""uia_inspector.py - dump the live UI Automation tree of a running app.

Purpose: build the control map for FACT ERP.NG. Start the app, log in, open the
screen you care about (Sale Order - Add, for example), type your dummy data,
then run this script, pick the app, and it writes the whole window tree to
``scripts/uia/<name>.json``.

    python scripts/uia_inspector.py                 # pick an app, then a window, dump it
    python scripts/uia_inspector.py --list          # only list apps
    python scripts/uia_inspector.py --pid 4321      # skip the picker
    python scripts/uia_inspector.py --window 2      # only the 2nd window
    python scripts/uia_inspector.py --name add_form # scripts/uia/add_form.json
    python scripts/uia_inspector.py --probe         # also test Ctrl+C on the focused control

What ends up in the JSON, per control:

    path            0/2/1 style index path from the window (stable identity)
    name, automation_id, class_name, control_type, framework_id
    value           ValuePattern value (what an edit box currently holds)
    text            legacy window text, when different from value
    patterns        Value/Text/Grid/Table/Selection/... the control supports
    grid/table      row and column counts when the control exposes them
    editable, keyboard_focusable, enabled, offscreen, rectangle
    children        the subtree, up to --depth / --max-nodes

The important part for planning: whether the line-item grid exposes Grid/Table
patterns and per-cell values, or only draws itself. Add ``--probe`` to test if a
focused grid cell can be copied with Ctrl+C.

This tool only reads the UI. It never clicks, types, or changes the app, except
for ``--probe`` which sends Ctrl+C to whatever control currently has focus.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import re
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = "uia-inspector/1"
OUT_DIR = Path(__file__).resolve().parent / "uia"

# Patterns worth recording for mapping. pywinauto resolves these names to the
# real COM interfaces, which is the only reliable availability test.
PATTERN_NAMES = (
    "Invoke",
    "Selection",
    "SelectionItem",
    "Value",
    "RangeValue",
    "Scroll",
    "ScrollItem",
    "ExpandCollapse",
    "Grid",
    "GridItem",
    "Table",
    "TableItem",
    "Text",
    "Toggle",
    "LegacyIAccessible",
)

# Windows that are chrome, not content. Never treat them as screens or popups.
IGNORED_CLASSES = {"tooltips_class32", "ShadowWindow", "Windows.UI.Core.CoreWindow"}

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.EnumWindows.argtypes = [
    ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM),
    wintypes.LPARAM,
]
user32.EnumWindows.restype = ctypes.c_bool
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = ctypes.c_bool
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = ctypes.c_bool
user32.GetForegroundWindow.restype = wintypes.HWND

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


# ------------------------------------------------------------------ win32 read --
def _window_text(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(1024)
    user32.GetWindowTextW(hwnd, buf, 1024)
    return buf.value


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetClassNameW(hwnd, buf, 512)
    return buf.value


def _window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _window_rect(hwnd: int) -> dict[str, int] | None:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return {
        "left": int(rect.left),
        "top": int(rect.top),
        "right": int(rect.right),
        "bottom": int(rect.bottom),
        "width": int(rect.right - rect.left),
        "height": int(rect.bottom - rect.top),
    }


def _process_exe(pid: int) -> str:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(2048)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def enumerate_windows() -> list[dict]:
    """Every visible top-level window, topmost first."""
    windows: list[dict] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            windows.append(
                {
                    "hwnd": int(hwnd),
                    "title": _window_text(hwnd),
                    "class_name": _window_class(hwnd),
                    "pid": _window_pid(hwnd),
                    "rect": _window_rect(hwnd),
                }
            )
        return True

    user32.EnumWindows(callback, 0)
    for index, window in enumerate(windows):
        window["z_order"] = index
    return windows


def group_apps(windows: list[dict]) -> list[dict]:
    """Group titled, non-chrome windows by process."""
    apps: dict[int, dict] = {}
    for window in windows:
        if window["class_name"] in IGNORED_CLASSES:
            continue
        if not window["title"].strip():
            continue
        pid = window["pid"]
        app = apps.setdefault(
            pid,
            {
                "pid": pid,
                "exe": _process_exe(pid),
                "windows": [],
            },
        )
        app["windows"].append(window)
    for app in apps.values():
        name = Path(app["exe"]).name if app["exe"] else f"pid_{app['pid']}"
        app["process"] = name
        app["exe"] = app["exe"]
    return sorted(apps.values(), key=lambda item: (item["process"].lower(), item["pid"]))


# -------------------------------------------------------------------- dumping --
class Budget:
    """Hard stop for very large trees, so the dump always finishes."""

    def __init__(self, max_nodes: int):
        self.left = max_nodes
        self.truncated = False

    def take(self) -> bool:
        if self.left <= 0:
            self.truncated = True
            return False
        self.left -= 1
        return True


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _patterns(wrapper) -> list[str]:
    found: list[str] = []
    element = _safe(lambda: wrapper.element_info.element)
    if element is None:
        return found
    from pywinauto import uia_defines

    for name in PATTERN_NAMES:
        try:
            uia_defines.get_elem_interface(element, name)
            found.append(name)
        except Exception:
            continue
    return found


def _value(wrapper, patterns: list[str]) -> str | None:
    if "Value" in patterns:
        raw = _safe(lambda: wrapper.iface_value.CurrentValue)
        if raw is not None:
            return str(raw)
    return None


ROW_NAME_RE = re.compile(r"^(?P<column>.*?)\s+row\s+(?P<row>\d+)\s*$", re.IGNORECASE)


def infer_grid(node: dict, max_sample: int = 12) -> dict | None:
    """Recognise a DevExpress-style grid from its cell names.

    The FACT line grid exposes no Grid/Table pattern, but every cell is a
    DataItem named ``"<column> row <n>"`` with a Value. That is enough to map
    and verify rows without pixel coordinates.
    """
    cells: list[tuple[int, str, str | None, str]] = []

    def collect(current: dict) -> None:
        for child in current.get("children") or []:
            name = (child.get("name") or "").strip()
            match = ROW_NAME_RE.match(name)
            if match:
                cells.append(
                    (
                        int(match.group("row")),
                        match.group("column").strip(),
                        child.get("value"),
                        child.get("path", ""),
                    )
                )
            collect(child)

    collect(node)
    if len(cells) < 2:
        return None
    rows = sorted({cell[0] for cell in cells})
    columns: list[str] = []
    for _, column, _, _ in cells:
        label = column or "(blank)"
        if label not in columns:
            columns.append(label)
    valued = [cell for cell in cells if cell[2] not in (None, "")]
    sample = [
        {"row": row, "column": column, "value": value, "path": path}
        for row, column, value, path in cells[:max_sample]
    ]
    return {
        "rows": len(rows),
        "row_numbers": rows,
        "columns": columns,
        "cells": len(cells),
        "cells_with_value": len(valued),
        "sample_cells": sample,
    }


def _grid_info(wrapper, patterns: list[str]) -> dict:
    info: dict = {}
    if "Table" in patterns:
        table = _safe(lambda: wrapper.iface_table)
        if table is not None:
            info["table"] = {
                "rows": _safe(lambda: int(table.CurrentRowCount)),
                "columns": _safe(lambda: int(table.CurrentColumnCount)),
            }
    if "Grid" in patterns:
        grid = _safe(lambda: wrapper.iface_grid)
        if grid is not None:
            info["grid"] = {
                "rows": _safe(lambda: int(grid.CurrentRowCount)),
                "columns": _safe(lambda: int(grid.CurrentColumnCount)),
            }
    if "GridItem" in patterns:
        item = _safe(lambda: wrapper.iface_grid_item)
        if item is not None:
            info["grid_item"] = {
                "row": _safe(lambda: int(item.CurrentRow)),
                "column": _safe(lambda: int(item.CurrentColumn)),
            }
    return info


def dump_control(
    wrapper,
    path: str,
    depth: int,
    max_depth: int,
    budget: Budget,
    errors: list[str],
) -> dict | None:
    if not budget.take():
        return None

    info = wrapper.element_info
    patterns = _patterns(wrapper)
    node: dict = {
        "path": path,
        "depth": depth,
        "control_type": _safe(lambda: info.control_type),
        "name": _safe(lambda: info.name) or "",
        "automation_id": _safe(lambda: info.automation_id) or "",
        "class_name": _safe(lambda: info.class_name) or "",
        "framework_id": _safe(lambda: info.framework_id) or "",
        "value": _value(wrapper, patterns),
        "text": _safe(lambda: wrapper.window_text()) or "",
        "patterns": patterns,
        "editable": bool(_safe(lambda: wrapper.is_editable(), False)),
        "keyboard_focusable": bool(_safe(lambda: wrapper.is_keyboard_focusable(), False)),
        "enabled": bool(_safe(lambda: wrapper.is_enabled(), False)),
        "offscreen": bool(_safe(lambda: wrapper.is_offscreen(), False)),
        "rectangle": None,
    }
    rect = _safe(lambda: wrapper.rectangle(), None)
    if isinstance(rect, (list, tuple)):
        node["rectangle"] = [int(value) for value in rect]
    elif rect is not None:
        node["rectangle"] = {
            "left": int(rect.left),
            "top": int(rect.top),
            "right": int(rect.right),
            "bottom": int(rect.bottom),
        }
    node.update(_grid_info(wrapper, patterns))

    if depth >= max_depth:
        node["children"] = []
        node["depth_limited"] = True
        return node

    children = _safe(lambda: wrapper.children(), []) or []
    node["child_count"] = len(children)
    dumped: list[dict] = []
    for index, child in enumerate(children):
        child_node = dump_control(
            child,
            f"{path}/{index}",
            depth + 1,
            max_depth,
            budget,
            errors,
        )
        if child_node is None:
            node["children_truncated"] = True
            break
        dumped.append(child_node)
    node["children"] = dumped
    if node.get("control_type") == "Table" and dumped:
        inferred = infer_grid(node)
        if inferred is not None:
            node["inferred_grid"] = inferred
    return node


def dump_window(hwnd: int, max_depth: int, max_nodes: int) -> dict:
    from pywinauto import Desktop

    errors: list[str] = []
    budget = Budget(max_nodes)
    result: dict = {
        "hwnd": hwnd,
        "title": _window_text(hwnd),
        "class_name": _window_class(hwnd),
        "rectangle": _window_rect(hwnd),
        "foreground": int(user32.GetForegroundWindow()) == hwnd,
        "uia": None,
        "errors": errors,
        "truncated": False,
    }
    try:
        wrapper = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
    except Exception as exc:
        errors.append(f"UIA attach failed: {exc!r}")
        return result

    try:
        tree = dump_control(wrapper, "0", 0, max_depth, budget, errors)
    except Exception as exc:
        errors.append(f"tree walk failed: {exc!r}")
        tree = None
    result["uia"] = tree
    result["truncated"] = budget.truncated
    result["uia_node_count"] = max_nodes - budget.left
    return result


# -------------------------------------------------------------------- summary --
def summarise(windows: list[dict]) -> dict:
    control_types: dict[str, int] = {}
    patterns: dict[str, int] = {}
    values: list[dict] = []
    grids: list[dict] = []
    total_nodes = 0

    def walk(node: dict) -> None:
        nonlocal total_nodes
        if node is None:
            return
        total_nodes += 1
        control_type = node.get("control_type") or "Unknown"
        control_types[control_type] = control_types.get(control_type, 0) + 1
        for pattern in node.get("patterns") or []:
            patterns[pattern] = patterns.get(pattern, 0) + 1
        if "table" in node or "grid" in node or "grid_item" in node or "inferred_grid" in node:
            grids.append(
                {
                    "path": node["path"],
                    "control_type": control_type,
                    "name": node.get("name", ""),
                    "automation_id": node.get("automation_id", ""),
                    **{
                        key: node[key]
                        for key in ("table", "grid", "grid_item", "inferred_grid")
                        if key in node
                    },
                }
            )
        if node.get("value") or node.get("text"):
            values.append(
                {
                    "path": node["path"],
                    "control_type": control_type,
                    "name": node.get("name", ""),
                    "automation_id": node.get("automation_id", ""),
                    "value": (node.get("value") or node.get("text") or "")[:200],
                }
            )
        for child in node.get("children") or []:
            walk(child)

    for window in windows:
        walk(window.get("uia"))
    return {
        "nodes": total_nodes,
        "control_types": dict(sorted(control_types.items())),
        "patterns": dict(sorted(patterns.items())),
        "value_controls": values,
        "grids": grids,
    }


# ----------------------------------------------------------------------- probe --
def probe_clipboard() -> dict:
    """Test whether the focused control can be copied with Ctrl+C.

    Sends Ctrl+C to whatever currently has focus, so focus the grid cell or edit
    box you want to test first. The previous clipboard text is restored.
    """
    result: dict = {"ok": False, "before": "", "after": "", "changed": False}
    try:
        import pyperclip
        from pywinauto.keyboard import send_keys
    except Exception as exc:
        result["error"] = f"probe unavailable: {exc!r}"
        return result

    try:
        result["before"] = pyperclip.paste() or ""
        send_keys("^c")
        time.sleep(0.6)
        result["after"] = pyperclip.paste() or ""
        result["changed"] = result["after"] != result["before"]
        result["ok"] = True
        if result["before"]:
            pyperclip.copy(result["before"])
    except Exception as exc:
        result["error"] = repr(exc)
    return result


# ------------------------------------------------------------------------- cli --
def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", text.strip()).strip("_")
    return cleaned or "uia_dump"


def _ask_app(apps: list[dict]) -> dict | None:
    print("\nRunning applications with visible windows:\n")
    for index, app in enumerate(apps, start=1):
        print(f"  [{index:2}] {app['process']:28} pid {app['pid']:6}  {len(app['windows'])} window(s)")
    if not apps:
        print("  (none found - is the app running?)")
        return None

    while True:
        raw = input("\nSelect an application number (Enter = 1, q = quit): ").strip().lower()
        if raw in {"q", "quit", "exit"}:
            return None
        if not raw:
            return apps[0]
        if raw.isdigit() and 1 <= int(raw) <= len(apps):
            return apps[int(raw) - 1]
        print("  please enter one of the numbers above")


def _ask_window(windows: list[dict]) -> dict | None:
    """Prompt for one visible window from the selected application."""
    print("\nWindows in the selected application:\n")
    for index, window in enumerate(windows, start=1):
        title = window["title"].strip() or "(untitled window)"
        print(
            f"  [{index:2}] hwnd {window['hwnd']:10} "
            f"{window['class_name'][:24]:24} {title[:110]}"
        )
    if not windows:
        print("  (no eligible windows found)")
        return None

    while True:
        raw = input("\nSelect a window number (Enter = 1, q = quit): ").strip().lower()
        if raw in {"q", "quit", "exit"}:
            return None
        if not raw:
            return windows[0]
        if raw.isdigit() and 1 <= int(raw) <= len(windows):
            return windows[int(raw) - 1]
        print("  please enter one of the numbers above")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dump the live UIA tree of a running app for control mapping."
    )
    parser.add_argument("--list", action="store_true", help="list apps and exit")
    parser.add_argument("--pid", type=int, help="dump this process id (skips the picker)")
    parser.add_argument("--window", type=int, help="1-based window index within the app")
    parser.add_argument("--name", help="output file name without .json")
    parser.add_argument("--out", help="output directory (default: scripts/uia)")
    parser.add_argument("--depth", type=int, default=40, help="max tree depth (default 40)")
    parser.add_argument("--max-nodes", type=int, default=8000, help="node cap (default 8000)")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="send Ctrl+C to the focused control and record the clipboard result",
    )
    parser.add_argument("--all-windows", action="store_true", help="include untitled windows")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("uia_inspector only runs on Windows.")
        return 2

    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    windows = enumerate_windows()
    apps = group_apps(windows)
    if args.list:
        for index, app in enumerate(apps, start=1):
            print(f"[{index:2}] {app['process']:28} pid {app['pid']:6}  {len(app['windows'])} window(s)")
            for window in app["windows"]:
                print(f"      {window['hwnd']:10}  {window['title'][:110]}")
        return 0

    if args.pid is not None:
        app = next((item for item in apps if item["pid"] == args.pid), None)
        if app is None:
            print(f"No visible titled window found for pid {args.pid}.")
            return 2
    else:
        app = _ask_app(apps)
        if app is None:
            return 2

    print(f"\nTarget: {app['process']} (pid {app['pid']})")
    if app["exe"]:
        print(f"Exe:    {app['exe']}")

    targets = app["windows"]
    if not args.all_windows:
        targets = [window for window in targets if window["title"].strip()]
    if not targets:
        print("Nothing to dump.")
        return 2
    if args.window is not None:
        if not 1 <= args.window <= len(targets):
            print(f"--window must be between 1 and {len(targets)}")
            return 2
        targets = [targets[args.window - 1]]
    else:
        selected_window = _ask_window(targets)
        if selected_window is None:
            return 2
        targets = [selected_window]

    print("Capturing (this can take a few seconds on a busy form)...")
    dumped: list[dict] = []
    for window in targets:
        print(f"  - {window['title'][:80]!r}")
        dumped.append(dump_window(window["hwnd"], args.depth, args.max_nodes))

    payload: dict = {
        "schema": SCHEMA_VERSION,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "app": {"process": app["process"], "pid": app["pid"], "exe": app["exe"]},
        "windows": dumped,
    }
    summary = summarise(dumped)
    payload["summary"] = summary

    if args.probe:
        print("Probing clipboard (Ctrl+C on the focused control)...")
        payload["clipboard_probe"] = probe_clipboard()

    out_dir = Path(args.out) if args.out else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _slug(args.name or Path(app["process"]).stem.lower())
    path = out_dir / f"{stem}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\nControl types:")
    for control_type, count in summary["control_types"].items():
        print(f"  {control_type:22} {count}")
    print("Patterns:")
    for pattern, count in summary["patterns"].items():
        print(f"  {pattern:22} {count}")

    grids = summary["grids"]
    if grids:
        print(f"\nGrid/table controls: {len(grids)}")
        for grid in grids[:10]:
            inferred = grid.get("inferred_grid")
            if inferred:
                print(
                    f"  {grid['path']:16} {grid['control_type']:16} "
                    f"rows={inferred['rows']} cols={len(inferred['columns'])} "
                    f"cells={inferred['cells']} with-value={inferred['cells_with_value']} "
                    f"name={grid['name'] or grid['automation_id']!r}"
                )
                print(f"      columns: {', '.join(inferred['columns'])}")
                continue
            dims = grid.get("table") or grid.get("grid") or {}
            print(
                f"  {grid['path']:16} {grid['control_type']:16} "
                f"rows={dims.get('rows')} cols={dims.get('columns')} "
                f"name={grid['name'][:40]!r}"
            )
    else:
        print("\nNo Grid/Table pattern found - the line grid may be custom-drawn.")

    values = summary["value_controls"]
    print(f"\nControls with a value/text: {len(values)}")
    for item in values[:40]:
        print(
            f"  {item['path']:16} {item['control_type']:16} "
            f"{item['name'][:32]!r:34} {item['value'][:60]!r}"
        )
    if len(values) > 40:
        print(f"  ... {len(values) - 40} more in the JSON")

    if args.probe:
        probe = payload["clipboard_probe"]
        print(
            f"\nClipboard probe: ok={probe.get('ok')} changed={probe.get('changed')} "
            f"text={probe.get('after', '')[:80]!r}"
        )

    print(f"\nSaved {summary['nodes']} UIA nodes from {len(dumped)} window(s) to:\n  {path}")
    if summary["nodes"] == 0:
        print(
            "No UIA nodes were read. If the app runs elevated, run this script "
            "elevated as well."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
