"""How far one automation run got, and whether its Save is still unresolved.

This is deliberately a *separate* record from :model:`core.state.store`, which
tracks Excel rows in a batch (``pending`` is a list of row numbers). A step
cursor is a different thing with different rules, and mixing them would either
break that model's version check or make ``row_number`` mean two things.

Two guards live here, because they are the difference between a resume and a
duplicate invoice:

* ``values_digest`` - the record data the run started with. Resuming with
  different values is refused unless the run is restarted on purpose.
* ``pending_save`` - written *before* the Save click and cleared only once the
  save is confirmed. A process that dies in between leaves this set, and the
  next run must stop and ask rather than click Save a second time.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.config import project_root

PROGRESS_SCHEMA = "automation-progress/1"

OUTCOME_SAVED = "saved"
OUTCOME_FAILED = "failed"
OUTCOME_ABORTED = "aborted"


class PendingSave(BaseModel):
    """The marker written before Save is clicked, cleared when it is proved."""

    model_config = ConfigDict(extra="forbid")

    step_id: str = "save"
    started_at: str = ""


class ProgressState(BaseModel):
    """One automation's last run."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: str = Field(default=PROGRESS_SCHEMA, alias="schema")
    run_id: str
    automation: str
    values_digest: str = ""
    cursor: int = Field(default=0, ge=0)
    completed: list[str] = Field(default_factory=list)
    pending_save: PendingSave | None = None
    outcome: str = ""
    updated_at: str = ""

    @model_validator(mode="after")
    def _known_schema(self) -> "ProgressState":
        if self.schema_version != PROGRESS_SCHEMA:
            raise ValueError(
                f"unsupported progress schema {self.schema_version!r}; "
                f"expected {PROGRESS_SCHEMA!r}"
            )
        return self

    # ---------------------------------------------------------------- guards --
    @property
    def save_unresolved(self) -> bool:
        """True when a Save was attempted and its result is not known."""
        return self.pending_save is not None and self.outcome != OUTCOME_SAVED

    def block_reason(self) -> str:
        """Why a run must not continue, in words a person can act on."""
        if not self.save_unresolved:
            return ""
        step = self.pending_save.step_id if self.pending_save else "save"
        when = self.pending_save.started_at if self.pending_save else ""
        return (
            f"run {self.run_id} reached Save ({step}"
            + (f" at {when}" if when else "")
            + ") and the result is unknown; check the record in FACT, then "
            "re-run with --saved-already or --not-saved"
        )

    def values_changed(self, digest: str) -> bool:
        """True when the record data differs from the run this continues."""
        return bool(self.values_digest and digest and self.values_digest != digest)

    # ------------------------------------------------------------- recording --
    def mark_step(self, index: int, step_id: str, when: datetime | None = None) -> None:
        """Record that the step at ``index`` finished; the cursor moves past it."""
        self.cursor = max(self.cursor, index + 1)
        if step_id not in self.completed:
            self.completed.append(step_id)
        self.touch(when)

    def begin_save(self, step_id: str = "save", when: datetime | None = None) -> None:
        """Write ahead: Save is about to be clicked."""
        self.pending_save = PendingSave(
            step_id=step_id,
            started_at=(when or datetime.now()).isoformat(timespec="seconds"),
        )
        self.touch(when)

    def resolve_save(self, saved: bool, when: datetime | None = None) -> None:
        """Clear the marker once its result is known."""
        self.pending_save = None
        if saved:
            self.outcome = OUTCOME_SAVED
        elif self.outcome == OUTCOME_SAVED:
            self.outcome = ""
        self.touch(when)

    def finish(self, outcome: str, when: datetime | None = None) -> None:
        """Record how the run ended without touching the save marker."""
        self.outcome = outcome
        self.touch(when)

    def touch(self, when: datetime | None = None) -> None:
        self.updated_at = (when or datetime.now()).isoformat(timespec="seconds")


def digest_values(values: Mapping[str, Any]) -> str:
    """A stable fingerprint of the record data, order-independent."""
    payload = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def new_progress(
    automation: str,
    values: Mapping[str, Any],
    *,
    run_id: str | None = None,
    when: datetime | None = None,
) -> ProgressState:
    """A fresh record for a run that is about to start at the first step."""
    stamp = when or datetime.now()
    return ProgressState(
        run_id=run_id or stamp.strftime("%Y%m%d_%H%M%S"),
        automation=automation,
        values_digest=digest_values(values),
        updated_at=stamp.isoformat(timespec="seconds"),
    )


def progress_root(root: str | Path | None = None) -> Path:
    """The directory holding one progress file per automation."""
    if root is not None:
        base = Path(root)
        return base if base.is_absolute() else project_root() / base
    return project_root() / "data" / "state" / "automation"


def progress_path(name: str, root: str | Path | None = None) -> Path:
    return progress_root(root) / f"{name}.json"


def load_progress(name: str, root: str | Path | None = None) -> ProgressState | None:
    """Read an automation's last run, or ``None`` when it never ran."""
    path = progress_path(name, root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProgressError(f"cannot read {path}: {exc}") from exc
    try:
        return ProgressState.model_validate(data)
    except Exception as exc:
        raise ProgressError(f"{path} is not a valid progress record: {exc}") from exc


def save_progress(progress: ProgressState, root: str | Path | None = None) -> Path:
    """Write the record atomically, so a crash leaves the previous one intact."""
    path = progress_path(progress.automation, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = progress.model_dump(mode="json", exclude_none=True, by_alias=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return path


def clear_progress(name: str, root: str | Path | None = None) -> bool:
    """Forget an automation's last run; True when a file was removed."""
    path = progress_path(name, root)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ProgressError(f"cannot remove {path}: {exc}") from exc


class ProgressError(RuntimeError):
    """The progress record is missing, unreadable, or inconsistent."""


__all__ = [
    "OUTCOME_ABORTED",
    "OUTCOME_FAILED",
    "OUTCOME_SAVED",
    "PROGRESS_SCHEMA",
    "PendingSave",
    "ProgressError",
    "ProgressState",
    "clear_progress",
    "digest_values",
    "load_progress",
    "new_progress",
    "progress_path",
    "progress_root",
    "save_progress",
]
