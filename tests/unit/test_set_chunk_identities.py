"""The chunks a redaction keeps for a live head are those `write_set_chunks` wrote for it:
`set_chunk_identities` of the value read back from its stored column, planned as far as
the chunks written for it."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from server.memory.app.set_chunks import plan_set_chunks, set_chunk_identities
from server.memory.store.rows import parse_memory_value, serialize_value
from server.settings import settings

_LONG = "\n\n".join(f"Paragraph {i}: the incident notes run on. " * 12 for i in range(40))


@pytest.mark.parametrize(
    "value", [_LONG, {"summary": _LONG, "tags": ["a", "b"]}], ids=["text", "object"]
)
def test_the_stored_value_names_the_chunks_written_for_it(value: Any) -> None:
    written = set_chunk_identities(value, "semantic", "k")
    assert written
    assert (
        set_chunk_identities(parse_memory_value(serialize_value(value)), "semantic", "k") == written
    )


def test_a_plan_reaches_the_chunks_written_under_a_higher_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redaction and search judge a live head's chunks by these identities: with the cap
    lowered since the head was written, a plan asked to reach its stored chunks names
    them all, and one asked for nothing more stops at today's cap."""
    monkeypatch.setattr(settings, "memory_set_chunk_max_chunks", 48)
    planned, _ = plan_set_chunks(_LONG, "semantic", "k")
    written = {(i, hashlib.sha256(c.encode()).hexdigest()) for i, (c, _s, _e) in enumerate(planned)}
    assert len(written) > 4
    monkeypatch.setattr(settings, "memory_set_chunk_max_chunks", 4)
    assert len(set_chunk_identities(_LONG, "semantic", "k")) == 4
    assert set_chunk_identities(_LONG, "semantic", "k", at_least=len(written)) == written
