"""One user-written instruction + screenshot -> concrete click/fill actions.

Multi-step mode asks the user, in their own words, what to do next ("click Add
New", "double-click the order", "fill up all the form"). This module turns that
one sentence into an action the deterministic executor knows: click or
double-click a named control, type one declared data field into one named text
field, or press a key.

The AI runs once per step, while the user is watching and about to approve the
result. Nothing here is used when rows are replayed.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from core.config import GeminiConfig
from desktop.screenshots.capture import Capture
from desktop.types import (
    CLICKABLE_CONTROL_TYPES,
    EDITABLE_CONTROL_TYPES,
    UIACandidate,
)
from desktop.screenshots.images import prepare_image
from ai.providers.gemini import GeminiClient
from ai.prompts.loader import format_candidates, load_prompt, render_prompt

BUTTON_TYPES = CLICKABLE_CONTROL_TYPES
EDITABLE_TYPES = EDITABLE_CONTROL_TYPES


class StepPlanError(RuntimeError):
    """The instruction could not be turned into actions on this screen."""


class StepUnsupported(StepPlanError):
    """The instruction needs an interaction multi-step mode cannot drive yet."""


@dataclass(frozen=True)
class PlannedFill:
    """One declared data field typed into one live text field."""

    key: str
    label: str
    confidence: float = 0.0


@dataclass(frozen=True)
class StepPlan:
    """The concrete actions one instruction resolves to."""

    kind: Literal["click", "double_click", "fill", "key"]
    label: str = ""
    #: The keys a ``key`` plan presses (``Enter``, ``Ctrl+S``). Named
    #: ``keypress`` because ``keys`` is already the data fields it fills.
    keypress: str = ""
    fills: tuple[PlannedFill, ...] = ()
    confidence: float = 0.0

    @property
    def keys(self) -> tuple[str, ...]:
        """Every data field this step types into."""
        return tuple(fill.key for fill in self.fills)


class _Fill(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str = ""
    field: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)

    @field_validator("key", "field", mode="before")
    @classmethod
    def _no_null_text(cls, value: Any) -> Any:
        return "" if value is None else value

    @field_validator("confidence", mode="before")
    @classmethod
    def _no_null_confidence(cls, value: Any) -> Any:
        return 0.0 if value is None else value


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Literal[
        "click", "double_click", "fill", "key", "unsupported", "unclear"
    ] = "unclear"
    button: str = ""
    keys: str = ""
    fills: list[_Fill] = Field(default_factory=list)
    reason: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)

    @field_validator("button", "keys", "reason", mode="before")
    @classmethod
    def _no_null_text(cls, value: Any) -> Any:
        return "" if value is None else value

    @field_validator("kind", mode="before")
    @classmethod
    def _no_null_kind(cls, value: Any) -> Any:
        return "unclear" if value is None else value

    @field_validator("fills", mode="before")
    @classmethod
    def _no_null_fills(cls, value: Any) -> Any:
        return [] if value is None else value

    @field_validator("confidence", mode="before")
    @classmethod
    def _no_null_confidence(cls, value: Any) -> Any:
        return 0.0 if value is None else value


def _normalise(text: str) -> str:
    return " ".join(str(text).split()).casefold()


def _match_key(text: str) -> str:
    """Normalise a label for matching, ignoring ``&`` accelerators.

    WinForms captions carry accelerators (``"&Save && New"``); the user types
    "Save & New" or "Save New". Dropping ``&`` makes all three the same key.
    """
    return " ".join(str(text).replace("&", " ").split()).casefold()


def _named(
    candidates: Sequence[UIACandidate],
    control_types: frozenset[str],
) -> list[UIACandidate]:
    return [
        item
        for item in candidates
        if item.control_type in control_types
        and (item.name.strip() or item.automation_id.strip() or item.label)
    ]


def match_label(
    candidates: Sequence[UIACandidate],
    label: str,
    control_types: frozenset[str],
) -> UIACandidate | None:
    """Return the live control matching *label*, by name or automation id."""
    wanted = _match_key(label)
    if not wanted:
        return None
    for item in _named(candidates, control_types):
        keys = {
            _match_key(item.name),
            _match_key(item.automation_id),
            _match_key(item.label),
        }
        if wanted in keys:
            return item
    return None


def suggestions(
    candidates: Sequence[UIACandidate],
    label: str,
    control_types: frozenset[str],
    limit: int = 4,
) -> list[str]:
    """Return the closest control labels, for an error message."""
    items = _named(candidates, control_types)
    names = [item.label for item in items]
    normalised = [_match_key(name) for name in names]
    wanted = _match_key(label)

    picked: list[str] = []
    for close in difflib.get_close_matches(wanted, normalised, n=limit, cutoff=0.5):
        name = names[normalised.index(close)]
        if name not in picked:
            picked.append(name)
    if not picked:
        for name, key in zip(names, normalised):
            if name in picked:
                continue
            if wanted and (wanted in key or key in wanted):
                picked.append(name)
            if len(picked) >= limit:
                break
    return picked[:limit]


def parse_step_plan(
    data: dict[str, Any],
    *,
    candidates: Sequence[UIACandidate],
    fields: Sequence[str],
    editable_types: frozenset[str] = EDITABLE_TYPES,
    button_types: frozenset[str] = BUTTON_TYPES,
) -> StepPlan:
    """Validate one model response against the live controls and the fields."""
    try:
        response = _Response.model_validate(data)
    except ValidationError as exc:
        raise StepPlanError(
            f"the model response does not match the schema: {exc}"
        ) from exc

    if response.kind in ("unsupported", "unclear"):
        raise StepUnsupported(
            response.reason.strip() or "the instruction is not something this mode can do yet"
        )

    if response.kind == "key":
        spec = " ".join(response.keys.split())
        if not spec:
            raise StepPlanError(
                "the instruction is a key press, but the model named no key"
            )
        return StepPlan(
            kind="key", keypress=spec, confidence=response.confidence
        )

    if response.kind in ("click", "double_click"):
        label = response.button.strip()
        buttons = _named(candidates, button_types)
        if not label:
            message = "the instruction did not name a button on this screen"
            if buttons:
                message += "; this window has " + ", ".join(
                    repr(item.name) for item in buttons[:4]
                )
            raise StepPlanError(message)
        button = match_label(candidates, label, button_types)
        if button is None:
            if not buttons:
                raise StepPlanError(
                    f"no control named {label!r}, and this window exposes no "
                    "clickable controls to Windows UI Automation; keep the "
                    "window restored and in front, then add the step again"
                )
            message = f"no clickable control on this screen is named {label!r}"
            near = suggestions(candidates, label, button_types)
            if near:
                message += "; it does have " + ", ".join(repr(name) for name in near)
            raise StepPlanError(message)
        return StepPlan(
            kind=response.kind, label=button.label, confidence=response.confidence
        )

    if not response.fills:
        raise StepPlanError("the model returned no field to fill")
    if not _named(candidates, editable_types):
        raise StepPlanError(
            "this window exposes no text fields to Windows UI Automation; keep "
            "the window restored and in front, then add the step again"
        )

    field_set = set(fields)
    problems: list[str] = []
    by_key: dict[str, PlannedFill] = {}
    for item in response.fills:
        key = item.key.strip()
        label = item.field.strip() or key
        if not key:
            problems.append("one fill entry names no data field")
            continue
        if key not in field_set:
            near = difflib.get_close_matches(key, list(fields), n=3, cutoff=0.6)
            message = f"{key!r} is not a declared data field"
            if near:
                message += "; did you mean " + ", ".join(repr(name) for name in near) + "?"
            problems.append(message)
            continue
        field = match_label(candidates, label, editable_types)
        if field is None:
            message = f"no text field on this screen is named {label!r}"
            near = suggestions(candidates, label, editable_types)
            if near:
                message += "; it does have " + ", ".join(repr(name) for name in near)
            problems.append(message)
            continue
        by_key[key] = PlannedFill(
            key=key,
            label=field.name,
            confidence=item.confidence or response.confidence,
        )

    if problems:
        raise StepPlanError("; ".join(problems))
    if not by_key:
        raise StepPlanError("the model filled no field")

    used_labels: dict[str, None] = {}
    fills: list[PlannedFill] = []
    for fill in by_key.values():
        key = _normalise(fill.label)
        if key in used_labels:
            continue
        used_labels[key] = None
        fills.append(fill)
    return StepPlan(kind="fill", fills=tuple(fills), confidence=response.confidence)


def build_step_prompt(
    instruction: str,
    fields: Sequence[str],
    candidates: Sequence[UIACandidate],
    image_width: int,
    image_height: int,
    correction: str = "",
) -> str:
    """Render the step prompt for one instruction."""
    candidate_list = list(candidates)
    field_list = list(fields)
    template = load_prompt("steps")
    return render_prompt(
        template,
        {
            "INSTRUCTION": instruction.strip() or "(none)",
            "CORRECTION": correction.strip() or "(none)",
            "CANDIDATES": format_candidates(candidate_list),
            "CANDIDATE_COUNT": str(len(candidate_list)),
            "FIELDS": "\n".join(f"- {name}" for name in field_list)
            or "(no fields)",
            "IMAGE_WIDTH": str(image_width),
            "IMAGE_HEIGHT": str(image_height),
        },
    )


def plan_step(
    client: GeminiClient,
    capture: Capture,
    candidates: Sequence[UIACandidate],
    fields: Sequence[str],
    instruction: str,
    config: GeminiConfig,
    *,
    correction: str = "",
) -> StepPlan:
    """Ask Gemini once what the instruction means on this screen."""
    image = prepare_image(capture.image, config.max_image_dimension)
    prompt = build_step_prompt(
        instruction,
        fields,
        candidates,
        image.width,
        image.height,
        correction,
    )
    response = client.generate_json(prompt, image)
    return parse_step_plan(response.data, candidates=candidates, fields=fields)


__all__ = [
    "BUTTON_TYPES",
    "EDITABLE_TYPES",
    "PlannedFill",
    "StepPlan",
    "StepPlanError",
    "StepUnsupported",
    "build_step_prompt",
    "match_label",
    "parse_step_plan",
    "plan_step",
    "suggestions",
]
