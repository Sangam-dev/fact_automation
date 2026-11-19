"""Pre-save and post-save Gemini checks (Segment 6).

The prompts live in ``config/prompts``. This module builds them, validates the
model's JSON strictly, and applies deterministic cross-checks so a response
that contradicts itself can never approve a save.
"""

from __future__ import annotations

import json
from typing import Callable, Sequence

from pydantic import ValidationError

from core.config import GeminiConfig
from desktop.screenshots.capture import Capture
from ai.validators.models import (
    FieldCheck,
    PostSaveResponse,
    PostSaveVerdict,
    PreSaveResponse,
    PreSaveVerdict,
    VerificationError,
)
from desktop.screenshots.images import prepare_image
from ai.providers.gemini import AIResponse, GeminiClient
from ai.validators.models import FieldSpec
from ai.prompts.loader import load_prompt, render_prompt

_OnAttempt = Callable[[int], None] | None


def _normalise(text: str) -> str:
    return " ".join(str(text).split())


def _expected_matches(check_value: str, expected: str, field_type: str) -> bool:
    """Compare the model's echoed expected value with the real one."""
    if field_type == "number":
        try:
            return abs(
                float(check_value.replace(",", ""))
                - float(expected.replace(",", ""))
            ) < 1e-9
        except ValueError:
            pass
    return _normalise(check_value) == _normalise(expected)


def _format_fields(
    entries: Sequence[FieldSpec], values: dict[str, str]
) -> str:
    lines: list[str] = []
    for entry in entries:
        expected = values.get(entry.key, "")
        lines.append(
            f"- key={json.dumps(entry.key, ensure_ascii=False)} | "
            f"field_id={entry.field_id} | type={entry.type} | "
            f"expected={json.dumps(expected, ensure_ascii=False)}"
        )
    return "\n".join(lines) if lines else "(no mapped fields)"


def build_pre_save_prompt(
    entries: Sequence[FieldSpec],
    values: dict[str, str],
    image_width: int,
    image_height: int,
) -> str:
    """Render the pre-save prompt for one filled form."""
    return render_prompt(
        load_prompt("pre_save"),
        {
            "FIELDS": _format_fields(entries, values),
            "IMAGE_WIDTH": str(image_width),
            "IMAGE_HEIGHT": str(image_height),
        },
    )


def build_post_save_prompt(
    entries: Sequence[FieldSpec],
    values: dict[str, str],
    pre_size: tuple[int, int],
    post_size: tuple[int, int],
) -> str:
    """Render the post-save prompt for a before/after screenshot pair."""
    return render_prompt(
        load_prompt("post_save"),
        {
            "FIELDS": _format_fields(entries, values),
            "PRE_WIDTH": str(pre_size[0]),
            "PRE_HEIGHT": str(pre_size[1]),
            "POST_WIDTH": str(post_size[0]),
            "POST_HEIGHT": str(post_size[1]),
        },
    )


def _resolve_check(
    check: FieldCheck,
    *,
    entries: Sequence[FieldSpec],
) -> FieldSpec:
    """Match one field check to its mapping entry, rejecting ambiguity."""
    by_field = {entry.field_id: entry for entry in entries}
    by_key = {entry.key: entry for entry in entries}

    entry: FieldSpec | None = None
    if check.field_id:
        entry = by_field.get(check.field_id)
    if entry is None and check.key:
        entry = by_key.get(check.key)
    if entry is None:
        raise VerificationError(
            "the model checked a field that is not declared: "
            f"field_id={check.field_id!r} key={check.key!r}"
        )
    if check.field_id and check.field_id != entry.field_id:
        raise VerificationError(
            f"field check {check.field_id!r} claims key {check.key!r}, "
            f"but that key is mapped to {entry.field_id!r}"
        )
    if check.key and check.key != entry.key:
        raise VerificationError(
            f"field check for {check.field_id!r} claims key "
            f"{check.key!r}, but the declaration says {entry.key!r}"
        )
    return entry


def parse_pre_save(
    data: dict,
    *,
    entries: Sequence[FieldSpec],
    values: dict[str, str],
    prompt: str = "",
    ai_log: str | None = None,
) -> PreSaveVerdict:
    """Validate a pre-save response and apply cross-field checks."""
    try:
        response = PreSaveResponse.model_validate(data)
    except ValidationError as exc:
        raise VerificationError(
            f"the pre-save response does not match the schema: {exc}"
        ) from exc

    issues = list(response.issues)
    seen: set[str] = set()
    checks_ok = True

    for check in response.field_checks:
        entry = _resolve_check(check, entries=entries)
        if entry.field_id in seen:
            raise VerificationError(
                f"the pre-save response checks field {entry.field_id!r} twice"
            )
        seen.add(entry.field_id)

        expected = values.get(entry.key, "")
        if check.expected and not _expected_matches(
            check.expected, expected, entry.type
        ):
            issues.append(
                f"{entry.key}: the model compared against "
                f"{check.expected!r}, but the row value is {expected!r}"
            )
        if not check.ok:
            checks_ok = False
            issues.append(
                f"{entry.key}: expected {expected!r}, "
                f"observed {check.observed!r}"
                + (f" ({check.note})" if check.note else "")
            )

    missing = [
        entry.key for entry in entries if entry.field_id not in seen
    ]
    if missing:
        checks_ok = False
        issues.append(
            "the model did not check: " + ", ".join(repr(name) for name in missing)
        )

    if response.approved and not checks_ok:
        issues.append(
            "the model approved the form even though a field check failed"
        )

    approved = response.approved and checks_ok and not issues
    return PreSaveVerdict(
        approved=approved,
        response=response,
        issues=tuple(dict.fromkeys(issues)),
        prompt=prompt,
        ai_log=ai_log,
    )


def parse_post_save(
    data: dict,
    *,
    prompt: str = "",
    ai_log: str | None = None,
) -> PostSaveVerdict:
    """Validate a post-save response and apply cross-field checks."""
    try:
        response = PostSaveResponse.model_validate(data)
    except ValidationError as exc:
        raise VerificationError(
            f"the post-save response does not match the schema: {exc}"
        ) from exc

    issues = list(response.issues)
    if response.approved and not response.saved:
        issues.append(
            "the model approved a save it reported as not saved"
        )
    if response.saved and not response.approved and not issues:
        issues.append("the model reported a save but did not approve it")

    approved = response.approved and response.saved and not issues
    return PostSaveVerdict(
        approved=approved,
        saved=response.saved,
        response=response,
        issues=tuple(dict.fromkeys(issues)),
        prompt=prompt,
        ai_log=ai_log,
    )


def _log_path(ai: AIResponse) -> str | None:
    return str(ai.log_path) if ai.log_path is not None else None


def check_pre_save(
    client: GeminiClient,
    capture: Capture,
    entries: Sequence[FieldSpec],
    values: dict[str, str],
    config: GeminiConfig,
    *,
    on_attempt: _OnAttempt = None,
) -> PreSaveVerdict:
    """Send the filled-form screenshot to Gemini and parse the verdict."""
    image = prepare_image(capture.image, config.max_image_dimension)
    prompt = build_pre_save_prompt(
        entries, values, image.width, image.height
    )
    ai = client.generate_json(prompt, image, on_attempt=on_attempt)
    return parse_pre_save(
        ai.data,
        entries=entries,
        values=values,
        prompt=prompt,
        ai_log=_log_path(ai),
    )


def check_post_save(
    client: GeminiClient,
    pre: Capture,
    post: Capture,
    entries: Sequence[FieldSpec],
    values: dict[str, str],
    config: GeminiConfig,
    *,
    on_attempt: _OnAttempt = None,
) -> PostSaveVerdict:
    """Send the before/after screenshots to Gemini and parse the verdict."""
    pre_image = prepare_image(pre.image, config.max_image_dimension)
    post_image = prepare_image(post.image, config.max_image_dimension)
    prompt = build_post_save_prompt(
        entries, values, pre_image.size, post_image.size
    )
    ai = client.generate_json_images(
        prompt, [pre_image, post_image], on_attempt=on_attempt
    )
    return parse_post_save(
        ai.data,
        prompt=prompt,
        ai_log=_log_path(ai),
    )


__all__ = [
    "build_post_save_prompt",
    "build_pre_save_prompt",
    "check_post_save",
    "check_pre_save",
    "parse_post_save",
    "parse_pre_save",
]
