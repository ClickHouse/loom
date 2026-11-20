"""Anonymous identity spellings bind projections to their parent and keep readable keys."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.memory.app import chunk_projection


async def _project(monkeypatch, user, session):
    ch = SimpleNamespace(insert=AsyncMock())

    async def embedded(text, ranges, chunks, embed):
        return [SimpleNamespace(written=True, vector=[1.0, 0.0]) for _ in chunks]

    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr("server.embedding.get_embedder", lambda: SimpleNamespace(model_name="fake"))
    monkeypatch.setattr(chunk_projection, "embed_chunks_of", embedded)
    parent = SimpleNamespace(org="o", namespace="n", agent="a", user_id=user, session_id=session)
    await chunk_projection.write_memory_chunks(
        parent,
        [{"role": "user", "content": "A conversation passage linked to a durable parent memory."}],
        parent_memory_keys=["parent"],
    )
    call = next(c for c in ch.insert.call_args_list if c.args[0] == "loom.memory_chunks")
    assert call.args[1]
    return call.args[1]


@pytest.mark.parametrize("user,session", [("", "s"), ("u", "s"), ("", "-"), ("-", "-")])
async def test_chunk_projection_keeps_explicit_sessions(monkeypatch, user, session):
    rows = await _project(monkeypatch, user, session)
    assert {(r["agent"], r["user_id"], r["session_id"], r["parent_memory_key"]) for r in rows} == {
        ("a", user or "-", session, "parent")
    }


@pytest.mark.parametrize("second_user", ["first", "second"])
async def test_blank_sessions_keep_distinct_readable_chunk_identities(monkeypatch, second_user):
    from uuid import UUID

    from server.memory.app.handlers import split_chunk_key
    from server.memory.app.retrieval.chunks import _chunk_anchor_keys

    first = (await _project(monkeypatch, "first", ""))[0]
    second = (await _project(monkeypatch, second_user, ""))[0]
    sessions = [first["session_id"], second["session_id"]]
    assert len(set(sessions)) == 2
    for row in [first, second]:
        sid = row["session_id"]
        assert UUID(sid).hex == sid
        assert split_chunk_key(f"chunk:{sid}:{row['chunk_index']}") == (sid, row["chunk_index"])
        assert _chunk_anchor_keys([row]) == [(sid, row["chunk_index"])]
