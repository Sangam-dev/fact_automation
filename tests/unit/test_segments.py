"""Designed segments: the generated library, and appending one to a plan."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.automation import registry
from core.automation.model import Automation
from core.automation.segments import (
    SEGMENT_NOTE,
    Segment,
    SegmentError,
    SegmentTable,
    append_segment,
    append_segment_to,
    library_summary,
    load_segments,
    segment_table_path,
)
from desktop.state.states import load_state_table

NAME = "fact_automation"


def automation(**overrides: Any) -> Automation:
    data = {
        "name": NAME,
        "app": {"title_contains": "FACT ERP.NG", "process_name": "FACTERPNG.exe"},
        "fields": [{"name": "customer", "field": "Customer Name"}],
        "steps": [
            {
                "id": "01-open",
                "action": "click",
                "button": "&Add",
                "checkpoint": {"state": "04_view", "resume": "replay"},
            }
        ],
        "save": {"button": "Save", "verifier": "row", "footprint": ["customer"]},
    }
    data.update(overrides)
    return Automation.model_validate(data)


def segment(**overrides: Any) -> Segment:
    data = {
        "id": "sale_order.header",
        "title": "Sale Order header",
        "fields": [{"name": "customer", "field": "Customer Name"}],
        "steps": [
            {
                "id": "01-fill-customer",
                "action": "fill",
                "key": "customer",
                "field": "Customer Name",
                "checkpoint": {"state": "05_add", "resume": "restart"},
            }
        ],
    }
    data.update(overrides)
    return Segment.model_validate(data)


# ------------------------------------------------------------------ the model --
def test_a_segment_must_declare_the_fields_it_fills() -> None:
    with pytest.raises(Exception, match="does not declare"):
        Segment.model_validate(
            {
                "id": "x",
                "fields": [],
                "steps": [
                    {
                        "id": "01-a",
                        "action": "fill",
                        "key": "customer",
                        "checkpoint": {"state": "05_add", "resume": "restart"},
                    }
                ],
            }
        )


def test_segment_step_ids_must_be_unique() -> None:
    with pytest.raises(Exception, match="used twice"):
        Segment.model_validate(
            {
                "id": "x",
                "fields": [{"name": "c", "field": "C"}],
                "steps": [
                    {
                        "id": "01-a",
                        "action": "fill",
                        "key": "c",
                        "checkpoint": {"state": "05_add", "resume": "restart"},
                    },
                    {
                        "id": "01-a",
                        "action": "fill",
                        "key": "c",
                        "checkpoint": {"state": "05_add", "resume": "restart"},
                    },
                ],
            }
        )


def test_a_segment_needs_at_least_one_step_and_an_id() -> None:
    with pytest.raises(Exception):
        Segment.model_validate({"id": "x", "steps": []})
    with pytest.raises(Exception, match="needs an id"):
        Segment.model_validate(
            {
                "id": "  ",
                "steps": [
                    {
                        "id": "01-a",
                        "action": "click",
                        "button": "Add",
                        "checkpoint": {"state": "04_view", "resume": "replay"},
                    }
                ],
            }
        )


def test_segment_ids_must_be_unique_in_the_library() -> None:
    with pytest.raises(Exception, match="declared twice"):
        SegmentTable.model_validate({"segments": [segment().model_dump(), segment().model_dump()]})


def test_an_unknown_library_schema_is_refused() -> None:
    with pytest.raises(Exception, match="schema"):
        SegmentTable.model_validate({"schema": "segments/9", "segments": []})


def test_an_unknown_segment_name_lists_what_exists() -> None:
    table = SegmentTable(segments=[segment()])
    with pytest.raises(SegmentError, match="sale_order.header"):
        table.get("nope")


# ------------------------------------------------------------------- loading --
def test_the_shipped_library_is_usable() -> None:
    table = load_segments()
    assert table.segments, "config/segments/fact_segments.json is empty"
    assert "sale_order.header" in table.ids()


def test_the_shipped_header_segment_matches_the_learned_screens() -> None:
    table = load_segments()
    header = table.get("sale_order.header")
    known = load_state_table().ids()

    header.validate_states(known)
    assert header.state_ids() == ["05_add"]
    assert [step.action for step in header.steps] == ["fill"] * len(header.steps)
    assert [step.checkpoint.resume for step in header.steps] == [
        "restart"
    ] * len(header.steps)
    assert header.steps[0].key == "document_scheme"
    assert len(header.steps) >= 8


def test_the_shipped_segment_uses_the_labels_from_the_form_map() -> None:
    from execution.forms.form_map import load_form_map

    form = load_form_map("config/forms/sale_order.json")
    header = load_segments().get("sale_order.header")
    for step in header.steps:
        assert step.field == form.field(step.key).label


def test_a_missing_library_says_how_to_build_it(tmp_path: Path) -> None:
    with pytest.raises(SegmentError, match="build_segments"):
        load_segments(tmp_path / "nope.json")


def test_a_broken_library_is_reported_with_its_path(tmp_path: Path) -> None:
    path = tmp_path / "segments.json"
    path.write_text("{ nope", encoding="utf-8")
    with pytest.raises(SegmentError, match="cannot read"):
        load_segments(path)


def test_the_library_path_is_where_the_builder_writes() -> None:
    assert segment_table_path().is_file()
    assert segment_table_path().name == "fact_segments.json"


def test_library_summary_names_steps_and_screens() -> None:
    lines = library_summary(SegmentTable(segments=[segment()]))
    assert lines[0].startswith("sale_order.header (1 step(s) on 05_add)")


# ------------------------------------------------------------------ appending --
def test_appending_signs_the_new_steps_with_the_segment() -> None:
    plan = append_segment(automation(), segment())

    assert len(plan.steps) == 2
    appended = plan.steps[-1]
    assert appended.id == "header-01-fill-customer"
    assert appended.checkpoint.note == f"{SEGMENT_NOTE}sale_order.header"
    assert appended.checkpoint.state == "05_add"


def test_appending_does_not_touch_the_original_plan() -> None:
    original = automation()
    append_segment(original, segment())
    assert len(original.steps) == 1
    assert original.fields == automation().fields


def test_appending_merges_the_fields_once() -> None:
    plan = append_segment(
        automation(),
        segment(
            fields=[
                {"name": "customer", "field": "Customer Name"},
                {"name": "order_date", "field": "Date"},
            ],
            steps=[
                {
                    "id": "01-fill-customer",
                    "action": "fill",
                    "key": "customer",
                    "field": "Customer Name",
                    "checkpoint": {"state": "05_add", "resume": "restart"},
                },
                {
                    "id": "02-fill-date",
                    "action": "fill",
                    "key": "order_date",
                    "field": "Date",
                    "checkpoint": {"state": "05_add", "resume": "restart"},
                },
            ],
        ),
    )

    assert [field.name for field in plan.fields] == ["customer", "order_date"]


def test_a_field_mapped_to_two_controls_is_refused() -> None:
    with pytest.raises(SegmentError, match="already mapped"):
        append_segment(
            automation(),
            segment(fields=[{"name": "customer", "field": "Buyer"}]),
        )


def test_appending_the_same_segment_twice_is_refused() -> None:
    once = append_segment(automation(), segment())
    with pytest.raises(SegmentError, match="already contains"):
        append_segment(once, segment())


def test_a_segment_on_an_unknown_screen_is_refused() -> None:
    with pytest.raises(SegmentError, match="state table"):
        append_segment(automation(), segment(), known_states=["04_view"])


def test_appending_marks_the_plan_unproved_but_keeps_its_history() -> None:
    plan = automation()
    plan.verified = True
    plan.touch()
    created = plan.created_at

    updated = append_segment(plan, segment())

    assert updated.verified is False
    assert updated.created_at == created
    assert updated.name == plan.name
    assert updated.save == plan.save


def test_the_appended_plan_still_passes_model_validation() -> None:
    plan = append_segment(automation(), segment())
    assert plan.step_ids() == ["01-open", "header-01-fill-customer"]
    assert plan.fill_keys() == ["customer"]


# ------------------------------------------------------------ appending to disk --
def test_appending_writes_the_automation_and_keeps_the_previous_one(
    tmp_path: Path,
) -> None:
    registry.save(automation(), tmp_path)

    result = append_segment_to(
        NAME,
        "sale_order.header",
        automations_root=tmp_path,
        segments=SegmentTable(segments=[segment()]),
    )
    assert result.ok, result.message
    assert result.steps_added == 1
    assert result.path is not None and result.path.is_file()
    assert registry.load(NAME, tmp_path).step_ids()[-1] == "header-01-fill-customer"

    again = append_segment_to(
        NAME,
        "sale_order.header",
        automations_root=tmp_path,
        segments=SegmentTable(segments=[segment()]),
    )
    assert again.status == "refused"
    assert (tmp_path / NAME / registry.PREVIOUS_FILE).is_file()


def test_a_dry_run_does_not_write(tmp_path: Path) -> None:
    registry.save(automation(), tmp_path)
    before = (tmp_path / NAME / "automation.json").read_text(encoding="utf-8")

    result = append_segment_to(
        NAME,
        "sale_order.header",
        automations_root=tmp_path,
        segments=SegmentTable(segments=[segment()]),
        write=False,
    )
    assert result.status == "drafted"
    assert (
        tmp_path / NAME / "automation.json"
    ).read_text(encoding="utf-8") == before


def test_an_unknown_automation_is_refused(tmp_path: Path) -> None:
    result = append_segment_to(
        NAME, "sale_order.header", automations_root=tmp_path
    )
    assert result.status == "refused"
    assert "register" in result.message


def test_an_unknown_segment_is_refused(tmp_path: Path) -> None:
    registry.save(automation(), tmp_path)
    result = append_segment_to(
        NAME,
        "nope",
        automations_root=tmp_path,
        segments=SegmentTable(segments=[segment()]),
    )
    assert result.status == "refused"
    assert "sale_order.header" in result.message


def test_the_shipped_header_segment_appends_to_a_real_plan(tmp_path: Path) -> None:
    registry.save(automation(), tmp_path)
    result = append_segment_to(
        NAME,
        "sale_order.header",
        automations_root=tmp_path,
        known_states=load_state_table().ids(),
    )

    assert result.ok, result.message
    assert result.automation is not None
    assert len(result.automation.steps) == 1 + result.steps_added
    assert json.loads(result.path.read_text(encoding="utf-8"))["verified"] is False
