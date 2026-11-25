"""The step vocabulary: double-click and key press, end to end.

The plan for FACT needs a double-click (records open that way) and a key press
(committing a typed lookup). These tests pin the three places that must agree:
the automation model that is saved, the planner response that authors it, and
the executor that runs it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from ai.planners.step_planner import parse_step_plan
from core.automation.model import Automation, AutomationStep
from desktop.types import Rect
from execution.actions import engine as form_steps
from execution.actions.models import DoubleClickStep, KeyStep
from execution.actions.engine import execute_form_steps


def _checkpoint(state: str = "04_view", resume: str = "replay") -> dict:
    return {"state": state, "resume": resume}


def _automation(steps: list[dict]) -> Automation:
    return Automation.model_validate(
        {
            "name": "fact_automation",
            "app": {"process_name": "FACTERPNG.exe"},
            "fields": [{"name": "customer", "field": "Customer"}],
            "steps": steps,
            "save": {"button": "Save & New", "verifier": "none"},
        }
    )


# --------------------------------------------------------------------- model --


def test_a_double_click_and_a_key_step_survive_a_save_and_a_load() -> None:
    plan = _automation(
        [
            {"id": "1", "action": "double_click", "button": "Sales Order",
             "checkpoint": _checkpoint()},
            {"id": "2", "action": "fill", "key": "customer", "field": "Customer",
             "checkpoint": _checkpoint("05_add", "restart")},
            {"id": "3", "action": "key", "keys": "Enter",
             "checkpoint": _checkpoint("05_add")},
        ]
    )

    stored = plan.model_dump(mode="json", by_alias=True)
    again = Automation.model_validate(stored)

    assert [step.action for step in again.steps] == ["double_click", "fill", "key"]
    assert again.steps[0].button == "Sales Order"
    assert again.steps[2].keys == "Enter"
    assert again.steps[2].describe() == "3 (key 'Enter')"


def test_click_progress_wait_survives_storage_and_runtime_conversion() -> None:
    plan = _automation(
        [
            {
                "id": "wait-and-add",
                "action": "click",
                "button": "&Add",
                "wait_for_progress": {
                    "automation_id": "pgbar",
                    "target_percent": 100,
                    "timeout_seconds": 120,
                    "stable_reads": 3,
                },
                "checkpoint": _checkpoint(),
            }
        ]
    )

    stored = plan.model_dump(mode="json", by_alias=True)
    again = Automation.model_validate(stored)
    runtime_step = again.to_config().workflow.steps[0]

    assert again.steps[0].wait_for_progress.automation_id == "pgbar"
    assert runtime_step.wait_for_progress.target_percent == 100
    assert runtime_step.wait_for_progress.stable_reads == 3


def test_the_executor_config_keeps_the_same_actions() -> None:
    plan = _automation(
        [
            {"id": "1", "action": "double_click", "button": "Sales Order",
             "checkpoint": _checkpoint()},
            {"id": "2", "action": "key", "keys": "Tab",
             "checkpoint": _checkpoint()},
        ]
    )

    config = plan.to_config()

    assert [step.action for step in config.workflow.steps] == [
        "double_click",
        "key",
    ]
    assert config.workflow.steps[1].keys == "Tab"


def test_a_key_step_cannot_also_name_a_button() -> None:
    with pytest.raises(ValueError):
        AutomationStep.model_validate(
            {
                "id": "1",
                "action": "key",
                "keys": "Enter",
                "button": "OK",
                "checkpoint": _checkpoint(),
            }
        )


def test_a_second_click_on_save_is_still_refused_as_a_double_click() -> None:
    with pytest.raises(ValueError):
        _automation(
            [
                {"id": "1", "action": "double_click", "button": "Save & New",
                 "checkpoint": _checkpoint()},
            ]
        )


# ------------------------------------------------------------------- planner --


def _button(label: str) -> object:
    @dataclass
    class Candidate:
        candidate_id: str = "b1"
        name: str = ""
        control_type: str = "Button"
        automation_id: str = ""
        rect: Rect = Rect(10, 10, 60, 40)
        value: str = ""
        enabled: bool = True

        @property
        def label(self) -> str:
            return self.name or self.automation_id

    return Candidate(name=label)


def test_an_instruction_to_double_click_keeps_its_own_kind() -> None:
    plan = parse_step_plan(
        {"kind": "double_click", "button": "Sales Order", "confidence": 0.9},
        candidates=[_button("Sales Order")],
        fields=[],
    )

    assert plan.kind == "double_click"
    assert plan.label == "Sales Order"


def test_a_key_press_resolves_without_a_control() -> None:
    plan = parse_step_plan(
        {"kind": "key", "keys": "Ctrl + S"},
        candidates=[],
        fields=[],
    )

    assert plan.kind == "key"
    assert plan.keypress == "Ctrl + S"


# -------------------------------------------------------------------- engine --


@dataclass
class FakeCandidate:
    candidate_id: str
    name: str
    control_type: str
    rect: Rect
    automation_id: str = ""


class FakeInspection:
    def __init__(self, candidates: list[FakeCandidate]) -> None:
        self.candidates = tuple(candidates)
        self.window = _window()

    def read_value(self, candidate_id: str) -> str:
        raise AssertionError("a key or double-click step reads nothing back")


def _window():
    from desktop.types import WindowInfo

    return WindowInfo(
        handle=1,
        title="FACT ERP.NG - Sale Order View",
        process_name="FACTERPNG.exe",
        class_name="WindowsForms10.Window",
        rect=Rect(0, 0, 400, 300),
        client_rect=Rect(0, 0, 400, 300),
        dpi=96,
    )


def test_a_key_step_goes_straight_to_the_keyboard(monkeypatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(form_steps, "press_keys", sent.append)
    inspection = FakeInspection([])
    monkeypatch.setattr(form_steps, "inspect_window", lambda *a, **k: inspection)

    result = execute_form_steps(
        [KeyStep(action="key", keys="Enter")],
        {},
        window=_window(),
        inspection=inspection,
        labels={},
        types={},
        pause_after_click=0,
        delay=0,
    )

    assert sent == ["Enter"]
    assert result.ok
    assert result.steps_run == 1


def test_a_double_click_step_clicks_the_named_control_twice(monkeypatch) -> None:
    clicked: list[tuple[int, int]] = []
    monkeypatch.setattr(
        form_steps,
        "double_click_checked",
        lambda x, y, window, **kwargs: clicked.append((x, y)),
    )
    inspection = FakeInspection(
        [FakeCandidate("b1", "Sales Order", "Button", Rect(10, 20, 110, 60))]
    )
    monkeypatch.setattr(form_steps, "inspect_window", lambda *a, **k: inspection)

    result = execute_form_steps(
        [DoubleClickStep(action="double_click", button="Sales Order")],
        {},
        window=_window(),
        inspection=inspection,
        labels={},
        types={},
        pause_after_click=0,
        delay=0,
    )

    assert result.ok, result.problems
    assert clicked == [(60, 40)]
