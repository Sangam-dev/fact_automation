"""Learned ways out of screens a plan did not expect.

The plan knows the screens it walks through. FACT does not always stay on them:
a required field is empty, a date is out of range, a confirmation appears. Today
the run stops there, which is correct but not enough - the same dialog will stop
the next run too.

This module is the memory for those cases. A **rule** is a small, readable
record:

    a trigger  - which window this applies to (title, class, the controls that
                 must be present and the ones that must not), so a similar
                 screen cannot trigger it by accident;
    actions    - what to click or press, in order;
    expect     - the screen that must be showing afterwards.

The trigger is the important part. A rule matched on "looks like a dialog" would
eventually click OK on something that matters, so every required signal is a
condition and one missing signal means the rule does not apply.

Rules live beside the automation they belong to, in
``config/automations/<name>/exceptions.json``. Nothing here calls a model: a
rule learned once is applied by the same deterministic runner that does the rest.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.config import project_root

EXCEPTIONS_SCHEMA = "exceptions/1"
RULE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class RuleError(RuntimeError):
    """The rules for an automation cannot be read, written or used."""


@dataclass(frozen=True)
class Observation:
    """What the screen looks like when a rule would be tried."""

    state: str = ""
    title: str = ""
    class_name: str = ""
    auto_ids: tuple[str, ...] = ()


def _fold(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


class Trigger(BaseModel):
    """The conditions a screen must meet for a rule to apply.

    Every field that is set is required; an empty field is not checked. That is
    what keeps a learned "click OK" from firing on the next screen that happens
    to have an OK button.
    """

    model_config = ConfigDict(extra="forbid")

    #: A recogniser answer: ``POPUP``, ``UNKNOWN``, or a learned state id.
    state: str = ""
    title_contains: str = ""
    class_contains: str = ""
    #: Every one of these automation ids must be on the screen.
    auto_ids: list[str] = Field(default_factory=list)
    #: None of these may be on the screen.
    absent_auto_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_a_signal(self) -> "Trigger":
        if not (
            self.state
            or self.title_contains
            or self.class_contains
            or self.auto_ids
            or self.absent_auto_ids
        ):
            raise ValueError(
                "a trigger needs at least one signal (state, title, class or a "
                "control id); a rule that matches everything is a rule that "
                "clicks the wrong button"
            )
        return self

    def matches(self, seen: Observation) -> bool:
        """True only when every signal agrees; open questions are not guesses."""
        if self.state and _fold(self.state) != _fold(seen.state):
            return False
        if self.title_contains and _fold(self.title_contains) not in _fold(seen.title):
            return False
        if self.class_contains and _fold(self.class_contains) not in _fold(
            seen.class_name
        ):
            return False
        present = {_fold(item) for item in seen.auto_ids}
        if any(_fold(item) not in present for item in self.auto_ids):
            return False
        if any(_fold(item) in present for item in self.absent_auto_ids):
            return False
        return True

    def describe(self) -> str:
        parts = [f"state {self.state}"] if self.state else []
        if self.title_contains:
            parts.append(f"title contains {self.title_contains!r}")
        if self.class_contains:
            parts.append(f"class contains {self.class_contains!r}")
        if self.auto_ids:
            parts.append("has " + ", ".join(self.auto_ids))
        if self.absent_auto_ids:
            parts.append("lacks " + ", ".join(self.absent_auto_ids))
        return "; ".join(parts)


class RuleAction(BaseModel):
    """One interaction a rule performs before the plan continues."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["click", "double_click", "key"]
    button: str = ""
    keys: str = ""

    @model_validator(mode="after")
    def _one_target(self) -> "RuleAction":
        if self.action == "key":
            keys = self.keys.strip()
            if not keys:
                raise ValueError("a key action needs the keys to press")
            if self.button:
                raise ValueError("a key action has no button")
            object.__setattr__(self, "keys", keys)
            return self
        button = self.button.strip()
        if not button:
            raise ValueError(f"a {self.action} action needs a button name")
        if self.keys:
            raise ValueError(f"a {self.action} action has no keys")
        object.__setattr__(self, "button", button)
        return self

    def describe(self) -> str:
        return (
            f"press {self.keys!r}"
            if self.action == "key"
            else f"{self.action.replace('_', '-')} {self.button!r}"
        )


class Rule(BaseModel):
    """One learned situation: when this is on screen, do that."""

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str = ""
    when: Trigger
    actions: list[RuleAction] = Field(min_length=1)
    #: The state that must be showing after the actions; empty means "the state
    #: the step was waiting for".
    expect: str = ""
    learned_at: str = ""

    @model_validator(mode="after")
    def _clean_id(self) -> "Rule":
        self.id = self.id.strip()
        if not RULE_ID.match(self.id):
            raise ValueError(
                "rule ids are lower case letters, digits, '-' and '_': "
                f"{self.id!r}"
            )
        return self

    def describe(self) -> str:
        steps = ", ".join(action.describe() for action in self.actions)
        after = f" -> {self.expect}" if self.expect else ""
        detail = f" ({self.description})" if self.description else ""
        return f"{self.id}{detail}: when {self.when.describe()}, {steps}{after}"


class RuleTable(BaseModel):
    """Every rule learned for one automation."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: str = Field(default=EXCEPTIONS_SCHEMA, alias="schema")
    automation: str = ""
    rules: list[Rule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> "RuleTable":
        if self.schema_version != EXCEPTIONS_SCHEMA:
            raise ValueError(
                f"unsupported exceptions schema {self.schema_version!r}; "
                f"expected {EXCEPTIONS_SCHEMA!r}"
            )
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"rule id {rule.id!r} is used twice")
            seen.add(rule.id)
        return self

    def ids(self) -> list[str]:
        return [rule.id for rule in self.rules]

    def get(self, rule_id: str) -> Rule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise RuleError(
            f"there is no rule {rule_id!r}; learned: " + (", ".join(self.ids()) or "(none)")
        )

    def find(self, seen: Observation) -> Rule | None:
        """The first rule whose every signal matches this screen."""
        for rule in self.rules:
            if rule.when.matches(seen):
                return rule
        return None

    def add(self, rule: Rule) -> "RuleTable":
        """Replace a rule with the same id, or append; the newer one wins."""
        object.__setattr__(
            self, "rules", [item for item in self.rules if item.id != rule.id] + [rule]
        )
        return self

    def remove(self, rule_id: str) -> "RuleTable":
        self.get(rule_id)
        object.__setattr__(
            self, "rules", [item for item in self.rules if item.id != rule_id]
        )
        return self


def rules_path(name: str, automations_root: str | Path | None = None) -> Path:
    """Where one automation's learned rules live (beside its ``automation.json``)."""
    base = (
        Path(automations_root)
        if automations_root is not None
        else project_root() / "config" / "automations"
    )
    if not base.is_absolute():
        base = project_root() / base
    return base / name / "exceptions.json"


def load_rules(
    name: str, automations_root: str | Path | None = None
) -> RuleTable | None:
    """Read an automation's rules; ``None`` when it has learned none yet."""
    path = rules_path(name, automations_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuleError(f"cannot read {path}: {exc}") from exc
    try:
        table = RuleTable.model_validate(data)
    except Exception as exc:
        raise RuleError(f"{path} is not a valid rule file: {exc}") from exc
    if table.automation and table.automation != name:
        raise RuleError(
            f"{path} belongs to {table.automation!r}, not {name!r}"
        )
    return table


def save_rules(
    table: RuleTable, automations_root: str | Path | None = None
) -> Path:
    """Write the rules atomically, so a crash cannot leave half a file."""
    if not table.automation:
        raise RuleError("a rule table must name the automation it belongs to")
    path = rules_path(table.automation, automations_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = table.model_dump(mode="json", by_alias=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return path


def describe_rules(table: RuleTable | None) -> list[str]:
    """One readable line per learned rule."""
    if table is None or not table.rules:
        return ["(no learned rules yet)"]
    return [rule.describe() for rule in table.rules]


def rule_from_screen(
    rule_id: str,
    seen: Observation,
    actions: Iterable[RuleAction],
    *,
    expect: str = "",
    description: str = "",
    when: datetime | None = None,
) -> Rule:
    """Turn "this screen, right now" into a rule, with its whole identity.

    The trigger records the state, the exact window title, the window class and
    the automation ids on screen, so it is specific to the situation that needed
    help. ``absent_auto_ids`` is left empty: a person can add a "not this"
    condition by editing the JSON, and guessing one here would make the rule
    silently stop matching.
    """
    actions = list(actions)
    if not actions:
        raise RuleError("a rule needs at least one action")
    auto_ids = [
        item for item in dict.fromkeys(seen.auto_ids) if item
    ][:12]
    trigger = Trigger(
        state=seen.state if seen.state in ("POPUP", "UNKNOWN") else "",
        title_contains=seen.title.strip(),
        class_contains=seen.class_name.strip(),
        auto_ids=auto_ids,
    )
    return Rule(
        id=rule_id,
        description=description.strip(),
        when=trigger,
        actions=actions,
        expect=expect.strip(),
        learned_at=(when or datetime.now()).isoformat(timespec="seconds"),
    )


__all__ = [
    "EXCEPTIONS_SCHEMA",
    "Observation",
    "RULE_ID",
    "Rule",
    "RuleAction",
    "RuleError",
    "RuleTable",
    "Trigger",
    "describe_rules",
    "load_rules",
    "rule_from_screen",
    "rules_path",
    "save_rules",
]
