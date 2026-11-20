"""A parked source publication cannot make an obsolete digest readable."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.core import pg
from server.memory.app.retrieval.summaries import fetch_session_summaries
from server.memory.store import pg_memory
from server.memory.store.rows import identity_key

KEY = ("org", "namespace", "writer", "reader", "session", "source")
STAMP = datetime(2026, 1, 1)


def _source() -> dict[str, Any]:
    return dict(
        zip(("org", "namespace", "agent", "user_id", "session_id", "memory_key"), KEY, strict=True),
        version=100,
        content_sha256="a" * 64,
        status="active",
        sensitivity="low",
        tombstoned_at=None,
        redacted_at=None,
        expires_at=None,
        valid_to=None,
        created_at=STAMP,
        valid_at=None,
        temporal_anchor=None,
        invalid_at=None,
    )


class SourceProjection:
    def __init__(self, authority: dict[Any, Any], replacement: dict[str, Any] | None) -> None:
        self.authority = authority
        self.replacement = replacement
        self.lineage = ["source"]
        self.projected_hash: object = "a" * 64

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "FROM loom.session_summaries FINAL" in sql:
            return [
                {
                    "agent": "writer",
                    "user_id": "reader",
                    "session_id": "session",
                    "version": 200,
                    "derived_from": self.lineage,
                    "summary_text": "a source fact",
                    "distance": 0.1,
                }
            ]
        assert "FROM loom.memory FINAL" in sql
        # The source moves after admission and the projection read. Its delivery stays parked.
        self.authority.clear()
        if self.replacement is not None:
            self.authority[identity_key(self.replacement)] = self.replacement
        return [
            _source()
            | {
                "source_user": "reader",
                "source_session": "session",
                "source_served": True,
                "content_sha256": self.projected_hash,
            }
        ]


@pytest.fixture
def source_read(monkeypatch: pytest.MonkeyPatch):
    authority = {KEY: _source()}
    requested: list[Any] = []
    unavailable = False

    async def read(sql: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if unavailable:
            raise TimeoutError("authority unavailable")
        keys = list(zip(*args, strict=True))
        requested.extend(keys)
        return [{"row": authority[key]} for key in keys if key in authority]

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "_read", read)

    async def retrieve(
        replacement: dict[str, Any] | None,
        *,
        lineage: bool = True,
        fail=False,
        projected_hash: object = "a" * 64,
    ):
        nonlocal unavailable
        unavailable = fail
        ch = SourceProjection(authority, replacement)
        ch.projected_hash = projected_hash
        if not lineage:
            ch.lineage = []
        return await fetch_session_summaries(
            ch,
            org="org",
            namespace="namespace",
            agent="writer",
            user_id="reader",
            query_vec=[1.0, 0.0],
            embedding_model="test-model",
        )

    return retrieve, requested


@pytest.mark.parametrize(
    "change",
    [
        {"tombstoned_at": STAMP, "status": "tombstoned"},
        {"version": 150},
        {"content_sha256": "b" * 64},
        {"content_sha256": ""},
        {"status": "rejected"},
        {"sensitivity": "high"},
        {"redacted_at": STAMP},
        {"expires_at": STAMP},
        {"valid_to": STAMP},
    ],
    ids=[
        "deleted",
        "newer-version",
        "same-version-rewrite",
        "missing-proof",
        "rejected",
        "high",
        "redacted",
        "expired",
        "invalid",
    ],
)
async def test_a_digest_withholds_a_source_changed_after_the_projection_read(source_read, change):
    retrieve, _ = source_read
    assert await retrieve(_source() | change) == []


@pytest.mark.parametrize(
    "axis", ["org", "namespace", "agent", "user_id", "session_id", "memory_key"]
)
async def test_another_identitys_source_cannot_validate_a_digest(source_read, axis):
    retrieve, requested = source_read
    assert await retrieve(_source() | {axis: "other"}) == []
    assert requested == [KEY]


async def test_a_missing_authoritative_source_withholds_the_digest(source_read):
    retrieve, _ = source_read
    assert await retrieve(None) == []


async def test_an_unavailable_authority_never_falls_back_to_the_projection(source_read):
    retrieve, _ = source_read
    assert await retrieve(_source(), fail=True) == []


async def test_the_matching_source_keeps_its_digest_with_the_full_identity(source_read):
    retrieve, requested = source_read
    head = _source() | {"created_at": STAMP.replace(tzinfo=UTC).astimezone(UTC)}
    [hit] = await retrieve(head)
    assert hit.content_excerpt == "a source fact"
    assert hit.derived_from == ["source"]
    assert requested == [KEY]


async def test_missing_lineage_remains_transcript_evidence_without_an_authority_read(source_read):
    retrieve, requested = source_read
    [hit] = await retrieve(None, lineage=False, fail=True)
    assert hit.content_excerpt == "a source fact"
    assert requested == []


async def test_clickhouse_authority_uses_its_own_source_state(source_read, monkeypatch):
    retrieve, requested = source_read
    monkeypatch.setattr(pg, "enabled", lambda: False)
    [hit] = await retrieve(None, fail=True)
    assert hit.content_excerpt == "a source fact"
    assert requested == []


async def test_source_proof_reads_metadata_and_restores_sparse_defaults(monkeypatch):
    source = _source()
    stored = {
        col: value
        for col, value in source.items()
        if col not in pg_memory.SPARSE_DEFAULTS or value != pg_memory.SPARSE_DEFAULTS[col]
    }
    stored["created_at"] = STAMP.isoformat()

    async def read(sql, *args, durable):
        assert list(zip(*args, strict=True)) == [KEY]
        assert not durable
        assert "USING (org, namespace, agent, user_id, session_id, memory_key)" in sql
        assert "SELECT c.row " not in sql
        assert "c.row->'value'" not in sql and "c.row->'value_json'" not in sql
        assert "jsonb_strip_nulls" in sql
        return [{"row": stored}]

    monkeypatch.setattr(pg_memory, "_read", read)
    heads = await pg_memory.summary_source_heads([KEY])
    assert heads[KEY] == {col: source[col] for col in pg_memory.SUMMARY_SOURCE_STATE_COLUMNS}


async def test_an_unstorable_source_key_never_reaches_the_authority(monkeypatch):
    async def unreadable(*args, **kwargs):
        raise AssertionError("an unstorable key must not reach Postgres")

    monkeypatch.setattr(pg_memory, "_read", unreadable)
    assert await pg_memory.summary_source_heads([]) == {}
    assert await pg_memory.summary_source_heads([(*KEY[:5], "bad\x00key")]) == {}


@pytest.mark.parametrize("projected_hash", [b"a" * 64, bytearray(b"a" * 64)])
async def test_native_hash_bytes_keep_a_matching_authoritative_digest(source_read, projected_hash):
    retrieve, requested = source_read
    [hit] = await retrieve(_source(), projected_hash=projected_hash)
    assert hit.content_excerpt == "a source fact"
    assert hit.derived_from == ["source"]
    assert requested == [KEY]


@pytest.mark.parametrize(
    ("projected_hash", "authoritative_hash"),
    [
        (b"b" * 64, "a" * 64),
        (b"", ""),
        (b"\x00" * 64, ""),
        (b"g" * 64, "g" * 64),
        (b"\xff" * 64, "\ufffd" * 64),
    ],
    ids=["changed", "empty", "null-padding", "nonhex", "invalid-utf8"],
)
async def test_native_hash_bytes_cannot_validate_changed_or_invalid_sources(
    source_read, projected_hash, authoritative_hash
):
    retrieve, requested = source_read
    assert (
        await retrieve(
            _source() | {"content_sha256": authoritative_hash}, projected_hash=projected_hash
        )
        == []
    )
    assert requested == [KEY]
