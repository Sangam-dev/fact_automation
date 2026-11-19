"""Run one workflow against one record, once, on a live window.

This is the thin orchestration layer above :mod:`execution.actions.engine`: it
attaches to the target window, executes the ordered plan, captures before/after
evidence, clicks Save exactly once, and decides whether the save worked. It
never retries a save that may already have succeeded, and it writes no workbook,
queue, or transaction - the caller owns the record data.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.config import AgentConfig, project_root
from core.runtime.errors import RunnerAborted, RunnerError
from core.runtime.pause_guard import PauseGuard
from core.safety.controller import SafetyController
from desktop.screenshots.capture import capture_window
from desktop.state.fact import FactError, attach_target
from desktop.types import WindowInfo
from desktop.uia.uia import UIAError, UIAInspection, inspect_window
from desktop.windows.window import WindowError
from execution.actions.engine import (
    BUTTON_CONTROL_TYPES,
    SETTLE_SECONDS,
    add_button_labels,
    clear_open_form,
    execute_form_steps,
    read_save_window,
    save_screenshot,
)
from execution.verification.confirmation import (
    CONFIRMED,
    ConfirmationError,
    ConfirmationResult,
    SaveConfirmer,
)
from execution.verification.controls import (
    ResolvedLayout,
    ResolvedTarget,
    compare_saved_fields,
    resolve_page_target,
)
from execution.verification.evidence import (
    check_row_appeared,
    footprint_values,
)
from execution.verification.search import find_appended_row

logger = logging.getLogger(__name__)

SAVED = "saved"
UNCONFIRMED = "unconfirmed"
FAILED = "failed"


@dataclass(frozen=True)
class SaveResult:
    """What the Save click left behind."""

    outcome: ConfirmationResult
    before: UIAInspection | None = None
    after: UIAInspection | None = None
    after_image: Any = None
    evidence: str = ""
    screenshots: tuple[Path, ...] = ()

    def __bool__(self) -> bool:
        return self.outcome.outcome == CONFIRMED


@dataclass(frozen=True)
class ExecutionOutcome:
    """The result of one record's run."""

    status: str
    record_label: str
    attempts: int = 0
    error: str = ""
    screenshots: tuple[Path, ...] = ()
    save: SaveResult | None = None
    messages: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.status == SAVED


def _format_value(value: Any) -> str:
    """Render one record value for typing."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value).strip()


#: The one key in a values file that is not a field: the line rows.
ROWS_KEY = "rows"
#: The same document, written the way the deterministic fillers read it.
LINES_KEY = "lines"
#: The header, when the document nests it instead of flattening it.
HEADER_KEY = "header"

#: Keys that describe the document rather than a field on the form.
ENVELOPE_KEYS = (ROWS_KEY, LINES_KEY, HEADER_KEY)


def split_rows(
    data: dict[str, Any], *, source: str = "values"
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Separate one document's header values from its line rows.

    A values file describes *one document*: the fields typed once, plus an
    optional row list typed once per line. The document is saved once, at the
    end - not once per row.

    It can be written two ways, and both mean the same thing: flat
    (``{"customer": "Ram", "rows": [...]}``) or in the shape the
    deterministic fillers already read (``{"header": {...}, "lines":
    [...]}``). One file then serves the endpoint, the plan and the script, and
    none of them has to be translated for the others.
    """
    row_key = ROWS_KEY if ROWS_KEY in data else LINES_KEY
    raw = data.get(row_key)
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise RunnerError(
            f'{source}: "{row_key}" must be a list of objects, one per row'
        )
    rows: list[dict[str, str]] = []
    for index, row in enumerate(raw, start=1):
        if not isinstance(row, dict):
            raise RunnerError(
                f"{source}: row {index} must be an object of field -> value"
            )
        rows.append({str(key): _format_value(value) for key, value in row.items()})

    nested = data.get(HEADER_KEY)
    if nested is not None and not isinstance(nested, dict):
        raise RunnerError(
            f'{source}: "{HEADER_KEY}" must be an object of field -> value'
        )
    fields = nested if isinstance(nested, dict) else data
    header = {
        str(key): _format_value(value)
        for key, value in fields.items()
        if key not in ENVELOPE_KEYS
    }
    return header, rows


def load_values_with_rows(
    path: str | Path,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Load one document: its header values and its line rows."""
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = project_root() / file_path
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError(f"cannot read values {file_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RunnerError(
            f"values {file_path} must contain a JSON object of field -> value"
        )
    return split_rows(data, source=f"values {file_path}")


def load_values(path: str | Path) -> dict[str, str]:
    """Load one record from a JSON object (``{"field": value}``)."""
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = project_root() / file_path
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError(f"cannot read values {file_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RunnerError(
            f"values {file_path} must contain a JSON object of field -> value"
        )
    header, _ = split_rows(data, source=f"values {file_path}")
    return header


def _screenshot_dir(value: str | Path | None) -> Path:
    if value is not None:
        return Path(value)
    return project_root() / "data" / "screenshots"


def save_and_verify(
    *,
    config: AgentConfig,
    window: WindowInfo,
    inspection: UIAInspection,
    fields: dict[str, ResolvedTarget],
    values: dict[str, str],
    controller: SafetyController | None = None,
    confirmation: SaveConfirmer | None = None,
    screenshot_dir: str | Path | None = None,
    record_label: str = "record",
    attempt: int = 1,
    output: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> SaveResult:
    """Click Save on the filled form and collect proof that it worked.

    Returns a falsy :class:`SaveResult` when Save could not be reached at all;
    the caller decides what that means for the record.
    """
    directory = _screenshot_dir(screenshot_dir)
    shots: list[Path] = []

    save, warning = resolve_page_target(
        inspection,
        label=config.actions.save_button,
        control_types=BUTTON_CONTROL_TYPES,
    )
    if warning:
        output(f"  note: Save: {warning}")
    if save is None:
        output(
            f"[WARN] The Save button {config.actions.save_button!r} was not "
            "found in the live window; nothing was clicked."
        )
        return SaveResult(
            outcome=ConfirmationResult(
                outcome="unreachable",
                reason="the Save button was not on screen",
                source="window",
            )
        )

    layout = ResolvedLayout(fields=dict(fields), save=save)
    pre = capture_window(window)
    shots.append(save_screenshot(pre, directory, record_label, attempt, "pre_save"))

    guard = PauseGuard(controller, window, output)
    if not guard.check("stopped before Save; nothing was saved"):
        return SaveResult(
            outcome=ConfirmationResult(
                outcome="aborted",
                reason="the run was stopped before Save",
                source="user",
            ),
            screenshots=tuple(shots),
        )

    x, y = save.rect.center
    from desktop.input.input import click_checked  # local: avoids a wide import

    click_checked(x, y, window, require_foreground=config.app.require_foreground)
    sleep(config.actions.wait_after_save_seconds)

    post = capture_window(window)
    shots.append(save_screenshot(post, directory, record_label, attempt, "post_save"))

    after = read_save_window(window)
    evidence = compare_saved_fields(pre, post, layout, values)
    output(f"  save hint: {evidence.detail}")

    result: ConfirmationResult | None = None
    # A success message, where the app has one, is the strongest evidence.
    if config.confirmation.success_pattern or config.confirmation.error_patterns:
        if confirmation is None and not config.confirmation.success_pattern:
            result = None
        elif confirmation is not None:
            try:
                result = confirmation.confirm(post.image, weak_hint=evidence.detail)
                output(f"  save confirmation: {result.outcome} ({result.reason})")
            except ConfirmationError as exc:
                output(f"  the success text could not be read: {exc}")

    # An app that says nothing still shows the record, so judge the screen with
    # no model call. This is the same check every later record gets.
    if result is None or result.outcome not in (CONFIRMED, "REJECTED"):
        footprint = footprint_values(config) or list(values)
        local = check_row_appeared(
            inspection,
            after,
            values,
            footprint,
            config.confirmation.error_patterns,
        )
        if (
            local.outcome != CONFIRMED
            and after is not None
            and config.scrolling.enabled
        ):
            workflow = config.workflow
            search = find_appended_row(
                after,
                window=window,
                before=inspection,
                values=values,
                footprint=footprint,
                error_patterns=config.confirmation.error_patterns,
                anchor_labels=add_button_labels(workflow) if workflow else [],
                max_screens=config.scrolling.max_screens,
                settle_seconds=config.scrolling.settle_seconds,
                wheel_notches=config.scrolling.wheel_notches,
                note=output,
            )
            if search.fatal:
                output(f"[WARN] {search.fatal}")
            local = search.result
        output(f"  save confirmation: {local.outcome} ({local.reason})")
        result = local

    return SaveResult(
        outcome=result,
        before=inspection,
        after=after,
        after_image=post.image,
        evidence=evidence.detail,
        screenshots=tuple(shots),
    )


def run_workflow(
    values: dict[str, str],
    *,
    config: AgentConfig,
    confirmation: SaveConfirmer | None = None,
    controller: SafetyController | None = None,
    screenshot_dir: str | Path | None = None,
    record_label: str = "record",
    on_before_save: Callable[[str], None] | None = None,
    confirm_each: bool = False,
    input_func: Callable[[str], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    output: Callable[[str], None] = print,
) -> ExecutionOutcome:
    """Run the configured workflow once and report the outcome.

    The plan is retried only when ``workflow.retry_on_failure`` is set: a click
    in the plan may create a record, so replaying it can duplicate work. A save
    that could not be confirmed is never clicked again.
    """
    workflow = config.workflow
    if workflow is None or not workflow.steps:
        raise RunnerError("this config has no workflow.steps to run")

    labels = {item.name: item.field for item in config.fields}
    types = {item.name: item.type for item in config.fields}
    max_attempts = config.safety.max_attempts if workflow.retry_on_failure else 1
    screenshots: list[Path] = []
    issues: list[str] = []

    for attempt in range(1, max_attempts + 1):
        if controller is not None and not controller.check():
            raise RunnerAborted("stopped before the record was saved")
        output(
            f"{record_label}: attempt {attempt}/{max_attempts} "
            f"({len(workflow.steps)} step(s))"
        )
        logger.info(
            "%s: attempt %s/%s, %s step(s)",
            record_label,
            attempt,
            max_attempts,
            len(workflow.steps),
        )
        try:
            window = attach_target(config.app)
            inspection = inspect_window(window)
        except (FactError, WindowError, UIAError) as exc:
            raise RunnerError(f"cannot read the target window: {exc}") from exc

        if attempt > 1 and not clear_open_form(
            window, config, output=output, sleep=sleep
        ):
            return ExecutionOutcome(
                status=FAILED,
                record_label=record_label,
                attempts=attempt - 1,
                error=(
                    "a form from the failed attempt was still open, so the "
                    "record was not retried; entering it again would have "
                    "created a second record"
                ),
                screenshots=tuple(screenshots),
            )

        def refresh() -> tuple[WindowInfo, UIAInspection]:
            """Re-attach after a click; FACT may have replaced the window."""
            try:
                fresh_window = attach_target(config.app)
                return fresh_window, inspect_window(fresh_window)
            except (FactError, WindowError, UIAError) as exc:
                raise UIAError(str(exc)) from exc

        plan = execute_form_steps(
            workflow.steps,
            values,
            window=window,
            inspection=inspection,
            labels=labels,
            types=types,
            controller=controller,
            delay=config.safety.action_delay_seconds,
            pause_after_click=workflow.pause_after_click_seconds,
            require_foreground=config.app.require_foreground,
            settle_seconds=SETTLE_SECONDS,
            reacquire=refresh,
            sleep=sleep,
            output=output,
        )
        for warning in plan.warnings:
            output(f"  note: {warning}")
        if plan.problems:
            message = "; ".join(plan.problems)
            issues.append(f"attempt {attempt}: {message}")
            output(f"  steps failed: {message}")
            if workflow.retry_on_failure and attempt < max_attempts:
                time.sleep(0.5)
                continue
            return ExecutionOutcome(
                status=FAILED,
                record_label=record_label,
                attempts=attempt,
                error=f"step plan failed before Save: {message}",
                screenshots=tuple(screenshots),
            )

        if confirm_each:
            ask = input_func or input
            answer = ask(
                f"{record_label}: press Enter or Y to click Save "
                "(anything else stops): "
            )
            if answer.strip().casefold() not in ("", "y", "yes"):
                raise RunnerAborted(
                    f"Save declined by the user for {record_label}; nothing was saved"
                )
        if on_before_save is not None:
            on_before_save(record_label)

        # The last click may have moved the plan to a new FACT window; Save is
        # on the screen the plan ended on, not the one it started from.
        if getattr(plan.inspection, "window", None) is not None:
            window = plan.inspection.window

        save = save_and_verify(
            config=config,
            window=window,
            inspection=plan.inspection,
            fields=plan.fields,
            values=values,
            controller=controller,
            confirmation=confirmation,
            screenshot_dir=screenshot_dir,
            record_label=record_label,
            attempt=attempt,
            output=output,
            sleep=sleep,
        )
        screenshots.extend(save.screenshots)
        if save:
            output(f"{record_label}: saved")
            return ExecutionOutcome(
                status=SAVED,
                record_label=record_label,
                attempts=attempt,
                screenshots=tuple(screenshots),
                save=save,
            )
        return ExecutionOutcome(
            status=UNCONFIRMED,
            record_label=record_label,
            attempts=attempt,
            error=save.outcome.reason or "the save could not be confirmed",
            screenshots=tuple(screenshots),
            save=save,
        )

    return ExecutionOutcome(
        status=FAILED,
        record_label=record_label,
        attempts=max_attempts,
        error="; ".join(issues) or "the workflow did not run",
        screenshots=tuple(screenshots),
    )


__all__ = [
    "FAILED",
    "SAVED",
    "UNCONFIRMED",
    "ExecutionOutcome",
    "SaveResult",
    "load_values",
    "run_workflow",
    "save_and_verify",
]
