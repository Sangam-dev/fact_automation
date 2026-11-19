"""Designed, deterministic pieces that can be appended to an automation.

The wizard authors a plan with AI and proves it; this module is the other half
of the story - the parts that are *designed*, not discovered. The Sale Order
header is nine known fields in a known order on a known screen. Typing them from
a model's reading of a screenshot every time would be slower, costs money, and
could drift; appending a designed segment gives the same steps every run.

A segment is almost an :class:`~core.automation.model.Automation`: steps, each
with the screen it belongs on, plus the fields it types into. Appending one to a
registered automation concatenates the steps, merges the fields, and marks the
plan unproven - because its new tail has not run on this application yet.

The library is generated (``scripts/build_segments.py``) from the compiled form
map and the curated Sale Order plan, so a label that changes on screen changes
here too.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.automation.model import Automation, AutomationError, AutomationStep
from core.automation.registry import save as save_automation
from core.config import FieldConfig, project_root

SEGMENTS_SCHEMA = "segments/1"

#: How a segment records where its steps came from, in ``checkpoint.note``.
SEGMENT_NOTE = "segment:"

_STEP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$")


class SegmentError(RuntimeError):
    """The segment library cannot be read, or a segment cannot be appended."""


class Segment(BaseModel):
    """One designed, ordered piece of an automation."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = ""
    summary: str = ""
    source: str = ""
    fields: list[FieldConfig] = Field(default_factory=list)
    steps: list[AutomationStep] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> "Segment":
        if not self.id.strip():
            raise ValueError("a segment needs an id")
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"step id {step.id!r} is used twice in {self.id!r}")
            seen.add(step.id)
        declared = {item.name for item in self.fields}
        undeclared = [
            step.key
            for step in self.steps
            if step.action == "fill" and step.key not in declared
        ]
        if undeclared:
            raise ValueError(
                f"segment {self.id!r} fills field(s) it does not declare: "
                + ", ".join(repr(name) for name in dict.fromkeys(undeclared))
            )
        return self

    def state_ids(self) -> list[str]:
        """Every screen the segment's steps belong on, in first-use order."""
        return list(dict.fromkeys(step.checkpoint.state for step in self.steps))

    def describe(self) -> str:
        screens = ", ".join(self.state_ids())
        return f"{self.id}: {len(self.steps)} step(s) on {screens}"

    def validate_states(self, known: set[str] | list[str]) -> None:
        known_set = set(known)
        if not known_set:
            return
        unknown = [state for state in self.state_ids() if state not in known_set]
        if unknown:
            raise SegmentError(
                f"segment {self.id!r} expects state(s) the state table does not "
                f"define: {', '.join(unknown)}; run scripts/learn_states.py"
            )


class SegmentTable(BaseModel):
    """Every designed segment this project ships."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: str = Field(default=SEGMENTS_SCHEMA, alias="schema")
    generated_from: str = ""
    generated_at: str = ""
    segments: list[Segment] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> "SegmentTable":
        if self.schema_version != SEGMENTS_SCHEMA:
            raise ValueError(
                f"unsupported segment schema {self.schema_version!r}; "
                f"expected {SEGMENTS_SCHEMA!r}"
            )
        seen: set[str] = set()
        for segment in self.segments:
            if segment.id in seen:
                raise ValueError(f"segment id {segment.id!r} is declared twice")
            seen.add(segment.id)
        return self

    def ids(self) -> list[str]:
        return [segment.id for segment in self.segments]

    def get(self, segment_id: str) -> Segment:
        for segment in self.segments:
            if segment.id == segment_id:
                return segment
        raise SegmentError(
            f"there is no segment {segment_id!r}; available: "
            + (", ".join(self.ids()) or "(none)")
        )


def segment_table_path() -> Path:
    """Where the designed segments live."""
    return project_root() / "config" / "segments" / "fact_segments.json"


def load_segments(path: str | Path | None = None) -> SegmentTable:
    """Read the segment library, failing loudly on a shape this code cannot read."""
    file_path = Path(path) if path is not None else segment_table_path()
    if not file_path.is_absolute():
        file_path = project_root() / file_path
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SegmentError(
            f"no segment library at {file_path}; run "
            "`python scripts/build_segments.py` to build it"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SegmentError(f"cannot read {file_path}: {exc}") from exc
    try:
        return SegmentTable.model_validate(data)
    except Exception as exc:
        raise SegmentError(f"{file_path} is not a valid segment library: {exc}") from exc


def save_segments(table: SegmentTable, path: str | Path | None = None) -> Path:
    """Write the library atomically, so a crash cannot leave half of one."""
    file_path = Path(path) if path is not None else segment_table_path()
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


def _prefix(segment: Segment) -> str:
    """A short, stable prefix that keeps appended step ids unique."""
    tail = segment.id.rsplit(".", 1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", tail).strip("-").casefold()
    return cleaned[:16] or "seg"


def appended_steps(segment: Segment) -> list[AutomationStep]:
    """The segment's steps, renamed and marked with where they came from."""
    prefix = _prefix(segment)
    steps: list[AutomationStep] = []
    for step in segment.steps:
        copy = step.model_copy(deep=True)
        copy.id = f"{prefix}-{step.id}"
        if not _STEP_ID.match(copy.id):
            raise SegmentError(
                f"segment {segment.id!r} produces a step id that cannot be "
                f"stored: {copy.id!r}"
            )
        copy.checkpoint.note = f"{SEGMENT_NOTE}{segment.id}"
        steps.append(copy)
    return steps


def _merge_fields(
    automation: Automation, segment: Segment
) -> list[FieldConfig]:
    """The plan's fields plus the segment's, refusing contradictory mappings."""
    merged = list(automation.fields)
    by_name = {item.name: item for item in merged}
    for field in segment.fields:
        current = by_name.get(field.name)
        if current is None:
            merged.append(field.model_copy(deep=True))
            by_name[field.name] = field
            continue
        if current.field.casefold() != field.field.casefold():
            raise SegmentError(
                f"field {field.name!r} is already mapped to {current.field!r}, "
                f"but segment {segment.id!r} types it into {field.field!r}; "
                "the two would disagree about the same control"
            )
    return merged


def append_segment(
    automation: Automation,
    segment: Segment,
    *,
    known_states: set[str] | list[str] | None = None,
) -> Automation:
    """Return a new automation with the segment's steps after the existing ones.

    The original is not modified: a refused append must not leave a half-built
    plan anywhere.
    """
    if known_states is not None:
        segment.validate_states(known_states)

    appended_from = {
        step.checkpoint.note
        for step in automation.steps
        if step.checkpoint.note.startswith(SEGMENT_NOTE)
    }
    if f"{SEGMENT_NOTE}{segment.id}" in appended_from:
        raise SegmentError(
            f"{automation.name!r} already contains segment {segment.id!r}"
        )

    steps = list(automation.steps) + appended_steps(segment)
    fields = _merge_fields(automation, segment)

    data = automation.model_dump(by_alias=True)
    data["steps"] = [step.model_dump(by_alias=True) for step in steps]
    data["fields"] = [field.model_dump() for field in fields]
    data["verified"] = False
    try:
        return Automation.model_validate(data)
    except AutomationError as exc:  # pragma: no cover - validated above
        raise SegmentError(str(exc)) from exc


def library_summary(table: SegmentTable) -> list[str]:
    """One line per segment, for a person choosing what to append."""
    lines = []
    for segment in table.segments:
        screens = ", ".join(segment.state_ids())
        summary = f" - {segment.summary}" if segment.summary else ""
        lines.append(
            f"{segment.id} ({len(segment.steps)} step(s) on {screens}){summary}"
        )
    return lines


@dataclass(frozen=True)
class AppendResult:
    """What an append attempt produced."""

    status: str
    name: str
    segment: str
    message: str = ""
    path: Path | None = None
    automation: Automation | None = None
    steps_added: int = 0

    @property
    def ok(self) -> bool:
        return self.status in ("appended", "drafted")


def append_segment_to(
    name: str,
    segment_id: str,
    *,
    automations_root: str | Path | None = None,
    segments: SegmentTable | None = None,
    known_states: set[str] | list[str] | None = None,
    write: bool = True,
    save_to_registry=save_automation,
) -> AppendResult:
    """Append one designed segment to a registered automation."""
    from core.automation.registry import AutomationError as RegistryError
    from core.automation.registry import AutomationNotFound
    from core.automation.registry import load as load_automation

    try:
        automation = load_automation(name, automations_root)
    except (AutomationNotFound, RegistryError) as exc:
        return AppendResult("refused", name, segment_id, str(exc))

    try:
        table = segments if segments is not None else load_segments()
        segment = table.get(segment_id)
    except SegmentError as exc:
        return AppendResult("refused", name, segment_id, str(exc))

    try:
        updated = append_segment(automation, segment, known_states=known_states)
    except SegmentError as exc:
        return AppendResult("refused", name, segment_id, str(exc))

    path = None
    if write:
        try:
            path = save_to_registry(updated, automations_root)
        except (RegistryError, OSError) as exc:
            return AppendResult("failed", name, segment_id, str(exc))

    return AppendResult(
        status="appended" if write else "drafted",
        name=name,
        segment=segment_id,
        message=(
            f"{len(segment.steps)} step(s) added; the plan is no longer marked "
            "as proved, because its new steps have not run yet"
            if write
            else "dry run: nothing was written"
        ),
        path=path,
        automation=updated,
        steps_added=len(segment.steps),
    )


__all__ = [
    "SEGMENT_NOTE",
    "SEGMENTS_SCHEMA",
    "AppendResult",
    "Segment",
    "SegmentError",
    "SegmentTable",
    "append_segment",
    "append_segment_to",
    "appended_steps",
    "library_summary",
    "load_segments",
    "save_segments",
    "segment_table_path",
]
