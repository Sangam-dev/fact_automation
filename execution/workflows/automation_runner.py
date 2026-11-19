"""Run one saved automation: recognise the screen, decide where to start, go.

This is the layer the wizard's ``run_automation`` verb and ``automation run``
both call. It owns the order that makes a resume safe, and nothing else:

1. load the plan and refuse it if a checkpoint names a screen the state table
   does not define (before any click);
2. settle any *unfinished Save* first - a previous run that died between the
   click and the confirmation is a question for a person, not a retry;
3. ask the recogniser what the app is showing now, and let
   :func:`core.automation.resume.plan_resume` choose the first step that is both
   safe from here and belongs here;
4. execute from that step, checking each step's screen before it runs and
   writing progress after every step that finished;
5. write the Save marker *before* clicking Save, and clear it only once the
   save is proved - so a crash leaves evidence instead of ambiguity.

No model is called anywhere in this file.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from core.automation.model import Automation, AutomationError
from core.automation.progress import (
    OUTCOME_ABORTED,
    OUTCOME_FAILED,
    OUTCOME_SAVED,
    ProgressError,
    ProgressState,
    clear_progress,
    digest_values,
    load_progress,
    new_progress,
    save_progress,
)
from core.automation.registry import (
    AutomationNotFound,
    load as load_automation,
)
from core.automation.resume import ResumePlan, plan_resume
from core.config import AgentConfig
from core.runtime.errors import RunnerAborted
from core.safety.controller import SafetyController
from desktop.state.recognizer import StateMatch, detect
from desktop.state.states import StateTable, StateTableError, load_state_table
from desktop.types import WindowInfo
from desktop.uia.uia import UIAError, UIAInspection, inspect_window
from desktop.windows.window import WindowError, attach_window
from execution.actions.engine import (
    PlanResult,
    clear_open_form,
    execute_form_steps,
)
from execution.actions.models import FormStep
from execution.verification.confirmation import SaveConfirmer
from execution.workflows.runner import (
    FAILED,
    SAVED,
    UNCONFIRMED,
    SaveResult,
    save_and_verify,
)

#: Nothing was attempted, so there is nothing to undo - but a person has to
#: decide something (open the app, resolve a save, restart on purpose).
BLOCKED = "blocked"
#: The user pressed stop; the cursor stays where it is and a re-run continues.
STOPPED = "stopped"

EXIT_CODES = {SAVED: 0, UNCONFIRMED: 1, FAILED: 1, BLOCKED: 2, STOPPED: 3}

#: The save button's own step id when the plan has no explicit save click.
DEFAULT_SAVE_STEP = "save"


class CheckpointError(RuntimeError):
    """A step ran, but the screen it promised to reach is not there."""


@dataclass(frozen=True)
class AutomationRun:
    """What one ``automation run`` did, and what it costs to exit with it."""

    status: str
    automation: str
    message: str = ""
    start_index: int = 0
    steps_run: int = 0
    resume: ResumePlan | None = None
    progress: ProgressState | None = None
    save: SaveResult | None = None
    screenshots: tuple[Path, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.status == SAVED

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.status, 1)


def _now() -> datetime:
    return datetime.now()


def _blocked(
    name: str,
    message: str,
    *,
    progress: ProgressState | None = None,
    resume: ResumePlan | None = None,
) -> AutomationRun:
    return AutomationRun(
        status=BLOCKED,
        automation=name,
        message=message,
        resume=resume,
        progress=progress,
    )


def _wait_for_state(
    detect_now: Callable[[], StateMatch],
    expected: str,
    *,
    seconds: float,
    interval: float,
    sleep: Callable[[float], None],
) -> StateMatch:
    """Read the screen until it shows ``expected``, or the time runs out."""
    match = detect_now()
    waited = 0.0
    while match.state_id != expected and waited < seconds:
        step = interval if interval > 0 else seconds
        sleep(step)
        waited += step
        match = detect_now()
    return match


def _save_step_id(plan: Automation, config: AgentConfig) -> str:
    """The id of the step that clicks Save, if the plan contains one."""
    for step in reversed(plan.steps):
        if step.action == "click" and step.button == config.actions.save_button:
            return step.id
    return DEFAULT_SAVE_STEP


def run_automation(
    name: str,
    values: dict[str, str],
    *,
    automations_root: str | Path | None = None,
    progress_root: str | Path | None = None,
    table: StateTable | None = None,
    detector: Callable[[], StateMatch] | None = None,
    saved_already: bool = False,
    not_saved: bool = False,
    restart: bool = False,
    confirmation: SaveConfirmer | None = None,
    controller: SafetyController | None = None,
    screenshot_dir: str | Path | None = None,
    record_label: str = "record",
    checkpoint_seconds: float = 3.0,
    checkpoint_interval: float = 0.3,
    output: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    attach: Callable[[AgentConfig], WindowInfo] = attach_window,
    inspect: Callable[[WindowInfo], UIAInspection] = inspect_window,
    clear_form: Callable[..., bool] = clear_open_form,
    execute: Callable[..., PlanResult] = execute_form_steps,
    save_call: Callable[..., SaveResult] = save_and_verify,
) -> AutomationRun:
    """Run one automation by name, resuming where the last run stopped."""
    try:
        plan = load_automation(name, automations_root)
    except (AutomationError, AutomationNotFound) as exc:
        return _blocked(name, str(exc))

    try:
        table = table if table is not None else load_state_table()
        plan.validate_states(table.ids())
    except AutomationError as exc:
        return _blocked(name, str(exc))
    except StateTableError as exc:
        return _blocked(name, f"the state table cannot be used: {exc}")

    missing = [key for key in plan.fill_keys() if key not in values]
    if missing:
        return _blocked(
            name,
            "the values are missing field(s): "
            + ", ".join(repr(key) for key in missing),
        )

    config = plan.to_config()
    detect_now = detector or (lambda: detect(table))

    try:
        item = load_progress(name, progress_root)
    except ProgressError as exc:
        return _blocked(name, str(exc))

    flags = saved_already or not_saved
    if restart:
        if flags:
            return _blocked(
                name,
                "--restart cannot be combined with --saved-already or --not-saved: "
                "one starts the record again, the other finishes the last attempt",
                progress=item,
            )
        try:
            clear_progress(name, progress_root)
        except ProgressError as exc:
            return _blocked(name, str(exc))
        item = None
        output(f"  {name}: progress cleared; the record will be entered again")
    elif item is not None and item.pending_save is not None:
        if saved_already:
            item.resolve_save(True, _now())
        elif not_saved:
            item.resolve_save(False, _now())
        else:
            return _blocked(name, item.block_reason(), progress=item)
        save_progress(item, progress_root)
        output(
            "  the unfinished save was recorded as "
            + ("done" if saved_already else "not done")
        )
        if saved_already:
            return AutomationRun(
                status=SAVED,
                automation=name,
                message=(
                    f"run {item.run_id} was recorded as saved; nothing was "
                    "executed, and the record is not entered twice"
                ),
                progress=item,
            )
    elif flags:
        return _blocked(
            name,
            "there is no unfinished save to resolve; --saved-already and "
            "--not-saved only apply to a run that stopped at Save",
            progress=item,
        )

    # A completed run is finished work, not a starting point: replaying it would
    # enter the same record a second time.
    if item is not None and item.outcome == OUTCOME_SAVED:
        return _blocked(
            name,
            f"run {item.run_id} already saved this automation's record; "
            "re-run with --restart to enter it again",
            progress=item,
        )

    digest = digest_values(values)
    if item is None:
        item = new_progress(name, values, when=_now())

    screen = detect_now()
    if not screen.known:
        return _blocked(
            name,
            f"cannot start: {screen.message()}",
            progress=item,
        )
    output(f"  screen: {screen.message()}")

    try:
        resume = plan_resume(
            plan.steps,
            item,
            values_digest=digest,
            is_satisfied=lambda state: state == screen.state_id,
        )
    except ValueError as exc:
        return _blocked(name, str(exc), progress=item)
    if resume.blocked:
        return _blocked(name, resume.blocked_reason, progress=item, resume=resume)
    output(f"  {resume.describe(plan.steps)}")

    try:
        window = attach(config.app)
        inspection = inspect(window)
    except (WindowError, UIAError) as exc:
        return AutomationRun(
            status=FAILED,
            automation=name,
            message=f"cannot read the target window: {exc}",
            start_index=resume.index,
            resume=resume,
            progress=item,
        )

    # Starting from the top means "as if nothing had happened yet", so a form
    # left open by an earlier attempt has to go: typing into it would land this
    # record's values in the abandoned one.
    if resume.index == 0:
        try:
            closed = clear_form(window, config, output=output, sleep=sleep)
        except (WindowError, UIAError) as exc:
            closed = False
            output(f"  the open form could not be checked: {exc}")
        if not closed:
            return AutomationRun(
                status=FAILED,
                automation=name,
                message=(
                    "a form left open by an earlier run would not close, so nothing "
                    "was started"
                ),
                start_index=resume.index,
                resume=resume,
                progress=item,
            )

    steps = plan.steps

    def checkpoint_before(position: int, _step: FormStep) -> None:
        """Hold the step until the screen it belongs on is actually showing.

        A checkpoint is a precondition, not a promise: clicking "Add" is
        *followed* by a different screen, so checking afterwards would fail
        every navigation step in the plan. Nothing has run when this raises.
        """
        record = steps[position - 1]
        expected = record.checkpoint.state
        match = _wait_for_state(
            detect_now,
            expected,
            seconds=checkpoint_seconds,
            interval=checkpoint_interval,
            sleep=sleep,
        )
        if match.state_id != expected:
            raise CheckpointError(
                f"step {position} ({record.id}) belongs on {expected!r}, but the "
                f"screen is {match.state_id}: {match.message()}. Nothing was "
                "done for this step, so a re-run resumes from here."
            )
        output(f"  checkpoint {expected}: ok (step {position} of {len(steps)})")

    def record_step(position: int, _step: FormStep) -> None:
        """A step finished: the cursor moves past it, on disk."""
        record = steps[position - 1]
        item.mark_step(position - 1, record.id, _now())
        save_progress(item, progress_root)

    try:
        plan_result = execute(
            config.workflow.steps,
            values,
            window=window,
            inspection=inspection,
            labels={item.name: item.field for item in config.fields},
            types={item.name: item.type for item in config.fields},
            controller=controller,
            delay=config.safety.action_delay_seconds,
            pause_after_click=config.workflow.pause_after_click_seconds,
            require_foreground=config.app.require_foreground,
            start_index=resume.index,
            before_step=checkpoint_before,
            on_step=record_step,
            sleep=sleep,
            output=output,
        )
    except RunnerAborted as exc:
        item.finish(OUTCOME_ABORTED, _now())
        save_progress(item, progress_root)
        return AutomationRun(
            status=STOPPED,
            automation=name,
            message=str(exc) or "the run was stopped",
            start_index=resume.index,
            resume=resume,
            progress=item,
        )
    except CheckpointError as exc:
        item.finish(OUTCOME_FAILED, _now())
        save_progress(item, progress_root)
        return AutomationRun(
            status=FAILED,
            automation=name,
            message=str(exc),
            start_index=resume.index,
            resume=resume,
            progress=item,
        )

    for warning in plan_result.warnings:
        output(f"  note: {warning}")
    if plan_result.problems:
        item.finish(OUTCOME_FAILED, _now())
        save_progress(item, progress_root)
        return AutomationRun(
            status=FAILED,
            automation=name,
            message="; ".join(plan_result.problems),
            start_index=resume.index,
            steps_run=plan_result.steps_run,
            resume=resume,
            progress=item,
        )

    # Write ahead: from here until the confirmation the screen is ambiguous, and
    # this marker is what stops the next run from clicking Save again blindly.
    item.begin_save(_save_step_id(plan, config), _now())
    save_progress(item, progress_root)
    output("  save: written to the progress file before the click")

    try:
        saved = save_call(
            config=config,
            window=window,
            inspection=plan_result.inspection,
            fields=plan_result.fields,
            values=values,
            controller=controller,
            confirmation=confirmation,
            screenshot_dir=screenshot_dir,
            record_label=record_label,
            attempt=1,
            output=output,
            sleep=sleep,
        )
    except (RunnerAborted, OSError, UIAError) as exc:
        # The click may or may not have landed, and this code cannot tell: leave
        # the marker set so the next run asks instead of guessing.
        return AutomationRun(
            status=UNCONFIRMED,
            automation=name,
            message=(
                f"the save step failed partway ({exc}); the save is unresolved "
                "and the next run will stop for it"
            ),
            start_index=resume.index,
            steps_run=plan_result.steps_run,
            resume=resume,
            progress=item,
        )

    outcome = saved.outcome.outcome if saved is not None else ""
    if outcome == "unreachable":
        # Nothing was clicked, so there is nothing pending: clear the marker.
        item.resolve_save(False, _now())
        save_progress(item, progress_root)
        return AutomationRun(
            status=FAILED,
            automation=name,
            message="the Save button was not on screen; nothing was clicked",
            start_index=resume.index,
            steps_run=plan_result.steps_run,
            resume=resume,
            progress=item,
        )

    if saved and saved.outcome.outcome:
        item.resolve_save(True, _now())
        save_progress(item, progress_root)
        return AutomationRun(
            status=SAVED,
            automation=name,
            message=f"record saved and confirmed ({saved.outcome.reason})",
            start_index=resume.index,
            steps_run=plan_result.steps_run,
            resume=resume,
            progress=item,
            save=saved,
            screenshots=saved.screenshots,
        )

    return AutomationRun(
        status=UNCONFIRMED,
        automation=name,
        message=(
            f"Save was clicked but could not be confirmed "
            f"({saved.outcome.reason if saved is not None else 'no result'}); "
            "the save is unresolved and the next run will stop for it"
        ),
        start_index=resume.index,
        steps_run=plan_result.steps_run,
        resume=resume,
        progress=item,
        save=saved,
        screenshots=saved.screenshots if saved is not None else (),
    )


__all__ = [
    "BLOCKED",
    "DEFAULT_SAVE_STEP",
    "EXIT_CODES",
    "STOPPED",
    "AutomationRun",
    "CheckpointError",
    "run_automation",
]
