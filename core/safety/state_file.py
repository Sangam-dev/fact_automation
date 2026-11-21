"""The safety state on disk, so a helper process can read it.

A ``run`` step starts another Python process (the form filler).  It cannot see
the controller object, so the state the hotkeys change is written to
``data/state/safety.json`` and that process polls the file: Alt+P really does
hold it still, and Alt+T really does stop it.

The file is written through a temporary file and ``os.replace``, so a reader
never sees half a document.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from core.config import project_root

logger = logging.getLogger(__name__)


def state_path() -> Path:
    """Where the current safety state lives."""
    return project_root() / "data" / "state" / "safety.json"


def write_state(state: str, reason: str = "") -> None:
    """Record the current state. Never raises: a missing file only costs the hint."""
    path = state_path()
    value = getattr(state, "value", state)
    document = {
        "state": str(value),
        "reason": reason,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    temp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(document, indent=2), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        logger.debug("could not write the safety state file", exc_info=True)
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
