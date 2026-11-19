"""Where saved automations live, and how they are read and written.

One automation is one directory, so it can grow siblings later (its previous
version, a generated launcher, evidence from its last proof run):

    config/automations/fact_automation/
        automation.json
        previous.json      (written when a save replaces an existing file)

The name is part of the path, so it is validated rather than trusted: it comes
from a person typing it in a wizard, and it becomes a directory and a script
file name.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.automation.model import Automation, AutomationError
from core.config import project_root

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
AUTOMATION_FILE = "automation.json"
PREVIOUS_FILE = "previous.json"


class AutomationNameError(AutomationError):
    """The requested name cannot be a directory name."""


class AutomationNotFound(AutomationError):
    """No automation is stored under that name."""


def validate_name(name: str) -> str:
    """Return the canonical name, or explain why it cannot be used."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise AutomationNameError("an automation needs a name")
    if cleaned != cleaned.lower():
        raise AutomationNameError(
            f"automation names are lower case: use {cleaned.lower()!r}"
        )
    if not NAME_PATTERN.match(cleaned):
        raise AutomationNameError(
            "automation names are 2-40 characters of a-z, 0-9 and '_', "
            f"starting with a letter: {cleaned!r}"
        )
    return cleaned


def automations_root(root: str | Path | None = None) -> Path:
    """The directory holding every saved automation."""
    if root is not None:
        base = Path(root)
        return base if base.is_absolute() else project_root() / base
    return project_root() / "config" / "automations"


def automation_path(name: str, root: str | Path | None = None) -> Path:
    """The file one automation is stored in."""
    return automations_root(root) / validate_name(name) / AUTOMATION_FILE


def exists(name: str, root: str | Path | None = None) -> bool:
    try:
        return automation_path(name, root).is_file()
    except AutomationNameError:
        return False


def list_automations(root: str | Path | None = None) -> list[str]:
    """Every stored automation name, sorted.

    A folder without a readable ``automation.json`` is skipped rather than
    listed: half-saved or hand-made folders must not appear as runnable.
    """
    folder = automations_root(root)
    if not folder.is_dir():
        return []
    names: list[str] = []
    for entry in sorted(folder.iterdir()):
        if not entry.is_dir() or not NAME_PATTERN.match(entry.name):
            continue
        if (entry / AUTOMATION_FILE).is_file():
            names.append(entry.name)
    return names


def load(name: str, root: str | Path | None = None) -> Automation:
    """Read one automation, structurally validated.

    State ids are *not* checked here - that needs the state table, and the
    runner does it with :meth:`Automation.validate_states` before acting.
    """
    canonical = validate_name(name)
    path = automation_path(canonical, root)
    if not path.is_file():
        raise AutomationNotFound(
            f"no automation {canonical!r} at {path}; "
            "run the wizard's register step to create one"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AutomationError(f"cannot read {path}: {exc}") from exc
    try:
        automation = Automation.model_validate(data)
    except Exception as exc:
        raise AutomationError(f"{path} is not a valid automation: {exc}") from exc

    if automation.name != canonical:
        raise AutomationError(
            f"{path} is named {automation.name!r} but lives in {canonical!r}; "
            "the file name and the automation name must agree"
        )
    return automation


def save(automation: Automation, root: str | Path | None = None) -> Path:
    """Write one automation atomically, keeping the version it replaces."""
    canonical = validate_name(automation.name)
    path = automation_path(canonical, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    automation.touch()

    if path.is_file():
        try:
            (path.parent / PREVIOUS_FILE).write_text(
                path.read_text(encoding="utf-8"), encoding="utf-8"
            )
        except OSError:
            # A missing safety copy must not stop the save: the file being
            # written is the one that matters.
            pass

    payload = automation.model_dump(mode="json", exclude_none=True, by_alias=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return path


__all__ = [
    "AUTOMATION_FILE",
    "NAME_PATTERN",
    "PREVIOUS_FILE",
    "AutomationNameError",
    "AutomationNotFound",
    "automation_path",
    "automations_root",
    "exists",
    "list_automations",
    "load",
    "save",
    "validate_name",
]
