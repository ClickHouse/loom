"""The digest chain: consecutive session digests of one
identity are linked with `precedes` edges in loom.operator_edges — at digest
write time — with every write fail-open."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest

from server.memory.app import digest_chain as dc


class _CH:
    """query_all answers come from a queue (prev, next, ...); inserts are kept."""

    def __init__(self, answers: list[list[dict[str, Any]]] | None = None, fail: bool = False):
        self.answers = list(answers or [])
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.fail = fail

    async def query_all(self, sql, params=None):
        if self.fail:
            raise RuntimeError("boom")
        self.queries.append((sql, dict(params or {})))
        return self.answers.pop(0) if self.answers else []

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, list(rows)))


def test_digest_key_prefix() -> None:
    assert dc.digest_key("s1") == "summary:s1"


def test_edge_row_fills_every_column_and_marks_loom() -> None:
    row = dc.chain_edge_row(org="o", namespace="n", from_session="a", to_session="b")
    assert set(row) == {
        "org",
        "namespace",
        "from_key",
        "to_key",
        "relation",
        "label",
        "explanation",
        "evidence",
        "confidence",
        "weight",
        "author",
        "run_id",
        "created_at",
        "tombstoned_at",
    }
    assert (row["from_key"], row["to_key"], row["relation"]) == (
        "summary:a",
        "summary:b",
        "precedes",
    )
    assert row["author"] == "loom" and row["run_id"] == "digest_chain"
    assert row["evidence"] == ["a", "b"] and row["tombstoned_at"] is None
    assert isinstance(row["created_at"], datetime)


@pytest.mark.asyncio
async def test_link_digest_writes_prev_and_next_edges() -> None:
    ch = _CH(answers=[[{"session_id": "s1"}], [{"session_id": "s3"}]])
    n = await dc.link_digest(
        ch,
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        session_id="s2",
        at=datetime(2024, 3, 11),
    )
    assert n == 2
    assert len(ch.queries) == 2
    # Both reads scope to the identity and exclude the digest itself.
    for _sql, params in ch.queries:
        assert (params["org"], params["ns"], params["agent"], params["uid"]) == ("o", "n", "a", "u")
        assert params["sid"] == "s2"
    prev_sql, next_sql = ch.queries[0][0], ch.queries[1][0]
    assert "DESC" in prev_sql and "ASC" in next_sql
    table, rows = ch.inserts[0]
    assert table == "loom.operator_edges"
    assert [(r["from_key"], r["to_key"]) for r in rows] == [
        ("summary:s1", "summary:s2"),
        ("summary:s2", "summary:s3"),
    ]


@pytest.mark.asyncio
async def test_link_digest_first_digest_writes_nothing() -> None:
    ch = _CH(answers=[[], []])
    n = await dc.link_digest(
        ch,
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        session_id="s1",
        at=datetime(2024, 3, 11),
    )
    assert n == 0 and ch.inserts == []


@pytest.mark.asyncio
async def test_link_digest_fails_open() -> None:
    ch = _CH(fail=True)
    n = await dc.link_digest(
        ch,
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        session_id="s1",
        at=datetime(2024, 3, 11),
    )
    assert n == 0 and ch.inserts == []


# ---------------------------------------------------------------------------
# The digest writer calls the chain after the digest insert, gated by the
# setting, and a chain failure never fails the digest.
# ---------------------------------------------------------------------------


@dataclass
class _FakeInput:
    org: str = "o"
    namespace: str = "n"
    agent: str = "a"
    user_id: str = "u"
    session_id: str = "s2"


@dataclass
class _FakeState:
    extracted_facts: list[dict[str, Any]] = field(default_factory=lambda: [{"value": "f"}])
    observation_date: str | None = "2024-03-12"
    written: list[dict[str, Any]] = field(default_factory=list)


class _WriterCH(_CH):
    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, list(rows)))


def _draft():
    from server.memory.app.session_summary import SessionSummaryDraft

    return SessionSummaryDraft(
        summary_text="on 2024-03-12, the user moved.",
        embedding=[0.1] * 4,
        embedding_model="fake-4",
    )


@pytest.mark.asyncio
async def test_digest_insert_then_chain_edges(monkeypatch) -> None:
    from server.memory.app import bulk_set as _bulk_set_mod
    from server.memory.app import session_summary as _session_summary_mod
    from server.memory.app.session_summary import _insert_session_summary

    ch = _WriterCH(answers=[[{"session_id": "s1"}], []])
    monkeypatch.setattr(_bulk_set_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_session_summary_mod, "get_client", lambda: ch)
    await _insert_session_summary(_FakeInput(), _FakeState(), _draft())
    tables = [t for t, _ in ch.inserts]
    assert tables == ["loom.session_summaries", "loom.operator_edges"]
    edge = ch.inserts[1][1][0]
    assert (edge["from_key"], edge["to_key"], edge["relation"]) == (
        "summary:s1",
        "summary:s2",
        "precedes",
    )
    # The chain read is keyed by the digest's event time (valid_at) and identity.
    _sql, params = ch.queries[0]
    assert params["at"] == datetime(2024, 3, 12) and params["agent"] == "a" and params["uid"] == "u"


@pytest.mark.asyncio
async def test_chain_failure_leaves_the_digest(monkeypatch) -> None:
    from server.memory.app import bulk_set as _bulk_set_mod
    from server.memory.app import session_summary as _session_summary_mod
    from server.memory.app.session_summary import _insert_session_summary

    class _NoQuery:
        def __init__(self):
            self.inserts = []

        async def insert(self, table, rows, settings=None):
            self.inserts.append((table, list(rows)))

    ch = _NoQuery()  # no query_all at all: the chain raises, the digest stays
    monkeypatch.setattr(_bulk_set_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_session_summary_mod, "get_client", lambda: ch)
    await _insert_session_summary(_FakeInput(), _FakeState(), _draft())
    assert [t for t, _ in ch.inserts] == ["loom.session_summaries"]
