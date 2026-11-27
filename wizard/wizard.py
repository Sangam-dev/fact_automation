"""Interactive GUI workflow wizard.

This is the planner/executor loop for FACT ERP.NG, with no workbook in it:

1. Attach to FACT ERP.NG by *screen*, not by a window picked from a list.
2. Collect the business data field names and a sample record.
3. For each step: screenshot + UIA hierarchy -> natural-language instruction ->
   AI plan -> validate the named controls -> show the plan -> approve, correct,
   or replan -> execute the approved actions.
4. Write the config: the plan's last save click becomes the save action, which
   is clicked once at the end rather than once per entry. Proving the plan -
   and the record that proves it - happens when it is registered.

Business data is supplied separately from the workflow design: the wizard asks
for field names and a sample record, and the saved config contains only the
workflow, never the record.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import (
    AgentConfig,
    get_gemini_model,
    load_env,
    project_root,
)
from desktop.state.fact import (
    FACT_PROCESS,
    FactError,
    FactScreen,
    read_current,
    read_screen,
    wait_for_screen,
)
from desktop.uia.uia import UIAError
from execution.actions.engine import SETTLE_SECONDS, execute_form_steps
from execution.workflows.runner import load_values


def _prompt(text: str, default: str | None = None) -> str:
    """Prompt the user for input, displaying an optional default."""
    if default is not None and default != "":
        display = f"{text} [{default}]: "
    else:
        display = f"{text}: "
    try:
        raw = input(display).strip()
    except (KeyboardInterrupt, EOFError):
        print("\nWizard cancelled.")
        sys.exit(0)
    if not raw and default is not None:
        return default
    return raw


def _prompt_yes_no(question: str, default_yes: bool = True) -> bool:
    """Prompt for a yes/no answer."""
    hint = "[Y/n]" if default_yes else "[y/N]"
    try:
        raw = input(f"{question} {hint}: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print("\nWizard cancelled.")
        sys.exit(0)
    if not raw:
        return default_yes
    return raw in ("y", "yes")


def _clean_path(path_str: str) -> str:
    """Clean quoted or drag-and-dropped file paths."""
    clean = path_str.strip()
    if (clean.startswith('"') and clean.endswith('"')) or (
        clean.startswith("'") and clean.endswith("'")
    ):
        clean = clean[1:-1]
    return clean.strip()


def _attach_fact() -> tuple[str, str] | None:
    """Attach to FACT ERP.NG, without ever listing the desktop's windows.

    The wizard is for one application. Asking the recogniser which FACT screen
    is showing also means the wizard starts on whatever form the user is on
    (Login, the menu, a Sale Order) instead of binding to a stale title.
    """
    print()
    print("------------------------------------------------------------------------")
    print("  STEP 1: FACT ERP.NG")
    print("------------------------------------------------------------------------")
    print(f"Looking for {FACT_PROCESS} and reading the screen it is showing...")
    try:
        window, match = wait_for_screen(seconds=20.0)
    except FactError as exc:
        print(f"[WARN] {exc}")
        print(
            "Open FACT ERP.NG, leave it on a screen the state table knows "
            "(Login, Company Open, the main menu, or a Sale Order form), then "
            "run the wizard again."
        )
        return None
    print(f"[ OK ] {match.message()}")
    if match.stack:
        print("       behind: " + " < ".join(match.stack))
    print(
        f"       window 0x{window.handle:X} {window.title!r} "
        f"({window.process_name or FACT_PROCESS})"
    )
    # The title is deliberately left empty: FACT changes window titles between
    # screens, so the process name plus the recogniser is the stable target.
    return "", FACT_PROCESS


def _read_fact_screen(seconds: float = 10.0) -> FactScreen | None:
    """Capture and inspect the FACT screen that is on top right now."""
    try:
        return read_screen(seconds=seconds)
    except FactError as exc:
        print(f"[WARN] {exc}")
        return None


def _reacquire_fact() -> tuple[Any, Any]:
    """Re-resolve the FACT window after a click (the executor's refresh hook)."""
    try:
        window, inspection, _match = read_current(seconds=5.0)
    except FactError as exc:
        # The executor knows how to report a UIA failure; a FactError escaping
        # to the chat window would only produce a traceback.
        raise UIAError(str(exc)) from exc
    return window, inspection


def _step_session_dir() -> Path:
    """Where the screenshots of one setup session are kept."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return project_root() / "data" / "steps" / stamp


def _save_shot(capture: Any, directory: Path, name: str) -> Path:
    """Save one screenshot and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    capture.image.save(path)
    return path


def _take_save_step(
    steps: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], bool]:
    """The save button the plan ends with, and the plan without that step.

    The wizard no longer asks which button saves: the last click in the plan is
    the one the AI found, and it becomes the save action that the runner clicks
    once, after the last row. A plan whose last click is not a save keeps its
    steps and saves on the default button.
    """
    if not steps:
        return "Save", steps, False
    last = steps[-1]
    if last.get("action") != "click":
        return "Save", steps, False
    button = str(last.get("button") or "").strip()
    if "save" not in button.casefold():
        return "Save", steps, False
    return button, steps[:-1], True


def _describe_step(step: dict) -> str:
    """One line that names the work a step does."""
    action = step.get("action")
    if action == "run":
        return f"run the script {step.get('script', '')!r}"
    if action == "key":
        return f"press {step.get('keys', '')!r}"
    if action in ("click", "double_click"):
        verb = "double-click" if action == "double_click" else "click"
        description = f"{verb} {step.get('button', '')!r}"
        wait = step.get("wait_for_progress")
        if action == "click" and wait:
            description += (
                f" after progress bar {wait.get('automation_id', '')!r} reaches "
                f"{wait.get('target_percent', 100):g}%"
            )
        return description
    label = step.get("field") or step.get("key", "")
    return f"fill {label!r} from field {step.get('key', '')!r}"


def _form_steps(plan: Any) -> list[Any]:
    """Turn a resolved plan into the executor's own step models."""
    from execution.actions.models import (
        ClickStep,
        DoubleClickStep,
        FillStep,
        KeyStep,
    )

    if plan.kind == "click":
        return [
            ClickStep(
                action="click",
                button=plan.label,
                wait_for_progress=plan.wait_for_progress,
            )
        ]
    if plan.kind == "double_click":
        return [DoubleClickStep(action="double_click", button=plan.label)]
    if plan.kind == "key":
        return [KeyStep(action="key", keys=plan.keypress)]
    return [
        FillStep(action="fill", key=fill.key, field=fill.label)
        for fill in plan.fills
    ]


def _segment_steps(segment: Any) -> list[dict[str, Any]]:
    """A designed segment's steps, in the order it declares them.

    The segment's own checkpoints are deliberately dropped: registration proves
    every screen by watching it, and an observed checkpoint is a fact about this
    application where a declared one is only an intention.
    """
    entries: list[dict[str, Any]] = []
    for step in segment.steps:
        entry: dict[str, Any] = {"action": step.action, "optional": step.optional}
        if step.action in ("click", "double_click"):
            entry["button"] = step.button
            if step.wait_for_progress is not None:
                entry["wait_for_progress"] = (
                    step.wait_for_progress.model_dump()
                )
        elif step.action == "key":
            entry["keys"] = step.keys
        elif step.action == "run":
            entry["script"] = step.script
            entry["args"] = list(step.args)
            entry["timeout_seconds"] = step.timeout_seconds
        else:
            entry["key"] = step.key
            if step.field:
                entry["field"] = step.field
        entries.append(entry)
    return entries


def _print_step_plan(
    plan: Any,
    *,
    index: int,
    instruction: str,
    screenshot: Path,
    field_names: list[str],
    types: dict[str, str],
) -> None:
    """Show what the agent understood, before it touches the screen."""
    print()
    print("-" * 72)
    print(f"  Step {index}: {instruction}")
    print(f"  Screen   : {screenshot}")
    print("  I will do this, in order:")
    if plan.kind == "key":
        print(f"    1. press {plan.keypress!r}")
    elif plan.kind == "double_click":
        print(f"    1. double-click {plan.label!r}")
    elif plan.kind == "click":
        print(f"    1. click {plan.label!r}")
        if plan.wait_for_progress is not None:
            wait = plan.wait_for_progress
            print(
                f"       after progress bar {wait.automation_id!r} reaches "
                f"{wait.target_percent:g}% "
                f"(timeout {wait.timeout_seconds:g}s; "
                f"{wait.stable_reads} stable reads)"
            )
    else:
        for position, fill in enumerate(plan.fills, start=1):
            detail = types.get(fill.key, "?")
            if fill.confidence:
                detail += f", {fill.confidence:.2f}"
            print(
                f"    {position}. fill {fill.label!r} <- field "
                f"{fill.key!r} ({detail})"
            )
        touched = set(plan.keys)
        other = [name for name in field_names if name not in touched]
        if other:
            print("  Not filled by this step: " + ", ".join(other))
    print("-" * 72)


def _review_step_plan(problem: str | None = None) -> tuple[str, str]:
    """Ask what to do with the resolved step.

    Returns ``("done", "")`` to run it now, ``("redo", "")`` to look at the
    screen again, or ``("edit", correction)`` when the user said what is wrong.
    With a *problem*, the Enter default becomes ``redo``.
    """
    if problem:
        print(f"  {problem}")
    print()
    print("  [Enter] done - do it now on the screen")
    print("  [r]     redo - look at the screen again")
    print("  or type what is wrong, in your words")
    answer = _prompt("Your answer", default="").strip()
    lowered = answer.casefold()
    if lowered in ("", "d", "done"):
        return ("redo" if problem else "done"), ""
    if lowered in ("r", "redo"):
        return "redo", ""
    return "edit", answer


def _ask_field_names() -> list[str]:
    """Ask which business data fields this workflow enters.

    Optional: pressing Enter with no names continues to the next step. A
    workflow built that way can still click, but it has no declared data keys
    to fill, so the planner will only be able to plan clicks.
    """
    print()
    print("------------------------------------------------------------------------")
    print("  STEP 2: Business Data Fields")
    print("------------------------------------------------------------------------")
    print("Name the fields this workflow types into the app. These names are the")
    print("keys the AI plans against; sample values are asked for separately and")
    print("are never written into the saved workflow.")
    raw = _prompt("Field names, comma separated (Enter to skip)", default="")
    names = [item.strip() for item in raw.split(",") if item.strip()]
    if not names:
        print("  Skipped: no data keys declared, so only click steps can be planned.")
    return list(dict.fromkeys(names))


def _ask_sample_values(field_names: list[str]) -> dict[str, str]:
    """Ask for one sample value per field, used to prove the workflow."""
    print()
    print("------------------------------------------------------------------------")
    print("  STEP 3: Sample Record")
    print("------------------------------------------------------------------------")
    print("The sample values are used to prove the workflow now. They are kept in")
    print("memory only; the saved workflow holds no business data.")
    values: dict[str, str] = {}
    for name in field_names:
        values[name] = _prompt(f"Sample value for {name!r}", default="")
    return values


def _config_data(
    app_title: str,
    process_name: str | None,
    save_button: str,
    *,
    steps: list[dict[str, Any]],
    fields: list[dict[str, Any]],
    confirmation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the configuration a wizard run writes."""
    config_data: dict[str, Any] = {
        "app": {
            "title_contains": app_title,
            "process_name": process_name,
            "require_foreground": True,
            "pin_window": True,
        },
        "fields": fields,
        "actions": {
            "save_button": save_button,
            "new_record_button": None,
            "wait_after_save_seconds": 1.0,
        },
        "workflow": {
            "steps": steps,
            "pause_after_click_seconds": 0.5,
        },
        "gemini": {
            # Whatever .env says, not a literal: the file records the model
            # that actually planned and proved these steps.
            "model": get_gemini_model(),
            "max_image_dimension": 1600,
            "timeout_seconds": 60,
            "max_retries": 3,
            "max_ai_calls": 50,
            "min_layout_confidence": 0.6,
            "min_mapping_confidence": 0.6,
        },
        "confirmation": confirmation
        or {
            "success_region": None,
            "error_region": None,
            "success_pattern": "",
            "error_patterns": [],
        },
        "safety": {
            "stop_hotkey": "alt+t",
            "pause_hotkey": "alt+p",
            "resume_hotkey": "alt+r",
            "block_external_input": True,
            "max_attempts": 3,
            "action_delay_seconds": 0.15,
            "stop_on_failure": True,
        },
    }
    return config_data


def _preview_config(app_title: str, process_name: str | None) -> AgentConfig:
    """The app/Gemini settings used before any step exists."""
    return AgentConfig.model_validate(
        _config_data(
            app_title,
            process_name,
            save_button="Save",
            steps=[{"action": "click", "button": "Save"}],
            fields=[],
        )
    )


def _generate_config(
    app_title: str,
    process_name: str | None,
    save_button: str,
    *,
    steps: list[dict[str, Any]],
    fields: list[dict[str, Any]],
    confirmation: dict[str, Any] | None = None,
    filename: str = "workflow.json",
    path: str | Path | None = None,
) -> Path:
    """Generate and write a workflow configuration file."""
    config_path = (
        Path(path)
        if path is not None
        else project_root() / "config" / "generated" / filename
    )
    if not config_path.is_absolute():
        config_path = project_root() / config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)

    config_data = _config_data(
        app_title,
        process_name,
        save_button,
        steps=steps,
        fields=fields,
        confirmation=confirmation,
    )
    config_path.write_text(json.dumps(config_data, indent=2), encoding="utf-8")
    return config_path


@dataclass(frozen=True)
class WizardResult:
    """What one wizard session produced."""

    config_path: Path | None = None
    steps: tuple[dict[str, Any], ...] = ()
    saved: bool = False
    status: str = ""


def run_wizard(
    config_path: str | None = None,
    env_path: str | None = None,
    values_path: str | None = None,
) -> int:
    """Main interactive workflow wizard."""
    from ai.planners.step_planner import (
        StepPlanError,
        StepUnsupported,
        plan_step,
    )
    from ai.providers.gemini import GeminiClient, GeminiError

    load_env(env_path)

    # One file is the record, and the wizard, the proof and any script step all
    # read it: the project's own input, unless another one was named.
    if values_path is None:
        default_values = project_root() / "data" / "data.json"
        if default_values.is_file():
            values_path = str(default_values)

    print("=" * 72)
    print("  FACT Automation - Workflow Wizard")
    print("=" * 72)
    if values_path:
        print(f"  Sample record: {values_path}")

    # 1. Target application: FACT ERP.NG only, found by the screen recogniser.
    target = _attach_fact()
    if target is None:
        return 1
    app_title, process_name = target

    # 2. Business data fields + sample record (kept out of the saved workflow)
    field_names = _ask_field_names()
    values: dict[str, str] = {}
    if values_path:
        try:
            values = load_values(values_path)
        except Exception as exc:  # a missing sample file must not abort the wizard
            print(f"[WARN] {exc}")
    missing = [name for name in field_names if name not in values]
    if missing:
        values.update(_ask_sample_values(missing))

    preview = _preview_config(app_title, process_name)
    try:
        client = GeminiClient(preview.gemini)
    except GeminiError as exc:
        print(f"[WARN] {exc}")
        return 1

    print()
    print("------------------------------------------------------------------------")
    print("  STEP 4: Your Steps")
    print("------------------------------------------------------------------------")
    print("Reading the controls this window exposes to Windows UI Automation...")
    live = _read_fact_screen()
    if live is None:
        print("Open FACT again and re-run the wizard if the window went away.")
        return 1
    window, inspection = live.window, live.inspection
    print(f"[ OK ] Detected {len(inspection.candidates)} control(s) on {live.match.state_id}.")
    if len(inspection.candidates) < 3:
        print(
            "[WARN] This window exposes almost no controls. Keep it restored "
            "(not minimized) and in front while you add a step; a browser "
            "only exposes its page controls while the page is drawn."
        )

    session = _step_session_dir()
    #: The plan itself, in the order it will run. Everything the wizard adds -
    #: an AI step, a designed segment, a script - goes on the end, so "add this
    #: segment, then keep going" is simply the order things were added in.
    steps: list[dict] = []
    #: The screen each step was written on, position for position with
    #: ``steps`` - ``None`` where nothing was watching (a script, a segment).
    #: Registration proves what is left of the plan from the screen FACT shows,
    #: so it has to know where the earlier steps belong.
    authored_states: list[str | None] = []
    #: Only for refusing the same segment twice; the plan holds its steps.
    chosen_segments: list[str] = []
    labels: dict[str, str] = {}
    types: dict[str, str] = {name: "text" for name in field_names}
    live_values: dict[str, str] = {}

    pause_after_click = 0.5

    while True:
        print()
        print(f"  Your steps ({len(steps)} so far):")
        if steps:
            for position, step in enumerate(steps, start=1):
                print(f"    {position}. {_describe_step(step)}")
        else:
            print("    (none yet)")
        print("  [1] Add a step")
        print("  [2] Start automation")
        print("  [3] Add a deterministic automation (a script or designed steps)")
        print("  [4] Add a script by path (one that is not in execution/scripts)")
        choice = _prompt("Choose 1, 2, 3 or 4", default="1").strip().casefold()

        if choice.startswith("2") or choice.startswith("s"):
            if not steps:
                print("No steps yet, so there is nothing to run.")
                continue
            if field_names and not any(
                step["action"] in ("fill", "run") for step in steps
            ):
                print(
                    "No step fills anything yet - add the step that types "
                    "the data."
                )
                continue
            break
        if choice.startswith("3") or choice.startswith("det"):
            from core.automation.scripts import (
                DEFAULT_SCRIPT_ARGS,
                ScriptError,
                find_scripts,
                get_script,
                script_step,
            )
            from core.automation.segments import SegmentError, load_segments

            # One menu for both halves of "deterministic": the scripts that
            # were written, and the steps that were designed. Either one can be
            # inserted here, and authoring simply continues after it.
            try:
                table = load_segments()
            except SegmentError as exc:
                print(f"  {exc}")
                table = None
            scripts = find_scripts()
            designed = list(table.segments) if table is not None else []
            if not scripts and not designed:
                print("  Nothing to insert yet: a script in execution/scripts/ or a ")
                print("  designed segment in config/segments/ shows up here.")
                continue

            choices: list[tuple[str, str]] = []
            print()
            print("  Deterministic automations you can insert here:")
            for script in scripts:
                choices.append(("script", script.id))
                print(f"    {len(choices)}. [script] {script.describe()}")
            for segment in designed:
                choices.append(("steps", segment.id))
                print(
                    f"    {len(choices)}. [steps]  {segment.id}: "
                    f"{len(segment.steps)} step(s) on "
                    f"{', '.join(segment.state_ids())}"
                )
            picked = _prompt(
                "  Which one (number or id, Enter to cancel)", default=""
            ).strip()
            if not picked:
                continue
            chosen = None
            if picked.isdigit():
                position = int(picked)
                if 1 <= position <= len(choices):
                    chosen = choices[position - 1]
            else:
                chosen = next(
                    (item for item in choices if item[1] == picked), None
                )
            if chosen is None:
                print(f"  There is nothing called {picked!r} in that list.")
                continue

            kind, name = chosen
            if kind == "script":
                try:
                    entry = get_script(name)
                except ScriptError as exc:
                    print(f"  {exc}")
                    continue
                raw = _prompt(
                    "  Arguments, space separated "
                    "({values_file} is the values JSON)",
                    default=" ".join(DEFAULT_SCRIPT_ARGS),
                )
                steps.append(script_step(entry, args=raw.split()))
                authored_states.append(None)
                print(
                    f"  {name!r} runs here, after the {len(steps) - 1} step(s) "
                    "above it. It types into the form itself and never clicks Save."
                )
                continue

            if table is None:
                continue
            try:
                segment = table.get(name)
            except SegmentError as exc:
                print(f"  {exc}")
                continue
            if name in chosen_segments:
                print(f"  {name!r} is already in this workflow.")
                continue
            # Its steps join the plan here, in this order, so anything added
            # next runs after them - which is the whole point of inserting it.
            added = _segment_steps(segment)
            steps.extend(added)
            authored_states.extend([None] * len(added))
            for field in segment.fields:
                labels.setdefault(field.name, field.field)
                types.setdefault(field.name, field.type)
            extra = [field.name for field in segment.fields if field.name not in values]
            if extra:
                print("  It types these fields; give one sample value each so the ")
                print("  proof can run it now:")
                values.update(_ask_sample_values(extra))
            chosen_segments.append(name)
            print(
                f"  {name!r}: {len(segment.steps)} step(s) added here. Anything "
                "you add next runs after them."
            )
            continue

        if choice.startswith("4") or choice.startswith("scr"):
            from core.automation.scripts import find_scripts

            known = find_scripts()
            script = _clean_path(
                _prompt(
                    "  Script to run at this point (path from the project root)",
                    default=known[0].path if known else "",
                )
            )
            if not script:
                continue
            raw_args = _prompt(
                "  Arguments, space separated ({values_file} is the values JSON)",
                default="{values_file}",
            )
            steps.append(
                {
                    "action": "run",
                    "script": script,
                    "args": raw_args.split(),
                    "timeout_seconds": 900.0,
                }
            )
            authored_states.append(None)
            print(
                f"  {script!r} will run here, after the {len(steps) - 1} step(s) "
                "above it. It types into the form itself and never clicks Save."
            )
            continue

        if choice not in ("", "1", "a", "add"):
            print("Enter 1 to add a step, 2 to start automation, 3 to insert a")
            print("deterministic automation, or 4 to run a script by path.")
            continue

        index = len(steps) + 1
        instruction = _prompt(
            f"Step {index} - what should the agent do here, in your words"
        )
        if not instruction:
            continue

        # Ask the recogniser again for every step: the previous click may have
        # closed one FACT window and opened another (Login -> Main).
        live = _read_fact_screen(seconds=5.0)
        if live is None:
            print("  Get FACT back to a known screen and add the step again.")
            continue
        capture, inspection, window = live.capture, live.inspection, live.window
        shot = _save_shot(capture, session, f"step-{index:02d}-before.png")
        print(f"  Screen saved: {shot}")
        # A click can open a dialog, so the control list has to come from the
        # same moment as the screenshot - not from before the earlier steps.
        print(
            f"  {len(inspection.candidates)} control(s) on {live.match.state_id} "
            "right now."
        )

        correction = ""
        plan: Any | None = None
        problem = ""
        while True:
            if plan is None:
                try:
                    plan = plan_step(
                        client,
                        capture,
                        inspection.candidates,
                        field_names,
                        instruction,
                        preview.gemini,
                        correction=correction,
                    )
                    problem = ""
                except StepUnsupported as exc:
                    problem = (
                        f"Not supported yet: {exc} Clicking buttons and typing "
                        "into text fields are the only actions in this mode."
                    )
                except (StepPlanError, GeminiError) as exc:
                    problem = str(exc)
            if plan is not None:
                _print_step_plan(
                    plan,
                    index=index,
                    instruction=instruction,
                    screenshot=shot,
                    field_names=field_names,
                    types=types,
                )
            action, fix = _review_step_plan(problem or None)
            if action == "done":
                break
            if action == "edit":
                correction = fix
                plan = None
                continue
            live = _read_fact_screen(seconds=5.0)
            if live is None:
                print("  Get FACT back to a known screen and add the step again.")
                break
            capture, inspection, window = live.capture, live.inspection, live.window
            shot = _save_shot(capture, session, f"step-{index:02d}-before.png")
            print(f"  Screen saved: {shot}")
            print(
                f"  {len(inspection.candidates)} control(s) on "
                f"{live.match.state_id} right now."
            )
            plan = None

        if plan is None:
            continue

        # The plan is about to run on the live screen, so re-resolve the window
        # once more instead of trusting the handle from before the review.
        live = _read_fact_screen(seconds=5.0)
        if live is None:
            print("  Get FACT back to a known screen and add the step again.")
            continue
        inspection, window = live.inspection, live.window

        step_values = {
            key: values.get(key, "") for key in getattr(plan, "keys", ())
        }
        result = execute_form_steps(
            _form_steps(plan),
            step_values,
            window=window,
            inspection=inspection,
            labels={},
            types=types,
            delay=preview.safety.action_delay_seconds,
            pause_after_click=pause_after_click,
            settle_seconds=SETTLE_SECONDS,
            reacquire=_reacquire_fact,
        )
        for note in result.warnings:
            print(f"  note: {note}")
        if result.problems:
            print(f"  The screen did not take that: {'; '.join(result.problems)}")
            if plan.kind in ("click", "double_click") and "save" in str(
                plan.label
            ).casefold():
                # The click did happen; what followed was the app answering it.
                # Saving is not a step to add - and a plan that carried it would
                # save the record twice.
                print(
                    "  Save is not a step you add: it is clicked once, after "
                    "your last step. Clear the box the app put up (if any) and "
                    "carry on adding what comes before it."
                )
            else:
                print(
                    "  The form is left as it is; fix it by hand and add the "
                    "step again."
                )
            continue

        live_values.update(step_values)
        # The screen this step ran on, kept for registration: it is what lets a
        # proof start where the app is instead of replaying the plan from its
        # first step, which the app has already left behind.
        authored = live.match.state_id
        if plan.kind in ("click", "double_click"):
            step_data: dict[str, Any] = {
                "action": plan.kind,
                "button": plan.label,
            }
            if plan.wait_for_progress is not None:
                step_data["wait_for_progress"] = (
                    plan.wait_for_progress.model_dump()
                )
            steps.append(step_data)
            authored_states.append(authored)
        elif plan.kind == "key":
            steps.append({"action": "key", "keys": plan.keypress})
            authored_states.append(authored)
        else:
            for fill in plan.fills:
                labels.setdefault(fill.key, fill.label)
                steps.append(
                    {
                        "action": "fill",
                        "key": fill.key,
                        "field": fill.label,
                    }
                )
                authored_states.append(authored)
        print(f"[ OK ] Step {index} done on the live screen.")

    print()
    # Save is not a question any more: the plan itself ends with the click that
    # saves. The button the AI chose is taken out of the steps and becomes the
    # save action, which the runner clicks exactly once, after the last step.
    save_button, steps, dropped = _take_save_step(steps)
    if dropped and len(authored_states) == len(steps) + 1:
        # The save click left the plan, so its screen leaves with it.
        authored_states.pop()
    if dropped:
        print(
            f"  Save is the last step of the plan ({save_button!r}); it is clicked "
            "once, after the last row."
        )
    # Every key the plan actually types, in the order it met them - including
    # the ones a designed segment brought, which no AI step ever saw.
    field_configs = [
        {"name": name, "field": label, "type": types.get(name, "text")}
        for name, label in labels.items()
    ]
    output_path = (
        Path(config_path)
        if config_path is not None
        else project_root() / "config" / "generated" / "workflow.json"
    )
    config_file = _generate_config(
        app_title=app_title,
        process_name=process_name,
        save_button=save_button,
        steps=steps,
        fields=field_configs,
        path=output_path,
    )

    print()
    print("=" * 72)
    print("  WORKFLOW READY")
    print("=" * 72)
    for index, step in enumerate(steps, start=1):
        print(f"  {index}. {_describe_step(step)}")
    print(f"  Save button : {save_button} (clicked once, at the end)")
    print(f"  Config      : {config_file}")
    print("  Safety      : Alt+T (Stop) | Alt+P (Pause) | Alt+R (Resume)")
    print("=" * 72)
    print("  The workflow is written but not yet proved: proving it runs the whole")
    print("  plan and saves one record. Registering below does that.")

    # Save it as a resumable automation too. The proof pass runs the steps once
    # more without clicking Save, which is what teaches each step its screen.
    print()
    if _prompt_yes_no(
        "Register this as a reusable automation now? (it runs the whole plan "
        "and saves one record)",
        default_yes=False,
    ):
        from wizard.register import describe_checkpoints, register_automation

        name = _prompt("  Automation name", default="fact_automation").strip()
        print()
        # Answer no when the app refuses to save the record (a trial build, a
        # read-only database): the steps are still worth keeping, and there is
        # nothing a save check could ever confirm.
        check_save = _prompt_yes_no(
            "  Should a run check that Save actually saved the record?",
            default_yes=True,
        )
        if not check_save:
            print(
                "  Save will be clicked and not checked, so a run finishes as "
                "soon as it clicks."
            )
        print()
        result = register_automation(
            name=name,
            config_path=config_file,
            values=live_values,
            check_save=check_save,
            authored_states=authored_states,
            # A ``run`` step in the plan is proved for real, so it gets the
            # same values file every later run will be given.
            values_file=values_path,
        )
        if result.automation is not None:
            print("  Checkpoints proved by the run:")
            for line in describe_checkpoints(result.automation):
                print(f"    {line}")
        if result.path is not None:
            print(f"  Automation saved: {result.path}")
        if result.launcher is not None:
            print(f"  Launcher:         {result.launcher}")
        if result.ok:
            print(
                f"  Run it with: python cli.py automation run {name} "
                "--values <values.json>"
            )
            if result.launcher is not None:
                print(
                    "  or double-click the launcher, with a values.json beside it"
                )
        else:
            print(f"  Not registered: {result.message}")
            print("  The workflow itself is saved and can still be run.")

    return 0


__all__ = [
    "WizardResult",
    "run_wizard",
]
