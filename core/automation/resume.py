"""Choosing which step a run starts at, given the last one and the screen.

The rule is not "jump to the cursor". A fill cannot be resumed in place: typing
into a half-filled form appends or corrupts, so a run that stopped halfway
through a form must go back to the nearest step where starting again is safe -
the click that opens the form - and replay from there.

So the walk is: keep going back while a step is a ``restart`` step or its
screen is not the one on display. What it lands on is either a step that is
safe to run from a standing start, or the first step, where the runner clears
any stale form and lets the checkpoint fail loudly if even that is wrong.

A person can also say where to start: with ``from_screen`` the screen on
display chooses the step, not the cursor - the app is already where it should
be, so replaying the navigation that got there would be pointless.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from core.automation.model import AutomationStep
from core.automation.progress import ProgressState

#: Answers "is this state on screen right now?" - the recogniser, in practice.
Satisfied = Callable[[str], bool]


@dataclass(frozen=True)
class ResumePlan:
    """Where a run should start, and why."""

    index: int
    reason: str
    blocked: bool = False
    blocked_reason: str = ""
    walked_back: int = 0

    def describe(self, steps: Sequence[AutomationStep] | None = None) -> str:
        total = f" of {len(steps)}" if steps is not None else ""
        where = (
            f"step {self.index + 1}{total} ({steps[self.index].id})"
            if steps is not None
            else f"step {self.index + 1}"
        )
        if self.blocked:
            return f"stopped before {where}: {self.blocked_reason}"
        return f"{self.reason}: starting at {where}"


def find_start_index(
    steps: Sequence[AutomationStep],
    cursor: int,
    is_satisfied: Satisfied,
) -> int:
    """The first step that is both safe to run from here and belongs here."""
    if not steps:
        raise ValueError("an automation with no steps cannot be resumed")

    index = max(0, min(cursor, len(steps) - 1))

    # A run may only *start* on a step that is safe to run from a standing
    # start. A fill is not: the form it types into may already hold earlier
    # steps' data, so replaying it would append. Those steps are walked past,
    # and so is any step whose screen is not on display.
    while index > 0:
        checkpoint = steps[index].checkpoint
        if checkpoint.resume == "restart" or not is_satisfied(checkpoint.state):
            index -= 1
            continue
        break

    # Index 0 is the only place a restart step may be used: there is nothing
    # before it to duplicate, and the runner clears a stale form first.
    return index


def start_on_screen(
    steps: Sequence[AutomationStep],
    is_satisfied: Satisfied,
) -> int | None:
    """The first step that belongs on the screen on display, or None.

    This is the explicit "start where I am" rule, and it differs from
    :func:`find_start_index` on purpose: it may land on a ``restart`` step.
    That policy exists to stop a run *guessing* its way into a form that may
    already hold data, not to stop a person saying "the form is open, fill
    it". The refusals that protect the record itself - an unresolved save,
    changed values - are decided before this is consulted.
    """
    for index, step in enumerate(steps):
        if is_satisfied(step.checkpoint.state):
            return index
    return None


def plan_resume(
    steps: Sequence[AutomationStep],
    progress: ProgressState | None = None,
    *,
    values_digest: str | None = None,
    is_satisfied: Satisfied | None = None,
    from_screen: bool = False,
) -> ResumePlan:
    """Decide the starting step, or refuse to start at all.

    ``is_satisfied`` is called once per candidate with the checkpoint's state
    id. When it is omitted every screen is assumed to be available, which is
    what the wizard's proof run wants; a real run always passes the recogniser.

    ``from_screen`` starts at the step the screen belongs to instead of at the
    step an earlier run left off at. It is the operator's explicit choice: it
    means "the app is already where I want it", so it is allowed to begin later
    in the plan than any run has been.
    """
    if not steps:
        raise ValueError("an automation with no steps cannot be resumed")
    available = is_satisfied or (lambda _state: True)

    # The record's own protection comes first, whichever start was asked for: a
    # Save of unknown result and a different set of values both mean a person
    # has to decide something before anything is typed.
    if progress is not None and progress.save_unresolved:
        return ResumePlan(
            index=min(progress.cursor, len(steps) - 1),
            reason="the previous run did not finish",
            blocked=True,
            blocked_reason=progress.block_reason(),
        )

    if (
        progress is not None
        and values_digest is not None
        and progress.values_changed(values_digest)
    ):
        return ResumePlan(
            index=min(progress.cursor, len(steps) - 1),
            reason="the record data changed",
            blocked=True,
            blocked_reason=(
                f"run {progress.run_id} used different values; re-run with "
                "--restart to type this record from the beginning"
            ),
        )

    if from_screen:
        found = start_on_screen(steps, available)
        if found is None:
            return _settle(
                steps,
                0,
                "the screen on display is not one of the plan's screens",
            )
        if steps[found].action in ("fill", "run"):
            return _settle(
                steps,
                found,
                f"the screen on display is where step {found + 1} starts, and "
                "that step types into the form already on it",
            )
        return _settle(
            steps,
            found,
            f"the screen on display is where step {found + 1} starts",
        )

    if progress is None:
        return _settle(
            steps,
            0,
            f"no earlier run of this automation; starting at step 1",
        )

    if progress.cursor >= len(steps):
        return _settle(
            steps,
            0,
            "the previous run reached the last step",
        )

    index = find_start_index(steps, progress.cursor, available)
    if index == progress.cursor:
        reason = f"resuming at step {index + 1} of {len(steps)}"
    else:
        skipped = progress.cursor - index
        undone = (
            f"step {progress.cursor} types into the form"
            if skipped == 1
            else f"steps {index + 1}-{progress.cursor} type into the form"
        )
        reason = (
            f"the last run stopped before step {progress.cursor + 1}; "
            f"{undone}, so it replays from step {index + 1}"
        )
    return _settle(
        steps,
        index,
        reason,
        walked_back=progress.cursor - index,
    )


def _settle(
    steps: Sequence[AutomationStep],
    index: int,
    reason: str,
    *,
    walked_back: int = 0,
) -> ResumePlan:
    """Turn a chosen index into a plan, refusing steps that need a person."""
    step = steps[index]
    if step.checkpoint.resume == "hand_edit":
        return ResumePlan(
            index=index,
            reason=reason,
            blocked=True,
            blocked_reason=(
                f"step {index + 1} ({step.id}) is marked hand_edit: a person has "
                "to do that step; the run will not do it"
            ),
            walked_back=walked_back,
        )
    return ResumePlan(index=index, reason=reason, walked_back=walked_back)


__all__ = [
    "ResumePlan",
    "Satisfied",
    "find_start_index",
    "plan_resume",
    "start_on_screen",
]
