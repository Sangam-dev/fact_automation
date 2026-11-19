"""Payload parsing for the first-pass Sale Order filler (no desktop needed)."""

from __future__ import annotations

import json
from pathlib import Path

from execution.forms.test_filler import cell_name, fmt, load_payload

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = ROOT / "config" / "forms" / "test_payload.json"


def test_payload_maps_onto_form_map_keys():
    header, lines = load_payload(PAYLOAD)
    assert header["customer_code"] == "CE-CUS2542"
    assert header["order_date"] == "2083/03/32"
    assert header["expected_delivery_date"] == ""
    assert [line["product_code"] for line in lines] == ["BRM-003", "BRAND- 14"]


def test_blank_rows_are_dropped(tmp_path):
    # The original export ended with an all-null row; it must not be typed.
    data = {
        "header": {},
        "lines": [
            {"product_code": "A", "quantity": 1},
            {"product_code": None, "quantity": 0},
            {"product_code": "   ", "quantity": 0},
        ],
    }
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    _, lines = load_payload(path)
    assert [line["product_code"] for line in lines] == ["A"]


def test_values_are_formatted_for_typing():
    assert fmt(1000.0) == "1000"
    assert fmt(29.5) == "29.5"
    assert fmt(None) == ""
    assert fmt(True) == "Y"
    assert fmt("R") == "R"


def test_cell_names_follow_the_uia_convention():
    # Index is zero-based while the visible Sl# starts at 1.
    assert cell_name("Quantity ", 0) == "Quantity  row 0"
    assert cell_name("Product Code", 2) == "Product Code row 2"
