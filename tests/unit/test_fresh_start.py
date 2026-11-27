from __future__ import annotations


import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop.app import launch
from desktop.app.launch import AppPaths, LaunchError
from desktop.state.recognizer import POPUP, UNKNOWN
from desktop.state.states import StateTable


TABLE = StateTable.model_validate(
    {
        "process": "FACT.exe",
        "states": [
            {
                "id": "login",
                "match": {"auto_id": "frmLogin", "title_is": "Login"},
            },
            {
                "id": "order",
                "match": {"auto_id": "frmOrder", "title_is": "Order"},
            },
        ],
    }
)


def _paths(exe: Path) -> AppPaths:
    return AppPaths(process="FACT.exe", exe_path=str(exe))


def test_known_registered_windows_are_kept(monkeypatch, tmp_path) -> None:
    exe = tmp_path / "FACT.exe"
    exe.write_bytes(b"test")
    window = SimpleNamespace(handle=10)
    terminated = []
    monkeypatch.setattr(launch, "_visible_process_windows", lambda _process: [window])
    monkeypatch.setattr(launch, "_inspect_process_windows", lambda _process, _table: [])
    monkeypatch.setattr(
        launch,
        "_terminate_processes",
        lambda *_args, **_kwargs: terminated.append(True),
    )

    restarted = launch.restart_if_unregistered(_paths(exe), TABLE, output=lambda _: None)

    assert restarted is False
    assert terminated == []


@pytest.mark.parametrize("state_id", [POPUP, UNKNOWN])
def test_unknown_active_state_forces_process_restart(monkeypatch, tmp_path, state_id) -> None:
    exe = tmp_path / "FACT.exe"
    exe.write_bytes(b"test")
    window = SimpleNamespace(handle=10)
    terminated = []
    monkeypatch.setattr(launch, "_visible_process_windows", lambda _process: [window])
    monkeypatch.setattr(
        launch,
        "_inspect_process_windows",
        lambda _process, _table: [f"state is {state_id}"],
    )
    monkeypatch.setattr(
        launch,
        "_terminate_processes",
        lambda paths, windows, *, output: terminated.append((paths, windows)),
    )

    restarted = launch.restart_if_unregistered(_paths(exe), TABLE, output=lambda _: None)

    assert restarted is True
    assert terminated == [(_paths(exe), [window])]


def test_unknown_state_without_launch_path_does_not_kill(monkeypatch, tmp_path) -> None:
    window = SimpleNamespace(handle=10)
    monkeypatch.setattr(launch, "_visible_process_windows", lambda _process: [window])
    monkeypatch.setattr(
        launch,
        "_inspect_process_windows",
        lambda _process, _table: ["unexpected dialog"],
    )
    monkeypatch.setattr(
        launch,
        "_terminate_processes",
        lambda *_args, **_kwargs: pytest.fail("must not kill without a restart path"),
    )

    with pytest.raises(LaunchError, match="does not exist"):
        launch.restart_if_unregistered(
            AppPaths(process="FACT.exe", exe_path=str(tmp_path / "missing.exe")),
            TABLE,
            output=lambda _: None,
        )


def test_force_termination_targets_only_verified_pid(monkeypatch, tmp_path) -> None:
    exe = tmp_path / "FACT.exe"
    exe.write_bytes(b"test")
    commands = []
    fake_process = SimpleNamespace(
        GetWindowThreadProcessId=lambda _hwnd: (0, 4321)
    )
    monkeypatch.setitem(sys.modules, "win32process", fake_process)
    monkeypatch.setattr(
        "desktop.windows.window.process_path", lambda _pid: str(exe)
    )
    monkeypatch.setattr(
        launch.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(command)
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(launch, "_visible_process_windows", lambda _process: [])

    launch._terminate_processes(
        _paths(exe), [SimpleNamespace(handle=987)], output=lambda _: None
    )

    assert commands == [["taskkill", "/PID", "4321", "/F"]]
