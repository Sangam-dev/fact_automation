"""One document, N rows, one Save: the per-row block and how it runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.automation import registry
from core.automation.model import Automation
from core.automation.progress import digest_values
from execution.workflows.automation_runner import run_automation
from execution.workflows.runner import RunnerError, split_rows

NAME = "fact_automation"
STATES = ["05_add"]


def plan(**overrides: Any) -> Automation:
    data = {
        "name": NAME,
        "app": {"title_contains": "FACT ERP.NG", "process_name": "FACTERPNG.exe"},
        "fields": [
            {"name": "customer", "field": "Customer Name"},
            {"name": "product_code", "field": "Product Code"},
            {"name": "quantity", "field": "Quantity"},
        ],
        "steps": [
            {
                "id": "01-fill-customer",
                "action": "fill",
                "key": "customer",
                "field": "Customer Name",
                "checkpoint": {"state": "05_add", "resume": "replay"},
            },
            {
                "id": "02-fill-product",
                "action": "fill",
                "key": "product_code",
                "field": "Product Code",
                "per_row": True,
                "checkpoint": {"state": "05_add", "resume": "replay"},
            },
            {
                "id": "03-fill-quantity",
                "action": "fill",
                "key": "quantity",
                "field": "Quantity",
                "per_row": True,
                "checkpoint": {"state": "05_add", "resume": "replay"},
            },
        ],
        "save": {"button": "Save", "verifier": "row", "footprint": ["customer"]},
    }
    data.update(overrides)
    return Automation.model_validate(data)


def test_row_keys_are_separate_from_record_keys() -> None:
    assert plan().fill_keys() == ["customer"]
    assert plan().row_keys() == ["product_code", "quantity"]


def test_split_steps_keeps_order_and_membership() -> None:
    once, rows = plan().split_steps()
    assert [step.id for step in once] == ["01-fill-customer"]
    assert [step.id for step in rows] == ["02-fill-product", "03-fill-quantity"]


def test_a_once_step_after_a_row_step_is_refused() -> None:
    steps = plan().model_dump(mode="json")["steps"]
    steps[2]["per_row"] = False  # a step that runs once after one that repeats
    with pytest.raises(Exception, match="per row"):
        Automation.model_validate(
            {**plan().model_dump(mode="json"), "steps": steps}
        )


def test_a_plan_without_rows_has_no_row_steps() -> None:
    plain = plan(
        steps=[
            {
                "id": "01-fill-customer",
                "action": "fill",
                "key": "customer",
                "checkpoint": {"state": "05_add", "resume": "replay"},
            }
        ]
    )
    assert plain.repeats_per_row() is False
    assert plain.split_steps()[1] == []


# ---------------------------------------------------------------- the values --
def test_rows_are_separated_from_the_header() -> None:
    header, rows = split_rows(
        {"customer": "Ram", "rows": [{"product_code": "P1", "quantity": 2}]}
    )
    assert header == {"customer": "Ram"}
    assert rows == [{"product_code": "P1", "quantity": "2"}]


def test_no_rows_means_an_empty_list() -> None:
    assert split_rows({"customer": "Ram"}) == ({"customer": "Ram"}, [])


def test_the_shape_the_form_filler_reads_means_the_same_document() -> None:
    # One file for the endpoint, the plan and the script: a document written as
    # {"header": {...}, "lines": [...]} is the flat document, not a second one.
    header, rows = split_rows(
        {
            "header": {"customer": "Ram", "order_date": "2026-11-19"},
            "lines": [{"product_code": "P1", "quantity": 2}],
        }
    )

    assert header == {"customer": "Ram", "order_date": "2026-11-19"}
    assert rows == [{"product_code": "P1", "quantity": "2"}]


def test_a_nested_header_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(RunnerError, match="must be an object of field"):
        split_rows({"header": "Ram", "lines": []})


def test_rows_must_be_a_list_of_objects() -> None:
    with pytest.raises(RunnerError, match="list of objects"):
        split_rows({"rows": "P1"})
    with pytest.raises(RunnerError, match="row 1 must be an object"):
        split_rows({"rows": ["P1"]})


def test_the_digest_covers_the_rows() -> None:
    one = digest_values({"customer": "Ram"}, [{"product_code": "P1"}])
    two = digest_values({"customer": "Ram"}, [{"product_code": "P2"}])
    assert one != two


# ------------------------------------------------------------- running it --
class Screen:
    def __call__(self):
        from desktop.state.recognizer import StateMatch

        return StateMatch(state_id="05_add")


class Executor:
    """Records each pass: the steps it was given and the values it was given."""

    def __init__(self) -> None:
        self.passes: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, steps: list, values: dict, **options: Any):
        from execution.actions.engine import PlanResult

        self.passes.append(
            ([getattr(step, "key", None) or step.button for step in steps], dict(values))
        )
        return PlanResult(inspection=object(), fields={}, steps_run=len(steps))


class Saver:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **options: Any):
        from execution.verification.confirmation import CONFIRMED, ConfirmationResult
        from execution.workflows.runner import SaveResult

        self.calls.append(options)
        return SaveResult(outcome=ConfirmationResult(CONFIRMED, "row is on screen"))


def _table():
    from desktop.state.states import StateTable

    return StateTable.model_validate(
        {"process": "FACTERPNG.exe", "states": [{"id": "05_add", "match": {"auto_id": "1"}}]}
    )


VALUES = {"customer": "Ram"}
ROWS = [
    {"product_code": "P1", "quantity": "1"},
    {"product_code": "P2", "quantity": "2"},
]


def test_the_header_runs_once_and_the_rows_once_each(tmp_path: Path) -> None:
    executor, saver = Executor(), Saver()
    registry.save(plan(), tmp_path / "automations")

    run = run_automation(
        NAME,
        VALUES,
        rows=ROWS,
        automations_root=tmp_path / "automations",
        progress_root=tmp_path / "progress",
        table=_table(),
        detector=Screen(),
        attach=lambda app, **kwargs: object(),
        inspect=lambda window: object(),
        execute=executor,
        save_call=saver,
        clear_form=lambda *a, **k: True,
        output=lambda line: None,
        sleep=lambda seconds: None,
        checkpoint_seconds=0,
    )

    assert run.status == "saved", run.message
    # header pass, then one pass per row
    assert [pass_[0] for pass_ in executor.passes] == [
        ["customer"],
        ["product_code", "quantity"],
        ["product_code", "quantity"],
    ]
    assert executor.passes[1][1] == {"customer": "Ram", "product_code": "P1", "quantity": "1"}
    assert executor.passes[2][1] == {"customer": "Ram", "product_code": "P2", "quantity": "2"}
    assert len(saver.calls) == 1, "one document, one Save"
    assert run.steps_run == 5


def test_no_rows_refuses_a_plan_that_repeats(tmp_path: Path) -> None:
    executor, saver = Executor(), Saver()
    registry.save(plan(), tmp_path / "automations")
    result = run_automation(
        NAME,
        VALUES,
        automations_root=tmp_path / "automations",
        progress_root=tmp_path / "progress",
        table=_table(),
        detector=Screen(),
        execute=executor,
        save_call=saver,
        output=lambda line: None,
    )
    assert result.status == "blocked"
    assert "rows" in result.message
    assert executor.passes == []


def test_a_row_missing_a_value_is_refused_before_anything_runs(tmp_path: Path) -> None:
    executor, saver = Executor(), Saver()
    registry.save(plan(), tmp_path / "automations")
    result = run_automation(
        NAME,
        VALUES,
        rows=[{"product_code": "P1"}],
        automations_root=tmp_path / "automations",
        progress_root=tmp_path / "progress",
        table=_table(),
        detector=Screen(),
        execute=executor,
        save_call=saver,
        output=lambda line: None,
    )
    assert result.status == "blocked"
    assert "row 1" in result.message
    assert "'quantity'" in result.message
    assert executor.passes == []


def test_a_plan_without_row_steps_refuses_rows(tmp_path: Path) -> None:
    registry.save(
        plan(
            steps=[
                {
                    "id": "01-fill-customer",
                    "action": "fill",
                    "key": "customer",
                    "checkpoint": {"state": "05_add", "resume": "replay"},
                }
            ]
        ),
        tmp_path / "automations",
    )
    result = run_automation(
        NAME,
        VALUES,
        rows=ROWS,
        automations_root=tmp_path / "automations",
        progress_root=tmp_path / "progress",
        table=_table(),
        detector=Screen(),
        execute=Executor(),
        save_call=Saver(),
        output=lambda line: None,
    )
    assert result.status == "blocked"
    assert "no steps that repeat per row" in result.message


def test_a_script_given_the_values_file_enters_the_rows_itself(
    tmp_path: Path,
) -> None:
    """The filler reads the lines from the document, so the plan needs no rows."""
    seen: list[tuple[list[str], Any]] = []

    def executor(steps: list, values: dict, **options: Any):
        from execution.actions.engine import PlanResult

        seen.append(
            (
                [step.action for step in steps],
                options.get("values_file"),
                options.get("settle_seconds"),
            )
        )
        return PlanResult(inspection=object(), fields={}, steps_run=len(steps))

    registry.save(
        plan(
            steps=[
                {
                    "id": "01-run-filler",
                    "action": "run",
                    "script": "execution/scripts/form_filler_1.py",
                    "args": ["{values_file}"],
                    "checkpoint": {"state": "05_add", "resume": "restart"},
                }
            ]
        ),
        tmp_path / "automations",
    )
    result = run_automation(
        NAME,
        VALUES,
        rows=ROWS,
        values_file="order.json",
        automations_root=tmp_path / "automations",
        progress_root=tmp_path / "progress",
        table=_table(),
        detector=Screen(),
        attach=lambda app, **kwargs: object(),
        inspect=lambda window: object(),
        execute=executor,
        save_call=Saver(),
        clear_form=lambda *a, **k: True,
        output=lambda line: None,
        sleep=lambda seconds: None,
    )
    assert result.status == "saved", result.message
    assert seen == [(["run"], "order.json", 15.0)], "once, with the file, patient"


def test_a_step_that_cannot_find_its_control_says_which_screen_is_up(
    tmp_path: Path,
) -> None:
    """"no match in the live window" is useless on its own: name the screen."""
    registry.save(
        plan(
            steps=[
                {
                    "id": "01-click-open",
                    "action": "click",
                    "button": "Open",
                    "checkpoint": {"state": "05_add", "resume": "replay"},
                }
            ]
        ),
        tmp_path / "automations",
    )
    def missing_control(steps: list, values: dict, **options: Any):
        from execution.actions.engine import PlanResult

        return PlanResult(
            inspection=object(),
            fields={},
            problems=("step 1 (click 'Open'): no match in the live window",),
        )

    result = run_automation(
        NAME,
        VALUES,
        automations_root=tmp_path / "automations",
        progress_root=tmp_path / "progress",
        table=_table(),
        detector=Screen(),
        attach=lambda app, **kwargs: object(),
        inspect=lambda window: object(),
        execute=missing_control,
        save_call=Saver(),
        clear_form=lambda *a, **k: True,
        output=lambda line: None,
        sleep=lambda seconds: None,
        checkpoint_seconds=0,
    )
    assert result.status == "failed"
    assert "the screen on display is 05_add" in result.message
