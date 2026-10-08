"""A pagination cursor round-trips; one the server did not issue is a 422, not a 500."""

from __future__ import annotations

import base64
from datetime import datetime

import pytest

from server.core.cursors import decode_cursor, encode_cursor
from server.core.errors import ValidationError


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def test_round_trip() -> None:
    ts = datetime(2026, 3, 1, 12, 0, 0, 123456)
    assert decode_cursor(encode_cursor(ts, "req|with-pipe")) == (ts, "req|with-pipe")


@pytest.mark.parametrize(
    "cursor",
    [
        "abc",  # bad padding
        "not-a-cursor!",  # not base64
        "é",  # not ASCII
        "",  # empty timestamp
        _b64(b"notadate|x"),  # decodes, timestamp does not parse
        _b64(b"\xff\xfe"),  # decodes, not UTF-8
    ],
)
def test_malformed_cursor_is_a_validation_error(cursor: str) -> None:
    with pytest.raises(ValidationError) as ei:
        decode_cursor(cursor)
    assert ei.value.status_code == 422
    assert ei.value.code == "VALIDATION_FAILED"
