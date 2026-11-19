"""Declarative actions for the ordered workflow runner.

A workflow is described as an ordered list of steps. Four actions are
supported, on purpose:

* ``click``        - click a named button;
* ``double_click`` - the same, twice, for what FACT opens that way;
* ``fill``         - type one value into one named text field;
* ``key``          - press a key or a chord (``Enter``, ``Tab``, ``Ctrl+S``).

A fifth action, ``run``, is the escape hatch for work that is *designed* rather
than discovered: a script that already knows how to drive this form (a sideways
scrolling grid, a lookup that settles on its own) is one step in the plan, not a
second runner beside it. It is a leaf: it types into the screen and touches no
control the plan names, so the screen before it is checked exactly like any
other step, and the screen after it is where the next step's checkpoint looks.

Combo boxes, date pickers and grid cells are still left out: they need
control-specific interaction, and a click plus a key press is the honest way to
drive one until that behaviour has been proved on the live form.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _clean(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("step names must not be empty")
    return value


class _Step(BaseModel):
    """Fields shared by every step."""

    model_config = ConfigDict(extra="forbid")

    optional: bool = False


class ClickStep(_Step):
    """Click a button, matched by its UIA name."""

    action: Literal["click"]
    button: str

    @field_validator("button")
    @classmethod
    def _named(cls, value: str) -> str:
        return _clean(value)


class DoubleClickStep(_Step):
    """Double-click a control, matched by its UIA name.

    FACT opens a listed record this way, and the two clicks are sent together so
the application sees a double-click rather than two clicks.
    """

    action: Literal["double_click"]
    button: str

    @field_validator("button")
    @classmethod
    def _named(cls, value: str) -> str:
        return _clean(value)


class KeyStep(_Step):
    """Press one key or one chord; no control is clicked.

    ``keys`` is written the way a person writes it: ``Enter``, ``Tab``,
    ``F5``, ``Ctrl+S``. The executor resolves the name to a virtual key when the
    step runs, so a typo fails at the step, with the step number in the message.
    """

    action: Literal["key"]
    keys: str

    @field_validator("keys")
    @classmethod
    def _keys(cls, value: str) -> str:
        cleaned = _clean(value)
        if len(cleaned.split("+")) > 2:
            raise ValueError(
                f"{cleaned!r} names more than one modifier; write it as Ctrl+S"
            )
        return cleaned


class FillStep(_Step):
    """Type one data value into one text field.

    ``key`` selects the value from the record passed to the runner; ``field``
    overrides the UIA label when the same value has to go into a differently
    named control. By default the label comes from ``fields``.
    """

    action: Literal["fill"]
    key: str
    field: str | None = None

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        return _clean(value)

    @field_validator("field")
    @classmethod
    def _field(cls, value: str | None) -> str | None:
        return None if value is None else _clean(value)


class ScriptStep(_Step):
    """Run a script that drives the form itself, as one step in the plan.

    ``script`` is a path relative to the project root (``execution/workflows/
    form_filler_1.py``) or a command. A ``.py`` script is run with the same
    interpreter that is running the plan, so the run cannot pick up a different
    Python from ``PATH``.

    ``args`` may contain two placeholders, filled in when the step runs:

    * ``{values_file}`` - the values JSON the run was given, so the script and
      the plan cannot disagree about the record;
    * ``{job_id}`` - the job this run belongs to, for the script's own log.

    The step succeeds only if the script exits 0. It never clicks Save: the
    plan's one save still belongs to the runner, after the last step.
    """

    action: Literal["run"]
    script: str
    args: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=900.0, gt=0)

    @field_validator("script")
    @classmethod
    def _script(cls, value: str) -> str:
        return _clean(value)

    @field_validator("args")
    @classmethod
    def _args(cls, value: list[str]) -> list[str]:
        return [str(item) for item in value]


FormStep = Annotated[
    Union[ClickStep, DoubleClickStep, KeyStep, FillStep, ScriptStep],
    Field(discriminator="action"),
]


__all__ = [
    "ClickStep",
    "DoubleClickStep",
    "FillStep",
    "FormStep",
    "KeyStep",
    "ScriptStep",
]
