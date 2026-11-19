"""Unit tests for configuration loading and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.config import (
    AgentConfig,
    ConfigError,
    ConfirmationConfig,
    load_config,
    project_root,
    resolve_path,
)


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_valid_config_loads(valid_config: dict, tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, valid_config))

    assert isinstance(config, AgentConfig)
    assert config.app.title_contains == "Test Form"
    assert config.workflow is None
    assert len(config.fields) == 2
    assert config.gemini.model == "gemini-3.6-flash"


def test_a_config_with_a_workflow_loads(valid_config: dict, tmp_path: Path) -> None:
    valid_config["workflow"] = {
        "steps": [
            {"action": "click", "button": "Add New"},
            {"action": "fill", "key": "name"},
        ]
    }

    config = load_config(_write(tmp_path, valid_config))

    assert config.workflow is not None
    assert len(config.workflow.steps) == 2


def test_the_example_config_is_valid() -> None:
    config = load_config("config/examples/workflow.example.json")

    assert config.workflow is not None
    assert config.actions.save_button == "Save Record"
    assert config.confirmation.verifier == "row"


def test_missing_config_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read config"):
        load_config(tmp_path / "does-not-exist.json")


def test_invalid_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ConfigError, match="invalid JSON"):
        load_config(path)


def test_duplicate_data_field_is_rejected(valid_config: dict, tmp_path: Path) -> None:
    valid_config["fields"].append(
        {"name": "name", "field": "Other Field", "type": "text"}
    )

    with pytest.raises(ConfigError, match="declared only once"):
        load_config(_write(tmp_path, valid_config))


def test_duplicate_form_field_is_rejected(valid_config: dict, tmp_path: Path) -> None:
    valid_config["fields"].append(
        {"name": "other", "field": "Amount", "type": "number"}
    )

    with pytest.raises(ConfigError, match="mapped from only one data field"):
        load_config(_write(tmp_path, valid_config))


def test_unknown_key_is_rejected(valid_config: dict, tmp_path: Path) -> None:
    valid_config["unexpected"] = True

    with pytest.raises(ConfigError, match="invalid config"):
        load_config(_write(tmp_path, valid_config))


def test_unsupported_field_type_is_rejected(valid_config: dict) -> None:
    valid_config["fields"][0]["type"] = "dropdown"

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(valid_config)


def test_hotkeys_are_normalised(valid_config: dict) -> None:
    valid_config["safety"] = {"stop_hotkey": " ALT+T ", "pause_hotkey": "Alt+P"}

    config = AgentConfig.model_validate(valid_config)

    assert config.safety.stop_hotkey == "alt+t"
    assert config.safety.pause_hotkey == "alt+p"


def test_app_requires_a_matcher(valid_config: dict) -> None:
    valid_config["app"] = {"title_contains": "", "process_name": None}

    with pytest.raises(ValidationError, match="title_contains or app.process_name"):
        AgentConfig.model_validate(valid_config)


def test_hotkeys_must_be_distinct(valid_config: dict) -> None:
    valid_config["safety"] = {"stop_hotkey": "alt+t", "pause_hotkey": "alt+t"}

    with pytest.raises(ValidationError, match="different hotkeys"):
        AgentConfig.model_validate(valid_config)


def test_resolve_path_uses_project_root() -> None:
    assert resolve_path("data/work/a.json") == project_root() / "data" / "work" / "a.json"
    absolute = project_root() / "data" / "work" / "a.json"
    assert resolve_path(absolute) == absolute


def test_a_workflow_step_must_fill_a_declared_field(valid_config: dict) -> None:
    valid_config["workflow"] = {
        "steps": [{"action": "fill", "key": "unknown_field"}]
    }

    with pytest.raises(ValidationError, match="does not declare"):
        AgentConfig.model_validate(valid_config)


def test_a_workflow_needs_at_least_one_step(valid_config: dict) -> None:
    valid_config["workflow"] = {"steps": []}

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(valid_config)


def test_an_unknown_action_is_rejected(valid_config: dict) -> None:
    valid_config["workflow"] = {"steps": [{"action": "scroll", "key": "name"}]}

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(valid_config)


def test_the_save_verifier_defaults_to_reading_text() -> None:
    config = ConfirmationConfig()

    assert config.verifier == "text"
    assert config.footprint == []


def test_a_row_verifier_keeps_a_clean_footprint() -> None:
    config = ConfirmationConfig(
        verifier="row", footprint=[" name ", "", "name", "amount"]
    )

    assert config.footprint == ["name", "amount"]


def test_an_unknown_save_verifier_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ConfirmationConfig(verifier="vibes")
