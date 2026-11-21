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
import logging
import os
import platform
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from core.config import (
    ConfigError,
    load_config,
    load_env,
    project_root,
)
from core.logging.setup import mask_secret, setup_logging
from core.safety.state_file import write_state
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
    from desktop.state.fact import FactError, attach_target
    from desktop.uia.uia import UIAError, inspect_window
    from desktop.windows.window import WindowError

    try:
        config = load_config(config_path)
        window = attach_target(config.app)
        capture = capture_window(window)
    except (ConfigError, FactError, WindowError, CaptureError) as exc:
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
    from desktop.state.fact import FactError, attach_target
    from desktop.uia.uia import UIAError, inspect_window
    from desktop.windows.window import WindowError

    field_names = [item.strip() for item in fields.split(",") if item.strip()]
    if not field_names:
        print("ERROR: --fields must name at least one data field")
        return 1
    try:
        config = load_config(config_path)
        client = GeminiClient(config.gemini)
        window = attach_target(config.app)
        capture = capture_window(window)
        inspection = inspect_window(window)
    except (ConfigError, FactError, GeminiError, WindowError, CaptureError, UIAError) as exc:
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


class _Transcript:
    """Console output for one run, copied to data/logs/automation.log.

    The launcher window closes when the run ends, so the file is what is left to
    read afterwards.  A file that cannot be opened is simply not written.
    """

    def __init__(self, name: str) -> None:
        self.path = project_root() / "data" / "logs" / "automation.log"
        self._file = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.path.open("a", encoding="utf-8")
            self._write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S}  {name} ===")
        except OSError:
            self._file = None

    def _write(self, line: str) -> None:
        if self._file is None:
            return
        try:
            self._file.write(line + "\n")
            self._file.flush()
        except OSError:
            self._file = None

    def say(self, text: str = "") -> None:
        print(text, flush=True)
        self._write(text)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def _state_printer(labels: dict[str, str]):
    """Print what the safety hotkeys did, so the console shows the state."""

    def report(state: str, reason: str = "") -> None:
        # A ``run`` step is a separate process: it reads this file to know that
        # Alt+P paused the run and Alt+T stopped it.
        write_state(state, reason)
        if str(getattr(state, "value", state)) == "paused":
            print(f"  paused - press {labels['resume']} to continue")
        elif str(getattr(state, "value", state)) == "running":
            print("  running")
        elif str(getattr(state, "value", state)) == "stopped":
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
    from desktop.state.fact import FactError, attach_target
    from desktop.uia.uia import UIAError
    from desktop.windows.window import WindowError
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

        window = attach_target(config.app)
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
        FactError,
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
    from_screen: bool = False,
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
    from desktop.windows.window import WindowError, release_pinned_windows
    from execution.verification.confirmation import GeminiTextReader, TextConfirmation
    from desktop.app.launch import LaunchError, ensure_running
    from desktop.state.fact import is_fact
    from execution.workflows.automation_runner import run_automation
    from execution.workflows.runner import load_values_with_rows

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

    transcript = _Transcript(name)
    say = transcript.say

    started = False
    try:
        if is_fact(config.app):
            started = ensure_running(output=print)
    except LaunchError as exc:
        say(f"ERROR: {exc}")
        transcript.close()
        return 2
    if started:
        say("  FACT was started for this run")

    try:
        values, rows = load_values_with_rows(values_path)
    except Exception as exc:  # noqa: BLE001 - any read problem is the same answer
        say(f"ERROR: {exc}")
        transcript.close()
        return 2
    if rows:
        say(
            f"  one document: {len(rows)} line row(s), saved once at the end"
        )

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
            rows=rows,
            values_file=values_path,
            saved_already=saved_already,
            not_saved=not_saved,
            restart=restart,
            from_screen=from_screen,
            confirmation=confirmation,
            controller=controller,
            output=say,
        )
        say()
        say(f"Outcome: {run.status} ({name})")
        if run.message:
            say(f"  {run.message}")
        for shot in run.screenshots:
            say(f"  evidence: {shot}")
        say(f"  log: {transcript.path}")
        return run.exit_code
    except (AutomationError, WindowError, CaptureError, UIAError, GeminiError, HotkeyError) as exc:
        say(f"ERROR: {exc}")
        return 1
    finally:
        try:
            if controller is not None:
                controller.close()
        finally:
            try:
                if config.app.pin_window:
                    try:
                        release_pinned_windows(config.app.process_name)
                    except Exception as exc:  # cleanup must not strand run state or lock
                        say(f"warning: FACT windows could not be unpinned: {exc}")
            finally:
                write_state("idle")          # a finished run must not leave a filler waiting
                lock.release()
                transcript.close()


def run_register(
    name: str,
    config_path: str,
    values_path: str | None = None,
    *,
    dry_run: bool = False,
    check_save: bool = True,
    overlay: bool = True,
    env_path: str | None = None,
) -> int:
    """Prove a workflow config on the live screen and register it as an automation."""
    from core.automation.model import AutomationError
    from core.safety.controller import SafetyController
    from core.safety.hotkeys import HotkeyError, hotkeys_from_config
    from core.safety.overlay import NullOverlay, StatusOverlay
    from desktop.screenshots.capture import CaptureError
    from desktop.uia.uia import UIAError
    from desktop.windows.window import WindowError
    from execution.workflows.runner import load_values
    from wizard.register import REFUSED, describe_checkpoints, register_automation

    load_env(env_path)

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"ERROR: {exc}")
        return 2

    values: dict[str, str] = {}
    if values_path:
        try:
            values = load_values(values_path)
        except Exception as exc:  # noqa: BLE001 - any read problem is the same answer
            print(f"ERROR: {exc}")
            return 2

    lock = RunLock()
    try:
        lock.acquire(note=f"register:{name}")
    except RunLockError as exc:
        print(f"ERROR: {exc}")
        return 2

    controller = None
    try:
        hotkeys = hotkeys_from_config(config.safety)
        labels = {hotkey.action: hotkey.label for hotkey in hotkeys}
        controller = SafetyController(
            config.safety,
            overlay=StatusOverlay(labels) if overlay else NullOverlay(),
            on_state=_state_printer(labels),
        )
        print(f"Registering {name!r} from {config_path}")
        print("  the proof pass runs the steps once and clicks Save once")
        controller.start()

        result = register_automation(
            name=name,
            config_path=config_path,
            values=values,
            # The proof runs a ``run`` step for real, so it gets the same file
            # the automation will be given when it is run later.
            values_file=values_path,
            check_save=check_save,
            write=not dry_run,
        )
    except (
        AutomationError,
        WindowError,
        CaptureError,
        UIAError,
        HotkeyError,
    ) as exc:
        print(f"ERROR: {exc}")
        return 1
    finally:
        if controller is not None:
            controller.close()
        lock.release()

    print()
    if result.automation is not None:
        print("Checkpoints proved by the run:")
        for line in describe_checkpoints(result.automation):
            print(f"  {line}")
        check = (
            f"{result.automation.save.verifier} check"
            if result.automation.save.check
            else "not checked"
        )
        print(f"  save: {result.automation.save.button!r} ({check})")
    if result.path is not None:
        print(f"  automation: {result.path}")
    if result.launcher is not None:
        print(f"  launcher:   {result.launcher}")
    print(f"Outcome: {result.status} ({name})")
    if result.message:
        print(f"  {result.message}")
    if result.ok and not dry_run:
        print()
        print(
            f"Run it with: python cli.py automation run {name} --values <values.json>"
        )
        if result.launcher is not None:
            print(f"or double-click {result.launcher.name} (a values.json beside it)")
    return 0 if result.ok else (2 if result.status == REFUSED else 1)


def run_write_launcher(name: str) -> int:
    """(Re)write the double-clickable launcher for a registered automation."""
    from core.automation.launcher import write_launcher
    from core.automation.registry import AutomationError, load

    try:
        automation = load(name)
    except AutomationError as exc:
        print(f"ERROR: {exc}")
        return 2

    try:
        path = write_launcher(name)
    except OSError as exc:
        print(f"ERROR: the launcher could not be written: {exc}")
        return 1

    print(f"Launcher for {automation.name!r} ({len(automation.steps)} step(s)):")
    print(f"  {path}")
    print(
        "Put a values.json beside it (or drag one onto it), then double-click "
        "to run."
    )
    return 0


def run_list_segments() -> int:
    """List the designed segments that can be appended to an automation."""
    from core.automation.segments import SegmentError, library_summary, load_segments

    try:
        table = load_segments()
    except SegmentError as exc:
        print(f"ERROR: {exc}")
        return 2
    if not table.segments:
        print("No designed segments yet; run python scripts/build_segments.py")
        return 2
    print(f"Designed segments ({len(table.segments)}):")
    for line in library_summary(table):
        print(f"  {line}")
    return 0


def run_append_automation(
    name: str, segment_id: str, *, dry_run: bool = False
) -> int:
    """Append one designed segment to a registered automation."""
    from core.automation.segments import (
        SegmentError,
        append_segment_to,
        load_segments,
    )
    from desktop.state.states import StateTableError, load_state_table

    try:
        known = load_state_table().ids()
    except StateTableError as exc:
        print(f"ERROR: the state table cannot be used: {exc}")
        return 2

    try:
        load_segments()
    except SegmentError as exc:
        print(f"ERROR: {exc}")
        return 2

    result = append_segment_to(
        name, segment_id, known_states=known, write=not dry_run
    )
    print()
    if result.automation is not None:
        print(f"Plan now ({len(result.automation.steps)} step(s)):")
        for position, step in enumerate(result.automation.steps, start=1):
            target = step.button if step.action == "click" else (step.field or step.key)
            print(
                f"  {position}. {step.action} {target!r} "
                f"on {step.checkpoint.state}"
            )
    if result.path is not None:
        print(f"  automation: {result.path}")
    print(f"Outcome: {result.status} ({name} + {segment_id})")
    if result.message:
        print(f"  {result.message}")
    if result.ok and not dry_run:
        print()
        print(
            f"Run it with: python cli.py automation run {name} --values <values.json>"
        )
    return 0 if result.ok else (2 if result.status == "refused" else 1)


def run_state(env_path: str | None = None) -> int:
    """Print which screen FACT is showing, and the evidence for it."""
    from desktop.state.fact import detect_state
    from desktop.state.states import StateTableError, load_state_table

    load_env(env_path)
    try:
        table = load_state_table()
    except StateTableError as exc:
        print(f"ERROR: {exc}")
        return 2
    match = detect_state(table)
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


def run_doctor(env_path: str | None = None) -> int:
    """Check, in one command, whether this machine can run an automation."""
    from core.automation.exceptions import RuleError, load_rules
    from core.automation.progress import ProgressError, load_progress
    from core.automation.registry import AutomationError, list_automations
    from core.automation.registry import load as load_automation
    from desktop.state.states import StateTableError, load_state_table

    load_env(env_path)
    failures = 0
    warnings = 0

    def line(status: str, name: str, detail: str = "") -> None:
        nonlocal failures, warnings
        if status == "FAIL":
            failures += 1
        elif status == "WARN":
            warnings += 1
        print(f"[{status:^4}] {name}" + (f": {detail}" if detail else ""))

    print(f"FACT Automation v{__version__} - doctor")
    print("-" * 72)

    version = sys.version_info
    line(
        "OK" if version >= (3, 11) else "FAIL",
        "Python",
        f"{version.major}.{version.minor}.{version.micro} (needs >= 3.11)",
    )
    for module_name in ("pywinauto", "comtypes", "PIL", "google.genai"):
        ok, detail = _import_check(module_name)
        line("OK" if ok else "FAIL", f"dependency {module_name}", detail)

    # The application: where it is, and whether it is open right now.
    from desktop.app.launch import (
        LaunchError,
        app_config_path,
        is_running,
        load_app_paths,
    )

    try:
        paths = load_app_paths()
    except LaunchError as exc:
        line("FAIL", "config/app.json", str(exc))
        paths = None
    if paths is not None:
        if paths.exe_path and paths.exists():
            line("OK", "FACT executable", paths.exe_path)
        elif paths.exe_path:
            line("FAIL", "FACT executable", f"not found: {paths.exe_path}")
        else:
            line(
                "WARN",
                "FACT executable",
                f"exe_path is empty in {app_config_path()}; runs then need FACT already open",
            )

    try:
        table = load_state_table()
        line("OK", "state table", f"{len(table.states)} state(s): {', '.join(table.ids())}")
        thin = [item.id for item in table.states if getattr(item, "samples", 1) < 2]
        if thin:
            line(
                "WARN",
                "state samples",
                "learned from one snapshot each: " + ", ".join(thin)
                + " (scripts/snapshots.py captures more)",
            )
        # Deliberately a fast window check, not a screen read: doctor must answer
        # in a second, and recognition is a run's job (``automation state``).
        if is_running(paths.process if paths else None):
            line(
                "OK",
                "FACT right now",
                "running; `python cli.py automation state` names the screen",
            )
        else:
            line(
                "WARN",
                "FACT right now",
                "not running; a run will start it if exe_path is set",
            )
    except StateTableError as exc:
        line("FAIL", "state table", str(exc))

    names = list_automations()
    if not names:
        line("WARN", "automations", "none yet; run cli.py wizard and register one")
    for name in names:
        try:
            plan = load_automation(name)
            plan.validate_states(load_state_table().ids())
            rules = load_rules(name)
            progress = load_progress(name)
        except (AutomationError, StateTableError, RuleError, ProgressError) as exc:
            line("FAIL", f"automation {name}", str(exc))
            continue
        count = len(rules.rules) if rules else 0
        where = (
            "never run"
            if progress is None
            else f"last run {progress.outcome or 'in progress'}, step {progress.cursor + 1}"
        )
        line(
            "OK",
            f"automation {name}",
            f"{len(plan.steps)} step(s), {len(plan.state_ids())} screen(s), "
            f"{count} learned rule(s), {where}",
        )

    print("-" * 72)
    if failures:
        print(f"{failures} problem(s) to fix, {warnings} warning(s).")
        return 1
    print(f"Ready. ({warnings} warning(s))")
    return 0


def run_learn_rule(
    name: str,
    *,
    clicks: Sequence[str] = (),
    doubles: Sequence[str] = (),
    keys: str = "",
    expect: str = "",
    description: str = "",
    rule_id: str | None = None,
    env_path: str | None = None,
) -> int:
    """Learn the screen FACT is showing now as a way past it."""
    from core.automation.exceptions import (
        Observation,
        RuleAction,
        RuleError,
        RuleTable,
        load_rules,
        rule_from_screen,
        save_rules,
    )
    from core.automation.registry import AutomationError
    from core.automation.registry import load as load_automation
    from desktop.state.fact import FactError, read_current
    from desktop.state.states import StateTableError, load_state_table

    load_env(env_path)
    try:
        load_automation(name)
    except AutomationError as exc:
        print(f"ERROR: {exc}")
        return 2

    actions = [RuleAction(action="click", button=item) for item in clicks]
    actions += [RuleAction(action="double_click", button=item) for item in doubles]
    if keys.strip():
        actions.append(RuleAction(action="key", keys=keys.strip()))
    if not actions:
        print(
            "ERROR: nothing to learn; pass --click \"OK\", --double-click ... "
            "or --key Enter"
        )
        return 2

    try:
        known = load_state_table().ids()
    except StateTableError as exc:
        print(f"ERROR: the state table cannot be used: {exc}")
        return 2
    if expect and expect not in known:
        print(
            f"ERROR: --expect {expect!r} is not a known screen; known: "
            + ", ".join(known)
        )
        return 2

    try:
        window, inspection, match = read_current(seconds=5.0)
    except FactError as exc:
        print(f"ERROR: {exc}")
        return 1

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

    try:
        table = load_rules(name) or RuleTable(automation=name)
    except RuleError as exc:
        print(f"ERROR: {exc}")
        return 1

    identifier = (rule_id or "").strip() or f"{match.state_id.lower()}-{len(table.rules) + 1}"
    try:
        rule = rule_from_screen(
            identifier,
            seen,
            actions,
            expect=expect,
            description=description,
        )
        table.add(rule)
        path = save_rules(table)
    except (RuleError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"Learned rule {rule.id!r} for {name!r}:")
    print(f"  {rule.describe()}")
    print(f"  trigger: {rule.when.describe()}")
    print(f"  rules:   {path}")
    print(
        "  The next run applies it automatically, with no model call; "
        "edit that file to add a 'not this' condition."
    )
    return 0


def run_rules(name: str) -> int:
    """List the situations an automation has learned to handle."""
    from core.automation.exceptions import RuleError, describe_rules, load_rules

    try:
        table = load_rules(name)
    except RuleError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Learned rules for {name!r}:")
    for rule in describe_rules(table):
        print(f"  {rule}")
    return 0


def run_forget_rule(name: str, rule_id: str) -> int:
    """Remove one learned rule."""
    from core.automation.exceptions import RuleError, load_rules, save_rules

    try:
        table = load_rules(name)
        if table is None:
            print(f"ERROR: {name!r} has learned no rules")
            return 2
        table.remove(rule_id)
        save_rules(table)
    except RuleError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Forgot {rule_id!r}; {name!r} now knows {len(table.rules)} rule(s).")
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
        help="start the FACT ERP.NG workflow wizard",
    )
    parser.add_argument(
        "--agent-ui",
        action="store_true",
        help="start the FACT ERP.NG wizard in the floating chat window",
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
    automation_run.add_argument(
        "--from-screen",
        dest="from_screen",
        action="store_true",
        help=(
            "start at the step whose screen FACT is showing, instead of at the "
            "first step (e.g. the form is already open, so just fill it)"
        ),
    )
    automation_run.add_argument("--no-overlay", action="store_true")

    automation_register = automation_sub.add_parser(
        "register",
        help="prove a workflow config once and save it as an automation",
    )
    automation_register.add_argument("name", help="the name to register it under")
    automation_register.add_argument(
        "--config",
        dest="sub_config",
        required=True,
        help="a workflow config written by the wizard",
    )
    automation_register.add_argument(
        "--values",
        dest="sub_values",
        default=None,
        help="JSON object of field -> value, typed during the proof run",
    )
    automation_register.add_argument("--env-file", dest="sub_env", default=None)
    automation_register.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "prove the steps (one record is still saved) without writing the "
            "automation"
        ),
    )
    automation_register.add_argument(
        "--no-save-check",
        dest="no_save_check",
        action="store_true",
        help=(
            "do not prove or check the save: runs click Save and finish (for an "
            "app that refuses to write, such as a trial build)"
        ),
    )
    automation_register.add_argument("--no-overlay", action="store_true")

    automation_append = automation_sub.add_parser(
        "append", help="append one designed segment to an automation"
    )
    automation_append.add_argument("name", help="the registered automation")
    automation_append.add_argument("segment", help="the segment id, from `automation segments`")
    automation_append.add_argument(
        "--dry-run",
        action="store_true",
        help="show the resulting plan without writing it",
    )
    automation_script = automation_sub.add_parser(
        "script", help="write the double-clickable launcher for an automation"
    )
    automation_script.add_argument("name", help="the registered automation")

    automation_rules = automation_sub.add_parser(
        "rules", help="list the situations an automation has learned"
    )
    automation_rules.add_argument("name", help="the registered automation")

    automation_learn = automation_sub.add_parser(
        "learn", help="teach the screen FACT is showing now as a way past it"
    )
    automation_learn.add_argument("name", help="the registered automation")
    automation_learn.add_argument(
        "--click", action="append", default=[], metavar="BUTTON",
        help="click this control; repeat for more clicks, in order",
    )
    automation_learn.add_argument(
        "--double-click", dest="double_click", action="append", default=[],
        metavar="BUTTON", help="double-click this control; repeat for more",
    )
    automation_learn.add_argument(
        "--key", dest="keys", default="", metavar="KEYS",
        help="press this key after the clicks, e.g. Enter or Ctrl+S",
    )
    automation_learn.add_argument(
        "--expect", default="", metavar="STATE",
        help="the screen that must be showing afterwards (a state id)",
    )
    automation_learn.add_argument("--description", default="")
    automation_learn.add_argument("--id", dest="rule_id", default=None)
    automation_learn.add_argument("--env-file", dest="sub_env", default=None)

    automation_forget = automation_sub.add_parser(
        "forget", help="remove one learned rule"
    )
    automation_forget.add_argument("name", help="the registered automation")
    automation_forget.add_argument("rule", help="the rule id, from `automation rules`")

    automation_sub.add_parser(
        "segments", help="list the designed segments that can be appended"
    )

    automation_state = automation_sub.add_parser(
        "state", help="show which screen FACT is showing"
    )
    automation_state.add_argument("--env-file", dest="sub_env", default=None)
    automation_sub.add_parser("list", help="list saved automations and their last run")

    subparsers.add_parser("wizard", help="build and prove a workflow interactively")
    subparsers.add_parser("check", help="print the environment report")
    subparsers.add_parser(
        "doctor", help="check this machine can run an automation, in one screen"
    )

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
    if args.command == "doctor":
        return run_doctor(env_path)
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
                from_screen=args.from_screen,
                overlay=not args.no_overlay,
                env_path=env_path,
            )
        if sub == "register":
            return run_register(
                args.name,
                config_path,
                args.sub_values,
                dry_run=args.dry_run,
                check_save=not args.no_save_check,
                overlay=not args.no_overlay,
                env_path=env_path,
            )
        if sub == "append":
            return run_append_automation(
                args.name, args.segment, dry_run=args.dry_run
            )
        if sub == "script":
            return run_write_launcher(args.name)
        if sub == "rules":
            return run_rules(args.name)
        if sub == "learn":
            return run_learn_rule(
                args.name,
                clicks=args.click,
                doubles=args.double_click,
                keys=args.keys,
                expect=args.expect,
                description=args.description,
                rule_id=args.rule_id,
                env_path=env_path,
            )
        if sub == "forget":
            return run_forget_rule(args.name, args.rule)
        if sub == "segments":
            return run_list_segments()
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
