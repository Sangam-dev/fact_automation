from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from execution.scripts import udf_filler
from execution.scripts.udf_filler import Stop, load_udf_values, read_udf_values


def _payload(*, lines=None):
    return {
        "deliveryContact": "John Doe",
        "deliveryContactNumber": "9821522626",
        "deliveryAddress": "Kathmandu",
        "order_remarks": "deposit 40000",
        "paymentMethod": "CASH",
        "plant": "BHAIRAHAWA",
        "lines": lines or [
            {"product_code": "FG-0001", "quantity": 20, "unitcode": "BAG"}
        ],
    }


def test_maps_payload_to_first_page_and_highest_quantity_sms_entry() -> None:
    payload = _payload(lines=[
        {"product_code": "FG-0001", "quantity": 10, "unitcode": "BAG"},
        {"product_code": "FG-0003", "quantity": 25, "unitcode": "MT"},
    ])

    text_values, dropdown_values, sms_entry = read_udf_values(payload)

    assert text_values == {
        "F1": ".",
        "F2": "John Doe",
        "F3": "9821522626",
        "F4": "Kathmandu",
        "F7": "deposit 40000",
        "F16": ".",
    }
    assert dropdown_values == {
        "F19": "Against Cash",
        "F18": "Bhairahawa Plant",
    }
    assert sms_entry == {
        "product_code": "FG-0003",
        "description": "Jagdamba PPC Cement(T)",
        "quantity": "25",
        "uom": "MT",
    }


def test_sms_quantity_tie_uses_first_line_in_json_order() -> None:
    _, _, sms_entry = read_udf_values(_payload(lines=[
        {"product_code": "FG-0001", "quantity": "50.0", "unitcode": "BAG"},
        {"product_code": "FG-0002", "quantity": "50", "unitcode": "MT"},
    ]))

    assert sms_entry["product_code"] == "FG-0001"
    assert sms_entry["quantity"] == "50"
    assert sms_entry["uom"] == "Bags"


def test_missing_text_values_use_dot_but_dropdowns_and_lines_are_required() -> None:
    text_values, dropdown_values, sms_entry = read_udf_values(
        {"paymentMethod": "CASH", "plant": "SUNWOL", "lines": [
            {"product_code": "FG-0001", "quantity": 3, "unitcode": "BAG"}
        ]}
    )

    assert set(text_values.values()) == {"."}
    assert dropdown_values == {"F19": "Against Cash", "F18": "Sunwol Plant"}
    assert sms_entry["uom"] == "Bags"

    with pytest.raises(Stop, match="missing required dropdown value 'paymentMethod'"):
        read_udf_values({"plant": "SUNWOL", "lines": [
            {"product_code": "FG-0001", "quantity": 3, "unitcode": "BAG"}
        ]})
    with pytest.raises(Stop, match="non-empty list for Entry For SMS"):
        read_udf_values({"paymentMethod": "CASH", "plant": "SUNWOL"})


def test_rejects_unknown_dropdown_values_invalid_root_and_unmapped_sms_line() -> None:
    with pytest.raises(Stop, match="unrecognized paymentMethod"):
        read_udf_values({"paymentMethod": "SELF", "plant": "BHAIRAHAWA", "lines": [
            {"product_code": "FG-0001", "quantity": 3, "unitcode": "BAG"}
        ]})
    with pytest.raises(Stop, match="JSON root must be an object"):
        read_udf_values([])
    with pytest.raises(Stop, match="unsupported SMS product_code"):
        read_udf_values(_payload(lines=[
            {"product_code": "UNKNOWN", "quantity": 3, "unitcode": "BAG"}
        ]))
    with pytest.raises(Stop, match="unsupported SMS unitcode"):
        read_udf_values(_payload(lines=[
            {"product_code": "FG-0001", "quantity": 3, "unitcode": "EA"}
        ]))


def test_loads_and_validates_inline_json(tmp_path) -> None:
    values_path = tmp_path / "values.json"
    values_path.write_text(
        '{"paymentMethod":"CASH","plant":"SUNWOL","lines":['
        '{"product_code":"FG-0001","quantity":3,"unitcode":"BAG"}]}',
        encoding="utf-8",
    )

    text_values, dropdown_values, sms_entry = load_udf_values(values_path)

    assert text_values["F7"] == "."
    assert dropdown_values["F19"] == "Against Cash"
    assert sms_entry["quantity"] == "3"


def test_clicks_the_observed_global_udf_button(monkeypatch) -> None:
    control = SimpleNamespace(
        element_info=SimpleNamespace(automation_id="btnUDF", name=""),
        is_visible=lambda: True,
        is_enabled=lambda: True,
    )
    form = SimpleNamespace(descendants=lambda: [control])
    clicked = []
    monkeypatch.setattr(
        udf_filler,
        "agent_click",
        lambda target, owner, where: clicked.append((target, owner, where)),
    )

    udf_filler._click_udf_opener(form)

    assert clicked == [(control, form, "opening Global UDF")]


def test_grid_match_ignores_filter_cell_with_same_text() -> None:
    filter_cell = SimpleNamespace(
        window_text=lambda: "Product Description row -999997",
        iface_value=SimpleNamespace(CurrentValue="Jagdamba Ultra Premium OPC 43 Grade(T)"),
    )
    result_cell = SimpleNamespace(
        window_text=lambda: "Product Description row 0",
        iface_value=SimpleNamespace(CurrentValue="Jagdamba Ultra Premium OPC 43 Grade(T)"),
    )
    grid = SimpleNamespace(descendants=lambda **_: [filter_cell, result_cell])

    assert udf_filler._find_grid_match(
        grid, "Jagdamba Ultra Premium OPC 43 Grade(T)"
    ) is result_cell


def test_clicks_udf_ok_by_id_without_assuming_button_control_type(monkeypatch) -> None:
    state = {"visible": True}
    ok = SimpleNamespace(
        element_info=SimpleNamespace(automation_id="btnSave", control_type="Custom"),
        is_visible=lambda: True,
        is_enabled=lambda: True,
    )
    dialog = SimpleNamespace(
        handle=789,
        descendants=lambda: [ok],
        is_visible=lambda: state["visible"],
    )
    clicked = []
    monkeypatch.setattr(udf_filler, "_top_windows", lambda: [])

    def click(control, owner, where):
        clicked.append((control, owner, where))
        state["visible"] = False

    monkeypatch.setattr(udf_filler, "agent_click", click)

    udf_filler._click_udf_ok(dialog)

    assert clicked == [(ok, dialog, "confirming Global UDF details")]


def test_confirms_picker_by_id_without_assuming_button_control_type(monkeypatch) -> None:
    state = {"visible": True}
    ok = SimpleNamespace(
        element_info=SimpleNamespace(automation_id="btnMok", control_type="Custom"),
        is_visible=lambda: True,
        is_enabled=lambda: True,
    )
    picker = SimpleNamespace(
        descendants=lambda: [ok],
        is_visible=lambda: state["visible"],
    )
    clicked = []

    def click(control, owner, where):
        clicked.append((control, owner, where))
        state["visible"] = False

    monkeypatch.setattr(udf_filler, "agent_click", click)

    udf_filler._confirm_picker(picker)

    assert clicked == [(ok, picker, "confirming picker selection")]


def test_finds_picker_through_native_window_handle(monkeypatch) -> None:
    picker = SimpleNamespace(
        element_info=SimpleNamespace(automation_id="frmHelp"),
        is_visible=lambda: True,
    )
    desktop = SimpleNamespace(
        window=lambda *, handle: SimpleNamespace(wrapper_object=lambda: picker)
    )
    monkeypatch.setattr(
        udf_filler,
        "_top_windows",
        lambda: [(456, "Item Ordered(SMS)", "WindowsForms", 500, 300)],
    )
    monkeypatch.setattr(udf_filler, "_window_process_id", lambda _hwnd: 123)

    assert udf_filler._find_picker(desktop, "Item Ordered(SMS)", 123) is picker


def test_fills_first_page_then_sms_then_confirms_only_udf_dialog(monkeypatch, capsys) -> None:
    events = []
    form = SimpleNamespace(
        is_enabled=lambda: True,
        element_info=SimpleNamespace(process_id=123),
    )
    dialog = SimpleNamespace(is_visible=lambda: True)
    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Desktop=lambda **_: object()))
    monkeypatch.setattr(udf_filler, "_find_sale_order", lambda _: form)
    monkeypatch.setattr(udf_filler, "_focus_sale_order", lambda _desktop, target: target)
    monkeypatch.setattr(udf_filler, "_click_udf_opener", lambda _form: events.append("open"))
    monkeypatch.setattr(udf_filler, "_find_udf_dialog", lambda _desktop, pid: dialog)
    monkeypatch.setattr(
        udf_filler,
        "_fill_text_field",
        lambda _dialog, field_id, _value: events.append(("text", field_id)),
    )
    monkeypatch.setattr(
        udf_filler,
        "_select_dropdown",
        lambda _dialog, field_id, _value: events.append(("dropdown", field_id)),
    )
    monkeypatch.setattr(
        udf_filler,
        "_fill_sms_entry",
        lambda _desktop, _dialog, entry: events.append(("sms", entry["product_code"])),
    )
    monkeypatch.setattr(
        udf_filler,
        "_click_udf_ok",
        lambda _dialog: events.append("udf-ok"),
    )

    udf_filler.fill_udf_page(
        {"F1": "."},
        {"F19": "Against Cash"},
        {"product_code": "FG-0001"},
    )

    assert events == [
        "open",
        ("text", "F1"),
        ("dropdown", "F19"),
        ("sms", "FG-0001"),
        "udf-ok",
    ]
    assert "UDF dialog confirmed" in capsys.readouterr().out
