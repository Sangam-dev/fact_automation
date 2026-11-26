"""The pre-written scripts: what the wizard offers, and the step it becomes.

Everything in ``execution/scripts/`` is a deterministic automation somebody can
insert into a plan, so the folder itself is the library - no registration, no
list to keep in step.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.automation.model import Automation
from core.automation.scripts import (
    SCRIPTS_DIR,
    ScriptError,
    find_scripts,
    get_script,
    script_step,
)


def _write(root: Path, name: str, body: str) -> Path:
    folder = root.joinpath(*SCRIPTS_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(body, encoding="utf-8")
    return path


def test_every_script_in_the_folder_is_offered_with_what_it_does(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "fill_grid.py",
        '"""Fills the grid sideways.\n\nLonger explanation."""\n',
    )
    _write(tmp_path, "_helper.py", "x = 1\n")
    _write(tmp_path, "notes.txt", "not a script\n")

    entries = find_scripts(tmp_path)

    assert [entry.id for entry in entries] == ["fill_grid"]
    assert entries[0].path == "execution/scripts/fill_grid.py"
    assert entries[0].summary == "Fills the grid sideways."
    assert entries[0].describe() == "fill_grid - Fills the grid sideways."


def test_a_script_that_cannot_be_parsed_is_still_offered(tmp_path: Path) -> None:
    _write(tmp_path, "broken.py", "def oops(:\n")

    entries = find_scripts(tmp_path)

    assert [entry.id for entry in entries] == ["broken"]
    assert entries[0].summary == ""


def test_a_long_docstring_is_shortened_for_the_menu(tmp_path: Path) -> None:
    _write(tmp_path, "wordy.py", f'"""{"x" * 300}"""\n')

    described = find_scripts(tmp_path)[0].describe()

    assert described.endswith("...")
    assert len(described.split(" - ", 1)[1]) <= 100


def test_no_folder_means_no_scripts(tmp_path: Path) -> None:
    assert find_scripts(tmp_path) == []


def test_an_unknown_script_names_the_ones_there_are(tmp_path: Path) -> None:
    _write(tmp_path, "fill_grid.py", "")

    with pytest.raises(ScriptError, match="fill_grid"):
        get_script("missing", tmp_path)


def test_a_script_becomes_a_run_step_with_the_runs_own_values_file(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "fill_grid.py", "")

    step = script_step(get_script("fill_grid", tmp_path))

    assert step == {
        "action": "run",
        "script": "execution/scripts/fill_grid.py",
        "args": ["{values_file}"],
        "timeout_seconds": 900.0,
    }


def test_the_step_it_makes_is_one_a_plan_can_hold(tmp_path: Path) -> None:
    _write(tmp_path, "fill_grid.py", "")

    plan = Automation.model_validate(
        {
            "name": "sale_order",
            "app": {"process_name": "FACTERPNG.exe"},
            "fields": [],
            "steps": [
                {"id": "open", "action": "click", "button": "&Add",
                 "checkpoint": {"state": "04_view", "resume": "replay"}},
                {
                    **script_step(get_script("fill_grid", tmp_path)),
                    "id": "fill",
                    "checkpoint": {"state": "05_add", "resume": "restart"},
                },
                {"id": "next", "action": "click", "button": "Freight",
                 "checkpoint": {"state": "05_add", "resume": "replay"}},
            ],
            "save": {"button": "Save", "verifier": "none"},
        }
    )

    assert [step.action for step in plan.steps] == ["click", "run", "click"]
    assert plan.steps[1].script == "execution/scripts/fill_grid.py"


def test_the_projects_own_filler_is_in_the_library() -> None:
    # The pre-written fillers live in one folder, and the menu is that folder.
    scripts = find_scripts()

    script_ids = [entry.id for entry in scripts]
    assert "form_filler_1" in script_ids
    assert "remarks_filler" in script_ids
    assert "udf_filler" in script_ids
    assert "_form_filler_template" not in script_ids
    assert get_script("form_filler_1").path == "execution/scripts/form_filler_1.py"
    remarks_script = get_script("remarks_filler")
    assert remarks_script.path == "execution/scripts/remarks_filler.py"
    assert "Remarks" in remarks_script.summary
    udf_script = get_script("udf_filler")
    assert udf_script.path == "execution/scripts/udf_filler.py"
    assert "Global UDF" in udf_script.summary
