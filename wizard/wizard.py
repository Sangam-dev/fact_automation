"""Interactive GUI workflow wizard.

This is the generic planner/executor loop, with no workbook in it:

1. Pick the target application window.
2. Collect the business data field names and a sample record.
3. For each step: screenshot + UIA hierarchy -> natural-language instruction ->
   AI plan -> validate the named controls -> show the plan -> approve, correct,
   or replan -> execute the approved actions.
4. Identify the Save button, record the Save check, prove the first save, and
   write a reusable workflow config.

Business data is supplied separately from the workflow design: the wizard asks
for field names and a sample record, and the saved config contains only the
workflow, never the record.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import (
    AgentConfig,
    ConfigError,
    load_config,
    load_env,
    project_root,
)
from desktop.screenshots.capture import CaptureError, capture_window
from desktop.uia.uia import UIAError, inspect_window
from desktop.windows.window import (
    WindowError,
    find_window,
    focus_window,
    list_windows,
    pin_window,
)
from execution.actions.engine import execute_form_steps
from execution.verification.controls import ResolvedTarget
from execution.verification.evidence import decide_footprint
from execution.workflows.runner import load_values, save_and_verify


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


def _select_window() -> tuple[str, str | None]:
    """Interactively select or search for an open target window."""
    print()
    print("------------------------------------------------------------------------")
    print("  STEP 1: Target Application Window")
    print("------------------------------------------------------------------------")
    print("Open windows detected on your desktop:")

    all_windows = list_windows()
    filtered = [
        w
        for w in all_windows
        if w.title
        and w.process_name.lower()
        not in ("explorer.exe", "shellexperiencehost.exe", "searchhost.exe")
    ]

    for idx, win in enumerate(filtered[:10], start=1):
        print(f"  [{idx:2d}] {win.title} ({win.process_name})")

    print()
    print("Type a number from the list above, or enter text to search by title.")
    while True:
        entry = _prompt("Window selection")
        if not entry:
            continue

        if entry.isdigit() and 1 <= int(entry) <= len(filtered):
            selected = filtered[int(entry) - 1]
            focus_window(selected)
            pin_window(selected.handle, True)
            print(
                f"[ OK ] Selected & pinned window in front: "
                f"{selected.title!r} ({selected.process_name})"
            )
            return selected.title, selected.process_name

        try:
            matched = find_window(title_contains=entry)
            focus_window(matched)
            pin_window(matched.handle, True)
            print(
                f"[ OK ] Found & pinned window in front: "
                f"{matched.title!r} ({matched.process_name})"
            )
            return matched.title, matched.process_name
        except WindowError as exc:
            print(f"Window search failed: {exc}")
            print("Please try again, or make sure your application is open and visible.")


def _inspect_live(app_title: str) -> tuple[Any, Any] | None:
    """Return ``(window, inspection)`` for the target window, or ``None``."""
    try:
        window = find_window(title_contains=app_title)
        inspection = inspect_window(window, max_elements=600)
    except (WindowError, UIAError) as exc:
        print(f"[WARN] Could not read the window's controls: {exc}")
        return None
    # inspect_window restores a minimized window, so hand back the geometry it
    # actually read instead of the snapshot taken before.
    return inspection.window, inspection


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


def _describe_step(step: dict) -> str:
    """One line that names the work a step does."""
    if step.get("action") == "click":
        return f"click {step.get('button', '')!r}"
    label = step.get("field") or step.get("key", "")
    return f"fill {label!r} from field {step.get('key', '')!r}"


def _form_steps(plan: Any) -> list[Any]:
    """Turn a resolved plan into the executor's own step models."""
    from execution.actions.models import ClickStep, FillStep

    if plan.kind == "click":
        return [ClickStep(action="click", button=plan.label)]
    return [
        FillStep(action="fill", key=fill.key, field=fill.label)
        for fill in plan.fills
    ]


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
    if plan.kind == "click":
        print(f"    1. click {plan.label!r}")
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
    """Ask which business data fields this workflow enters."""
    print()
    print("------------------------------------------------------------------------")
    print("  STEP 2: Business Data Fields")
    print("------------------------------------------------------------------------")
    print("Name the fields this workflow types into the app. These names are the")
    print("keys the AI plans against; sample values are asked for separately and")
    print("are never written into the saved workflow.")
    while True:
        raw = _prompt("Field names, comma separated", default="")
        names = [item.strip() for item in raw.split(",") if item.strip()]
        if names:
            return list(dict.fromkeys(names))
        print("Enter at least one field name.")


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


def _ask_save_button(window: Any, inspection: Any) -> str:
    """Ask for the Save button's name, showing the buttons already on screen."""
    try:
        inspection = inspect_window(window)
    except UIAError as exc:
        print(f"  note: could not re-read the window: {exc}")
    names = [
        candidate.name
        for candidate in inspection.candidates
        if candidate.control_type == "Button" and candidate.name.strip()
    ]
    # A browser window lists its own chrome first, so lead with the buttons
    # that look like Save instead of cutting them off.
    save_like = [name for name in names if "save" in name.casefold()]
    ordered = save_like + [name for name in names if name not in save_like]
    if ordered:
        shown = ", ".join(repr(name) for name in ordered[:8])
        if len(ordered) > 8:
            shown += f", and {len(ordered) - 8} more"
        print(f"  Buttons on this screen: {shown}")
    default = save_like[0] if len(save_like) == 1 else "Save"
    known = {" ".join(name.split()).casefold() for name in names}
    save_button = _prompt("Enter Save button text in your form", default=default)
    while names and " ".join(save_button.split()).casefold() not in known:
        print(f"  No button named {save_button!r} is on this screen.")
        save_button = _prompt(
            "Type the Save button's name exactly as listed above", default=default
        )
    return save_button


def _ask_confirmation() -> dict[str, Any]:
    """Ask what a successful save looks like, in plain language."""
    print()
    print("How does your app show that a save worked?")
    print("  Type a word or phrase it displays (e.g. Saved, Success,")
    print("  Record created). Leave it blank if it shows nothing - then each")
    print("  Save is checked by looking for the new record instead.")
    text = _prompt("Success text", default="")
    cleaned = text.strip()
    return {
        "success_region": None,
        "error_region": None,
        "success_pattern": f"(?i){re.escape(cleaned)}" if cleaned else "",
        "error_patterns": [
            "(?i)required",
            "(?i)invalid",
            "(?i)error",
        ]
        if cleaned
        else [],
    }


def _ask_save_check() -> str:
    """Ask how every Save should be checked.

    Returns ``"row"`` (local screen check), ``"text"`` (AI reads the screen) or
    ``"none"`` (no check at all - the record is just filled and saved).
    """
    print()
    print("Check the screen after every Save?")
    print("  [y] yes - each Save is checked")
    print("  [n] no - fill and save without checking")
    if not _prompt_yes_no("Check the screen after each Save", default_yes=True):
        return "none"
    print()
    print("Check every Save with AI? Gemini reads the screen after each Save.")
    print("  [y] AI reads each screen")
    print("  [n] check the new record locally (no AI, no API cost)")
    return "text" if _prompt_yes_no("Use AI for every record", default_yes=False) else "row"


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
            "model": "gemini-3.6-flash",
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

    print("=" * 72)
    print("  FACT Automation - Workflow Wizard")
    print("=" * 72)

    # 1. Target window
    app_title, process_name = _select_window()

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
    live = _inspect_live(app_title)
    if live is None:
        print("Open the app again and re-run the wizard if the window went away.")
        return 1
    window, inspection = live
    print(f"[ OK ] Detected {len(inspection.candidates)} control(s).")
    if len(inspection.candidates) < 3:
        print(
            "[WARN] This window exposes almost no controls. Keep it restored "
            "(not minimized) and in front while you add a step; a browser "
            "only exposes its page controls while the page is drawn."
        )

    session = _step_session_dir()
    steps: list[dict] = []
    labels: dict[str, str] = {}
    types: dict[str, str] = {name: "text" for name in field_names}
    live_values: dict[str, str] = {}
    filled: dict[str, ResolvedTarget] = {}
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
        choice = _prompt("Choose 1 or 2", default="1").strip().casefold()

        if choice.startswith("2") or choice.startswith("s"):
            if not steps:
                print("No steps yet, so there is nothing to run.")
                continue
            if not any(step["action"] == "fill" for step in steps):
                print(
                    "No step fills anything yet - add the step that types "
                    "the data."
                )
                continue
            break
        if choice not in ("", "1", "a", "add"):
            print("Enter 1 to add a step, or 2 to start automation.")
            continue

        index = len(steps) + 1
        instruction = _prompt(
            f"Step {index} - what should the agent do here, in your words"
        )
        if not instruction:
            continue

        try:
            capture = capture_window(window)
            inspection = inspect_window(window)
        except CaptureError as exc:
            print(f"[WARN] {exc}")
            continue
        except UIAError as exc:
            print(f"[WARN] Could not read the window's controls: {exc}")
            continue
        window = inspection.window
        shot = _save_shot(capture, session, f"step-{index:02d}-before.png")
        print(f"  Screen saved: {shot}")
        # A click can open a dialog, so the control list has to come from the
        # same moment as the screenshot - not from before the earlier steps.
        print(f"  {len(inspection.candidates)} control(s) on this screen right now.")

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
            try:
                capture = capture_window(window)
                inspection = inspect_window(window)
            except (CaptureError, UIAError) as exc:
                print(f"[WARN] {exc}")
                break
            window = inspection.window
            shot = _save_shot(capture, session, f"step-{index:02d}-before.png")
            print(f"  Screen saved: {shot}")
            print(
                f"  {len(inspection.candidates)} control(s) on this screen "
                "right now."
            )
            plan = None

        if plan is None:
            continue

        try:
            inspection = inspect_window(window)
        except UIAError as exc:
            print(f"[WARN] Could not read the window: {exc}")
            continue

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
        )
        for note in result.warnings:
            print(f"  note: {note}")
        if result.problems:
            print(f"  The screen did not take that: {'; '.join(result.problems)}")
            print(
                "  The form is left as it is; fix it by hand and add the "
                "step again."
            )
            continue

        filled.update(result.fields)
        live_values.update(step_values)
        if plan.kind == "click":
            steps.append({"action": "click", "button": plan.label})
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
        print(f"[ OK ] Step {index} done on the live screen.")

    print()
    save_button = _ask_save_button(window, inspection)
    confirmation = _ask_confirmation()
    field_configs = [
        {
            "name": name,
            "field": labels.get(name, name),
            "type": types.get(name, "text"),
        }
        for name in field_names
        if name in labels
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
        confirmation=confirmation,
        path=output_path,
    )

    print()
    print("=" * 72)
    print("  WORKFLOW READY")
    print("=" * 72)
    for index, step in enumerate(steps, start=1):
        print(f"  {index}. {_describe_step(step)}")
    print(f"  Save button : {save_button}")
    print(f"  Config      : {config_file}")
    print("  Safety      : Alt+T (Stop) | Alt+P (Pause) | Alt+R (Resume)")
    print("=" * 72)

    try:
        config = load_config(str(config_file))
    except ConfigError as exc:
        print(f"[WARN] {exc}")
        return 1

    print()
    print("Saving the record that is already filled...")
    try:
        save = save_and_verify(
            config=config,
            window=window,
            inspection=inspection,
            fields=filled,
            values=live_values,
            record_label="record",
            screenshot_dir=session,
        )
    except Exception as exc:  # a save failure must not lose the workflow
        print(f"[WARN] The first save could not be completed: {exc}")
        return 1

    if not save:
        print(
            "The first record was not saved ("
            + (save.outcome.reason or "no confirmation")
            + "), so the workflow was written but not proven."
        )
        return 1

    print(f"  save confirmation: {save.outcome.outcome} ({save.outcome.reason})")

    verifier = _ask_save_check()
    if verifier == "none":
        print("  Save check: none - every Save counts as done")
        footprint: list[str] = []
    else:
        footprint = decide_footprint(
            save.before,
            save.after,
            live_values,
            list(live_values),
        )
        if footprint:
            print(
                "  Save evidence: "
                + ", ".join(
                    f"{live_values.get(key, '')!r} for {key!r}"
                    for key in footprint
                )
                + " is now on the screen."
            )
        else:
            footprint = list(live_values)[:1]
            print(
                "  No new value was visible after Save, so later saves are "
                "checked with the first filled value."
            )
        if verifier == "text" and not config.confirmation.success_pattern:
            print(
                "  No success text was given earlier, so AI has nothing to match; "
                "checking the screen locally instead."
            )
            verifier = "row"
        print(
            "  Save check: "
            + ("AI reads each screen" if verifier == "text" else "local window text")
        )

    data = json.loads(config_file.read_text(encoding="utf-8"))
    data.setdefault("confirmation", {})["verifier"] = verifier
    data["confirmation"]["footprint"] = list(footprint)
    config_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"  Workflow saved: {config_file}")
    return 0


__all__ = [
    "WizardResult",
    "run_wizard",
]
