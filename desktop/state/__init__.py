"""Recognising which screen the target application is showing (Segment 3)."""

from __future__ import annotations

from desktop.state.recognizer import (
    AppState,
    ControlView,
    Observation,
    StateMatch,
    WindowView,
    detect,
    match,
    observe,
)
from desktop.state.states import (
    STATE_TABLE_SCHEMA,
    ControlNeed,
    MatchSpec,
    StateSpec,
    StateTable,
    load_state_table,
    save_state_table,
    state_table_path,
)

__all__ = [
    "STATE_TABLE_SCHEMA",
    "AppState",
    "ControlNeed",
    "ControlView",
    "MatchSpec",
    "Observation",
    "StateMatch",
    "StateSpec",
    "StateTable",
    "WindowView",
    "detect",
    "load_state_table",
    "match",
    "observe",
    "save_state_table",
    "state_table_path",
]
