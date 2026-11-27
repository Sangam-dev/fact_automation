"""Unit tests for the interactive workflow wizard."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import wizard.wizard as wizard_module
from wizard.wizard import (
    _ask_field_names,
    _ask_sample_values,
    _attach_fact,
    _clean_path,
    _config_data,
    _describe_step,
    _form_steps,
    _generate_config,
    _print_step_plan,
    _prompt,
    _prompt_yes_no,
    _review_step_plan,
    _segment_steps,
    _take_save_step,
)
from ai.planners.step_planner import PlannedFill, StepPlan
from core.config import load_config
from desktop.state.fact import FactError
from desktop.state.recognizer import StateMatch, WindowView
from execution.actions.models import ClickStep, FillStep, ProgressWait


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
        {
            "action": "click",
            "button": "&Add",
            "wait_for_progress": {
                "automation_id": "pgbar",
                "target_percent": 100,
            },
        }
    ) == "click '&Add' after progress bar 'pgbar' reaches 100%"
    assert _describe_step(
        {"action": "fill", "key": "Amount", "field": "Total"}
    ) == "fill 'Total' from field 'Amount'"


def test_form_steps_turn_a_click_plan_into_a_click_step() -> None:
    steps = _form_steps(StepPlan(kind="click", label="Add New"))

    assert steps == [ClickStep(action="click", button="Add New")]


def test_form_steps_keep_a_clicks_progress_precondition() -> None:
    wait = ProgressWait(automation_id="pgbar", stable_reads=2)
    steps = _form_steps(
        StepPlan(kind="click", label="&Add", wait_for_progress=wait)
    )

    assert steps == [
        ClickStep(action="click", button="&Add", wait_for_progress=wait)
    ]


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


def test_the_plan_screen_shows_a_clicks_progress_precondition(
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = StepPlan(
        kind="click",
        label="&Add",
        wait_for_progress=ProgressWait(automation_id="pgbar", stable_reads=2),
    )

    _print_step_plan(
        plan,
        index=4,
        instruction="click Add after the blue bar reaches 100 percent",
        screenshot=Path("shot.png"),
        field_names=[],
        types={},
    )

    out = capsys.readouterr().out
    assert "click '&Add'" in out
    assert "after progress bar 'pgbar' reaches 100%" in out
    assert "2 stable reads" in out


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


def test_field_names_can_be_skipped_with_enter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "")

    assert _ask_field_names() == []


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


def test_the_written_config_records_the_model_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Asking for a model in .env has to reach the file the wizard writes, which
    # is what the proof and every later run read.
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

    path = _generate_config(
        "Clients",
        None,
        "Save client",
        steps=[{"action": "click", "button": "Add New"}],
        fields=[],
        path=tmp_path / "workflow.json",
    )

    assert json.loads(path.read_text(encoding="utf-8"))["gemini"]["model"] == (
        "gemini-3.5-flash-lite"
    )


def test_generate_config_keeps_progress_wait_on_its_click(tmp_path: Path) -> None:
    path = _generate_config(
        "FACT ERP.NG",
        "FACTERPNG.exe",
        "Save",
        steps=[
            {
                "action": "click",
                "button": "&Add",
                "wait_for_progress": {
                    "automation_id": "pgbar",
                    "target_percent": 100,
                    "timeout_seconds": 120,
                    "stable_reads": 2,
                },
            }
        ],
        fields=[],
        path=tmp_path / "workflow.json",
    )

    loaded = load_config(path)
    click = loaded.workflow.steps[0]
    assert isinstance(click, ClickStep)
    assert click.wait_for_progress is not None
    assert click.wait_for_progress.automation_id == "pgbar"
    assert click.wait_for_progress.stable_reads == 2


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


# ------------------------------------------------------------- the save step --
def test_the_plans_save_click_becomes_the_save_action() -> None:
    button, steps, dropped = _take_save_step(
        [
            {"action": "click", "button": "&Add"},
            {"action": "fill", "key": "customer"},
            {"action": "click", "button": "Save & Close"},
        ]
    )

    assert dropped is True
    assert button == "Save & Close"
    assert steps == [
        {"action": "click", "button": "&Add"},
        {"action": "fill", "key": "customer"},
    ]


def test_a_plan_without_a_save_click_keeps_its_steps() -> None:
    steps = [{"action": "click", "button": "&Add"}]
    button, kept, dropped = _take_save_step(list(steps))

    assert dropped is False
    assert button == "Save"
    assert kept == steps


def test_a_last_fill_is_not_mistaken_for_the_save() -> None:
    button, steps, dropped = _take_save_step([{"action": "fill", "key": "customer"}])

    assert (button, dropped) == ("Save", False)
    assert steps == [{"action": "fill", "key": "customer"}]


def test_an_empty_plan_asks_for_nothing() -> None:
    assert _take_save_step([]) == ("Save", [], False)


# ---------------------------------------------------------- FACT-only attach --
def _fact_window() -> SimpleNamespace:
    return SimpleNamespace(
        handle=7,
        title="FACT ERP.NG",
        process_name="FACTERPNG.exe",
    )


def test_the_wizard_attaches_to_fact_without_listing_windows(monkeypatch) -> None:
    match = StateMatch(
        state_id="03_entry",
        window=WindowView(title="FACT ERP.NG", handle=7),
    )
    seen: list[float] = []

    def fake_wait_for_screen(*args, **kwargs):
        seen.append(kwargs.get("seconds", 0.0))
        return _fact_window(), match

    monkeypatch.setattr(wizard_module, "wait_for_screen", fake_wait_for_screen)
    monkeypatch.setattr(
        "desktop.windows.window.list_windows",
        lambda: pytest.fail("the wizard must not list the desktop's windows"),
    )

    assert _attach_fact() == ("", "FACTERPNG.exe")
    assert seen


def test_the_wizard_says_what_to_do_when_fact_is_not_ready(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise FactError("FACTERPNG.exe is not running")

    monkeypatch.setattr(wizard_module, "wait_for_screen", fail)

    assert _attach_fact() is None


# --------------------------------------------------- one ordered plan of steps --


def _segment() -> SimpleNamespace:
    """The two things a designed segment has to survive: fields and order."""
    from core.automation.model import AutomationStep

    def step(**data) -> AutomationStep:
        data.setdefault("checkpoint", {"state": "05_add", "resume": "restart"})
        return AutomationStep.model_validate(data)

    return SimpleNamespace(
        id="sale_order.header",
        fields=[
            SimpleNamespace(
                name="customer", field="Customer Name", type="text"
            )
        ],
        steps=[
            step(id="cust", action="fill", key="customer", field="Customer Name"),
            step(
                id="fill-it",
                action="run",
                script="execution/scripts/form_filler_1.py",
                args=["{values_file}"],
            ),
        ],
    )


def test_segment_click_keeps_its_progress_wait() -> None:
    from core.automation.model import AutomationStep

    step = AutomationStep.model_validate(
        {
            "id": "wait-add",
            "action": "click",
            "button": "&Add",
            "wait_for_progress": {
                "automation_id": "pgbar",
                "target_percent": 100,
                "timeout_seconds": 120,
                "stable_reads": 2,
            },
            "checkpoint": {"state": "04_view", "resume": "replay"},
        }
    )

    assert _segment_steps(SimpleNamespace(steps=[step])) == [
        {
            "action": "click",
            "optional": False,
            "button": "&Add",
            "wait_for_progress": {
                "automation_id": "pgbar",
                "target_percent": 100.0,
                "timeout_seconds": 120.0,
                "stable_reads": 2,
            },
        }
    ]


def test_a_chosen_segment_lands_where_it_was_chosen_in_the_plan() -> None:
    plan = [{"action": "click", "button": "&Add"}]
    plan.extend(_segment_steps(_segment()))
    plan.append({"action": "click", "button": "Freight"})

    assert [entry["action"] for entry in plan] == [
        "click",
        "fill",
        "run",
        "click",
    ], "steps added after a segment have to come after it"
    assert plan[1] == {"action": "fill", "key": "customer", "field": "Customer Name", "optional": False}
    assert plan[2]["script"] == "execution/scripts/form_filler_1.py"
    assert plan[2]["args"] == ["{values_file}"]
    assert plan[2]["timeout_seconds"] == 900.0


def test_a_step_that_runs_a_script_says_so() -> None:
    assert _describe_step(
        {"action": "run", "script": "execution/scripts/form_filler_1.py"}
    ) == "run the script 'execution/scripts/form_filler_1.py'"


def test_a_written_workflow_can_carry_the_script_step(tmp_path: Path) -> None:
    path = tmp_path / "workflow.json"

    written = _generate_config(
        "FACT ERP.NG",
        "FACTERPNG.exe",
        "Save",
        steps=[
            {"action": "click", "button": "&Add"},
            {
                "action": "run",
                "script": "execution/scripts/form_filler_1.py",
                "args": ["{values_file}"],
            },
        ],
        fields=[],
        path=path,
    )

    config = load_config(str(written))

    assert [step.action for step in config.workflow.steps] == ["click", "run"]
    assert config.workflow.steps[1].timeout_seconds == 900.0

