"""Unit tests for the session-summary plane write side.

Two halves:
  - build_session_summary_row — the single source of truth for
    loom.session_summaries INSERT rows. Guards the RMT(version) silent-data-loss
    class (a write omits a column -> FINAL surfaces the schema DEFAULT over a
    prior real value).
  - _write_session_summary — the inline ingest writer. Stubs authoritative source
    heads, the embedder and the CH client to check complete fact rendering,
    row shape, date labels and skipped writes without network access.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock

import pytest

from server.memory.store import (
    SESSION_SUMMARY_PERSISTED_COLS,
    build_session_summary_row,
)

# ----------------------------------------------------------------------
# build_session_summary_row — mirror discipline
# ----------------------------------------------------------------------


def test_builder_emits_every_persisted_column() -> None:
    row = build_session_summary_row(org="o", namespace="n", session_id="s")
    # No column may be silently missing — that is the whole point (RMT trap).
    assert set(row) == set(SESSION_SUMMARY_PERSISTED_COLS)


def test_unknown_column_raises() -> None:
    # A typo'd column name is a bug, not a silently dropped value.
    with pytest.raises(ValueError, match="unknown columns"):
        build_session_summary_row(org="o", embeddings=[1, 2, 3])  # note the typo


def test_non_nullable_datetime_never_none() -> None:
    row = build_session_summary_row(org="o")
    # created_at is non-Nullable DateTime in the DDL; the builder must fill it so
    # an insert never sends NULL into a non-Nullable column.
    assert isinstance(row["created_at"], datetime)


def test_version_stamped_when_unset() -> None:
    row = build_session_summary_row(org="o")
    # ReplacingMergeTree(version): a re-ingest of the same session must win FINAL,
    # so an unset version is stamped from the nanosecond clock (non-zero).
    assert isinstance(row["version"], int)
    assert row["version"] > 0


def test_overrides_are_applied() -> None:
    row = build_session_summary_row(
        org="o",
        session_id="s1",
        summary_embedding=[0.1, 0.2],
        embedding_dim=2,
        key_facts=["a", "b"],
    )
    assert row["org"] == "o"
    assert row["session_id"] == "s1"
    assert row["summary_embedding"] == [0.1, 0.2]
    assert row["embedding_dim"] == 2
    assert row["key_facts"] == ["a", "b"]


def test_defaults_match_schema_for_omitted_columns() -> None:
    row = build_session_summary_row(org="o")
    assert row["summary_embedding"] == []
    assert row["key_facts"] == []
    assert row["valid_at"] is None
    assert row["tombstoned_at"] is None
    assert row["embedding_dim"] == 0
    assert row["agent"] == "-"
    assert row["user_id"] == "-"


@pytest.fixture
def writer(monkeypatch: Any, no_blacklisted_values: Any) -> Any:
    from server.memory import store
    from server.memory.app import digest_chain, session_summary
    from tests.unit.test_memory_verify import _row

    source = _row(
        namespace="n",
        agent="a",
        user_id="u",
        session_id="s",
        memory_key="known",
        value="current fact",
    )
    state = SimpleNamespace(
        written=[
            {
                "memory_id": source.memory_id,
                "memory_key": source.memory_key,
                "version": source.version,
            }
        ],
        extracted_facts=[{"memory_key": "uncommitted", "value": "retired transcript fact"}],
        messages=[{"role": "user", "content": "retired transcript fact"}],
        observation_date=None,
    )
    ch = SimpleNamespace(insert=AsyncMock(), query_all=AsyncMock(return_value=[]))
    from server.memory.store.rows import identity_key

    monkeypatch.setattr(
        "server.memory.store.summary_sources.memory_rows_by_identity_keys",
        AsyncMock(return_value={identity_key(source): source}),
    )
    read = AsyncMock(return_value=source)
    vector = AsyncMock(return_value=[1.0, 0.0])
    link = AsyncMock()
    monkeypatch.setattr(store, "get_by_id", read)
    monkeypatch.setattr(session_summary, "get_client", lambda: ch)
    monkeypatch.setattr(session_summary, "get_embedder", lambda: SimpleNamespace(model_name="fake"))
    monkeypatch.setattr(session_summary, "document_vector", vector)
    monkeypatch.setattr(digest_chain, "link_digest", link)
    inp = SimpleNamespace(
        org="o", namespace="n", agent="a", user_id="u", session_id="s", run_id="run"
    )
    return SimpleNamespace(
        input=inp, state=state, source=source, ch=ch, read=read, vector=vector, link=link
    )


async def test_digest_contains_only_committed_current_sources(writer: Any) -> None:
    from server.memory.app.session_summary import _write_session_summary

    await _write_session_summary(writer.input, writer.state)
    table, rows = writer.ch.insert.call_args.args
    assert table == "loom.session_summaries"
    row = rows[0]
    assert set(row) == set(SESSION_SUMMARY_PERSISTED_COLS)
    assert row["summary_text"] == "current fact"
    assert row["key_facts"] == ["current fact"]
    assert row["derived_from"] == ["known"]
    assert "retired transcript fact" not in str(row)
    assert row["run_id"] == "run"
    writer.vector.assert_awaited_once_with(ANY, "current fact")
    writer.link.assert_awaited_once()


@pytest.mark.parametrize(
    "change",
    [
        {"status": "rejected"},
        {"status": "proposed"},
        {"sensitivity": "high"},
        {"version": 101},
        {"session_id": "another-session"},
        {"value_truncated": True},
    ],
)
async def test_unqualified_sources_are_not_embedded_or_written(writer: Any, change: Any) -> None:
    from server.memory.app.session_summary import _write_session_summary

    writer.read.return_value = writer.source.model_copy(update=change)
    await _write_session_summary(writer.input, writer.state)
    writer.ch.insert.assert_not_awaited()
    writer.vector.assert_not_awaited()


async def test_no_committed_fact_produces_no_digest(writer: Any) -> None:
    from server.memory.app.session_summary import _write_session_summary

    writer.state.written = []
    await _write_session_summary(writer.input, writer.state)
    writer.read.assert_not_awaited()
    writer.ch.insert.assert_not_awaited()


async def test_a_source_read_failure_is_reported_to_the_projection_caller(writer: Any) -> None:
    from server.memory.app.session_summary import _write_session_summary

    writer.read.side_effect = TimeoutError("authority unavailable")
    with pytest.raises(TimeoutError):
        await _write_session_summary(writer.input, writer.state)
    writer.ch.insert.assert_not_awaited()


async def test_digest_cap_keeps_whole_facts_instead_of_partial_provenance(
    writer: Any, monkeypatch: Any
) -> None:
    from server.memory.app.session_summary import _write_session_summary
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_session_digest_max_input_chars", 500)
    writer.read.return_value = writer.source.model_copy(update={"value": "x" * 501})
    await _write_session_summary(writer.input, writer.state)
    writer.ch.insert.assert_not_awaited()
    writer.vector.assert_not_awaited()


async def test_a_missing_observation_date_stays_undated(writer: Any) -> None:
    from server.memory.app.session_summary import _write_session_summary

    await _write_session_summary(writer.input, writer.state)
    assert writer.ch.insert.call_args.args[1][0]["valid_at"] is None


async def test_a_source_removed_while_embedding_is_not_published(writer, monkeypatch):
    from server.memory.app.session_summary import _write_session_summary

    async def removed_during_embedding(*args, **kwargs):
        monkeypatch.setattr(
            "server.memory.store.summary_sources.memory_rows_by_identity_keys",
            AsyncMock(return_value={}),
        )
        return [1.0, 0.0]

    monkeypatch.setattr(
        "server.memory.app.session_summary.document_vector", removed_during_embedding
    )
    await _write_session_summary(writer.input, writer.state)
    writer.ch.insert.assert_not_awaited()
    writer.link.assert_not_awaited()


async def test_a_current_source_after_many_missing_candidates_can_still_form_a_digest(writer):
    from server.memory.app.session_summary import _write_session_summary

    committed = writer.state.written[0]
    writer.state.written = [dict(memory_id=f"missing-{i}", version=100) for i in range(48)] + [
        committed
    ]
    writer.read.side_effect = [None] * 48 + [writer.source]
    await _write_session_summary(writer.input, writer.state)
    assert writer.read.await_count == 49
    assert writer.ch.insert.call_args.args[1][0]["key_facts"] == [writer.source.value]
