"""The exact-identifier reads.

A cosine-ordered `find_by_identifier` is one keyed FINAL re-read whose
identity pass (no FINAL, no vector) is its subquery; the length and recency
orders are one plain statement, and `find_chunks_by_identifier` reads its
matches under FINAL keyed to a subquery's identities. The fake
below holds a small versioned table and answers each statement shape from
it, so the state filters and the FINAL semantics (a superseded or
tombstoned version never resurrects) are checked against the rows a plain
FINAL read of the table would serve."""

from __future__ import annotations

import asyncio
import math
from typing import Any

from server.memory import store as memory_repo

QV = [1.0, 0.0, 0.0]


def _row(key: str, sess: str, text: str, vec: list[float], **over: Any) -> dict[str, Any]:
    r = {
        "org": "o",
        "namespace": "n",
        "agent": "bot",
        "user_id": "-",
        "session_id": sess,
        "scope": "private",
        "embedding_model": "m",
        "memory_key": key,
        "memory_type": "semantic",
        "content_excerpt": text,
        "embedding": vec,
        "status": "active",
        "tombstoned_at": None,
        "expires_at": None,
        "version": 1,
        "created_at": "2026-09-01 00:00:00",
        "valid_at": "2026-09-01 00:00:00",
    }
    r.update(over)
    return r


def _dist(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return 1.0 - dot / (na * nb)


_IDENTITY = (
    "org",
    "namespace",
    "agent",
    "user_id",
    "session_id",
    "scope",
    "embedding_model",
    "memory_key",
)


class _TableCH:
    """Answers the fact statement shapes from an in-memory versioned table."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _matches(self, r: dict[str, Any], params: dict[str, Any]) -> bool:
        if (
            r["org"] != params["org"]
            or r["namespace"] != params["ns"]
            or r["scope"] != params["scope"]
        ):
            return False
        if "agent" in params and r["agent"] != params["agent"]:
            return False
        if r["tombstoned_at"] is not None or r["status"] not in (
            "active",
            "duplicate",
            "conflicted",
        ):
            return False
        ids = [v for k, v in params.items() if k.startswith("id")]
        return any(i in r["content_excerpt"] for i in ids)

    def _final(self) -> list[dict[str, Any]]:
        latest: dict[tuple[Any, ...], dict[str, Any]] = {}
        for r in self.rows:
            k = tuple(r[c] for c in _IDENTITY)
            if k not in latest or r["version"] > latest[k]["version"]:
                latest[k] = r
        return list(latest.values())

    @staticmethod
    def _project(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "memory_key": r["memory_key"],
            "memory_type": r["memory_type"],
            "content_excerpt": r["content_excerpt"],
            "created_at": r["created_at"],
            "valid_at": r["valid_at"],
        }

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        params = params or {}
        self.calls.append((sql, params))
        # the identity subquery is a superset of the FINAL matches, so the keyed
        # read serves exactly the FINAL rows that match the predicate
        rows = [r for r in self._final() if self._matches(r, params)]
        if "qv" in params:
            rows.sort(key=lambda r: _dist(r["embedding"], params["qv"]))
        else:
            rows.sort(key=lambda r: -len(r["content_excerpt"]))
        return [self._project(r) for r in rows[: int(params["lim"])]]


def _table() -> list[dict[str, Any]]:
    return [
        _row("k1", "s1", "PR 117947 merged", [1.0, 0.1, 0.0]),
        _row("k2", "s1", "issue 117947 triage", [0.0, 1.0, 0.0]),
        _row("k3", "s2", "117947 CI failure", [0.9, 0.0, 0.4]),
        _row("k4", "s2", "unrelated", [1.0, 0.0, 0.0]),
        # k5: an old version cites the number, the current one does not — must not resurrect
        _row("k5", "s3", "117947 old text", [1.0, 0.0, 0.0], version=1),
        _row("k5", "s3", "rewritten", [1.0, 0.0, 0.0], version=2),
        # k6: current version tombstoned
        _row(
            "k6",
            "s3",
            "117947 gone",
            [1.0, 0.0, 0.0],
            version=2,
            tombstoned_at="2026-09-02 00:00:00",
        ),
        _row("k6", "s3", "117947 gone", [1.0, 0.0, 0.0], version=1),
        # k7: two versions, both cite it; the newest is what FINAL serves
        _row("k7", "s4", "117947 v1", [0.99, 0.0, 0.1], version=1),
        _row("k7", "s4", "117947 v2", [0.99, 0.0, 0.1], version=2),
    ]


def _run(ch: Any, **kw: Any) -> list[dict[str, Any]]:
    async def go() -> list[dict[str, Any]]:
        return await memory_repo.find_by_identifier(
            org="o",
            namespace="n",
            identifiers=["117947"],
            limit=3,
            query_embedding=QV,
            scope="private",
            agent="bot",
            ch=ch,
            **kw,
        )

    return asyncio.run(go())


def test_the_cosine_read_is_one_keyed_final_statement() -> None:
    ch = _TableCH(_table())
    got = _run(ch)
    assert [r["memory_key"] for r in got] == ["k1", "k7", "k3"]
    assert "117947 v2" in got[1]["content_excerpt"], "the current version, not the superseded one"
    assert len(ch.calls) == 1
    sql, params = ch.calls[0]
    assert "FROM loom.memory_embeddings FINAL" in sql and "arrayZip" not in sql
    assert sql.count("position(content_excerpt, {id0:String}) > 0") == 2, (
        "predicate inside and outside"
    )
    assert "SELECT DISTINCT org, namespace, agent, user_id, session_id" in sql
    assert "optimize_move_to_prewhere_if_final = 1" in sql and "use_skip_indexes = 0" in sql
    assert "ORDER BY cosineDistance(embedding, {qv:Array(Float32)}) ASC" in sql


def test_the_keyed_read_keeps_every_state_and_identity_filter_on_both_sides() -> None:
    ch = _TableCH(_table())
    _run(ch, user_id="u1")
    sql, params = ch.calls[0]
    for clause in (
        "scope = {scope:String}",
        "agent = {agent:String}",
        "user_id = {user_id:String}",
        "tombstoned_at IS NULL",
        "status IN ('active', 'duplicate', 'conflicted')",
        "(expires_at IS NULL OR expires_at > now())",
        "position(content_excerpt, {id0:String}) > 0",
    ):
        assert sql.count(clause) == 2, clause
    assert params["user_id"] == "u1" and params["agent"] == "bot"


def test_reads_without_a_vector_keep_one_plain_statement() -> None:
    """The length and recency orders never touch `embedding`; nothing to key."""

    class _CH:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            self.calls.append(sql)
            return []

    ch = _CH()
    asyncio.run(
        memory_repo.find_by_identifier(org="o", namespace="n", identifiers=["117947"], ch=ch)
    )
    asyncio.run(
        memory_repo.find_by_identifier(
            org="o",
            namespace="n",
            identifiers=["117947"],
            ch=ch,
            query_embedding=QV,
            order="recency",
        )
    )
    assert len(ch.calls) == 2
    for sql in ch.calls:
        assert "FROM loom.memory_embeddings FINAL" in sql
        assert "SELECT DISTINCT" not in sql and "SETTINGS" not in sql and "PREWHERE" not in sql
    assert "length(content_excerpt) DESC" in ch.calls[0] and "valid_at DESC" in ch.calls[1]


class _SQLCH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params or {}))
        return []


def _chunks(**kw: Any) -> str:
    ch = _SQLCH()
    asyncio.run(
        memory_repo.find_chunks_by_identifier(
            org="o",
            namespace="n",
            identifiers=["117947"],
            limit=4,
            scope="private",
            agent="bot",
            ch=ch,
            **kw,
        )
    )
    return ch.calls[0][0]


def test_chunk_read_collapses_versions_across_partitions() -> None:
    """A chunk's versions can sit in two monthly partitions (retired in a
    later month than it was written), which merges never collapse, so the
    read is one statement under FINAL keyed to the identities a subquery
    without FINAL finds (the planner moves the predicate ahead of the vector
    there): a retired chunk's tombstone wins and its text is never served."""
    sql = _chunks(query_embedding=QV, markers=["root cause"])
    assert "PREWHERE" not in sql and "arrayZip" not in sql
    assert "FROM loom.memory_chunks FINAL" in sql
    assert (
        "(org, namespace, session_id, chunk_index, content_sha256) IN (\n"
        "              SELECT DISTINCT org, namespace, session_id, chunk_index, content_sha256 "
        "FROM loom.memory_chunks WHERE org = {org:String}"
    ) in sql
    assert "position(content, {id0:String}) > 0" in sql
    assert "multiSearchAnyCaseInsensitive(content, {marks:Array(String)}) > 0" in sql
    assert "ORDER BY cosineDistance(embedding, {qv:Array(Float32)}) ASC" in sql
