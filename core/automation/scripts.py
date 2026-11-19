"""The pre-written scripts a plan can hand one screen to.

These are the parts that were *written*, not discovered: a filler that already
knows how to drive a sideways-scrolling grid, settle a lookup, or wait for a
code's pop-up. They live in ``execution/scripts/`` so that everything in that
folder is a thing the wizard can offer you - drop a script in, and it appears in
the deterministic menu without any registration step.

A script in that folder is a leaf for the plan: it types into the form it finds
and never clicks Save. Its inputs come from the run, so the same file serves
every record:

* ``{values_file}`` - the JSON the run was given;
* ``{job_id}`` - the job this run belongs to.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from core.config import project_root

#: Where the pre-written scripts live, relative to the project root.
SCRIPTS_DIR = ("execution", "scripts")

#: The suffix a file in that folder must have to be offered.
SCRIPT_SUFFIX = ".py"

#: What a script step passes by default: the run's own values file.
DEFAULT_SCRIPT_ARGS = ("{values_file}",)

#: How long a script may run before the plan stops it. These scripts drive a
#: GUI, and product lookups in FACT can take 13 s on a good day.
DEFAULT_SCRIPT_TIMEOUT_SECONDS = 900.0

#: How much of the script's own docstring the menu shows.
SUMMARY_WIDTH = 100


class ScriptError(RuntimeError):
    """The script folder cannot be read, or a named script is not there."""


@dataclass(frozen=True)
class ScriptEntry:
    """One pre-written script, as the wizard offers it."""

    #: The file's name without its suffix; how the menu names it.
    id: str
    #: Its path from the project root, which is what a ``run`` step stores.
    path: str
    #: The first line of its docstring, so the menu can say what it does.
    summary: str = ""

    def describe(self) -> str:
        summary = self.summary
        if len(summary) > SUMMARY_WIDTH:
            summary = summary[: SUMMARY_WIDTH - 3].rstrip() + "..."
        return f"{self.id} - {summary}" if summary else self.id


def scripts_root(root: str | Path | None = None) -> Path:
    """Where the pre-written scripts live."""
    base = Path(root) if root is not None else project_root()
    return base.joinpath(*SCRIPTS_DIR)


def _summary(path: Path) -> str:
    """The first line of the script's docstring, or ``""``.

    A script that cannot even be parsed still gets listed: refusing to show it
    would hide the very thing somebody needs to see and fix.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return ""
    docstring = ast.get_docstring(tree) or ""
    return next((line.strip() for line in docstring.splitlines() if line.strip()), "")


def _relative(path: Path, root: str | Path | None) -> str:
    base = Path(root) if root is not None else project_root()
    try:
        return path.relative_to(base).as_posix()
    except ValueError:  # a folder outside the project is stored as it is
        return path.as_posix()


def find_scripts(root: str | Path | None = None) -> list[ScriptEntry]:
    """Every pre-written script, in a stable order, ready to be chosen."""
    folder = scripts_root(root)
    if not folder.is_dir():
        return []
    entries = []
    for path in sorted(folder.glob(f"*{SCRIPT_SUFFIX}")):
        if path.name.startswith((".", "_")):
            continue
        entries.append(
            ScriptEntry(
                id=path.stem,
                path=_relative(path, root),
                summary=_summary(path),
            )
        )
    return entries


def get_script(script_id: str, root: str | Path | None = None) -> ScriptEntry:
    """The script with this id, or a refusal that names the ones there are."""
    for entry in find_scripts(root):
        if entry.id == script_id:
            return entry
    available = ", ".join(entry.id for entry in find_scripts(root))
    raise ScriptError(
        f"there is no script {script_id!r} in {scripts_root(root)}; "
        f"available: {available or '(none)'}"
    )


def script_step(
    entry: ScriptEntry,
    *,
    args: list[str] | None = None,
    timeout_seconds: float = DEFAULT_SCRIPT_TIMEOUT_SECONDS,
) -> dict:
    """The plan entry that runs this script where it is chosen."""
    return {
        "action": "run",
        "script": entry.path,
        "args": list(DEFAULT_SCRIPT_ARGS if args is None else args),
        "timeout_seconds": timeout_seconds,
    }


def library_lines(entries: list[ScriptEntry] | None = None) -> list[str]:
    """One line per script, for a person choosing what to insert."""
    return [f" - {entry.describe()}" for entry in (entries or find_scripts())]


__all__ = [
    "DEFAULT_SCRIPT_ARGS",
    "DEFAULT_SCRIPT_TIMEOUT_SECONDS",
    "SCRIPTS_DIR",
    "SCRIPT_SUFFIX",
    "ScriptEntry",
    "ScriptError",
    "find_scripts",
    "get_script",
    "library_lines",
    "script_step",
    "scripts_root",
]
