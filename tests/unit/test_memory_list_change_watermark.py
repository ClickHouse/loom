"""Memory.list carries a change watermark.

The Memories view refetches a 100-row page, a total and a today-count under
FINAL every 8 s. The answer carries the namespace's newest version; a caller that sends it back gets `unchanged`
with no rows when the namespace has not moved — one small read."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from server.memory import store as memory_repo


class _Calls:
    version = 0
    lists = 0
    counts = 0


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch):
    from server.memory.app.query import reset_list_counts_cache

    reset_list_counts_cache()
    _Calls.version, _Calls.lists, _Calls.counts = 0, 0, 0

    async def namespace_version(org: str, ns: str, **kw: Any) -> int:
        _Calls.version += 1
        return 4200

    async def list_by_namespace(identity: Any, **kw: Any) -> list[Any]:
        _Calls.lists += 1
        return []

    async def count_by_namespace(identity: Any, **kw: Any) -> int:
        _Calls.counts += 1
        return 0

    monkeypatch.setattr(memory_repo, "namespace_version", namespace_version)
    monkeypatch.setattr(memory_repo, "list_by_namespace", list_by_namespace)
    monkeypatch.setattr(memory_repo, "count_by_namespace", count_by_namespace)


@pytest.mark.asyncio
async def test_a_matching_known_version_answers_unchanged_with_one_read() -> None:
    from server.memory.api.models_crud import MemoryListInput
    from server.memory.app.query import _handle_memory_list

    out = await _handle_memory_list(
        MemoryListInput(org="acme", namespace="team", agent="*", limit=100, known_version=4200)
    )
    assert out.unchanged is True and out.entries == [] and out.total == 0 and out.today == 0
    assert out.namespace_version == "4200"
    assert (_Calls.version, _Calls.lists, _Calls.counts) == (1, 0, 0), "no page, no total, no today"


@pytest.mark.asyncio
async def test_a_stale_or_absent_known_version_gets_the_full_answer_with_the_watermark() -> None:
    from server.memory.api.models_crud import MemoryListInput
    from server.memory.app.query import _handle_memory_list

    out = await _handle_memory_list(
        MemoryListInput(org="acme", namespace="team", agent="*", limit=100, known_version=4199)
    )
    assert out.unchanged is False and out.namespace_version == "4200"
    assert (_Calls.lists, _Calls.counts) == (1, 2)

    out2 = await _handle_memory_list(
        MemoryListInput(org="acme", namespace="team", agent="*", limit=100)
    )
    assert out2.unchanged is False and out2.namespace_version == "4200"
    # The same version remembers its two counts
    assert (_Calls.lists, _Calls.counts) == (2, 2)


@pytest.mark.asyncio
async def test_an_empty_namespace_never_answers_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_crud import MemoryListInput
    from server.memory.app.query import _handle_memory_list

    async def zero(org: str, ns: str, **kw: Any) -> int:
        return 0

    monkeypatch.setattr(memory_repo, "namespace_version", zero)
    out = await _handle_memory_list(
        MemoryListInput(org="acme", namespace="team", agent="*", limit=100, known_version=0)
    )
    assert out.unchanged is False and _Calls.lists == 1


@pytest.mark.asyncio
async def test_a_nanosecond_version_round_trips_as_a_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The version is above 2^53; a JavaScript client rounds it as a number
    (…253051 comes back as …253000 and never matches)."""
    from server.memory.api.models_crud import MemoryListInput
    from server.memory.app.query import _handle_memory_list

    big = 1789763207370253051

    async def nsv(org: str, ns: str, **kw: Any) -> int:
        return big

    monkeypatch.setattr(memory_repo, "namespace_version", nsv)
    full = await _handle_memory_list(
        MemoryListInput(org="acme", namespace="team", agent="*", limit=10)
    )
    assert full.namespace_version == str(big)
    same = await _handle_memory_list(
        MemoryListInput(org="acme", namespace="team", agent="*", limit=10, known_version=str(big))
    )
    assert same.unchanged is True
    rounded = await _handle_memory_list(
        MemoryListInput(
            org="acme", namespace="team", agent="*", limit=10, known_version=1789763207370253000
        )
    )
    assert rounded.unchanged is False


def test_the_poll_field_is_hidden_from_the_mcp_listing() -> None:
    """The watermark is the dashboard's; an agent never sends it, and every
    listed property is paid for on every turn, on every listing that carries memory.list."""
    from server.transport.mcp_schemas import _strip_blob_fields_from_knowledge

    schema = {
        "properties": {"namespace": {"type": "string"}, "known_version": {"type": "integer"}},
        "required": [],
    }
    out = _strip_blob_fields_from_knowledge("memory.list", schema)
    assert "known_version" not in out["properties"] and "namespace" in out["properties"]
    assert _strip_blob_fields_from_knowledge("memory.get", schema) is schema


def test_the_watermark_read_has_no_final_and_no_value_column() -> None:
    # the autouse fixture fakes the function; read the module text instead
    from server.memory.store import reads

    text = inspect.getsource(reads)
    start = text.index("async def namespace_version(")
    src = text[start : text.index("\n\n\nasync def", start)]
    sql = src.split('"""')[3]
    assert "max(version)" in sql and "FINAL" not in sql and "value" not in sql
