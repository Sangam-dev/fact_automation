"""Unit tests for the interactive workflow wizard."""

from __future__ import annotations

from pathlib import Path

import pytest

from wizard.wizard import (
    _ask_field_names,
    _ask_sample_values,
    _clean_path,
    _config_data,
    _describe_step,
    _form_steps,
    _generate_config,
    _print_step_plan,
    _prompt,
    _prompt_yes_no,
    _review_step_plan,
)
from ai.planners.step_planner import PlannedFill, StepPlan
from core.config import load_config
from execution.actions.models import ClickStep, FillStep


def test_clean_path_removes_quotes_and_whitespace() -> None:
    assert _clean_path('  "C:/data/values.json"  ') == "C:/data/values.json"
    assert _clean_path("  'C:/data/values.json'  ") == "C:/data/values.json"
    assert _clean_path("C:/data/values.json") == "C:/data/values.json"


def test_prompt_uses_default_on_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert _prompt("Enter value", default="default_val") == "default_val"


def test_prompt_returns_user_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "my_value")
    assert _prompt("Enter value", default="default_val") == "my_value"


def test_prompt_yes_no(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert _prompt_yes_no("Proceed?") is True

    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert _prompt_yes_no("Proceed?") is False


def test_describe_step_names_the_work() -> None:
    assert _describe_step({"action": "click", "button": "Add New"}) == (
        "click 'Add New'"
    )
    assert _describe_step(
        {"action": "fill", "key": "Amount", "field": "Total"}
    ) == "fill 'Total' from field 'Amount'"


def test_form_steps_turn_a_click_plan_into_a_click_step() -> None:
    steps = _form_steps(StepPlan(kind="click", label="Add New"))

    assert steps == [ClickStep(action="click", button="Add New")]


def test_form_steps_turn_a_fill_plan_into_one_step_per_field() -> None:
    plan = StepPlan(
        kind="fill",
        fills=(
            PlannedFill(key="Invoice Number", label="Invoice No."),
            PlannedFill(key="Amount", label="Total"),
        ),
    )

    steps = _form_steps(plan)

    assert steps == [
        FillStep(action="fill", key="Invoice Number", field="Invoice No."),
        FillStep(action="fill", key="Amount", field="Total"),
    ]


def test_the_plan_screen_lists_the_actions_and_the_leftover_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = StepPlan(
        kind="fill",
        fills=(PlannedFill(key="Amount", label="Total", confidence=0.9),),
    )

    _print_step_plan(
        plan,
        index=2,
        instruction="fill the amount",
        screenshot=Path("shot.png"),
        field_names=["Amount", "Notes"],
        types={"Amount": "number", "Notes": "text"},
    )

    out = capsys.readouterr().out
    assert "Step 2: fill the amount" in out
    assert "fill 'Total' <- field 'Amount' (number, 0.90)" in out
    assert "Not filled by this step: Notes" in out


def test_review_accepts_the_step_on_enter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert _review_step_plan() == ("done", "")


def test_review_redoes_the_step(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "r")
    assert _review_step_plan() == ("redo", "")


def test_review_takes_a_correction_in_plain_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "use Total instead")
    assert _review_step_plan() == ("edit", "use Total instead")


def test_review_of_a_failed_step_defaults_to_redo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert _review_step_plan("it is not on screen") == ("redo", "")


def test_field_names_are_split_deduplicated_and_ordered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "builtins.input", lambda _: "Amount, Notes, Amount, Customer Name"
    )

    assert _ask_field_names() == ["Amount", "Notes", "Customer Name"]


def test_sample_values_keep_the_workflow_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(["500", "paid"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    assert _ask_sample_values(["Amount", "Notes"]) == {
        "Amount": "500",
        "Notes": "paid",
    }


def test_config_data_has_no_business_values() -> None:
    data = _config_data(
        "Clients",
        "clients.exe",
        "Save client",
        steps=[{"action": "click", "button": "Add New"}],
        fields=[{"name": "Amount", "field": "Total", "type": "number"}],
    )

    assert data["workflow"]["steps"] == [
        {"action": "click", "button": "Add New"}
    ]
    assert data["fields"] == [
        {"name": "Amount", "field": "Total", "type": "number"}
    ]
    assert "values" not in data
    assert "excel" not in data


def test_generate_config_writes_a_loadable_workflow(tmp_path: Path) -> None:
    path = tmp_path / "workflow.json"

    written = _generate_config(
        "Clients",
        None,
        "Save client",
        steps=[
            {"action": "click", "button": "Add New"},
            {"action": "fill", "key": "Amount"},
        ],
        fields=[{"name": "Amount", "field": "Total", "type": "number"}],
        path=path,
    )

    assert written == path
    config = load_config(path)
    assert config.app.title_contains == "Clients"
    assert config.actions.save_button == "Save client"
    assert config.workflow is not None
    assert len(config.workflow.steps) == 2
    assert config.fields[0].name == "Amount"
