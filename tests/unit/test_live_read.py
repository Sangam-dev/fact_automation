"""A screen read always comes back: with the answer, or with a reason.

Windows UI Automation can take a very long time to answer a busy or modal
application, and a run that waits forever has no error to report. These tests
pin the two halves of the budget: a fast read is returned untouched, and a read
that overruns is abandoned with a message a person can act on.
"""

from __future__ import annotations

import time

from desktop.state import fact
from desktop.state.recognizer import StateMatch


def test_a_read_that_overruns_is_abandoned_with_a_reason(monkeypatch) -> None:
    def slow(table, *, process=None):
        time.sleep(2.0)
        return StateMatch(state_id="04_view")

    # fact.py holds its own reference to the recogniser, so that is what has to
    # be replaced - patching the module attribute would leave the real read in
    # place and the test would be measuring nothing.
    monkeypatch.setattr(fact, "detect", slow)

    started = time.monotonic()
    match = fact.detect_state(seconds=0.1)

    assert time.monotonic() - started < 1.5
    assert match.state_id == "UNKNOWN"
    assert "did not answer" in match.detail
    assert match.known is False


def test_a_fast_read_is_returned_as_the_recogniser_answered(monkeypatch) -> None:
    monkeypatch.setattr(
        fact,
        "detect",
        lambda table, *, process=None: StateMatch(state_id="05_add"),
    )

    assert fact.detect_state(seconds=5.0).state_id == "05_add"


def test_an_error_inside_the_read_is_reported_not_swallowed(monkeypatch) -> None:
    def broken(table, *, process=None):
        raise RuntimeError("UIA is not available")

    monkeypatch.setattr(fact, "detect", broken)

    try:
        fact.detect_state(seconds=5.0)
    except fact.FactError as exc:
        assert "UIA is not available" in str(exc)
    else:  # pragma: no cover - the failure the test is about
        raise AssertionError("a broken read must not be reported as a screen")
