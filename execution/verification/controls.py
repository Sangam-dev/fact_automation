"""Resolve named controls against live UIA and verify typed values.

This is the read-only half of verification: given a UIA inspection, find the
control a step names, compare a control's value with the expected value, and
compare before/after screenshots for pixel evidence. Nothing here clicks,
types, or knows where the data came from.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageChops

from desktop.types import EDITABLE_CONTROL_TYPES, Rect
from desktop.uia.uia import UIAError, UIAInspection
from desktop.windows.coordinates import rect_within


@dataclass(frozen=True)
class ResolvedTarget:
    """One field or button resolved against the live window."""

    key: str
    rect: Rect
    candidate_id: str | None
    source: str
    field_type: str = "text"


@dataclass(frozen=True)
class ResolvedLayout:
    """The live rectangles for one execution attempt."""

    fields: dict[str, ResolvedTarget]
    save: ResolvedTarget | None
    warnings: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class SaveEvidence:
    """The deterministic before/after comparison of the filled fields."""

    unchanged: bool
    compared: tuple[str, ...] = ()
    identical: tuple[str, ...] = ()
    detail: str = ""


def _normalise(text: str) -> str:
    return " ".join(str(text).split())


def _match_key(text: str) -> str:
    return " ".join(str(text).replace("&", " ").split()).casefold()


def _matches(candidate, label: str) -> bool:
    """True when *label* names the control by caption, label or automation id."""
    wanted = _match_key(label)
    if not wanted:
        return True
    name = str(getattr(candidate, "name", "") or "")
    automation_id = str(getattr(candidate, "automation_id", "") or "")
    display = str(getattr(candidate, "label", "") or name)
    return wanted in {
        _match_key(name),
        _match_key(automation_id),
        _match_key(display),
    }


def _inside(rect: Rect, window: Rect) -> bool:
    return rect_within(rect, window, tolerance=2)


def _pick_candidate(
    inspection: UIAInspection,
    *,
    candidate_id: str | None,
    label: str,
    control_types: set[str],
) -> tuple[ResolvedTarget | None, str | None]:
    """Find a live control by id (when the name agrees), then by name."""
    if not candidate_id and not label:
        return None, "has neither a UIA id nor a label"
    by_id = {item.candidate_id: item for item in inspection.candidates}
    window = inspection.window.rect

    candidate = by_id.get(candidate_id) if candidate_id else None
    warning: str | None = None
    if candidate is not None:
        name_ok = _matches(candidate, label)
        if name_ok and candidate.control_type in control_types and _inside(
            candidate.rect, window
        ):
            return (
                ResolvedTarget(
                    key="",
                    rect=candidate.rect,
                    candidate_id=candidate.candidate_id,
                    source="uia-id",
                ),
                None,
            )
        if label and not name_ok:
            warning = (
                f"UIA id {candidate_id} now names {candidate.name!r} instead of "
                f"{label!r}; ignoring it"
            )

    for item in inspection.candidates:
        if item.control_type not in control_types:
            continue
        if not _matches(item, label):
            continue
        if not _inside(item.rect, window):
            continue
        return (
            ResolvedTarget(
                key="",
                rect=item.rect,
                candidate_id=item.candidate_id,
                source="uia-name",
            ),
            warning,
        )
    return None, warning


def resolve_page_target(
    inspection: UIAInspection,
    *,
    label: str,
    control_types: set[str] | frozenset[str],
    candidate_id: str | None = None,
) -> tuple[ResolvedTarget | None, str | None]:
    """Find one live control by id (when its name agrees), then by name."""
    return _pick_candidate(
        inspection,
        candidate_id=candidate_id,
        label=label,
        control_types=set(control_types),
    )


def values_match(observed: str, expected: str, field_type: str) -> bool:
    """Compare a control's read-back value with the expected value."""
    if field_type == "number":
        try:
            return abs(
                float(str(observed).strip().replace(",", ""))
                - float(str(expected).strip().replace(",", ""))
            ) < 1e-9
        except ValueError:
            return False
    return _normalise(observed) == _normalise(expected)


def read_back(
    inspection: UIAInspection,
    layout: ResolvedLayout,
    values: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Read every live field value back; return ``(problems, warnings)``."""
    problems: list[str] = []
    warnings: list[str] = []
    for key, target in layout.fields.items():
        if target.candidate_id is None:
            continue
        try:
            observed = inspection.read_value(target.candidate_id)
        except UIAError as exc:
            warnings.append(
                f"{key}: could not read the value back ({exc}); "
                "relying on the screenshot check"
            )
            continue
        expected = values.get(key, "")
        if not values_match(observed, expected, target.field_type):
            problems.append(f"{key}: read back {observed!r}, expected {expected!r}")
    return problems, warnings


def _crop(capture, rect: Rect) -> Image.Image | None:
    """Crop a screen rectangle from a capture as grayscale."""
    scale_x = capture.scale_x or 1.0
    scale_y = capture.scale_y or 1.0
    left = round((rect.left - capture.origin[0]) / scale_x)
    top = round((rect.top - capture.origin[1]) / scale_y)
    right = round((rect.right - capture.origin[0]) / scale_x)
    bottom = round((rect.bottom - capture.origin[1]) / scale_y)

    left = max(0, min(capture.image.width - 1, left))
    top = max(0, min(capture.image.height - 1, top))
    right = max(left + 1, min(capture.image.width, right))
    bottom = max(top + 1, min(capture.image.height, bottom))
    if right <= left or bottom <= top:
        return None
    return capture.image.crop((left, top, right, bottom)).convert("L")


def _same_image(left: Image.Image, right: Image.Image, tolerance: int = 12) -> bool:
    if left.size != right.size:
        return False
    difference = ImageChops.difference(left, right)
    mask = difference.point(lambda value: 255 if value > tolerance else 0)
    return mask.getbbox() is None


def compare_saved_fields(
    pre,
    post,
    layout: ResolvedLayout,
    values: dict[str, str],
    tolerance: int = 12,
) -> SaveEvidence:
    """Detect whether the filled fields are pixel-identical after Save."""
    if pre.image.size != post.image.size:
        return SaveEvidence(
            unchanged=False,
            detail="the window size changed between captures",
        )

    compared: list[str] = []
    identical: list[str] = []
    for key, target in layout.fields.items():
        if not values.get(key, "").strip():
            continue
        before = _crop(pre, target.rect)
        after = _crop(post, target.rect)
        if before is None or after is None:
            continue
        compared.append(key)
        if _same_image(before, after, tolerance):
            identical.append(key)

    unchanged = bool(compared) and len(identical) == len(compared)
    if not compared:
        detail = "no non-empty field could be compared"
    elif unchanged:
        detail = "the filled fields are unchanged"
    else:
        detail = "the filled fields changed"
    return SaveEvidence(
        unchanged=unchanged,
        compared=tuple(compared),
        identical=tuple(identical),
        detail=detail,
    )


__all__ = [
    "EDITABLE_CONTROL_TYPES",
    "ResolvedLayout",
    "ResolvedTarget",
    "SaveEvidence",
    "compare_saved_fields",
    "read_back",
    "resolve_page_target",
    "values_match",
]
