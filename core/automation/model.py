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
from pathlib import Path
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.config import AgentConfig, AppConfig, FieldConfig, project_root
from execution.actions.models import ProgressWait

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
    """One interaction, with the screen it expects first.

    ``click`` / ``double_click`` name a button; ``fill`` types a data value into
    a field; ``key`` presses a key or a chord with no target at all; ``run``
    hands the screen to a script that already knows how to drive it (the
    deterministic form filler), and is a step like any other so that a plan can
    be navigation, then that script, then more steps.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    action: Literal["click", "double_click", "fill", "key", "run"]
    button: str | None = None
    wait_for_progress: ProgressWait | None = None
    key: str | None = None
    field: str | None = None
    #: The keys a ``key`` step presses, e.g. ``"Enter"`` or ``"Ctrl+S"``.
    keys: str | None = None
    #: The script a ``run`` step executes, relative to the project root.
    script: str | None = None
    #: Arguments for that script; ``{values_file}`` and ``{job_id}`` are filled
    #: in from the run, so the script types the same record the plan was given.
    args: list[str] = Field(default_factory=list)
    #: How long a ``run`` step may take before it is stopped.
    timeout_seconds: float | None = None
    optional: bool = False
    #: Runs once per row of the values file's ``rows`` list, instead of once
    #: per record. A document with N line items is one record, not N.
    per_row: bool = False
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
        if self.action in ("click", "double_click"):
            button = (self.button or "").strip()
            if not button:
                raise ValueError(f"a {self.action} step needs a button name")
            object.__setattr__(self, "button", button)
            if self.key or self.field or self.keys:
                raise ValueError(f"a {self.action} step has no key, field or keys")
            if self.action == "double_click" and self.wait_for_progress is not None:
                raise ValueError("progress waits are supported only on click steps")
            self._no_script()
        elif self.action == "key":
            keys = (self.keys or "").strip()
            if not keys:
                raise ValueError("a key step needs the keys to press, e.g. \"Enter\"")
            if len(keys.split("+")) > 2:
                raise ValueError(
                    f"{keys!r} names more than one modifier; write it as Ctrl+S"
                )
            object.__setattr__(self, "keys", keys)
            if (
                self.button
                or self.key
                or self.field
                or self.wait_for_progress is not None
            ):
                raise ValueError("a key step has no button, key, field, or progress wait")
            self._no_script()
        elif self.action == "run":
            script = (self.script or "").strip()
            if not script:
                raise ValueError(
                    "a run step needs the script to run, e.g. "
                    '"execution/scripts/form_filler_1.py"'
                )
            object.__setattr__(self, "script", script)
            if (
                self.button
                or self.key
                or self.field
                or self.keys
                or self.wait_for_progress is not None
            ):
                raise ValueError("a run step has no button, key, field or keys")
            if self.per_row:
                raise ValueError(
                    "a run step cannot repeat per row: it fills one form, and "
                    "the line items are its own business, not the plan's"
                )
            timeout = 900.0 if self.timeout_seconds is None else float(
                self.timeout_seconds
            )
            if timeout <= 0:
                raise ValueError(
                    "a run step's timeout has to be more than zero seconds"
                )
            object.__setattr__(self, "timeout_seconds", timeout)
            object.__setattr__(self, "args", [str(item) for item in self.args])
            # A script types into the form, so it can never be replayed in
            # place: a resumed run walks back to the click that opened the form.
            if self.checkpoint.resume != "restart":
                raise ValueError(
                    "a run step types into the form, so its checkpoint has to "
                    'say "resume": "restart"; resuming it in place would type '
                    "the same values over themselves"
                )
        else:
            key = (self.key or "").strip()
            if not key:
                raise ValueError("a fill step needs a key naming its value")
            object.__setattr__(self, "key", key)
            if self.button or self.keys or self.wait_for_progress is not None:
                raise ValueError("a fill step has no button, keys, or progress wait")
            if self.field is not None:
                field = self.field.strip()
                object.__setattr__(self, "field", field or None)
            self._no_script()
        return self

    def _no_script(self) -> None:
        """Refuse a script on a step that is not a ``run`` step."""
        if self.script or self.args or self.timeout_seconds is not None:
            raise ValueError(
                f"a {self.action} step has no script, args or timeout; "
                "only a run step does"
            )

    def describe(self) -> str:
        if self.action == "key":
            target = self.keys
        elif self.action in ("click", "double_click"):
            target = self.button
        elif self.action == "run":
            target = self.script
        else:
            target = self.field or self.key
        return f"{self.id} ({self.action} {target!r})"


class SaveSpec(BaseModel):
    """How the record is saved and how the save is proved."""

    model_config = ConfigDict(extra="forbid")

    button: str = "Save"
    #: Whether a run checks that the record actually landed. Turned off for an
    #: app whose save cannot be proved (a trial that refuses to write): the run
    #: then clicks Save and reports the record as done without looking for it.
    check: bool = True
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
    #: How long one screen change may take. Clicking Login can spend seconds
    #: before the main window is drawable, and FACT swaps whole windows between
    #: screens, so every wait in the runner is this one number: the per-step
    #: checkpoint, the re-read after a click, and the wait for a learned rule.
    #: Raise it on a slow machine or a slow module; lower it to fail faster.
    wait_seconds: float = Field(default=15.0, ge=0.5, le=120)
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
            if step.action in ("click", "double_click")
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

        # The repeating part is one block: a step that runs once cannot sit
        # between two that run for every row, because "once" would then mean
        # "once per row" and the plan would say something it does not do.
        flags = [step.per_row for step in self.steps]
        if any(flags) and flags != sorted(flags):
            first = flags.index(True)
            raise ValueError(
                "step(s) that do not repeat come after step(s) that repeat per row "
                f"({self.steps[flags.index(False, first)].id!r}); put every per-row "
                "step in one block at the end"
            )
        return self

    # ------------------------------------------------------------- inspection --
    def state_ids(self) -> list[str]:
        """Every screen the plan expects, in first-use order."""
        return list(dict.fromkeys(step.checkpoint.state for step in self.steps))

    def step_ids(self) -> list[str]:
        return [step.id for step in self.steps]

    def fill_keys(self) -> list[str]:
        """Every key the plan types once, per record, in first-use order."""
        return list(
            dict.fromkeys(
                step.key
                for step in self.steps
                if step.action == "fill" and step.key and not step.per_row
            )
        )

    def row_keys(self) -> list[str]:
        """Every key the plan types once per row, in first-use order."""
        return list(
            dict.fromkeys(
                step.key
                for step in self.steps
                if step.action == "fill" and step.key and step.per_row
            )
        )

    def split_steps(self) -> tuple[list[AutomationStep], list[AutomationStep]]:
        """``(steps that run once, steps that repeat per row)``."""
        once = [step for step in self.steps if not step.per_row]
        rows = [step for step in self.steps if step.per_row]
        return once, rows

    def repeats_per_row(self) -> bool:
        return any(step.per_row for step in self.steps)

    def fills_rows_itself(self) -> bool:
        """Whether a step enters the line items from the values file itself.

        A ``run`` step handed ``{values_file}`` reads the whole document, lines
        included - the form filler does - so such a plan needs no per-row
        steps, and a document with rows is not a mismatch.
        """
        return any(
            step.action == "run" and "{values_file}" in step.args
            for step in self.steps
        )

    def validate_scripts(self, root: str | Path | None = None) -> None:
        """Fail before touching the application when a script cannot run.

        A script step is part of the registered workflow, not an optional file
        discovered at run time. Resolving relative paths here also makes the
        generated launcher independent of the directory it was double-clicked
        from. Absolute paths remain supported for existing external fillers.
        """
        base = project_root() if root is None else Path(root)
        missing: list[str] = []
        for step in self.steps:
            if step.action != "run" or not step.script:
                continue
            path = Path(step.script)
            if not path.is_absolute():
                path = base / path
            if not path.is_file():
                missing.append(f"step {step.id!r}: {path}")
        if missing:
            raise AutomationError(
                "the automation references unavailable script file(s): "
                + "; ".join(missing)
                + ". Restore the deterministic filler or register it again."
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
            if step.action in ("click", "double_click"):
                payload["button"] = step.button
                if step.wait_for_progress is not None:
                    payload["wait_for_progress"] = (
                        step.wait_for_progress.model_dump()
                    )
            elif step.action == "key":
                payload["keys"] = step.keys
            elif step.action == "run":
                payload["script"] = step.script
                payload["args"] = list(step.args)
                payload["timeout_seconds"] = step.timeout_seconds
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
