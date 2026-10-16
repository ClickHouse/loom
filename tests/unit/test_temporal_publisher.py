"""The namespace publisher against an in-memory journal: order, projection,
uncertain inserts, durable retry identity, strict-mode fencing."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.core import idempotency, request_work
from server.core.errors import PreconditionFailedError, TemporalUnavailableError
from server.core.hashing import sha256_value_json
from server.memory.app import temporal_publisher as pub
from server.memory.app import temporal_rows as rows
from server.memory.domain import temporal_model as tm
from server.memory.store import temporal as store

pytestmark = pytest.mark.temporal_journal


class Journal:
    """The store functions the publisher calls, over lists."""

    def __init__(self) -> None:
        self.commits: list[dict[str, Any]] = []
        self.assertions: dict[str, dict[str, Any]] = {}
        self.watermark = 0
        self.fail_inserts = 0
        self.land_failed = False
        self.erasures_late = 0
        self.published = 0
        self.published_at = None

    async def head(self, org, ns, **_):
        if not self.commits:
            return self.published, None, 0
        last = max(self.commits, key=lambda c: c["commit_seq"])
        payloads: dict[int, set] = {}
        for c in self.commits:
            payloads.setdefault(c["commit_seq"], set()).add(c["payload_sha256"])
        broken = min((q for q, p in payloads.items() if len(p) > 1), default=0)
        at = max(t for t in (last["recorded_at"], self.published_at) if t is not None)
        return max(last["commit_seq"], self.published), at, broken

    async def publish_mark(self, org, ns, seq, recorded_at=None, **_):
        if seq >= self.published:
            self.published, self.published_at = seq, recorded_at

    async def repair(self, org, ns, **_):
        # The list is landing order: keep the first commit at each sequence.
        seen: set[int] = set()
        kept = []
        for c in self.commits:
            if c["commit_seq"] not in seen:
                seen.add(c["commit_seq"])
                kept.append(c)
        self.commits = kept
        return []

    async def others(self, org, ns, seqs, ids, **_):
        return [
            c["commit_seq"]
            for c in self.commits
            if c["commit_seq"] in seqs and c["commit_id"] not in ids
        ]

    async def withdraw(self, org, ns, ids, **_):
        self.commits = [c for c in self.commits if c["commit_id"] not in ids]

    async def insert_commits(self, rows, **_):
        store._check(rows, store.COMMIT_COLS, store.COMMITS)
        if self.fail_inserts:
            self.fail_inserts -= 1
            if self.land_failed:
                self.commits.extend(dict(r) for r in rows)
            raise ConnectionError("reset by peer")
        self.commits.extend(dict(r) for r in rows)

    async def commits_by_id(self, org, ns, ids, **_):
        return [c for c in self.commits if c["commit_id"] in ids]

    async def insert_assertions(self, rows, **_):
        store._check(rows, store.ASSERTION_COLS, store.ASSERTIONS)
        for r in rows:
            cur = self.assertions.get(r["assertion_id"])
            if cur is None or cur["projected_seq"] <= r["projected_seq"]:
                self.assertions[r["assertion_id"]] = dict(r)

    async def watermark_of(self, *_a, **_k):
        return self.watermark

    async def advance(self, org, ns, seq, *_a, **_k):
        self.watermark = max(self.watermark, seq)

    async def served_for_keys(self, org, ns, keys, **_):
        return [
            r
            for r in self.assertions.values()
            if r["retired_seq"] is None
            and (r["agent"], r["user_id"], r["session_id"], r["memory_key"]) in set(keys)
        ]

    async def restated(self, org, ns, heads, **_):
        out: dict = {}
        for h in heads:
            ident = (h["agent"], h["user_id"], h["session_id"], h["memory_key"])
            for c in self.commits:
                ci = (c["agent"], c["user_id"], c["session_id"], c["memory_key"])
                if (
                    ci == ident
                    and c.get("restated_assertion_id")
                    and c["row_version"] == h.get("version")
                ):
                    out[ident] = str(c["restated_assertion_id"])
        return out

    async def none(self, *_a, **_k):
        return [] if not _k.get("one") else None

    async def landed(self, *_a, **_k):
        return {}

    async def hashes(self, org, idem, **_):
        return {
            c["request_sha256"]
            for c in self.commits
            if c.get("idempotency_key") == idem and c.get("request_sha256")
        }

    async def erasures(self, *_a, **_k):
        return 0, self.erasures_late

    async def idem(self, org, ns, key, keys, **_):
        out: dict = {}
        for c in self.commits:
            ident = (c["agent"], c["user_id"], c["session_id"], c["memory_key"])
            if c["idempotency_key"] == key and ident in keys:
                out.setdefault(ident, []).append(c)
        return out

    async def row_versions(self, org, ns, keys, **_):
        out: dict = {}
        for c in self.commits:
            ident = (c["agent"], c["user_id"], c["session_id"], c["memory_key"])
            if ident in set(keys):
                out[ident] = max(out.get(ident, 0), int(c.get("row_version") or 0))
        return out

    async def parents(self, org, ns, keys, **_):
        out: dict = {}
        for c in sorted(self.commits, key=lambda c: c["commit_seq"]):
            ident = (c["agent"], c["user_id"], c["session_id"], c["memory_key"])
            if ident in set(keys):
                out[ident] = c["commit_id"]
        return out

    async def between(self, org, ns, lo, hi, **_):
        return [c for c in self.commits if lo < c["commit_seq"] <= hi]


@pytest.fixture
def journal(monkeypatch: pytest.MonkeyPatch) -> Journal:
    j = Journal()
    for name, fn in {
        "head": j.head,
        "insert_commits": j.insert_commits,
        "commits_by_id": j.commits_by_id,
        "insert_assertions": j.insert_assertions,
        "watermark": j.watermark_of,
        "advance_watermark": j.advance,
        "served_for_keys": j.served_for_keys,
        "memory_heads": j.none,
        "commits_for_idempotency_key": j.idem,
        "last_commits": j.parents,
        "newest_row_versions": j.row_versions,
        "row_versions": j.landed,
        "landed_heads": j.landed,
        "request_hashes": j.hashes,
        "commits_between": j.between,
        "other_commits_at": j.others,
        "withdraw_commits": j.withdraw,
        "repair_namespace": j.repair,
        "mark_published": j.publish_mark,
        "restated_claims": j.restated,
    }.items():
        monkeypatch.setattr(store, name, fn)

    monkeypatch.setattr(pub.fence, "scope_erasures", j.erasures)

    async def not_erased(*_a: Any, **_k: Any) -> Any:
        return store.NOT_ERASED

    async def on_time(*_a: Any, **_k: Any) -> bool:
        return False

    async def clock(*_a: Any, **_k: Any) -> datetime:
        return datetime.now(UTC)

    monkeypatch.setattr(pub, "erased", not_erased)
    monkeypatch.setattr(store, "landed_late", on_time)
    monkeypatch.setattr(store, "server_now", clock)

    @contextlib.asynccontextmanager
    async def held_lease(name: str, **_k: Any) -> Any:
        yield types.SimpleNamespace(held=True)

    monkeypatch.setattr(pub._locks, "lease", held_lease)
    return j


_VERSIONS = itertools.count(1_000)


def mrow(value: str, **kw: Any) -> dict[str, Any]:
    r = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k",
        "memory_id": "00000000-0000-0000-0000-000000000001",
        "memory_type": "semantic",
        "value": value,
        "content_sha256": sha256_value_json(value),
        "status": "active",
        "tombstoned_at": None,
        "redacted_at": None,
        "valid_at": None,
        "valid_to": None,
        "temporal_anchor": None,
        "version": next(_VERSIONS),  # drawn at write time: each write later than the last
        "actor": "human:x",
        "source_class": "asserted",
    }
    r.update(kw)
    return r


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_commits_get_the_next_sequence_and_a_rising_recording_time(journal: Journal) -> None:
    run(pub.publish_rows([mrow('"A"')]))
    run(pub.publish_rows([mrow('"B"')]))
    seqs = [c["commit_seq"] for c in journal.commits]
    assert seqs == [1, 2]
    assert journal.commits[0]["recorded_at"] < journal.commits[1]["recorded_at"]
    assert [c["change_kind"] for c in journal.commits] == ["assert", "assert"]
    # The second commit retired the first one's assertion in the projection.
    first = json.loads(journal.commits[0]["assertions"])[0]["assertion_id"]
    assert journal.assertions[first]["retired_seq"] == 2
    assert journal.watermark == 2


def test_a_restatement_names_its_claim_at_its_version(journal: Journal) -> None:
    # Empty, so the claim keeps its id and birth; the commit names the claim its row
    # shows, and its version orders the key's next write.
    run(pub.publish_rows([mrow('"A"')]))
    (first,) = json.loads(journal.commits[0]["assertions"])
    (row,) = run(pub.publish_rows([mrow('"A"', recall_count=4)]))
    restate = journal.commits[-1]
    assert [c["change_kind"] for c in journal.commits] == ["assert", "restate"]
    assert json.loads(restate["assertions"]) == [] and list(restate["retired_assertion_ids"]) == []
    assert str(restate["restated_assertion_id"]) == first["assertion_id"]
    assert restate["row_version"] == row["version"]


def test_an_uncertain_insert_that_landed_is_reconciled_not_republished(journal: Journal) -> None:
    journal.fail_inserts, journal.land_failed = 1, True
    run(pub.publish_rows([mrow('"A"')]))
    assert [c["commit_seq"] for c in journal.commits] == [1]


def test_an_insert_that_never_lands_fails_the_write(journal: Journal, monkeypatch) -> None:
    monkeypatch.setattr(pub.settings, "memory_temporal_insert_retries", 1)
    journal.fail_inserts = 5
    with pytest.raises(TemporalUnavailableError):
        run(pub.publish_rows([mrow('"A"')]))
    assert journal.commits == []


def test_a_namespace_another_process_is_publishing_answers_busy(
    journal: Journal, monkeypatch
) -> None:
    from server.core.leases import LeaseBusy

    @contextlib.asynccontextmanager
    async def busy(name: str, **_k: Any) -> Any:
        raise LeaseBusy(name)
        yield

    monkeypatch.setattr(pub._locks, "lease", busy)
    with pytest.raises(TemporalUnavailableError, match="busy"):
        run(pub.publish_rows([mrow('"A"')]))


def test_a_retry_under_the_same_key_reuses_the_commit_and_a_different_body_is_refused(
    journal: Journal,
) -> None:
    async def write(value: str, body: str) -> list[pub.Receipt]:
        request_work.begin()
        idempotency.request_key_var.set("idem-1")
        idempotency.request_body_var.set(body)
        with pub.collect_receipts() as bag:
            await pub.publish_rows([mrow(value)])
        return bag

    async def scenario() -> None:
        # Each attempt is its own request (its own task context), as over HTTP.
        first = await asyncio.create_task(write('"A"', "body-A"))
        # The retry is recognised by key and body, whatever the key serves now: a
        # journal whose projection lost the key still answers it.
        journal.assertions.clear()
        journal.watermark = 0
        again = await asyncio.create_task(write('"A"', "body-A"))
        assert again and again[0].commit_seq == first[0].commit_seq
        # The retry reports the committed change, not the (empty) re-plan.
        assert again[0].amendment == first[0].amendment and first[0].amendment.added
        # The same key with another body is refused, even one that would change nothing.
        for value in ('"B"', '"A"'):
            with pytest.raises(PreconditionFailedError, match="Idempotency-Key"):
                await asyncio.create_task(write(value, "body-B"))

    run(scenario())
    assert len(journal.commits) == 1


def test_a_second_change_in_child_tasks_of_one_request_is_not_a_retry(journal: Journal) -> None:
    # A handler that gathers its writes commits from child tasks; the request's
    # record of what it committed must survive them.
    async def scenario() -> None:
        request_work.begin()
        idempotency.request_key_var.set("idem-2")
        await asyncio.create_task(pub.publish_rows([mrow('"A"')]))
        await asyncio.create_task(pub.publish_rows([mrow('"B"')]))

    run(scenario())
    assert len(journal.commits) == 2


def test_a_lost_sequence_race_withdraws_this_batch(journal: Journal, monkeypatch) -> None:
    # Another publisher wrote the same sequence between our head read and insert.
    real_insert = journal.insert_commits

    async def racing_insert(rows, **kw):
        await real_insert(rows, **kw)
        journal.commits.append(
            {
                **rows[0],
                "commit_id": "00000000-0000-0000-0000-00000000beef",
                "payload_sha256": "f" * 64,
            }
        )

    monkeypatch.setattr(store, "insert_commits", racing_insert)
    with pytest.raises(TemporalUnavailableError, match="another publisher"):
        run(pub.publish_rows([mrow('"A"')]))
    assert [c["commit_id"] for c in journal.commits] == ["00000000-0000-0000-0000-00000000beef"]


def test_a_break_left_by_a_failed_withdrawal_is_repaired_by_the_next_write(
    journal: Journal, monkeypatch
) -> None:
    real_insert = journal.insert_commits

    async def racing_insert(rows, **kw):
        await real_insert(rows, **kw)
        journal.commits.append(
            {
                **rows[0],
                "commit_id": "00000000-0000-0000-0000-00000000beef",
                "payload_sha256": "f" * 64,
            }
        )

    async def failing_withdraw(*_a, **_k):
        raise RuntimeError("Code: 999. transient")

    monkeypatch.setattr(store, "insert_commits", racing_insert)
    monkeypatch.setattr(store, "withdraw_commits", failing_withdraw)
    with pytest.raises(TemporalUnavailableError, match="another publisher"):
        run(pub.publish_rows([mrow('"A"')]))
    first = journal.commits[0]["commit_id"]
    monkeypatch.setattr(store, "insert_commits", real_insert)
    run(pub.publish_rows([mrow('"B"', memory_key="k2")]))
    # The first-landed commit stays; the later one at its sequence is gone.
    assert [(c["commit_seq"], c["commit_id"]) for c in journal.commits][:1] == [(1, first)]
    assert [c["commit_seq"] for c in journal.commits] == [1, 2]


def test_a_commit_to_the_same_key_between_plan_and_lock_replans(
    journal: Journal, monkeypatch
) -> None:
    run(pub.publish_rows([mrow('"A"')]))
    real_head = journal.head
    calls = {"n": 0}

    async def head(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:  # the head read under the lock: someone else wrote k meanwhile
            await real_insert_other()
        return await real_head(*a, **k)

    async def real_insert_other():
        other = dict(
            journal.commits[-1], commit_seq=2, commit_id="00000000-0000-0000-0000-0000000000b2"
        )
        journal.commits.append(other)

    monkeypatch.setattr(store, "head", head)
    run(pub.publish_rows([mrow('"B"')]))
    assert calls["n"] >= 3  # planned twice
    assert [c["commit_seq"] for c in journal.commits] == [1, 2, 3]


def test_a_projection_landing_between_plan_and_lock_goes_below_the_write(
    journal: Journal, monkeypatch
) -> None:
    # C is planned, then a projection lands a later version of k without moving the
    # sequence: C still commits, as planned, and its row goes above the landed one,
    # so loom.memory serves what the journal serves.
    run(pub.publish_rows([mrow('"A"')]))
    c = mrow('"C"')
    landed = {"v": 0}
    real_head = journal.head
    calls = {"n": 0}

    async def head(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:  # under the lock
            landed["v"] = c["version"] + 5
        return await real_head(*a, **k)

    async def versions(*_a, **_k):
        # A projection row this build wrote: stamped at its own version.
        return {("a", "-", "-", "k"): (landed["v"], landed["v"])} if landed["v"] else {}

    monkeypatch.setattr(store, "head", head)
    monkeypatch.setattr(store, "landed_heads", versions)
    (row,) = run(pub.publish_rows([c]))
    assert calls["n"] == 2  # planned once
    assert row["version"] == landed["v"] + 1
    assert journal.commits[-1]["row_version"] == row["version"]
    # The claim the row added names the version it was written at.
    (added,) = json.loads(journal.commits[-1]["assertions"])
    assert added["source_version"] == row["version"]


def test_a_write_begun_before_an_erasure_is_fenced(journal: Journal) -> None:
    async def scenario() -> None:
        request_work.begin()
        journal.erasures_late = 1
        with pytest.raises(PreconditionFailedError, match="erasure"):
            await pub.publish_rows([mrow('"A"')])

    run(scenario())


def test_a_lagging_projection_is_reduced_from_the_journal(journal: Journal) -> None:
    run(pub.publish_rows([mrow('"A"')]))
    journal.assertions.clear()  # the projection lost its rows
    journal.watermark = 0
    run(pub.publish_rows([mrow('"B"')]))
    first = json.loads(journal.commits[0]["assertions"])[0]["assertion_id"]
    assert [str(x) for x in journal.commits[1]["retired_assertion_ids"]] == [first]


def test_projection_rows_carry_every_column() -> None:
    """A loom.memory_temporal_assertions version row is a ReplacingMergeTree re-emit:
    every column, or FINAL serves a DDL default over the real value."""
    a = tm.first('"A"', valid_from=datetime(2026, 1, 1, tzinfo=UTC))
    row = rows.projection_row(
        "o", "n", "private", ("a", "-", "-", "k"), rows.Served(a, "m", 1, datetime.now(UTC)), 1
    )
    assert set(row) == set(store.ASSERTION_COLS)


def test_an_erased_head_sequence_is_never_allocated_again(journal: Journal) -> None:
    # Erasure deleted the newest commit; its sequence stays published.
    async def scenario() -> None:
        await pub.publish_rows([mrow('"A"')])
        await pub.publish_rows([mrow('"B"', memory_key="k2")])
        journal.commits = [c for c in journal.commits if c["memory_key"] != "k2"]
        await pub.publish_rows([mrow('"C"', memory_key="k3")])

    run(scenario())
    assert [c["commit_seq"] for c in journal.commits] == [1, 3]


def test_a_write_waits_out_a_running_erasure(journal: Journal) -> None:
    # A worker pass binds no request: it is fenced all the same.
    journal.erasures_late = 1
    with pytest.raises(PreconditionFailedError, match="erasure"):
        run(pub.publish_rows([mrow('"A"')]))
    assert journal.commits == []


def test_a_refused_batch_writes_no_embedding_and_a_moved_on_retry_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.store import inserts

    inserted: list[str] = []
    embedded: list[Any] = []

    class Ch:
        async def insert(self, table: str, rows: Any, **_: Any) -> None:
            inserted.extend(r["memory_key"] for r in rows)

    async def embed(kept: Any) -> None:
        embedded.append([r["memory_key"] for r in kept])

    monkeypatch.setattr(inserts, "check_memory_rows", lambda rows: None)
    monkeypatch.setattr(inserts, "_AFTER_INSERT", [])
    rows = [{"memory_key": "a"}, {"memory_key": "b"}]

    async def refuse(rows: Any, **_: Any) -> Any:
        raise TemporalUnavailableError("journal down")

    monkeypatch.setattr(inserts, "_BEFORE_INSERT", [refuse])
    with pytest.raises(TemporalUnavailableError):
        run(inserts.insert_memory_versions(rows, ch=Ch(), alongside=[embed]))
    assert inserted == [] and embedded == []

    async def drop_a(rows: Any, **_: Any) -> Any:
        return [r for r in rows if r["memory_key"] != "a"]

    monkeypatch.setattr(inserts, "_BEFORE_INSERT", [drop_a])
    assert run(inserts.insert_memory_versions(rows, ch=Ch(), alongside=[embed])) == 1
    assert inserted == ["b"] and embedded == [["b"]]


def test_a_retry_after_the_key_moved_on_replays_without_rewriting(journal: Journal) -> None:
    async def write(value: str, key: str = "", body: str = "") -> list[Any]:
        request_work.begin()
        idempotency.request_key_var.set(key)
        idempotency.request_body_var.set(body)
        return await pub.publish_rows([mrow(value)])

    async def scenario() -> None:
        assert await asyncio.create_task(write('"A"', "idem-9", "body-A"))
        assert await asyncio.create_task(write('"B"'))
        # The first request retried after B: answered from its commit, row left out.
        with pub.collect_receipts() as bag:
            kept = await asyncio.create_task(write('"A"', "idem-9", "body-A"))
        assert kept == [] and bag[0].commit_seq == 1 and bag[0].moved_on

    run(scenario())
    assert [c["commit_seq"] for c in journal.commits] == [1, 2]


def test_late_evidence_is_journaled_but_does_not_replace_the_current_row(
    journal: Journal,
) -> None:
    feb = datetime(2026, 2, 1, tzinfo=UTC)
    jan = datetime(2026, 1, 1, tzinfo=UTC)

    async def scenario() -> None:
        assert await pub.publish_rows([mrow('"B"', valid_at=feb)])
        with pub.collect_receipts() as bag:
            kept = await pub.publish_rows([mrow('"A"', valid_at=jan)])
        assert kept == [] and bag[-1].past_only  # the present is still B
        assert await pub.publish_rows([mrow('"C"')])  # an undated write replaces

    run(scenario())
    assert len(journal.commits) == 3


def test_a_retry_is_written_with_the_expiry_its_commit_recorded(journal: Journal) -> None:
    first = datetime(2026, 1, 1, tzinfo=UTC)

    async def write(expires_at: datetime) -> list[Any]:
        request_work.begin()
        idempotency.request_key_var.set("idem-ttl")
        idempotency.request_body_var.set("body")
        return await pub.publish_rows([mrow('"A"', expires_at=expires_at)])

    async def scenario() -> None:
        await asyncio.create_task(write(first))
        # The retry recomputed its TTL from a later clock: the journal kept the first.
        (row,) = await asyncio.create_task(write(first + timedelta(hours=1)))
        assert row["expires_at"] == first

    run(scenario())
    assert len(journal.commits) == 1


def test_an_assertion_retired_in_its_own_batch_keeps_its_real_birth(journal: Journal) -> None:
    run(pub.publish_rows([mrow('"X"', memory_key="other")]))
    run(pub.publish_rows([mrow('"A"'), mrow('"B"')]))
    a = next(r for r in journal.assertions.values() if r["value"] == '"A"')
    assert (a["born_seq"], a["retired_seq"]) == (2, 3)
    assert a["born_at"].year > 1970


def test_a_retraction_is_kept_as_long_as_what_it_retracts(journal: Journal) -> None:
    until = datetime(2050, 1, 1)
    run(pub.publish_rows([mrow('"A"', expires_at=until)]))
    run(pub.publish_rows([mrow('"A"', expires_at=until, tombstoned_at=datetime(2026, 1, 1))]))
    retraction = journal.commits[-1]
    assert retraction["assertions"] == "[]"
    assert tm.utc(retraction["retain_until"]) == tm.utc(until)


def test_a_retried_batch_keeps_each_identitys_committed_id(journal: Journal) -> None:
    # One key under two users, as a namespace rollback batches it.
    ids = ("00000000-0000-0000-0000-00000000000a", "00000000-0000-0000-0000-00000000000b")

    async def write(fresh: bool) -> list[Any]:
        request_work.begin()
        idempotency.request_key_var.set("idem-ids")
        idempotency.request_body_var.set("body")
        return await pub.publish_rows(
            [
                mrow('"A"', user_id=u, memory_id="f" * 8 + mid[8:] if fresh else mid)
                for u, mid in zip(("u1", "u2"), ids, strict=True)
            ]
        )

    async def scenario() -> list[Any]:
        await asyncio.create_task(write(False))
        return await asyncio.create_task(write(True))

    again = run(scenario())
    assert [(r["user_id"], r["memory_id"]) for r in again] == list(
        zip(("u1", "u2"), ids, strict=True)
    )


def test_a_clock_behind_the_journal_refuses_to_stamp(journal: Journal) -> None:
    # An allocation whose insert outcome stayed unknown was stamped by a clock ahead
    # of ours. Stamping past the clock would date the batch after an erasure
    # requested once the fence passed, and the erasure cut would not hide it.
    run(pub.publish_rows([mrow('"A"')]))
    journal.published, journal.published_at = 2, datetime.now(UTC) + timedelta(seconds=30)
    with pytest.raises(TemporalUnavailableError, match="clock"):
        run(pub.publish_rows([mrow('"B"')]))
    assert len(journal.commits) == 1


def test_a_failed_embedding_write_still_runs_the_after_insert_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.store import inserts

    fenced: list[Any] = []

    class Ch:
        async def insert(self, table: str, rows: Any, **_: Any) -> None:
            pass

    async def embed(kept: Any) -> None:
        raise ConnectionError("embedding insert failed")

    async def keep(rows: Any, **_: Any) -> Any:
        return rows

    async def fence(rows: Any, **_: Any) -> None:
        fenced.append(rows)

    monkeypatch.setattr(inserts, "check_memory_rows", lambda rows: None)
    monkeypatch.setattr(inserts, "_BEFORE_INSERT", [keep])
    monkeypatch.setattr(inserts, "_AFTER_INSERT", [fence])
    with pytest.raises(ConnectionError):
        run(inserts.insert_memory_versions([{"memory_key": "a"}], ch=Ch(), alongside=[embed]))
    assert fenced == [[{"memory_key": "a", "journal_version": 0}]]  # stamped as inserted


def test_a_cancelled_request_still_runs_the_after_insert_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.store import inserts

    fenced: list[Any] = []
    sent = asyncio.Event()

    class Ch:
        async def insert(self, table: str, rows: Any, **_: Any) -> None:
            sent.set()
            await asyncio.sleep(0.05)  # the insert is on the wire when the client goes

    async def keep(rows: Any, **_: Any) -> Any:
        return rows

    async def fence(rows: Any, **_: Any) -> None:
        fenced.append(rows)

    monkeypatch.setattr(inserts, "check_memory_rows", lambda rows: None)
    monkeypatch.setattr(inserts, "_BEFORE_INSERT", [keep])
    monkeypatch.setattr(inserts, "_AFTER_INSERT", [fence])

    async def scenario() -> None:
        write = asyncio.create_task(inserts.insert_memory_versions([{"memory_key": "a"}], ch=Ch()))
        await sent.wait()
        write.cancel()
        with pytest.raises(asyncio.CancelledError):
            await write
        assert fenced == []  # the caller is gone, the insert is not done yet
        await asyncio.sleep(0.1)  # the insert returns, with nobody waiting on it

    run(scenario())
    assert fenced == [[{"memory_key": "a", "journal_version": 0}]]  # stamped as inserted


def test_a_clock_a_moment_behind_the_journal_is_waited_out(journal: Journal, monkeypatch) -> None:
    # Replicas' clocks differ by milliseconds: the next write waits, not refuses.
    run(pub.publish_rows([mrow('"A"')]))
    ahead = datetime.now(UTC) + timedelta(milliseconds=50)
    journal.published, journal.published_at = 2, ahead
    run(pub.publish_rows([mrow('"B"')]))
    assert journal.commits[-1]["recorded_at"] > ahead


async def _attempt(rows: list[Any], intent: Any = None) -> tuple[list[Any], list[Any]]:
    from server.memory.domain.temporal_capture import write_intent

    request_work.begin()
    idempotency.request_key_var.set("idem-r")
    idempotency.request_body_var.set("body")
    with pub.collect_receipts() as bag:
        if intent is None:
            return await pub.publish_rows(rows), bag
        with write_intent(intent):
            return await pub.publish_rows(rows), bag


def test_a_transitions_retry_takes_the_new_values_expiry(journal: Journal) -> None:
    from server.memory.domain.temporal_capture import WriteIntent

    old, new, at = (datetime(y, 1, 1, tzinfo=UTC) for y in (2027, 2030, 2026))

    async def scenario() -> None:
        await pub.publish_rows(
            [mrow('"A"', expires_at=old, valid_at=datetime(2025, 1, 1, tzinfo=UTC))]
        )
        intent = WriteIntent(memory_key="k", change_kind="transition", effective_at=at)
        row = mrow('"B"', expires_at=new, valid_at=at, content_sha256='"B"')
        await asyncio.create_task(_attempt([row], intent))
        (kept,), _ = await asyncio.create_task(_attempt([row], intent))
        assert kept["expires_at"] == new  # not the kept segment of A's

    run(scenario())


def test_a_retry_of_two_rows_for_one_key_writes_both(journal: Journal) -> None:
    rows = [mrow('"A"'), mrow('"A"', status="conflicted")]

    async def scenario() -> None:
        first, _ = await asyncio.create_task(_attempt(rows))
        again, bag = await asyncio.create_task(_attempt(rows))
        assert len(first) == len(again) == 2 and not any(r.moved_on for r in bag)

    run(scenario())


def test_a_cancelled_requests_failed_fence_is_logged_and_drained(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from server.memory.store import inserts

    sent = asyncio.Event()

    class Ch:
        async def insert(self, table: str, rows: Any, **_: Any) -> None:
            sent.set()
            await asyncio.sleep(0.05)

    async def keep(rows: Any, **_: Any) -> Any:
        return rows

    async def fence(rows: Any, **_: Any) -> None:
        raise RuntimeError("fence down")

    monkeypatch.setattr(inserts, "check_memory_rows", lambda rows: None)
    monkeypatch.setattr(inserts, "_BEFORE_INSERT", [keep])
    monkeypatch.setattr(inserts, "_AFTER_INSERT", [fence])

    async def scenario() -> None:
        write = asyncio.create_task(inserts.insert_memory_versions([{"memory_key": "a"}], ch=Ch()))
        await sent.wait()
        write.cancel()
        with pytest.raises(asyncio.CancelledError):
            await write
        await inserts.drain(timeout_s=1.0)
        await asyncio.sleep(0)  # done callbacks run on the next loop turn

    run(scenario())
    assert "fence down" in caplog.text


def test_an_already_journaled_row_is_fenced_too(
    journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reconciler's and amend's current-row writes: their commit was fenced when it
    # published, but an erasure can begin before the row itself lands.
    import server.memory.store as memory_repo
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    undone: list[Any] = []

    async def _insert(rows: Any, **_: Any) -> int:
        undone.extend(rows)
        return len(rows)

    async def _retire(*_a: Any, **_k: Any) -> int:
        return 0

    monkeypatch.setattr(memory_repo, "insert_memory_versions", _insert)
    monkeypatch.setattr(memory_repo, "retire_vectors_for_keys", _retire)
    journal.erasures_late = 1

    async def scenario() -> None:
        request_work.begin()
        with write_intent(WriteIntent(memory_key="k", journaled=True)):
            with pytest.raises(PreconditionFailedError, match="erasure"):
                await pub.refence_rows([mrow('"A"')])

    run(scenario())
    assert [r["tombstoned_at"] is not None for r in undone] == [True]


def test_a_write_drawn_before_one_its_key_committed_is_superseded(journal: Journal) -> None:
    # B drew its version later but committed first; A and a delete, both drawn
    # earlier, follow. The later draw wins, in the journal as in loom.memory's merge.
    run(pub.publish_rows([mrow('"B"', version=20)]))
    with pub.collect_receipts() as bag:
        a = run(pub.publish_rows([mrow('"A"', version=10)]))
        gone = run(pub.publish_rows([mrow('"A"', version=5, tombstoned_at=datetime(2026, 1, 1))]))
    assert a == gone == [] and [r.left_out for r in bag] == ["superseded", "superseded"]
    assert [c["row_version"] for c in journal.commits] == [20]


def test_an_amendment_commit_records_who_made_it(journal: Journal) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role

    set_current_auth(
        AuthInfo(
            subject="ann",
            org="o",
            namespaces=("n",),
            roles=(Role.WRITER,),
            is_service_account=False,
        )
    )
    try:
        run(
            pub.publish_amendment(
                "o", "n", ("a", "-", "-", "k"), "m", tm.retract, operation="memory.amend"
            )
        )
        run(pub.publish_rows([mrow('"A"')]))  # something to retract
        run(
            pub.publish_amendment(
                "o", "n", ("a", "-", "-", "k"), "m", tm.retract, operation="memory.amend"
            )
        )
    finally:
        set_current_auth(None)
    retraction = journal.commits[-1]
    assert retraction["change_kind"] == "retract"
    assert retraction["actor"] == "human:ann" and retraction["source_class"]


def test_a_durable_retry_is_recognised_before_any_new_write_guard(journal: Journal) -> None:
    async def first() -> None:
        request_work.begin()
        idempotency.request_key_var.set("idem-old")
        idempotency.request_body_var.set("body-A")
        await pub.publish_rows([mrow('"A"')])

    async def retry(body: str) -> Any:
        request_work.begin()
        idempotency.request_key_var.set("idem-old")
        idempotency.request_body_var.set(body)
        return await pub.durable_replay("o", "n", ("a", "-", "-", "k"))

    async def scenario() -> None:
        await asyncio.create_task(first())
        # Not moved on: the normal path rewrites its row under the commit.
        assert await asyncio.create_task(retry("body-A")) is None
        await pub.publish_rows([mrow('"B"')])  # the key moved on (pinned B, say)
        got = await asyncio.create_task(retry("body-A"))
        assert (
            got is not None
            and got.commit_seq == 1
            and pub.row_assertion(got.amendment).value == '"A"'
        )
        with pytest.raises(PreconditionFailedError, match="different request"):
            await asyncio.create_task(retry("body-other"))

    run(scenario())


def test_a_removal_of_a_key_locked_since_it_was_read_is_refused_with_its_row(
    journal: Journal,
) -> None:
    run(pub.publish_rows([mrow('"A"', lock_mode="no_delete")]))
    with pub.collect_receipts() as bag:
        kept = run(pub.publish_rows([mrow('"A"', tombstoned_at=datetime(2026, 1, 1))]))
    assert (
        kept == [] and len(journal.commits) == 1 and bag[-1].left_out == "locked"
    )  # the journal still serves it, as the row does


def test_a_key_written_before_the_journal_is_seeded_then_removed(
    journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = mrow('"old"', memory_id="00000000-0000-0000-0000-0000000000aa")

    async def heads(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return [head]

    monkeypatch.setattr(store, "memory_heads", heads)
    (row,) = run(pub.publish_rows([mrow('"old"', tombstoned_at=datetime.now(UTC))]))
    seed, gone = journal.commits
    assert (seed["operation"], seed["idempotency_key"]) == ("memory.journal_seed", "")
    assert json.loads(seed["assertions"])[0]["value"] == '"old"'
    assert gone["change_kind"] == "retract" and gone["parent_commit_id"] == seed["commit_id"]
    assert row["tombstoned_at"] is not None


def test_resending_a_plan_with_new_tags_keeps_it_out_of_the_current_row(journal: Journal) -> None:
    run(pub.publish_rows([mrow('"now"', valid_at=datetime(2026, 1, 1, tzinfo=UTC))]))
    plan = datetime.now(UTC) + timedelta(days=400)
    assert run(pub.publish_rows([mrow('"plan"', valid_at=plan)])) == []
    assert run(pub.publish_rows([mrow('"plan"', valid_at=plan, tags=["x"])])) == []
    assert journal.commits[-1]["change_kind"] == "assess"


def test_late_evidence_of_the_current_value_leaves_the_row_alone(journal: Journal) -> None:
    jan = datetime(2026, 1, 1, tzinfo=UTC)
    run(pub.publish_rows([mrow('"v"', valid_at=jan, sensitivity="high")]))
    late = mrow(
        '"v"', valid_at=datetime(2025, 1, 1, tzinfo=UTC), valid_to=datetime(2025, 6, 1, tzinfo=UTC)
    )
    assert run(pub.publish_rows([late])) == []


def test_values_compare_as_canonical_json() -> None:
    assert pub._same_value('{"v":1}', '{"v": 1}')
    assert not pub._same_value("1", "true") and not pub._same_value("1", "1.0")


_LOCKLESS = {
    "lock_mode": "",
    "lock_until": None,
    "lock_reason": "",
    "locked_by": "",
    "locked_at": None,
}


def _over(journal: Journal, monkeypatch: pytest.MonkeyPatch, head: dict[str, Any]) -> None:
    async def heads(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return [head]

    monkeypatch.setattr(store, "memory_heads", heads)


def _copy(base: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {**base, **changes, "version": next(_VERSIONS)}


def _meta(row: dict[str, Any], *fields: str) -> Any:
    from server.memory.domain.temporal_capture import declare_fields, row_key

    return declare_fields({row_key(row): frozenset(fields)})


_LOCKCOLS = ("lock_mode", "lock_until", "lock_reason", "locked_by", "locked_at")


def test_a_metadata_write_sets_only_its_fields_over_the_journal(
    journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = mrow('"A"', **_LOCKLESS)
    run(pub.publish_rows([base]))
    locked = _copy(base, lock_mode="no_delete")
    with _meta(locked, *_LOCKCOLS):
        run(pub.publish_rows([locked]))
    _over(journal, monkeypatch, locked)
    # A status flip built from the head read before the lock keeps the lock.
    flip = _copy(base, status="conflicted")
    with _meta(flip, "status"):
        (row,) = run(pub.publish_rows([flip]))
    assert (row["status"], row["lock_mode"]) == ("conflicted", "no_delete")
    (a,) = json.loads(journal.commits[-1]["assertions"])
    assert (a["status"], a["lock_mode"]) == ("conflicted", "no_delete")
    # A delete built from that read is refused by the lock it never saw.
    gone = _copy(base, tombstoned_at=datetime.now(UTC))
    with _meta(gone, "tombstoned_at"):
        assert run(pub.publish_rows([gone])) == []


def test_a_metadata_write_over_moved_content_lands_on_what_holds_now(
    journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = mrow('"A"', **_LOCKLESS)
    run(pub.publish_rows([base]))
    moved = _copy(base, value='"B"', content_sha256=sha256_value_json('"B"'))
    run(pub.publish_rows([moved]))
    _over(journal, monkeypatch, moved)
    tag = _copy(base, tags=["t"])  # built from the read of "A"
    with _meta(tag, "tags"):
        (row,) = run(pub.publish_rows([tag]))
    assert (row["value"], row["tags"]) == ('"B"', ["t"])
    (a,) = json.loads(journal.commits[-1]["assertions"])
    assert (a["value"], a["tags"]) == ('"B"', ["t"])


def test_late_evidence_leaves_the_key_writable(journal: Journal) -> None:
    present = mrow('"present"', valid_at=datetime(2026, 6, 1, tzinfo=UTC), **_LOCKLESS)
    run(pub.publish_rows([present]))
    old = datetime(2024, 6, 1, tzinfo=UTC)
    late = mrow('"older"', valid_at=old, valid_to=old + timedelta(days=30))
    assert run(pub.publish_rows([late])) == []
    lock = _copy(present, lock_mode="no_delete")
    with _meta(lock, *_LOCKCOLS):
        assert run(pub.publish_rows([lock])) == [lock]
    assert run(
        pub.publish_rows(
            [_copy(present, value='"newer"', content_sha256=sha256_value_json('"newer"'))]
        )
    )


def test_unlocking_a_read_only_memory_with_an_expiry(journal: Journal) -> None:
    exp = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(hours=1)
    base = mrow('"A"', **_LOCKLESS, expires_at=exp)
    run(pub.publish_rows([base]))
    locked = _copy(base, lock_mode="read_only")
    with _meta(locked, *_LOCKCOLS):
        run(pub.publish_rows([locked]))
    unlock = _copy(locked, lock_mode="")
    with _meta(unlock, *_LOCKCOLS):
        assert run(pub.publish_rows([unlock])) == [unlock]


def test_a_set_after_a_concurrent_delete_asserts_its_value(journal: Journal) -> None:
    base = mrow('"A"', **_LOCKLESS)
    run(pub.publish_rows([base]))
    run(pub.publish_rows([_copy(base, tombstoned_at=datetime.now(UTC))]))
    new = _copy(base, value='"B"', content_sha256=sha256_value_json('"B"'))
    (row,) = run(pub.publish_rows([new]))
    assert row["tombstoned_at"] is None and row["value"] == '"B"'


def test_the_expiry_sweep_loses_to_a_touch_since_its_read(journal: Journal) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    old = datetime.now(UTC).replace(tzinfo=None, microsecond=0) - timedelta(minutes=1)
    base = mrow('"A"', **_LOCKLESS, expires_at=old)
    run(pub.publish_rows([base]))
    touched = _copy(base, expires_at=old + timedelta(days=1))
    with _meta(touched, "expires_at"):
        run(pub.publish_rows([touched]))
    sweep = _copy(base, tombstoned_at=datetime.now(UTC), expires_at=None)
    with _meta(sweep, "tombstoned_at", "expires_at"), write_intent(WriteIntent(conditional=True)):
        assert run(pub.publish_rows([sweep])) == []


def test_a_set_does_not_replace_a_value_pinned_since_it_read(journal: Journal) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    base = mrow('"A"', **_LOCKLESS, pinned=0)
    run(pub.publish_rows([base]))
    pin = _copy(base, pinned=1)
    with _meta(pin, "pinned"):
        run(pub.publish_rows([pin]))
    b = _copy(base, value='"B"', content_sha256=sha256_value_json('"B"'))
    with write_intent(WriteIntent(memory_key="k", pin_guard=True)):
        assert run(pub.publish_rows([b])) == []


def test_a_lock_never_loosens_one_committed_since_its_read(journal: Journal) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    base = mrow('"A"', **_LOCKLESS)
    run(pub.publish_rows([base]))
    strict = _copy(base, lock_mode="read_only")
    with _meta(strict, *_LOCKCOLS):
        run(pub.publish_rows([strict]))
    weak = _copy(base, lock_mode="no_delete")
    with _meta(weak, *_LOCKCOLS), write_intent(WriteIntent(memory_key="k", tighten_lock=True)):
        assert run(pub.publish_rows([weak])) == []


def test_the_same_value_under_new_qualifiers_is_a_new_claim(journal: Journal) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    one = mrow('"same"', **_LOCKLESS)
    with write_intent(WriteIntent(memory_key="k", qualifiers={"scope": "one"})):
        run(pub.publish_rows([one]))
    with write_intent(WriteIntent(memory_key="k", qualifiers={"scope": "two"})):
        assert run(pub.publish_rows([_copy(one)]))
    held = {
        json.dumps(a["qualifiers"]) for c in journal.commits for a in json.loads(c["assertions"])
    }
    assert held == {'{"scope": "one"}', '{"scope": "two"}'}


def test_a_lock_on_an_ended_claim_binds_nothing() -> None:
    now = datetime.now(UTC)
    ended = tm.first(
        '"A"',
        valid_from=now - timedelta(days=2),
        valid_to=now - timedelta(days=1),
        lock_mode="read_only",
    )
    held = tm.first('"B"', valid_from=now - timedelta(days=1))
    assert not pub.locked_against([ended, held], "value", now)
    assert pub.locked_against(
        [held.__class__(**{**held.__dict__, "lock_mode": "read_only"})], "value", now
    )


def test_a_touch_changes_only_the_expiry_on_every_claim(journal: Journal) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    for scope, conf in (("one", 0.2), ("two", 0.9)):
        with write_intent(WriteIntent(memory_key="k", qualifiers={"scope": scope})):
            run(pub.publish_rows([mrow('"v"', **_LOCKLESS, confidence=conf)]))
    exp = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(days=1)
    touch = mrow('"v"', **_LOCKLESS, confidence=0.9, expires_at=exp)
    with _meta(touch, "expires_at"):
        run(pub.publish_rows([touch]))
    added = json.loads(journal.commits[-1]["assertions"])
    assert sorted(a["confidence"] for a in added) == [0.2, 0.9]
    assert all(a["expires_at"].startswith(exp.isoformat()) for a in added)


def test_a_metadata_write_never_revives_a_deleted_key(journal: Journal) -> None:
    base = mrow('"A"', **_LOCKLESS)
    run(pub.publish_rows([base]))
    run(pub.publish_rows([_copy(base, tombstoned_at=datetime.now(UTC))]))
    tag = _copy(base, tags=["t"])  # built from a read before the delete
    with _meta(tag, "tags"):
        assert run(pub.publish_rows([tag])) == []


def test_a_stale_attestation_is_left_out(journal: Journal) -> None:
    base = mrow('"monday"', **_LOCKLESS, verified_by="", verified_sha256="")
    run(pub.publish_rows([base]))
    run(
        pub.publish_rows(
            [_copy(base, value='"friday"', content_sha256=sha256_value_json('"friday"'))]
        )
    )
    stale = _copy(base, verified_by="human:rev", verified_sha256=sha256_value_json('"monday"'))
    with _meta(stale, "verified_by", "verified_sha256"):
        assert run(pub.publish_rows([stale])) == []


def test_a_value_write_keeps_a_pin_committed_since_its_read(journal: Journal) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    base = mrow('"A"', **_LOCKLESS, pinned=0)
    run(pub.publish_rows([base]))
    pin = _copy(base, pinned=1)
    with _meta(pin, "pinned"):
        run(pub.publish_rows([pin]))
    again = _copy(base, tags=["x"])  # a restatement built from the unpinned read
    with write_intent(WriteIntent(memory_key="k", pin_guard=True, stated=frozenset())):
        (row,) = run(pub.publish_rows([again]))
    assert row["pinned"] == 1


def test_a_value_write_never_lowers_the_texts_sensitivity(journal: Journal) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    base = mrow('"A"', **_LOCKLESS, sensitivity="")
    run(pub.publish_rows([base]))
    high = _copy(base, sensitivity="high")
    with _meta(high, "sensitivity"):
        run(pub.publish_rows([high]))
    again = _copy(base, tags=["x"])
    with write_intent(WriteIntent(memory_key="k", stated=frozenset())):
        (row,) = run(pub.publish_rows([again]))
    assert row["sensitivity"] == "high"


def test_an_inherited_pin_never_authorizes_replacing_the_pinned_value(journal: Journal) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    base = mrow('"A"', **_LOCKLESS, pinned=0)
    run(pub.publish_rows([base]))
    pin = _copy(base, pinned=1)
    with _meta(pin, "pinned"):
        run(pub.publish_rows([pin]))
    b = _copy(base, value='"B"', content_sha256=sha256_value_json('"B"'))
    with write_intent(WriteIntent(memory_key="k", pin_guard=True, stated=frozenset())):
        assert run(pub.publish_rows([b])) == []


def test_a_metadata_write_never_lands_on_a_recreated_memory(journal: Journal) -> None:
    base = mrow('"A"', **_LOCKLESS)
    run(pub.publish_rows([base]))
    run(pub.publish_rows([_copy(base, tombstoned_at=datetime.now(UTC))]))
    reborn = _copy(
        base,
        value='"B"',
        content_sha256=sha256_value_json('"B"'),
        memory_id="00000000-0000-0000-0000-0000000000bb",
    )
    run(pub.publish_rows([reborn]))
    lock = _copy(base, lock_mode="no_delete")  # read A, under A's memory id
    with _meta(lock, *_LOCKCOLS):
        assert run(pub.publish_rows([lock])) == []


def test_a_new_value_never_inherits_an_expiry_that_has_passed(journal: Journal) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    gone = datetime.now(UTC).replace(tzinfo=None, microsecond=0) - timedelta(seconds=5)
    base = mrow('"A"', **_LOCKLESS, expires_at=gone)
    run(pub.publish_rows([base]))
    new = _copy(base, value='"B"', content_sha256=sha256_value_json('"B"'), expires_at=None)
    with write_intent(WriteIntent(memory_key="k", stated=frozenset())):
        (row,) = run(pub.publish_rows([new]))
    assert row["expires_at"] is None


def test_late_evidence_keeps_its_own_expiry(journal: Journal) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    ahead = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(days=1)
    run(
        pub.publish_rows(
            [
                mrow(
                    '"now"',
                    **_LOCKLESS,
                    valid_at=datetime(2026, 1, 1, tzinfo=UTC),
                    expires_at=ahead,
                )
            ]
        )
    )
    old = datetime(2024, 1, 1, tzinfo=UTC)
    late = mrow('"then"', **_LOCKLESS, valid_at=old, valid_to=old + timedelta(days=30))
    with write_intent(WriteIntent(memory_key="k", stated=frozenset())):
        assert run(pub.publish_rows([late])) == []
    added = json.loads(journal.commits[-1]["assertions"])
    assert [a["expires_at"] for a in added if a["value"] == '"then"'] == [None]


def test_an_update_never_lands_on_a_recreated_memory(journal: Journal) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    base = mrow('"A"', **_LOCKLESS)
    run(pub.publish_rows([base]))
    run(pub.publish_rows([_copy(base, tombstoned_at=datetime.now(UTC))]))
    run(
        pub.publish_rows(
            [
                _copy(
                    base,
                    value='"B"',
                    content_sha256=sha256_value_json('"B"'),
                    memory_id="00000000-0000-0000-0000-0000000000bb",
                )
            ]
        )
    )
    update = _copy(base, value='"C"', content_sha256=sha256_value_json('"C"'))  # read A
    with write_intent(WriteIntent(memory_key="k", existing=True, stated=frozenset())):
        assert run(pub.publish_rows([update])) == []


def test_an_explicit_unpinned_set_never_replaces_a_pinned_value(journal: Journal) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    base = mrow('"A"', **_LOCKLESS, pinned=1)
    run(pub.publish_rows([base]))
    # The handler kept the pin of the row it read; the caller asked for none.
    b = _copy(base, value='"B"', content_sha256=sha256_value_json('"B"'), pinned=1)
    intent = WriteIntent(memory_key="k", pin_guard=True, stated=frozenset({"pinned"}))
    with write_intent(intent):
        assert run(pub.publish_rows([b])) == []


def test_a_plan_never_inherits_the_present_ttl(journal: Journal) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    ahead = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(seconds=30)
    run(pub.publish_rows([mrow('"now"', **_LOCKLESS, expires_at=ahead)]))
    plan = mrow(
        '"later"', **_LOCKLESS, valid_at=datetime.now(UTC) + timedelta(days=2), expires_at=ahead
    )
    with write_intent(WriteIntent(memory_key="k", stated=frozenset())):
        assert run(pub.publish_rows([plan])) == []
    added = json.loads(journal.commits[-1]["assertions"])
    assert [a["expires_at"] for a in added if a["value"] == '"later"'] == [None]


def test_open_ended_late_evidence_keeps_none_of_the_present_ttl(journal: Journal) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    ahead = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(hours=2)
    bob = mrow('"bob"', **_LOCKLESS, valid_at=datetime(2026, 1, 9, tzinfo=UTC), expires_at=ahead)
    run(pub.publish_rows([bob]))
    carol = mrow(
        '"carol"', **_LOCKLESS, valid_at=datetime(2026, 1, 3, tzinfo=UTC), expires_at=ahead
    )
    with write_intent(WriteIntent(memory_key="k", stated=frozenset())):
        assert run(pub.publish_rows([carol])) == []
    added = json.loads(journal.commits[-1]["assertions"])
    assert [a["expires_at"] for a in added if a["value"] == '"carol"'] == [None]


def test_removing_what_holds_now_removes_the_row_whatever_history_remains(journal: Journal) -> None:
    paris = mrow(
        '"paris"',
        **_LOCKLESS,
        valid_at=datetime(2020, 1, 1, tzinfo=UTC),
        valid_to=datetime(2021, 1, 1, tzinfo=UTC),
    )
    run(pub.publish_rows([paris]))
    berlin = mrow('"berlin"', **_LOCKLESS, valid_at=datetime(2022, 1, 1, tzinfo=UTC))
    run(pub.publish_rows([berlin]))
    gone = _copy(berlin, tombstoned_at=datetime.now(UTC))
    (row,) = run(pub.publish_rows([gone]))
    assert row["tombstoned_at"] is not None


def test_a_revision_keeps_its_claims_sub_second_start(journal: Journal) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    start = datetime(2026, 3, 1, 10, 0, 0, 600000, tzinfo=UTC)
    leeds = mrow('"leeds"', **_LOCKLESS, valid_at=start)
    run(pub.publish_rows([leeds]))
    # The row update read keeps validity to the second, as loom.memory stores it.
    hull = _copy(
        leeds,
        value='"hull"',
        content_sha256=sha256_value_json('"hull"'),
        valid_at=start.replace(microsecond=0),
    )
    with write_intent(WriteIntent(memory_key="k", existing=True, stated=frozenset())):
        (row,) = run(pub.publish_rows([hull]))
    assert row["value"] == '"hull"' and tm.utc(row["valid_at"]) == start


def test_an_update_revises_the_claim_the_row_shows_under_its_qualifiers(
    journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    for scope, value in (("eu", '"hull"'), ("us", '"boston"')):
        with write_intent(WriteIntent(memory_key="k", qualifiers={"region": scope})):
            run(pub.publish_rows([mrow(value, **_LOCKLESS)]))
    us = journal.commits[-1]
    head = mrow(
        '"boston"', **_LOCKLESS, version=int(json.loads(us["assertions"])[0]["source_version"])
    )
    _over(journal, monkeypatch, head)
    austin = _copy(head, value='"austin"', content_sha256=sha256_value_json('"austin"'))
    with write_intent(WriteIntent(memory_key="k", existing=True, stated=frozenset())):
        assert run(pub.publish_rows([austin]))
    (a,) = [x for x in json.loads(journal.commits[-1]["assertions"]) if x["value"] == '"austin"']
    assert a["qualifiers"] == {"region": "us"}


def test_an_update_precondition_is_checked_against_the_journal(journal: Journal) -> None:
    from server.core.errors import PreconditionFailedError
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    base = mrow('"leeds"', **_LOCKLESS)
    run(pub.publish_rows([base]))
    run(pub.publish_rows([_copy(base, value='"york"', content_sha256=sha256_value_json('"york"'))]))
    hull = _copy(base, value='"hull"', content_sha256=sha256_value_json('"hull"'))
    # The handler checked the caller's hash against the Leeds row it read.
    intent = WriteIntent(
        memory_key="k", existing=True, stated=frozenset(), expected_version=base["version"]
    )
    with write_intent(intent), pytest.raises(PreconditionFailedError):
        run(pub.publish_rows([hull]))


def test_an_update_inherits_only_from_its_own_qualifiers_claim(
    journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    with write_intent(WriteIntent(memory_key="k", qualifiers={"region": "us"})):
        run(pub.publish_rows([mrow('"boston"', **_LOCKLESS, pinned=0)]))
    us = journal.commits[-1]
    with write_intent(WriteIntent(memory_key="k", qualifiers={"region": "eu"})):
        run(pub.publish_rows([mrow('"leeds"', **_LOCKLESS, pinned=1)]))
    head = mrow(
        '"boston"',
        **_LOCKLESS,
        pinned=0,
        version=int(json.loads(us["assertions"])[0]["source_version"]),
    )
    _over(journal, monkeypatch, head)
    austin = _copy(head, value='"austin"', content_sha256=sha256_value_json('"austin"'))
    with write_intent(WriteIntent(memory_key="k", existing=True, stated=frozenset())):
        (row,) = run(pub.publish_rows([austin]))
    assert row["pinned"] == 0


def test_a_restated_claim_stays_the_one_its_row_shows(
    journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    for scope in ("us", "eu"):
        with write_intent(WriteIntent(memory_key="k", qualifiers={"region": scope})):
            run(pub.publish_rows([mrow('"A"', **_LOCKLESS)]))
    with write_intent(WriteIntent(memory_key="k", qualifiers={"region": "eu"})):
        (row,) = run(pub.publish_rows([mrow('"A"', **_LOCKLESS)]))
    _over(journal, monkeypatch, row)
    c = _copy(row, value='"C"', content_sha256=sha256_value_json('"C"'))
    with write_intent(WriteIntent(memory_key="k", existing=True, stated=frozenset())):
        run(pub.publish_rows([c]))
    (a,) = [x for x in json.loads(journal.commits[-1]["assertions"]) if x["value"] == '"C"']
    assert a["qualifiers"] == {"region": "eu"}


def test_read_only_protects_the_claim_being_changed(journal: Journal) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    with write_intent(WriteIntent(memory_key="k", qualifiers={"region": "us"})):
        run(pub.publish_rows([mrow('"B"', **_LOCKLESS)]))
    with write_intent(WriteIntent(memory_key="k", qualifiers={"region": "eu"})):
        (eu,) = run(pub.publish_rows([mrow('"A"', **_LOCKLESS)]))
    lock = _copy(eu, lock_mode="read_only")  # as memory.lock writes it
    with _meta(lock, *_LOCKCOLS):
        run(pub.publish_rows([lock]))
    b = mrow('"B"', **_LOCKLESS)  # eu A → B: B is us's text, still a change to eu
    with write_intent(WriteIntent(memory_key="k", qualifiers={"region": "eu"}, stated=frozenset())):
        assert run(pub.publish_rows([b])) == []


def test_a_precondition_holds_on_a_row_a_metadata_change_wrote(
    journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    base = mrow('"A"', **_LOCKLESS)
    run(pub.publish_rows([base]))
    tagged = _copy(base, tags=["x"])  # an assessment: the claim keeps its source_version
    with _meta(tagged, "tags"):
        (head,) = run(pub.publish_rows([tagged]))
    _over(journal, monkeypatch, head)
    b = _copy(head, value='"B"', content_sha256=sha256_value_json('"B"'))
    intent = WriteIntent(
        memory_key="k", existing=True, stated=frozenset(), expected_version=head["version"]
    )
    with write_intent(intent):
        assert run(pub.publish_rows([b]))  # the caller checked its hash against this row


def test_a_redirect_never_lands_on_a_key_with_a_plan(
    journal: Journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    (head,) = run(
        pub.publish_rows([mrow('"A"', valid_at=datetime(2026, 1, 1, tzinfo=UTC), **_LOCKLESS)])
    )
    plan = datetime.now(UTC) + timedelta(days=30)
    assert run(pub.publish_rows([mrow('"B"', valid_at=plan, **_LOCKLESS)])) == []
    _over(journal, monkeypatch, head)
    from server.memory.domain.temporal_capture import WriteIntent, write_intent

    moved = _copy(head, status="duplicate", duplicate_of="dest")
    with (
        _meta(moved, "status", "duplicate_of"),
        write_intent(WriteIntent(memory_key="k", redirect=True)),
    ):
        assert run(pub.publish_rows([moved])) == []  # the plan would revive it behind the redirect
    marked = _copy(head, status="duplicate", duplicate_of="peer")
    with _meta(marked, "status", "duplicate_of"):
        assert run(pub.publish_rows([marked]))  # a duplicate mark is not a move


def test_late_evidence_names_no_row_version(journal: Journal) -> None:
    """It writes no loom.memory row, so its commit names none: a row version there
    would read as a row still landing, and hold back every repair of the key."""
    run(pub.publish_rows([mrow('"v"', valid_at=datetime(2026, 1, 1, tzinfo=UTC))]))
    late = mrow(
        '"w"', valid_at=datetime(2025, 1, 1, tzinfo=UTC), valid_to=datetime(2025, 6, 1, tzinfo=UTC)
    )
    assert run(pub.publish_rows([late])) == []
    assert journal.commits[-1]["row_version"] == 0
    assert journal.commits[0]["row_version"] > 0


def test_a_recheck_under_the_lock_refuses_the_whole_batch(journal: Journal) -> None:
    async def scrubbed_since() -> None:
        raise PreconditionFailedError("changed since planned")

    def build(served: list[tm.Assertion]) -> tm.Amendment:
        return tm.Amendment("correct", (), (tm.first('"X"'),))

    ident = ("a", "-", "-", "k")
    with pytest.raises(PreconditionFailedError):
        run(
            pub.publish_amendments(
                "o",
                "n",
                [(ident, "00000000-0000-0000-0000-000000000001", build)],
                operation="t",
                recheck=scrubbed_since,
            )  # fmt: skip
        )
    assert journal.commits == []


def test_a_raised_version_reaches_the_claims_later_rows_of_the_batch_carry(
    journal: Journal, monkeypatch
) -> None:
    # One batch: C, then C again with new tags (an assessment carrying C's claim). A
    # projection lands above both drawn versions before the lock: every claim the
    # batch commits names a version one of its rows was written at.
    run(pub.publish_rows([mrow('"A"')]))
    c1, c2 = mrow('"C"'), mrow('"C"', tags=["x"])
    drawn = {c1["version"], c2["version"]}
    landed = {"v": 0}
    real_head = journal.head
    calls = {"n": 0}

    async def head(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:  # under the lock
            landed["v"] = max(drawn) + 5
        return await real_head(*a, **k)

    async def versions(*_a, **_k):
        # A projection row this build wrote: stamped at its own version.
        return {("a", "-", "-", "k"): (landed["v"], landed["v"])} if landed["v"] else {}

    monkeypatch.setattr(store, "head", head)
    monkeypatch.setattr(store, "landed_heads", versions)
    rows = run(pub.publish_rows([c1, c2]))
    written = {r["version"] for r in rows}
    assert written.isdisjoint(drawn)
    claims = [a for c in journal.commits[1:] for a in json.loads(c["assertions"])]
    assert claims and {a["source_version"] for a in claims} <= written


def test_a_bulk_entry_answers_with_the_version_its_row_was_published_at() -> None:
    from server.memory.api.models_bulk import _BulkSetResult
    from server.memory.app.bulk_set import _answer_published_versions

    first, second, skipped = (
        _BulkSetResult(memory_key="k", version=10),
        _BulkSetResult(memory_key="k", version=11),
        _BulkSetResult(memory_key="j", version=0),
    )
    rows = [{"memory_key": "k", "version": 10}, {"memory_key": "k", "version": 11}]
    drawn = [(r, int(r["version"])) for r in rows]
    rows[0]["version"], rows[1]["version"] = 20, 21  # the publisher raised both
    _answer_published_versions([first, second, skipped], drawn)
    assert (first.version, second.version, skipped.version) == (20, 21, 0)


def test_a_lease_lost_while_publishing_is_a_temporal_503(journal: Journal, monkeypatch) -> None:
    from server.core.leases import LeaseLost

    @contextlib.asynccontextmanager
    async def lost(name: str, **_k: Any) -> Any:
        yield types.SimpleNamespace(held=True)
        raise LeaseLost(name)  # as Lease.renewed raises it once the block exits

    monkeypatch.setattr(pub._locks, "lease", lost)
    with pytest.raises(TemporalUnavailableError, match="lease lost"):
        run(pub.publish_amendments(
            "o", "n", [(("a", "-", "-", "k"), "00000000-0000-0000-0000-000000000001",
                        lambda served: tm.Amendment("correct", (), (tm.first('"X"'),)))],
            operation="t",
        ))  # fmt: skip


@pytest.mark.parametrize(
    ("version", "journal_version", "committed", "foreign"),
    [
        (20, 20, 10, False),  # this build wrote it
        (20, 10, 10, True),  # another build wrote it, carrying the column it found
        (20, 0, 10, True),  # another build made the key
        (20, 10, 20, False),  # already journaled (adopted)
        (0, 0, 0, False),
    ],
)
def test_a_row_another_build_wrote_is_foreign(
    version: int, journal_version: int, committed: int, foreign: bool
) -> None:
    head = {"version": version, "journal_version": journal_version}
    assert pub.foreign(head, committed) is foreign


def test_a_value_another_build_wrote_is_journaled_as_observed(
    journal: Journal, monkeypatch
) -> None:
    run(pub.publish_rows([mrow('"Paris"')]))
    first = journal.commits[-1]
    berlin = mrow('"Berlin"', journal_version=first["row_version"])  # carried by the other build

    async def heads(*_a, **_k):
        return [berlin]

    async def committed(*_a, **_k):
        return {("a", "-", "-", "k"): first["row_version"]}

    monkeypatch.setattr(store, "memory_heads", heads)
    monkeypatch.setattr(store, "newest_row_versions", committed)
    assert run(pub.adopt("o", "n", [("a", "-", "-", "k")])) == 1
    observed = journal.commits[-1]
    assert (observed["operation"], observed["row_version"]) == (
        "memory.observed",
        berlin["version"],
    )
    assert observed["change_kind"] == "assert" and "Berlin" in observed["assertions"]


def test_a_row_another_build_lands_before_the_lock_is_adopted_not_overwritten(
    journal: Journal, monkeypatch
) -> None:
    # C is planned; another build writes B (drawn later) before the lock: the batch is
    # planned again, journals B as observed, and C, drawn earlier, is left out.
    run(pub.publish_rows([mrow('"A"')]))
    c = mrow('"C"')
    b = mrow('"B"', journal_version=journal.commits[-1]["row_version"])
    landed: dict[str, Any] = {}
    real_head = journal.head
    calls = {"n": 0}

    async def head(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:  # under the lock
            landed["row"] = b
        return await real_head(*a, **k)

    async def heads(*_a, **_k):
        return [landed["row"]] if landed else []

    async def versions(*_a, **_k):
        return {("a", "-", "-", "k"): (b["version"], b["journal_version"])} if landed else {}

    monkeypatch.setattr(store, "head", head)
    monkeypatch.setattr(store, "memory_heads", heads)
    monkeypatch.setattr(store, "landed_heads", versions)
    assert run(pub.publish_rows([c])) == []  # C was drawn before B: B wins
    ops = [x["operation"] for x in journal.commits]
    assert ops[-1] == "memory.observed" and "B" in journal.commits[-1]["assertions"]


def test_a_key_only_another_build_wrote_is_seeded_and_written_not_planned_again(
    journal: Journal, monkeypatch
) -> None:
    # The key's only row is another build's (no commit, an unstamped journal_version):
    # the seed journals it, and the write goes on without planning again forever.
    theirs = mrow('"Paris"', journal_version=0)

    async def heads(*_a, **_k):
        return [theirs]

    async def versions(*_a, **_k):
        return {("a", "-", "-", "k"): (theirs["version"], 0)}

    monkeypatch.setattr(store, "memory_heads", heads)
    monkeypatch.setattr(store, "landed_heads", versions)
    (row,) = run(pub.publish_rows([mrow('"Berlin"')]))
    ops = [c["operation"] for c in journal.commits]
    assert ops[0] == "memory.journal_seed" and row["value"] == '"Berlin"'


def test_another_builds_resave_of_the_same_value_does_not_block_the_next_write(
    journal: Journal, monkeypatch
) -> None:
    # The other build re-saved A as it is: its row changes nothing the journal models,
    # so it is journaled as an empty observed commit and the next write goes through.
    run(pub.publish_rows([mrow('"A"')]))
    resaved = mrow('"A"', journal_version=journal.commits[-1]["row_version"])

    async def heads(*_a, **_k):
        return [resaved]

    async def versions(*_a, **_k):
        return {("a", "-", "-", "k"): (resaved["version"], resaved["journal_version"])}

    monkeypatch.setattr(store, "memory_heads", heads)
    monkeypatch.setattr(store, "landed_heads", versions)
    (row,) = run(pub.publish_rows([mrow('"B"')]))
    kinds = [(c["operation"], c["change_kind"]) for c in journal.commits[1:]]
    assert kinds[0] == ("memory.observed", "restate") and row["value"] == '"B"'
    assert journal.commits[1]["row_version"] == resaved["version"]
