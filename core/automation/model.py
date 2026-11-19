"""A saved, runnable automation: an ordered plan plus the screen each step needs.

An automation is what the wizard produces and what ``automation run`` executes.
It is deliberately *not* an :class:`~core.config.AgentConfig`: the config is the
runtime shape the existing runner already understands, while this is the
authored artefact - the same steps with a checkpoint on each one, so a run can
be started from the middle instead of only from the beginning.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.config import AgentConfig, AppConfig, FieldConfig

AUTOMATION_SCHEMA = "automation/1"

# How a run may approach one step again.
#   replay    - running the step from here is harmless (navigation, idempotent).
#   restart   - the step types into a form, so it can only be redone from the
#               nearest earlier safe boundary, never resumed in place.
#   hand_edit - a person has to do this one; a run must stop and ask.
ResumePolicy = Literal["replay", "restart", "hand_edit"]

RESUME_POLICIES: tuple[str, ...] = ("replay", "restart", "hand_edit")

_STEP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")


class Checkpoint(BaseModel):
    """What must be true of the screen for this step to run.

    ``state`` is an id from the learned state table (``04_view``, ``05_add``),
    and it is required, never defaulted: a step that does not say which screen
    it belongs on cannot be resumed safely.
    """

    model_config = ConfigDict(extra="forbid")

    state: str
    resume: ResumePolicy
    note: str = ""

    @field_validator("state")
    @classmethod
    def _named_state(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("a checkpoint must name the state it expects")
        return value


class AutomationStep(BaseModel):
    """One click or one typed value, with the screen it expects first."""

    model_config = ConfigDict(extra="forbid")

    id: str
    action: Literal["click", "fill"]
    button: str | None = None
    key: str | None = None
    field: str | None = None
    optional: bool = False
    checkpoint: Checkpoint

    @field_validator("id")
    @classmethod
    def _clean_id(cls, value: str) -> str:
        value = value.strip()
        if not _STEP_ID.match(value):
            raise ValueError(
                "step ids are letters, digits, '_.-' and start with a letter "
                f"or digit: {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _action_fields(self) -> "AutomationStep":
        if self.action == "click":
            button = (self.button or "").strip()
            if not button:
                raise ValueError("a click step needs a button name")
            object.__setattr__(self, "button", button)
            if self.key or self.field:
                raise ValueError("a click step has no key or field")
        else:
            key = (self.key or "").strip()
            if not key:
                raise ValueError("a fill step needs a key naming its value")
            object.__setattr__(self, "key", key)
            if self.button:
                raise ValueError("a fill step has no button")
            if self.field is not None:
                field = self.field.strip()
                object.__setattr__(self, "field", field or None)
        return self

    def describe(self) -> str:
        target = self.button if self.action == "click" else (self.field or self.key)
        return f"{self.id} ({self.action} {target!r})"


class SaveSpec(BaseModel):
    """How the record is saved and how the save is proved."""

    model_config = ConfigDict(extra="forbid")

    button: str = "Save"
    verifier: Literal["text", "row", "none"] = "row"
    footprint: list[str] = Field(default_factory=list)
    success_pattern: str = ""

    @field_validator("button")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("the save button must not be empty")
        return value


class Automation(BaseModel):
    """One saved automation, as stored in ``config/automations/<name>/``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # ``schema`` is the key every config file in this project uses; the
    # attribute is aliased because ``schema`` shadows a pydantic method.
    schema_version: str = Field(default=AUTOMATION_SCHEMA, alias="schema")
    name: str
    created_at: str = ""
    updated_at: str = ""
    verified: bool = False
    app: AppConfig
    fields: list[FieldConfig] = Field(default_factory=list)
    steps: list[AutomationStep] = Field(min_length=1)
    save: SaveSpec = Field(default_factory=SaveSpec)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("an automation must have a name")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "Automation":
        if self.schema_version != AUTOMATION_SCHEMA:
            raise ValueError(
                f"unsupported automation schema {self.schema_version!r}; "
                f"expected {AUTOMATION_SCHEMA!r}"
            )
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"step id {step.id!r} is used twice")
            seen.add(step.id)

        declared = {item.name for item in self.fields}
        undeclared = [
            step.key
            for step in self.steps
            if step.action == "fill" and step.key not in declared
        ]
        if undeclared:
            raise ValueError(
                "step(s) fill field(s) that `fields` does not declare: "
                + ", ".join(repr(name) for name in dict.fromkeys(undeclared))
            )

        # Save is clicked once, by the runner, after the last step and with its
        # result written ahead. A plan that also clicks it would save twice.
        double_save = [
            step.id
            for step in self.steps
            if step.action == "click"
            and step.button
            and step.button.casefold() == self.save.button.casefold()
        ]
        if double_save:
            raise ValueError(
                "step(s) click the save button "
                f"{self.save.button!r}: {', '.join(repr(name) for name in double_save)}. "
                "The save is described by `save` and clicked once by the runner; "
                "remove the step"
            )

        unknown_footprint = [name for name in self.save.footprint if name not in declared]
        if unknown_footprint:
            raise ValueError(
                "save.footprint names field(s) that `fields` does not declare: "
                + ", ".join(repr(name) for name in unknown_footprint)
            )
        return self

    # ------------------------------------------------------------- inspection --
    def state_ids(self) -> list[str]:
        """Every screen the plan expects, in first-use order."""
        return list(dict.fromkeys(step.checkpoint.state for step in self.steps))

    def step_ids(self) -> list[str]:
        return [step.id for step in self.steps]

    def fill_keys(self) -> list[str]:
        """Every record key the plan types, in first-use order."""
        return list(
            dict.fromkeys(
                step.key for step in self.steps if step.action == "fill" and step.key
            )
        )

    def validate_states(self, known: Iterable[str]) -> None:
        """Fail if a checkpoint names a screen the recogniser does not know.

        A run should die here - before a click - rather than halfway through a
        form, so this is called at load time by whoever runs the plan.
        """
        known_set = set(known)
        if not known_set:
            return
        unknown = [state for state in self.state_ids() if state not in known_set]
        if unknown:
            raise AutomationError(
                f"automation {self.name!r} expects state(s) that the state table "
                f"does not define: {', '.join(unknown)}; known: "
                + ", ".join(sorted(known_set))
            )

    def to_config(self) -> AgentConfig:
        """The same plan in the shape the existing runner executes.

        Built as plain data and validated by :class:`AgentConfig`, so the
        execution layer stays the only place that own the step models.
        """
        steps: list[dict] = []
        for step in self.steps:
            payload: dict = {"action": step.action, "optional": step.optional}
            if step.action == "click":
                payload["button"] = step.button
            else:
                payload["key"] = step.key
                if step.field:
                    payload["field"] = step.field
            steps.append(payload)

        return AgentConfig.model_validate(
            {
                "app": self.app.model_dump(),
                "fields": [item.model_dump() for item in self.fields],
                "actions": {"save_button": self.save.button},
                "workflow": {"steps": steps},
                "confirmation": {
                    "verifier": self.save.verifier,
                    "footprint": self.save.footprint,
                    "success_pattern": self.save.success_pattern,
                },
            }
        )

    def touch(self, when: datetime | None = None) -> "Automation":
        """Stamp ``updated_at``; ``created_at`` is set once, on first save."""
        stamp = (when or datetime.now()).isoformat(timespec="seconds")
        if not self.created_at:
            self.created_at = stamp
        self.updated_at = stamp
        return self


class AutomationError(RuntimeError):
    """The automation is inconsistent or cannot be used."""


__all__ = [
    "AUTOMATION_SCHEMA",
    "RESUME_POLICIES",
    "Automation",
    "AutomationError",
    "AutomationStep",
    "Checkpoint",
    "ResumePolicy",
    "SaveSpec",
]
