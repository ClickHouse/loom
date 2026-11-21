"""A digest is served only when every byte can be reconstructed from live facts."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from server.core.datetimes import naive_utc_now
from server.memory import store
from server.memory.app.retrieval.summaries import fetch_session_summaries
from server.memory.store.summary_sources import current_summary_sources
from tests.unit.test_memory_verify import _row


def digest(source: Any, **overrides: Any) -> dict[str, Any]:
    return {
        "agent": source.agent,
        "user_id": source.user_id,
        "session_id": source.session_id,
        "derived_from": [source.memory_key],
        "key_facts": [source.value],
        "summary_text": source.value,
        "version": 200,
        "distance": 0.1,
        **overrides,
    }


@pytest.fixture
def sources(monkeypatch: Any) -> dict[str, Any]:
    values = {"source": _row(value="current fact", memory_key="source")}

    async def read(keys, **kwargs):
        from server.memory.store.rows import identity_key

        assert kwargs["sequential"]
        return {identity_key(r): r for r in values.values() if identity_key(r) in keys}

    monkeypatch.setattr("server.memory.store.summary_sources.memory_rows_by_identity_keys", read)
    return values


@pytest.mark.parametrize("lineage", [None, []])
async def test_a_summary_without_source_lineage_is_not_retrievable(lineage: Any) -> None:
    row = {"summary_text": "unknown fact", "key_facts": ["unknown fact"]}
    if lineage is not None:
        row["derived_from"] = lineage
    assert await current_summary_sources(None, [row], org="o", namespace="ns") == []


@pytest.mark.parametrize(
    "change",
    [
        {"status": "rejected"},
        {"status": "proposed"},
        {"status": "duplicate"},
        {"status": "superseded"},
        {"sensitivity": "high"},
        {"value": "replacement fact"},
        {"value_truncated": True},
        {"requires_review": True},
        {"user_id": "another-user"},
        {"session_id": "another-session"},
        {"agent": "another-agent"},
    ],
)
async def test_changed_or_unserved_sources_withhold_the_digest(sources: Any, change: Any) -> None:
    row = digest(sources["source"])
    sources["source"] = sources["source"].model_copy(update=change)
    assert await current_summary_sources(None, [row], org="o", namespace="ns") == []


@pytest.mark.parametrize("field", ["expires_at", "valid_to", "tombstoned_at"])
async def test_retired_sources_withhold_the_digest(sources: Any, field: str) -> None:
    row = digest(sources["source"])
    sources["source"] = sources["source"].model_copy(
        update={field: naive_utc_now() - timedelta(seconds=1)}
    )
    assert await current_summary_sources(None, [row], org="o", namespace="ns") == []


async def test_a_touch_does_not_invalidate_verifiable_unchanged_text(sources: Any) -> None:
    row = digest(sources["source"])
    sources["source"] = sources["source"].model_copy(update={"version": 999, "pinned": True})
    assert await current_summary_sources(None, [row], org="o", namespace="ns") == [row]


async def test_a_missing_source_and_a_forged_sentence_do_not_hide_a_valid_neighbour(
    sources: Any,
) -> None:
    valid = digest(sources["source"])
    missing = digest(sources["source"], derived_from=["missing"])
    forged = digest(sources["source"], summary_text="current fact\nretired fact")
    assert await current_summary_sources(
        None, [missing, forged, valid], org="o", namespace="ns"
    ) == [valid]


async def test_a_current_model_digest_does_not_qualify_a_stale_model_row(sources):
    from server.memory.app.period_digest import PeriodDigestConfig, _plan_identity

    source = sources["source"] = sources["source"].model_copy(update={"session_id": "session"})
    scope = {c: getattr(source, c) for c in ("org", "namespace")}
    stale = digest(
        source,
        **scope,
        embedding_model="old",
        summary_text="retired fact",
        key_facts=["retired fact"],
    )
    current = digest(source, **scope, embedding_model="current")
    ch = SimpleNamespace(query_all=AsyncMock(return_value=[]))
    identity = {c: getattr(source, c) for c in ("org", "namespace", "agent", "user_id")}
    assert await _plan_identity(
        ch, [stale, current], identity, PeriodDigestConfig(sessions_per_period=2)
    ) == ([], 0)


async def test_source_state_failure_withholds_the_summary(monkeypatch: Any) -> None:
    source = _row(value="current fact", memory_key="source")

    async def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("source state unavailable")

    class CH:
        async def query_all(self, *args: Any, **kwargs: Any) -> Any:
            return [digest(source)]

    monkeypatch.setattr(
        "server.memory.store.summary_sources.memory_rows_by_identity_keys", unavailable
    )
    assert (
        await fetch_session_summaries(
            CH(),
            org="o",
            namespace="ns",
            agent=source.agent,
            user_id="-",
            query_vec=[1.0, 0.0],
            embedding_model="test-model",
        )
        == []
    )


async def test_postgres_authority_overrides_a_stale_clickhouse_source(monkeypatch: Any) -> None:
    from server.memory.store import pg_memory
    from server.settings import settings

    source = _row(value="current fact", memory_key="source")
    rejected = store.build_memory_version_row(source, status="rejected", version=201)
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(
        pg_memory, "heads_by_keys", AsyncMock(return_value={store.identity_key(source): rejected})
    )
    ch = SimpleNamespace(query_all=AsyncMock(side_effect=AssertionError("must read the authority")))
    assert await current_summary_sources(ch, [digest(source)], org="o", namespace="ns") == []
    ch.query_all.assert_not_awaited()


@pytest.mark.parametrize("fact", ["文" * 600, "x" * 3000, "🌱" * 600])
async def test_encoded_source_size_does_not_drop_a_complete_fact(monkeypatch, fact):
    from server.memory.store.rows import identity_key

    source = _row(value=fact, memory_key="source")

    async def read(keys, **kwargs):
        assert kwargs["max_value_chars"] >= len(json.dumps(fact))
        return {identity_key(source): source}

    monkeypatch.setattr("server.memory.store.summary_sources.memory_rows_by_identity_keys", read)
    row = digest(source)
    assert await current_summary_sources(None, [row], org="o", namespace="ns") == [row]


@pytest.mark.parametrize("count", [6, 50])
@pytest.mark.parametrize("retired_last", [False, True])
async def test_many_sources_are_qualified_in_one_exact_key_read(monkeypatch, count, retired_last):
    from server.memory.store.rows import identity_key

    sources = [_row(value="文" * 400, memory_key=f"source-{n}") for n in range(count)]
    calls = []

    async def read(keys, **kwargs):
        calls.append(keys)
        return {identity_key(source): source for source in sources}

    monkeypatch.setattr("server.memory.store.summary_sources.memory_rows_by_identity_keys", read)
    facts = [source.value for source in sources]
    row = digest(
        sources[0],
        derived_from=[source.memory_key for source in sources],
        key_facts=facts,
        summary_text="\n".join(facts),
    )
    if retired_last:
        sources[-1] = sources[-1].model_copy(update={"status": "superseded"})
    expected = [] if retired_last else [row]
    assert await current_summary_sources(None, [row], org="o", namespace="ns") == expected
    assert len(calls) == 1 and len(calls[0]) == count
