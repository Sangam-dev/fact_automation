"""Registration: a proven workflow becomes an automation with real checkpoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.automation import registry
from core.automation.model import Automation
from core.config import AgentConfig
from desktop.state.recognizer import CLOSED, POPUP, StateMatch
from desktop.state.states import StateTable
from execution.actions.engine import PlanResult
from execution.verification.confirmation import CONFIRMED
from execution.workflows.runner import SaveResult
from wizard.register import (
    DRAFTED,
    FAILED,
    REFUSED,
    REGISTERED,
    RegisterError,
    assign_checkpoints,
    describe_checkpoints,
    draft_automation,
    register_automation,
)

NAME = "fact_automation"
VALUES = {"customer": "Ram Bahadur"}
STATES = ["04_view", "05_add", "05_add"]


def workflow(**overrides: Any) -> dict:
    data = {
        "app": {"title_contains": "FACT ERP.NG", "process_name": "FACTERPNG.exe"},
        "fields": [{"name": "customer", "field": "Customer Name"}],
        "actions": {"save_button": "Save", "wait_after_save_seconds": 0},
        "workflow": {
            "steps": [
                {"action": "click", "button": "&Add"},
                {"action": "fill", "key": "customer"},
                # The wizard's own plans end with the Save click.
                {"action": "click", "button": "Save"},
            ],
            "pause_after_click_seconds": 0,
        },
        "confirmation": {"verifier": "row", "footprint": ["customer"]},
    }
    data.update(overrides)
    return data


def config(**overrides: Any) -> AgentConfig:
    return AgentConfig.model_validate(workflow(**overrides))


def write_config(tmp_path: Path, **overrides: Any) -> Path:
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(workflow(**overrides)), encoding="utf-8")
    return path


def table(*ids: str) -> StateTable:
    return StateTable.model_validate(
        {
            "process": "FACTERPNG.exe",
            "states": [{"id": state, "match": {"auto_id": "40200"}} for state in ids],
        }
    )


class Screen:
    def __init__(self, state: str = "04_view") -> None:
        self.state = state

    def __call__(self) -> StateMatch:
        return StateMatch(state_id=self.state)


class Executor:
    """Runs the plan and moves the screen along, like the live app does."""

    def __init__(
        self,
        screen: Screen | None = None,
        *,
        states: list[str] | None = None,
        stop_after: int | None = None,
        problems: tuple[str, ...] = (),
    ) -> None:
        self.screen = screen
        self.states = states or STATES
        self.stop_after = stop_after
        self.problems = problems
        self.calls = 0

    def __call__(self, steps: list, values: dict, **options: Any) -> PlanResult:
        before_step = options.get("before_step")
        ran = 0
        for position in range(1, len(steps) + 1):
            if self.stop_after is not None and position > self.stop_after:
                break
            if self.screen is not None:
                self.screen.state = self.states[position - 1]
            if before_step is not None:
                before_step(position, steps[position - 1])
            self.calls += 1
            ran += 1
        return PlanResult(
            inspection=object(), fields={}, steps_run=ran, problems=self.problems
        )


class Save:
    """Stands in for the Save click, and counts how often it was asked for."""

    def __init__(
        self,
        confirmed: bool = True,
        reason: str = "the new row is on screen",
        before: Any = None,
        after: Any = None,
    ):
        self.confirmed = confirmed
        self.reason = reason
        self.before = before
        self.after = after
        self.calls = 0
        self.values: dict[str, Any] = {}

    def __call__(self, **options: Any) -> SaveResult:
        from execution.verification.confirmation import ConfirmationResult

        self.calls += 1
        self.values = dict(options.get("values") or {})
        return SaveResult(
            outcome=ConfirmationResult(
                outcome=CONFIRMED if self.confirmed else "unconfirmed",
                reason=self.reason,
            ),
            before=self.before,
            after=self.after,
        )


def go(
    tmp_path: Path,
    *,
    save: Save | None = None,
    screen: Screen | None = None,
    executor: Executor | None = None,
    config_path: Path | None = None,
    values: dict | None = None,
    states: tuple[str, ...] = ("04_view", "05_add"),
    name: str = NAME,
    **options: Any,
):
    screen = screen or Screen("04_view")
    executor = executor or Executor()
    if executor.screen is None:
        executor.screen = screen
    return register_automation(
        name=name,
        config_path=config_path or write_config(tmp_path),
        values=dict(values if values is not None else VALUES),
        automations_root=tmp_path / "automations",
        table=table(*states),
        detector=screen,
        attach=lambda app: object(),
        inspect=lambda window: object(),
        execute=executor,
        save_call=save if save is not None else Save(),
        output=lambda line: None,
        sleep=lambda seconds: None,
        **options,
    )


# -------------------------------------------------------------------- draft --
def test_the_save_click_leaves_the_plan_and_becomes_the_save_spec() -> None:
    draft = draft_automation(config(), name=NAME, entry_state="04_view")

    assert [step.action for step in draft.steps] == ["click", "fill"]
    assert draft.save.button == "Save"
    assert draft.save.verifier == "row"
    assert draft.save.footprint == ["customer"]
    assert draft.fill_keys() == ["customer"]
    assert draft.verified is False


def test_step_ids_are_readable_and_unique() -> None:
    draft = draft_automation(config(), name=NAME, entry_state="04_view")
    assert draft.step_ids() == ["01-click-add", "02-fill-customer"]


def test_clicks_may_be_replayed_and_fills_may_not() -> None:
    draft = draft_automation(config(), name=NAME, entry_state="04_view")
    assert [step.checkpoint.resume for step in draft.steps] == ["replay", "restart"]


def test_every_checkpoint_starts_on_the_opening_screen() -> None:
    draft = draft_automation(config(), name=NAME, entry_state="04_view")
    assert draft.state_ids() == ["04_view"]


def test_a_plan_that_only_clicks_save_is_refused() -> None:
    with pytest.raises(RegisterError, match="only step"):
        draft_automation(
            config(workflow={"steps": [{"action": "click", "button": "Save"}]}),
            name=NAME,
            entry_state="04_view",
        )


def test_a_config_without_a_workflow_is_refused() -> None:
    data = workflow()
    data.pop("workflow")
    with pytest.raises(RegisterError, match="no workflow"):
        draft_automation(
            AgentConfig.model_validate(data), name=NAME, entry_state="04_view"
        )


# -------------------------------------------------------------- checkpoints --
def test_observed_screens_become_the_checkpoints() -> None:
    draft = draft_automation(config(), name=NAME, entry_state="04_view")
    assign_checkpoints(draft, ["04_view", "05_add"])

    assert [step.checkpoint.state for step in draft.steps] == ["04_view", "05_add"]


def test_a_checkpoint_for_every_step_is_required() -> None:
    draft = draft_automation(config(), name=NAME, entry_state="04_view")
    with pytest.raises(RegisterError, match="one for one"):
        assign_checkpoints(draft, ["04_view"])


def test_describe_checkpoints_names_the_screen_of_each_step() -> None:
    draft = draft_automation(config(), name=NAME, entry_state="04_view")
    assign_checkpoints(draft, ["04_view", "05_add"])
    lines = describe_checkpoints(draft)
    assert lines[0].startswith("1. click '&Add' on 04_view")
    assert lines[1].startswith("2. fill 'Customer Name' on 05_add")


# --------------------------------------------------------------- registering --
def test_a_proved_workflow_is_registered(tmp_path: Path) -> None:
    result = go(tmp_path)

    assert result.status == REGISTERED
    assert result.ok
    assert result.path == tmp_path / "automations" / NAME / "automation.json"
    assert result.path.is_file()
    # registering hands you something to double-click, not just a JSON file
    assert result.launcher == tmp_path / "automations" / NAME / "run.bat"
    assert result.launcher.is_file()
    assert f"automation run {NAME}" in result.launcher.read_text(encoding="ascii")
    assert result.checkpoints == ("04_view", "05_add")
    assert result.steps_run == 2

    stored = registry.load(NAME, tmp_path / "automations")
    assert stored.verified is True
    assert stored.created_at and stored.updated_at
    assert [step.checkpoint.state for step in stored.steps] == ["04_view", "05_add"]


def test_the_registered_plan_is_runnable_by_the_runner(tmp_path: Path) -> None:
    result = go(tmp_path)
    assert result.automation is not None
    runnable = result.automation.to_config()
    assert len(runnable.workflow.steps) == 2
    assert runnable.actions.save_button == "Save"


def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    result = go(tmp_path, write=False)

    assert result.status == DRAFTED
    assert result.path is None
    assert not (tmp_path / "automations").exists()
    assert result.checkpoints == ("04_view", "05_add")


def test_registering_twice_keeps_the_previous_version(tmp_path: Path) -> None:
    go(tmp_path)
    go(tmp_path)
    previous = tmp_path / "automations" / NAME / registry.PREVIOUS_FILE
    assert previous.is_file()


# ----------------------------------------------------------------- refusals --
def test_an_unknown_opening_screen_is_refused(tmp_path: Path) -> None:
    result = go(tmp_path, screen=Screen(CLOSED))

    assert result.status == REFUSED
    assert "cannot register from here" in result.message
    assert "not running" in result.message
    assert not (tmp_path / "automations").exists()


def test_an_invalid_name_is_refused(tmp_path: Path) -> None:
    result = go(tmp_path, name="Fact Automation")
    assert result.status == REFUSED
    assert "lower case" in result.message or "2-40" in result.message


def test_missing_values_are_refused_before_the_proof(tmp_path: Path) -> None:
    result = go(tmp_path, values={})
    assert result.status == REFUSED
    assert "'customer'" in result.message
    assert not (tmp_path / "automations").exists()


def test_a_broken_config_is_refused(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    result = go(tmp_path, config_path=broken)
    assert result.status == REFUSED
    assert "cannot read" in result.message or "invalid" in result.message


# ------------------------------------------------------------- the proof run --
def test_a_screen_the_table_does_not_know_stops_the_registration(tmp_path: Path) -> None:
    screen = Screen("04_view")

    class FailsOnStepTwo(Executor):
        def __call__(self, steps: list, values: dict, **options: Any) -> PlanResult:
            self.screen.state = "04_view"
            options["before_step"](1, steps[0])
            self.screen.state = POPUP
            options["before_step"](2, steps[1])
            return PlanResult(inspection=object(), fields={}, steps_run=1)

    result = go(tmp_path, screen=screen, executor=FailsOnStepTwo(screen))

    assert result.status == FAILED
    assert "does not know" in result.message
    assert not (tmp_path / "automations").exists()


def test_a_step_that_fails_on_the_screen_registers_nothing(tmp_path: Path) -> None:
    result = go(
        tmp_path,
        executor=Executor(problems=("step 2 ('Customer Name'): no match",)),
    )

    assert result.status == FAILED
    assert "did not complete" in result.message
    assert "no match" in result.message
    assert not (tmp_path / "automations").exists()


def test_a_proof_that_stops_early_registers_nothing(tmp_path: Path) -> None:
    result = go(tmp_path, executor=Executor(stop_after=1))

    assert result.status == FAILED
    assert "only 1 of 2" in result.message
    assert not (tmp_path / "automations").exists()


def test_the_proof_saves_once_at_the_end(tmp_path: Path) -> None:
    saver = Save()

    result = go(tmp_path, save=saver)

    assert result.status == REGISTERED
    assert saver.calls == 1, "the record is saved once, by the proof"
    assert saver.values == VALUES


def test_the_save_is_not_one_of_the_steps(tmp_path: Path) -> None:
    seen: list[str] = []

    class Recorder(Executor):
        def __call__(self, steps: list, values: dict, **options: Any) -> PlanResult:
            for step in steps:
                seen.append(type(step).__name__ + ":" + getattr(step, "button", ""))
            return super().__call__(steps, values, **options)

    result = go(tmp_path, executor=Recorder())
    assert result.status == REGISTERED
    assert seen == ["ClickStep:&Add", "FillStep:"], "Save must not be in the plan"


def test_a_save_that_cannot_be_confirmed_registers_nothing(tmp_path: Path) -> None:
    saver = Save(confirmed=False, reason="no new row appeared")

    result = go(tmp_path, save=saver)

    assert result.status == FAILED
    assert "not confirmed" in result.message
    assert "no new row appeared" in result.message
    assert not (tmp_path / "automations").exists()


# ------------------------------------------------------- the save footprint --
def test_the_proof_decides_what_a_saved_record_looks_like(tmp_path: Path) -> None:
    result = go(tmp_path, footprint_for=lambda *a, **k: ["customer"])

    assert result.status == REGISTERED
    stored = registry.load(NAME, tmp_path / "automations")
    assert stored.save.verifier == "row"
    assert stored.save.footprint == ["customer"]


def test_a_proof_that_observed_nothing_keeps_the_typed_values_as_its_check(
    tmp_path: Path,
) -> None:
    result = go(tmp_path, footprint_for=lambda *a, **k: [])

    assert result.status == REGISTERED
    stored = registry.load(NAME, tmp_path / "automations")
    # better a check that looks for what was typed than no check at all
    assert stored.save.verifier == "row"
    assert stored.save.footprint == ["customer"]


def test_an_explicit_none_verifier_is_honoured(tmp_path: Path) -> None:
    result = go(
        tmp_path,
        config_path=write_config(
            tmp_path, confirmation={"verifier": "none", "footprint": []}
        ),
    )

    assert result.status == REGISTERED
    stored = registry.load(NAME, tmp_path / "automations")
    assert stored.save.verifier == "none"
    assert stored.save.footprint == []


def test_the_default_footprint_is_read_from_the_proof_screens(tmp_path: Path) -> None:
    from desktop.types import Rect, UIACandidate
    from desktop.uia.uia import UIAInspection

    def candidate(name: str, value: str = "", control_type: str = "Text") -> UIACandidate:
        return UIACandidate(
            candidate_id=name,
            name=name,
            control_type=control_type,
            automation_id="",
            class_name="",
            rect=Rect(0, 0, 10, 10),
            value=value,
            enabled=True,
        )

    def inspection(*candidates: UIACandidate) -> UIAInspection:
        return UIAInspection(window=object(), candidates=candidates, _controls={})

    before = inspection(candidate("Customer"))
    after = inspection(candidate("Customer"), candidate("Ram Bahadur"))

    result = go(tmp_path, save=Save(before=before, after=after))

    assert result.status == REGISTERED
    stored = registry.load(NAME, tmp_path / "automations")
    assert stored.save.footprint == ["customer"], "the value that appeared is the footprint"
