"""The `promoted_from` entry: built and parsed back, a key holding `/` included."""

from __future__ import annotations

import pytest

from server.memory.domain.promotion_source import parse_promotion_source, promotion_source


@pytest.mark.parametrize("key", ["arr_note", "definition/arr", "a/b/c"])
def test_an_entry_parses_back_to_its_fields(key: str) -> None:
    entry = promotion_source("o", "n", "bot", "-", "-", key)
    assert parse_promotion_source(entry) == {
        "org": "o",
        "namespace": "n",
        "agent": "bot",
        "user_id": "-",
        "session_id": "-",
        "memory_key": key,
    }


@pytest.mark.parametrize("entry", ["", "o/n/a/u", "o/n/a/u/s/"])
def test_a_malformed_entry_is_none(entry: str) -> None:
    assert parse_promotion_source(entry) is None


def test_an_identity_part_holding_a_slash_is_read_into_the_key() -> None:
    # The format cannot tell, so the key takes everything after the fifth `/`.
    parsed = parse_promotion_source("o/n/-/-/run/7/definition/arr")
    assert parsed is not None
    assert (parsed["session_id"], parsed["memory_key"]) == ("run", "7/definition/arr")
