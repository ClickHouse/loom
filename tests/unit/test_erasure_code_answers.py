"""code.ask's answer cache under erasure, on clickhouse-local: an answer that quoted
memory excerpts names their namespace and keys, and the namespace and user sagas delete
it, its tombstone versions included, while the census counts it until they do. An answer
drawn from the code index alone, or from another namespace's memory, stays."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from server.erasure.store import census as erasure_census
from server.erasure.store import mutations as erasure_mutations
from server.erasure.store import steps_graph as graph_steps
from server.erasure.store import steps_memory as memory_steps
from server.memory.store import redaction
from tests.unit.clickhouse_local import CLICKHOUSE, VECTOR, ddl, run

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


def _answer(qid: str, *, memory_ns: str = "", keys: tuple[str, ...] = (), **kw: Any) -> dict:
    return {
        "org": "o",
        "namespace": "code",
        "question_id": f"00000000-0000-0000-0000-{qid:0>12}",
        "question": "why is the guard there?",
        "embedding_model": "m",
        "question_embedding": json.loads(VECTOR),
        "answer": f"answer {qid}",
        "memory_namespace": memory_ns,
        "memory_keys": list(keys),
        "tombstoned_at": None,
        "version": 1,
        **kw,
    }


_ROWS = [
    _answer("1", memory_ns="decisions", keys=("alice_decision",)),
    # a tombstoned answer keeps the text it retired; each row has its own key, so no
    # merge collapses one into another
    _answer(
        "5",
        memory_ns="decisions",
        keys=("alice_decision",),
        tombstoned_at="2030-01-01 00:00:00",
    ),
    _answer("2", memory_ns="decisions", keys=("bob_decision",)),
    _answer("3"),  # the code index alone
    _answer("4", memory_ns="other", keys=("carol_note",)),
]


class _LocalCH:
    """One clickhouse-local database kept in `path` with loom.code_answers and alice's
    memory row; the census reads every other substrate as empty."""

    def __init__(self, path) -> None:
        self.path = path
        run(
            ["SET enable_quantized_codec = 1", ddl("code_answers"), ddl("memory")],
            "SELECT 1",
            path=path,
        )
        memory = {"org": "o", "namespace": "decisions", "memory_key": "alice_decision"}
        for table, rows in (("code_answers", _ROWS), ("memory", [{**memory, "user_id": "alice"}])):
            cols = ", ".join(rows[0])
            run(
                [
                    f"INSERT INTO loom.{table} ({cols}) "
                    "SELECT * FROM format(JSONEachRow, {rows:String})"
                ],
                "SELECT 1",
                {"rows": "\n".join(json.dumps(r) for r in rows)},
                path=path,
            )

    async def query_one(self, sql, params=None):
        own_memory_count = " ".join(sql.split()).startswith("SELECT count() AS n FROM loom.memory ")
        if "loom.code_answers" not in sql and " AS keys" not in sql and not own_memory_count:
            return {"n": 0}
        rows = run([], sql, params, path=self.path)
        return rows[0] if rows else None

    async def query_all(self, sql, params=None):
        if "system.mutations" in sql:
            return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]
        if "loom.code_answers" not in sql:
            return []
        return run([], sql, params, path=self.path)

    async def execute(self, sql, params=None):
        sync = sql.replace("mutations_sync = 0", "mutations_sync = 2").replace(
            "lightweight_deletes_sync = 0", "lightweight_deletes_sync = 2"
        )
        run([sync], "SELECT 1", params, path=self.path)

    def left(self) -> list[str]:
        rows = run([], "SELECT answer FROM loom.code_answers ORDER BY answer", path=self.path)
        return [r["answer"] for r in rows]


def _census(ch: _LocalCH, scope_type: str, value: Any) -> int:
    out = asyncio.run(erasure_census.residue_census("o", scope_type, value, ch=ch))
    return out["loom.code_answers"]


@pytest.mark.parametrize("scope", ["decisions", ["decisions", "unrelated"]])
def test_a_namespace_erasure_deletes_the_answers_that_quoted_it(tmp_path, scope) -> None:
    ch = _LocalCH(tmp_path)
    assert _census(ch, "namespace", scope) == 3  # answers 1 and 2, and the tombstoned 5
    n = asyncio.run(graph_steps.delete_code_answers_by_namespace("o", scope, ch=ch))
    assert n == 3
    assert ch.left() == ["answer 3", "answer 4"]
    assert _census(ch, "namespace", scope) == 0


def test_a_tombstone_holding_no_text_leaves_the_census_clean(tmp_path) -> None:
    # written from a row read before the delete, it lands after it: keys, no text
    ch = _LocalCH(tmp_path)
    asyncio.run(graph_steps.delete_code_answers_by_namespace("o", "decisions", ch=ch))
    tomb = _answer("6", memory_ns="decisions", keys=("alice_decision",), answer="", citations="")
    run(
        [
            f"INSERT INTO loom.code_answers ({', '.join(tomb)}) "
            "SELECT * FROM format(JSONEachRow, {rows:String})"
        ],
        "SELECT 1",
        {"rows": json.dumps({**tomb, "tombstoned_at": "2030-01-01 00:00:00"})},
        path=tmp_path,
    )
    assert _census(ch, "namespace", "decisions") == 0
    assert _census(ch, "user_id", "alice") == 0


def test_a_user_erasure_deletes_the_answers_that_quoted_the_users_keys(tmp_path) -> None:
    ch = _LocalCH(tmp_path)
    assert _census(ch, "user_id", "alice") == 2  # answer 1 and the tombstoned 5
    n = asyncio.run(graph_steps.delete_code_answers_by_user("o", "alice", ch=ch))
    assert n == 2
    assert ch.left() == ["answer 2", "answer 3", "answer 4"]
    assert _census(ch, "user_id", "alice") == 0


def test_a_user_purge_reads_the_users_keys_before_their_memory_rows_go(tmp_path) -> None:
    ch = _LocalCH(tmp_path)

    async def saga() -> int:
        await memory_steps.tombstone_memory_by_user("o", "alice", ch=ch)
        return await graph_steps.delete_code_answers_by_user("o", "alice", ch=ch)

    with erasure_mutations.purge_scope(True):
        assert asyncio.run(saga()) == 2
    assert ch.left() == ["answer 2", "answer 3", "answer 4"]
    # tombstoned, not deleted: `memory.retire` deletes the rows once every step ran
    assert run([], "SELECT count() AS n FROM loom.memory", path=ch.path) == [{"n": 1}]


def test_a_redact_deletes_the_answers_that_quoted_the_key(tmp_path) -> None:
    ch = _LocalCH(tmp_path)
    assert asyncio.run(redaction.delete_code_answers_by_key("o", "bob_decision", ch=ch)) == 1
    assert ch.left() == ["answer 1", "answer 3", "answer 4", "answer 5"]


def test_the_table_is_censused_in_both_scopes_by_the_code_answers_step() -> None:
    sub = next(s for s in erasure_census._CENSUS if s.table == "loom.code_answers")
    assert sub.owner("namespace") == sub.owner("user_id") == "code_answers.delete"
    assert "loom.code_answers" not in erasure_census.NO_MEMORY_CONTENT


def _request(status: str, requested_at: str, org: str = "o", completed_at: Any = None) -> dict:
    return {
        "completed_at": completed_at,
        "id": "00000000-0000-0000-0000-0000000000e1",
        "org": org,
        "scope_type": "user_id",
        "scope_value": "alice",
        "requester": "ops",
        "status": status,
        "requested_at": requested_at,
        "version": 1,
    }


@pytest.mark.parametrize(
    ("row", "pending"),
    [
        (None, False),
        (_request("completed", "2030-01-01 00:00:00", completed_at="2030-01-01 00:04:00"), False),
        # requested since the ask began: it completes after it
        (_request("completed", "2030-01-01 00:10:00", completed_at="2030-01-01 00:11:00"), True),
        (_request("in_progress", "2030-01-01 00:00:00"), True),  # still running
        (_request("queued", "2030-01-01 00:00:00"), True),
        (_request("in_progress", "2030-01-01 00:00:00", org="other"), False),
        # requested before the ask and finished during it: its delete may have passed
        (_request("completed", "2030-01-01 00:00:00", completed_at="2030-01-01 00:06:00"), True),
        # a failed one counts until it completes
        (_request("failed", "2029-12-01 00:00:00", completed_at="2029-12-01 00:06:00"), True),
    ],
)
def test_the_erasure_ledger_read_a_cache_write_waits_on(row, pending) -> None:
    from datetime import UTC, datetime

    from server.erasure.store import ledger

    setup = [ddl("erasure_requests")]
    if row is not None:
        cols = ", ".join(row)
        setup.append(
            f"INSERT INTO loom.erasure_requests ({cols}) "
            f"SELECT * FROM format(JSONEachRow, $${json.dumps(row)}$$)"
        )

    class _CH:
        async def query_all(self, sql, params=None):
            return run(setup, sql, params)

    since_s = int(datetime(2030, 1, 1, 0, 5, 0, tzinfo=UTC).timestamp())
    rows = asyncio.run(ledger.unfinished_since("o", since_s, 0, ch=_CH()))
    assert bool(rows) is pending


def test_the_reads_that_decide_a_code_answer_delete_are_sequential() -> None:
    # an answer another replica just stored, or a redaction stamp it just wrote, must be
    # seen: a count of 0 issues no delete, and an unseen stamp leaves the answer cached
    from server.core.ch import sequential_reads_active

    seen: list[tuple[str, bool]] = []

    class _CH:
        async def query_one(self, sql, params=None):
            seen.append((" ".join(sql.split())[:60], sequential_reads_active()))
            return {"n": 0, "keys": ["alice_decision"]}

        async def query_all(self, sql, params=None):
            seen.append((" ".join(sql.split())[:60], sequential_reads_active()))
            return []

    asyncio.run(redaction.keys_redacted("o", "decisions", ["alice_decision"], ch=_CH()))
    asyncio.run(redaction.delete_code_answers_by_key("o", "alice_decision", ch=_CH()))
    asyncio.run(graph_steps.delete_code_answers_by_namespace("o", "decisions", ch=_CH()))
    asyncio.run(graph_steps.delete_code_answers_by_user("o", "alice", ch=_CH()))
    assert len(seen) >= 4
    assert all(sequential for _, sequential in seen), seen


def test_the_delete_after_a_store_takes_every_answer_quoting_the_keys(tmp_path) -> None:
    ch = _LocalCH(tmp_path)
    asyncio.run(redaction.delete_code_answers_quoting("o", ["alice_decision", "nobody"], ch=ch))
    assert ch.left() == ["answer 2", "answer 3", "answer 4"]  # 1 and the tombstoned 5 went


def test_a_key_with_a_redacted_version_reads_as_redacted() -> None:
    version = {
        "org": "o",
        "namespace": "decisions",
        "memory_key": "alice_decision",
        "user_id": "alice",
        "version": 1,
    }
    rows = [
        {**version, "redacted_at": "2030-01-01 00:00:00"},
        {**version, "memory_key": "bob_decision", "version": 2},  # never redacted
    ]
    setup = [
        ddl("memory_versions"),
        "INSERT INTO loom.memory_versions (org, namespace, memory_key, user_id, version, "
        "redacted_at) SELECT org, namespace, memory_key, user_id, version, redacted_at "
        f"FROM format(JSONEachRow, $${chr(10).join(json.dumps(r) for r in rows)}$$)",
    ]

    class _CH:
        async def query_one(self, sql, params=None):
            out = run(setup, sql, params)
            return out[0] if out else None

    def redacted(ns: str, keys: list[str]) -> bool:
        return asyncio.run(redaction.keys_redacted("o", ns, keys, ch=_CH()))

    assert redacted("decisions", ["bob_decision", "alice_decision"]) is True
    assert redacted("decisions", ["bob_decision"]) is False
    assert redacted("other", ["alice_decision"]) is False


def test_the_ledger_read_stops_at_its_limit() -> None:
    from datetime import UTC, datetime

    from server.erasure.store import ledger

    rows = [
        {
            **_request("queued", "2030-01-01 00:00:00"),
            "id": f"00000000-0000-0000-0000-0000000000e{i}",
        }
        for i in (1, 2)
    ]
    setup = [
        ddl("erasure_requests"),
        f"INSERT INTO loom.erasure_requests ({', '.join(rows[0])}) "
        f"SELECT * FROM format(JSONEachRow, $${chr(10).join(json.dumps(r) for r in rows)}$$)",
    ]

    class _CH:
        async def query_all(self, sql, params=None):
            return run(setup, sql, params)

    since_s = int(datetime(2030, 1, 1, 0, 5, 0, tzinfo=UTC).timestamp())
    assert len(asyncio.run(ledger.unfinished_since("o", since_s, 0, ch=_CH()))) == 2
    assert len(asyncio.run(ledger.unfinished_since("o", since_s, 0, limit=1, ch=_CH()))) == 1
