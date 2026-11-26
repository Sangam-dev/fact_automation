from __future__ import annotations

import pytest

from execution.scripts.remarks_filler import Stop, read_remarks


def test_reads_top_level_remarks() -> None:
    assert read_remarks({"remarks": "Deliver before month end"}) == "Deliver before month end"


def test_reads_remarks_from_header() -> None:
    assert read_remarks({"header": {"remarks": "Call on arrival"}}) == "Call on arrival"


def test_rejects_conflicting_remarks_sources() -> None:
    with pytest.raises(Stop, match="contain different values"):
        read_remarks(
            {
                "remarks": "Top-level value",
                "header": {"remarks": "Different header value"},
            }
        )


def test_rejects_missing_or_empty_remarks() -> None:
    with pytest.raises(Stop, match="missing required JSON value"):
        read_remarks({"header": {}})
    with pytest.raises(Stop, match="must not be empty"):
        read_remarks({"remarks": "  "})
