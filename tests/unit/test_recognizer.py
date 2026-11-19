"""The state recogniser, checked against the snapshots it was learned from.

These tests are offline: they read ``scripts/snapshots/*/snapshot.json`` and
never touch Windows. They are the gate for the state layer, because everything
above it - checkpoints, resume, "start from the state you find" - trusts the
answer this produces.
"""

from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
from pathlib import Path

import pytest

from desktop.state.learner import SnapshotSample, learn, load_snapshots, verify
from desktop.state.recognizer import (
    POPUP,
    STARTING,
    UNKNOWN,
    ControlView,
    Observation,
    WindowView,
    match,
    normalise,
)
from desktop.state.states import (
    STATE_TABLE_SCHEMA,
    ControlNeed,
    MatchSpec,
    StateSpec,
    StateTable,
)

SNAPSHOT_ROOT = Path(__file__).resolve().parents[2] / "scripts" / "snapshots"
FORM_CLASS = "WindowsForms10.Window.8.app.0.141b42a_r6_ad1"
SALE_ORDER_ID = "40200"

pytestmark = pytest.mark.skipif(
    not (SNAPSHOT_ROOT / "05_add_101539").exists(),
    reason="captured snapshots are not present",
)


@lru_cache(maxsize=1)
def samples() -> tuple[SnapshotSample, ...]:
    return tuple(load_snapshots(SNAPSHOT_ROOT))


@lru_cache(maxsize=1)
def table() -> StateTable:
    return learn(list(samples()), "FACTERPNG.exe")


def sample(state_id: str) -> SnapshotSample:
    for item in samples():
        if item.state_id == state_id:
            return item
    raise AssertionError(f"no snapshot for {state_id!r}")


def control(auto_id: str = "", name: str = "", control_type: str = "Button") -> ControlView:
    return ControlView(type=control_type, name=name, auto_id=auto_id)


def window(
    title: str,
    auto_id: str = "",
    *,
    class_name: str = FORM_CLASS,
    controls: tuple[ControlView, ...] | None = (),
    z_order: int = 0,
) -> WindowView:
    return WindowView(
        title=title,
        class_name=class_name,
        auto_id=auto_id,
        z_order=z_order,
        controls=controls,
    )


def observation(*windows: WindowView) -> Observation:
    return Observation(process="FACTERPNG.exe", windows=windows)


def retitled(item: SnapshotSample, auto_id: str, title: str) -> Observation:
    """The same screen with the title of its sibling window."""
    windows = tuple(
        replace(candidate, title=title) if candidate.auto_id == auto_id else candidate
        for candidate in item.observation.windows
    )
    return Observation(process=item.observation.process, windows=windows)


# --------------------------------------------------------------- the snapshots --
@pytest.mark.parametrize(
    "state_id",
    ["01_login", "02_welcome", "03_entry", "04_view", "05_add"],
)
def test_each_snapshot_recognises_its_own_state(state_id: str) -> None:
    item = sample(state_id)
    result = match(item.observation, table())
    assert result.state_id == state_id, result.message()
    assert result.known


def test_learned_table_recognises_every_snapshot() -> None:
    assert verify(table(), list(samples())) == []


def test_sale_order_screens_carry_disjoint_control_evidence() -> None:
    view = table().get("04_view")
    add = table().get("05_add")
    assert view.match.require and view.match.forbid
    assert add.match.require and add.match.forbid

    def keys(needs: list[ControlNeed]) -> set[tuple[str, str]]:
        return {
            ("id", (need.auto_id or "").casefold())
            if need.auto_id
            else ("name", normalise(need.name))
            for need in needs
        }

    assert not keys(view.match.require) & keys(add.match.require)
    assert view.match.require != add.match.require


# --------------------------------------------------- controls, not title, decide --
def test_add_screen_wearing_the_view_title_is_not_the_view() -> None:
    item = sample("05_add")
    disguised = retitled(item, SALE_ORDER_ID, sample("04_view").window.title)
    result = match(disguised, table())
    assert result.state_id != "04_view", "the title alone was enough to fool the table"
    assert not result.known


def test_view_screen_wearing_the_add_title_is_not_the_add() -> None:
    item = sample("04_view")
    disguised = retitled(item, SALE_ORDER_ID, sample("05_add").window.title)
    result = match(disguised, table())
    assert result.state_id != "05_add"
    assert not result.known


def test_losing_a_proving_control_loses_the_state() -> None:
    item = sample("04_view")
    proving = {
        (need.auto_id or "").casefold()
        for need in table().get("04_view").match.require
        if need.auto_id
    }
    assert proving & {c.auto_id.casefold() for c in item.window.controls or ()}

    stripped = tuple(
        control for control in item.window.controls or ()
        if control.auto_id.casefold() not in proving
    )
    windows = tuple(
        replace(candidate, controls=stripped)
        if candidate.auto_id == SALE_ORDER_ID
        else candidate
        for candidate in item.observation.windows
    )
    result = match(Observation(process="FACTERPNG.exe", windows=windows), table())
    assert result.state_id != "04_view"
    assert not result.known


def test_unread_controls_are_reported_as_pending_not_guessed() -> None:
    item = sample("04_view")
    windows = tuple(
        replace(candidate, controls=None)
        if candidate.auto_id == SALE_ORDER_ID
        else candidate
        for candidate in item.observation.windows
    )
    result = match(Observation(process="FACTERPNG.exe", windows=windows), table())
    assert result.state_id == UNKNOWN
    assert result.needs_controls
    assert result.pending is not None and result.pending.id == "04_view"


# ------------------------------------------------------------ windows it ignores --
def test_shadow_windows_alone_mean_the_app_is_starting() -> None:
    result = match(observation(window("", class_name="ShadowWindow")), table())
    assert result.state_id == STARTING


def test_the_menu_window_behind_the_form_does_not_decide_the_state() -> None:
    item = sample("04_view")
    result = match(item.observation, table())
    assert result.state_id == "04_view"
    assert "03_entry" in result.stack, "the menu behind the form should be in the stack"


def test_an_unfamiliar_window_on_top_is_a_popup() -> None:
    result = match(
        observation(
            window("FACT ERP.NG", "", class_name="#32770", controls=None),
            window("FACT ERP.NG", "frmMenu", z_order=1),
        ),
        table(),
    )
    assert result.state_id == POPUP
    assert "FACT ERP.NG" in result.message()


def test_main_menu_title_must_be_exact() -> None:
    spec = table().get("03_entry")
    assert spec.match.title_is == "FACT ERP.NG"

    result = match(
        observation(window("FACT ERP.NG - Reports", "frmMenu", controls=None)),
        table(),
    )
    assert result.state_id == POPUP


def test_a_window_without_controls_scans_as_a_known_state_when_identity_is_enough() -> None:
    result = match(
        observation(window("FACT ERP.NG", "frmMenu", controls=None)),
        table(),
    )
    assert result.state_id == "03_entry"


# ------------------------------------------------------------------- the table --
def test_normalise_folds_accelerators_and_spacing() -> None:
    assert normalise("&Save && &New") == "save new"
    assert normalise("  &Add ") == "add"
    assert normalise("USER NAME:") == "user name"
    assert normalise(None) == ""


def test_table_round_trips_through_json(tmp_path: Path) -> None:
    from desktop.state.states import load_state_table, save_state_table

    path = save_state_table(table(), tmp_path / "states.json")
    reloaded = load_state_table(path)
    assert reloaded.ids() == table().ids()
    assert reloaded.get("05_add").match.require == table().get("05_add").match.require


def test_table_rejects_duplicate_state_ids() -> None:
    spec = StateSpec(id="dup", match=MatchSpec(auto_id="x"))
    with pytest.raises(Exception):
        StateTable(process="FACTERPNG.exe", states=[spec, spec])


def test_table_rejects_an_unknown_schema() -> None:
    with pytest.raises(Exception):
        StateTable(process="FACTERPNG.exe", schema_version="states/999", states=[])


def test_state_needs_an_identity() -> None:
    with pytest.raises(Exception):
        MatchSpec()


def test_control_need_takes_one_key() -> None:
    with pytest.raises(Exception):
        ControlNeed(auto_id="x", name="y")
    with pytest.raises(Exception):
        ControlNeed()
    assert STATE_TABLE_SCHEMA == "states/1"
