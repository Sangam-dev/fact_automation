"""The automation registry: names, atomic saves, and readable failures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.automation import registry
from core.automation.model import Automation


def sample(name: str = "fact_automation") -> Automation:
    return Automation.model_validate(
        {
            "name": name,
            "app": {"title_contains": "FACT ERP.NG"},
            "fields": [{"name": "customer", "field": "Customer Name"}],
            "steps": [
                {
                    "id": "01-open",
                    "action": "click",
                    "button": "Add",
                    "checkpoint": {"state": "04_view", "resume": "replay"},
                }
            ],
        }
    )


@pytest.mark.parametrize("name", ["fact_automation", "so_2", "a1"])
def test_valid_names_are_returned_unchanged(name: str) -> None:
    assert registry.validate_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "   ", "FACT", "Fact_Automation", "has space", "../evil", "a", "x" * 41],
)
def test_names_that_cannot_be_a_directory_are_rejected(name: str) -> None:
    with pytest.raises(registry.AutomationNameError):
        registry.validate_name(name)


def test_name_errors_say_what_is_wrong() -> None:
    with pytest.raises(registry.AutomationNameError, match="lower case"):
        registry.validate_name("Fact_Automation")
    with pytest.raises(registry.AutomationNameError, match="2-40"):
        registry.validate_name("a")


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    path = registry.save(sample(), tmp_path)
    assert path == tmp_path / "fact_automation" / "automation.json"

    loaded = registry.load("fact_automation", tmp_path)
    assert loaded.name == "fact_automation"
    assert loaded.step_ids() == ["01-open"]
    assert loaded.created_at and loaded.updated_at


def test_the_saved_file_uses_the_documented_key_names(tmp_path: Path) -> None:
    path = registry.save(sample(), tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == "automation/1"
    assert data["steps"][0]["checkpoint"]["resume"] == "replay"
    assert "button" in data["steps"][0]


def test_saving_again_keeps_the_previous_version(tmp_path: Path) -> None:
    registry.save(sample(), tmp_path)
    changed = sample()
    changed.steps[0].button = "&Add"
    registry.save(changed, tmp_path)

    previous = tmp_path / "fact_automation" / registry.PREVIOUS_FILE
    assert previous.is_file()
    assert "Add" in previous.read_text(encoding="utf-8")
    assert registry.load("fact_automation", tmp_path).steps[0].button == "&Add"


def test_a_save_leaves_no_temporary_file(tmp_path: Path) -> None:
    registry.save(sample(), tmp_path)
    leftovers = list((tmp_path / "fact_automation").glob("*.tmp"))
    assert leftovers == []


def test_list_automations_is_sorted_and_ignores_junk(tmp_path: Path) -> None:
    registry.save(sample("second_one"), tmp_path)
    registry.save(sample("first_one"), tmp_path)
    (tmp_path / "not_an_automation").mkdir()
    (tmp_path / "empty_folder").mkdir()
    (tmp_path / "Bad Name").mkdir()

    assert registry.list_automations(tmp_path) == ["first_one", "second_one"]


def test_list_automations_on_a_missing_folder_is_empty(tmp_path: Path) -> None:
    assert registry.list_automations(tmp_path / "no_such_dir") == []


def test_loading_a_missing_automation_says_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(registry.AutomationNotFound, match="register"):
        registry.load("fact_automation", tmp_path)


def test_a_file_named_after_a_different_automation_is_refused(tmp_path: Path) -> None:
    registry.save(sample("other_name"), tmp_path)
    (tmp_path / "fact_automation").mkdir()
    (tmp_path / "fact_automation" / "automation.json").write_text(
        (tmp_path / "other_name" / "automation.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(registry.AutomationError, match="must agree"):
        registry.load("fact_automation", tmp_path)


def test_broken_json_is_reported_with_the_path(tmp_path: Path) -> None:
    folder = tmp_path / "fact_automation"
    folder.mkdir()
    (folder / "automation.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(registry.AutomationError, match="cannot read"):
        registry.load("fact_automation", tmp_path)


def test_a_structurally_invalid_automation_is_reported_with_the_path(
    tmp_path: Path,
) -> None:
    data = json.loads(sample().model_dump_json(by_alias=True))
    data["steps"] = []
    folder = tmp_path / "fact_automation"
    folder.mkdir()
    (folder / "automation.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(registry.AutomationError, match="not a valid automation"):
        registry.load("fact_automation", tmp_path)


def test_exists_only_answers_for_real_files(tmp_path: Path) -> None:
    assert registry.exists("fact_automation", tmp_path) is False
    registry.save(sample(), tmp_path)
    assert registry.exists("fact_automation", tmp_path) is True
    assert registry.exists("Bad Name", tmp_path) is False
