"""memory.redact reaches a shared copy of the text it scrubs: the loom.shared versions
promoted from the key whose value is that text, and their search rows."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import server.memory.store as memory_repo
from server.core import namespace_write_marks
from server.core.hashing import sha256_value
from server.memory.app import curation
from server.memory.domain.promotion_source import promotion_source
from server.memory.domain.redaction_lineage import REDACTED_VALUE
from server.memory.store import redaction
from server.memory.store.history import HistoryTarget
from server.memory.store.redaction import SharedCopy
from tests.unit.clickhouse_local import CLICKHOUSE, EMBEDDINGS, VECTOR, KeptCH, ddl

SECRET = {"note": "the vault code is 4417"}
SOURCE = promotion_source("acme", "shop", "a", "-", "-", "k")


def _target(value: Any = SECRET) -> HistoryTarget:
    return HistoryTarget(
        namespace="shop",
        agent="a",
        user_id="-",
        session_id="-",
        memory_key="k",
        version=7,
        org="acme",
        memory_id="33333333-3333-3333-3333-333333333333",
        content_sha256=sha256_value(value),
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"read": [], "scrubbed": [], "bumps": []}
    copies = [
        SharedCopy("k", 4, 6, json.dumps(SECRET)),
        SharedCopy("renamed", 5, 0, json.dumps(SECRET)),
        SharedCopy("old", 2, 0, REDACTED_VALUE),
        SharedCopy("k", 6, 0, json.dumps({"note": "x"})),
    ]

    async def shared_copies(org: str, namespace: str, source: str) -> list[SharedCopy]:
        seen["read"].append((org, namespace, source))
        return copies

    async def scrub(org: str, namespace: str, source: str, scrubbed: Any) -> None:
        seen["scrubbed"].append(
            (source, sorted((c.memory_key, c.version, c.next_shared_version) for c in scrubbed))
        )

    async def bump(org: str, namespace: str, plane: str) -> None:
        seen["bumps"].append((org, namespace, plane))

    monkeypatch.setattr(memory_repo, "shared_copies", shared_copies)
    monkeypatch.setattr(memory_repo, "scrub_shared_copies", scrub)
    monkeypatch.setattr(namespace_write_marks, "bump", bump)
    return seen


async def _scrub(value: Any = SECRET, head: Any = None) -> set[str]:
    return await curation._scrub_shared_copies("acme", _target(value), head)


async def test_the_copies_holding_the_text_are_scrubbed(calls: dict[str, Any]) -> None:
    # A copy an earlier run scrubbed is the redact's too: its search rows and caches.
    assert await _scrub() == {"k", "old", "renamed"}
    assert calls["read"] == [("acme", "shop", SOURCE)]
    assert calls["scrubbed"] == [(SOURCE, [("k", 4, 6), ("old", 2, 0), ("renamed", 5, 0)])]
    assert calls["bumps"] == [("acme", "shop", "memory")]


async def test_a_live_head_holding_the_text_keeps_its_copies(calls: dict[str, Any]) -> None:
    assert await _scrub(head=SimpleNamespace(content_sha256=sha256_value(SECRET))) == set()
    assert calls == {"read": [], "scrubbed": [], "bumps": []}


async def test_a_text_never_promoted_reaches_only_copies_already_scrubbed(
    calls: dict[str, Any],
) -> None:
    assert await _scrub({"note": "never promoted"}) == {"old"}
    assert calls["scrubbed"] == [(SOURCE, [("old", 2, 0)])]


async def test_no_copy_rewrites_nothing(
    calls: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_copies(org: str, namespace: str, source: str) -> list[SharedCopy]:
        return []

    monkeypatch.setattr(memory_repo, "shared_copies", no_copies)
    assert await _scrub() == set()
    assert calls["scrubbed"] == [] and calls["bumps"] == []


def _shared(key: str, version: int, value: str, source: str = SOURCE, demoted: bool = False) -> str:
    gone = "now()" if demoted else "NULL"
    return (
        "INSERT INTO loom.shared (org, namespace, memory_key, value, memory_type, confidence,"
        " promoted_from, promotion_event, promoted_by, reversible_until, tombstoned_at, version)"
        f" VALUES ('acme', 'shop', '{key}', '{value}', 'semantic', 1, ['{source}'],"
        f" generateUUIDv4(), 'sa:p', now(), {gone}, {version})"
    )


def _vector(key: str, version: int, *, stub: bool = False, retired: bool = False) -> str:
    vec = "[" + ",".join(["0"] * 3072) + "]" if stub else VECTOR  # a stub is all zeros
    return (
        "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id,"
        " memory_key, memory_type, scope, content_excerpt, embedding_model, embedding,"
        f" memory_key_embedding, embedding_dim, tombstoned_at, version) VALUES ('acme', 'shop',"
        f" '_shared', '-', '-', '{key}', 'semantic', 'shared', 'secret', 'm', {vec}, {VECTOR},"
        f" {0 if stub else 3072}, {'now()' if retired else 'NULL'}, {version})"
    )


async def _run(ch: KeptCH, monkeypatch: pytest.MonkeyPatch) -> set[str]:
    monkeypatch.setattr(redaction, "_default_client", lambda: ch)

    async def bump(org: str, namespace: str, plane: str) -> None:
        pass

    monkeypatch.setattr(namespace_write_marks, "bump", bump)
    return await curation._scrub_shared_copies("acme", _target(), None)


async def _vectors(ch: KeptCH) -> list[tuple[str, int]]:
    rows = await ch.query_all(
        "SELECT memory_key, version FROM loom.memory_embeddings ORDER BY memory_key, version"
    )
    return [(r["memory_key"], int(r["version"])) for r in rows]


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_another_sources_copy_of_the_key_keeps_its_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy's stub, the vector the embedding worker filled in above it and a demote's
    retired vector go; another source's later copy of the key keeps its row; a retry
    purges a row a cut-off run left."""
    secret = json.dumps(SECRET)
    other = promotion_source("acme", "shop", "b", "-", "-", "k")
    ch = KeptCH(
        tmp_path,
        ddl("shared"),
        *EMBEDDINGS,
        _shared("k", 1, secret),
        _vector("k", 1, stub=True),  # a failed inline embed
        _vector("k", 2),  # ... the worker's vector above it
        _shared("k", 5, secret, source=other),
        _vector("k", 5),
        _shared("renamed", 3, REDACTED_VALUE),  # scrubbed by an earlier run
        _vector("renamed", 3),  # ... whose purge was cut off
        _shared("renamed", 4, REDACTED_VALUE, demoted=True),
        _vector("renamed", 4, retired=True),  # the demote's retired vector
    )
    assert await _run(ch, monkeypatch) == {"k", "renamed"}
    shared = await ch.query_all(
        "SELECT memory_key, version, value FROM loom.shared ORDER BY memory_key, version"
    )
    assert [(r["memory_key"], int(r["version"]), r["value"]) for r in shared] == [
        ("k", 1, REDACTED_VALUE),
        ("k", 5, secret),
        ("renamed", 3, REDACTED_VALUE),
        ("renamed", 4, REDACTED_VALUE),
    ]
    assert await _vectors(ch) == [("k", 5)]


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_an_older_shared_row_of_the_key_does_not_resurface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vector below the newest copy's, which the copy replaced, goes with it, so `FINAL`
    serves no older text of the key, and so does the embedding worker's fill of the
    copy's stub."""
    other = promotion_source("acme", "shop", "b", "-", "-", "k")
    ch = KeptCH(
        tmp_path,
        ddl("shared"),
        *EMBEDDINGS,
        _shared("k", 1, json.dumps({"note": "older text"}), source=other),
        _vector("k", 1),
        _shared("k", 5, json.dumps(SECRET)),
        _vector("k", 5, stub=True),
        _vector("k", 6),
    )
    assert await _run(ch, monkeypatch) == {"k"}
    assert await _vectors(ch) == []


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_copy_followed_by_its_own_demote_loses_every_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy's stub, the worker's fill above it and the demote's retired vector all go:
    the demote is the key's newest shared version."""
    secret = json.dumps(SECRET)
    ch = KeptCH(
        tmp_path,
        ddl("shared"),
        *EMBEDDINGS,
        _shared("k", 1, secret),
        _vector("k", 1, stub=True),
        _vector("k", 2),
        _shared("k", 3, secret, demoted=True),
        _vector("k", 3, retired=True),
    )
    assert await _run(ch, monkeypatch) == {"k"}
    assert await _vectors(ch) == []


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_copy_merged_away_under_a_later_promotion_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`loom.shared` keeps one version per key once merged: a copy a later promotion
    replaced is gone, so the redact finds nothing to scrub (the history page says so)."""
    other = promotion_source("acme", "shop", "b", "-", "-", "k")
    ch = KeptCH(
        tmp_path,
        ddl("shared"),
        *EMBEDDINGS,
        _shared("k", 1, json.dumps(SECRET)),
        _shared("k", 5, json.dumps({"note": "later text"}), source=other),
        "OPTIMIZE TABLE loom.shared FINAL",
    )
    assert await _run(ch, monkeypatch) == set()


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_key_whose_copies_are_all_scrubbed_gets_no_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rerun with nothing left to rewrite counts, and issues no statement."""
    ch = KeptCH(tmp_path, ddl("shared"), *EMBEDDINGS, _shared("k", 1, REDACTED_VALUE))
    issued: list[str] = []
    execute = ch.execute

    async def recording(sql: str, params: Any = None, **kw: Any) -> None:
        issued.append(sql)
        await execute(sql, params, **kw)

    monkeypatch.setattr(ch, "execute", recording)
    assert await _run(ch, monkeypatch) == {"k"}
    assert issued == []
