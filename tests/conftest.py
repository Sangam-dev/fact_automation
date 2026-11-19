"""Shared pytest setup."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture()
def valid_config() -> dict:
    """A minimal configuration that should always validate."""
    return {
        "app": {"title_contains": "Test Form"},
        "fields": [
            {"name": "name", "field": "Customer Name", "type": "text"},
            {"name": "amount", "field": "Amount", "type": "number"},
        ],
        "actions": {"save_button": "Save"},
        "gemini": {"model": "gemini-3.6-flash"},
        "safety": {"max_attempts": 3},
    }
