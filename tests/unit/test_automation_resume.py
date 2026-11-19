"""Where a run starts: the walk back out of a form, and the refusals."""

from __future__ import annotations

import pytest

from core.automation.model import AutomationStep, Checkpoint
from core.automation.progress import digest_values, new_progress
from core.automation.resume import find_start_index, plan_resume


def plans() -> list[AutomationStep]:
    """Open the list, open the form, type twice, then save."""
    return [
        AutomationStep(
            id="01-open",
            action="click",
            button="&Add",
            checkpoint=Checkpoint(state="04_view", resume="replay"),
        ),
        AutomationStep(
            id="02-customer",
            action="fill",
            key="customer",
            checkpoint=Checkpoint(state="05_add", resume="restart"),
        ),
        AutomationStep(
            id="03-lines",
            action="fill",
            key="lines",
            checkpoint=Checkpoint(state="05_add", resume="restart"),
        ),
        AutomationStep(
            id="04-save",
            action="click",
            button="Save",
            checkpoint=Checkpoint(state="05_add", resume="replay"),
        ),
    ]


def at_view(state: str) -> bool:
    return state == "04_view"


def everything(state: str) -> bool:
    return True


def nothing(state: str) -> bool:
    return False


def progress(cursor: int, **overrides):
    item = new_progress("fact_automation", {"customer": "Ram"})
    item.cursor = cursor
    for key, value in overrides.items():
        setattr(item, key, value)
    return item


# ------------------------------------------------------------ the walk-back --
def test_no_previous_run_starts_at_the_first_step() -> None:
    plan = plan_resume(plans())
    assert plan.index == 0
    assert plan.blocked is False
    assert "no earlier run" in plan.reason


def test_a_cursor_in_the_middle_of_a_form_replays_from_the_click() -> None:
    plan = plan_resume(plans(), progress(2), is_satisfied=everything)
    assert plan.index == 0, "typing into a half-filled form must not be resumed"
    assert plan.walked_back == 2
    assert "replays from step 1" in plan.reason


def test_a_cursor_on_a_click_step_can_resume_right_here() -> None:
    plan = plan_resume(plans(), progress(3), is_satisfied=everything)
    assert plan.index == 3
    assert plan.walked_back == 0
    assert "resuming at step 4" in plan.reason


def test_a_cursor_on_the_last_fill_still_walks_back_to_the_click() -> None:
    # cursor 2 means steps 1-2 are done, so the next step is a fill: back to 1.
    plan = plan_resume(plans(), progress(2), is_satisfied=everything)
    assert plan.index == 0


def test_the_walk_back_continues_while_the_screen_does_not_match() -> None:
    # Step 4 is safe to replay, but only the list screen is on display, so the
    # run has to go back to the step that belongs on it.
    plan = plan_resume(plans(), progress(3), is_satisfied=at_view)
    assert plan.index == 0


def test_a_run_never_starts_on_a_fill_step() -> None:
    # Step 4 is safe to replay but its screen is not up, so the walk goes back.
    # Step 3's screen *is* up, but step 3 types into the form: stopping there
    # would append to data the form already holds, so it keeps walking.
    steps = plans()
    steps[3].checkpoint = Checkpoint(state="06_saved", resume="replay")

    def only_the_form(state: str) -> bool:
        return state == "05_add"

    plan = plan_resume(steps, progress(3), is_satisfied=only_the_form)
    assert steps[2].checkpoint.resume == "restart"
    assert only_the_form(steps[2].checkpoint.state)
    assert plan.index == 0, "starting on a fill would append to the open form"


def test_the_first_step_may_be_a_fill_because_nothing_precedes_it() -> None:
    steps = [
        AutomationStep(
            id="01-type",
            action="fill",
            key="customer",
            checkpoint=Checkpoint(state="05_add", resume="restart"),
        )
    ]
    assert find_start_index(steps, 0, everything) == 0


def test_a_cursor_at_the_start_is_used_directly() -> None:
    plan = plan_resume(plans(), progress(0), is_satisfied=everything)
    assert plan.index == 0
    assert "resuming at step 1" in plan.reason


def test_no_matching_screen_falls_back_to_the_first_step() -> None:
    plan = plan_resume(plans(), progress(2), is_satisfied=nothing)
    assert plan.index == 0
    assert plan.blocked is False


def test_a_finished_run_starts_over() -> None:
    plan = plan_resume(plans(), progress(4), is_satisfied=everything)
    assert plan.index == 0
    assert "reached the last step" in plan.reason


def test_an_out_of_range_cursor_is_clamped() -> None:
    steps = plans()
    assert find_start_index(steps, 99, everything) == len(steps) - 1
    plan = plan_resume(steps, progress(99), is_satisfied=everything)
    assert plan.index == 0
    assert "reached the last step" in plan.reason


def test_an_empty_plan_cannot_be_resumed() -> None:
    with pytest.raises(ValueError):
        plan_resume([])
    with pytest.raises(ValueError):
        find_start_index([], 0, everything)


# --------------------------------------------------------------- the refusals --
def test_an_unresolved_save_stops_the_run_before_it_starts() -> None:
    item = progress(4)
    item.begin_save()
    plan = plan_resume(plans(), item, is_satisfied=everything)
    assert plan.blocked is True
    assert "result is unknown" in plan.blocked_reason
    assert "--saved-already" in plan.blocked_reason


def test_different_values_stop_the_run() -> None:
    item = progress(2, values_digest=digest_values({"customer": "Ram"}))
    plan = plan_resume(
        plans(),
        item,
        values_digest=digest_values({"customer": "Sita"}),
        is_satisfied=everything,
    )
    assert plan.blocked is True
    assert "--restart" in plan.blocked_reason


def test_the_same_values_do_not_stop_the_run() -> None:
    item = progress(2, values_digest=digest_values({"customer": "Ram"}))
    plan = plan_resume(
        plans(),
        item,
        values_digest=digest_values({"customer": "Ram"}),
        is_satisfied=everything,
    )
    assert plan.blocked is False


def test_a_hand_edit_step_is_never_run_automatically() -> None:
    steps = plans()
    steps[1].checkpoint.resume = "hand_edit"
    plan = plan_resume(steps, progress(1), is_satisfied=everything)
    assert plan.index == 1
    assert plan.blocked is True
    assert "hand_edit" in plan.blocked_reason


def test_a_hand_edit_first_step_blocks_a_fresh_run() -> None:
    steps = plans()
    steps[0].checkpoint.resume = "hand_edit"
    plan = plan_resume(steps, is_satisfied=everything)
    assert plan.blocked is True
    assert "hand_edit" in plan.blocked_reason


# -------------------------------------------------------------------- wording --
def test_describe_names_the_step_that_will_run() -> None:
    steps = plans()
    plan = plan_resume(steps, progress(3), is_satisfied=everything)
    text = plan.describe(steps)
    assert "04-save" in text
    assert "step 4 of 4" in text


def test_describe_explains_a_refusal() -> None:
    item = progress(4)
    item.begin_save()
    steps = plans()
    text = plan_resume(steps, item).describe(steps)
    assert "stopped before" in text
    assert "result is unknown" in text
