"""The dataset catalog under the Postgres authority (`datasets_repo` with `pg_datasets`
faked): a write commits to Postgres and then projects, a read takes Postgres's rows and
those only ClickHouse holds, a delete removes Postgres's rows first, and a refused
approval rolls back."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime
from typing import Any

import pytest

from server.core import pg
from server.knowledge.store import datasets as repo
from server.knowledge.store import pg_datasets


def _row(dataset_id: str, version: int, *, position: int = 0, snapshot: int = 1) -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "ns",
        "source_uri": "repo://a.csv",
        "dataset_id": dataset_id,
        "doc_version": 100,
        "snapshot_id": snapshot,
        "position": position,
        "version": version,
        "user_id": "u",
    }


class _CH:
    """ClickHouse: the catalog rows it holds and the inserts sent to it."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, *, fail: bool = False) -> None:
        self.rows = rows or []
        self.inserted: list[dict[str, Any]] = []
        self.fail = fail

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        if self.fail:
            raise ConnectionError("ClickHouse is down")
        self.inserted.extend(rows)

    async def query_all(self, sql: str, params: Any = None, **_: Any) -> list[dict[str, Any]]:
        if "DISTINCT dataset_id" in sql:
            return [{"dataset_id": r["dataset_id"]} for r in self.rows]
        if "DISTINCT snapshot_id" in sql:
            return []
        return [dict(r) for r in self.rows]

    async def execute(self, sql: str, params: Any = None, **_: Any) -> None:
        return None


@pytest.fixture
def on_pg(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    held: dict[str, Any] = {"rows": [], "upserted": [], "jobs": [], "newer": set()}

    async def upsert(rows: list[dict[str, Any]], *, job: Any = None) -> list[dict[str, Any]]:
        held["upserted"].extend(rows)
        held["jobs"].append(job)
        return [r for r in rows if r["dataset_id"] not in held["newer"]]

    async def at_version(*a: Any, **k: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in held["rows"]]

    async def forget_published(org: str, ns: str, through: Any) -> tuple[set[str], set[int]]:
        return {"ds_pg_only"}, {42}

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_datasets, "upsert", upsert)
    monkeypatch.setattr(pg_datasets, "at_version", at_version)
    monkeypatch.setattr(pg_datasets, "forget_published", forget_published)
    return held


def test_a_catalog_write_commits_to_postgres_then_projects(on_pg: dict[str, Any]) -> None:
    ch = _CH()
    rows = [_row("ds_a", 5)]
    asyncio.run(repo.insert_catalog(rows, ch=ch))  # type: ignore[arg-type]
    assert on_pg["upserted"] == rows and ch.inserted == rows


def test_a_row_postgres_keeps_out_is_not_projected(on_pg: dict[str, Any]) -> None:
    """A key Postgres holds at a newer version keeps the older row out of ClickHouse too."""
    ch = _CH()
    on_pg["newer"].add("ds_old")
    rows = [_row("ds_a", 5), _row("ds_old", 6)]
    asyncio.run(repo.insert_catalog(rows, ch=ch))  # type: ignore[arg-type]
    assert on_pg["upserted"] == rows and ch.inserted == rows[:1]


def test_a_projection_that_fails_leaves_the_commit_and_warns(
    on_pg: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    rows = [_row("ds_a", 5)]
    with caplog.at_level(logging.WARNING, logger="server.knowledge.store.datasets"):
        asyncio.run(repo.insert_catalog(rows, ch=_CH(fail=True)))  # type: ignore[arg-type]
    assert on_pg["upserted"] == rows
    assert any("not projected" in r.getMessage() for r in caplog.records)


def test_a_read_takes_postgres_s_rows_and_those_only_clickhouse_holds(
    on_pg: dict[str, Any],
) -> None:
    """The newer version of a dataset wins; a dataset an older build published (only in
    ClickHouse) is read too; table order holds across the two."""
    on_pg["rows"] = [_row("ds_a", 9, position=1), _row("ds_b", 3, position=2)]
    ch = _CH([_row("ds_a", 8, position=1), _row("ds_b", 4, position=2), _row("ds_c", 1)])
    got = asyncio.run(repo.at_version("o", "ns", "repo://a.csv", 100, ch=ch))  # type: ignore[arg-type]
    assert [(r["dataset_id"], r["version"]) for r in got] == [
        ("ds_c", 1),
        ("ds_a", 9),
        ("ds_b", 4),
    ]


def test_a_forget_deletes_postgres_s_rows_first_and_every_dataset_they_named(
    on_pg: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    sent: list[dict[str, Any]] = []
    ch = _CH([_row("ds_a", 1)])

    async def forget(org: str, ns: str, uris: Any, **k: Any) -> set[str]:
        order.append("postgres")
        return {"ds_pg_only"}

    async def execute(sql: str, params: Any = None, **_: Any) -> None:
        order.append("clickhouse")
        sent.append({"sql": sql, **(params or {})})

    monkeypatch.setattr(pg_datasets, "forget", forget)
    monkeypatch.setattr(ch, "execute", execute)
    asyncio.run(repo.forget("o", "ns", ["repo://a.csv"], before=100, ch=ch))  # type: ignore[arg-type]
    assert order == ["postgres", "clickhouse", "clickhouse"]  # the rows, then the catalog
    rows_delete = next(s for s in sent if "dataset_rows" in s["sql"])
    assert set(rows_delete["ids"]) == {"ds_a", "ds_pg_only"}


def test_a_published_forget_keeps_the_snapshots_postgres_s_later_rows_serve(
    on_pg: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict[str, Any]] = []
    ch = _CH([_row("ds_a", 1)])

    async def execute(sql: str, params: Any = None, **_: Any) -> None:
        sent.append({"sql": sql, **(params or {})})

    monkeypatch.setattr(ch, "execute", execute)
    asyncio.run(repo.forget_published("o", "ns", {"repo://a.csv": 100}, ch=ch))  # type: ignore[arg-type]
    rows_delete = next(s for s in sent if "dataset_rows" in s["sql"])
    assert rows_delete["keep"] == [42] and set(rows_delete["ids"]) == {"ds_a", "ds_pg_only"}


def test_a_row_postgres_cannot_hold_is_written_to_clickhouse_alone(
    on_pg: dict[str, Any],
) -> None:
    ch = _CH()
    held, kept_out = _row("ds_a", 5), {**_row("ds_b", 5), "user_id": "u\x00"}
    asyncio.run(repo.insert_catalog([held, kept_out], ch=ch))  # type: ignore[arg-type]
    assert on_pg["upserted"] == [held] and ch.inserted == [held, kept_out]


def test_a_publish_s_job_reaches_the_postgres_write(on_pg: dict[str, Any]) -> None:
    key = ("o", "ns", "j")
    asyncio.run(repo.insert_catalog([_row("ds_a", 5)], job=key, ch=_CH()))  # type: ignore[arg-type]
    assert on_pg["jobs"] == [key]


def test_a_read_takes_postgres_s_row_when_both_stores_hold_its_version(
    on_pg: dict[str, Any],
) -> None:
    on_pg["rows"] = [{**_row("ds_a", 9), "schema_state": "approved"}]
    ch = _CH([{**_row("ds_a", 9), "schema_state": "needs_clarification"}])
    (got,) = asyncio.run(repo.at_version("o", "ns", "repo://a.csv", 100, ch=ch))  # type: ignore[arg-type]
    assert got["schema_state"] == "approved"


def test_a_catalog_row_round_trips_through_postgres_json_to_the_millisecond() -> None:
    """The times a row is built with are DateTime64(3)'s, so the row read back from
    Postgres compares equal to the one ClickHouse serves; an unset approval stays None."""
    fields = {
        c: 0 for c in repo.DATASET_COLUMNS if c != "version" and c not in pg_datasets.TIME_COLUMNS
    }
    approved = repo.dataset_row(
        None, **{**fields, "approved_at": datetime(2026, 1, 2, 3, 4, 5, 678901)}
    )
    pending = repo.dataset_row(None, **{**fields, "approved_at": None})
    for row in (approved, pending):
        back = pg_datasets._decode(json.loads(json.dumps(pg_datasets._encode(row))))
        assert back == row
        assert all(
            row[c] is None or row[c].microsecond % 1000 == 0 for c in pg_datasets.TIME_COLUMNS
        )
    assert approved["approved_at"].microsecond == 678000 and pending["approved_at"] is None


class _Conn:
    """A Postgres connection answering the approval's head read and its writes."""

    def __init__(
        self, head: dict[str, Any] | None, approved: bool = True, erased: bool = False
    ) -> None:
        self.head, self.approved, self.erased = head, approved, erased

    async def execute(self, *a: Any) -> None:
        return None

    async def fetchrow(self, *a: Any) -> dict[str, Any] | None:
        return self.head

    async def fetchval(self, sql: str, *a: Any) -> Any:
        if "INSERT" in sql:
            return 1 if self.approved else None
        return self.erased or None  # the erasure evidence

    async def fetch(self, *a: Any) -> list[dict[str, Any]]:
        return [{"dataset_id": "ds_a"}]


def _transactions(monkeypatch: pytest.MonkeyPatch, conn: _Conn) -> list[str]:
    """Fake `pg.transaction`: each one's end, `commit` or `rollback`."""
    ends: list[str] = []

    @contextlib.asynccontextmanager
    async def transaction() -> Any:
        try:
            yield conn
        except BaseException:
            ends.append("rollback")
            raise
        ends.append("commit")

    monkeypatch.setattr(pg, "transaction", transaction)
    return ends


@pytest.mark.parametrize(
    ("head", "approved", "erased"),
    [({"version": 101, "deleted": False}, True, False),
     ({"version": 100, "deleted": True}, True, False),
     ({"version": 100, "deleted": False}, True, True),
     (None, False, False)],
)  # fmt: skip
def test_a_refused_approval_rolls_back(
    monkeypatch: pytest.MonkeyPatch, head: dict[str, Any] | None, approved: bool, erased: bool
) -> None:
    """A refusal commits nothing, so its locks never wait for the standby: it answers
    `moved`, not a 503 whose outcome is unknown."""
    ends = _transactions(monkeypatch, _Conn(head, approved, erased))
    new = {**_row("ds_a", 9), "doc_version": 100}
    assert asyncio.run(pg_datasets.approve(new, read=9, doc_id="d")) == "moved"
    assert ends == ["rollback"]


def test_an_approval_over_the_reviewed_head_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    ends = _transactions(monkeypatch, _Conn({"version": 100, "deleted": False}))
    new = {**_row("ds_a", 9), "doc_version": 100}
    assert asyncio.run(pg_datasets.approve(new, read=9, doc_id="d")) == "switched"
    assert ends == ["commit"]


def test_a_forget_deletes_in_a_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """A commit the standby's wait leaves unknown then answers `outcome_unknown`."""
    ends = _transactions(monkeypatch, _Conn(None))
    assert asyncio.run(pg_datasets.forget("o", "ns", ["repo://a.csv"], before=100)) == {"ds_a"}
    assert ends == ["commit"]
