"""Compiled form map: the on-screen targets one FACT form needs.

A form map answers a single question for every business value: *which UIA
control on which window carries it, how is it written, and how is it read
back*. It is compiled from two inputs:

* a curated **source** spec (``config/forms/sale_order.source.json``) that a
  human writes from the screenshot: business key, screen label, and the
  automation id that identifies the control;
* one or more **UIA inspection dumps** (``scripts/uia/*.json``) captured from
  the live window.

Compiling resolves every target against the dumps, so a map that names a
control the application no longer has fails at build time instead of halfway
through typing a document. The dumps also record what the build cannot guess:
whether a field is enabled right now, which fields are derived (read-only
values the application fills in), and which controls are nested duplicates
whose automation ids are unstable.

Nothing here touches the desktop; it is pure JSON in, JSON out.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SCHEMA_SOURCE = "form-map-source/1"
SCHEMA = "form-map/1"

FieldKind = Literal["text", "number", "lookup", "date", "combo", "derived", "readonly"]

#: Kinds whose value the application produces itself; writing them would be a
#: bug, but reading them back proves the lookup actually took.
DERIVED_KINDS = frozenset({"derived", "readonly"})

_WHITESPACE = re.compile(r"\s+")
_CELL_LABEL = re.compile(r"^(?P<column>.+?)\s+row\s+(?P<index>\d+)$")


class FormMapError(RuntimeError):
    """The source spec and the captured window disagree."""


def normalise(text: str) -> str:
    """Collapse whitespace so header labels match despite Xtra padding."""
    return _WHITESPACE.sub(" ", str(text)).strip().casefold()


class Rect(BaseModel):
    model_config = ConfigDict(extra="ignore")

    left: int
    top: int
    right: int
    bottom: int


class Resolved(BaseModel):
    """Where one target was found in one capture."""

    model_config = ConfigDict(extra="forbid")

    capture: str
    path: str
    control_type: str
    inner_path: str | None = None
    writable: bool = False
    enabled: bool
    value: str = ""
    rect: Rect | None = None


class FieldSpec(BaseModel):
    """One header field, or a read-only value used as evidence."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str = ""
    automation_id: str
    kind: FieldKind = "text"
    required: bool = False
    write: Literal["set_edit_text", "type", "select"] = "type"
    commit: Literal["tab", "enter", "none"] = "tab"
    resolved: dict[str, Resolved] = Field(default_factory=dict)

    @property
    def derived(self) -> bool:
        return self.kind in DERIVED_KINDS


class GridColumnSpec(BaseModel):
    """One product-grid column, addressed by header label + row index."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: FieldKind = "text"
    write: Literal["set_edit_text", "type", "select"] = "type"
    commit: Literal["tab", "enter", "none"] = "tab"
    header_label: str = ""
    #: capture name -> path of the cell in row 0, when the grid exposes it
    resolved_cells: dict[str, str] = Field(default_factory=dict)

    @property
    def derived(self) -> bool:
        return self.kind in DERIVED_KINDS


class GridSpec(BaseModel):
    """The line-item table: cells are named ``<column> row <index>``."""

    model_config = ConfigDict(extra="forbid")

    automation_id: str
    row_container: str = "Row {row}"
    cell_name: str = "{column} row {index}"
    #: visual row number of UIA row index 0 (the form starts at Sl# 1)
    row_index_base: int = 1
    columns: list[GridColumnSpec] = Field(default_factory=list)
    #: capture name -> column order the grid exposed, including off-screen ones
    columns_seen: dict[str, list[str]] = Field(default_factory=dict)
    #: capture name -> whether the grid's cell set changes with horizontal scroll
    virtualized: dict[str, bool] = Field(default_factory=dict)
    resolved: dict[str, Resolved] = Field(default_factory=dict)

    def column(self, name: str) -> GridColumnSpec:
        wanted = normalise(name)
        for column in self.columns:
            if normalise(column.name) == wanted or normalise(column.header_label) == wanted:
                return column
        raise FormMapError(f"grid has no column {name!r}")


class ActionsSpec(BaseModel):
    """Named buttons, resolved per capture like fields."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    save_new: str
    save_close: str | None = None
    #: named ``copy`` in the source spec; ``BaseModel.copy`` is why it is renamed
    copy_button: str | None = Field(default=None, alias="copy")
    cancel: str | None = None
    resolved: dict[str, dict[str, Resolved]] = Field(default_factory=dict)


class CaptureInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    file: str
    captured_at: str = ""
    window_title: str = ""
    hwnd: int | None = None
    nodes: int = 0


class FormMap(BaseModel):
    """The compiled, validated description of one form."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal["form-map/1"] = Field(
        default=SCHEMA, validation_alias="schema", serialization_alias="schema"
    )
    form_id: str
    title_contains: str
    process: str = ""
    window_automation_id: str = ""
    status_text: FieldSpec | None = None
    header: list[FieldSpec] = Field(default_factory=list)
    grid: GridSpec | None = None
    actions: ActionsSpec | None = None
    captures: list[CaptureInfo] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def field(self, key: str) -> FieldSpec:
        for spec in self.header:
            if spec.key == key:
                return spec
        if self.status_text is not None and self.status_text.key == key:
            return self.status_text
        raise FormMapError(f"form {self.form_id!r} has no field {key!r}")

    def writable_fields(self) -> list[FieldSpec]:
        return [spec for spec in self.header if not spec.derived]


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FormMapError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FormMapError(f"invalid JSON in {path}: {exc}") from exc


def load_dump(path: str | Path) -> dict[str, Any]:
    dump = _read_json(Path(path))
    schema = dump.get("schema")
    if schema != "uia-inspector/1":
        raise FormMapError(f"{path} is not a uia-inspector/1 dump (schema={schema!r})")
    return dump


def load_form_map(path: str | Path) -> FormMap:
    try:
        return FormMap.model_validate(_read_json(Path(path)))
    except ValidationError as exc:
        raise FormMapError(f"invalid form map {path}: {exc}") from exc


# --------------------------------------------------------------------------
# Tree helpers
# --------------------------------------------------------------------------


def iter_nodes(node: dict[str, Any]) -> Iterable[tuple[int, dict[str, Any]]]:
    """Pre-order walk yielding ``(depth, node)``."""
    stack: list[tuple[int, dict[str, Any]]] = [(0, node)]
    while stack:
        depth, current = stack.pop()
        yield depth, current
        children = current.get("children") or []
        for child in reversed(children):
            stack.append((depth + 1, child))


def _window(dump: dict[str, Any]) -> dict[str, Any]:
    windows = dump.get("windows") or []
    if not windows:
        raise FormMapError("dump contains no windows")
    node = windows[0].get("uia")
    if not isinstance(node, dict):
        raise FormMapError("dump window has no uia tree")
    return node


def _unique_by_automation_id(dump: dict[str, Any], automation_id: str) -> dict[str, Any]:
    hits = [
        node
        for _, node in iter_nodes(_window(dump))
        if str(node.get("automation_id") or "").strip() == automation_id
    ]
    if not hits:
        raise FormMapError(f"control {automation_id!r} is not in the captured window")
    if len(hits) > 1:
        paths = ", ".join(str(node.get("path")) for node in hits)
        raise FormMapError(
            f"control {automation_id!r} is ambiguous: {len(hits)} matches at {paths}; "
            "target a unique ancestor automation id instead"
        )
    return hits[0]


def _inner_edit(node: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
    """The control that actually holds text, and how deep it sits.

    Xtra editors nest a real ``Edit`` inside a wrapper that reuses the same
    automation id, and the innermost child often has a numeric id that changes
    between application builds. The wrapper is the stable handle; the inner
    edit is what receives keystrokes.
    """
    best: tuple[int, dict[str, Any]] | None = None
    for depth, candidate in iter_nodes(node):
        if candidate.get("control_type") != "Edit" or not candidate.get("editable"):
            continue
        if best is None or depth > best[0]:
            best = (depth, candidate)
    return best


def _rect(node: dict[str, Any]) -> Rect | None:
    raw = node.get("rectangle") or {}
    try:
        rect = Rect(
            left=int(raw["left"]),
            top=int(raw["top"]),
            right=int(raw["right"]),
            bottom=int(raw["bottom"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if rect.right <= rect.left or rect.bottom <= rect.top:
        return None
    return rect


def _resolve(capture: str, node: dict[str, Any]) -> Resolved:
    inner = _inner_edit(node)
    # Wrapper panes carry no value; the text lives on the inner Edit.
    holder = inner[1] if inner else node
    return Resolved(
        capture=capture,
        path=str(node.get("path") or ""),
        control_type=str(node.get("control_type") or ""),
        inner_path=str(inner[1].get("path")) if inner else None,
        writable=inner is not None,
        enabled=bool(node.get("enabled", True)),
        value=str(holder.get("value") or ""),
        rect=_rect(node),
    )


# --------------------------------------------------------------------------
# Compilation
# --------------------------------------------------------------------------


def _capture_name(path: Path) -> str:
    return path.stem


def _compile_field(
    spec: FieldSpec,
    dumps: dict[str, dict[str, Any]],
    warnings: list[str],
) -> None:
    for capture, dump in dumps.items():
        node = _unique_by_automation_id(dump, spec.automation_id)
        resolved = _resolve(capture, node)
        spec.resolved[capture] = resolved

        if spec.kind == "readonly":
            # Asserted by the spec author (status text, order number): a UIA
            # Edit can look editable while the form treats it as output.
            continue
        if spec.derived:
            if resolved.enabled:
                warnings.append(
                    f"{spec.key}: {spec.automation_id} is marked derived but is "
                    f"enabled in {capture}; confirm the application owns this value"
                )
        elif not resolved.writable:
            warnings.append(
                f"{spec.key}: {spec.automation_id} has no editable Edit control in "
                f"{capture}; it cannot be written yet"
            )
        elif not resolved.enabled:
            warnings.append(
                f"{spec.key}: {spec.automation_id} is disabled in {capture}; "
                "another field probably enables it first"
            )


def _compile_grid(
    spec: GridSpec,
    dumps: dict[str, dict[str, Any]],
    warnings: list[str],
) -> None:
    cell_columns: dict[str, set[str]] = {}
    for capture, dump in dumps.items():
        grid = _unique_by_automation_id(dump, spec.automation_id)
        spec.resolved[capture] = _resolve(capture, grid)

        headers: list[str] = []
        cells: dict[str, str] = {}
        row_containers: list[str] = []
        for _, node in iter_nodes(grid):
            name = str(node.get("name") or "")
            control_type = node.get("control_type")
            if control_type == "Header":
                headers.append(name)
            elif control_type == "Custom" and re.match(r"^Row\s+\d+$", name):
                row_containers.append(name)
            elif control_type == "DataItem":
                match = _CELL_LABEL.match(_WHITESPACE.sub(" ", name))
                if match:
                    cells.setdefault(normalise(match.group("column")), str(node.get("path")))

        spec.columns_seen[capture] = headers
        cell_columns[capture] = set(cells)
        if not headers:
            warnings.append(f"grid {spec.automation_id}: no column headers in {capture}")
        if row_containers:
            spec.row_container = "Row {row}"

    # A column is virtualized when a capture exposes the header but not the
    # cell, which is what happens once the grid is scrolled horizontally.
    header_columns = {
        normalise(header) for headers in spec.columns_seen.values() for header in headers
    }
    virtualized = any(
        header_columns - {column for column in columns}
        for columns in cell_columns.values()
    )
    spec.virtualized = {capture: virtualized for capture in dumps}

    for column in spec.columns:
        wanted = normalise(column.name)
        labels = {
            header
            for headers in spec.columns_seen.values()
            for header in headers
            if normalise(header) == wanted
        }
        if not labels:
            raise FormMapError(f"grid column {column.name!r} is not a header in any capture")
        column.header_label = labels.pop()
        for capture, dump in dumps.items():
            if wanted in cell_columns[capture]:
                grid = _unique_by_automation_id(dump, spec.automation_id)
                for _, node in iter_nodes(grid):
                    if node.get("control_type") != "DataItem":
                        continue
                    match = _CELL_LABEL.match(_WHITESPACE.sub(" ", str(node.get("name") or "")))
                    if match and normalise(match.group("column")) == wanted:
                        column.resolved_cells[capture] = str(node.get("path"))
                        break
        missing = [capture for capture in dumps if capture not in column.resolved_cells]
        if missing:
            warnings.append(
                f"grid column {column.name!r}: cell not exposed in "
                + ", ".join(missing)
                + " (scrolled out of view)"
            )


def compile_form_map(source: dict[str, Any], dumps: dict[str, dict[str, Any]]) -> FormMap:
    """Resolve a curated source spec against one or more captured windows."""
    warnings: list[str] = []

    raw = dict(source)
    raw.pop("schema", None)
    raw["schema_version"] = SCHEMA
    for entry in raw.get("header", []):
        entry["resolved"] = {}
    if raw.get("status_text"):
        raw["status_text"]["resolved"] = {}
    if raw.get("grid"):
        raw["grid"]["columns_seen"] = {}
        raw["grid"]["virtualized"] = {}
        raw["grid"]["resolved"] = {}
    if raw.get("actions"):
        raw["actions"]["resolved"] = {}

    captures = [
        CaptureInfo(
            name=name,
            file=str(dump.get("_file", name)),
            captured_at=str(dump.get("captured_at") or ""),
            window_title=str((dump.get("windows") or [{}])[0].get("title") or ""),
            hwnd=(dump.get("windows") or [{}])[0].get("hwnd"),
            nodes=int((dump.get("summary") or {}).get("nodes") or 0),
        )
        for name, dump in dumps.items()
    ]

    try:
        form = FormMap.model_validate(raw)
    except ValidationError as exc:
        raise FormMapError(f"invalid source spec: {exc}") from exc

    form.captures = captures
    for spec in form.header:
        _compile_field(spec, dumps, warnings)
    if form.status_text is not None:
        _compile_field(form.status_text, dumps, warnings)
    if form.grid is not None:
        _compile_grid(form.grid, dumps, warnings)
    if form.actions is not None:
        for action in ("save_new", "save_close", "copy_button", "cancel"):
            automation_id = getattr(form.actions, action)
            if not automation_id:
                continue
            form.actions.resolved[action] = {}
            for capture, dump in dumps.items():
                form.actions.resolved[action][capture] = _resolve(
                    capture, _unique_by_automation_id(dump, automation_id)
                )

    form.warnings = warnings
    return form


def compile_file(source_path: str | Path, dump_paths: list[str | Path]) -> FormMap:
    source = _read_json(Path(source_path))
    dumps: dict[str, dict[str, Any]] = {}
    for path in dump_paths:
        path = Path(path)
        dump = load_dump(path)
        dump["_file"] = path.as_posix()
        name = _capture_name(path)
        if name in dumps:
            raise FormMapError(f"two dumps share the capture name {name!r}")
        dumps[name] = dump
    return compile_form_map(source, dumps)


def dump_json(form: FormMap) -> str:
    """Stable JSON text, so ``--check`` can compare byte for byte."""
    return json.dumps(form.model_dump(by_alias=True, mode="json"), indent=2) + "\n"
