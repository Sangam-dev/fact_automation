
import json
import time
import ctypes
from pathlib import Path

from pywinauto import Desktop
from pywinauto.keyboard import send_keys


# ============================================================
# CONFIGURATION
# ============================================================

JSON_FILE = Path(__file__).parent / "sale_order.json"

WINDOW_TITLE = "Sale Order"
GRID_AUTO_ID = "gcProduct"

NAVIGATION_KEY = "{TAB}"

DELAY = 0.20
TYPE_DELAY = 0.01

# Maximize FACT ERP when automation starts
MAXIMIZE_WINDOW = True

# ============================================================
# WINDOWS API - BRING WINDOW TO FRONT
# ============================================================

user32 = ctypes.windll.user32

SW_RESTORE = 9
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040

HWND_TOP = 0


def bring_window_to_front(hwnd):

    # Restore minimized window
    user32.ShowWindow(hwnd, SW_RESTORE)

    # Move window to the top of Z-order
    user32.SetWindowPos(
        hwnd,
        HWND_TOP,
        0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
    )

    # Request foreground activation
    user32.SetForegroundWindow(hwnd)

    time.sleep(0.5)


# ============================================================
# LOAD JSON
# ============================================================

def load_data():

    if not JSON_FILE.exists():
        raise FileNotFoundError(
            f"JSON file not found: {JSON_FILE}"
        )

    with open(JSON_FILE, "r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data.get("header", {}), dict):
        raise ValueError("Header must be a JSON object.")

    if not isinstance(data.get("products", []), list):
        raise ValueError("Products must be a JSON array.")

    return data


# ============================================================
# CONNECT TO FACT ERP
# ============================================================

def connect_window():

    desktop = Desktop(backend="uia")

    matches = [
        w for w in desktop.windows()
        if WINDOW_TITLE.lower() in w.window_text().lower()
    ]

    if not matches:
        raise RuntimeError(
            f"Window containing '{WINDOW_TITLE}' was not found."
        )

    hwnd = matches[0].handle

    # IMPORTANT:
    # Return a WindowSpecification, not a UIAWrapper.
    window = desktop.window(handle=hwnd)

    # Restore and maximize
    window.restore()

    if MAXIMIZE_WINDOW:
        window.maximize()

    # Bring it to the top
    bring_window_to_front(hwnd)

    window.set_focus()

    print("\nConnected to FACT ERP:")
    print(window.window_text())

    return window


# ============================================================
# TEXT ENTRY
# ============================================================

def enter_text(value):

    value = "" if value is None else str(value)

    send_keys(
        value,
        with_spaces=True,
        pause=TYPE_DELAY
    )

    time.sleep(DELAY)


def replace_text(control, value):

    value = "" if value is None else str(value)

    control.set_focus()

    try:
        control.set_edit_text(value)

    except Exception:

        control.click_input()

        send_keys("^a")
        enter_text(value)

    time.sleep(DELAY)


# ============================================================
# HEADER ENTRY
# ============================================================

def fill_header(window, header):

    print("\n========== HEADER ENTRY ==========")

    for automation_id, value in header.items():

        try:

            # window is a WindowSpecification here
            control = window.child_window(
                auto_id=automation_id
            ).wrapper_object()

            if not control.is_visible():
                raise RuntimeError("Control is not visible.")

            if not control.is_enabled():
                raise RuntimeError("Control is disabled.")

            replace_text(control, value)

            print(
                f"[OK] {automation_id} = {value}"
            )

        except Exception as error:

            raise RuntimeError(
                f"Failed to fill header field "
                f"'{automation_id}': {error}"
            ) from error

    print("\nHeader entry completed.")


# ============================================================
# PRODUCT GRID
# ============================================================

def find_product_grid(window):

    print("\nLocating product grid...")

    grid = window.child_window(
        auto_id=GRID_AUTO_ID,
        control_type="Table"
    ).wrapper_object()

    if not grid.is_visible():
        raise RuntimeError(
            "Product grid is not visible."
        )

    print("[OK] Product grid found.")

    return grid


# ============================================================
# INSPECT GRID
# ============================================================

def inspect_grid(grid):

    print("\n========== GRID ELEMENTS ==========")

    descendants = grid.descendants()

    candidates = []

    for index, element in enumerate(descendants):

        try:

            info = element.element_info
            rect = element.rectangle()

            if not element.is_visible():
                continue

            if not element.is_enabled():
                continue

            if rect.width() <= 0 or rect.height() <= 0:
                continue

            print(
                f"\nIndex: {index}"
                f"\nName: {info.name}"
                f"\nType: {info.control_type}"
                f"\nAutomation ID: {info.automation_id}"
                f"\nRectangle: {rect}"
            )

            if info.control_type in ("Edit", "DataItem"):
                candidates.append(element)

        except Exception:
            continue

    return candidates


# ============================================================
# FIND FIRST PRODUCT CELL
# ============================================================

def find_first_product_cell(grid):

    candidates = inspect_grid(grid)

    if not candidates:

        raise RuntimeError(
            "No visible editable grid cells were identified. "
            "Inspect the grid UIA hierarchy."
        )

    # Sort candidates from top-left to bottom-right
    candidates.sort(
        key=lambda element: (
            element.rectangle().top,
            element.rectangle().left
        )
    )

    print("\n========== POSSIBLE FIRST CELLS ==========")

    for index, element in enumerate(candidates):

        print(
            f"\nCandidate {index}"
            f"\nName: {element.element_info.name}"
            f"\nType: {element.element_info.control_type}"
            f"\nAutomation ID: {element.element_info.automation_id}"
            f"\nRectangle: {element.rectangle()}"
        )

    # We do not blindly click the first UIA element.
    selected_index = input(
        "\nEnter the candidate number corresponding "
        "to the Product Code cell: "
    )

    selected_index = int(selected_index)

    if selected_index < 0 or selected_index >= len(candidates):
        raise ValueError("Invalid candidate selection.")

    return candidates[selected_index]


# ============================================================
# START PRODUCT ENTRY
# ============================================================

def start_product_entry(grid):

    print("\nLocating Product Code cell...")

    cell = find_first_product_cell(grid)

    print("\nSelected cell:")
    print("Name:", cell.element_info.name)
    print("Rectangle:", cell.rectangle())

    input(
        "\nConfirm that this is the Product Code cell. "
        "Press Enter to click it..."
    )

    cell.click_input()

    time.sleep(DELAY)

    print("[OK] Product Code cell selected.")


# ============================================================
# PRODUCT ENTRY
# ============================================================

PRODUCT_FIELDS = [
    "product_code",
    "description",
    "quantity",
    "uom",
    "pre_close_quantity",
    "rate",
    "difference",
    "amount",
    "document_class",
    "document_class_description",
    "export_documentation",
    "additional_description",
    "expected_delivery_date"
]


def enter_products(products):

    print("\n========== PRODUCT ENTRY ==========")

    for row_number, product in enumerate(products, start=1):

        print(f"\nStarting product row {row_number}")

        for field_index, field in enumerate(PRODUCT_FIELDS):

            value = product.get(field, "")

            print(
                f"Entering {field}: {value}"
            )

            enter_text(value)

            # Move to next cell
            send_keys(NAVIGATION_KEY)

            time.sleep(DELAY)

        print(
            f"[OK] Product row {row_number} entered."
        )

    print("\nAll product rows processed.")


# ============================================================
# MAIN
# ============================================================

def main():

    print("====================================")
    print("       FACT ERP AUTOMATION")
    print("====================================")

    data = load_data()

    # Connect and bring ERP to foreground
    window = connect_window()

    input(
        "\nFACT ERP is now in front. "
        "Prepare the Sale Order form and press Enter..."
    )

    # Fill document header
    fill_header(
        window,
        data.get("header", {})
    )

    # Find product grid
    grid = find_product_grid(window)

    input(
        "\nHeader completed. Ensure the product grid "
        "is ready, then press Enter..."
    )

    # Select first product cell
    start_product_entry(grid)

    # Enter all products
    enter_products(
        data.get("products", [])
    )

    print("\n====================================")
    print("       AUTOMATION COMPLETED")
    print("====================================")

    print(
        "\nPlease verify the document before saving."
    )


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:
        print("\nAutomation interrupted by user.")

    except Exception as error:
        print("\nAUTOMATION FAILED:")
        print(error)

        raise
