"""The run step: a designed script as one step of an authored plan.

A plan is navigation, then the deterministic form filler, then more steps. The
three things that make that safe, pinned here:

* the plan model refuses a script that could be resumed in place or repeated
  per row, because a script types into the form it finds;
* the executor runs it as a leaf - screen checked before, exit code checked
  after - and stops the plan when it fails, so nothing saves half a document;
* registration proves it for real, against the same values file, starting from
  a clean form.
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from core.automation.model import Automation, AutomationStep
from core.config import AgentConfig
from desktop.state.recognizer import StateMatch
from desktop.state.states import StateTable
from desktop.types import Rect, WindowInfo
from desktop.uia.uia import UIAInspection
from execution.actions import engine as form_steps
from execution.actions.engine import execute_form_steps, script_command
from execution.actions.models import ClickStep, ScriptStep
from execution.verification.confirmation import CONFIRMED, ConfirmationResult
from execution.workflows.runner import SaveResult
from wizard.register import (
    REGISTERED,
    RESUME_FOR,
    _step_payload,
    draft_automation,
    register_automation,
)

SCRIPT = "execution/scripts/form_filler_1.py"


def _window() -> WindowInfo:
    return WindowInfo(
        handle=1,
        title="FACT ERP.NG - Sale Order",
        process_name="FACTERPNG.exe",
        class_name="WindowsForms10.Window",
        rect=Rect(0, 0, 400, 300),
        client_rect=Rect(0, 0, 400, 300),
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
    def __init__(self, candidates: list[FakeCandidate] | None = None) -> None:
        self.candidates = tuple(candidates or [])
        self.window = _window()

    def read_value(self, candidate_id: str) -> str:
        raise AssertionError("a run step reads nothing back")


def _automation(steps: list[dict], fields: list[dict] | None = None) -> Automation:
    return Automation.model_validate(
        {
            "name": "sale_order",
            "app": {"process_name": "FACTERPNG.exe"},
            "fields": fields or [],
            "steps": steps,
            "save": {"button": "Save", "verifier": "none"},
        }
    )


def _run_step(**overrides: Any) -> dict:
    step = {
        "id": "fill-the-form",
        "action": "run",
        "script": SCRIPT,
        "args": ["{values_file}"],
        "checkpoint": {"state": "05_add", "resume": "restart"},
    }
    step.update(overrides)
    return step


def _script_file(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "filler.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# --------------------------------------------------------------------- model --


def test_a_run_step_survives_a_save_and_a_load_and_keeps_its_order() -> None:
    plan = _automation(
        [
            {"id": "1", "action": "click", "button": "&Add",
             "checkpoint": {"state": "04_view", "resume": "replay"}},
            _run_step(),
            {"id": "3", "action": "click", "button": "Freight",
             "checkpoint": {"state": "05_add", "resume": "replay"}},
        ]
    )

    stored = plan.model_dump(mode="json", by_alias=True)
    again = Automation.model_validate(stored)

    assert [step.action for step in again.steps] == ["click", "run", "click"]
    assert again.steps[1].script == SCRIPT
    assert again.steps[1].args == ["{values_file}"]
    assert again.steps[1].timeout_seconds == 900.0
    assert again.steps[1].describe() == "fill-the-form (run 'execution/scripts/form_filler_1.py')"


def test_the_executor_config_carries_the_script_and_its_arguments() -> None:
    config = _automation([_run_step(args=["--check", "{values_file}"])]).to_config()

    step = config.workflow.steps[0]
    assert step.action == "run"
    assert step.script == SCRIPT
    assert step.args == ["--check", "{values_file}"]
    assert step.timeout_seconds == 900.0


def test_a_run_step_that_could_be_resumed_in_place_is_refused() -> None:
    with pytest.raises(ValueError, match="restart"):
        AutomationStep.model_validate(
            _run_step(checkpoint={"state": "05_add", "resume": "replay"})
        )


def test_a_run_step_cannot_repeat_per_row() -> None:
    with pytest.raises(ValueError, match="per row"):
        AutomationStep.model_validate(_run_step(per_row=True))


def test_a_run_step_needs_a_script() -> None:
    with pytest.raises(ValueError, match="script"):
        AutomationStep.model_validate(_run_step(script="  "))


def test_a_run_step_cannot_also_name_a_control() -> None:
    with pytest.raises(ValueError, match="no button"):
        AutomationStep.model_validate(_run_step(button="&Add"))


def test_only_a_run_step_carries_a_script() -> None:
    with pytest.raises(ValueError, match="only a run step"):
        AutomationStep.model_validate(
            {
                "id": "1",
                "action": "fill",
                "key": "customer",
                "script": SCRIPT,
                "checkpoint": {"state": "05_add", "resume": "restart"},
            }
        )


# -------------------------------------------------------------------- engine --


def _run(
    tmp_path: Path,
    script: Path,
    *,
    values_file,
    steps=None,
    job_id="",
    args: list[str] | None = None,
) -> Any:
    inspection = FakeInspection(
        [FakeCandidate("b1", "Freight", "Button", Rect(10, 20, 110, 60))]
    )
    return execute_form_steps(
        steps
        or [
            ScriptStep(
                action="run",
                script=str(script),
                args=["{values_file}", "{job_id}"]
                if args is None
                else args,
            ),
            ClickStep(action="click", button="Freight"),
        ],
        {},
        window=_window(),
        inspection=inspection,
        labels={},
        types={},
        pause_after_click=0,
        delay=0,
        settle_seconds=0,
        values_file=values_file,
        job_id=job_id,
    )


def test_the_script_runs_and_the_plan_continues_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = tmp_path / "args.json"
    script = _script_file(
        tmp_path,
        f"""
        import json, sys
        with open(r"{recorded}", "w", encoding="utf-8") as handle:
            json.dump(sys.argv[1:], handle)
        """,
    )
    values = tmp_path / "data.json"
    values.write_text("{}", encoding="utf-8")
    clicked: list[tuple[int, int]] = []

    def screen_after_the_script(*_args: Any, **_kwargs: Any) -> FakeInspection:
        return FakeInspection(
            [FakeCandidate("b1", "Freight", "Button", Rect(10, 20, 110, 60))]
        )

    monkeypatch.setattr(form_steps, "inspect_window", screen_after_the_script)
    monkeypatch.setattr(
        form_steps,
        "click_checked",
        lambda x, y, window, **kwargs: clicked.append((x, y)),
    )

    result = _run(tmp_path, script, values_file=values, job_id="job-7")

    assert result.ok, result.problems
    assert result.steps_run == 2
    assert json.loads(recorded.read_text(encoding="utf-8")) == [str(values), "job-7"]
    assert clicked == [(60, 40)]


def test_consecutive_scripts_skip_the_redundant_intermediate_screen_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        form_steps, "script_command", lambda *_args, **_kwargs: (["script"], "")
    )
    monkeypatch.setattr(
        form_steps, "run_script", lambda *_args, **_kwargs: (False, "")
    )
    checked: list[int] = []
    refreshed: list[int] = []
    inspection = FakeInspection()

    def reacquire():
        refreshed.append(1)
        return _window(), cast(UIAInspection, inspection)

    result = execute_form_steps(
        [
            ScriptStep(action="run", script="first.py"),
            ScriptStep(action="run", script="second.py"),
        ],
        {},
        window=_window(),
        inspection=cast(UIAInspection, inspection),
        labels={},
        types={},
        before_step=lambda index, _step: checked.append(index),
        reacquire=reacquire,
        settle_seconds=0,
        output=lambda _line: None,
    )

    assert result.ok, result.problems
    assert result.steps_run == 2
    assert checked == [1, 2]
    # The second script's precondition remains; only the redundant read after
    # the first script is skipped. The final read is still needed for Save.
    assert refreshed == [1]


def test_a_script_that_fails_stops_the_plan_before_the_next_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script_file(
        tmp_path, 'print("the grid is not on screen"); raise SystemExit(3)'
    )
    clicked: list[int] = []
    monkeypatch.setattr(form_steps, "inspect_window", lambda *a, **k: FakeInspection())
    monkeypatch.setattr(
        form_steps, "click_checked", lambda *a, **k: clicked.append(1)
    )

    result = _run(tmp_path, script, values_file=None, args=[])

    assert not result.ok
    assert result.steps_run == 0
    assert "exited with code 3" in result.problems[0]
    assert "the grid is not on screen" in result.problems[0]
    assert clicked == []


def test_an_optional_script_that_fails_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script_file(tmp_path, "raise SystemExit(2)")
    monkeypatch.setattr(form_steps, "inspect_window", lambda *a, **k: FakeInspection())
    monkeypatch.setattr(form_steps, "click_checked", lambda *a, **k: None)

    result = _run(
        tmp_path,
        script,
        values_file=None,
        args=[],
        steps=[
            ScriptStep(action="run", script=str(script), optional=True),
            ClickStep(action="click", button="Freight"),
        ],
    )

    assert result.ok, result.problems
    assert result.steps_run == 1
    assert any("skipped" in warning for warning in result.warnings)


def test_a_script_that_never_finishes_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script_file(tmp_path, "import time\ntime.sleep(30)")
    monkeypatch.setattr(form_steps, "inspect_window", lambda *a, **k: FakeInspection())

    result = _run(
        tmp_path,
        script,
        values_file=None,
        steps=[ScriptStep(action="run", script=str(script), timeout_seconds=0.4)],
    )

    assert not result.ok
    assert "still running after 0.4s" in result.problems[0]


def test_a_values_placeholder_without_a_values_file_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "ran.txt"
    script = _script_file(
        tmp_path,
        f'open(r"{marker}", "w", encoding="utf-8").write("ran")',
    )
    monkeypatch.setattr(form_steps, "inspect_window", lambda *a, **k: FakeInspection())

    result = _run(tmp_path, script, values_file=None)

    assert not result.ok
    assert "no values file" in result.problems[0]
    assert not marker.exists()


def test_a_script_that_is_not_there_is_a_step_failure(tmp_path: Path) -> None:
    command, complaint = script_command(
        ScriptStep(action="run", script="execution/workflows/nowhere.py")
    )

    assert command == []
    assert "there is no script at" in complaint


# ---------------------------------------------------------------- registration --


def test_a_run_step_is_resumed_only_by_restarting() -> None:
    assert RESUME_FOR["run"] == "restart"


def test_the_draft_keeps_the_script_as_a_run_step_on_the_screen_it_opens() -> None:
    config = AgentConfig.model_validate(
        {
            "app": {"process_name": "FACTERPNG.exe"},
            "fields": [{"name": "customer", "field": "Customer Name"}],
            "actions": {"save_button": "Save"},
            "workflow": {
                "steps": [
                    {"action": "click", "button": "&Add"},
                    {"action": "run", "script": SCRIPT, "args": ["{values_file}"]},
                    {"action": "click", "button": "Save"},
                ],
                "pause_after_click_seconds": 0,
            },
            "confirmation": {"verifier": "row", "footprint": ["customer"]},
        }
    )

    draft = draft_automation(config, name="sale_order", entry_state="04_view")

    assert [step.action for step in draft.steps] == ["click", "run"]
    assert draft.steps[1].script == SCRIPT
    assert draft.steps[1].checkpoint.resume == "restart"
    payload = _step_payload(
        ScriptStep(action="run", script=SCRIPT, args=["{values_file}"]),
        2,
        "05_add",
    )
    assert payload["id"] == "02-run-form-filler-1"
    assert payload["checkpoint"] == {"state": "05_add", "resume": "restart"}


class _Screen:
    def __init__(self, state: str = "04_view") -> None:
        self.state = state

    def __call__(self) -> StateMatch:
        return StateMatch(state_id=self.state)


class _Executor:
    """Moves the screen along and remembers what the proof was given."""

    def __init__(self, screen: _Screen, states: list[str]) -> None:
        self.screen = screen
        self.states = states
        self.values_file: Any = "not given"
        self.values_file_given = False

    def __call__(self, steps: list, values: dict, **options: Any) -> Any:
        from execution.actions.engine import PlanResult

        if "values_file" in options:
            self.values_file_given = True
            self.values_file = options["values_file"]
        before_step = options.get("before_step")
        for position in range(1, len(steps) + 1):
            self.screen.state = self.states[position - 1]
            if before_step is not None:
                before_step(position, steps[position - 1])
        return PlanResult(
            inspection=object(), fields={}, steps_run=len(steps), problems=()
        )


def test_registering_proves_the_script_from_a_clean_form(
    tmp_path: Path,
) -> None:
    screen = _Screen()
    executor = _Executor(screen, ["04_view", "05_add"])
    closed: list[str] = []
    config_path = tmp_path / "workflow.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {"process_name": "FACTERPNG.exe"},
                "fields": [{"name": "customer", "field": "Customer Name"}],
                "actions": {"save_button": "Save"},
                "workflow": {
                    "steps": [
                        {"action": "click", "button": "&Add"},
                        {"action": "run", "script": SCRIPT,
                         "args": ["{values_file}"]},
                        {"action": "click", "button": "Save"},
                    ],
                    "pause_after_click_seconds": 0,
                },
                "confirmation": {"verifier": "row", "footprint": ["customer"]},
            }
        ),
        encoding="utf-8",
    )
    values_file = tmp_path / "data.json"
    values_file.write_text("{}", encoding="utf-8")

    result = register_automation(
        name="sale_order",
        config_path=config_path,
        values={"customer": "Ram Bahadur"},
        values_file=values_file,
        automations_root=tmp_path,
        table=StateTable.model_validate(
            {
                "process": "FACTERPNG.exe",
                "states": [
                    {"id": "04_view", "match": {"auto_id": "40200"}},
                    {"id": "05_add", "match": {"auto_id": "40201"}},
                ],
            }
        ),
        detector=screen,
        attach=lambda config: _window(),
        inspect=lambda window: object(),
        execute=executor,
        close_form=lambda *a, **k: (closed.append("closed"), True)[1],
        save_call=lambda **options: SaveResult(
            outcome=ConfirmationResult(outcome=CONFIRMED, reason="the row is listed")
        ),
        footprint_for=lambda *a, **k: ["customer"],
        output=lambda _line: None,
        sleep=lambda _seconds: None,
    )

    assert result.status == REGISTERED, result.message
    assert closed == ["closed"], "the proof must start from a clean form"
    assert executor.values_file_given, "the script needs the run's values file"
    assert executor.values_file == values_file
    assert result.automation is not None
    assert result.automation.steps[1].action == "run"
    assert result.automation.steps[1].checkpoint.state == "05_add"


def test_a_plan_without_a_script_is_proved_without_closing_anything(
    tmp_path: Path,
) -> None:
    screen = _Screen()
    executor = _Executor(screen, ["04_view", "05_add"])
    closed: list[str] = []
    config_path = tmp_path / "workflow.json"
    config_path.write_text(
        json.dumps(
            {
                "app": {"process_name": "FACTERPNG.exe"},
                "fields": [{"name": "customer", "field": "Customer Name"}],
                "actions": {"save_button": "Save"},
                "workflow": {
                    "steps": [
                        {"action": "click", "button": "&Add"},
                        {"action": "fill", "key": "customer"},
                        {"action": "click", "button": "Save"},
                    ],
                    "pause_after_click_seconds": 0,
                },
                "confirmation": {"verifier": "row", "footprint": ["customer"]},
            }
        ),
        encoding="utf-8",
    )

    register_automation(
        name="sale_order",
        config_path=config_path,
        values={"customer": "Ram Bahadur"},
        automations_root=tmp_path,
        table=StateTable.model_validate(
            {
                "process": "FACTERPNG.exe",
                "states": [
                    {"id": "04_view", "match": {"auto_id": "40200"}},
                    {"id": "05_add", "match": {"auto_id": "40201"}},
                ],
            }
        ),
        detector=screen,
        attach=lambda config: _window(),
        inspect=lambda window: object(),
        execute=executor,
        close_form=lambda *a, **k: (closed.append("closed"), True)[1],
        save_call=lambda **options: SaveResult(
            outcome=ConfirmationResult(outcome=CONFIRMED, reason="the row is listed")
        ),
        footprint_for=lambda *a, **k: ["customer"],
        output=lambda _line: None,
        sleep=lambda _seconds: None,
    )

    assert closed == [], "a plan without a script is proved exactly as before"
