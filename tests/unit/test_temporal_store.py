"""The journal store's statement shapes: the branches that decide which rows a
read sees (erasure fences, filters, the 'high' fence) and what a write or a
redaction sends. The SQL itself runs in tests/integration/test_bitemporal_memory.py."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters
from server.memory.domain.temporal_model import first as tm_first
from server.memory.store import temporal as t
from server.memory.store.redaction import REDACTED_VALUE

pytestmark = pytest.mark.temporal_journal

IDENT = ("a", "u", "s", "k")


class _CH:
    """Records every statement; answers reads from `rows` / `one`."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, one: Any = None) -> None:
        self.rows, self.one, self.calls = rows or [], one, []

    async def query_all(self, sql: str, params: Any = None, **_: Any) -> list[dict[str, Any]]:
        self.calls.append(("query_all", sql, params))
        return self.rows

    async def query_one(self, sql: str, params: Any = None, **_: Any) -> Any:
        self.calls.append(("query_one", sql, params))
        return self.one

    async def execute(self, sql: str, params: Any = None, **kw: Any) -> None:
        self.calls.append(("execute", sql, params, kw.get("settings")))

    async def insert(self, table: str, rows: Any, **kw: Any) -> None:
        self.calls.append(("insert", table, rows, kw.get("settings")))


def _sha(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


def test_erasure_fence_binds_namespace_and_user_cuts() -> None:
    params: dict[str, Any] = {}
    assert t._not_erased("born_at", t.NOT_ERASED, params) == "" and params == {}
    sql = t._not_erased("born_at", t.Erased(namespace_us=5, users=(("u1", 7),)), params)
    assert "toUnixTimestamp64Micro(born_at) >= {er_ns:Int64}" in sql
    assert "NOT arrayExists" in sql
    assert params == {"er_ns": 5, "er_users": [("u1", 7)]}


async def test_the_journal_refuses_a_partial_row() -> None:
    ch = _CH()
    await t.insert_commits([], ch=ch)
    await t.insert_assertions([], ch=ch)
    assert ch.calls == []
    full = dict.fromkeys(t.COMMIT_COLS, "")
    partial = {k: v for k, v in full.items() if k != "retain_until"} | {"extra": 1}
    with pytest.raises(ValueError, match=r"missing=\['retain_until'\] unknown=\['extra'\]"):
        await t.insert_commits([partial], ch=ch)
    with pytest.raises(ValueError, match="memory_temporal_assertions row #0"):
        await t.insert_assertions([{"org": "o"}], ch=ch)
    assert ch.calls == []
    await t.insert_commits([full], ch=ch)
    await t.insert_assertions([dict.fromkeys(t.ASSERTION_COLS, "")], ch=ch)
    (_, table, _, settings), (_, table2, _, _) = ch.calls
    assert table == t.COMMITS and settings["async_insert"] == 0
    assert (
        settings["wait_for_async_insert"] == 1 and len(settings["insert_deduplication_token"]) == 64
    )
    await t.insert_commits([full], ch=ch)  # the same rows again: the same token
    assert ch.calls[-1][3]["insert_deduplication_token"] == settings["insert_deduplication_token"]
    assert table2 == t.ASSERTIONS


async def test_empty_inputs_send_nothing() -> None:
    ch = _CH()
    assert await t.commits_by_id("o", "n", [], ch=ch) == []
    assert await t.commits_for_idempotency_key("o", "n", "k", [], ch=ch) == {}
    assert await t.served_for_keys("o", "n", [], ch=ch) == []
    assert await t.current_vectors("o", "n", "m", [], ch=ch) == []
    assert await t.rank_by_vector("o", "n", "m", [0.1], [], limit=5, ch=ch) == {}
    assert await t.memory_heads("o", "n", [], ch=ch) == []
    assert await t.last_commits("o", "n", [], ch=ch) == {}
    assert await t.assertions_by_id("o", "n", [], ch=ch) == []
    assert await t.divergent_rows("o", "n", [], ch=ch) == []
    await t.insert_vectors([], ch=ch)
    await t.delete_assertions("o", "n", [], ch=ch)
    await t.drop_unbacked_vectors("o", "n", [], ch=ch)
    assert ch.calls == []


async def test_server_now_is_utc_whatever_the_driver_returns() -> None:
    naive = datetime(2026, 1, 1, 12)
    assert (await t.server_now(ch=_CH(one={"t": naive}))).tzinfo is UTC
    aware = datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert await t.server_now(ch=_CH(one={"t": aware})) is aware


async def test_head_and_counters_read_an_empty_journal_as_zero() -> None:
    assert await t.head("o", "n", ch=_CH(one=None)) == (0, None, 0, 0)
    assert await t.landed_late("o", "n", ["c"], ch=_CH(one={"late": 1})) is True
    assert await t.landed_late("o", "n", ["c"], ch=_CH(one=None)) is False
    assert await t.watermark("o", "n", ch=_CH(one=None)) == 0


async def test_identity_keyed_reads_map_rows_to_their_identity() -> None:
    row = {"agent": "a", "user_id": "u", "session_id": "s", "memory_key": "k", "id": "c1"}
    assert await t.high_after("o", "n", 3, ch=_CH(rows=[row])) == {IDENT}
    assert await t.last_commits("o", "n", [IDENT], ch=_CH(rows=[row])) == {IDENT: "c1"}
    got = await t.commits_for_idempotency_key("o", "n", "k", [IDENT], ch=_CH(rows=[row]))
    assert got == {IDENT: [row]}
    assert await t.other_commits_at("o", "n", [1], ["c"], ch=_CH(rows=[{"commit_seq": "4"}])) == [4]
    assert await t.rank_by_vector(
        "o", "n", "m", [0.1], ["h"], limit=5, ch=_CH(rows=[{"sha": "h", "d": "0.5"}])
    ) == {"h": 0.5}


async def test_commits_between_narrows_to_keys_only_when_asked() -> None:
    ch = _CH()
    await t.commits_between("o", "n", 1, 9, ch=ch)
    await t.commits_between("o", "n", 1, 9, memory_keys=["k"], ch=ch)
    (_, all_sql, all_p), (_, keyed_sql, keyed_p) = ch.calls
    assert "memory_key IN" not in all_sql and "keys" not in all_p
    assert "memory_key IN {keys:Array(String)}" in keyed_sql and keyed_p["keys"] == ["k"]
    assert t._ON_TIME in all_sql


async def test_cutoff_counts_everything_without_a_knowledge_time() -> None:
    ch = _CH(one=None)
    assert await t.cutoff("o", "n", None, ch=ch) == {}
    k = datetime(2026, 1, 1, tzinfo=UTC)
    await t.cutoff("o", "n", k, ch=ch)
    (_, live_sql, live_p), (_, k_sql, k_p) = ch.calls
    assert "maxIf(commit_seq, 1)" in live_sql and "k" not in live_p
    assert "recorded_at <= {k:DateTime64(6, 'UTC')}" in k_sql and k_p["k"] == k


async def test_assertions_at_applies_filters_and_the_high_fence() -> None:
    ch = _CH()
    await t.assertions_at("o", "n", 5, scope=WHOLE_NAMESPACE, agent="*", ch=ch)
    _, sql, p = ch.calls[-1]
    assert "sensitivity != 'high'" in sql and "NOT IN (SELECT agent" in sql
    assert "agent = " not in sql and "memory_type" not in sql and "keys" not in p

    scope = ReadFilters(scope="own_user", user_id="u", session_id=None)
    await t.assertions_at(
        "o",
        "n",
        5,
        scope=scope,
        memory_keys=["k"],
        agent="a",
        user_id="u",
        session_id="s",
        include_high=True,
        memory_type="semantic",
        tags=["t"],
        erased=t.Erased(namespace_us=1),
        ch=ch,
    )
    _, sql, p = ch.calls[-1]
    assert "sensitivity != 'high'" not in sql
    assert "AND memory_type = {mt:String} AND hasAny(tags, {tg:Array(String)})" in sql
    assert {"keys": ["k"], "agent": "a", "user_id": "u", "session_id": "s"}.items() <= p.items()
    assert p["mt"] == "semantic" and p["tg"] == ["t"] and p["er_ns"] == 1

    await t.assertions_at("o", "n", 5, scope=WHOLE_NAMESPACE, tags=["t"], ch=ch)
    _, sql, p = ch.calls[-1]
    assert "hasAny(tags" in sql and "mt" not in p


async def test_key_history_skips_wildcard_identity_filters() -> None:
    ch = _CH()
    await t.key_history(
        "o", "n", "k", upto_seq=7, scope=WHOLE_NAMESPACE, agent="*", user_id="u", ch=ch
    )
    _, sql, p = ch.calls[-1]
    assert "agent = " not in sql and "user_id = {user_id:String}" in sql and p["user_id"] == "u"
    assert "born_seq <= {c:UInt64}" in sql and p["c"] == 7  # the cut, before the limit


def _commit(cid: str, assertions: list[dict[str, Any]], retired: list[str] = ()) -> dict:
    return {
        "commit_id": cid,
        "assertions": json.dumps(assertions),
        "retired_assertion_ids": list(retired),
    }


async def test_value_lookups_read_the_journal_payloads() -> None:
    commits = [
        _commit("c1", [{"assertion_id": "x1", "value": "old"}]),
        _commit("c2", [{"assertion_id": "x2", "value": REDACTED_VALUE}], retired=["x1"]),
    ]
    ch = _CH(rows=commits)
    assert await t.assertion_value_sha("o", "n", IDENT, "x1", ch=ch) == _sha("old")
    assert await t.assertion_value_sha("o", "n", IDENT, "x2", ch=ch) == ""  # redacted
    assert await t.assertion_value_sha("o", "n", IDENT, "nope", ch=ch) == ""
    assert await t.value_served("o", "n", IDENT, _sha("old"), ch=ch) is False  # retired
    live = _CH(rows=[_commit("c1", [{"assertion_id": "x1", "value": "old"}])])
    assert await t.value_served("o", "n", IDENT, _sha("old"), ch=live) is True


class _JournalCH(_CH):
    """Commits from the journal; the projection holds `projected`."""

    def __init__(self, rows: list[dict[str, Any]], projected: list[dict[str, Any]] = ()) -> None:
        super().__init__(rows=rows)
        self.projected = list(projected)

    async def query_all(self, sql: str, params: Any = None, **_: Any) -> list[dict[str, Any]]:
        self.calls.append(("query_all", sql, params))
        return self.projected if t.ASSERTIONS in sql else self.rows


async def test_redaction_scrubs_only_the_commits_that_held_the_value() -> None:
    ch = _JournalCH(rows=[_commit("c1", [{"assertion_id": "x9", "value": "keep"}])])
    assert await t.redact_value("o", "n", IDENT, _sha("secret"), ch=ch) == 0
    assert [c[0] for c in ch.calls] == ["query_all", "query_all"]  # nothing rewritten

    # A retry after the payload was scrubbed still scrubs the projection and vectors.
    ch = _JournalCH(rows=[], projected=[{"id": "x1", "e": "e1"}])
    assert await t.redact_value("o", "n", IDENT, _sha("secret"), ch=ch) == 1
    projection, vectors = (c for c in ch.calls if c[0] == "execute")
    assert projection[2]["ids"] == ["x1"] and vectors[2]["shas"] == ["e1"]

    held = [
        _commit("c1", [{"assertion_id": "x1", "value": "secret", "embed_input_sha256": "e1"}]),
        _commit("c2", [{"assertion_id": "x2", "value": "other", "entities": ["E"]}]),
    ]
    ch = _JournalCH(rows=held)
    assert await t.redact_value("o", "n", IDENT, _sha("secret"), ch=ch) == 1
    rewrite, projection, vectors = (c for c in ch.calls if c[0] == "execute")
    assert rewrite[2]["cid"] == "c1"
    assert json.loads(rewrite[2]["p"]) == [
        {"assertion_id": "x1", "value": REDACTED_VALUE, "embed_input_sha256": "e1", "entities": []}
    ]
    assert projection[2]["ids"] == ["x1"] and "memory_temporal_assertions" in projection[1]
    assert vectors[2]["shas"] == ["e1"] and f"value != '{REDACTED_VALUE}'" in vectors[1]


async def test_repair_keeps_the_first_commit_at_each_broken_sequence() -> None:
    ch = _CH(rows=[{"commit_seq": 3, "kept": "c1"}, {"commit_seq": 4, "kept": "c7"}])
    assert await t.repair_namespace("o", "n", ch=ch) == [3, 4]
    deletes = [c for c in ch.calls if c[0] == "execute"]
    assert [(d[2]["seq"], d[2]["kept"]) for d in deletes] == [(3, "c1"), (4, "c7")]
    assert all(d[3] == {"lightweight_deletes_sync": 2} for d in deletes)


async def test_orphan_vectors_are_deleted_only_when_counted() -> None:
    ch = _CH(one={"n": 0})
    assert await t.delete_orphan_vectors(ch=ch) == 0
    assert [c[0] for c in ch.calls] == ["query_one"]
    ch = _CH(one={"n": 2})
    assert await t.delete_orphan_vectors(ch=ch) == 2
    assert ch.calls[-1][0] == "execute" and t.VECTORS in ch.calls[-1][1]


async def test_scoped_scans_bind_their_namespace() -> None:
    ch = _CH()
    await t.unchecked_namespaces("assertions", ch=ch)
    await t.unchecked_namespaces("assertions", only=("o", "n"), ch=ch)
    (_, any_sql, any_p), (_, one_sql, one_p) = ch.calls
    assert "org = {o:String}" not in any_sql and any_p["o"] == ""
    assert "org = {o:String}" in one_sql and (one_p["o"], one_p["n"]) == ("o", "n")

    a, u = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
    await t.boundaries_after(a, u, limit=10, ch=ch)
    await t.boundaries_after(a, u, limit=10, org="o", namespace="n", ch=ch)
    (_, b_sql, b_p), (_, bs_sql, bs_p) = ch.calls[2:]
    assert "org = {org:String}" not in b_sql and "org" not in b_p
    assert "org = {org:String}" in bs_sql and bs_p["org"] == "o" and bs_p["ns"] == "n"


async def test_a_held_boundary_keeps_the_earliest_instant() -> None:
    ch = _CH(one={"s": 100, "n": 1})
    await t.hold_boundary("o", "n", 200, ch=ch)  # an earlier one is already held
    assert [c[0] for c in ch.calls] == ["query_one"]

    ch = _CH(one={"s": 300, "n": 1})
    await t.hold_boundary("o", "n", 200, ch=ch)
    _, delete, insert = ch.calls
    assert delete[0] == "execute" and delete[2]["s"] == 200
    assert insert[0] == "insert" and insert[2][0]["applied_seq"] == 200
    assert insert[2][0]["projection"] == t.PENDING

    ch = _CH(one={"s": 0, "n": 0})  # nothing held yet
    await t.hold_boundary("o", "n", 200, ch=ch)
    assert [c[0] for c in ch.calls] == ["query_one", "execute", "insert"]


async def test_a_key_history_says_when_it_is_short(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import temporal_reads as reads
    from server.memory.domain.temporal_reads import Axes

    def _row(seq: int) -> dict[str, Any]:
        a = tm_first(f'"v{seq}"')
        row = {k: v for k, v in a.to_json().items() if k in t.ASSERTION_COLS}
        return {**row, "org": "o", "namespace": "n", "agent": "a", "user_id": "-",
                "session_id": "-", "memory_key": "k", "memory_id": "m", "born_seq": seq,
                "born_at": datetime(2026, 1, 1, tzinfo=UTC), "retired_seq": None}  # fmt: skip

    async def _key_history(*_a: Any, limit: int, **_k: Any) -> list[dict[str, Any]]:
        return [_row(s) for s in (3, 2)][:limit]  # newest first

    async def _no_erasure(*_a: Any) -> t.Erased:
        return t.NOT_ERASED

    async def _all(org: str, ns: str, held: list) -> list:
        return held

    monkeypatch.setattr(reads.store, "key_history", _key_history)
    monkeypatch.setattr(reads, "erased", _no_erasure)
    monkeypatch.setattr(reads, "visible", _all)
    cut = reads.Cut("n", Axes(valid=None, known=None), seq=3, head=3, watermark=2)
    got = await reads.history("o", cut, "k", scope=WHOLE_NAMESPACE, agent=None,
                              user_id=None, session_id=None, limit=2)  # fmt: skip
    assert [h.born_seq for h in got] == [2, 3]  # oldest first
    assert cut.gaps == ["projection_not_ready", "truncated:2"]


def test_the_current_row_is_never_a_plan() -> None:
    from server.memory.app import temporal_reads as reads
    from server.memory.domain.temporal_reads import Axes

    now = datetime(2026, 6, 1, tzinfo=UTC)
    cut = reads.Cut("n", Axes(valid=now, known=None), seq=2, head=2, watermark=2)

    def held(value: str, lo: datetime, hi: datetime | None = None) -> reads.Held:
        a = tm_first(value, valid_from=lo, valid_to=hi, validity_basis="stated")
        return reads.Held(a, "a", "-", "-", "k", "m", 1, now)

    tomorrow = held('"plan"', datetime(2026, 6, 2, tzinfo=UTC))
    assert reads.applies_now([tomorrow], cut, "k") == []  # only a plan: no row yet
    ended = held('"was"', datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 3, 1, tzinfo=UTC))
    assert reads.applies_now([ended, tomorrow], cut, "k") == [ended]  # a closed fact stays
    live = held('"is"', datetime(2026, 3, 1, tzinfo=UTC))
    assert reads.applies_now([ended, live, tomorrow], cut, "k") == [live]


def test_the_stable_prefix_stops_below_an_allocation_still_in_doubt() -> None:
    # 1-3 settled; 5 and 6 landed recently; 4 is allocated, its insert in doubt.
    row = {"settled": 3, "committed": 6, "recent": [5, 6]}
    assert t.stable_prefix(row) == 3
    # Once 5 is settled, 4 can no longer land on time: it never will.
    assert t.stable_prefix({"settled": 5, "committed": 6, "recent": [6]}) == 6
    assert t.stable_prefix({"settled": 0, "committed": 2, "recent": [1, 2]}) == 2
    assert t.stable_prefix({"n": 0}) == 0


async def _no_rows(*_a: Any, **_k: Any) -> bool:
    return False


async def test_a_token_the_cut_at_k_cannot_cover_is_disclosed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import temporal_reads as reads
    from server.memory.domain.temporal_reads import Axes, encode_token

    async def _cutoff(*_a: Any, **_k: Any) -> dict[str, Any]:
        # The token names seq 2, committed after K: the cut at K is 1.
        return {"head": 2, "committed": 2, "n": 2, "seq": 1, "known": 1, "settled": 2}

    async def _zero(*_a: Any, **_k: Any) -> int:
        return 2

    async def _no_erasure(*_a: Any) -> t.Erased:
        return t.NOT_ERASED

    monkeypatch.setattr(reads.store, "cutoff", _cutoff)
    monkeypatch.setattr(reads.store, "watermark", _zero)
    monkeypatch.setattr(reads.store, "rows_before_journal", _no_rows)
    monkeypatch.setattr(reads, "erased", _no_erasure)
    k = datetime(2026, 1, 1, tzinfo=UTC)
    cut = await reads.cutoff("o", "n", Axes(valid=k, known=k), read_token=encode_token({"n": 2}))
    assert cut.seq == 1 and cut.gaps == ["read_token_after_known_as_of"]
    assert cut.wire().complete is False


async def test_data_older_than_the_journal_is_a_gap() -> None:
    first = datetime(2026, 1, 1, tzinfo=UTC)
    t._BEFORE_JOURNAL.clear()
    ch = _CH(one={"n": 1})
    assert await t.rows_before_journal("o", "n", first, ch=ch) is True
    _, sql, params = ch.calls[-1]
    assert "version < {v:UInt64}" in sql and params["v"] == int((first.timestamp() - 3600) * 1e9)
    assert "loom.memory_versions" in sql
    assert await t.rows_before_journal("o", "n", first, ch=_CH(one={"n": 0})) is True  # cached
    assert await t.rows_before_journal("o", "n2", None, ch=_CH(one={"n": 0})) is False


def test_after_a_namespace_erasure_gaps_above_its_head_are_still_checked() -> None:
    # The erasure removed 1-1000; 1002 landed before 1001, whose insert is in doubt.
    row = {"settled": 0, "committed": 1002, "recent": [1002]}
    assert t.stable_prefix(row, erased_head=1000) == 1000
    assert t.stable_prefix({**row, "recent": [1001, 1002]}, erased_head=1000) == 1002
    assert t.stable_prefix(row) == 0  # without the boundary nothing below is known


def test_an_undated_fact_retracted_from_a_date_is_still_the_latest_begun() -> None:
    from server.memory.app import temporal_reads as reads
    from server.memory.domain.temporal_reads import Axes

    now = datetime(2026, 6, 10, tzinfo=UTC)
    cut = reads.Cut("n", Axes(valid=now, known=None), seq=1, head=1, watermark=1)
    alpha = tm_first('"alpha"', valid_to=datetime(2026, 6, 1, tzinfo=UTC))  # undated, ended Jun
    held = reads.Held(alpha, "a", "-", "-", "k", "m", 1, now)
    assert reads.applies_now([held], cut, "k") == [held]  # the row carries it, with its end
