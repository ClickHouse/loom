"""The learnings queue's erasure step on clickhouse-local: a user erasure deletes the
user's supporter rows, deletes a pending candidate left unsupported and blanks a
decided one, and a review or a filing that races the step is never lost."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from server.erasure.store import steps_ledgers as ledger_steps
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

_CANDIDATE, _OTHER = "00000000-0000-0000-0000-0000000000a1", "00000000-0000-0000-0000-0000000000b2"


def _candidate(cid: str, status: str, value: str, version: int) -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "ns",
        "candidate_id": cid,
        "memory_key": "rule",
        "memory_type": "fact",
        "proposed_value": value,
        "supporter_count": 1,
        "confidence_avg": 1.0,
        "supporters": ["alice"],
        "status": status,
        "version": version,
        "source_kind": "assistant_turn",
    }


def _supporter(cid: str, user_id: str) -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "ns",
        "agent": "bot",
        "candidate_id": cid,
        "user_id": user_id,
        "session_id": "s",
        "confidence": 1.0,
        "asked": f"{user_id} asked",
        "said": "the rule",
    }


class _LocalCH:
    """A client over one clickhouse-local database kept in `path`: the step's statements
    run on real tables, in order. `late` rows ({table: rows}) land after everything the
    step read and wrote and before it re-reads the supporters of its pending candidates
    or first deletes from loom.learnings_queue, the window a review or a filing can race
    into."""

    def __init__(self, path, rows: dict[str, list[dict[str, Any]]], late=None) -> None:
        self.path, self.late = path, late or {}
        self.statements: list[str] = []
        run([ddl("learnings_queue"), ddl("learnings_supporters")], "SELECT 1", path=path)
        for table, table_rows in rows.items():
            self._write(table, table_rows)

    def _write(self, table: str, rows: list[dict[str, Any]]) -> None:
        columns = ", ".join(rows[0])
        run(
            [
                f"INSERT INTO loom.{table} ({columns}) SELECT * FROM format(JSONEachRow, {{rows:String}})"
            ],
            "SELECT 1",
            {"rows": "\n".join(json.dumps(r) for r in rows)},
            path=self.path,
        )

    async def query_one(self, sql, params=None):
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None

    def _land_late(self) -> None:
        for table, rows in self.late.items():
            self._write(table, rows)
        self.late = {}

    async def query_all(self, sql, params=None):
        if "system.mutations" in sql:  # execute ran each mutation to the end
            return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]
        if "FROM loom.learnings_supporters" in sql and "{cids:Array(UUID)}" in sql:
            self._land_late()
        return run([], sql, params, path=self.path)

    async def execute(self, sql, params=None):
        if "ALTER TABLE loom.learnings_queue" in sql:
            self._land_late()
        self.statements.append(sql)
        # The step waits for its mutation; a synchronous one is the same wait.
        sync = sql.replace("mutations_sync = 0", "mutations_sync = 2")
        run([sync], "SELECT 1", params, path=self.path)

    async def insert(self, table, rows, settings=None):
        self.statements.append(f"INSERT INTO {table}")
        self._write(table.removeprefix("loom."), rows)

    def queue(self) -> list[tuple[str, str, str, int]]:
        rows = run(
            [],
            "SELECT candidate_id, status, proposed_value, version FROM loom.learnings_queue "
            "ORDER BY candidate_id, version",
            path=self.path,
        )
        return [
            (r["candidate_id"], r["status"], r["proposed_value"], int(r["version"])) for r in rows
        ]

    def supporters(self) -> list[tuple[str, str]]:
        rows = run(
            [],
            "SELECT candidate_id, user_id FROM loom.learnings_supporters ORDER BY user_id",
            path=self.path,
        )
        return [(r["candidate_id"], r["user_id"]) for r in rows]


def _erase_alice(ch: _LocalCH) -> None:
    asyncio.run(ledger_steps.delete_learnings_queue_by_user("o", "alice", ch=ch))


_needs_clickhouse = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


@_needs_clickhouse
def test_the_learnings_user_step_deletes_the_users_support_rows_only(tmp_path) -> None:
    """A user's words live on their supporter rows: the step deletes those, and a
    candidate someone else still supports keeps its rule."""
    filed = _candidate(_CANDIDATE, "pending", '"rule"', 1)
    ch = _LocalCH(
        tmp_path,
        {
            "learnings_queue": [filed],
            "learnings_supporters": [
                _supporter(_CANDIDATE, "alice"),
                _supporter(_CANDIDATE, "bob"),
            ],
        },
    )
    _erase_alice(ch)
    assert ch.supporters() == [(_CANDIDATE, "bob")]
    assert ch.queue() == [(_CANDIDATE, "pending", '"rule"', 1)]


@_needs_clickhouse
def test_an_orphaned_candidate_goes_when_pending_and_keeps_its_decision_when_decided(
    tmp_path,
) -> None:
    ch = _LocalCH(
        tmp_path,
        {
            "learnings_queue": [
                _candidate(_CANDIDATE, "pending", '"rule"', 1),
                _candidate(_OTHER, "rejected", '"rule"', 7),
            ],
            "learnings_supporters": [_supporter(_CANDIDATE, "alice"), _supporter(_OTHER, "alice")],
        },
    )
    _erase_alice(ch)
    # The pending one goes whole; the rejected one keeps its decision with the rule blanked.
    assert ch.queue() == [(_OTHER, "rejected", "", 8)]
    assert ch.supporters() == []
    # The blank row is written before the rows holding the rule are deleted.
    deletes = [
        i for i, sql in enumerate(ch.statements) if "ALTER TABLE loom.learnings_queue" in sql
    ]
    assert ch.statements.index("INSERT INTO loom.learnings_queue") < deletes[-1]


@_needs_clickhouse
def test_a_review_between_the_read_and_the_delete_survives_and_the_reopened_step_blanks_it(
    tmp_path,
) -> None:
    """The step read a pending candidate; a review rejected it before the delete.
    The delete spares the review row, which still holds the rule, so the census
    counts it and the reopened step blanks it."""
    ch = _LocalCH(
        tmp_path,
        {"learnings_queue": [_candidate(_CANDIDATE, "pending", '"r"', 1)]},
        late={"learnings_queue": [_candidate(_CANDIDATE, "rejected", '"r"', 9)]},
    )
    _erase_alice(ch)
    assert ch.queue() == [(_CANDIDATE, "rejected", '"r"', 9)]
    _erase_alice(ch)
    assert ch.queue() == [(_CANDIDATE, "rejected", "", 10)]


@_needs_clickhouse
def test_a_candidate_a_supporter_backed_after_the_read_survives_the_pending_delete(
    tmp_path,
) -> None:
    """The step read a pending candidate with no supporter; a filing then wrote its
    supporter row before the delete. The step re-reads the supporter rows right before
    the delete, so the candidate stays."""
    filed = _candidate(_CANDIDATE, "pending", '"r"', 1)
    ch = _LocalCH(
        tmp_path,
        {"learnings_queue": [filed]},
        late={"learnings_supporters": [_supporter(_CANDIDATE, "bob")]},
    )
    _erase_alice(ch)
    assert ch.queue() == [(_CANDIDATE, "pending", '"r"', 1)]


@_needs_clickhouse
def test_a_pending_candidate_nobody_supports_goes(tmp_path) -> None:
    ch = _LocalCH(tmp_path, {"learnings_queue": [_candidate(_CANDIDATE, "pending", '"r"', 1)]})
    _erase_alice(ch)
    assert ch.queue() == []


@_needs_clickhouse
def test_a_review_landing_after_the_blank_write_survives_the_decided_delete(tmp_path) -> None:
    """The step read a rejected candidate and wrote its blank row; a review then
    promoted it before the delete. The delete takes only what was read, so the newer
    review row stays (holding the rule) for the reopened step to blank."""
    ch = _LocalCH(
        tmp_path,
        {"learnings_queue": [_candidate(_CANDIDATE, "rejected", '"r"', 7)]},
        late={"learnings_queue": [_candidate(_CANDIDATE, "promoted", '"r"', 20)]},
    )
    _erase_alice(ch)
    assert ch.queue() == [(_CANDIDATE, "rejected", "", 8), (_CANDIDATE, "promoted", '"r"', 20)]


@_needs_clickhouse
def test_a_late_review_on_the_older_of_two_decided_candidates_survives(tmp_path) -> None:
    """The step read candidate a at version 200 and b at version 100; b was then
    re-reviewed at version 150 (a replica's clock behind). b's bound is its own read
    version, not a's, so the review row stays for the reopened step to blank."""
    a, b = _CANDIDATE, _OTHER
    ch = _LocalCH(
        tmp_path,
        {
            "learnings_queue": [
                _candidate(a, "rejected", '"r"', 200),
                _candidate(b, "rejected", '"s"', 100),
            ]
        },
        late={"learnings_queue": [_candidate(b, "promoted", '"s"', 150)]},
    )
    _erase_alice(ch)
    assert ch.queue() == [
        (a, "rejected", "", 201),
        (b, "rejected", "", 101),
        (b, "promoted", '"s"', 150),
    ]


def test_a_rerun_binds_the_same_delete_slices_whatever_order_it_reads(monkeypatch) -> None:
    # the mutation tag hashes the bound values, so a resumed step finds its own mutations
    from server.core.ch_params import chunks

    ids = [f"00000000-0000-0000-0000-{i:012d}" for i in range(40)]
    latest = [_candidate(c, "pending" if i % 2 else "approved", "", 1) for i, c in enumerate(ids)]
    bound: list[Any] = []

    async def record(table: str, *, params: dict[str, Any], **_: Any) -> int:
        bound.append(params.get("cids") or params.get("decided"))
        return 0

    class _Reads:
        def __init__(self, order: list[dict[str, Any]]) -> None:
            self.order = order

        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            if "SELECT DISTINCT candidate_id" in sql:
                return [{"candidate_id": r["candidate_id"]} for r in self.order]
            return self.order if "SELECT * FROM loom.learnings_queue" in sql else []

        async def insert(self, *a: Any, **k: Any) -> None:
            return None

    monkeypatch.setattr(ledger_steps, "delete_scoped", record)
    monkeypatch.setattr(ledger_steps, "chunks", lambda v: chunks(v, limit=400))  # several slices
    runs = []
    for order in (latest, latest[::-1]):
        bound.clear()
        asyncio.run(ledger_steps.delete_learnings_queue_by_user("o", "alice", ch=_Reads(order)))
        runs.append(list(bound))
    assert len(runs[0]) > 3 and runs[0] == runs[1]
