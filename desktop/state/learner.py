"""Compile the state table from named snapshot folders.

``scripts/snapshots.py`` saves one folder per screen, named for the step you
were on: ``01_login_101400``, ``05_add_101539``. This module turns those
folders into ``config/states/fact_states.json``: for each screen, the identity
of its window and the controls that prove it is that screen and not a
neighbour drawn by the same window.

What it keeps, and what it throws away:

* an **automation id that is unique across your snapshots** is the best
  evidence there is, so it is preferred;
* a **name** is kept only when it carries no data - a label reading
  ``2083/03/32`` or ``SALES ORDER 2082/83 [Date Range ...]`` is a value, not a
  name, and would stop matching on the next record;
* layout scaffolding (``LayoutControlItem12``, ``System.Drawing.Bitmap``) is
  noise and never becomes evidence;
* when two screens are drawn by the same window (Sale Order View and Add both
  report automation id ``40200``) the learner adds **forbidden** controls, so
  the recognizer rejects the wrong screen even if its title were wrong.

Re-run it whenever you capture more screens; it overwrites the table and
verifies the result against every snapshot before reporting success.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.config import project_root
from desktop.state.recognizer import Observation, WindowView, match, normalise
from desktop.state.states import (
    ControlNeed,
    MatchSpec,
    StateSpec,
    StateTable,
    save_state_table,
    state_table_path,
)

# Window names that are layout plumbing rather than a screen's identity.
_NOISE_NAMES = re.compile(
    r"^(System\.Drawing|The XtraLayoutControl|LayoutControl|EmptySpaceItem|"
    r"GridColumn|DataItem|panelTitleBar|panelControl)",
    re.I,
)

# A label that holds data instead of a name: dates, amounts, counts, ranges.
_DATA_LIKE = re.compile(r"[0-9]|[/\\:@<>%()\[\]{}$]")

_STAMP = re.compile(r"_\d{4,8}$")


@dataclass(frozen=True)
class SnapshotSample:
    """One captured screen, ready to be learned from."""

    path: Path
    label: str
    state_id: str
    observation: Observation
    window: WindowView


def _state_id(label: str) -> str:
    """``05_add_101539`` -> ``05_add``: the name you gave the step."""
    return _STAMP.sub("", label) or label


def _choose_window(snapshot: dict) -> int | None:
    """The window the user pointed at: foreground, else picked, else fullest."""
    windows = snapshot.get("windows") or []
    best: tuple[tuple[int, int, int], int] | None = None
    for index, window in enumerate(windows):
        controls = len((window.get("uia") or {}).get("controls") or [])
        score = (
            1 if window.get("is_foreground") else 0,
            1 if window.get("is_picked") else 0,
            controls,
        )
        if best is None or score > best[0]:
            best = (score, index)
    if best is None or best[0][2] == 0:
        return None
    return best[1]


def load_snapshots(root: str | Path | None = None) -> list[SnapshotSample]:
    """Read every snapshot folder under ``root``, in name order."""
    folder = Path(root) if root is not None else project_root() / "scripts" / "snapshots"
    if not folder.is_absolute():
        folder = project_root() / folder
    samples: list[SnapshotSample] = []
    for path in sorted(folder.iterdir()):
        snapshot_file = path / "snapshot.json"
        if not path.is_dir() or not snapshot_file.exists():
            continue
        try:
            snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"cannot read {snapshot_file}: {exc}") from exc
        index = _choose_window(snapshot)
        if index is None:
            print(f"  skip {path.name}: no window with controls in it")
            continue
        observation = Observation.from_snapshot(snapshot)
        by_handle = {window.handle: window for window in observation.windows}
        handle = int(snapshot["windows"][index].get("hwnd") or 0)
        window = by_handle.get(handle)
        if window is None or window.controls is None:
            print(f"  skip {path.name}: the picked window has no control list")
            continue
        label = str(snapshot.get("label") or path.name)
        samples.append(
            SnapshotSample(
                path=path,
                label=label,
                state_id=_state_id(label),
                observation=observation,
                window=window,
            )
        )
    return samples


def _anchor_keys(window: WindowView) -> dict[tuple[str, str], tuple[str, str]]:
    """Every control that could serve as evidence, keyed by kind and value.

    Each entry carries what to write into the table (the original spelling, so
    the generated JSON stays readable) and the control type, which decides how
    good the evidence is: a button proves a screen, a progress bar does not.
    """
    keys: dict[tuple[str, str], tuple[str, str]] = {}
    for control in window.controls or ():
        if not control.visible:
            continue
        auto_id = control.auto_id.strip()
        if auto_id and not _NOISE_NAMES.search(auto_id):
            keys.setdefault(("id", auto_id.casefold()), (auto_id, control.type))
        name = control.name.strip()
        folded = normalise(name)
        if (
            folded
            and 3 <= len(folded) <= 40
            and not _DATA_LIKE.search(name)
            and not _NOISE_NAMES.search(name)
        ):
            keys.setdefault(("name", folded), (name, control.type))
    return keys


# Containers and layout scaffolding are present, but they say "a panel is
# here", not "this is the form": prefer a real control as evidence.
_CONTAINER_HINTS = ("panel", "group", "container", "layout", "splitter", "navbar")
_CONTROL_TYPES = frozenset(
    {"Button", "Edit", "ComboBox", "CheckBox", "RadioButton", "TabItem"}
)
_WEAK_TYPES = frozenset(
    {"ProgressBar", "Pane", "Group", "Custom", "Header", "HeaderItem", "Document"}
)


def _score(kind: str, value: str, control_type: str = "") -> int:
    score = 100 if kind == "id" else 60
    folded = value.casefold()
    if any(character.isalpha() for character in value):
        score += 20
    if len(value) <= 24:
        score += 5
    if value.isdigit():
        score -= 120
    if any(hint in folded for hint in _CONTAINER_HINTS):
        score -= 45
    if control_type in _CONTROL_TYPES:
        score += 30
    elif control_type in _WEAK_TYPES:
        score -= 35
    return score


def _rank(item: tuple[str, str, str]) -> tuple:
    """Best evidence first, with a stable order for equal scores."""
    kind, value, control_type = item
    return (-_score(kind, value, control_type), len(value), value.casefold())


def _need(kind: str, value: str) -> ControlNeed:
    return ControlNeed(auto_id=value) if kind == "id" else ControlNeed(name=value)


def _title_spec(state_id: str, titles: dict[str, str], auto_ids: dict[str, str]) -> dict:
    """Pick the cheapest title predicate that still tells this state apart."""
    title = titles[state_id]
    siblings = [
        other
        for other in titles
        if other != state_id and auto_ids[other] and auto_ids[other] == auto_ids[state_id]
    ]
    if siblings:
        # Same window draws several screens: keep only the part that differs.
        def tokens(value: str) -> list[str]:
            return [part.strip() for part in value.split(" - ")]

        group = [tokens(titles[state_id])] + [tokens(titles[other]) for other in siblings]
        common = 0
        while all(len(parts) > common for parts in group) and len(
            {parts[common].casefold() for parts in group}
        ) == 1:
            common += 1
        tail = tokens(titles[state_id])[common:]
        if tail:
            return {"title_ends_with": " - ".join(tail)}
        return {"title_is": title}

    # A title that is part of every other title (the main menu) must be exact,
    # or every screen would look like the menu.
    folded = title.casefold()
    if any(other != state_id and folded in titles[other].casefold() for other in titles):
        return {"title_is": title}
    return {"title_contains": title}


def learn(samples: list[SnapshotSample], process: str) -> StateTable:
    """Build the table: identity, proven controls, and rejected controls."""
    if not samples:
        raise SystemExit("no snapshots to learn from")

    titles = {sample.state_id: sample.window.title for sample in samples}
    auto_ids = {sample.state_id: sample.window.auto_id for sample in samples}
    anchors = {sample.state_id: _anchor_keys(sample.window) for sample in samples}

    states: list[StateSpec] = []
    for sample in samples:
        mine = set(anchors[sample.state_id])
        others: set[tuple[str, str]] = set()
        for other, keys in anchors.items():
            if other != sample.state_id:
                others |= set(keys)

        same_window = [
            other
            for other in anchors
            if other != sample.state_id
            and auto_ids[other]
            and auto_ids[other] == auto_ids[sample.state_id]
        ]

        # Controls are *evidence for a collision*, not decoration. A screen
        # whose window is already unique is recognised by its identity alone:
        # that keeps detection cheap and immune to widgets changing per user
        # (the main menu's dashboard buttons, for instance). Only screens drawn
        # by the same window - Sale Order View and Add - need control proof.
        require: list[ControlNeed] = []
        forbid: list[ControlNeed] = []
        if same_window:
            unique = [(kind, *anchors[sample.state_id][(kind, value)])
                      for kind, value in mine - others]
            unique.sort(key=_rank)
            require = [_need(kind, value) for kind, value, _type in unique[:3]]

            rejected: list[tuple[str, str, str]] = []
            for other in same_window:
                for key in set(anchors[other]) - mine:
                    kind, value, control_type = (key[0], *anchors[other][key])
                    rejected.append((kind, value, control_type))
            rejected.sort(key=_rank)
            forbid = [_need(kind, value) for kind, value, _type in rejected[:2]]

        spec = MatchSpec(
            auto_id=sample.window.auto_id,
            **(_title_spec(sample.state_id, titles, auto_ids)),
            require=require,
            forbid=forbid,
        )
        states.append(
            StateSpec(
                id=sample.state_id,
                label=sample.state_id,
                match=spec,
                provisional=True,
                samples=1,
                control_count=len(sample.window.controls or ()),
            )
        )

    source = samples[0].path.parent
    try:
        source_text = str(source.relative_to(project_root()))
    except ValueError:
        source_text = str(source)

    return StateTable(
        process=process,
        generated_from=source_text.replace("\\", "/"),
        generated_at=datetime.now().isoformat(timespec="seconds"),
        states=states,
    )


def verify(table: StateTable, samples: list[SnapshotSample]) -> list[str]:
    """Re-recognise every snapshot; return the problems, if any."""
    problems: list[str] = []
    for sample in samples:
        result = match(sample.observation, table)
        if result.state_id != sample.state_id:
            problems.append(
                f"{sample.label}: recognised as {result.state_id!r}, "
                f"expected {sample.state_id!r} ({result.message()})"
            )
    return problems


def _process_name(samples: list[SnapshotSample]) -> str:
    for sample in samples:
        if sample.observation.process:
            return sample.observation.process
    return "FACTERPNG.exe"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Learn config/states/fact_states.json from snapshot folders"
    )
    parser.add_argument(
        "--root",
        default=None,
        help="snapshot folder (default: scripts/snapshots)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="state table to write (default: config/states/fact_states.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the table without writing it",
    )
    args = parser.parse_args(argv)

    samples = load_snapshots(args.root)
    if not samples:
        print("no snapshots found; capture some with scripts/snapshots.py first")
        return 1
    table = learn(samples, _process_name(samples))

    print(f"Learned {len(table.states)} state(s) from {len(samples)} snapshot(s):\n")
    for state in table.states:
        print(f"  {state.id:14} {state.match.describe()}")
        if state.samples < 2:
            note = "provisional: captured once"
            print(f"  {'':14} {note}")

    problems = verify(table, samples)
    if problems:
        print("\nThe table does not recognise its own snapshots:")
        for problem in problems:
            print(f"  FAIL {problem}")
        return 1
    print("\nVerified: every snapshot is recognised as its own state.")

    payload = json.dumps(
        table.model_dump(mode="json", exclude_none=True, by_alias=True),
        indent=2,
        ensure_ascii=False,
    )
    if args.dry_run:
        print("\n" + payload)
        return 0
    written = save_state_table(table, args.out or state_table_path())
    print(f"Wrote {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
