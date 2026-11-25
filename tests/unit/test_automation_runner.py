"""The automation runner: resume decisions, checkpoints, and the save guard.

Everything here is offline. The runner's collaborators - the recogniser, the
window, the form filler, the save - are arguments, so the order of operations is
tested without a Windows session.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.automation import progress as store
from core.automation import registry
from core.automation.model import Automation
from core.config import AgentConfig
from core.runtime.errors import RunnerAborted
from desktop.state.recognizer import CLOSED, StateMatch, WindowView
from desktop.state.states import StateTable
from execution.actions.engine import PlanResult
from execution.verification.confirmation import CONFIRMED, ConfirmationResult
from execution.workflows.automation_runner import (
    BLOCKED,
    DEFAULT_SAVE_STEP,
    FAILED,
    SAVED,
    STOPPED,
    UNCONFIRMED,
    run_automation,
)
from execution.workflows.runner import SaveResult

NAME = "fact_automation"
VALUES = {"customer": "Ram Bahadur"}

#: Open the list, open the form, type the customer. Save is not a step: it is
#: the plan's ``save`` spec, clicked once by the runner.
STEPS = [
    {
        "id": "01-open",
        "action": "click",
        "button": "&Add",
        "checkpoint": {"state": "04_view", "resume": "replay"},
    },
    {
        "id": "02-lines",
        "action": "click",
        "button": "Lines",
        "checkpoint": {"state": "05_add", "resume": "replay"},
    },
    {
        "id": "03-customer",
        "action": "fill",
        "key": "customer",
        "checkpoint": {"state": "05_add", "resume": "restart"},
    },
]


def payload(**overrides: Any) -> dict:
    data = {
        "name": NAME,
        "app": {"title_contains": "FACT ERP.NG", "process_name": "FACTERPNG.exe"},
        "fields": [{"name": "customer", "field": "Customer Name"}],
        "steps": [dict(step) for step in STEPS],
        "save": {"button": "Save", "verifier": "row", "footprint": ["customer"]},
    }
    data.update(overrides)
    return data


def table(*ids: str) -> StateTable:
    return StateTable.model_validate(
        {
            "process": "FACTERPNG.exe",
            "states": [{"id": state, "match": {"auto_id": "40200"}} for state in ids],
        }
    )


class Screen:
    """A recogniser whose answer the executor moves along as steps run."""

    def __init__(self, state: str = "04_view") -> None:
        self.state = state
        self.calls = 0

    def __call__(self) -> StateMatch:
        self.calls += 1
        return StateMatch(state_id=self.state)

    def set(self, state: str) -> None:
        self.state = state


class Executor:
    """Runs the plan the way the engine does: precondition, action, record."""

    def __init__(
        self,
        screen: Screen | None = None,
        *,
        states: list[str] | None = None,
        abort_at: int | None = None,
        problems: tuple[str, ...] = (),
    ) -> None:
        self.screen = screen
        self.states = states or ["04_view", "05_add", "05_add"]
        self.abort_at = abort_at
        self.problems = problems
        self.start_index: int | None = None
        self.positions: list[int] = []
        self.called = False

    def __call__(self, steps: list, values: dict, **options: Any) -> PlanResult:
        self.called = True
        self.start_index = options.get("start_index", 0)
        before_step = options.get("before_step")
        on_step = options.get("on_step")
        ran = 0
        for position in range(self.start_index + 1, len(steps) + 1):
            if self.abort_at == position:
                raise RunnerAborted("stopped by the user")
            # The previous step produced this screen, so it is on display when
            # this step's precondition is checked.
            if self.screen is not None:
                self.screen.set(self.states[position - 1])
            if before_step is not None:
                before_step(position, steps[position - 1])
            if on_step is not None:
                on_step(position, steps[position - 1])
            self.positions.append(position)
            ran += 1
        return PlanResult(
            inspection=object(), fields={}, steps_run=ran, problems=self.problems
        )


def saved_ok() -> SaveResult:
    return SaveResult(
        outcome=ConfirmationResult(
            outcome=CONFIRMED, reason="the row appeared", source="window text"
        )
    )


def saved_unknown() -> SaveResult:
    return SaveResult(
        outcome=ConfirmationResult(
            outcome="unknown", reason="could not read the window", source="window text"
        )
    )


def saved_unreachable() -> SaveResult:
    return SaveResult(
        outcome=ConfirmationResult(
            outcome="unreachable", reason="the Save button was not on screen"
        )
    )


def saved_aborted() -> SaveResult:
    return SaveResult(
        outcome=ConfirmationResult(
            outcome="aborted", reason="the run was stopped before Save", source="user"
        )
    )


class Saver:
    def __init__(self, result: SaveResult | None = None, raises: Exception | None = None):
        self.result = result if result is not None else saved_ok()
        self.raises = raises
        self.called = False

    def __call__(self, **options: Any) -> SaveResult:
        self.called = True
        if self.raises is not None:
            raise self.raises
        return self.result


class ClearForm:
    def __init__(self, closes: bool = True) -> None:
        self.closes = closes
        self.calls = 0

    def __call__(self, window: Any, config: AgentConfig, **options: Any) -> bool:
        self.calls += 1
        return self.closes


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "automations", tmp_path / "progress"


@pytest.fixture
def registered(roots) -> tuple[Path, Path]:
    automations, progress = roots
    registry.save(Automation.model_validate(payload()), automations)
    return automations, progress


def go(
    roots: tuple[Path, Path],
    *,
    screen: Screen | None = None,
    executor: Executor | None = None,
    saver: Any = None,
    clear: ClearForm | None = None,
    attach_fn: Any = None,
    values: dict | None = None,
    states: tuple[str, ...] = ("04_view", "05_add"),
    **options: Any,
):
    automations, progress = roots
    screen = screen or Screen("04_view")
    executor = executor or Executor()
    if executor.screen is None:
        executor.screen = screen
    saver = saver or Saver()
    clear = clear or ClearForm()
    run = run_automation(
        NAME,
        dict(values if values is not None else VALUES),
        automations_root=automations,
        progress_root=progress,
        table=table(*states),
        detector=screen,
        attach=attach_fn or (lambda config, **kwargs: object()),
        inspect=lambda window: object(),
        clear_form=clear,
        execute=executor,
        save_call=saver,
        output=lambda line: None,
        sleep=lambda seconds: None,
        **options,
    )
    return run, executor, saver, clear


def save(progress_root: Path, item: store.ProgressState) -> None:
    store.save_progress(item, progress_root)


# ---------------------------------------------------------------- a fresh run --
def test_starting_on_a_form_keeps_its_window_for_initial_attach(registered) -> None:
    class FormScreen(Screen):
        def __call__(self) -> StateMatch:
            self.calls += 1
            return StateMatch(
                state_id=self.state,
                window=WindowView(title="Sale Order - Add", handle=42),
            )

    screen = FormScreen("05_add")
    attached: dict[str, Any] = {}

    def attach(_config, **kwargs):
        attached.update(kwargs)
        return object()

    run, _executor, _saver, _clear = go(
        registered,
        screen=screen,
        attach_fn=attach,
        from_screen=True,
    )

    assert run.status == SAVED
    assert attached["expected_state"] == "05_add"
    assert attached["expected_handle"] == 42


def test_a_fresh_run_saves_the_record(registered) -> None:
    run, executor, saver, _clear = go(registered)

    assert run.status == SAVED
    assert run.exit_code == 0
    assert executor.start_index == 0
    assert saver.called
    assert run.progress is not None
    assert run.progress.outcome == store.OUTCOME_SAVED
    assert run.progress.pending_save is None


def test_progress_is_written_after_every_step(registered) -> None:
    run, _executor, _saver, _clear = go(registered)
    _automations, progress_root = registered
    item = store.load_progress(NAME, progress_root)

    assert item is not None
    assert item.cursor == 3
    assert item.completed == ["01-open", "02-lines", "03-customer"]
    assert item.run_id == run.progress.run_id


def test_a_stale_form_is_closed_before_a_fresh_start(registered) -> None:
    _run, _executor, _saver, clear = go(registered)
    assert clear.calls == 1


def test_a_form_that_will_not_close_stops_everything(registered) -> None:
    run, executor, saver, _clear = go(registered, clear=ClearForm(closes=False))

    assert run.status == FAILED
    assert "would not close" in run.message
    assert executor.called is False
    assert saver.called is False


# ------------------------------------------------------------------ resuming --
def test_a_fill_is_never_started_in_place(registered) -> None:
    _automations, progress_root = registered
    item = store.new_progress(NAME, VALUES)
    item.mark_step(0, "01-open")
    item.mark_step(1, "02-lines")
    save(progress_root, item)

    run, executor, _saver, clear = go(registered, screen=Screen("05_add"))

    # The cursor is on a fill (step 3): it cannot be resumed where it stopped, so
    # the run starts at step 2, the click that is safe to replay on this screen.
    assert executor.start_index == 1
    assert executor.positions == [2, 3]
    assert run.status == SAVED
    assert clear.calls == 0, "the form is still open and holds no typed values"


def test_a_run_resumes_in_place_on_a_step_that_is_safe_to_replay(registered) -> None:
    _automations, progress_root = registered
    item = store.new_progress(NAME, VALUES)
    item.mark_step(0, "01-open")
    save(progress_root, item)

    run, executor, _saver, clear = go(registered, screen=Screen("05_add"))

    assert executor.start_index == 1
    assert executor.positions == [2, 3]
    assert clear.calls == 0, "resuming mid-form must not close the form"
    assert run.progress.cursor == 3


def test_a_run_that_lost_its_form_starts_from_the_top(registered) -> None:
    _automations, progress_root = registered
    item = store.new_progress(NAME, VALUES)
    item.mark_step(0, "01-open")
    item.mark_step(1, "02-lines")
    save(progress_root, item)

    # Step 3 belongs on the open form, but the list screen is showing: the form
    # was closed, so everything has to be entered again from step 1.
    run, executor, _saver, clear = go(registered, screen=Screen("04_view"))

    assert executor.start_index == 0
    assert clear.calls == 1
    assert run.status == SAVED


def test_from_screen_starts_at_the_step_the_screen_belongs_to(registered) -> None:
    # The form is already open when the command starts, so the run begins on the
    # step that belongs there instead of replaying the clicks that got there.
    run, executor, _saver, clear = go(
        registered, screen=Screen("05_add"), from_screen=True
    )

    assert executor.start_index == 1
    assert executor.positions == [2, 3]
    assert clear.calls == 0, "a form the operator opened is not cleared"
    assert run.status == SAVED


def test_a_plan_without_a_save_check_finishes_when_it_clicks(roots) -> None:
    # The app refuses to write (a trial build), so the plan was registered with
    # the save unchecked: the click is the end of the job, and the next record
    # must not be stopped by a save nobody could ever resolve.
    automations, progress_root = roots
    data = payload()
    data["save"] = {
        "button": "Save",
        "verifier": "none",
        "footprint": [],
        "check": False,
    }
    registry.save(Automation.model_validate(data), automations)

    run, _executor, saver, _clear = go((automations, progress_root), saver=Saver(saved_unknown()))

    assert saver.called, "the Save click still happens"
    assert run.status == SAVED
    assert run.exit_code == 0
    assert "does not check" in run.message
    assert run.progress is not None
    assert run.progress.save_unresolved is False


def test_without_the_flag_the_screen_does_not_choose_the_step(registered) -> None:
    _run, executor, _saver, _clear = go(registered, screen=Screen("05_add"))

    assert executor.start_index == 0


def test_different_values_are_refused(registered) -> None:
    _automations, progress_root = registered
    item = store.new_progress(NAME, {"customer": "Sita"})
    item.mark_step(0, "01-open")
    save(progress_root, item)

    run, executor, _saver, _clear = go(registered, screen=Screen("05_add"))

    assert run.status == BLOCKED
    assert run.exit_code == 2
    assert "--restart" in run.message
    assert executor.called is False


def test_changed_values_start_a_new_record_when_no_form_is_on_screen(registered) -> None:
    # The values file was edited while the app sat on a screen that holds no form:
    # there is nothing half-typed to protect, so the run must begin the new record
    # instead of demanding a command-line flag a double-click user cannot type.
    _automations, progress_root = registered
    item = store.new_progress(NAME, {"customer": "Sita"})
    item.mark_step(0, "01-open")
    save(progress_root, item)

    run, executor, _saver, _clear = go(registered, screen=Screen("04_view"))

    assert run.status == SAVED
    assert executor.called is True
    assert executor.start_index == 0
    assert run.progress is not None
    assert run.progress.values_digest != item.values_digest, "the new data, not the old record"
    assert run.progress.completed == ["01-open", "02-lines", "03-customer"]


# ------------------------------------------------------------- the save guard --
def test_an_unresolved_save_blocks_the_next_run(registered) -> None:
    _automations, progress_root = registered
    item = store.new_progress(NAME, VALUES)
    item.mark_step(0, "01-open")
    item.begin_save("save")
    save(progress_root, item)

    run, executor, _saver, _clear = go(registered, screen=Screen("05_add"))

    assert run.status == BLOCKED
    assert "--saved-already" in run.message and "--not-saved" in run.message
    assert executor.called is False


def test_saved_already_finishes_the_run_without_executing_anything(registered) -> None:
    _automations, progress_root = registered
    item = store.new_progress(NAME, VALUES)
    item.begin_save("save")
    save(progress_root, item)

    run, executor, saver, _clear = go(
        registered, screen=Screen("05_add"), saved_already=True
    )

    assert run.status == SAVED
    assert run.exit_code == 0
    assert executor.called is False and saver.called is False
    stored = store.load_progress(NAME, progress_root)
    assert stored is not None
    assert stored.pending_save is None
    assert stored.outcome == store.OUTCOME_SAVED


def test_not_saved_clears_the_marker_and_runs(registered) -> None:
    _automations, progress_root = registered
    item = store.new_progress(NAME, VALUES)
    item.mark_step(0, "01-open")
    item.begin_save("save")
    save(progress_root, item)

    run, executor, saver, _clear = go(
        registered, screen=Screen("05_add"), not_saved=True
    )

    assert run.status == SAVED
    assert executor.called and saver.called


def test_flags_without_a_pending_save_are_refused(registered) -> None:
    run, executor, _saver, _clear = go(registered, saved_already=True)
    assert run.status == BLOCKED
    assert "no unfinished save" in run.message
    assert executor.called is False


def test_a_saved_run_is_not_repeated(registered) -> None:
    go(registered)

    run, executor, _saver, _clear = go(registered)

    assert run.status == BLOCKED
    assert "--restart" in run.message
    assert executor.called is False


def test_restart_forgets_the_last_run(registered) -> None:
    go(registered)

    run, executor, _saver, _clear = go(registered, restart=True)

    assert run.status == SAVED
    assert executor.called


def test_restart_cannot_be_combined_with_the_save_flags(registered) -> None:
    run, executor, _saver, _clear = go(registered, restart=True, not_saved=True)
    assert run.status == BLOCKED
    assert "cannot be combined" in run.message
    assert executor.called is False


def test_the_marker_is_written_before_save_is_clicked(registered) -> None:
    _automations, progress_root = registered
    seen: dict[str, Any] = {}

    def saver(**options: Any) -> SaveResult:
        # Read the progress file at the moment Save is about to be clicked.
        seen["progress"] = store.load_progress(NAME, progress_root)
        return saved_ok()

    run, _executor, _saver, _clear = go(registered, saver=saver)

    assert run.ok
    pending = seen["progress"]
    assert pending is not None
    assert pending.pending_save is not None, "the marker must exist before the click"
    assert pending.pending_save.step_id == DEFAULT_SAVE_STEP


def test_an_unconfirmed_save_leaves_the_run_blocked(registered) -> None:
    _automations, progress_root = registered
    run, _executor, _saver, _clear = go(registered, saver=Saver(saved_unknown()))

    assert run.status == UNCONFIRMED
    assert run.exit_code == 1
    stored = store.load_progress(NAME, progress_root)
    assert stored is not None
    assert stored.save_unresolved is True

    # ... and the next run refuses to click Save a second time.
    again, executor, _saver2, _clear2 = go(registered, screen=Screen("05_add"))
    assert again.status == BLOCKED
    assert executor.called is False


def test_a_missing_save_button_does_not_leave_a_marker(registered) -> None:
    _automations, progress_root = registered
    run, _executor, _saver, _clear = go(registered, saver=Saver(saved_unreachable()))

    assert run.status == FAILED
    stored = store.load_progress(NAME, progress_root)
    assert stored is not None
    assert stored.save_unresolved is False
    assert stored.pending_save is None


def test_a_save_stopped_before_click_does_not_block_the_next_run(registered) -> None:
    _automations, progress_root = registered
    run, _executor, saver, _clear = go(registered, saver=Saver(saved_aborted()))

    assert saver.called
    assert run.status == STOPPED
    stored = store.load_progress(NAME, progress_root)
    assert stored is not None
    assert stored.save_unresolved is False
    assert stored.pending_save is None


def test_a_save_that_blows_up_leaves_the_marker_set(registered) -> None:
    _automations, progress_root = registered
    run, _executor, _saver, _clear = go(registered, saver=Saver(raises=OSError("boom")))

    assert run.status == UNCONFIRMED
    stored = store.load_progress(NAME, progress_root)
    assert stored is not None
    assert stored.save_unresolved is True


# -------------------------------------------------------------- checkpoints --
def test_a_checkpoint_that_does_not_match_stops_the_run_before_the_step(
    registered,
) -> None:
    _automations, progress_root = registered
    executor = Executor(states=["04_view", "04_view", "05_add"])

    run, executor, saver, _clear = go(registered, executor=executor)

    assert run.status == FAILED
    assert "step 2" in run.message and "05_add" in run.message
    assert saver.called is False

    # Nothing was done for the step that failed its precondition, and the step
    # before it is recorded: a re-run resumes exactly here.
    stored = store.load_progress(NAME, progress_root)
    assert stored is not None
    assert stored.cursor == 1
    assert stored.completed == ["01-open"]


def test_a_navigation_click_is_not_judged_by_the_screen_it_leaves_behind(
    registered,
) -> None:
    # Step 1 runs on the list and the click opens the form; that is the normal
    # case, not a checkpoint failure.
    run, executor, _saver, _clear = go(registered)
    assert run.status == SAVED
    assert executor.positions == [1, 2, 3]


# -------------------------------------------------------------------- the wait --
class SlowScreen(Screen):
    """Answers the old screen for a while, then the new one: a slow login."""

    def __init__(self, after: int, final: str = "05_add") -> None:
        super().__init__("04_view")
        self.after = after
        self.final = final

    def __call__(self) -> StateMatch:
        self.calls += 1
        return StateMatch(state_id=self.state if self.calls <= self.after else self.final)


def slow_runner(registered, screen: SlowScreen, slept: list[float], **options: Any):
    """Run only as far as step 2's checkpoint, with the real waiting logic."""
    automations, progress = registered

    def execute(steps: list, values: dict, **call_options: Any) -> PlanResult:
        before_step = call_options["before_step"]
        before_step(1, steps[0])
        before_step(2, steps[1])
        return PlanResult(inspection=object(), fields={}, steps_run=2)

    return run_automation(
        NAME,
        dict(VALUES),
        automations_root=automations,
        progress_root=progress,
        table=table("04_view", "05_add"),
        detector=screen,
        attach=lambda config, **kwargs: object(),
        inspect=lambda window: object(),
        execute=execute,
        save_call=Saver(),
        clear_form=lambda *a, **k: True,
        output=lambda line: None,
        sleep=slept.append,
        **options,
    )


def test_a_plan_carries_its_own_screen_wait(registered) -> None:
    automations, _progress = registered
    assert registry.load(NAME, automations).wait_seconds == 15.0

    registry.save(Automation.model_validate(payload(wait_seconds=45)), automations)
    assert registry.load(NAME, automations).wait_seconds == 45.0


def test_a_screen_slower_than_three_seconds_is_still_waited_for(registered) -> None:
    # Twenty polls at the 0.3s interval is 6.3s: twice the old fixed wait, well
    # inside the plan's own default. This is the Login-window case.
    run = slow_runner(registered, SlowScreen(after=20), [])

    assert run.status == SAVED, run.message


def test_the_plan_wait_is_the_budget_a_checkpoint_gets(registered) -> None:
    automations, _progress = registered
    registry.save(Automation.model_validate(payload(wait_seconds=2)), automations)
    slept: list[float] = []

    run = slow_runner(registered, SlowScreen(after=50), slept)

    assert run.status == FAILED
    assert "04_view" in run.message
    # 2 seconds at a 0.3s interval: it genuinely waited its budget, not forever.
    assert 5 <= len(slept) <= 8, len(slept)


def test_the_checkpoint_waits_for_a_slow_screen() -> None:
    calls = {"n": 0}

    def detector() -> StateMatch:
        calls["n"] += 1
        # The form finishes drawing a moment after the click that opened it.
        return StateMatch(state_id="04_view" if calls["n"] < 3 else "05_add")

    from execution.workflows.automation_runner import _wait_for_state

    match = _wait_for_state(
        detector,
        "05_add",
        seconds=3.0,
        interval=0.1,
        sleep=lambda seconds: None,
    )
    assert match.state_id == "05_add"
    assert calls["n"] == 3


def test_a_stop_leaves_the_cursor_where_it_was(registered) -> None:
    _automations, progress_root = registered
    run, _executor, saver, _clear = go(registered, executor=Executor(abort_at=3))

    assert run.status == STOPPED
    assert run.exit_code == 3
    assert saver.called is False
    stored = store.load_progress(NAME, progress_root)
    assert stored is not None
    assert stored.cursor == 2
    assert stored.outcome == store.OUTCOME_ABORTED


def test_failed_steps_are_reported_without_saving(registered) -> None:
    run, _executor, saver, _clear = go(
        registered, executor=Executor(problems=("step 3 ('Customer Name'): no match",))
    )
    assert run.status == FAILED
    assert "no match" in run.message
    assert saver.called is False


# ------------------------------------------------------------------- refusals --
def test_an_automation_that_is_not_registered_is_refused(roots) -> None:
    run, executor, _saver, _clear = go(roots)
    assert run.status == BLOCKED
    assert "register" in run.message
    assert executor.called is False


def test_an_automation_expecting_an_unknown_state_is_refused(registered) -> None:
    run, executor, _saver, _clear = go(registered, states=("04_view",))
    assert run.status == BLOCKED
    assert "05_add" in run.message
    assert executor.called is False


def test_the_app_not_running_is_a_refusal_not_a_failure(registered) -> None:
    run, executor, _saver, _clear = go(registered, screen=Screen(CLOSED))
    assert run.status == BLOCKED
    assert run.exit_code == 2
    assert "not running" in run.message
    assert executor.called is False


def test_missing_values_are_refused_before_anything_moves(registered) -> None:
    run, executor, _saver, _clear = go(registered, values={})
    assert run.status == BLOCKED
    assert "'customer'" in run.message
    assert executor.called is False
