"""The editable Sale Order plan must stay aligned with the form map and payload."""

from __future__ import annotations

import json
from pathlib import Path

from execution.forms.form_map import load_form_map
from execution.forms.sale_order_plan import SALE_ORDER

ROOT = Path(__file__).resolve().parents[2]
FORM_MAP = ROOT / "config" / "forms" / "sale_order.json"
PAYLOAD = ROOT / "config" / "forms" / "test_payload.json"


def test_header_steps_exist_in_the_form_map():
    form = load_form_map(FORM_MAP)
    for step in SALE_ORDER.header:
        field = form.field(step.key)  # raises if the key is unknown
        assert field.automation_id


def test_proofs_are_read_only_fields():
    form = load_form_map(FORM_MAP)
    for step in SALE_ORDER.header:
        if step.proof:
            assert form.field(step.proof).derived, step.proof


def test_line_columns_exist_and_follow_grid_order():
    form = load_form_map(FORM_MAP)
    grid_order = [column.name for column in form.grid.columns]
    positions = [grid_order.index(step.column) for step in SALE_ORDER.columns]
    assert positions == sorted(positions), "row steps must follow the grid's tab order"
    assert all(step.column in grid_order for step in SALE_ORDER.columns)


def test_readback_columns_exist():
    form = load_form_map(FORM_MAP)
    names = {column.name for column in form.grid.columns}
    for column in SALE_ORDER.readback:
        assert column in names


def test_payload_only_uses_planned_keys():
    payload = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    header_keys = {step.key for step in SALE_ORDER.header}
    column_keys = {step.key for step in SALE_ORDER.columns}
    assert set(payload["header"]) <= header_keys
    for line in payload["lines"]:
        assert set(line) <= column_keys


def test_required_header_fields_have_values():
    payload = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    for step in SALE_ORDER.header:
        if step.required and step.key != "expected_delivery_date":
            assert payload["header"].get(step.key), step.key
