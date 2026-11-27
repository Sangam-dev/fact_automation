"""Turn a workflow config into a named automation whose checkpoints are proved.

Phase 3 of the automation layer. The wizard authors a click/fill plan and proves
the save; this module takes that plan and answers the one question the plan
cannot answer by itself - *which screen does each step belong on?* - by running
it once and watching.

The rule that keeps this honest:

* a checkpoint is **observed**, never guessed. The proof pass reads the screen
  immediately before each step and records what it saw, so the checkpoint is a
  fact about this application, not a guess by the model;
* nothing is written unless every step ran and every screen in between was one
  the state table already knows. A plan proved on a `POPUP` would carry a
  checkpoint that can never be satisfied again.

Registering proves the plan by running it as it will run: the steps once (and
once per row, when the plan has rows), then Save clicked once at the end. The
record that save creates is the evidence, and a save that cannot be confirmed
means nothing is registered.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from core.automation.launcher import write_launcher
from core.automation.model import Automation, AutomationError, AutomationStep
from core.automation.registry import AutomationNameError, validate_name
from core.automation.registry import save as save_automation
from core.config import AgentConfig, ConfigError, load_config
from desktop.state.fact import FactError, attach as attach_fact
from desktop.state.recognizer import StateMatch, detect
from desktop.state.states import StateTable, StateTableError, load_state_table
from desktop.types import WindowInfo
from desktop.uia.uia import UIAError, UIAInspection, inspect_window
from desktop.windows.window import WindowError
from execution.actions.engine import (
    SETTLE_SECONDS,
    PlanResult,
    clear_open_form,
    execute_form_steps,
)
from execution.actions.models import (
    ClickStep,
    DoubleClickStep,
    FormStep,
    KeyStep,
    ScriptStep,
)
from execution.verification.evidence import decide_footprint
from execution.verification.confirmation import SaveConfirmer
from execution.workflows.runner import SaveResult, save_and_verify

REGISTERED = "registered"
DRAFTED = "drafted"
REFUSED = "refused"
FAILED = "failed"

#: How a run may approach a step again. Every click in an authored plan is a
#: navigation step - it is safe to replay - and every fill types into a form,
#: which can only be redone by walking back to the click that opened it.
RESUME_FOR = {
    "click": "replay",
    "double_click": "replay",
    "key": "replay",
    "fill": "restart",
    # A script types into the form, so it can only be redone by walking back to
    # the click that opened that form - never resumed in place.
    "run": "restart",
}


class RegisterError(RuntimeError):
    """The workflow cannot become an automation as it stands."""


class ProofError(RuntimeError):
    """A step in the proof pass did not start on a screen the table knows."""


@dataclass(frozen=True)
class RegisterResult:
    """What a registration attempt produced."""

    status: str
    name: str
    message: str = ""
    path: Path | None = None
    automation: Automation | None = None
    steps_run: int = 0
    #: the double-clickable script written beside the automation
    launcher: Path | None = None

    @property
    def ok(self) -> bool:
        return self.status in (REGISTERED, DRAFTED)

    @property
    def checkpoints(self) -> tuple[str, ...]:
        return tuple(self.automation.state_ids()) if self.automation else ()


def _slug(text: str, fallback: str) -> str:
    """A step id fragment: letters, digits and dashes only."""
    cleaned = "".join(
        character if character.isalnum() else "-" for character in text.casefold()
    )
    parts = [part for part in cleaned.split("-") if part]
    return "-".join(parts)[:32] or fallback


def _step_payload(step: FormStep, index: int, state: str) -> dict:
    if isinstance(step, ScriptStep):
        return {
            "id": f"{index:02d}-run-{_slug(Path(step.script).stem, 'script')}",
            "action": "run",
            "optional": step.optional,
            "script": step.script,
            "args": list(step.args),
            "timeout_seconds": step.timeout_seconds,
            "checkpoint": {"state": state, "resume": RESUME_FOR["run"]},
        }
    target = (
        step.button
        if isinstance(step, (ClickStep, DoubleClickStep))
        else (step.keys if isinstance(step, KeyStep) else step.key)
    )
    payload: dict = {
        "id": f"{index:02d}-{step.action}-{_slug(target or '', step.action)}",
        "action": step.action,
        "optional": step.optional,
        "checkpoint": {"state": state, "resume": RESUME_FOR[step.action]},
    }
    if isinstance(step, (ClickStep, DoubleClickStep)):
        payload["button"] = step.button
        if isinstance(step, ClickStep) and step.wait_for_progress is not None:
            payload["wait_for_progress"] = (
                step.wait_for_progress.model_dump()
            )
    elif isinstance(step, KeyStep):
        payload["keys"] = step.keys
    else:
        payload["key"] = step.key
        if step.field:
            payload["field"] = step.field
    return payload


def _planned_steps(config: AgentConfig) -> list[tuple[int, FormStep]]:
    """Every step a run executes, with its position in the config.

    The save click is not one of them: the runner clicks Save once, after the
    last step, so a plan that also carried it would save the record twice. The
    position is kept because a caller sometimes has to put a step back on the
    config it came from - the proof starting partway through, for instance.
    """
    if config.workflow is None:
        return []
    save_button = config.actions.save_button.strip().casefold()
    planned: list[tuple[int, FormStep]] = []
    for position, item in enumerate(config.workflow.steps):
        if isinstance(item, (ClickStep, DoubleClickStep)) and (
            item.button.strip().casefold() == save_button
        ):
            continue
        planned.append((position, item))
    return planned


def draft_automation(
    config: AgentConfig,
    *,
    name: str,
    entry_state: str,
    states: Sequence[str | None] | None = None,
) -> Automation:
    """The workflow as an automation, its checkpoints as good as they get now.

    ``states`` is what the wizard watched while it wrote each step: the screen
    that step ran on, position for position with the config's steps. A
    checkpoint taken from it is still an observation, not a guess - the person
    watched the step work - and it saves the proof from replaying navigation
    the app has already left behind. Where no screen was recorded the
    checkpoint is ``entry_state``, the screen showing now, until the proof
    replaces it with what it sees.
    """
    if config.workflow is None or not config.workflow.steps:
        raise RegisterError(
            "this config has no workflow.steps, so there is nothing to register"
        )

    save_button = config.actions.save_button
    steps: list[dict] = []
    for position, item in _planned_steps(config):
        state = entry_state
        if states is not None and position < len(states) and states[position]:
            state = str(states[position])
        steps.append(_step_payload(item, len(steps) + 1, state))

    if not steps:
        raise RegisterError(
            f"the workflow's only step clicks "
            f"{config.actions.save_button!r}, which is the save itself; add at "
            "least one navigation or fill step"
        )

    # What the proof save looks for. The wizard no longer asks, so when the
    # config carries no footprint the typed values are the candidates: if none
    # of them shows up after Save, the record did not land and registration
    # fails instead of blessing a plan whose save was rejected.
    verifier = "none" if config.confirmation.verifier == "none" else "row"
    footprint = list(config.confirmation.footprint)
    if verifier == "row" and not footprint:
        footprint = list(
            dict.fromkeys(
                item.key
                for item in config.workflow.steps
                if item.action == "fill" and item.key
            )
        )

    return Automation.model_validate(
        {
            "name": name,
            "app": config.app.model_dump(),
            "fields": [item.model_dump() for item in config.fields],
            "steps": steps,
            "save": {
                "button": save_button,
                # Proven locally during registration and refined from it: a
                # model call at run time is not part of this design.
                "verifier": verifier,
                "footprint": footprint,
                "success_pattern": config.confirmation.success_pattern,
            },
        }
    )


def assign_checkpoints(
    automation: Automation, observed: list[str], start: int = 0
) -> Automation:
    """Write the screens the proof pass saw into the plan's checkpoints.

    ``start`` is the step the proof began at; the checkpoints before it keep
    what was recorded while the plan was being written.
    """
    expected = len(automation.steps) - start
    if len(observed) != expected:
        raise RegisterError(
            f"{len(observed)} checkpoint(s) were observed for "
            f"{expected} step(s); they have to match one for one"
        )
    for step, state in zip(automation.steps[start:], observed):
        step.checkpoint.state = state
    return automation


def proof_start(steps: Sequence[AutomationStep], state_id: str) -> int:
    """The first step a proof may begin at, standing on ``state_id``.

    Only a step that is safe to replay counts. A fill or a script would type
    into a form the wizard has already written into, so those are walked past
    and the proof starts at the click that opens that form. Zero means "no
    shortcut here - prove the plan from its first step".
    """
    for index, step in enumerate(steps):
        if step.checkpoint.state == state_id and step.checkpoint.resume == "replay":
            return index
    return 0


def describe_checkpoints(automation: Automation) -> list[str]:
    """One readable line per step: what it does, and where it belongs."""
    labels = {item.name: item.field for item in automation.fields}
    lines = []
    for position, step in enumerate(automation.steps, start=1):
        if step.action == "key":
            target = step.keys
        elif step.action in ("click", "double_click"):
            target = step.button
        elif step.action == "run":
            target = step.script
        else:
            target = step.field or labels.get(step.key or "", step.key)
        lines.append(
            f"{position}. {step.action} {target!r} on {step.checkpoint.state} "
            f"({step.checkpoint.resume})"
        )
    return lines


def register_automation(
    *,
    name: str,
    config_path: str | Path,
    values: Mapping[str, str] | None = None,
    values_file: str | Path | None = None,
    confirmation: SaveConfirmer | None = None,
    screenshot_dir: str | Path | None = None,
    automations_root: str | Path | None = None,
    table: StateTable | None = None,
    detector: Callable[[], StateMatch] | None = None,
    attach: Callable[[AgentConfig], WindowInfo] = attach_fact,
    inspect: Callable[[WindowInfo], UIAInspection] = inspect_window,
    execute: Callable[..., PlanResult] = execute_form_steps,
    close_form: Callable[..., bool] = clear_open_form,
    authored_states: Sequence[str | None] | None = None,
    save_call: Callable[..., SaveResult] | None = None,
    check_save: bool = True,
    footprint_for: Callable[..., list[str]] = decide_footprint,
    write: bool = True,
    output: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> RegisterResult:
    """Prove the workflow on the live screen and register it as an automation.

    The proof is the real thing: the plan is executed once and Save is clicked
    once at the end, because that is exactly what a run does. A registration
    that cannot save is not a proven plan, so nothing is written in that case.

    ``check_save=False`` is for an app whose save cannot be proved at all (a
    trial build that refuses to write the database). The steps are still proved
    - each one is run once and learns its screen - but Save is not clicked here
    and later runs click it without looking for the record afterwards.

    ``authored_states`` is optional and only the wizard passes it: the screen
    each step ran on while the plan was being written. With it the proof starts
    at the first step that belongs on the screen showing now, so a plan whose
    opening steps have already been clicked on the live app can still be proved
    from where the app actually is. The earlier checkpoints keep the screens
    they were written on; nothing is taken on trust.
    """
    try:
        validate_name(name)
    except AutomationNameError as exc:
        return RegisterResult(REFUSED, name, str(exc))

    try:
        config = load_config(str(config_path))
    except ConfigError as exc:
        return RegisterResult(REFUSED, name, str(exc))

    try:
        table = table if table is not None else load_state_table()
    except StateTableError as exc:
        return RegisterResult(
            REFUSED, name, f"the state table cannot be used: {exc}"
        )

    detect_now = detector or (lambda: detect(table))
    screen = detect_now()
    if not screen.known:
        return RegisterResult(
            REFUSED,
            name,
            f"cannot register from here: {screen.message()}. Open FACT on the "
            "screen the plan starts from, then run this again.",
        )
    output(f"  starting screen: {screen.message()}")

    try:
        draft = draft_automation(
            config,
            name=name,
            entry_state=screen.state_id,
            states=authored_states,
        )
        draft.validate_states(table.ids())
        # Refuse an invalid plan before attaching to FACT or running a proof.
        # Otherwise registration can appear to work while the later launcher
        # has no filler process to start.
        draft.validate_scripts()
    except AutomationError as exc:
        return RegisterResult(REFUSED, name, str(exc))

    missing = [key for key in draft.fill_keys() if key not in (values or {})]
    if missing:
        return RegisterResult(
            REFUSED,
            name,
            "the proof run needs a value for: "
            + ", ".join(repr(key) for key in missing)
            + "; use --values with a JSON object",
        )

    proof_config = draft.to_config()
    try:
        window = attach(proof_config.app)
        inspection = inspect(window)
    except (FactError, WindowError, UIAError) as exc:
        return RegisterResult(FAILED, name, f"cannot read the target window: {exc}")

    def refresh() -> tuple[WindowInfo, UIAInspection]:
        """Re-attach after a click, because FACT may have changed windows."""
        try:
            fresh_window = attach(proof_config.app)
            return fresh_window, inspect(fresh_window)
        except (FactError, WindowError, UIAError) as exc:
            raise UIAError(str(exc)) from exc

    # A script types the header and the rows into the form it finds. Proving it
    # on top of a form the wizard already filled would enter the document a
    # second time - and then Save both copies - so the proof starts clean.
    if any(isinstance(item, ScriptStep) for item in proof_config.workflow.steps):
        output("  before proving: closing any form left open by the wizard")
        try:
            closed = close_form(
                window, proof_config, output=output, sleep=sleep
            )
        except (WindowError, UIAError) as exc:
            closed = False
            output(f"  the open form could not be checked: {exc}")
        if not closed:
            return RegisterResult(
                FAILED,
                name,
                "a form left open by an earlier pass would not close, so the "
                "script would have typed into a half-filled document; nothing "
                "was registered",
            )
        try:
            window, inspection = refresh()
        except UIAError as exc:
            return RegisterResult(
                FAILED,
                name,
                f"the screen could not be read after closing the open form "
                f"({exc}); nothing was registered",
            )

    observed: list[str] = []

    def make_before_step(offset: int, where: str = "") -> Callable[[int, FormStep], None]:
        """Record the screen each step starts on; the same screen every row."""

        def before_step(position: int, _step: FormStep) -> None:
            match = detect_now()
            if not match.known:
                raise ProofError(
                    f"{where}step {start + position} would start on a screen the "
                    f"state table does not know: {match.message()}"
                )
            # The first pass teaches the checkpoint; later rows must see the
            # same screen, and are held to it by the run itself.
            if len(observed) < len(draft.steps) - start:
                observed.append(match.state_id)
            output(
                f"  {where}step {start + position}: starts on {match.state_id}"
                if not where
                else f"  {where}: starts on {match.state_id}"
            )

        return before_step

    # The plan's opening steps usually ran while it was being written: the
    # wizard clicks them on the live app. Those checkpoints are observed facts
    # already, so the proof does not replay navigation the app has left behind -
    # it begins at the first step that belongs on the screen showing now.
    planned = _planned_steps(proof_config)
    start = 0
    here = detect_now()
    if authored_states and here.known:
        start = proof_start(draft.steps, here.state_id)
        # Only a step whose screen is known may be skipped. A script or a
        # designed segment carries no recorded screen, so the proof still has
        # to see it - and the steps after it - itself.
        if start and not all(
            authored_states[position] for position, _ in planned[:start]
        ):
            start = 0
    proving = [item for _, item in planned[start:]]

    if start:
        output(
            f"  proving step(s) {start + 1}-{len(draft.steps)}: steps 1-{start} "
            "ran while you were adding them, so their screens are known"
        )
    else:
        output(
            f"  proving {len(draft.steps)} step(s); Save is clicked once at the end"
        )
    try:
        result = execute(
            proving,
            dict(values or {}),
            window=window,
            inspection=inspection,
            labels={item.name: item.field for item in proof_config.fields},
            types={item.name: item.type for item in proof_config.fields},
            delay=proof_config.safety.action_delay_seconds,
            pause_after_click=proof_config.workflow.pause_after_click_seconds,
            require_foreground=proof_config.app.require_foreground,
            settle_seconds=SETTLE_SECONDS,
            # The proof runs a ``run`` step for real, against the same values
            # file a later run will be given.
            values_file=values_file,
            before_step=make_before_step(0),
            reacquire=refresh,
            sleep=sleep,
            output=output,
        )
    except ProofError as exc:
        return RegisterResult(
            FAILED,
            name,
            f"{exc}. Nothing was registered; get the app to the right screen "
            "and try again.",
            steps_run=len(observed),
        )
    except (WindowError, UIAError) as exc:
        return RegisterResult(
            FAILED,
            name,
            f"the proof run stopped: {exc}. Nothing was registered.",
            steps_run=len(observed),
        )

    for warning in result.warnings:
        output(f"  note: {warning}")
    if result.problems:
        return RegisterResult(
            FAILED,
            name,
            "the proof run did not complete: "
            + "; ".join(result.problems)
            + ". Nothing was registered.",
            steps_run=result.steps_run,
        )
    if len(observed) != len(draft.steps) - start:
        return RegisterResult(
            FAILED,
            name,
            f"only {len(observed)} of {len(draft.steps) - start} step(s) ran, "
            "so their checkpoints are unknown. Nothing was registered.",
            steps_run=result.steps_run,
        )

    try:
        assign_checkpoints(draft, observed, start=start)
        draft.validate_states(table.ids())
    except (RegisterError, AutomationError) as exc:
        return RegisterResult(FAILED, name, str(exc), steps_run=result.steps_run)

    if not check_save:
        # No proof save is attempted: an app that refuses to write would only
        # produce a dialog to clear up, and there would be nothing to look for
        # anyway. What was proved is the steps, and that is what a run repeats.
        draft.save.check = False
        draft.save.verifier = "none"
        draft.save.footprint = []
        draft.verified = False
        draft.touch()
        output("  save: not clicked; this plan does not check the save")
        output(
            "  save check: off - a run clicks Save and does not look for the "
            "record afterwards"
        )
    else:
        # The save is clicked by the runner on the screen that is showing now,
        # not on the window the first step started from.
        try:
            window, inspection = refresh()
        except UIAError as exc:
            return RegisterResult(
                FAILED,
                name,
                f"the proof save could not re-read the current screen ({exc}); "
                "nothing was registered",
                steps_run=result.steps_run,
            )

        # The save is the proof: one click, at the end, exactly as a run does it.
        save_values = dict(values or {})
        save_call = save_call or save_and_verify
        output("  save: clicking once to prove the record saves")
        try:
            saved = save_call(
                config=proof_config,
                window=window,
                inspection=inspection,
                fields=result.fields,
                values=save_values,
                confirmation=confirmation,
                screenshot_dir=screenshot_dir,
                record_label="proof",
                attempt=1,
                output=output,
                sleep=sleep,
            )
        except (WindowError, UIAError, OSError) as exc:
            return RegisterResult(
                FAILED,
                name,
                f"the proof save failed partway ({exc}); nothing was registered",
                steps_run=result.steps_run,
            )
        if not saved or not saved.outcome.outcome:
            reason = saved.outcome.reason if saved is not None else "no result"
            return RegisterResult(
                FAILED,
                name,
                f"the proof save was not confirmed ({reason}); nothing was "
                "registered",
                steps_run=result.steps_run,
            )
        output(f"  save: confirmed ({saved.outcome.reason})")

        # The footprint is what a later run looks for to know a save worked. It
        # is read from this proof save - the value that appeared on screen -
        # rather than asked for, because the app is the only reliable witness.
        candidates = list(draft.save.footprint)
        footprint = footprint_for(
            saved.before, saved.after, save_values, candidates
        ) or candidates
        if footprint:
            draft.save.verifier = "row"
            draft.save.footprint = list(footprint)
            output(
                "  save check: "
                + ", ".join(repr(key) for key in footprint)
                + " is looked for on the screen after each save"
            )
        else:
            # Nothing appeared that could be recognised, so a local check would
            # answer UNKNOWN every time. Say so instead of pretending to check.
            draft.save.verifier = "none"
            draft.save.footprint = []
            output(
                "  save check: nothing new appeared on screen after Save, so "
                "later saves are not checked"
            )

        draft.verified = True
        draft.touch()

    path = None
    launcher = None
    if write:
        try:
            path = save_automation(draft, automations_root)
        except (AutomationError, OSError) as exc:
            return RegisterResult(
                FAILED, name, f"the automation could not be written: {exc}"
            )
        try:
            launcher = write_launcher(name, automations_root=automations_root)
        except OSError as exc:
            # The registration itself succeeded; a missing launcher is worth a
            # warning, not a lost plan.
            output(f"warning: the launcher could not be written: {exc}")

    status = REGISTERED if write else DRAFTED
    return RegisterResult(
        status=status,
        name=name,
        message=(
            (
                "the proof record was saved and confirmed; the plan is ready to run"
                if check_save
                else "the steps were proved; runs click Save without checking it"
            )
            if write
            else "dry run: nothing was written"
        ),
        path=path,
        automation=draft,
        steps_run=result.steps_run,
        launcher=launcher,
    )


__all__ = [
    "DRAFTED",
    "FAILED",
    "REFUSED",
    "REGISTERED",
    "RESUME_FOR",
    "ProofError",
    "RegisterError",
    "RegisterResult",
    "assign_checkpoints",
    "describe_checkpoints",
    "proof_start",
    "draft_automation",
    "register_automation",
]
