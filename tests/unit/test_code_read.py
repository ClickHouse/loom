from __future__ import annotations

import pytest

from server.code.app.read import _parse_range
from server.code.domain.symbol_body import retained_source_ranges
from server.core.errors import ValidationError


def test_retained_ranges_report_revision_disagreement_as_gap() -> None:
    rows = [
        {"start_line": 1, "end_line": 4, "content": "a\nb\nc\nd", "indexed_commit": "current"},
        {"start_line": 2, "end_line": 2, "content": "older\n", "indexed_commit": "old"},
    ]
    assert retained_source_ranges(rows, 1, 4, revision="current") == [
        (1, 1, "a\n"),
        (3, 3, "c\n"),
    ]
    assert retained_source_ranges(rows, 3, 3, revision="current") == [(3, 3, "c\n")]


def test_retained_ranges_omit_a_truncated_header_line() -> None:
    rows = [
        {
            "kind": "file_header",
            "start_line": 1,
            "end_line": 1,
            "content": "//" + "x" * 5998,
            "indexed_commit": "current",
        },
    ]
    assert retained_source_ranges(rows, 1, 1, revision="current") == []


@pytest.mark.parametrize("value", ["../file.py:2", "/file.py:1", "file.py:0", "file.py:9-2"])
def test_read_refuses_invalid_ranges(value: str) -> None:
    with pytest.raises(ValidationError):
        _parse_range(value)
