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

One document, one Save. A values file may carry ``rows``: the header is typed
once, the steps marked ``per_row`` are typed once per row, and Save is clicked
once after the last row. Rows are never saved individually.

No model is called anywhere in this file.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

from core.automation.exceptions import (
    Observation,
    RuleError,
    RuleTable,
    load_rules,
)
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
from desktop.state.fact import FactError, attach as attach_fact, detect_state
from desktop.state.recognizer import StateMatch
from desktop.state.states import StateTable, StateTableError, load_state_table
from desktop.types import WindowInfo
from desktop.uia.uia import UIAError, UIAInspection, inspect_window
from desktop.windows.window import WindowError
from desktop.input.input import (
    InputError,
    click_checked,
    double_click_checked,
    press_keys,
)
from execution.actions.engine import (
    BUTTON_CONTROL_TYPES,
    PlanResult,
    clear_open_form,
    execute_form_steps,
)
from execution.verification.controls import resolve_page_target
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


def _screen_note(detect_now: Callable[[], StateMatch]) -> str:
    """What is on display now, for a failure message. Never raises."""
    try:
        match = detect_now()
    except Exception as exc:  # the app may be gone, or unreadable
        return f"the screen could not be read: {exc}"
    where = f"the screen on display is {match.state_id}"
    return where if match.known else f"{where}, which the state table does not know"


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
    rows: Sequence[Mapping[str, str]] = (),
    values_file: str | Path | None = None,
    automations_root: str | Path | None = None,
    progress_root: str | Path | None = None,
    table: StateTable | None = None,
    detector: Callable[[], StateMatch] | None = None,
    saved_already: bool = False,
    not_saved: bool = False,
    restart: bool = False,
    from_screen: bool = False,
    confirmation: SaveConfirmer | None = None,
    controller: SafetyController | None = None,
    screenshot_dir: str | Path | None = None,
    record_label: str = "record",
    checkpoint_seconds: float | None = None,
    checkpoint_interval: float = 0.3,
    output: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    attach: Callable[..., WindowInfo] = attach_fact,
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
    rows = [dict(row) for row in rows]
    if not plan.repeats_per_row() and rows and not plan.fills_rows_itself():
        return _blocked(
            name,
            f"the values describe {len(rows)} row(s), but this automation has no "
            "steps that repeat per row; remove the rows or add the row steps",
        )
    wanted_row_keys = plan.row_keys()
    for number, row in enumerate(rows, start=1):
        absent = [key for key in wanted_row_keys if key not in row]
        if absent:
            return _blocked(
                name,
                f"row {number} of the values is missing field(s): "
                + ", ".join(repr(key) for key in absent),
            )
    if plan.repeats_per_row() and not rows:
        return _blocked(
            name,
            "this automation repeats steps per row, but the values contain no "
            '"rows": add the line items, or use a plan without row steps',
        )

    # Rules only ever *help* a run get past a screen: a missing or broken rule
    # file stops the automation from being trusted, and is reported, not ignored.
    try:
        rules: RuleTable | None = load_rules(name, automations_root)
    except RuleError as exc:
        return _blocked(name, str(exc))

    config = plan.to_config()
    # One number governs every screen wait. The plan carries it so a slow PC can
    # be told to wait longer without editing code; a caller that passes a value
    # explicitly (tests, a future --wait flag) still wins.
    wait_seconds = plan.wait_seconds if checkpoint_seconds is None else checkpoint_seconds
    detect_now = detector or (lambda: detect_state(table))

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

    digest = digest_values(values, rows)
    if item is None:
        item = new_progress(name, values, rows=rows, when=_now())

    screen = detect_now()
    if not screen.known:
        return _blocked(
            name,
            f"cannot start: {screen.message()}",
            progress=item,
        )
    output(f"  screen: {screen.message()}")

    # The values file changed since the last run. Typing the new values over a
    # form that still holds the old ones would mix two records, so that stays
    # blocked - but when the app is on some other screen there is nothing
    # half-typed to protect, and refusing to start is only a wall for whoever
    # changed the file and double-clicked the launcher.
    if item.values_changed(digest) and not item.save_unresolved:
        form_states = {step.checkpoint.state for step in plan.steps
                       if step.action in ("fill", "run")}
        if screen.state_id not in form_states:
            output(
                "  the record data changed since the last run, and no half-filled "
                "form is on screen: starting this record from the beginning"
            )
            try:
                clear_progress(name, progress_root)
            except ProgressError as exc:
                return _blocked(name, str(exc), progress=item)
            item = new_progress(name, values, rows=rows, when=_now())

    try:
        resume = plan_resume(
            plan.steps,
            item,
            values_digest=digest,
            is_satisfied=lambda state: state == screen.state_id,
            from_screen=from_screen,
        )
    except ValueError as exc:
        return _blocked(name, str(exc), progress=item)
    if resume.blocked:
        return _blocked(name, resume.blocked_reason, progress=item, resume=resume)
    output(f"  {resume.describe(plan.steps)}")
    if from_screen and plan.steps[resume.index].action in ("fill", "run"):
        output(
            "  this step types into the form that is already open; it is filled "
            "as it stands, not cleared first"
        )

    # Rows are typed into one open form, so a run that stopped after the header
    # cannot be continued without typing the rows again over the ones already
    # there. A person decides; this code does not guess.
    if plan.repeats_per_row() and resume.index > 0:
        return _blocked(
            name,
            "this run stopped after the header was entered, and the row steps "
            "cannot be repeated safely on a half-filled form; re-run with "
            "--restart to enter the document again, or finish it by hand",
            progress=item,
            resume=resume,
        )

    try:
        start_state = plan.steps[resume.index].checkpoint.state
        start_handle = (
            int(screen.window.handle)
            if screen.window is not None and screen.window.handle
            else None
        )
        # Keep the window that established the resume decision. FACT often has
        # multiple pinned top-level windows; a generic reattach could select
        # the main menu after we chose a form step.
        window = attach(
            config.app,
            seconds=wait_seconds,
            expected_state=start_state,
            expected_handle=start_handle,
        )
        inspection = inspect(window)
    except (FactError, WindowError, UIAError) as exc:
        return AutomationRun(
            status=FAILED,
            automation=name,
            message=f"cannot read the target window: {exc}",
            start_index=resume.index,
            resume=resume,
            progress=item,
        )

    # FACT swaps windows between screens (Login -> Main, View -> Add). The
    # executor is handed a refresh hook so a click that replaces the window is
    # followed by the *new* window, not an immediate failure on the old handle.
    screen_state: dict[str, WindowInfo | UIAInspection] = {
        "window": window,
        "inspection": inspection,
    }

    def refresh() -> tuple[WindowInfo, UIAInspection]:
        try:
            fresh_window = attach(config.app, seconds=wait_seconds)
            fresh_inspection = inspect(fresh_window)
        except (FactError, WindowError, UIAError) as exc:
            raise UIAError(str(exc)) from exc
        screen_state["window"] = fresh_window
        screen_state["inspection"] = fresh_inspection
        return fresh_window, fresh_inspection

    def _apply_rule(spot: str, expected: str, match: StateMatch) -> bool:
        """Try the learned rules for the screen that is in the way.

        A rule runs only when every signal in its trigger matches, and the run
        continues only when the screen afterwards is the one the rule promised.
        Anything less and the caller fails the way it did before, so a wrong
        rule costs a message, never a wrong click on a form.
        """
        if rules is None or not rules.rules:
            return False
        window = screen_state["window"]
        inspection = screen_state["inspection"]
        seen = Observation(
            state=match.state_id,
            title=window.title,
            class_name=window.class_name,
            auto_ids=tuple(
                item.automation_id
                for item in inspection.candidates
                if item.automation_id
            ),
        )
        rule = rules.find(seen)
        if rule is None:
            output(f"  {spot}: no learned rule matches this screen")
            return False
        output(
            f"  {spot}: applying rule {rule.id!r}"
            + (f" ({rule.description})" if rule.description else "")
        )
        for action in rule.actions:
            try:
                if action.action == "key":
                    press_keys(action.keys)
                else:
                    target, _warning = resolve_page_target(
                        inspection,
                        label=action.button,
                        control_types=BUTTON_CONTROL_TYPES,
                    )
                    if target is None:
                        output(
                            f"  rule {rule.id!r}: {action.button!r} is not on "
                            "the screen any more; stopping"
                        )
                        return False
                    x, y = target.rect.center
                    if action.action == "double_click":
                        double_click_checked(
                            x,
                            y,
                            window,
                            require_foreground=config.app.require_foreground,
                        )
                    else:
                        click_checked(
                            x,
                            y,
                            window,
                            require_foreground=config.app.require_foreground,
                        )
            except (InputError, UIAError) as exc:
                output(f"  rule {rule.id!r} could not run: {exc}")
                return False
            if config.workflow.pause_after_click_seconds > 0:
                sleep(config.workflow.pause_after_click_seconds)
            try:
                screen_state["window"], screen_state["inspection"] = refresh()
            except UIAError as exc:
                output(f"  rule {rule.id!r}: the screen could not be read: {exc}")
                return False
            window = screen_state["window"]
            inspection = screen_state["inspection"]
        wanted = rule.expect or expected
        recovered = _wait_for_state(
            detect_now,
            wanted,
            seconds=wait_seconds,
            interval=checkpoint_interval,
            sleep=sleep,
        )
        if recovered.state_id != wanted:
            output(
                f"  rule {rule.id!r} ran, but the screen is "
                f"{recovered.state_id}, not {wanted}; stopping"
            )
            return False
        output(f"  rule {rule.id!r}: recovered, the plan continues")
        return True

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
    once_steps, row_steps = plan.split_steps()
    config_steps = list(config.workflow.steps)
    once_config = config_steps[: len(once_steps)]
    row_config = config_steps[len(once_steps) :]

    def _checkpoint(offset: int, where: str = "") -> Callable[[int, FormStep], None]:
        """Hold a step until the screen it belongs on is actually showing.

        A checkpoint is a precondition, not a promise: clicking "Add" is
        *followed* by a different screen, so checking afterwards would fail
        every navigation step in the plan. Nothing has run when this raises.
        """

        def checkpoint_before(position: int, _step: FormStep) -> None:
            record = steps[offset + position - 1]
            expected = record.checkpoint.state
            spot = f"{where}, step {position}" if where else f"step {offset + position}"
            match = _wait_for_state(
                detect_now,
                expected,
                seconds=wait_seconds,
                interval=checkpoint_interval,
                sleep=sleep,
            )
            if match.state_id != expected:
                if _apply_rule(spot, expected, match):
                    return
                raise CheckpointError(
                    f"{spot} ({record.id}) belongs on {expected!r}, but the "
                    f"screen is {match.state_id}: {match.message()}. Nothing was "
                    "done for this step, so a re-run resumes from here. "
                    "If you clear this by hand, teach it: python cli.py "
                    f"automation learn {name} --click \"OK\" --expect {expected}"
                )
            output(f"  checkpoint {expected}: ok ({spot})")

        return checkpoint_before

    def _record(offset: int) -> Callable[[int, FormStep], None]:
        """A step finished: the cursor moves past it, on disk."""

        def record_step(position: int, _step: FormStep) -> None:
            record = steps[offset + position - 1]
            item.mark_step(offset + position - 1, record.id, _now())
            save_progress(item, progress_root)

        return record_step

    labels = {field_spec.name: field_spec.field for field_spec in config.fields}
    types = {field_spec.name: field_spec.type for field_spec in config.fields}

    def _stage(
        stage_steps: list,
        stage_values: dict[str, str],
        *,
        start_index: int,
        offset: int,
        where: str = "",
    ) -> PlanResult:
        return execute(
            stage_steps,
            stage_values,
            window=screen_state["window"],
            inspection=screen_state["inspection"],
            labels=labels,
            types=types,
            controller=controller,
            delay=config.safety.action_delay_seconds,
            pause_after_click=config.workflow.pause_after_click_seconds,
            require_foreground=config.app.require_foreground,
            start_index=start_index,
            # Clicking Login closes one window and opens another, and the button
            # the next step needs is not painted yet when the click returns. A
            # step looks for its control for as long as the plan says the screen
            # may take to arrive - the same patience its checkpoint gets.
            settle_seconds=wait_seconds,
            # A ``run`` step is handed the same file the run was given, so the
            # script and the plan cannot disagree about the record.
            values_file=values_file,
            before_step=_checkpoint(offset, where) if stage_steps else None,
            on_step=_record(offset) if stage_steps else None,
            reacquire=refresh if stage_steps else None,
            sleep=sleep,
            output=output,
        )

    total_steps = 0
    problems: list[str] = []
    warnings: list[str] = []
    filled: dict = {}
    plan_inspection = inspection

    try:
        if once_config:
            once_result = _stage(
                once_config, values, start_index=resume.index, offset=0
            )
            total_steps += once_result.steps_run
            problems = list(once_result.problems)
            warnings = list(once_result.warnings)
            filled = dict(once_result.fields)
            plan_inspection = once_result.inspection

        # One document: the header above, then these steps once per row. Save
        # comes after the last row, once, and is not one of these steps. A plan
        # whose script enters the lines itself has no row steps: the rows are
        # that script's business, so there is no per-row pass to make.
        for number, row in enumerate(rows if row_steps else (), start=1):
            if problems:
                break
            output(f"  row {number} of {len(rows)}")
            row_result = _stage(
                row_config,
                {**values, **row},
                start_index=0,
                offset=len(once_steps),
                where=f"row {number}",
            )
            total_steps += row_result.steps_run
            problems.extend(row_result.problems)
            warnings.extend(row_result.warnings)
            filled.update(row_result.fields)
            plan_inspection = row_result.inspection
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

    for warning in warnings:
        output(f"  note: {warning}")
    if problems:
        item.finish(OUTCOME_FAILED, _now())
        save_progress(item, progress_root)
        # "no match in the live window" says what was missing, not what was
        # there instead: name the screen, so a wrong first step is obvious.
        here = _screen_note(detect_now)
        if here:
            output(f"  {here}")
            problems.append(here)
        return AutomationRun(
            status=FAILED,
            automation=name,
            message="; ".join(problems),
            start_index=resume.index,
            steps_run=total_steps,
            resume=resume,
            progress=item,
        )

    # The footprint check looks for this document's values anywhere on screen,
    # so it covers every row, not just the last one typed.
    save_values = dict(values)
    for row in rows:
        save_values.update(row)

    # Write ahead: from here until the confirmation the screen is ambiguous, and
    # this marker is what stops the next run from clicking Save again blindly.
    item.begin_save(_save_step_id(plan, config), _now())
    save_progress(item, progress_root)
    output("  save: written to the progress file before the click")

    try:
        saved = save_call(
            config=config,
            window=screen_state["window"],
            inspection=plan_inspection,
            fields=filled,
            values=save_values,
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
            steps_run=total_steps,
            resume=resume,
            progress=item,
        )

    outcome = saved.outcome.outcome if saved is not None else ""
    if outcome == "aborted":
        # save_and_verify checks the safety controller before clicking. In that
        # case no Save ambiguity exists, so do not leave a write-ahead marker
        # that blocks every later launcher run.
        item.resolve_save(False, _now())
        save_progress(item, progress_root)
        return AutomationRun(
            status=STOPPED,
            automation=name,
            message=(
                f"Save was not clicked ({saved.outcome.reason}); "
                "the run can be started again"
            ),
            start_index=resume.index,
            steps_run=total_steps,
            resume=resume,
            progress=item,
            save=saved,
            screenshots=saved.screenshots,
        )

    if outcome == "unreachable":
        # Nothing was clicked, so there is nothing pending: clear the marker.
        item.resolve_save(False, _now())
        save_progress(item, progress_root)
        return AutomationRun(
            status=FAILED,
            automation=name,
            message="the Save button was not on screen; nothing was clicked",
            start_index=resume.index,
            steps_run=total_steps,
            resume=resume,
            progress=item,
        )

    if not plan.save.check and saved is not None and outcome == "unknown":
        # The plan was registered without a save check, so the click is the end
        # of the job: there is nothing to look for, and leaving the save
        # unresolved would stop the next record on a question nobody asked.
        item.resolve_save(True, _now())
        save_progress(item, progress_root)
        return AutomationRun(
            status=SAVED,
            automation=name,
            message=(
                "Save was clicked; this automation does not check whether the "
                "record landed, so it is finished here"
            ),
            start_index=resume.index,
            steps_run=total_steps,
            resume=resume,
            progress=item,
            save=saved,
            screenshots=saved.screenshots,
        )

    if saved and saved.outcome.outcome:
        item.resolve_save(True, _now())
        save_progress(item, progress_root)
        return AutomationRun(
            status=SAVED,
            automation=name,
            message=f"record saved and confirmed ({saved.outcome.reason})",
            start_index=resume.index,
            steps_run=total_steps,
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
        steps_run=total_steps,
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
