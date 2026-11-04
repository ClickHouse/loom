"""Stored content hashes come back from ClickHouse as bytes; every gate that
compares one must read it as hex text. Comparing `str(b"…")` — the repr —
to the hex the write path computed never matches, so the identity-scoped
dedup and the same-key skip gate would silently fall through to the cosine
merge verdict, later and after an embedding call."""

from __future__ import annotations

from typing import Any

import pytest

from server.core.hashing import hash_text, sha256_value
from tests.unit.memory_rows import projection_row


def test_hash_text_decodes_bytes_and_passes_text_through() -> None:
    h = sha256_value("x")
    assert hash_text(h.encode()) == h
    assert hash_text(h) == h
    assert hash_text(bytearray(h.encode())) == h
    assert hash_text(None) == "" and hash_text(b"0") == "0"
    assert hash_text(b"ab\x00\x00") == "ab"  # FixedString padding


@pytest.mark.asyncio
async def test_bulk_gates_match_when_the_hash_comes_back_as_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry
    from server.memory.app import bulk_set as _bulk_set_mod
    from server.memory.app import session_summary as _session_summary_mod
    from server.memory.app.extract import handler as mem_extract

    value = "User's task branch is master."
    sha_bytes = sha256_value(value).encode()

    class _CH:
        def __init__(self) -> None:
            self.inserts: list[str] = []

        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            if "content_sha256 IN" in sql:  # identity lookup
                return [
                    {
                        "head_sha": sha_bytes,
                        "memory_key": "user_task_branch",
                        "memory_id": "mid-1",
                    }
                ]
            if "IN {keys:Array(Tuple(" in sql:  # same-session lookup
                return [
                    {
                        **projection_row(
                            org="o",
                            namespace="n",
                            agent="a",
                            user_id="u",
                            session_id="s",
                            memory_key="user_task_branch",
                            memory_id="mid-1",
                        ),
                        "content_sha256": sha_bytes,
                    }
                ]
            return []

        async def insert(
            self, table: str, rows: list[dict[str, Any]], settings: Any = None
        ) -> None:
            self.inserts.append(table)

    ch = _CH()
    events: list[str] = []

    async def _none(*_: Any, **__: Any) -> Any:
        return None

    async def _record(**kw: Any) -> None:
        events.append(kw["memory_id"])

    monkeypatch.setattr(_bulk_set_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_session_summary_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.blacklist_guard.find_match", _none)
    monkeypatch.setattr("server.memory.app.blacklist_guard.note_hit", _none)
    monkeypatch.setattr("server.memory.store.usage.record_usage_event", _record)

    # same key, same session: the pre-existing skip gate, now matching
    out = await mem_extract.handle_memory_bulk_set(
        MemoryBulkSetInput(
            org="o",
            namespace="n",
            agent="a",
            user_id="u",
            session_id="s",
            entries=[_BulkSetEntry(memory_key="user_task_branch", value=value)],
        )
    )
    assert "loom.memory" not in ch.inserts and out.written[0].version == 0
    assert out.identity_duplicates == {}  # the same-key gate fired first
    # another key: the identity gate, now matching
    out = await mem_extract.handle_memory_bulk_set(
        MemoryBulkSetInput(
            org="o",
            namespace="n",
            agent="a",
            user_id="u",
            session_id="s2",
            entries=[_BulkSetEntry(memory_key="user_clickhouse_task_branch", value=value)],
        )
    )
    assert "loom.memory" not in ch.inserts
    assert out.identity_duplicates == {"user_clickhouse_task_branch": "user_task_branch"}
    # Two reinforcements of the same live row: the same-key restatement of the
    # first call and the identity dedup of the second.
    assert events == ["mid-1", "mid-1"]


@pytest.mark.asyncio
async def test_precedent_evidence_reads_bytes_hashes_as_hex() -> None:
    from server.memory.store import ask_precedents as repo

    class _CH:
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            return [
                {"namespace": "ns", "memory_key": "m1", "content_sha256": b"a" * 64},
                {"namespace": "ns", "memory_key": "m2", "content_sha256": b"0"},
            ]

    ev = await repo.evidence_for("o", [("ns", "m1"), ("ns", "m2")], ch=_CH())
    assert ev.hashes == {("ns", "m1"): "a" * 64, ("ns", "m2"): "0"}
    assert not ev.verifiable  # the unhashed row is seen as unhashed, not as "b'0'"


def test_row_model_list_verification_and_pipeline_dedup_read_bytes_as_hex() -> None:
    import asyncio

    from server.memory import store as memory_repo
    from server.memory.app import pipeline as mp
    from server.memory.app import pipeline_state as pipeline_state
    from server.memory.domain import trust

    h = sha256_value("v")
    from datetime import UTC, datetime

    row = {
        "memory_id": "11111111-1111-1111-1111-111111111111",
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "u",
        "session_id": "s",
        "memory_key": "k",
        "memory_type": "semantic",
        "value": "v",
        "confidence": 1.0,
        "version": 1,
        "pinned": 0,
        "created_at": datetime(2026, 9, 12, tzinfo=UTC),
        "expires_at": None,
        "char_limit": None,
        "content_sha256": h.encode(),
    }
    assert memory_repo._hydrate(row).content_sha256 == h
    # memory.list verification: a bytes hash equal to the verified hash is not "stale"
    verified = trust.row_verification(
        {
            "content_sha256": h.encode(),
            "verified_by": "alice",
            "verified_sha256": h,
            "value": "v",
        }
    )
    unverified = trust.row_verification(
        {
            "content_sha256": h.encode(),
            "verified_by": "alice",
            "verified_sha256": "f" * 64,
            "value": "v",
        }
    )
    assert verified != unverified and "stale" not in str(verified)
    # pipeline hash dedup: an existing row whose hash comes back as bytes still drops the repeat
    st = pipeline_state.WorkflowState(
        org="o", namespace="n", agent="a", user_id="u", session_id="s"
    )
    st.existing_memories = [{"content_sha256": h.encode()}]
    st.extracted_facts = [{"value": "v"}, {"value": "other"}]
    out = asyncio.run(mp._step_hash_dedup(st, {}))
    assert [f["value"] for f in out.extracted_facts] == ["other"] and out.skipped_dedup == 1


def test_a_stored_hash_wins_over_the_value_and_a_cut_value_has_none() -> None:
    from server.core.hashing import sha256_value_json
    from server.memory.domain import trust

    stored = "a" * 64
    assert trust.live_sha256(stored, '"v"') == stored
    assert trust.live_sha256("0", '"v"') == sha256_value_json('"v"')
    assert trust.live_sha256("0", None) == ""
