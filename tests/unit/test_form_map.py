"""The sale-order form map must stay honest about the captured window."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from execution.forms.form_map import (
    DERIVED_KINDS,
    FormMapError,
    compile_file,
    dump_json,
    load_form_map,
)

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "config" / "forms" / "sale_order.source.json"
COMMITTED = ROOT / "config" / "forms" / "sale_order.json"
DUMPS = [
    ROOT / "scripts" / "uia" / "first_part.json",
    ROOT / "scripts" / "uia" / "second_part.json",
]
CAPTURES = {"first_part", "second_part"}


@pytest.fixture(scope="module")
def form():
    return compile_file(SOURCE, DUMPS)


def test_every_header_field_resolves_in_every_capture(form):
    assert form.header
    for spec in form.header:
        assert set(spec.resolved) == CAPTURES, spec.key
        for resolved in spec.resolved.values():
            assert resolved.path
            assert resolved.control_type


def test_writable_fields_have_an_editable_control(form):
    for spec in form.header:
        if spec.kind in DERIVED_KINDS:
            continue
        for capture, resolved in spec.resolved.items():
            assert resolved.writable, f"{spec.key} not writable in {capture}"


def test_application_derived_values_are_disabled_in_the_capture(form):
    derived = {spec.key for spec in form.header if spec.kind == "derived"}
    assert {"customer_name", "document_class_description", "exchange_rate"} <= derived
    for key in derived:
        enabled = {capture: r.enabled for capture, r in form.field(key).resolved.items()}
        assert enabled == dict.fromkeys(CAPTURES, False), key


def test_date_value_is_read_from_the_inner_edit(form):
    # deOa_date is a wrapper pane; the captured value sits on the nested Edit.
    resolved = form.field("order_date").resolved["first_part"]
    assert resolved.control_type == "Pane"
    assert resolved.inner_path
    assert resolved.value == "2083/03/32"


def test_grid_columns_are_matched_to_headers(form):
    grid = form.grid
    assert grid is not None
    assert len(grid.columns) == 13
    assert grid.row_index_base == 1
    for column in grid.columns:
        assert column.header_label, column.name
    assert grid.column("quantity").header_label == "Quantity "
    assert grid.column("Rate ").name == "Rate"


def test_grid_cells_are_virtualized_by_horizontal_scroll(form):
    cells = {column.name: set(column.resolved_cells) for column in form.grid.columns}
    # The two screenshots differ only by scroll position; that is why the map
    # records per-capture cell paths instead of one fixed path per column.
    assert cells["Quantity"] == {"first_part"}
    assert cells["UOM"] == {"first_part"}
    assert cells["Expected Delivery Date"] == {"second_part"}
    assert cells["Product Code"] == CAPTURES
    assert all(form.grid.virtualized.values())


def test_actions_are_resolved(form):
    resolved = form.actions.resolved
    assert set(resolved) == {"save_new", "save_close", "copy_button", "cancel"}
    for capture in CAPTURES:
        assert resolved["save_new"][capture].control_type == "Button"


def test_ambiguous_automation_id_is_rejected(tmp_path):
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    source["header"][1]["automation_id"] = "NepDateEdit"  # four matches in the tree
    broken = tmp_path / "broken.source.json"
    broken.write_text(json.dumps(source), encoding="utf-8")

    with pytest.raises(FormMapError, match="ambiguous"):
        compile_file(broken, DUMPS)


def test_unknown_control_is_rejected(tmp_path):
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    source["header"][1]["automation_id"] = "noSuchControl"
    broken = tmp_path / "broken.source.json"
    broken.write_text(json.dumps(source), encoding="utf-8")

    with pytest.raises(FormMapError, match="not in the captured window"):
        compile_file(broken, DUMPS)


def test_committed_map_round_trips_through_the_loader(form):
    loaded = load_form_map(COMMITTED)
    assert loaded == form
    assert loaded.field("order_date").resolved["first_part"].control_type == "Pane"


def test_committed_map_matches_the_source_and_dumps(form):
    assert dump_json(form) == COMMITTED.read_text(encoding="utf-8"), (
        "config/forms/sale_order.json is stale; run scripts/build_form_map.py"
    )
