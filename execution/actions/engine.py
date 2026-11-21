"""Deterministic multistep executor: run a click/fill plan against one window.

A workflow names its controls, so the only thing it cannot express is *order*:
click "Add" to open the entry fields, type three text boxes, click Save. The
plan is executed in order, the live UIA tree is re-read after every click, and
every fill is read back before Save is allowed.

The actions are ``click``/``double_click``, ``key`` and ``fill``, plus one
escape hatch: ``run`` hands a single screen to a script that already knows how
to drive it. Values are supplied by the caller as a plain record (``{key:
value}``); this module never reads a workbook, a row, or a database.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from core.config import AgentConfig, WorkflowConfig, project_root
from core.runtime.errors import RunnerAborted
from core.runtime.pause_guard import PauseGuard
from core.safety.controller import SafetyController
from desktop.input.input import (
    VK_A,
    VK_CONTROL,
    VK_DELETE,
    VK_ESCAPE,
    InputError,
    click_checked,
    double_click_checked,
    paste_text,
    press_chord,
    press_key,
    press_keys,
)
from desktop.screenshots.capture import Capture
from desktop.types import CLICKABLE_CONTROL_TYPES, WindowInfo
from desktop.uia.uia import UIAError, UIAInspection, inspect_window
from execution.actions.models import (
    ClickStep,
    DoubleClickStep,
    FillStep,
    FormStep,
    KeyStep,
    ScriptStep,
)
from execution.verification.confirmation import (
    CONFIRMED,
    REJECTED,
    UNKNOWN,
    ConfirmationResult,
    SaveConfirmer,
)
from execution.verification.controls import (
    EDITABLE_CONTROL_TYPES,
    ResolvedTarget,
    resolve_page_target,
    values_match,
)
from execution.verification.evidence import check_row_appeared, footprint_values
from execution.verification.search import RowSearchUnsupported, find_appended_row

BUTTON_CONTROL_TYPES = CLICKABLE_CONTROL_TYPES

# How long to wait before reading a field again when the first read disagrees.
READ_BACK_RETRY_SECONDS = 0.15

# How long the executor waits for a control that is not there *yet*: a click
# that opens a form hands back a window before the form has finished drawing.
SETTLE_SECONDS = 10.0

# How often to look again while waiting for that form.
SETTLE_POLL_SECONDS = 0.25

#: The placeholder a ``run`` step uses for the values file the run was given.
SCRIPT_VALUES_PLACEHOLDER = "{values_file}"
#: The placeholder a ``run`` step uses for the job's own name, for its log.
SCRIPT_JOB_PLACEHOLDER = "{job_id}"
#: How many of a failed script's own output lines are quoted back.
SCRIPT_OUTPUT_TAIL = 3


@dataclass(frozen=True)
class PlanResult:
    """What one pass over the step plan did."""

    inspection: UIAInspection
    fields: dict[str, ResolvedTarget]
    steps_run: int = 0
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


def _running(controller: SafetyController | None) -> bool:
    return controller is None or controller.check()


def _require_running(
    controller: SafetyController | None, message: str
) -> None:
    if not _running(controller):
        raise RunnerAborted(message)


def _wait(controller: SafetyController | None, seconds: float) -> bool:
    if seconds <= 0:
        return _running(controller)
    if controller is not None:
        return controller.wait(seconds)
    time.sleep(seconds)
    return True


def add_button_labels(workflow: WorkflowConfig) -> list[str]:
    """The plan's first click: the button that opens the entry form."""
    for step in workflow.steps:
        if isinstance(step, ClickStep):
            return [step.button] if step.button else []
    return []


def step_keys(workflow: WorkflowConfig) -> list[str]:
    """Every data key the plan fills, in first-use order."""
    keys: dict[str, None] = {}
    for step in workflow.steps:
        if isinstance(step, FillStep):
            keys.setdefault(step.key, None)
    return list(keys)


def _resolved_files(
    values_file: str | Path | None, root: Path
) -> dict[str, str]:
    """What the placeholders in a script step stand for.

    The values file is made absolute once, here: the script runs with the
    project root as its working directory, and a relative path would then mean
    two different files to two readers of the same step.
    """
    if values_file is None:
        return {SCRIPT_VALUES_PLACEHOLDER: ""}
    path = Path(values_file)
    if not path.is_absolute():
        path = root / path
    return {SCRIPT_VALUES_PLACEHOLDER: str(path)}


def script_command(
    step: ScriptStep,
    *,
    values_file: str | Path | None = None,
    job_id: str = "",
    root: str | Path | None = None,
) -> tuple[list[str], str]:
    """The command a ``run`` step executes, and any reason it cannot run.

    Returned as a list, never as a shell string: an argument that contains a
    space or a quote has to arrive at the script exactly as it was written.
    """
    base = Path(root) if root is not None else project_root()
    files = _resolved_files(values_file, base)
    files[SCRIPT_JOB_PLACEHOLDER] = job_id

    def filled(text: str) -> str:
        for placeholder, value in files.items():
            text = text.replace(placeholder, value)
        return text

    script = filled(step.script)
    args = [filled(argument) for argument in step.args]
    if not files[SCRIPT_VALUES_PLACEHOLDER] and any(
        SCRIPT_VALUES_PLACEHOLDER in item for item in [step.script, *step.args]
    ):
        return [], (
            f"the step uses {SCRIPT_VALUES_PLACEHOLDER}, but this run was given no "
            "values file"
        )
    target = Path(script)
    if not target.is_absolute():
        target = base / target
    if target.suffix.casefold() == ".py":
        if not target.is_file():
            return [], (
                f"there is no script at {target}; the registered deterministic "
                "form filler is unavailable"
            )
        # Use the interpreter running the automation, not PATH's `python`.
        # This keeps the filler in the same environment as the launcher and
        # prevents a second Python installation from silently changing GUI or
        # dependency behaviour.
        return [sys.executable, str(target), *args], ""
    if not target.is_file():
        return [], f"there is no script at {target}"
    return [str(target), *args], ""


def run_script(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    cwd: str | Path | None = None,
    output: Callable[[str], None] = print,
) -> tuple[bool, str]:
    """Run one script step: ``(failed, detail)``, never raising on its failure.

    A plan that cannot see why a script refused to work is a plan somebody
    debugs by hand, so the script's own last lines come back as the detail and
    every line it printed goes to ``output`` - the run log is where a failure is
    read afterwards, and there the beginning matters as much as the end.
    """
    try:
        done = subprocess.run(
            list(command),
            cwd=str(cwd if cwd is not None else project_root()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return True, (
            f"it was still running after {timeout_seconds:g}s and was stopped"
        )
    except OSError as exc:
        return True, f"it could not be started: {exc}"

    lines = [
        line.strip()
        for line in f"{done.stdout or ''}\n{done.stderr or ''}".splitlines()
        if line.strip()
    ]
    for line in lines:
        output(f"  | {line}")
    tail = " | ".join(lines[-SCRIPT_OUTPUT_TAIL:])
    if done.returncode != 0:
        return True, f"it exited with code {done.returncode}" + (
            f": {tail}" if tail else ""
        )
    return False, tail


def execute_form_steps(
    steps: Sequence[FormStep],
    values: dict[str, str],
    *,
    window: WindowInfo,
    inspection: UIAInspection,
    labels: dict[str, str],
    types: dict[str, str],
    controller: SafetyController | None = None,
    delay: float = 0.0,
    pause_after_click: float = 0.5,
    require_foreground: bool = False,
    read_back_retry_seconds: float = READ_BACK_RETRY_SECONDS,
    settle_seconds: float = 0.0,
    start_index: int = 0,
    values_file: str | Path | None = None,
    job_id: str = "",
    script_cwd: str | Path | None = None,
    before_step: Callable[[int, FormStep], None] | None = None,
    on_step: Callable[[int, FormStep], None] | None = None,
    reacquire: Callable[[], tuple[WindowInfo, UIAInspection]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    output: Callable[[str], None] = print,
) -> PlanResult:
    """Run the plan in order, re-reading the live UI after every click.

    A click may create the controls the next steps need, so the tree is
    re-inspected after each one; a fill never changes the tree, so its
    read-back uses the inspection that resolved it.

    ``settle_seconds`` is how long a step keeps looking for a control that is
    not on screen yet - a click that opens a form hands back a window before the
    form has finished drawing. Callers that drive a live app pass
    :data:`SETTLE_SECONDS`; a caller that has already read the screen passes 0.

    ``start_index`` skips steps already known to be done (a resumed run): step
    numbers in messages stay absolute, so step 3 of the plan is still "step 3".
    ``before_step`` is called before a step does anything - a precondition, so
    an exception from it runs nothing - and ``on_step`` after it genuinely
    finished, which is where a caller records its checkpoint.

    ``values_file`` and ``job_id`` are what a ``run`` step's placeholders stand
    for; ``script_cwd`` is where a script is started (the project root). A
    script is one action, so Stop is honoured before it and after it, not in
    the middle: the script owns its own interrupt key.
    """
    fields: dict[str, ResolvedTarget] = {}
    problems: list[str] = []
    warnings: list[str] = []
    executed = 0
    guard = PauseGuard(controller, window, output)

    def read_again() -> UIAInspection:
        """Re-read the live screen, allowing the target window to change.

        A click can close the window the plan started on (FACT closes Login and
        shows the main menu), so the executor asks the caller for the *current*
        window instead of re-reading a handle that may already be dead.
        """
        nonlocal window, inspection
        if reacquire is not None:
            window, inspection = reacquire()
            guard.set_window(window)
            return inspection
        inspection = inspect_window(window)
        return inspection

    def screen_when_drawn(seconds: float) -> UIAInspection:
        """Re-read the screen, waiting while the window it lands on is opening.

        Clicking Add in FACT can close one window and open another, and for a
        moment there is nothing readable at all. Giving up on that first read
        reports a missing window for a form that was about to appear, so keep
        asking until ``seconds`` have passed.
        """
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            try:
                return read_again()
            except UIAError:
                if time.monotonic() >= deadline:
                    raise
                sleep(SETTLE_POLL_SECONDS)

    def target_when_drawn(
        label: str, control_types: Sequence[str], seconds: float
    ) -> tuple[ResolvedTarget | None, str]:
        """Resolve a control, waiting for a form that is still being drawn.

        The same rule as :func:`screen_when_drawn`, one level down: the window
        is there but the field the step types into has not been painted yet.
        """
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            found, warning = resolve_page_target(
                inspection, label=label, control_types=control_types
            )
            if found is not None or time.monotonic() >= deadline:
                return found, warning
            sleep(SETTLE_POLL_SECONDS)
            try:
                read_again()
            except UIAError:
                # The window went away while we looked: keep waiting for it.
                continue

    for index, step in enumerate(steps, start=1):
        if index <= start_index:
            continue
        if guard.check("stopped while filling the form"):
            # The user took over while the run was paused, so the screen may
            # have changed underneath it: read the live UI again before aiming.
            try:
                inspection = read_again()
            except UIAError as exc:
                problems.append(
                    f"step {index}: could not re-read the window after the "
                    f"pause: {exc}"
                )
                break

        if isinstance(step, ClickStep):
            action = "click"
            label = step.button
            control_types = BUTTON_CONTROL_TYPES
        elif isinstance(step, DoubleClickStep):
            action = "double_click"
            label = step.button
            control_types = BUTTON_CONTROL_TYPES
        elif isinstance(step, ScriptStep):
            # A script is a leaf: it types into the form itself, so the screen
            # it belongs on is checked first, exactly like a fill's. When it
            # returns, the screen belongs to whatever it left behind.
            if before_step is not None:
                before_step(index, step)
            _require_running(
                controller, f"stopped before step {index} (script)"
            )
            command, complaint = script_command(
                step,
                values_file=values_file,
                job_id=job_id,
                root=script_cwd,
            )
            if complaint:
                problems.append(
                    f"step {index} (run {step.script!r}): {complaint}"
                )
                break
            output(f"  step {index}: run {step.script!r}")
            failed, detail = run_script(
                command,
                timeout_seconds=step.timeout_seconds,
                cwd=script_cwd,
                output=output,
            )
            if failed:
                message = f"step {index} (run {step.script!r}): {detail}"
                if step.optional:
                    warnings.append(f"{message}; skipped")
                    output(f"  {message}; skipped")
                    continue
                problems.append(message)
                break
            executed += 1
            if detail:
                output(f"    {detail}")
            try:
                inspection = screen_when_drawn(settle_seconds)
            except UIAError as exc:
                problems.append(
                    f"step {index}: the screen could not be read after "
                    f"{step.script!r}: {exc}"
                )
                break
            if on_step is not None:
                on_step(index, step)
            continue
        elif isinstance(step, KeyStep):
            # A key press aims at nothing: it goes to whatever has focus, which
            # is the field the previous step filled. Keeping it in the plan as
            # its own step is what makes "Tab to commit" visible in the file.
            if before_step is not None:
                before_step(index, step)
            executed += 1
            try:
                press_keys(step.keys)
            except InputError as exc:
                problems.append(f"step {index} (key {step.keys!r}): {exc}")
                break
            output(f"  step {index}: key {step.keys!r}")
            if pause_after_click > 0:
                sleep(pause_after_click)
            try:
                inspection = screen_when_drawn(settle_seconds)
            except UIAError as exc:
                problems.append(
                    f"step {index}: could not re-read the window after "
                    f"pressing {step.keys!r}: {exc}"
                )
                break
            if on_step is not None:
                on_step(index, step)
            continue
        else:
            action = "fill"
            label = step.field or labels.get(step.key, "")
            control_types = EDITABLE_CONTROL_TYPES

        if not label:
            problems.append(
                f"step {index}: field {step.key!r} has no field label; "
                "add \"field\" to the step"
            )
            break

        target, warning = target_when_drawn(
            label, control_types, settle_seconds
        )
        if warning:
            warnings.append(f"step {index}: {warning}")
        if target is None:
            message = (
                f"step {index} ({action} {label!r}): no match in the live "
                "window"
            )
            if step.optional:
                if before_step is not None:
                    before_step(index, step)
                warnings.append(f"{message}; skipped")
                output(f"  {message}; skipped")
                continue
            problems.append(message)
            break

        # The caller is told which screen the step starts on *after* the target
        # was found, because finding it can mean waiting for the form the
        # previous step opened: told any earlier, it would record the screen the
        # step has already left. Nothing has been clicked or typed yet either
        # way, so this is still a precondition.
        if before_step is not None:
            before_step(index, step)

        x, y = target.rect.center
        try:
            if isinstance(step, DoubleClickStep):
                double_click_checked(
                    x,
                    y,
                    window,
                    require_foreground=require_foreground,
                )
            else:
                click_checked(
                    x,
                    y,
                    window,
                    require_foreground=require_foreground,
                )
        except InputError as exc:
            problems.append(f"step {index} ({action} {label!r}): {exc}")
            break
        executed += 1

        if action in ("click", "double_click"):
            if pause_after_click > 0:
                sleep(pause_after_click)
            output(f"  step {index}: {action.replace('_', '-')} {label!r}")
            try:
                inspection = screen_when_drawn(settle_seconds)
            except UIAError as exc:
                problems.append(
                    f"step {index}: could not re-read the window after "
                    f"clicking {label!r}: {exc}"
                )
                break
            if on_step is not None:
                on_step(index, step)
            continue

        if delay > 0:
            sleep(delay)
        if guard.check("stopped while filling the form"):
            # Pausing between the click and the paste would leave the field
            # selected and send the paste wherever the user left the focus:
            # click the field again before typing into it.
            again_x, again_y = target.rect.center
            try:
                click_checked(
                    again_x,
                    again_y,
                    window,
                    require_foreground=require_foreground,
                )
            except InputError as exc:
                problems.append(f"step {index} ({action} {label!r}): {exc}")
                break
            if delay > 0:
                sleep(delay)
        value = values.get(step.key, "")
        press_chord(VK_CONTROL, VK_A)
        if value:
            paste_text(value)
        else:
            press_key(VK_DELETE)
        if delay > 0:
            sleep(delay)
        fields[step.key] = target
        output(f"  step {index}: fill {label!r} = {value!r}")

        expected = value
        field_type = types.get(step.key, "text")
        # ``None`` means "cannot tell", not "wrong": the value is unreadable
        # either because the control has no id or because reading it failed, and
        # neither is evidence that the paste did not land.
        observed: str | None = None
        if target.candidate_id is not None:
            try:
                observed = inspection.read_value(target.candidate_id)
            except UIAError as exc:
                warnings.append(
                    f"step {index}: could not read {label!r} back ({exc}); "
                    "relying on the save check"
                )
            if (
                observed is not None
                and not values_match(observed, expected, field_type)
                and read_back_retry_seconds > 0
            ):
                # The target applies a paste a moment after the keystroke, so
                # the first read can arrive before the text does. Look once more
                # before failing a record that was actually filled correctly.
                sleep(read_back_retry_seconds)
                try:
                    observed = inspection.read_value(target.candidate_id)
                except UIAError as exc:
                    warnings.append(
                        f"step {index}: could not read {label!r} back again "
                        f"({exc}); relying on the save check"
                    )
                    observed = None
            if observed is not None and not values_match(observed, expected, field_type):
                problems.append(
                    f"step {index} ({label!r}): read back {observed!r}, "
                    f"expected {expected!r}"
                )
                break

        if on_step is not None:
            on_step(index, step)

    return PlanResult(
        inspection=inspection,
        fields=fields,
        steps_run=executed,
        problems=tuple(problems),
        warnings=tuple(warnings),
    )


def save_screenshot(
    capture: Capture,
    directory: Path,
    label: str,
    attempt: int,
    phase: str,
) -> Path:
    """Save one evidence screenshot and return its path."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = directory / f"form_{stamp}_{label}_attempt{attempt}_{phase}.png"
    directory.mkdir(parents=True, exist_ok=True)
    capture.image.save(path)
    return path


def form_is_open(window: WindowInfo, config: AgentConfig) -> bool:
    """True when the Save button is on screen, i.e. the form is still open."""
    try:
        inspection = inspect_window(window)
    except UIAError:
        return False
    target, _warning = resolve_page_target(
        inspection,
        label=config.actions.save_button,
        control_types=BUTTON_CONTROL_TYPES,
    )
    return target is not None


def clear_open_form(
    window: WindowInfo,
    config: AgentConfig,
    *,
    output: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Close a form left open by a failed attempt; False when it will not close.

    Retrying with the previous form still open clicks "add" on top of it and the
    fills land in the abandoned form: a duplicate record, or one holding another
    record's values. Closing it first is the only safe way to start again.
    """
    if not form_is_open(window, config):
        return True
    output("  a form from the failed attempt is still open; closing it")
    press_key(VK_ESCAPE)
    sleep(0.3)
    if not form_is_open(window, config):
        output("  the form was closed")
        return True
    output("  the form did not close, so the record will not be retried")
    return False


def read_save_window(window: WindowInfo) -> UIAInspection | None:
    """The screen a save is checked against, wide enough to see list records."""
    try:
        return inspect_window(window, include_data_items=True)
    except UIAError:
        return None


def verify_save(
    *,
    config: AgentConfig,
    confirmation: SaveConfirmer | None,
    window: WindowInfo,
    before: UIAInspection | None,
    post: Capture,
    evidence: str,
    values: dict[str, str],
    anchor_labels: Sequence[str] = (),
    output: Callable[[str], None] = print,
) -> ConfirmationResult:
    """Decide whether the Save worked: the model reads the screen, or we do.

    ``verifier: "row"`` is the local path - no model call, no screenshot sent
    anywhere. It needs the screen as it was before the click, so a record that
    was already listed cannot be mistaken for the new one. When the new record
    landed below the fold the screen is scrolled down looking for it, never
    upwards. ``verifier: "none"`` is the "just fill and save it" mode: the
    screen is not read at all, and every Save counts as done.
    """
    if config.confirmation.verifier == "none":
        return ConfirmationResult(
            outcome=CONFIRMED,
            reason="the screen is not checked (verifier: none)",
            source="no check",
        )
    if config.confirmation.verifier == "row":
        after = read_save_window(window)
        if after is None:
            return ConfirmationResult(
                outcome=UNKNOWN,
                reason="could not read the window after Save",
                source="window text",
            )
        footprint = footprint_values(config)
        result = check_row_appeared(
            before,
            after,
            values,
            footprint,
            config.confirmation.error_patterns,
        )
        if (
            result.outcome in (CONFIRMED, REJECTED)
            or not config.scrolling.enabled
        ):
            return result
        search = find_appended_row(
            after,
            window=window,
            before=before,
            values=values,
            footprint=footprint,
            error_patterns=config.confirmation.error_patterns,
            anchor_labels=anchor_labels,
            max_screens=config.scrolling.max_screens,
            settle_seconds=config.scrolling.settle_seconds,
            wheel_notches=config.scrolling.wheel_notches,
            note=output,
        )
        if search.fatal:
            raise RowSearchUnsupported(search.fatal)
        return search.result
    if confirmation is None:
        return ConfirmationResult(
            outcome=UNKNOWN,
            reason="no text confirmer was given, so the Save cannot be confirmed",
        )
    return confirmation.confirm(post.image, weak_hint=evidence)


__all__ = [
    "BUTTON_CONTROL_TYPES",
    "PlanResult",
    "READ_BACK_RETRY_SECONDS",
    "add_button_labels",
    "clear_open_form",
    "execute_form_steps",
    "form_is_open",
    "read_save_window",
    "SCRIPT_JOB_PLACEHOLDER",
    "SCRIPT_VALUES_PLACEHOLDER",
    "run_script",
    "save_screenshot",
    "script_command",
    "step_keys",
    "verify_save",
]
