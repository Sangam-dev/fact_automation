"""Declarative actions for the ordered workflow runner.

A workflow is described as an ordered list of steps. Only two actions are
supported, on purpose: click a named button, and fill a named text field.
Dropdowns, date pickers and grids need control-specific interaction, so they
stay out of this model until there is a proven way to drive them.
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


FormStep = Annotated[Union[ClickStep, FillStep], Field(discriminator="action")]


__all__ = ["ClickStep", "FillStep", "FormStep"]
