"""The progress record and its two guards: the values digest and the save marker."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from core.automation import progress as store

FIXED = datetime(2026, 11, 19, 10, 15, 0)


def record(**overrides) -> store.ProgressState:
    data = {
        "run_id": "20261119_101500",
        "automation": "fact_automation",
        "values_digest": store.digest_values({"customer": "Ram"}),
    }
    data.update(overrides)
    return store.ProgressState.model_validate(data)


def test_digest_does_not_depend_on_key_order() -> None:
    one = store.digest_values({"customer": "Ram", "date": "2083/03/32"})
    two = store.digest_values({"date": "2083/03/32", "customer": "Ram"})
    assert one == two
    assert one.startswith("sha256:")


def test_digest_changes_with_a_value() -> None:
    assert store.digest_values({"customer": "Ram"}) != store.digest_values(
        {"customer": "Sita"}
    )


def test_a_new_record_starts_at_the_first_step() -> None:
    fresh = store.new_progress("fact_automation", {"customer": "Ram"}, when=FIXED)
    assert fresh.cursor == 0
    assert fresh.completed == []
    assert fresh.run_id == "20261119_101500"
    assert fresh.values_digest == store.digest_values({"customer": "Ram"})


def test_marking_a_step_moves_the_cursor_and_lists_it_once() -> None:
    item = record()
    item.mark_step(0, "01-open", FIXED)
    item.mark_step(0, "01-open", FIXED)
    assert item.cursor == 1
    assert item.completed == ["01-open"]


def test_the_cursor_never_walks_back_when_a_step_is_replayed() -> None:
    item = record(cursor=3)
    item.mark_step(1, "02-customer", FIXED)
    assert item.cursor == 3


def test_the_save_marker_blocks_the_automation_until_it_is_resolved() -> None:
    item = record()
    item.begin_save(when=FIXED)

    assert item.save_unresolved is True
    reason = item.block_reason()
    assert "20261119_101500" in reason
    assert "--saved-already" in reason and "--not-saved" in reason

    item.resolve_save(True, FIXED)
    assert item.save_unresolved is False
    assert item.outcome == store.OUTCOME_SAVED
    assert item.block_reason() == ""


def test_resolving_a_save_as_not_done_allows_a_retry() -> None:
    item = record()
    item.begin_save(when=FIXED)
    item.resolve_save(False, FIXED)

    assert item.pending_save is None
    assert item.outcome == ""
    assert item.save_unresolved is False


def test_a_saved_record_that_still_has_a_marker_is_not_blocked() -> None:
    item = record()
    item.begin_save(when=FIXED)
    item.outcome = store.OUTCOME_SAVED
    assert item.save_unresolved is False


def test_values_changed_only_compares_known_digests() -> None:
    item = record()
    assert item.values_changed(store.digest_values({"customer": "Sita"})) is True
    assert item.values_changed(store.digest_values({"customer": "Ram"})) is False
    assert record(values_digest="").values_changed("sha256:whatever") is False


def test_progress_round_trips_through_disk(tmp_path: Path) -> None:
    item = record(cursor=2)
    item.mark_step(1, "02-customer", FIXED)
    path = store.save_progress(item, tmp_path)

    loaded = store.load_progress("fact_automation", tmp_path)
    assert loaded is not None
    assert loaded.cursor == item.cursor
    assert loaded.completed == item.completed
    assert path == tmp_path / "fact_automation.json"
    assert list(tmp_path.glob("*.tmp")) == []


def test_loading_a_record_that_was_never_written_is_none(tmp_path: Path) -> None:
    assert store.load_progress("fact_automation", tmp_path) is None


def test_clearing_a_record_reports_whether_it_existed(tmp_path: Path) -> None:
    store.save_progress(record(), tmp_path)
    assert store.clear_progress("fact_automation", tmp_path) is True
    assert store.clear_progress("fact_automation", tmp_path) is False
    assert store.load_progress("fact_automation", tmp_path) is None


def test_broken_json_is_reported_with_the_path(tmp_path: Path) -> None:
    (tmp_path / "fact_automation.json").write_text("{ nope", encoding="utf-8")
    with pytest.raises(store.ProgressError, match="cannot read"):
        store.load_progress("fact_automation", tmp_path)


def test_an_unknown_schema_is_refused(tmp_path: Path) -> None:
    data = json.loads(record().model_dump_json(by_alias=True))
    data["schema"] = "automation-progress/99"
    (tmp_path / "fact_automation.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(store.ProgressError, match="not a valid progress record"):
        store.load_progress("fact_automation", tmp_path)


def test_a_negative_cursor_is_rejected() -> None:
    with pytest.raises(Exception):
        record(cursor=-1)
