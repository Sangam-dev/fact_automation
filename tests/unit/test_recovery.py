"""Learned exception rules: what they match, and what they refuse to match.

The value of a learned rule is that it fires on the situation it was taught for
and on nothing else. These tests are mostly about the refusal: one missing
control id, or one forbidden id showing up, and the rule must not apply.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.automation.exceptions import (
    Observation,
    Rule,
    RuleAction,
    RuleError,
    RuleTable,
    Trigger,
    load_rules,
    rule_from_screen,
    rules_path,
    save_rules,
)


def _seen(**overrides) -> Observation:
    base = dict(
        state="POPUP",
        title="FACT ERP.NG",
        class_name="WindowsForms10.Window.8.app.0",
        auto_ids=("frmMessageBox", "btnOK", "lblMessage"),
    )
    base.update(overrides)
    return Observation(**base)


def _rule(**overrides) -> Rule:
    data = dict(
        id="missing-date",
        description="the date is required",
        when=Trigger(
            state="POPUP",
            title_contains="FACT ERP.NG",
            auto_ids=["frmMessageBox", "btnOK"],
        ),
        actions=[RuleAction(action="click", button="OK")],
        expect="05_add",
    )
    data.update(overrides)
    return Rule.model_validate(data)


# -------------------------------------------------------------------- trigger --


def test_a_trigger_with_no_signal_is_refused() -> None:
    with pytest.raises(ValueError):
        Trigger()


def test_every_signal_has_to_agree() -> None:
    trigger = _rule().when

    assert trigger.matches(_seen())
    assert not trigger.matches(_seen(state="05_add"))
    assert not trigger.matches(_seen(title="Some other program"))
    assert not trigger.matches(_seen(auto_ids=("frmMessageBox",)))


def test_a_forbidden_control_keeps_the_rule_away() -> None:
    rule = _rule(
        when=Trigger(
            state="POPUP",
            auto_ids=["btnOK"],
            absent_auto_ids=["btnDelete"],
        )
    )

    assert rule.when.matches(_seen())
    assert not rule.when.matches(
        _seen(auto_ids=("btnOK", "btnDelete"))
    )


# ---------------------------------------------------------------- learned rule --


def test_learning_records_the_whole_identity_of_the_screen() -> None:
    rule = rule_from_screen(
        "popup-1",
        _seen(),
        [RuleAction(action="click", button="OK")],
        expect="05_add",
        description="date required",
    )

    assert rule.when.state == "POPUP"
    assert rule.when.title_contains == "FACT ERP.NG"
    assert rule.when.class_contains
    assert rule.when.auto_ids == ["frmMessageBox", "btnOK", "lblMessage"]
    assert rule.actions[0].button == "OK"
    assert rule.expect == "05_add"


def test_learning_a_known_screen_does_not_pin_the_popup_state() -> None:
    rule = rule_from_screen(
        "view-1",
        _seen(state="04_view"),
        [RuleAction(action="key", keys="Enter")],
    )

    assert rule.when.state == ""
    assert rule.when.title_contains


def test_learning_needs_an_action() -> None:
    with pytest.raises(RuleError):
        rule_from_screen("popup-1", _seen(), [])


def test_a_rule_id_has_to_be_usable_as_a_name() -> None:
    with pytest.raises(ValueError):
        Rule.model_validate(
            {
                "id": "Not A Rule",
                "when": {"state": "POPUP"},
                "actions": [{"action": "click", "button": "OK"}],
            }
        )


# --------------------------------------------------------------------- table --


def test_find_returns_the_first_matching_rule() -> None:
    table = RuleTable(
        automation="fact_automation",
        rules=[
            _rule(id="one"),
            _rule(
                id="two",
                when=Trigger(state="UNKNOWN", title_contains="Something else"),
            ),
        ],
    )

    assert table.find(_seen()).id == "one"
    assert table.find(_seen(state="UNKNOWN", title="Something else")).id == "two"
    assert table.find(_seen(state="04_view")) is None


def test_adding_a_rule_with_the_same_id_replaces_it() -> None:
    table = RuleTable(automation="fact_automation", rules=[_rule()])
    table.add(
        _rule(actions=[RuleAction(action="key", keys="Escape")])
    )

    assert len(table.rules) == 1
    assert table.rules[0].actions[0].keys == "Escape"


def test_removing_a_rule_that_is_not_there_says_so() -> None:
    table = RuleTable(automation="fact_automation", rules=[_rule()])

    with pytest.raises(RuleError):
        table.remove("nope")


def test_rules_round_trip_beside_the_automation(tmp_path: Path) -> None:
    table = RuleTable(automation="fact_automation", rules=[_rule()])

    path = save_rules(table, tmp_path)
    again = load_rules("fact_automation", tmp_path)

    assert path == rules_path("fact_automation", tmp_path)
    assert path.name == "exceptions.json"
    assert again is not None
    assert again.rules[0].id == "missing-date"
    assert again.rules[0].actions[0].button == "OK"


def test_no_rule_file_means_no_rules_not_an_error(tmp_path: Path) -> None:
    assert load_rules("fact_automation", tmp_path) is None


def test_a_rule_file_for_another_automation_is_refused(tmp_path: Path) -> None:
    misplaced = rules_path("fact_automation", tmp_path)
    misplaced.parent.mkdir(parents=True)
    misplaced.write_text(
        RuleTable(automation="other", rules=[_rule()]).model_dump_json(
            by_alias=True
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuleError):
        load_rules("fact_automation", tmp_path)


def test_a_key_action_needs_keys_and_not_a_button() -> None:
    with pytest.raises(ValueError):
        RuleAction(action="key", button="OK")


# ------------------------------------------------- the runner actually uses it --
RUNNER = "fact_automation"


def _runner_plan():
    from core.automation.model import Automation

    return Automation.model_validate(
        {
            "name": RUNNER,
            "app": {
                "title_contains": "FACT ERP.NG",
                "process_name": "FACTERPNG.exe",
            },
            "fields": [{"name": "customer", "field": "customer_code"}],
            "steps": [
                {
                    "id": "01-click-add",
                    "action": "click",
                    "button": "Add",
                    "checkpoint": {"state": "04_view", "resume": "replay"},
                },
                {
                    "id": "02-fill-customer",
                    "action": "fill",
                    "key": "customer",
                    "checkpoint": {"state": "05_add", "resume": "restart"},
                },
            ],
            "save": {"button": "Save & New", "verifier": "row", "footprint": ["customer"]},
        }
    )


def _state_table():
    from desktop.state.states import StateTable

    return StateTable.model_validate(
        {
            "process": "FACTERPNG.exe",
            "states": [
                {"id": "04_view", "match": {"auto_id": "40200"}},
                {"id": "05_add", "match": {"auto_id": "40200"}},
            ],
        }
    )


def _live_window():
    from desktop.types import Rect, WindowInfo

    return WindowInfo(
        handle=1,
        title="FACT ERP.NG - Validation",
        process_name="FACTERPNG.exe",
        class_name="WindowsForms10.Window.8.app.0",
        rect=Rect(0, 0, 400, 300),
        client_rect=Rect(0, 0, 400, 300),
        dpi=96,
    )


class _LiveInspection:
    def __init__(self) -> None:
        from desktop.types import Rect, UIACandidate

        self.window = _live_window()
        self.candidates = (
            UIACandidate(
                candidate_id="b1",
                name="OK",
                control_type="Button",
                automation_id="btnOK",
                class_name="Button",
                rect=Rect(10, 10, 60, 40),
                value="",
                enabled=True,
            ),
        )

    def read_value(self, candidate_id: str) -> str:
        return ""


class _Screen:
    """04_view to begin with, then a popup until the learned click lands."""

    def __init__(self) -> None:
        self.calls = 0
        self.clicked = 0

    def __call__(self):
        from desktop.state.recognizer import StateMatch

        self.calls += 1
        if self.clicked:
            return StateMatch(state_id="05_add")
        if self.calls == 1:
            return StateMatch(state_id="04_view")
        return StateMatch(state_id="POPUP", window=_live_window())


class _Execute:
    """Calls the runner's own hooks, so checkpoints are really exercised."""

    def __call__(self, steps, values, **options):
        from execution.actions.engine import PlanResult

        before = options.get("before_step")
        after = options.get("on_step")
        for position, step in enumerate(steps, start=1):
            if before is not None:
                before(position, step)
            if after is not None:
                after(position, step)
        return PlanResult(
            inspection=_LiveInspection(), fields={}, steps_run=len(steps)
        )


class _Saver:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **options):
        from execution.verification.confirmation import CONFIRMED, ConfirmationResult
        from execution.workflows.runner import SaveResult

        self.calls += 1
        return SaveResult(
            outcome=ConfirmationResult(CONFIRMED, "the row is on screen")
        )


def _run(tmp_path: Path, rules, monkeypatch):
    from core.automation import registry
    from execution.workflows import automation_runner

    registry.save(_runner_plan(), tmp_path / "automations")
    if rules is not None:
        save_rules(rules, tmp_path / "automations")

    screen = _Screen()
    clicks: list[tuple[int, int]] = []

    def fake_click(x, y, window, **kwargs):
        clicks.append((x, y))
        screen.clicked += 1

    monkeypatch.setattr(automation_runner, "click_checked", fake_click)
    lines: list[str] = []

    run = automation_runner.run_automation(
        RUNNER,
        {"customer": "CE-CUS2542"},
        automations_root=tmp_path / "automations",
        progress_root=tmp_path / "progress",
        table=_state_table(),
        detector=screen,
        attach=lambda app, **kwargs: _live_window(),
        inspect=lambda window: _LiveInspection(),
        execute=_Execute(),
        save_call=_Saver(),
        clear_form=lambda *a, **k: True,
        output=lines.append,
        sleep=lambda seconds: None,
        checkpoint_seconds=0,
        checkpoint_interval=0,
    )
    return run, clicks, lines


def test_a_learned_rule_gets_the_run_past_the_popup(tmp_path: Path, monkeypatch) -> None:
    from core.automation.exceptions import Rule, RuleAction, RuleTable, Trigger

    rules = RuleTable(
        automation=RUNNER,
        rules=[
            Rule(
                id="validation-ok",
                description="dismiss the validation message",
                when=Trigger(state="POPUP", auto_ids=["btnOK"]),
                actions=[RuleAction(action="click", button="OK")],
                expect="05_add",
            )
        ],
    )

    run, clicks, lines = _run(tmp_path, rules, monkeypatch)

    assert run.status == "saved", run.message
    assert clicks == [(35, 25)]
    assert any("applying rule 'validation-ok'" in line for line in lines)
    assert any("recovered" in line for line in lines)


def test_an_unmatched_popup_still_stops_and_says_how_to_learn_it(
    tmp_path: Path, monkeypatch
) -> None:
    from core.automation.exceptions import Rule, RuleAction, RuleTable, Trigger

    rules = RuleTable(
        automation=RUNNER,
        rules=[
            Rule(
                id="another-dialog",
                when=Trigger(state="POPUP", auto_ids=["btnSomethingElse"]),
                actions=[RuleAction(action="click", button="OK")],
            )
        ],
    )

    run, clicks, lines = _run(tmp_path, rules, monkeypatch)

    assert run.status == "failed"
    assert clicks == []
    assert "automation learn" in run.message
    assert any("no learned rule matches" in line for line in lines)
