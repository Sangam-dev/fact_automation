"""The saved-automation model: shape, validation, and the runtime hand-off."""

from __future__ import annotations

from datetime import datetime

import pytest

from core.automation.model import (
    AUTOMATION_SCHEMA,
    Automation,
    AutomationError,
    AutomationStep,
    Checkpoint,
)
from core.config import AgentConfig
from execution.actions.models import ClickStep, FillStep


def sample(**overrides) -> dict:
    data = {
        "schema": AUTOMATION_SCHEMA,
        "name": "fact_automation",
        "app": {"title_contains": "FACT ERP.NG", "process_name": "FACTERPNG.exe"},
        "fields": [{"name": "customer", "field": "Customer Name"}],
        "steps": [
            {
                "id": "01-open",
                "action": "click",
                "button": "Add",
                "checkpoint": {"state": "04_view", "resume": "replay"},
            },
            {
                "id": "02-customer",
                "action": "fill",
                "key": "customer",
                "checkpoint": {"state": "05_add", "resume": "restart"},
            },
        ],
        "save": {"button": "Save", "verifier": "row", "footprint": ["customer"]},
    }
    data.update(overrides)
    return data


def automation(**overrides) -> Automation:
    return Automation.model_validate(sample(**overrides))


def test_minimal_automation_reports_its_screens_and_steps() -> None:
    plan = automation()
    assert plan.name == "fact_automation"
    assert plan.step_ids() == ["01-open", "02-customer"]
    assert plan.state_ids() == ["04_view", "05_add"]
    assert plan.verified is False


def test_to_config_hands_the_plan_to_the_existing_runner() -> None:
    config = automation().to_config()
    assert isinstance(config, AgentConfig)
    assert config.actions.save_button == "Save"
    assert config.confirmation.verifier == "row"
    assert config.confirmation.footprint == ["customer"]

    steps = config.workflow.steps
    assert isinstance(steps[0], ClickStep) and steps[0].button == "Add"
    assert isinstance(steps[1], FillStep) and steps[1].key == "customer"


def test_to_config_keeps_optional_steps_optional() -> None:
    data = sample()
    data["steps"][0]["optional"] = True
    assert Automation.model_validate(data).to_config().workflow.steps[0].optional


def test_validate_states_names_the_unknown_screens() -> None:
    plan = automation()
    plan.validate_states({"04_view", "05_add"})
    with pytest.raises(AutomationError) as error:
        plan.validate_states({"04_view"})
    assert "05_add" in str(error.value)


def test_validate_states_with_no_table_does_not_pretend_to_check() -> None:
    automation().validate_states([])


def test_duplicate_step_ids_are_rejected() -> None:
    data = sample()
    data["steps"][1]["id"] = "01-open"
    with pytest.raises(Exception, match="used twice"):
        Automation.model_validate(data)


def test_a_fill_must_name_a_declared_field() -> None:
    data = sample()
    data["steps"][1]["key"] = "not_declared"
    with pytest.raises(Exception, match="does not declare"):
        Automation.model_validate(data)


def test_a_click_must_name_a_button() -> None:
    data = sample()
    data["steps"][0].pop("button")
    with pytest.raises(Exception, match="button"):
        Automation.model_validate(data)


def test_a_fill_may_not_carry_a_button() -> None:
    data = sample()
    data["steps"][1]["button"] = "Add"
    with pytest.raises(Exception, match="no button"):
        Automation.model_validate(data)


def test_a_checkpoint_must_say_how_it_may_be_resumed() -> None:
    data = sample()
    data["steps"][0]["checkpoint"].pop("resume")
    with pytest.raises(Exception):
        Automation.model_validate(data)


def test_an_unknown_resume_policy_is_rejected() -> None:
    data = sample()
    data["steps"][0]["checkpoint"]["resume"] = "maybe"
    with pytest.raises(Exception):
        Automation.model_validate(data)


def test_a_checkpoint_must_name_a_state() -> None:
    with pytest.raises(Exception):
        Checkpoint(state="  ", resume="replay")


def test_a_plan_needs_at_least_one_step() -> None:
    with pytest.raises(Exception):
        automation(steps=[])


def test_an_unknown_schema_is_rejected() -> None:
    with pytest.raises(Exception, match="schema"):
        automation(schema="automation/99")


def test_a_step_may_not_click_save_a_second_time() -> None:
    data = sample()
    data["steps"].append(
        {
            "id": "03-save",
            "action": "click",
            "button": "Save",
            "checkpoint": {"state": "05_add", "resume": "replay"},
        }
    )
    with pytest.raises(Exception, match="click the save button"):
        Automation.model_validate(data)


def test_a_step_may_click_something_else_that_says_save() -> None:
    data = sample()
    data["save"] = {"button": "&Save"}
    data["steps"].append(
        {
            "id": "03-add-line",
            "action": "click",
            "button": "Save line",
            "checkpoint": {"state": "05_add", "resume": "replay"},
        }
    )
    assert Automation.model_validate(data).step_ids()[-1] == "03-add-line"


def test_footprint_must_name_declared_fields() -> None:
    with pytest.raises(Exception, match="footprint"):
        automation(save={"button": "Save", "footprint": ["ghost"]})


def test_step_ids_must_be_usable_as_keys() -> None:
    data = sample()
    data["steps"][0]["id"] = "has spaces"
    with pytest.raises(Exception, match="step ids"):
        Automation.model_validate(data)


def test_touch_stamps_created_once_and_updated_every_time() -> None:
    plan = automation()
    first = datetime(2026, 11, 19, 10, 0, 0)
    second = datetime(2026, 11, 19, 11, 0, 0)

    plan.touch(first)
    assert plan.created_at == first.isoformat()
    assert plan.updated_at == first.isoformat()

    plan.touch(second)
    assert plan.created_at == first.isoformat()
    assert plan.updated_at == second.isoformat()


def test_fill_keys_lists_what_the_plan_types_once_each() -> None:
    data = sample()
    data["fields"].append({"name": "date", "field": "NepDateEdit"})
    data["steps"].append(
        {
            "id": "03-date",
            "action": "fill",
            "key": "date",
            "checkpoint": {"state": "05_add", "resume": "restart"},
        }
    )
    data["steps"].append(
        {
            "id": "04-date-again",
            "action": "fill",
            "key": "date",
            "checkpoint": {"state": "05_add", "resume": "restart"},
        }
    )
    plan = Automation.model_validate(data)
    assert plan.fill_keys() == ["customer", "date"]


def test_step_describe_is_readable() -> None:
    step = AutomationStep(
        id="02-customer",
        action="fill",
        key="customer",
        checkpoint=Checkpoint(state="05_add", resume="restart"),
    )
    assert "02-customer" in step.describe()
    assert "customer" in step.describe()
