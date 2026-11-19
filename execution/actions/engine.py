"""Deterministic multistep executor: run a click/fill plan against one window.

A workflow names its controls, so the only thing it cannot express is *order*:
click "Add" to open the entry fields, type three text boxes, click Save. The
plan is executed in order, the live UIA tree is re-read after every click, and
every fill is read back before Save is allowed.

Only two actions exist, ``click`` and ``fill``. Values are supplied by the
caller as a plain record (``{key: value}``); this module never reads a workbook,
a row, or a database.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from core.config import AgentConfig, WorkflowConfig
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
    paste_text,
    press_chord,
    press_key,
)
from desktop.screenshots.capture import Capture
from desktop.types import WindowInfo
from desktop.uia.uia import UIAError, UIAInspection, inspect_window
from execution.actions.models import ClickStep, FillStep, FormStep
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

BUTTON_CONTROL_TYPES = frozenset({"Button"})

# How long to wait before reading a field again when the first read disagrees.
READ_BACK_RETRY_SECONDS = 0.15


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
    start_index: int = 0,
    before_step: Callable[[int, FormStep], None] | None = None,
    on_step: Callable[[int, FormStep], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    output: Callable[[str], None] = print,
) -> PlanResult:
    """Run the plan in order, re-reading the live UI after every click.

    A click may create the controls the next steps need, so the tree is
    re-inspected after each one; a fill never changes the tree, so its
    read-back uses the inspection that resolved it.

    ``start_index`` skips steps already known to be done (a resumed run): step
    numbers in messages stay absolute, so step 3 of the plan is still "step 3".
    ``before_step`` is called before a step does anything - a precondition, so
    an exception from it runs nothing - and ``on_step`` after it genuinely
    finished, which is where a caller records its checkpoint.
    """
    fields: dict[str, ResolvedTarget] = {}
    problems: list[str] = []
    warnings: list[str] = []
    executed = 0
    guard = PauseGuard(controller, window, output)

    for index, step in enumerate(steps, start=1):
        if index <= start_index:
            continue
        if before_step is not None:
            before_step(index, step)
        if guard.check("stopped while filling the form"):
            # The user took over while the run was paused, so the screen may
            # have changed underneath it: read the live UI again before aiming.
            try:
                inspection = inspect_window(window)
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

        target, warning = resolve_page_target(
            inspection, label=label, control_types=control_types
        )
        if warning:
            warnings.append(f"step {index}: {warning}")
        if target is None:
            message = (
                f"step {index} ({action} {label!r}): no match in the live "
                "window"
            )
            if step.optional:
                warnings.append(f"{message}; skipped")
                output(f"  {message}; skipped")
                continue
            problems.append(message)
            break

        x, y = target.rect.center
        try:
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

        if isinstance(step, ClickStep):
            if pause_after_click > 0:
                sleep(pause_after_click)
            output(f"  step {index}: click {label!r}")
            try:
                inspection = inspect_window(window)
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
    "save_screenshot",
    "step_keys",
    "verify_save",
]
