"""Command-line entry points for the FACT desktop automation foundation.

All commands operate on one application window and one workflow config. There
is no workbook, queue, or ERP transaction here yet:

* ``check``    - environment and configuration report
* ``inspect``  - capture the target window and list its UIA controls
* ``ground``   - AI field/button detection on the live window
* ``safety``   - exercise the stop/pause/resume hotkeys
* ``form run`` - execute a saved workflow once with a record from JSON
* ``automation`` - run a saved automation, resuming where it stopped
* ``wizard``   - build and prove a workflow interactively
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import platform
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.config import (
    ConfigError,
    load_config,
    load_env,
    project_root,
    resolve_path,
)
from core.logging.setup import mask_secret, setup_logging
from core.state.run_lock import RunLock, RunLockError

__version__ = "2.0.0"

logger = logging.getLogger(__name__)

_REQUIRED_MODULES = (
    ("pydantic", "configuration models"),
    ("dotenv", "environment loading"),
    ("PIL", "screenshot handling"),
    ("google.genai", "Gemini client"),
    ("win32gui", "window capture"),
    ("comtypes", "UI Automation"),
    ("pywinauto", "UIA helper"),
    ("pyperclip", "clipboard paste"),
)


@dataclass
class Check:
    """One line in the environment report."""

    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str
    critical: bool = True


def _import_check(module_name: str) -> tuple[bool, str]:
    try:
        with warnings.catch_warnings():
            # Third-party packages (notably pywinauto) warn on import; the
            # diagnostic report should stay clean.
            warnings.simplefilter("ignore")
            module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - report any import problem
        return False, f"{type(exc).__name__}: {exc}"
    version = getattr(module, "__version__", "")
    return True, str(version) if version else "imported"


def _writable_check(directory: Path) -> tuple[bool, str]:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return False, str(exc)
    return True, str(directory)


def _collect_checks(config_path: str, env_path: str | None) -> list[Check]:
    """Build the environment report without touching the target app."""
    checks: list[Check] = []
    system = platform.system()
    checks.append(
        Check(
            "Platform",
            "ok" if system == "Windows" else "fail",
            f"{system} {platform.release()} ({platform.machine()})",
        )
    )
    version = sys.version_info
    checks.append(
        Check(
            "Python",
            "ok" if version >= (3, 11) else "fail",
            f"{version.major}.{version.minor}.{version.micro} (requires >= 3.11)",
        )
    )

    for module_name, purpose in _REQUIRED_MODULES:
        ok, detail = _import_check(module_name)
        checks.append(
            Check(
                f"dependency: {module_name}",
                "ok" if ok else "fail",
                f"{purpose} - {detail}",
            )
        )

    env_file = load_env(env_path)
    if env_file.is_file():
        checks.append(Check(".env file", "ok", str(env_file)))
    else:
        checks.append(
            Check(
                ".env file",
                "warn",
                f"not found at {env_file}; set GEMINI_API_KEY in the environment",
                critical=False,
            )
        )

    key = os.getenv("GEMINI_API_KEY", "").strip()
    checks.append(
        Check(
            "GEMINI_API_KEY",
            "ok" if key else "warn",
            f"loaded ({mask_secret(key, 2)})" if key else "not set; AI planning is unavailable",
            critical=False,
        )
    )

    try:
        config = load_config(config_path)
        detail = f"{config_path} - {len(config.fields)} field(s)"
        if config.workflow is not None:
            detail += f", {len(config.workflow.steps)} step(s)"
        checks.append(Check("config", "ok", detail))
    except ConfigError as exc:
        checks.append(Check("config", "fail", str(exc)))

    for relative in ("data/logs", "data/screenshots", "data/state", "data/ai_logs"):
        ok, detail = _writable_check(project_root() / relative)
        checks.append(
            Check(
                f"{relative} writable",
                "ok" if ok else "fail",
                detail,
            )
        )

    checks.append(
        Check(
            "git repository",
            "ok" if (project_root() / ".git").exists() else "warn",
            "initialised" if (project_root() / ".git").exists() else "not initialised",
            critical=False,
        )
    )
    return checks


def _print_checks(checks: list[Check]) -> int:
    label = {"ok": " OK ", "warn": "WARN", "fail": "FAIL"}
    failed = False
    print(f"FACT Automation v{__version__} - environment check")
    print("-" * 72)
    for check in checks:
        if check.status == "fail" and check.critical:
            failed = True
        print(f"[{label[check.status]}] {check.name}: {check.detail}")
    print("-" * 72)
    if failed:
        print("One or more critical checks failed.")
        return 1
    print("Environment looks usable.")
    return 0


def run_check(config_path: str, env_path: str | None = None) -> int:
    """Print the environment report."""
    return _print_checks(_collect_checks(config_path, env_path))


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def run_inspect(config_path: str, annotate: bool = True, max_elements: int = 300) -> int:
    """Capture the target window and list its UIA controls."""
    from desktop.screenshots.capture import CaptureError, capture_window
    from desktop.screenshots.images import annotate_candidates
    from desktop.uia.uia import UIAError, inspect_window
    from desktop.windows.window import WindowError, attach_window

    try:
        config = load_config(config_path)
        window = attach_window(config.app)
        capture = capture_window(window)
    except (ConfigError, WindowError, CaptureError) as exc:
        print(f"ERROR: {exc}")
        return 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot_dir = project_root() / "data" / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    raw_path = screenshot_dir / f"inspect_{stamp}.png"
    capture.image.save(raw_path)

    print(f"Window: 0x{window.handle:X} {window.title!r}")
    print(f"Process: {window.process_name or 'unknown'} | class: {window.class_name}")
    print(
        f"Window rect: {window.rect.bbox} "
        f"({window.rect.width}x{window.rect.height})"
    )
    print(
        f"Client rect: {window.client_rect.bbox} "
        f"({window.client_rect.width}x{window.client_rect.height})"
    )
    print(f"DPI: {window.dpi} ({window.dpi_scale:.2f}x)")
    print(f"Screenshot: {raw_path} ({capture.image.width}x{capture.image.height})")

    try:
        inspection = inspect_window(window, max_elements=max_elements)
    except UIAError as exc:
        print(f"ERROR: {exc}")
        print("Screenshot saved for diagnosis.")
        return 1

    print(f"UIA candidates: {len(inspection.candidates)}")
    print(
        f"{'ID':<5} {'Type':<12} {'Name':<30} {'Automation ID':<22} "
        f"{'Rect':<26} Value"
    )
    print("-" * 120)
    for candidate in inspection.candidates:
        print(
            f"{candidate.candidate_id:<5} {candidate.control_type:<12} "
            f"{_truncate(candidate.name, 30):<30} "
            f"{_truncate(candidate.automation_id, 22):<22} "
            f"{str(candidate.rect.bbox):<26} "
            f"{_truncate(candidate.value, 30)}"
        )
    print("-" * 120)

    if annotate:
        annotated_path = screenshot_dir / f"inspect_{stamp}_annotated.png"
        annotate_candidates(
            capture.image,
            inspection.candidates,
            capture.origin,
            annotated_path,
        )
        print(f"Annotated: {annotated_path}")

    if not inspection.candidates:
        print("Warning: no UIA candidates found; the app may be custom-drawn.")
    return 0


def run_ground(config_path: str, fields: str, annotate: bool = True) -> int:
    """Run AI grounding on the live window for the declared data fields."""
    from ai.planners.grounding import GroundingError, ground_window
    from ai.providers.gemini import GeminiClient, GeminiError
    from desktop.screenshots.capture import CaptureError, capture_window
    from desktop.screenshots.images import annotate_boxes
    from desktop.uia.uia import UIAError, inspect_window
    from desktop.windows.window import WindowError, attach_window

    field_names = [item.strip() for item in fields.split(",") if item.strip()]
    if not field_names:
        print("ERROR: --fields must name at least one data field")
        return 1
    try:
        config = load_config(config_path)
        client = GeminiClient(config.gemini)
        window = attach_window(config.app)
        capture = capture_window(window)
        inspection = inspect_window(window)
    except (ConfigError, GeminiError, WindowError, CaptureError, UIAError) as exc:
        print(f"ERROR: {exc}")
        return 1

    try:
        result = ground_window(
            client, capture, inspection.candidates, field_names, config.gemini
        )
    except (GroundingError, GeminiError) as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"Form detected: {result.response.form_detected}")
    print(f"Layout confidence: {result.layout_confidence:.2f}")
    for field_id, element in result.fields.items():
        print(
            f"  field {field_id}: {element.label!r} "
            f"{element.rect.bbox} type={element.type}"
        )
    for button_id, element in result.buttons.items():
        print(
            f"  button {button_id}: {element.label!r} "
            f"{element.rect.bbox} role={element.type}"
        )
    for mapping in result.field_mapping:
        print(f"  map {mapping.key!r} -> {mapping.field_id}")
    for warning in result.warnings:
        print(f"  note: {warning}")
    if result.unmapped_keys:
        print("  unmapped data fields: " + ", ".join(repr(k) for k in result.unmapped_keys))

    if annotate:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = project_root() / "data" / "grounding" / f"grounding_{stamp}_annotated.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        boxes = [
            (element.rect, element.element_id)
            for element in list(result.fields.values()) + list(result.buttons.values())
        ]
        annotate_boxes(capture.image, boxes, capture.origin, out)
        print(f"Annotated: {out}")
    return 0


def run_safety_test(
    config_path: str,
    seconds: float = 10.0,
    overlay: bool = True,
) -> int:
    """Block input for a fixed time so the safety shortcuts can be tested."""
    from core.safety.controller import SafetyController
    from core.safety.hotkeys import HotkeyError, hotkeys_from_config
    from core.safety.overlay import NullOverlay, StatusOverlay

    try:
        config = load_config(config_path)
        hotkeys = hotkeys_from_config(config.safety)
    except (ConfigError, HotkeyError) as exc:
        print(f"ERROR: {exc}")
        return 1

    labels = {hotkey.action: hotkey.label for hotkey in hotkeys}
    controller = SafetyController(
        config.safety,
        overlay=StatusOverlay(labels) if overlay else NullOverlay(),
    )

    print("Safety test: external mouse and keyboard input will be blocked.")
    print(
        f"  {labels['stop']} stop | {labels['pause']} pause | "
        f"{labels['resume']} resume"
    )
    print("  Ctrl+Alt+Del is handled by Windows and is never blocked.")
    print(f"  Locking for up to {seconds:.0f}s; press {labels['stop']} to release.")
    print()

    try:
        controller.start()
    except Exception as exc:
        controller.close()
        print(f"ERROR: {exc}")
        return 1

    print("Input locked.", flush=True)

    try:
        controller.wait(seconds)
    except KeyboardInterrupt:
        controller.stop(reason="interrupted")

    reason = controller.reason or "timeout"
    controller.close()
    print(f"Input released ({reason}).")
    return 0


def _state_printer(labels: dict[str, str]):
    """Print what the safety hotkeys did, so the console shows the state."""

    def report(state: str) -> None:
        if state == "paused":
            print(f"  paused - press {labels['resume']} to continue")
        elif state == "running":
            print("  running")
        elif state == "stopped":
            print("  stopped")

    return report


def run_form(
    config_path: str,
    values_path: str,
    *,
    confirm_each: bool = False,
    overlay: bool = True,
    env_path: str | None = None,
) -> int:
    """Execute a saved workflow once with one record loaded from JSON."""
    from ai.providers.gemini import GeminiClient, GeminiError
    from core.safety.controller import SafetyController
    from core.safety.hotkeys import HotkeyError, hotkeys_from_config
    from core.safety.overlay import NullOverlay, StatusOverlay
    from desktop.screenshots.capture import CaptureError
    from desktop.uia.uia import UIAError
    from desktop.windows.window import WindowError, attach_window
    from execution.verification.confirmation import GeminiTextReader, TextConfirmation
    from execution.workflows.runner import SAVED, load_values, run_workflow

    load_env(env_path)

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"ERROR: {exc}")
        return 1

    if config.workflow is None or not config.workflow.steps:
        print("ERROR: this config has no workflow.steps to run.")
        return 1

    try:
        values = load_values(values_path)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    missing = [item.name for item in config.fields if item.name not in values]
    if missing:
        print(
            "ERROR: the values file is missing field(s): "
            + ", ".join(repr(name) for name in missing)
        )
        return 1

    lock = RunLock()
    try:
        lock.acquire(note=str(config_path))
    except RunLockError as exc:
        print(f"ERROR: {exc}")
        return 1

    controller = None
    try:
        verifier = config.confirmation.verifier
        confirmation = None
        if verifier == "text":
            client = GeminiClient(config.gemini)
            confirmation = TextConfirmation(
                config.confirmation, GeminiTextReader(client)
            )
        hotkeys = hotkeys_from_config(config.safety)
        labels = {hotkey.action: hotkey.label for hotkey in hotkeys}
        controller = SafetyController(
            config.safety,
            overlay=StatusOverlay(labels) if overlay else NullOverlay(),
            on_state=_state_printer(labels),
        )

        window = attach_window(config.app)
        print(f"Window: 0x{window.handle:X} {window.title!r}")
        print(
            f"Workflow: {len(config.workflow.steps)} step(s), "
            f"{len(config.fields)} declared field(s)"
        )
        print(f"Values: {values_path}")
        if verifier == "none":
            print("Save check: none - the screen is not read after Save")
        elif verifier == "row":
            print("Save check: local window text (no AI calls)")
        else:
            print(f"Gemini: {config.gemini.model} (reads the screen after Save)")

        controller.start()
        outcome = run_workflow(
            values,
            config=config,
            confirmation=confirmation,
            controller=controller,
            confirm_each=confirm_each,
        )
        print()
        print(f"Outcome: {outcome.status} ({outcome.record_label})")
        if outcome.error:
            print(f"  {outcome.error}")
        for shot in outcome.screenshots:
            print(f"  evidence: {shot}")
        return 0 if outcome.status == SAVED else 1
    except (
        WindowError,
        CaptureError,
        UIAError,
        GeminiError,
        HotkeyError,
    ) as exc:
        print(f"ERROR: {exc}")
        return 1
    finally:
        if controller is not None:
            controller.close()
        lock.release()


def run_automation_command(
    name: str,
    values_path: str,
    *,
    saved_already: bool = False,
    not_saved: bool = False,
    restart: bool = False,
    overlay: bool = True,
    env_path: str | None = None,
) -> int:
    """Run one saved automation, starting from the screen FACT is showing."""
    from ai.providers.gemini import GeminiClient, GeminiError
    from core.safety.controller import SafetyController
    from core.safety.hotkeys import HotkeyError, hotkeys_from_config
    from core.safety.overlay import NullOverlay, StatusOverlay
    from desktop.screenshots.capture import CaptureError
    from desktop.uia.uia import UIAError
    from desktop.windows.window import WindowError
    from execution.verification.confirmation import GeminiTextReader, TextConfirmation
    from execution.workflows.automation_runner import run_automation
    from execution.workflows.runner import load_values

    from core.automation.model import AutomationError
    from core.automation.registry import load as load_automation

    load_env(env_path)

    # The name is checked first: a typo there is the likelier mistake, and the
    # message names the automations that do exist.
    try:
        plan = load_automation(name)
        config = plan.to_config()
    except AutomationError as exc:
        print(f"ERROR: {exc}")
        return 2

    try:
        values = load_values(values_path)
    except Exception as exc:  # noqa: BLE001 - any read problem is the same answer
        print(f"ERROR: {exc}")
        return 2

    lock = RunLock()
    try:
        lock.acquire(note=f"automation:{name}")
    except RunLockError as exc:
        print(f"ERROR: {exc}")
        return 2

    controller = None
    try:
        confirmation = None
        # The plan decides its own save check; the reader is only built when the
        # automation asks for the text verifier.
        if plan.save.verifier == "text":
            client = GeminiClient(config.gemini)
            confirmation = TextConfirmation(config.confirmation, GeminiTextReader(client))

        hotkeys = hotkeys_from_config(config.safety)
        labels = {hotkey.action: hotkey.label for hotkey in hotkeys}
        controller = SafetyController(
            config.safety,
            overlay=StatusOverlay(labels) if overlay else NullOverlay(),
            on_state=_state_printer(labels),
        )
        controller.start()

        run = run_automation(
            name,
            values,
            saved_already=saved_already,
            not_saved=not_saved,
            restart=restart,
            confirmation=confirmation,
            controller=controller,
        )
        print()
        print(f"Outcome: {run.status} ({name})")
        if run.message:
            print(f"  {run.message}")
        for shot in run.screenshots:
            print(f"  evidence: {shot}")
        return run.exit_code
    except (AutomationError, WindowError, CaptureError, UIAError, GeminiError, HotkeyError) as exc:
        print(f"ERROR: {exc}")
        return 1
    finally:
        if controller is not None:
            controller.close()
        lock.release()


def run_state(env_path: str | None = None) -> int:
    """Print which screen FACT is showing, and the evidence for it."""
    from desktop.state.recognizer import detect
    from desktop.state.states import StateTableError, load_state_table

    load_env(env_path)
    try:
        table = load_state_table()
    except StateTableError as exc:
        print(f"ERROR: {exc}")
        return 2
    match = detect(table)
    print(match.message())
    if match.stack:
        print("  behind: " + " < ".join(match.stack))
    for item in table.states:
        print(f"  {'*' if item.id == match.state_id else ' '} {item.id}: {item.label}")
    return 0 if match.known else 2


def run_list_automations() -> int:
    """List saved automations with where their last run stopped."""
    from core.automation.progress import ProgressError, load_progress
    from core.automation.registry import list_automations

    names = list_automations()
    if not names:
        print("No automations yet: config/automations/ is empty.")
        return 2
    for name in names:
        try:
            item = load_progress(name)
        except ProgressError as exc:
            print(f"  {name}: progress unreadable ({exc})")
            continue
        if item is None:
            print(f"  {name}: never run")
            continue
        where = f"step {item.cursor + 1}" if item.completed else "not started"
        state = "save unresolved" if item.save_unresolved else (item.outcome or "in progress")
        print(f"  {name}: {state}, {where}")
    return 0


def run_agent(config_path: str | None, env_path: str | None, values_path: str | None) -> int:
    """Launch the interactive workflow wizard."""
    from wizard.wizard import run_wizard

    return run_wizard(
        config_path=config_path, env_path=env_path, values_path=values_path
    )


def run_agent_ui(config_path: str | None, env_path: str | None) -> int:
    """Launch the wizard inside the floating chat window."""
    from wizard.chat_ui import run_agent_chat

    return run_agent_chat(config_path=config_path, env_path=env_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fact-automation",
        description="Windows desktop automation foundation with AI GUI planning.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="show program's version number and exit",
    )
    parser.add_argument(
        "--config",
        default="config/examples/workflow.example.json",
        help="path to the workflow config JSON",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="path to the .env file (defaults to <project>/.env)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="print the environment report and exit",
    )
    parser.add_argument(
        "--agent",
        action="store_true",
        help="start the interactive workflow wizard",
    )
    parser.add_argument(
        "--agent-ui",
        action="store_true",
        help="start the wizard in the floating chat window",
    )
    parser.add_argument(
        "--values",
        default=None,
        help="path to a JSON object of field -> sample value",
    )

    subparsers = parser.add_subparsers(dest="command")

    inspect_parser = subparsers.add_parser(
        "inspect", help="capture the target window and list UIA controls"
    )
    inspect_parser.add_argument("--config", dest="sub_config", default=None)
    inspect_parser.add_argument("--env-file", dest="sub_env", default=None)
    inspect_parser.add_argument("--no-annotate", action="store_true")
    inspect_parser.add_argument("--max-elements", type=int, default=300)

    ground_parser = subparsers.add_parser(
        "ground", help="AI field/button detection on the live window"
    )
    ground_parser.add_argument("--config", dest="sub_config", default=None)
    ground_parser.add_argument("--env-file", dest="sub_env", default=None)
    ground_parser.add_argument(
        "--fields", required=True, help="comma-separated data field names"
    )
    ground_parser.add_argument("--no-annotate", action="store_true")

    safety_parser = subparsers.add_parser(
        "safety", help="exercise the stop/pause/resume hotkeys"
    )
    safety_parser.add_argument("--config", dest="sub_config", default=None)
    safety_parser.add_argument("--env-file", dest="sub_env", default=None)
    safety_parser.add_argument("--seconds", type=float, default=10.0)
    safety_parser.add_argument("--no-overlay", action="store_true")

    form_parser = subparsers.add_parser("form", help="run a saved workflow")
    form_sub = form_parser.add_subparsers(dest="form_command")
    form_run = form_sub.add_parser("run", help="execute one record from JSON")
    form_run.add_argument("--config", dest="sub_config", default=None)
    form_run.add_argument("--env-file", dest="sub_env", default=None)
    form_run.add_argument("--values", dest="sub_values", required=True)
    form_run.add_argument("--confirm-each", action="store_true")
    form_run.add_argument("--no-overlay", action="store_true")

    automation_parser = subparsers.add_parser(
        "automation", help="run a saved automation, or inspect one"
    )
    automation_sub = automation_parser.add_subparsers(dest="automation_command")
    automation_run = automation_sub.add_parser(
        "run", help="run an automation, resuming from the screen FACT shows"
    )
    automation_run.add_argument("name", help="the automation's registered name")
    automation_run.add_argument("--values", dest="sub_values", required=True)
    automation_run.add_argument("--env-file", dest="sub_env", default=None)
    automation_run.add_argument(
        "--saved-already",
        action="store_true",
        help="the unfinished Save did go through; record it and stop",
    )
    automation_run.add_argument(
        "--not-saved",
        action="store_true",
        help="the unfinished Save did not go through; continue the run",
    )
    automation_run.add_argument(
        "--restart",
        action="store_true",
        help="forget the last run and enter the record from the beginning",
    )
    automation_run.add_argument("--no-overlay", action="store_true")

    automation_state = automation_sub.add_parser(
        "state", help="show which screen FACT is showing"
    )
    automation_state.add_argument("--env-file", dest="sub_env", default=None)
    automation_sub.add_parser("list", help="list saved automations and their last run")

    subparsers.add_parser("wizard", help="build and prove a workflow interactively")
    subparsers.add_parser("check", help="print the environment report")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch one command."""
    parser = build_parser()
    args = parser.parse_args(argv)

    env_path = getattr(args, "sub_env", None) or args.env_file
    config_path = getattr(args, "sub_config", None) or args.config

    if args.version:
        print(__version__)
        return 0
    if args.check or args.command == "check":
        return run_check(config_path, env_path)
    if args.agent:
        return run_agent(config_path, env_path, args.values)
    if args.agent_ui:
        return run_agent_ui(config_path, env_path)

    if args.command == "wizard":
        return run_agent(config_path, env_path, args.values)
    if args.command == "inspect":
        return run_inspect(
            config_path,
            annotate=not args.no_annotate,
            max_elements=args.max_elements,
        )
    if args.command == "ground":
        return run_ground(
            config_path, args.fields, annotate=not args.no_annotate
        )
    if args.command == "safety":
        return run_safety_test(
            config_path, seconds=args.seconds, overlay=not args.no_overlay
        )
    if args.command == "form":
        if getattr(args, "form_command", None) != "run":
            parser.parse_args(["form", "--help"])
            return 2
        return run_form(
            config_path,
            args.sub_values,
            confirm_each=args.confirm_each,
            overlay=not args.no_overlay,
            env_path=env_path,
        )
    if args.command == "automation":
        sub = getattr(args, "automation_command", None)
        if sub == "run":
            return run_automation_command(
                args.name,
                args.sub_values,
                saved_already=args.saved_already,
                not_saved=args.not_saved,
                restart=args.restart,
                overlay=not args.no_overlay,
                env_path=env_path,
            )
        if sub == "state":
            return run_state(env_path)
        if sub == "list":
            return run_list_automations()
        parser.parse_args(["automation", "--help"])
        return 2

    parser.print_help()
    return 2


if __name__ == "__main__":
    setup_logging(
        level=os.getenv("LOG_LEVEL", "INFO"),
        console=os.getenv("LOG_CONSOLE", "0") == "1",
    )
    raise SystemExit(main())
