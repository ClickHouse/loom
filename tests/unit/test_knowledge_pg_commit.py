"""`pg_documents.commit`: a head replaced only over what its writer read, a first head
refused to the second of two writers, and a write built from an erased document
refused, against a fake Postgres connection."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest

from server.core import pg
from server.knowledge.store import pg_documents


class _Conn:
    def __init__(self, held: dict[str, int], *, erased: bool = False, inserted: bool = True):
        self.held, self.erased, self.inserted = held, erased, inserted
        self.statements: list[str] = []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.erased_ids: list[str] = []

    async def execute(self, sql: str, *args: Any) -> None:
        self.statements.append(sql)
        self.calls.append((sql, args))

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.statements.append(sql)
        self.calls.append((sql, args))
        # The heads of the sources asked for, in the namespace asked for.
        org, namespace, uris = args
        assert (org, namespace) == ("o", "n")
        return [{"source_uri": u, "version": v} for u, v in sorted(self.held.items()) if u in uris]

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.statements.append(sql)
        if "erased_documents" in sql:
            self.erased_ids = args[2]
            return self.erased
        return 1 if self.inserted else None  # the first head's INSERT … RETURNING 1


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> Any:
    box: dict[str, _Conn] = {}

    @contextlib.asynccontextmanager
    async def _transaction() -> AsyncIterator[_Conn]:
        yield box["conn"]

    monkeypatch.setattr(pg, "transaction", _transaction)
    return box


def _row(uri: str, version: int, doc_id: str = "d") -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "n",
        "source_uri": uri,
        "doc_id": doc_id,
        "content": "text",
        "version": version,
        "revision": 1,
        "ingested_at": datetime(2026, 1, 1),
        "superseded_at": None,
    }


def _commit(rows: list[dict[str, Any]], over: dict[str, int], **kw: Any) -> float:
    targets = {r["source_uri"]: pg_documents.DOCUMENT_ROW for r in rows}
    return asyncio.run(pg_documents.commit(rows, over=over, targets=targets, **kw))


@pytest.mark.parametrize(
    ("held", "read", "refused"),
    [
        (5, 5, False),  # the version read: replaced
        (5, 7, False),  # behind a row the writer read from ClickHouse: adopted
        (7, 5, True),  # moved past what the writer read
    ],
)
def test_a_head_is_replaced_only_when_it_is_not_past_the_version_read(
    conn: Any, held: int, read: int, refused: bool
) -> None:
    conn["conn"] = c = _Conn({"a": held})
    if refused:
        with pytest.raises(pg_documents.HeadMoved):
            _commit([_row("a", 9)], {"a": read})
        assert not any("UPDATE loom.knowledge_heads" in s for s in c.statements)
    else:
        _commit([_row("a", 9)], {"a": read})
        assert any("UPDATE loom.knowledge_heads" in s for s in c.statements)
        assert any("INSERT INTO loom.knowledge_deliveries" in s for s in c.statements)


@pytest.mark.parametrize(("held", "read", "row"), [(5, 5, 5), (5, 5, 4), (5, 7, 6)])
def test_a_row_not_above_what_it_replaces_is_refused(
    conn: Any, held: int, read: int, row: int
) -> None:
    """A writer whose clock runs behind, or whose version was taken before its read,
    would move the head back under a newer write."""
    conn["conn"] = c = _Conn({"a": held})
    with pytest.raises(pg_documents.HeadMoved):
        _commit([_row("a", row)], {"a": read})
    assert not any("UPDATE loom.knowledge_heads" in s for s in c.statements)


def test_a_commit_naming_a_source_twice_is_refused_before_any_statement(conn: Any) -> None:
    """Every row of a commit is published, so each must be its source's head."""
    conn["conn"] = c = _Conn({"a": 5})
    with pytest.raises(ValueError, match="one row per source"):
        _commit([_row("a", 8), _row("a", 9)], {"a": 5})
    assert c.statements == []


def test_the_second_of_two_first_commits_is_refused(conn: Any) -> None:
    """No head to lock: the head's insert finds the concurrent writer's row."""
    conn["conn"] = c = _Conn({}, inserted=False)
    with pytest.raises(pg_documents.HeadMoved):
        _commit([_row("a", 9)], {})
    assert not any("knowledge_deliveries" in s for s in c.statements)


def test_a_write_built_from_an_erased_document_is_refused(conn: Any) -> None:
    """Checked by the row's own id and the live document it carries columns from, after
    the namespace lock and the heads' locks."""
    conn["conn"] = c = _Conn({"a": 5}, erased=True)
    with pytest.raises(pg_documents.DocumentErased):
        _commit([_row("a", 9, doc_id="mine")], {"a": 5}, built_from=["read"])
    assert c.erased_ids == ["mine", "read"]
    order = [
        next(i for i, s in enumerate(c.statements) if mark in s)
        for mark in ("pg_advisory_xact_lock_shared", "FOR UPDATE", "erased_documents")
    ]
    assert order == sorted(order)
    assert not any("knowledge_deliveries" in s for s in c.statements)


def test_the_locks_name_the_commits_namespace_and_its_sources_in_key_order(conn: Any) -> None:
    conn["conn"] = c = _Conn({"a": 5, "b": 5})
    _commit([_row("b", 9), _row("a", 9)], {"a": 5, "b": 5})
    lock = next(args for sql, args in c.calls if "pg_advisory_xact_lock_shared" in sql)
    heads = next(args for sql, args in c.calls if "FOR UPDATE" in sql)
    assert lock == ("o", "n")
    assert heads == ("o", "n", ["a", "b"])  # sorted: every locker's order
