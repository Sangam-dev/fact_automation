"""Declarative screen states for one application, compiled from snapshots.

A *state* is the answer to "which screen is the app showing?", written down in
a form a program can test: one window identity (automation id and title) plus
the controls that prove it, and the controls that rule it out.

The table is learned from named snapshots captured with ``scripts/snapshots.py``
(see :mod:`desktop.state.learner`) instead of being typed by hand, so it moves
with the application: capture the screen again, re-run the learner, and the
recognizer knows the new screen.

Automatic ids are matched before names, because a name is often the value the
control happens to hold - ``Edit|NepDateEdit`` is called ``2083/03/32`` today -
while an automation id is stable across records and sessions.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.config import project_root

STATE_TABLE_SCHEMA = "states/1"


class ControlNeed(BaseModel):
    """One control that must (or must not) be on screen.

    Exactly one of the two keys is set: ``auto_id`` for a stable identity,
    ``name`` for a control that has no id (most buttons in this app).
    """

    model_config = ConfigDict(extra="forbid")

    auto_id: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def _one_key(self) -> "ControlNeed":
        keys = [value for value in (self.auto_id, self.name) if value]
        if len(keys) != 1:
            raise ValueError("a control need is either an auto_id or a name")
        for key in ("auto_id", "name"):
            value = getattr(self, key)
            if value is not None:
                setattr(self, key, value.strip())
                if not getattr(self, key):
                    raise ValueError("control names must not be blank")
        return self

    def describe(self) -> str:
        if self.auto_id:
            return f"id={self.auto_id!r}"
        return f"name={self.name!r}"


class MatchSpec(BaseModel):
    """How one state's window is recognised.

    ``auto_id`` and the title predicates narrow the window cheaply; ``require``
    and ``forbid`` are checked against its control list only when the cheap
    part matched. ``forbid`` is what separates two screens drawn by the same
    window (the Sale Order View and Add forms share automation id 40200).
    """

    model_config = ConfigDict(extra="forbid")

    auto_id: str = ""
    title_is: str | None = None
    title_contains: str | None = None
    title_ends_with: str | None = None
    require: list[ControlNeed] = Field(default_factory=list)
    forbid: list[ControlNeed] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_identity(self) -> "MatchSpec":
        titles = [self.title_is, self.title_contains, self.title_ends_with]
        if not self.auto_id and not any(titles):
            raise ValueError(
                "a state needs an auto_id or a title predicate to be recognisable"
            )
        titles = [value for value in titles if value]
        if len(titles) > 1:
            raise ValueError("use one title predicate per state")
        return self

    @property
    def needs_controls(self) -> bool:
        """True when the control list has to be read to decide this state."""
        return bool(self.require or self.forbid)

    def describe(self) -> str:
        parts: list[str] = []
        if self.auto_id:
            parts.append(f"auto_id {self.auto_id!r}")
        for label, value in (
            ("title", self.title_is),
            ("title contains", self.title_contains),
            ("title ends", self.title_ends_with),
        ):
            if value:
                parts.append(f"{label} {value!r}")
        if self.require:
            parts.append("needs " + ", ".join(need.describe() for need in self.require))
        if self.forbid:
            parts.append("without " + ", ".join(need.describe() for need in self.forbid))
        return "; ".join(parts)


class StateSpec(BaseModel):
    """One recognised screen."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str = ""
    match: MatchSpec
    provisional: bool = True
    samples: int = 1
    control_count: int = 0

    @model_validator(mode="after")
    def _clean_id(self) -> "StateSpec":
        self.id = self.id.strip()
        if not self.id:
            raise ValueError("state ids must not be blank")
        return self

    def describe(self) -> str:
        return f"{self.id}: {self.match.describe()}"


class StateTable(BaseModel):
    """Every recognised state of one process."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # ``schema`` is the key other config files use; the attribute cannot be
    # called that without shadowing a pydantic method, so it is aliased.
    schema_version: str = Field(default=STATE_TABLE_SCHEMA, alias="schema")
    process: str
    generated_from: str = ""
    generated_at: str = ""
    states: list[StateSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> "StateTable":
        if self.schema_version != STATE_TABLE_SCHEMA:
            raise ValueError(
                f"unsupported state table schema {self.schema_version!r}; "
                f"expected {STATE_TABLE_SCHEMA!r}"
            )
        seen: set[str] = set()
        for state in self.states:
            if state.id in seen:
                raise ValueError(f"state id {state.id!r} is declared twice")
            seen.add(state.id)
        return self

    def ids(self) -> list[str]:
        return [state.id for state in self.states]

    def get(self, state_id: str) -> StateSpec:
        for state in self.states:
            if state.id == state_id:
                return state
        raise KeyError(state_id)


def state_table_path() -> Path:
    """Where the recognised states of the target app live."""
    return project_root() / "config" / "states" / "fact_states.json"


def load_state_table(path: str | Path | None = None) -> StateTable:
    """Read the state table, failing loudly on a shape this code cannot read."""
    file_path = Path(path) if path is not None else state_table_path()
    if not file_path.is_absolute():
        file_path = project_root() / file_path
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StateTableError(
            f"no state table at {file_path}; run "
            "`python scripts/learn_states.py` to build it from your snapshots"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise StateTableError(f"cannot read state table {file_path}: {exc}") from exc
    try:
        return StateTable.model_validate(data)
    except Exception as exc:
        raise StateTableError(f"state table {file_path} is invalid: {exc}") from exc


def save_state_table(table: StateTable, path: str | Path | None = None) -> Path:
    """Write the state table atomically, so a crash cannot leave half of one."""
    file_path = Path(path) if path is not None else state_table_path()
    if not file_path.is_absolute():
        file_path = project_root() / file_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = table.model_dump(mode="json", exclude_none=True, by_alias=True)
    temporary = file_path.with_suffix(file_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(file_path)
    return file_path


class StateTableError(RuntimeError):
    """The state table is missing or cannot be understood."""


__all__ = [
    "STATE_TABLE_SCHEMA",
    "ControlNeed",
    "MatchSpec",
    "StateSpec",
    "StateTable",
    "StateTableError",
    "load_state_table",
    "save_state_table",
    "state_table_path",
]
