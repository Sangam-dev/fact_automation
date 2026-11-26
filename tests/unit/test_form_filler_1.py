from __future__ import annotations

import json
from pathlib import Path

from execution.scripts import form_filler_1


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_PATH = PROJECT_ROOT / "execution" / "scripts" / "sample.json"


def test_updated_sample_payload_normalizes_and_validates() -> None:
    sample = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))

    header, lines, errors = form_filler_1.check(sample)

    assert errors == []
    assert header["proforma_no"] == "TEST"
    assert header["customer_order_no"] == "2222"
    assert header["customer_code"] == "CE-1008"
    assert header["remarks"] == "hello world"
    assert len(lines) == 3
    assert [line["unitcode"] for line in lines] == ["BAG", "MT", "MT"]


def test_flat_sample_aliases_and_items_are_normalized() -> None:
    payload = {
        "quotation_no": "TEST",
        "order_no": "2222",
        "order_date": "2024-01-01",
        "due_date": "2024-02-01",
        "currency": "NPR",
        "customer_id": "CE-CUS2542",
        "salesman_id": "1534",
        "items": [
            {
                "product_code": "BRM-003",
                "quantity": 2,
                "unitcode": "BAG",
                "rate": 5,
            }
        ],
    }

    header, lines, errors = form_filler_1.check(payload)

    assert errors == []
    assert header["proforma_no"] == "TEST"
    assert header["customer_order_no"] == "2222"
    assert header["expected_delivery_date"] == "2024-02-01"
    assert header["customer_code"] == "CE-CUS2542"
    assert header["salesman_code"] == "1534"
    assert len(lines) == 1
    assert lines[0]["unitcode"] == "BAG"


def test_canonical_header_and_lines_payload_is_still_supported() -> None:
    payload = {
        "header": {"proforma_no": "TEST", "currency": "NPR"},
        "lines": [
            {
                "product_code": "BRM-003",
                "quantity": 2,
                "rate": 5,
                "unitcode": "BAG",
            }
        ],
    }

    header, lines, errors = form_filler_1.check(payload)

    assert errors == []
    assert header == payload["header"]
    assert lines == payload["lines"]


def test_unknown_unit_code_is_rejected_before_filling() -> None:
    payload = {
        "header": {},
        "lines": [
            {"product_code": "BRM-003", "quantity": 2, "unitcode": "BOX"}
        ],
    }

    _, _, errors = form_filler_1.check(payload)

    assert any("unitcode must be one of" in error for error in errors)


def test_uom_label_extracts_selected_unit_not_conversion_text() -> None:
    assert form_filler_1.uom_label("2. MT    (1 MT = 20.0000 Bags)") == "MT"
    assert form_filler_1.uom_label("1. Bags (1 Bags = 1.0000 Bags)") == "Bags"
