"""Unit tests for the static, step-driven form filler."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from PIL import Image

from execution.actions import engine as form_steps
from execution.verification.controls import ResolvedTarget
from execution.verification.confirmation import (
    CONFIRMED,
    UNKNOWN,
    ConfirmationResult,
)
from execution.actions.engine import PlanResult, execute_form_steps, step_keys
from core.config import AgentConfig
from core.runtime.errors import RunnerAborted
from desktop.screenshots.capture import Capture
from desktop.input.input import InputError
from desktop.uia.uia import UIAError, UIAInspection
from execution.actions.models import ClickStep, FillStep
from core.config import ConfirmationConfig
from desktop.types import Rect, UIACandidate, WindowInfo


def _window(width: int = 400, height: int = 300) -> WindowInfo:
    return WindowInfo(
        handle=1,
        title="Form",
        process_name="test.exe",
        class_name="Form",
        rect=Rect(0, 0, width, height),
        client_rect=Rect(0, 0, width, height),
        dpi=96,
    )


@dataclass
class FakeCandidate:
    candidate_id: str
    name: str
    control_type: str
    rect: Rect
    automation_id: str = ""


class FakeInspection:
    """The slice of ``UIAInspection`` that resolution and read-back use."""

    def __init__(
        self,
        candidates: list[FakeCandidate],
        *,
        values: dict[str, str] | None = None,
        window: WindowInfo | None = None,
    ) -> None:
        self.candidates = tuple(candidates)
        self.window = window or _window()
        self._values = dict(values or {})
        self.reads: list[str] = []

    def read_value(self, candidate_id: str) -> str:
        self.reads.append(candidate_id)
        if candidate_id not in self._values:
            raise UIAError(f"no value for {candidate_id}")
        return self._values[candidate_id]


def _field(name: str, candidate_id: str = "e1") -> FakeCandidate:
    return FakeCandidate(
        candidate_id=candidate_id,
        name=name,
        control_type="Edit",
        rect=Rect(10, 20, 110, 40),
    )


def _button(name: str, candidate_id: str = "b1") -> FakeCandidate:
    return FakeCandidate(
        candidate_id=candidate_id,
        name=name,
        control_type="Button",
        rect=Rect(200, 200, 260, 240),
    )


def _config(
    *,
    steps: list[dict] | None = None,
    fields: list[dict] | None = None,
    max_attempts: int = 3,
    retry_on_failure: bool = False,
    stop_on_failure: bool = False,
) -> AgentConfig:
    payload: dict = {
        "app": {"title_contains": "Form"},
        "fields": fields
        if fields is not None
        else [
            {"name": "Amount", "field": "Amount", "type": "number"},
            {"name": "Invoice Number", "field": "Invoice Number"},
        ],
        "actions": {"save_button": "Save", "wait_after_save_seconds": 0},
        "safety": {
            "max_attempts": max_attempts,
            "action_delay_seconds": 0,
            "stop_on_failure": stop_on_failure,
        },
    }
    if steps is not None:
        payload["workflow"] = {
            "steps": steps,
            "retry_on_failure": retry_on_failure,
            "pause_after_click_seconds": 0,
        }
    return AgentConfig.model_validate(payload)


def _capture() -> Capture:
    return Capture(
        image=Image.new("RGB", (400, 300), "white"),
        origin=(0, 0),
        scale_x=1.0,
        scale_y=1.0,
        window=_window(),
    )


def _stub_input(monkeypatch) -> dict[str, list]:
    """Replace the desktop input layer and the clock in ``form_steps``."""
    calls: dict[str, list] = {"clicks": [], "pastes": [], "keys": []}
    monkeypatch.setattr(
        form_steps,
        "click_checked",
        lambda x, y, *_a, **_k: calls["clicks"].append((x, y)),
    )
    monkeypatch.setattr(
        form_steps, "paste_text", lambda text: calls["pastes"].append(text)
    )
    monkeypatch.setattr(
        form_steps, "press_chord", lambda *_a: calls["keys"].append("all")
    )
    monkeypatch.setattr(
        form_steps, "press_key", lambda vk: calls["keys"].append(vk)
    )
    monkeypatch.setattr(form_steps, "_wait", lambda *a, **k: True)
    return calls


# ---------------------------------------------------------------------------
# Plan execution
# ---------------------------------------------------------------------------


def test_a_click_is_followed_by_a_fresh_inspection(monkeypatch) -> None:
    calls = _stub_input(monkeypatch)
    before = FakeInspection([_button("Add New")])
    after = FakeInspection([_field("Amount")], values={"e1": "500"})
    inspected: list[int] = []

    def fake_inspect(_window):
        inspected.append(1)
        return after

    monkeypatch.setattr(form_steps, "inspect_window", fake_inspect)

    plan = execute_form_steps(
        [
            ClickStep(action="click", button="Add New"),
            FillStep(action="fill", key="Amount"),
        ],
        {"Amount": "500"},
        window=_window(),
        inspection=before,
        labels={"Amount": "Amount"},
        types={"Amount": "number"},
    )

    assert plan.ok, plan.problems
    assert plan.steps_run == 2
    assert list(plan.fields) == ["Amount"]
    assert calls["pastes"] == ["500"]
    assert len(calls["clicks"]) == 2
    assert inspected == [1]


def test_a_click_can_hand_the_plan_to_a_new_window(monkeypatch) -> None:
    calls = _stub_input(monkeypatch)
    before = FakeInspection([_button("Login")])
    after = FakeInspection([_field("Amount")], values={"e1": "500"})
    refreshed: list[int] = []

    def reacquire():
        refreshed.append(1)
        return _window(), after

    monkeypatch.setattr(
        form_steps,
        "inspect_window",
        lambda _window: pytest.fail("a stale window must not be re-read"),
    )

    plan = execute_form_steps(
        [
            ClickStep(action="click", button="Login"),
            FillStep(action="fill", key="Amount"),
        ],
        {"Amount": "500"},
        window=_window(),
        inspection=before,
        labels={"Amount": "Amount"},
        types={"Amount": "number"},
        reacquire=reacquire,
    )

    assert plan.ok, plan.problems
    assert plan.steps_run == 2
    assert refreshed == [1]
    assert plan.inspection is after


def test_a_pane_button_is_clicked_by_its_automation_id(monkeypatch) -> None:
    calls = _stub_input(monkeypatch)
    monkeypatch.setattr(form_steps, "inspect_window", lambda _w: FakeInspection([]))
    image_button = FakeCandidate(
        candidate_id="b1",
        name="System.Drawing.Bitmap",
        control_type="Pane",
        rect=Rect(200, 200, 260, 240),
        automation_id="btnLogin",
    )

    plan = execute_form_steps(
        [ClickStep(action="click", button="btnLogin")],
        {},
        window=_window(),
        inspection=FakeInspection([image_button]),
        labels={},
        types={},
    )

    assert plan.ok, plan.problems
    assert plan.steps_run == 1
    assert calls["clicks"] == [(230, 220)]


def test_a_fill_is_read_back_from_the_inspection_that_resolved_it(
    monkeypatch,
) -> None:
    _stub_input(monkeypatch)
    inspection = FakeInspection([_field("Amount")], values={"e1": "500"})

    plan = execute_form_steps(
        [FillStep(action="fill", key="Amount")],
        {"Amount": "500"},
        window=_window(),
        inspection=inspection,
        labels={"Amount": "Amount"},
        types={"Amount": "number"},
    )

    assert plan.ok, plan.problems
    assert inspection.reads == ["e1"]


def test_a_read_back_mismatch_is_a_problem(monkeypatch) -> None:
    _stub_input(monkeypatch)
    inspection = FakeInspection([_field("Amount")], values={"e1": "50"})

    plan = execute_form_steps(
        [
            FillStep(action="fill", key="Amount"),
            FillStep(action="fill", key="Invoice Number"),
        ],
        {"Amount": "500", "Invoice Number": "INV-1"},
        window=_window(),
        inspection=inspection,
        labels={"Amount": "Amount", "Invoice Number": "Invoice Number"},
        types={"Amount": "number", "Invoice Number": "text"},
    )

    assert not plan.ok
    assert "read back '50'" in plan.problems[0]
    assert plan.steps_run == 1


def test_an_unreadable_field_is_a_warning_not_a_failure(monkeypatch) -> None:
    _stub_input(monkeypatch)
    inspection = FakeInspection([_field("Invoice Number")])

    plan = execute_form_steps(
        [FillStep(action="fill", key="Invoice Number")],
        {"Invoice Number": "INV-1"},
        window=_window(),
        inspection=inspection,
        labels={"Invoice Number": "Invoice Number"},
        types={"Invoice Number": "text"},
    )

    assert plan.ok, plan.problems
    assert "could not read" in plan.warnings[0]


def test_a_missing_button_blocks_the_plan(monkeypatch) -> None:
    calls = _stub_input(monkeypatch)

    plan = execute_form_steps(
        [
            ClickStep(action="click", button="Add New"),
            FillStep(action="fill", key="Amount"),
        ],
        {"Amount": "500"},
        window=_window(),
        inspection=FakeInspection([_field("Amount")]),
        labels={"Amount": "Amount"},
        types={"Amount": "number"},
    )

    assert not plan.ok
    assert "click 'Add New'" in plan.problems[0]
    assert plan.steps_run == 0
    assert calls["pastes"] == []


def test_an_optional_step_is_skipped_and_the_plan_continues(
    monkeypatch,
) -> None:
    calls = _stub_input(monkeypatch)
    inspection = FakeInspection([_field("Amount")], values={"e1": "500"})

    plan = execute_form_steps(
        [
            ClickStep(action="click", button="Add Line", optional=True),
            FillStep(action="fill", key="Amount"),
        ],
        {"Amount": "500"},
        window=_window(),
        inspection=inspection,
        labels={"Amount": "Amount"},
        types={"Amount": "number"},
    )

    assert plan.ok, plan.problems
    assert plan.steps_run == 1
    assert "skipped" in plan.warnings[0]
    assert calls["pastes"] == ["500"]


def test_a_step_can_override_the_field_label(monkeypatch) -> None:
    calls = _stub_input(monkeypatch)
    inspection = FakeInspection(
        [_field("Item Amount")], values={"e1": "500"}
    )

    plan = execute_form_steps(
        [FillStep(action="fill", key="Amount", field="Item Amount")],
        {"Amount": "500"},
        window=_window(),
        inspection=inspection,
        labels={"Amount": "Amount"},
        types={"Amount": "number"},
    )

    assert plan.ok, plan.problems
    assert calls["clicks"] == [(60, 30)]


def test_an_empty_value_clears_the_field(monkeypatch) -> None:
    calls = _stub_input(monkeypatch)
    inspection = FakeInspection([_field("Invoice Number")], values={"e1": ""})

    plan = execute_form_steps(
        [FillStep(action="fill", key="Invoice Number")],
        {"Invoice Number": ""},
        window=_window(),
        inspection=inspection,
        labels={"Invoice Number": "Invoice Number"},
        types={"Invoice Number": "text"},
    )

    assert plan.ok, plan.problems
    assert calls["pastes"] == []
    assert 0x2E in calls["keys"]


def test_a_stopped_controller_aborts_before_typing(monkeypatch) -> None:
    calls = _stub_input(monkeypatch)

    class Stopped:
        def check(self) -> bool:
            return False

    with pytest.raises(RunnerAborted):
        execute_form_steps(
            [FillStep(action="fill", key="Amount")],
            {"Amount": "500"},
            window=_window(),
            inspection=FakeInspection([_field("Amount")]),
            labels={"Amount": "Amount"},
            types={"Amount": "number"},
            controller=Stopped(),
        )
    assert calls["pastes"] == []


def test_an_undeclared_label_is_reported_not_typed(monkeypatch) -> None:
    _stub_input(monkeypatch)

    plan = execute_form_steps(
        [FillStep(action="fill", key="Amount")],
        {"Amount": "500"},
        window=_window(),
        inspection=FakeInspection([_field("Amount")]),
        labels={},
        types={"Amount": "number"},
    )

    assert not plan.ok
    assert "has no field label" in plan.problems[0]


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_steps_are_parsed_into_typed_actions() -> None:
    config = _config(
        steps=[
            {"action": "click", "button": "Add New"},
            {"action": "fill", "key": "Amount"},
        ]
    )

    assert config.workflow is not None
    assert isinstance(config.workflow.steps[0], ClickStep)
    assert isinstance(config.workflow.steps[1], FillStep)
    assert config.workflow.retry_on_failure is False


def test_a_config_without_a_workflow_is_still_valid() -> None:
    assert _config().workflow is None


def test_steps_must_fill_declared_fields() -> None:
    with pytest.raises(Exception) as excinfo:
        _config(steps=[{"action": "fill", "key": "Total"}])
    assert "does not declare" in str(excinfo.value)


def test_steps_must_not_be_empty() -> None:
    with pytest.raises(Exception) as excinfo:
        _config(steps=[])
    assert "at least 1" in str(excinfo.value)


def test_an_unknown_action_is_rejected() -> None:
    with pytest.raises(Exception) as excinfo:
        _config(steps=[{"action": "select", "key": "Amount"}])
    assert "action" in str(excinfo.value)


def test_the_plan_lists_each_key_once_in_first_use_order() -> None:
    config = _config(
        steps=[
            {"action": "fill", "key": "Invoice Number"},
            {"action": "click", "button": "Add Line"},
            {"action": "fill", "key": "Amount"},
            {"action": "fill", "key": "Invoice Number"},
        ]
    )

    assert config.workflow is not None
    assert step_keys(config.workflow) == ["Invoice Number", "Amount"]


# ---------------------------------------------------------------------------
# Save verification
# ---------------------------------------------------------------------------


def _uia(*items: tuple[str, str, str]) -> UIAInspection:
    candidates = tuple(
        UIACandidate(
            candidate_id=f"W{index}",
            name=name,
            control_type=control_type,
            automation_id="",
            class_name="",
            rect=Rect(10, 20 * index, 110, 20 * index + 18),
            value=value,
            enabled=True,
        )
        for index, (name, control_type, value) in enumerate(items, start=1)
    )
    return UIAInspection(window=_window(), candidates=candidates, _controls={})


def test_a_save_is_confirmed_from_the_window_text_without_ai(monkeypatch) -> None:
    """verifier="row" needs no confirmer at all: the new value is the evidence."""
    before = _uia(("Amount", "Edit", "500"), ("Save", "Button", ""))
    after = _uia(("500", "Text", ""))
    monkeypatch.setattr(form_steps, "inspect_window", lambda _w, **_k: after)
    config = _config().model_copy(
        update={
            "confirmation": ConfirmationConfig(
                verifier="row", footprint=["Amount"]
            )
        }
    )
    messages: list[str] = []

    result = form_steps.verify_save(
        config=config,
        confirmation=None,
        window=_window(),
        before=before,
        post=_capture(),
        evidence="",
        values={"Amount": "500"},
        output=messages.append,
    )

    assert result.outcome == CONFIRMED
    assert "the record appeared" in result.reason


def test_a_local_check_without_the_value_is_a_recheck(monkeypatch) -> None:
    monkeypatch.setattr(
        form_steps,
        "inspect_window",
        lambda _w, **_k: _uia(("Clients", "Text", "")),
    )
    config = _config().model_copy(
        update={
            "confirmation": ConfirmationConfig(
                verifier="row", footprint=["Amount"]
            )
        }
    )

    result = form_steps.verify_save(
        config=config,
        confirmation=None,
        window=_window(),
        before=_uia(("Amount", "Edit", "500"), ("Save", "Button", "")),
        post=_capture(),
        evidence="",
        values={"Amount": "500"},
        output=lambda _m: None,
    )

    assert result.outcome == UNKNOWN
    assert "none of the saved values appeared" in result.reason

# ---------------------------------------------------------------------------
# Resuming a run: start_index and the per-step hook
# ---------------------------------------------------------------------------


def _two_field_plan() -> tuple[list, dict, dict, dict]:
    steps = [
        FillStep(action="fill", key="Amount", optional=True),
        FillStep(action="fill", key="Invoice Number"),
    ]
    return (
        steps,
        {"Amount": "500", "Invoice Number": "INV-1"},
        {"Amount": "Amount", "Invoice Number": "Invoice Number"},
        {"Amount": "number", "Invoice Number": "text"},
    )


def test_start_index_skips_the_steps_already_done(monkeypatch) -> None:
    calls = _stub_input(monkeypatch)
    steps, values, labels, types = _two_field_plan()
    inspection = FakeInspection(
        [_field("Amount", "e1"), _field("Invoice Number", "e2")],
        values={"e1": "500", "e2": "INV-1"},
    )

    plan = execute_form_steps(
        steps,
        values,
        window=_window(),
        inspection=inspection,
        labels=labels,
        types=types,
        start_index=1,
    )

    assert plan.ok, plan.problems
    assert plan.steps_run == 1
    assert calls["pastes"] == ["INV-1"], "the skipped step must not be typed again"


def test_step_numbers_in_messages_stay_absolute_when_resuming(monkeypatch) -> None:
    _stub_input(monkeypatch)
    steps, values, labels, types = _two_field_plan()

    plan = execute_form_steps(
        steps,
        values,
        window=_window(),
        inspection=FakeInspection([_field("Amount", "e1")]),
        labels=labels,
        types=types,
        start_index=1,
    )

    assert not plan.ok
    assert plan.problems[0].startswith("step 2 "), plan.problems


def test_every_finished_step_is_reported_to_the_caller(monkeypatch) -> None:
    _stub_input(monkeypatch)
    steps = [
        ClickStep(action="click", button="Add"),
        FillStep(action="fill", key="Amount"),
    ]
    after = FakeInspection([_field("Amount", "e1")], values={"e1": "500"})
    monkeypatch.setattr(form_steps, "inspect_window", lambda _w, **_k: after)
    seen: list[tuple[int, str]] = []

    plan = execute_form_steps(
        steps,
        {"Amount": "500"},
        window=_window(),
        inspection=FakeInspection([_button("Add")]),
        labels={"Amount": "Amount"},
        types={"Amount": "number"},
        on_step=lambda position, step: seen.append((position, step.action)),
    )

    assert plan.ok, plan.problems
    assert seen == [(1, "click"), (2, "fill")]


def test_a_skipped_optional_step_is_not_reported(monkeypatch) -> None:
    _stub_input(monkeypatch)
    seen: list[int] = []

    plan = execute_form_steps(
        [FillStep(action="fill", key="Amount", optional=True)],
        {"Amount": "500"},
        window=_window(),
        inspection=FakeInspection([]),
        labels={"Amount": "Amount"},
        types={"Amount": "number"},
        on_step=lambda position, _step: seen.append(position),
    )

    assert plan.steps_run == 0
    assert "skipped" in plan.warnings[0]
    assert seen == []


def test_an_unreadable_field_is_still_reported_as_done(monkeypatch) -> None:
    _stub_input(monkeypatch)
    seen: list[int] = []
    inspection = FakeInspection([_field("Amount", "e1")], values={})

    plan = execute_form_steps(
        [FillStep(action="fill", key="Amount")],
        {"Amount": "500"},
        window=_window(),
        inspection=inspection,
        labels={"Amount": "Amount"},
        types={"Amount": "number"},
        on_step=lambda position, _step: seen.append(position),
    )

    assert plan.ok, plan.problems
    assert seen == [1], "the paste landed even though the value could not be read"


def test_a_failed_step_does_not_report_itself(monkeypatch) -> None:
    _stub_input(monkeypatch)
    seen: list[int] = []
    inspection = FakeInspection([_field("Amount", "e1")], values={"e1": "nonsense"})

    plan = execute_form_steps(
        [FillStep(action="fill", key="Amount")],
        {"Amount": "500"},
        window=_window(),
        inspection=inspection,
        labels={"Amount": "Amount"},
        types={"Amount": "number"},
        on_step=lambda position, _step: seen.append(position),
    )

    assert not plan.ok
    assert seen == []


def test_a_precondition_that_raises_runs_nothing(monkeypatch) -> None:
    calls = _stub_input(monkeypatch)

    def before_step(position, step):
        raise RuntimeError("the form is not open")

    with pytest.raises(RuntimeError, match="not open"):
        execute_form_steps(
            [FillStep(action="fill", key="Amount")],
            {"Amount": "500"},
            window=_window(),
            inspection=FakeInspection([_field("Amount")]),
            labels={"Amount": "Amount"},
            types={"Amount": "number"},
            before_step=before_step,
        )

    assert calls["clicks"] == [] and calls["pastes"] == []


def test_the_precondition_is_checked_before_the_action(monkeypatch) -> None:
    _stub_input(monkeypatch)
    monkeypatch.setattr(
        form_steps,
        "inspect_window",
        lambda _w, **_k: FakeInspection([_button("Add")]),
    )
    order: list[str] = []

    plan = execute_form_steps(
        [ClickStep(action="click", button="Add")],
        {},
        window=_window(),
        inspection=FakeInspection([_button("Add")]),
        labels={},
        types={},
        before_step=lambda position, step: order.append("check"),
        on_step=lambda position, step: order.append("done"),
    )

    assert plan.ok, plan.problems
    assert order == ["check", "done"]
