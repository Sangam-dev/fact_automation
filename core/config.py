"""Typed configuration for the desktop automation foundation.

Configuration lives in JSON (see ``config/examples/``). Secrets never live in
JSON: the Gemini API key is read from the environment, normally via ``.env`` in
the project root.

The model describes a *workflow* against one application window: which data
fields are filled into which controls, an ordered list of click/fill steps, and
the save/confirmation behaviour. It deliberately has no notion of a workbook or
a row; business values are supplied at run time.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from execution.actions.models import FillStep, FormStep

FieldType = Literal["text", "number"]


class ConfigError(RuntimeError):
    """Raised when configuration cannot be loaded or validated."""


def project_root() -> Path:
    """Return the project root (the directory containing ``main.py``)."""
    return Path(__file__).resolve().parents[1]


def resolve_path(value: str | Path) -> Path:
    """Resolve a config path relative to the project root."""
    path = Path(value)
    return path if path.is_absolute() else project_root() / path


class AppConfig(BaseModel):
    """Target application selection."""

    model_config = ConfigDict(extra="forbid")

    title_contains: str = ""
    process_name: str | None = None
    require_foreground: bool = True
    # Keep the target app in the always-on-top band, above our chat window.
    # Every window the app owns is pinned, including ones it opens later, so
    # pinning never buries a form the app itself puts in front.
    pin_window: bool = True

    @model_validator(mode="after")
    def _must_have_matcher(self) -> "AppConfig":
        if not self.title_contains and not self.process_name:
            raise ValueError("app.title_contains or app.process_name must be set")
        return self


class FieldConfig(BaseModel):
    """One business data field -> form control mapping.

    ``name`` is the key in the record values passed to the runner; ``field`` is
    the UIA label used when a step does not override it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    field: str
    type: FieldType = "text"

    @field_validator("name", "field")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("field names must not be empty")
        return value


class ActionsConfig(BaseModel):
    """Buttons and timing for the target form."""

    model_config = ConfigDict(extra="forbid")

    save_button: str
    new_record_button: str | None = None
    wait_after_save_seconds: float = Field(default=1.5, ge=0)


class WorkflowConfig(BaseModel):
    """An ordered click/fill plan for one screen.

    The steps exist because a field map alone cannot express the *order* of
    work: a form that needs "Add" clicked before its fields exist, or a button
    pressed between two fills, cannot be described as name -> control pairs.
    """

    model_config = ConfigDict(extra="forbid")

    steps: list[FormStep] = Field(min_length=1)
    retry_on_failure: bool = False
    pause_after_click_seconds: float = Field(default=0.5, ge=0)


#: The model used when neither ``.env`` nor the config file names one.
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"


def get_gemini_model() -> str:
    """The model to use: ``GEMINI_MODEL`` from the environment, else the default.

    Read where the model is *chosen*, not where it is used, so a config that is
    generated or proved records the model that did the work. Switching models -
    a quota change, a cheaper one - is then one line in ``.env``, not an edit to
    every saved automation.
    """
    return os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL


class GeminiConfig(BaseModel):
    """Gemini model and cost guards."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(default_factory=get_gemini_model)
    max_image_dimension: int = Field(default=1600, ge=200)
    timeout_seconds: int = Field(default=60, ge=5)
    max_retries: int = Field(default=3, ge=0)
    max_ai_calls: int = Field(default=50, ge=0)
    min_layout_confidence: float = Field(default=0.6, ge=0, le=1)
    min_mapping_confidence: float = Field(default=0.6, ge=0, le=1)

    @field_validator("model")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("gemini.model must not be empty")
        return value


class NormalizedRegion(BaseModel):
    """A rectangle in 0-1000 normalized window coordinates."""

    model_config = ConfigDict(extra="forbid")

    x1: int = Field(ge=0, le=1000)
    y1: int = Field(ge=0, le=1000)
    x2: int = Field(ge=0, le=1000)
    y2: int = Field(ge=0, le=1000)

    @model_validator(mode="after")
    def _ordered(self) -> "NormalizedRegion":
        if self.x2 <= self.x1:
            raise ValueError("normalized region x2 must be greater than x1")
        if self.y2 <= self.y1:
            raise ValueError("normalized region y2 must be greater than y1")
        return self

    def to_pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        """Convert to a PIL crop box, clamped to the image bounds."""
        left = max(0, min(width - 1, round(width * self.x1 / 1000)))
        top = max(0, min(height - 1, round(height * self.y1 / 1000)))
        right = max(left + 1, min(width, round(width * self.x2 / 1000)))
        bottom = max(top + 1, min(height, round(height * self.y2 / 1000)))
        return left, top, right, bottom


def _compile_pattern(value: str) -> None:
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(
            f"invalid regular expression {value!r}: {exc}"
        ) from exc


class ConfirmationConfig(BaseModel):
    """Where and how to read positive/negative Save evidence.

    ``verifier`` picks the evidence: ``text`` sends the screen to the model and
    matches ``success_pattern``; ``row`` stays local and looks for the values
    from ``footprint`` appearing on the screen after Save, which is what apps
    that show no success message actually offer; ``none`` checks nothing and
    records every Save as done.
    """

    model_config = ConfigDict(extra="forbid")

    success_region: NormalizedRegion | None = None
    error_region: NormalizedRegion | None = None
    success_pattern: str = ""
    error_patterns: list[str] = Field(default_factory=list)
    verifier: Literal["text", "row", "none"] = "text"
    footprint: list[str] = Field(default_factory=list)

    @field_validator("footprint")
    @classmethod
    def _clean_footprint(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            name = str(value).strip()
            if name and name not in cleaned:
                cleaned.append(name)
        return cleaned

    @field_validator("success_pattern")
    @classmethod
    def _valid_success_pattern(cls, value: str) -> str:
        value = value.strip()
        if value:
            _compile_pattern(value)
        return value

    @field_validator("error_patterns")
    @classmethod
    def _valid_error_patterns(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            value = value.strip()
            if not value:
                raise ValueError("error_patterns must not contain empty patterns")
            _compile_pattern(value)
            cleaned.append(value)
        return cleaned


class ScrollConfig(BaseModel):
    """Reaching a newly saved record that landed below the fold.

    Built for apps where the list scrolls while the Add button and the entry
    form stay put: the search only moves down from the current position, and
    nothing is scrolled back afterwards. ``enabled: false`` restores the
    single-read behaviour, where a record below the fold is simply not seen.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_screens: int = Field(default=6, ge=1, le=60)
    settle_seconds: float = Field(default=0.4, ge=0, le=5)
    wheel_notches: int = Field(default=5, ge=1, le=50)


class SafetyConfig(BaseModel):
    """Input lock and failure policy."""

    model_config = ConfigDict(extra="forbid")

    stop_hotkey: str = "alt+t"
    pause_hotkey: str = "alt+p"
    resume_hotkey: str = "alt+r"
    block_external_input: bool = True
    max_attempts: int = Field(default=3, ge=1)
    action_delay_seconds: float = Field(default=0.15, ge=0)
    stop_on_failure: bool = True

    @field_validator("stop_hotkey", "pause_hotkey", "resume_hotkey")
    @classmethod
    def _normalise_hotkey(cls, value: str) -> str:
        value = value.strip().lower()
        if not value:
            raise ValueError("hotkeys must not be empty")
        return value

    @model_validator(mode="after")
    def _distinct_hotkeys(self) -> "SafetyConfig":
        values = [self.stop_hotkey, self.pause_hotkey, self.resume_hotkey]
        if len(values) != len(set(values)):  # list holds all values but the set removes duplicates
            raise ValueError(
                "stop, pause, and resume must use different hotkeys: "
                + ", ".join(values)
            )
        return self


class AgentConfig(BaseModel):
    """Top-level configuration."""

    model_config = ConfigDict(extra="forbid")

    app: AppConfig
    fields: list[FieldConfig] = Field(default_factory=list)
    actions: ActionsConfig
    workflow: WorkflowConfig | None = None
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    confirmation: ConfirmationConfig = Field(default_factory=ConfirmationConfig)
    scrolling: ScrollConfig = Field(default_factory=ScrollConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)

    @model_validator(mode="after")
    def _fields_are_unique(self) -> "AgentConfig":
        names = [item.name for item in self.fields]
        fields = [item.field for item in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("each data field may be declared only once")
        if len(fields) != len(set(fields)):
            raise ValueError("each form field may be mapped from only one data field")
        return self

    @model_validator(mode="after")
    def _steps_fill_declared_fields(self) -> "AgentConfig":
        if self.workflow is None:
            return self
        declared = {item.name for item in self.fields}
        missing = [
            step.key
            for step in self.workflow.steps
            if isinstance(step, FillStep) and step.key not in declared
        ]
        if missing:
            raise ValueError(
                "workflow step(s) fill field(s) that `fields` does not declare: "
                + ", ".join(repr(name) for name in dict.fromkeys(missing))
            )
        return self


def load_env(env_path: str | Path | None = None) -> Path:
    """Load ``.env`` without overriding already-set environment variables."""
    path = Path(env_path) if env_path is not None else project_root() / ".env"
    if not path.is_absolute():
        path = project_root() / path
    if path.is_file():
        load_dotenv(path, override=False)
    return path


def get_gemini_api_key() -> str:
    """Return the Gemini API key from the environment, or an empty string."""
    return os.getenv("GEMINI_API_KEY", "").strip()


def load_config(path: str | Path) -> AgentConfig:
    """Load and validate a JSON configuration file."""
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = project_root() / config_path

    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {config_path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {config_path}: {exc}") from exc

    try:
        config = AgentConfig.model_validate(data)
    except Exception as exc:  # pydantic ValidationError and nested ValueErrors
        raise ConfigError(f"invalid config {config_path}: {exc}") from exc

    # The environment outranks the file. A saved config keeps the model it was
    # written with, which is right for the record but wrong as a policy: it
    # would pin every automation to a model that may be exhausted by noon.
    override = os.getenv("GEMINI_MODEL", "").strip()
    if override:
        config.gemini.model = override
    return config
