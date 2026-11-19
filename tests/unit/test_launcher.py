"""The launcher script: its text, its interpreter, and where it is written."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.automation import registry
from core.automation.launcher import (
    LAUNCHER_NAME,
    launcher_for,
    launcher_path,
    render_launcher,
    write_launcher,
)
from core.automation.model import Automation
from core.automation.registry import AutomationNameError

NAME = "fact_automation"


def automation() -> Automation:
    return Automation.model_validate(
        {
            "name": NAME,
            "app": {"title_contains": "FACT ERP.NG", "process_name": "FACTERPNG.exe"},
            "fields": [{"name": "customer", "field": "Customer Name"}],
            "steps": [
                {
                    "id": "01-open",
                    "action": "click",
                    "button": "&Add",
                    "checkpoint": {"state": "04_view", "resume": "replay"},
                }
            ],
            "save": {"button": "Save", "verifier": "row", "footprint": ["customer"]},
        }
    )


# ----------------------------------------------------------------- rendering --
def test_the_launcher_runs_the_registered_automation(tmp_path: Path) -> None:
    text = render_launcher(NAME, project=tmp_path, python_exe="python")

    assert text.startswith("@echo off")
    assert f"automation run {NAME} --values" in text
    assert '"cli.py"' in text


def test_the_launcher_uses_crlf_so_cmd_does_not_trip_over_line_endings(
    tmp_path: Path,
) -> None:
    text = render_launcher(NAME, project=tmp_path, python_exe="python")
    assert "\r\n" in text
    assert "\n" not in text.replace("\r\n", "")


def test_the_launcher_runs_from_the_project_folder(tmp_path: Path) -> None:
    text = render_launcher(NAME, project=tmp_path, python_exe="python")
    assert f'cd /d "{tmp_path}"' in text


def test_the_launcher_prefers_the_projects_own_python(tmp_path: Path) -> None:
    venv = tmp_path / "myvenv" / "Scripts" / "python.exe"
    venv.parent.mkdir(parents=True)
    venv.write_bytes(b"")

    text = render_launcher(NAME, project=tmp_path)
    assert f"set \"PY={venv}\"" in text


def test_the_launcher_falls_back_to_the_path_when_there_is_no_venv(
    tmp_path: Path,
) -> None:
    text = render_launcher(NAME, project=tmp_path)
    assert 'set "PY=python"' in text
    assert 'if not exist "%PY%" set "PY=python"' in text


def test_a_missing_values_file_is_explained_rather_than_run(tmp_path: Path) -> None:
    text = render_launcher(NAME, project=tmp_path, python_exe="python")
    assert 'set "VALUES=%~1"' in text
    assert 'set "VALUES=%~dp0values.json"' in text
    assert "no values file" in text
    # the check comes before the run, so no half-run happens with no record data
    assert text.index("no values file") < text.index("automation run")


def test_the_launcher_waits_only_when_double_clicked(tmp_path: Path) -> None:
    text = render_launcher(NAME, project=tmp_path, python_exe="python")
    assert 'if "%~1"=="" pause' in text
    assert "\npause\n" not in text.replace("\r\n", "\n")


def test_the_launcher_reports_the_exit_code_and_hands_it_back(tmp_path: Path) -> None:
    text = render_launcher(NAME, project=tmp_path, python_exe="python")
    assert 'set "CODE=%ERRORLEVEL%"' in text
    assert "0 saved, 1 failed or unconfirmed, 2 blocked, 3 stopped" in text
    assert "endlocal & exit /b %CODE%" in text


def test_the_launcher_is_ascii_so_cmd_can_read_it(tmp_path: Path) -> None:
    text = render_launcher(NAME, project=tmp_path, python_exe="python")
    text.encode("ascii")


def test_an_invalid_name_is_refused() -> None:
    with pytest.raises(AutomationNameError):
        render_launcher("Not Valid")


# ------------------------------------------------------------------- writing --
def test_the_launcher_sits_beside_its_automation(tmp_path: Path) -> None:
    path = launcher_path(NAME, tmp_path / "automations")
    assert path == tmp_path / "automations" / NAME / LAUNCHER_NAME


def test_writing_creates_the_folder_and_can_be_repeated(tmp_path: Path) -> None:
    root = tmp_path / "automations"
    first = write_launcher(NAME, project=tmp_path, automations_root=root, python_exe="python")

    assert first.is_file()
    assert first.read_bytes().count(b"\r\n") > 5
    assert b"\n" not in first.read_bytes().replace(b"\r\n", b"")

    second = write_launcher(NAME, project=tmp_path, automations_root=root, python_exe="python")
    assert second == first
    assert second.read_text(encoding="ascii").count("@echo off") == 1


def test_launcher_for_says_when_there_is_none_yet(tmp_path: Path) -> None:
    root = tmp_path / "automations"
    assert launcher_for(NAME, root) is None

    write_launcher(NAME, project=tmp_path, automations_root=root, python_exe="python")
    found = launcher_for(NAME, root)
    assert found is not None and found.is_file()


def test_writing_a_launcher_for_a_missing_automation_is_harmless(tmp_path: Path) -> None:
    # the launcher is inert text; the run itself is what refuses
    path = write_launcher("other_one", project=tmp_path, automations_root=tmp_path / "a", python_exe="python")
    assert path.is_file()
    assert registry.exists("other_one", tmp_path / "a") is False
