"""The Postgres authority's publication batch: commits queued together are sent in one
insert per ClickHouse table, each under its own lease, and what fails is retried alone or
left to the delivery worker."""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Any

import pytest

from server.core import pg_lease
from server.core.ch_writes import MAX_PARTITIONS_PER_INSERT, partition_blocks
from server.core.errors import BackendUnavailableError
from server.memory.store import pg_batch, pg_memory, pg_publish
from server.memory.store.pg_memory import AUDIT_ROW, MEMORY_ROW, OLD_VECTORS, SEARCH_DOC
from server.memory.store.rows import created_month
from server.settings import settings
from tests.unit.clock_fake import ManualClock

_FULL = MEMORY_ROW | AUDIT_ROW | SEARCH_DOC


def _row(org: str, ns: str, key: str, month: str = "2026-01") -> dict[str, Any]:
    return {
        "org": org,
        "namespace": ns,
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": key,
        "version": 1,
        "created_at": f"{month}-02 03:04:05",
    }


class _Batching:
    """One test's batch: a manual clock behind it, the sleeps it makes, and the
    ClickHouse inserts and Postgres completions its flushes make."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, interval_ms: int = 0) -> None:
        self.clock = ManualClock(t=time.monotonic())
        self.memory: list[list[str]] = []
        self.documents: list[list[str]] = []
        self.audits: list[int] = []
        self.landed: list[tuple[Any, int]] = []
        self.handed_over: list[Any] = []
        self.refuse_memory: set[str] = set()
        # Every memory insert's keys, tried or not; a send of one row of `hold` waits
        # for `release`; `alone` is what a send of one row raises.
        self.tried: list[list[str]] = []
        self.hold: set[str] = set()
        self.release = asyncio.Event()
        self.alone: BaseException | None = None
        self.refuse_documents: BaseException | None = None
        self.refuse_retirement = False
        # Each retirement's keys; `retire_alone` is what a retirement of one commit raises.
        self.retired: list[list[str]] = []
        self.retire_alone: BaseException | None = None
        # Keys whose head has moved on since their write.
        self.moved: set[str] = set()
        # Keys whose head is a newer row the test gives, by key.
        self.heads: dict[str, dict[str, Any]] = {}
        self.head_owes = 0  # the targets the heads' own deliveries owe
        self.document_rows: list[dict[str, Any]] = []
        # Seconds the heads read takes on the batch's clock.
        self.heads_take = 0.0
        self.inserting = asyncio.Event()
        self.inserting.set()
        self.ch = object()  # the flush groups commits by client
        real_sleep = asyncio.sleep

        async def _sleep(seconds: float) -> None:
            self.clock.sleep(seconds)
            await real_sleep(0)

        async def _memory(rows: Any, *, ch: Any, published: bool) -> int:
            assert published
            await self.inserting.wait()
            keys = [r["memory_key"] for r in rows]
            self.tried.append(keys)
            if len(keys) == 1 and self.hold & set(keys):
                await self.release.wait()
            if len(keys) == 1 and self.alone is not None:
                raise self.alone
            if self.refuse_memory & set(keys):
                raise RuntimeError("refused")
            self.memory.append(keys)
            return len(rows)

        async def _documents(rows: Any, *, ch: Any) -> None:
            if self.refuse_documents is not None:
                raise self.refuse_documents
            self.documents.append([r["memory_key"] for r in rows])
            self.document_rows += rows

        async def _audits(rows: Any, *, ch: Any, retried: bool) -> None:
            self.audits.append(len(rows))

        async def _landed(landed: Any) -> None:
            self.landed += landed

        async def _heads(keys: Any, **_: Any) -> dict[Any, Any]:
            self.clock.advance(self.heads_take)
            return {
                k: (
                    self.heads.get(k[-1]) or {"version": 2 if k[-1] in self.moved else 1},
                    self.head_owes,
                )
                for k in keys
            }

        async def _retire(rows: Any, seqs: Any, **_: Any) -> None:
            self.retired.append([r["memory_key"] for r in rows])
            if len(rows) == 1 and self.retire_alone is not None:
                raise self.retire_alone
            if self.refuse_retirement:
                raise RuntimeError("retirement refused")

        async def _records(owing: Any, **_: Any) -> None:
            return None

        async def _hand_over(seqs: Any) -> None:
            self.handed_over += seqs

        monkeypatch.setattr(settings, "pg_publish_interval_ms", interval_ms)
        monkeypatch.setattr(settings, "pg_projection_wait_ms", 60_000)  # no cap on the pace
        monkeypatch.setitem(pg_batch._batches, asyncio.get_running_loop(), pg_batch._Batch())
        monkeypatch.setattr(pg_batch, "time", SimpleNamespace(monotonic=self.clock.monotonic))
        monkeypatch.setattr(asyncio, "sleep", _sleep)
        monkeypatch.setattr("server.memory.store.inserts.insert_memory_versions", _memory)
        monkeypatch.setattr("server.memory.store.inserts.insert_embedding_rows", _documents)
        monkeypatch.setattr(pg_publish, "insert_audit_rows", _audits)
        monkeypatch.setattr(pg_publish, "retire_old_vectors", _retire)
        monkeypatch.setattr(pg_publish, "retire_decision_records", _records)
        monkeypatch.setattr(pg_memory, "complete_each", _landed)
        monkeypatch.setattr(pg_memory, "heads_owing", _heads)
        monkeypatch.setattr(pg_memory, "hand_over", _hand_over)

    def committed(
        self,
        key: str,
        seq: int,
        *,
        org: str = "o",
        ns: str = "n",
        lease_s: float = 30,
        targets: int = _FULL,
    ) -> pg_memory.Committed:
        return pg_memory.Committed(
            rows=[_row(org, ns, key)],
            audits=[{"org": org, "row_hash": f"h{seq}"}],
            seqs=[(org, ns, seq)],
            targets=[targets],
            deadline=self.clock.monotonic() + lease_s,
        )

    async def submit(
        self,
        key: str,
        seq: int,
        *,
        org: str = "o",
        ns: str = "n",
        documented: bool = True,
        lease_s: float = 30,
        wait: bool = True,
    ) -> None:
        """A commit of `key`, with its search document unless not `documented`."""
        committed = self.committed(key, seq, org=org, ns=ns, lease_s=lease_s)
        docs = [_row(org, ns, key)] if documented else []
        await pg_batch.submit(committed, documents=docs, ch=self.ch, wait=wait)


async def test_commits_queued_together_are_one_insert_per_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Writes of two orgs queued while a flush runs share the next one's inserts; each
    delivery clears what landed for it, its search document only where it handed one in."""
    batch = _Batching(monkeypatch)
    batch.inserting.clear()  # the first flush waits on ClickHouse
    first = asyncio.ensure_future(batch.submit("k1", 1))
    await asyncio.sleep(0)
    queued = asyncio.gather(
        batch.submit("k2", 2),
        batch.submit("k3", 3, org="o2", ns="m", documented=False),
        batch.submit("k4", 4, org="o2", ns="m"),
    )
    await asyncio.sleep(0)
    batch.inserting.set()
    await first
    await queued
    assert batch.memory == [["k1"], ["k2", "k3", "k4"]]
    assert batch.documents == [["k1"], ["k2", "k4"]]
    assert batch.audits == [1, 3]
    assert sorted(batch.landed) == [
        (("o", "n", 1), _FULL),
        (("o", "n", 2), _FULL),
        (("o2", "m", 3), MEMORY_ROW | AUDIT_ROW),
        (("o2", "m", 4), _FULL),
    ]


@pytest.mark.parametrize(
    ("interval_ms", "at_once", "then_waits"),
    [(250, 4, 0.25), (600, 1, 0.2), (2000, 1, 2.0)],
)
async def test_flushes_start_at_once_up_to_a_seconds_worth(
    monkeypatch: pytest.MonkeyPatch, interval_ms: int, at_once: int, then_waits: float
) -> None:
    """Flushes start at most 1000 / interval a second on average: up to a second's worth
    (at least one) start at once, so a writer that writes now and then sends each write at
    once, and the next waits for the bucket to hold one again."""
    batch = _Batching(monkeypatch, interval_ms=interval_ms)
    for i in range(at_once):
        await batch.submit(f"k{i}", i)
    assert batch.clock.sleeps == [] and len(batch.memory) == at_once
    await batch.submit("next", at_once)
    assert batch.clock.sleeps == [pytest.approx(then_waits)]
    assert batch.memory[-1] == ["next"]


async def test_the_pace_leaves_a_read_half_its_wait_for_the_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live change of the pace is not checked against LOOM_PG_PROJECTION_WAIT_MS: the
    flush caps it at half that wait, so a read right after a write is not refused."""
    batch = _Batching(monkeypatch, interval_ms=2000)
    monkeypatch.setattr(settings, "pg_projection_wait_ms", 1000)
    for i in range(3):  # paced as 500 ms: two at once, then one after half a second
        await batch.submit(f"k{i}", i)
    assert batch.clock.sleeps == [pytest.approx(0.5)]


async def test_turning_the_pace_off_and_on_starts_with_a_full_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flushes sent while the pace is off (0) spend nothing: setting it back starts the
    bucket full, not in debt."""
    batch = _Batching(monkeypatch, interval_ms=250)
    await batch.submit("k0", 0)
    monkeypatch.setattr(settings, "pg_publish_interval_ms", 0)
    for i in range(1, 50):
        await batch.submit(f"k{i}", i)
    monkeypatch.setattr(settings, "pg_publish_interval_ms", 250)
    for i in range(50, 54):
        await batch.submit(f"k{i}", i)
    assert batch.clock.sleeps == [] and len(batch.memory) == 54


async def test_a_request_that_does_not_wait_can_wait_for_its_own_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write that answers at its commit leaves its publication for the request's graph
    fences: `own_write_published` returns once the flush has landed it."""
    batch = _Batching(monkeypatch)
    batch.inserting.clear()
    await batch.submit("k", 1, wait=False)
    waiting = asyncio.ensure_future(pg_batch.own_write_published())
    await asyncio.sleep(0)
    assert not waiting.done()
    batch.inserting.set()
    await waiting
    assert batch.memory == [["k"]]


async def test_a_waited_write_keeps_the_requests_earlier_unwaited_one_for_its_fences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conflict mark's copy waits for its own flush only: the request's fences still wait
    for its first write, whatever order the flush sends them in."""
    batch = _Batching(monkeypatch)
    batch.inserting.clear()
    await batch.submit("k", 1, wait=False)
    (first,) = pg_batch._batch().queue
    asyncio.get_running_loop().call_soon(batch.inserting.set)
    await batch.submit("k", 2)  # awaited in the request's own context, as the mark is
    own = pg_batch._own_write.get()
    assert own is not None and own[0] is first.done


async def test_a_refused_row_is_sent_again_commit_by_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One row ClickHouse refuses holds back only its own commit, whose waiting writer is
    told its publication is pending."""
    batch = _Batching(monkeypatch)
    batch.refuse_memory = {"bad"}
    done = await asyncio.gather(
        batch.submit("good", 2), batch.submit("bad", 3), return_exceptions=True
    )
    assert done[0] is None and isinstance(done[1], pg_lease.PublicationPending)
    assert batch.memory == [["good"]]
    cleared = {seq: 0 for seq, _ in batch.landed}
    for seq, d in batch.landed:
        cleared[seq] |= d
    # The refused commit's memory row stays owed; its audit row and document do not.
    assert cleared == {("o", "n", 2): _FULL, ("o", "n", 3): AUDIT_ROW | SEARCH_DOC}


async def test_a_commit_that_lands_alone_answers_before_the_other_resends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused flush's resends run one after another: a commit that landed on its own
    answers then, not once every other one has been tried."""
    batch = _Batching(monkeypatch)
    batch.refuse_memory, batch.hold = {"bad"}, {"bad"}
    good = asyncio.ensure_future(batch.submit("good", 1))
    bad = asyncio.ensure_future(batch.submit("bad", 2))
    for _ in range(20):
        await asyncio.sleep(0)
    assert good.done() and good.exception() is None and not bad.done()
    batch.release.set()
    with pytest.raises(pg_lease.PublicationPending):
        await bad


async def test_a_resend_clickhouse_does_not_answer_leaves_the_rest_to_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sent alone after a refusal, a commit ClickHouse does not answer stops the resends:
    the rest would wait out the same deadline one after another."""
    batch = _Batching(monkeypatch)
    batch.refuse_memory, batch.alone = {"c"}, TimeoutError("no answer")
    done = await asyncio.gather(
        batch.submit("a", 1), batch.submit("b", 2), batch.submit("c", 3), return_exceptions=True
    )
    assert all(isinstance(d, pg_lease.PublicationPending) for d in done)
    assert batch.tried == [["a", "b", "c"], ["a"]]


async def test_a_refused_document_resends_only_the_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memory rows that landed are not sent again when only a document was refused:
    each commit then sends its own document alone."""
    batch = _Batching(monkeypatch)
    batch.refuse_documents = ValueError("a vector of the wrong width")
    done = await asyncio.gather(
        batch.submit("k1", 1), batch.submit("k2", 2), return_exceptions=True
    )
    assert all(isinstance(d, pg_lease.PublicationPending) for d in done)
    assert batch.memory == [["k1", "k2"]]
    assert sorted(batch.landed) == [
        (("o", "n", 1), MEMORY_ROW | AUDIT_ROW),
        (("o", "n", 2), MEMORY_ROW | AUDIT_ROW),
    ]


@pytest.mark.parametrize(
    "unanswered",
    [TimeoutError("the class deadline"), BackendUnavailableError("the connection dropped")],
)
async def test_a_flush_clickhouse_did_not_answer_is_left_whole(
    monkeypatch: pytest.MonkeyPatch, unanswered: BaseException
) -> None:
    """A timeout or a lost connection refused no row: no commit is sent again alone, each
    waiting out its own deadline in turn."""
    batch = _Batching(monkeypatch)
    batch.refuse_documents = unanswered
    done = await asyncio.gather(
        batch.submit("k1", 1), batch.submit("k2", 2), return_exceptions=True
    )
    assert all(isinstance(d, pg_lease.PublicationPending) for d in done)
    assert batch.documents == []
    assert batch.memory == [["k1", "k2"]]  # once, not again for each commit


async def test_a_document_whose_key_moved_on_is_not_sent_and_stays_owed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A head deleted, rewritten or copied since the write has its own retirement or
    columns, which the document would land live above: its delivery is handed to the
    delivery worker, which builds the document from the head or settles it."""
    batch = _Batching(monkeypatch)
    batch.moved = {"k1"}
    await asyncio.gather(batch.submit("k1", 1), batch.submit("k2", 2))
    assert batch.documents == [["k2"]]
    assert sorted(batch.landed) == [
        (("o", "n", 1), MEMORY_ROW | AUDIT_ROW),
        (("o", "n", 2), _FULL),
    ]
    # Handed over at once, so no search waits out the writer's lease for it.
    assert batch.handed_over == [("o", "n", 1)]


@pytest.mark.parametrize("head_owes", [0, SEARCH_DOC])
async def test_a_document_whose_row_was_copied_is_sent_onto_the_copy(
    monkeypatch: pytest.MonkeyPatch, head_owes: int
) -> None:
    """A conflict mark, pin or touch that copied the row before its flush owes no document
    of its own: the writer's vector is sent with the copy's columns and version, and the
    writer's delivery is done, so nothing embeds the text again. A same-content revision
    that owes its own document keeps it: the writer's is not sent over it."""
    from server.memory.store.rows import search_document_row

    batch = _Batching(monkeypatch)
    sha = "a" * 64
    row = _row("o", "n", "k") | {
        "run_id": "",
        "memory_type": "fact",
        "sensitivity": "normal",
        "source_class": "user",
        "confidence": 1.0,
        "tags": [],
        "files": [],
        "entities": [],
        "source_resource_id": "",
        "reinforcement_count": 0,
        "last_reinforced_at": None,
        "status": "active",
        "content_sha256": sha,
        "tombstoned_at": None,
        "redacted_at": None,
    }
    doc = search_document_row(
        row,
        embedding_model="m",
        embedding=[0.5, 0.5],
        key_embedding=[0.1, 0.9],
        key_text="k",
        excerpt="a fact",
    )
    batch.heads["k"] = row | {"version": 2, "status": "conflicted"}
    batch.head_owes = head_owes
    committed = batch.committed("k", 1)
    committed.rows[0].update(row)
    await pg_batch.submit(committed, documents=[doc], ch=batch.ch)
    if head_owes:
        assert batch.document_rows == [] and batch.handed_over == [("o", "n", 1)]
        return
    (sent,) = batch.document_rows
    assert (sent["version"], sent["status"]) == (2, "conflicted")
    assert (sent["embedding"], sent["memory_key_embedding"]) == ([0.5, 0.5], [0.1, 0.9])
    assert batch.landed == [(("o", "n", 1), _FULL)] and batch.handed_over == []


async def test_a_lease_that_ends_during_the_heads_read_leaves_only_its_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leases are checked again after the heads read: a commit whose lease ended
    during it is not sent, and the rest of the flush is sent together, not one by one."""
    batch = _Batching(monkeypatch)
    batch.heads_take = 2.0
    done = await asyncio.gather(
        batch.submit("late", 1, lease_s=1),
        batch.submit("k2", 2),
        batch.submit("k3", 3),
        return_exceptions=True,
    )
    assert isinstance(done[0], pg_lease.PublicationPending) and done[1:] == [None, None]
    assert batch.memory == [["k2", "k3"]]


async def test_a_refusal_after_a_lease_ran_out_sends_only_the_live_commits_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit whose lease ended during the heads read is not tried alone with the ones
    a refusal sends again."""
    batch = _Batching(monkeypatch)
    batch.heads_take = 2.0
    batch.refuse_memory = {"bad"}
    done = await asyncio.gather(
        batch.submit("late", 1, lease_s=1),
        batch.submit("bad", 2),
        batch.submit("good", 3),
        return_exceptions=True,
    )
    assert isinstance(done[0], pg_lease.PublicationPending) and done[2] is None
    assert isinstance(done[1], pg_lease.PublicationPending)
    assert batch.memory == [["good"]]  # "late" never sent, "bad" refused alone too


async def test_a_failed_retirement_leaves_only_the_commits_that_owe_it_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flush's old vectors are retired together; a retirement that fails leaves the
    commits that owe it pending, not the rest of the flush."""
    batch = _Batching(monkeypatch)
    batch.refuse_retirement = True
    owes = batch.committed("unembedded", 1, targets=MEMORY_ROW | AUDIT_ROW | OLD_VECTORS)
    plain = batch.committed("plain", 2, targets=MEMORY_ROW | AUDIT_ROW)
    done = await asyncio.gather(
        pg_batch.submit(owes, ch=batch.ch),
        pg_batch.submit(plain, ch=batch.ch),
        return_exceptions=True,
    )
    assert isinstance(done[0], pg_lease.PublicationPending) and done[1] is None
    # Refused, so the worker takes it at once.
    assert batch.handed_over == [("o", "n", 1)]


@pytest.mark.parametrize("alone", [None, TimeoutError("no answer")])
async def test_a_refused_retirement_is_tried_commit_by_commit_until_one_goes_unanswered(
    monkeypatch: pytest.MonkeyPatch, alone: BaseException | None
) -> None:
    """A retirement ClickHouse refused for the flush is tried again per commit; one it
    does not answer leaves the rest to the delivery worker rather than each waiting out
    the same deadline."""
    batch = _Batching(monkeypatch)
    batch.refuse_retirement, batch.retire_alone = True, alone
    owed = MEMORY_ROW | AUDIT_ROW | OLD_VECTORS
    done = await asyncio.gather(
        *(
            pg_batch.submit(batch.committed(k, seq, targets=owed), ch=batch.ch)
            for seq, k in enumerate(("a", "b", "c"), 1)
        ),
        return_exceptions=True,
    )
    assert all(isinstance(d, pg_lease.PublicationPending) for d in done)
    alone_tries = [["a"]] if alone else [["a"], ["b"], ["c"]]
    assert batch.retired == [["a", "b", "c"], *alone_tries]


async def test_a_bulk_set_cancelled_while_it_waits_for_its_flush_is_announced_and_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed batch whose caller is cancelled in its flush wait is announced all the
    same, and the flush still sends its rows."""
    from server.memory.app import bulk_set

    batch = _Batching(monkeypatch)
    batch.inserting.clear()
    announced: list[str] = []

    async def _announce(*_a: Any, **_k: Any) -> None:
        announced.append("announced")

    monkeypatch.setattr(bulk_set, "_fence_and_announce", _announce)
    state: Any = SimpleNamespace(embedding_rows=[], unpublished=None)
    task = asyncio.ensure_future(bulk_set._land_batch(batch.ch, state, batch.committed("k", 1), {}))
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert announced == ["announced"]
    batch.inserting.set()
    await pg_batch.drain(timeout_s=5)
    assert batch.memory == [["k"]]


async def test_a_drain_whose_flush_fails_returns_so_the_other_drains_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = _Batching(monkeypatch)
    batch.inserting.clear()
    entry = asyncio.ensure_future(batch.submit("k", 1))
    for _ in range(10):
        await asyncio.sleep(0)
    assert pg_batch._batch().sending  # the flush is in its insert
    pg_batch._batch().task.cancel()  # a flush that ends in error, as a bug would
    await pg_batch.drain(timeout_s=5)
    with pytest.raises(pg_lease.PublicationPending):
        await entry


async def test_a_commit_whose_lease_ran_out_in_the_queue_is_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past its lease a commit is the delivery worker's: nothing is sent for it."""
    batch = _Batching(monkeypatch)
    with pytest.raises(pg_lease.PublicationPending):
        await batch.submit("late", 1, lease_s=-1)
    assert batch.memory == [] and batch.landed == []


async def test_a_writer_that_does_not_wait_answers_before_the_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """memory.set's hand-off returns at its commit and the flush follows; a waiting
    writer that is cancelled leaves its commit queued, sent all the same."""
    batch = _Batching(monkeypatch)
    batch.inserting.clear()
    await batch.submit("k1", 1, wait=False)
    waiting = asyncio.ensure_future(batch.submit("k2", 2))
    await asyncio.sleep(0)
    waiting.cancel()
    assert batch.memory == []
    batch.inserting.set()
    await pg_batch.drain(timeout_s=5)
    assert batch.memory == [["k1"], ["k2"]]  # k2 queued while k1's flush ran


async def test_a_drain_that_runs_out_leaves_the_queue_to_the_worker(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """At shutdown the drain waits no longer than it is given: what it did not send is the
    delivery worker's once its lease runs out."""
    batch = _Batching(monkeypatch)
    batch.inserting.clear()
    await batch.submit("k1", 1, wait=False)
    with caplog.at_level(logging.WARNING, logger=pg_batch.__name__):
        await pg_batch.drain(timeout_s=0.01)
    assert "left to the delivery worker" in caplog.text and batch.memory == []
    batch.inserting.set()
    await pg_batch.drain(timeout_s=5)
    assert batch.memory == [["k1"]]


async def test_a_waiting_writer_answers_by_its_lease_behind_a_slow_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flush that hangs holds nobody past their lease: a writer that waits answers
    PublicationPending when its lease runs out, and the flush skips its commit."""
    batch = _Batching(monkeypatch)
    batch.inserting.clear()
    await batch.submit("k1", 1, wait=False)
    await asyncio.sleep(0)  # k1's flush starts, and hangs on ClickHouse
    with pytest.raises(pg_lease.PublicationPending):
        await batch.submit("k2", 2, lease_s=0.05)
    batch.clock.advance(1)
    batch.inserting.set()
    await pg_batch.drain(timeout_s=5)
    assert batch.memory == [["k1"]]


async def test_a_flush_runs_in_a_context_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flush sends every org's commits: it carries no request's query tag, quota key
    or budget, only its own tag."""
    from server.core import ch

    batch = _Batching(monkeypatch)
    seen: list[str] = []

    async def _audits(rows: Any, *, ch: Any, retried: bool) -> None:
        seen.append(current_query_tag())

    from server.core.ch import current_query_tag

    monkeypatch.setattr(pg_publish, "insert_audit_rows", _audits)
    with ch.query_tag("memory.set"):
        await batch.submit("k1", 1)
    assert seen == ["pg_batch.flush"]


@pytest.mark.parametrize(
    ("first", "relations", "sensitivity", "answers"),
    [
        (True, [], "normal", True),
        (False, [], "normal", False),
        (True, [("a", "b", "c")], "normal", False),
        (True, [], "high", False),
    ],
)
def test_only_a_first_write_with_no_relations_answers_before_its_flush(
    first: bool, relations: list[Any], sensitivity: str, answers: bool
) -> None:
    """A revision fences its edges on its new row, a write with relations its facts and a
    'high' row its fence: each reads ClickHouse, so they wait for their flush; a first
    write's only graph work runs beside its answer."""
    from server.memory.app import set_commit

    w = SimpleNamespace(
        had_existing_row=not first,
        input=SimpleNamespace(relations=relations),
        memory_row={"sensitivity": sensitivity},
    )
    assert set_commit._answers_at_commit(w) is answers  # type: ignore[arg-type]


async def test_a_set_whose_flush_goes_pending_runs_what_follows_and_answers_so_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed revision whose flush outlasts its lease is announced and records the
    pending answer, so its follow-ups (supersession, relations, audit) still run and the
    caller hears PUBLICATION_PENDING once they have."""
    from server.memory.app import set_commit

    pending = pg_lease.PublicationPending()
    waited: list[bool] = []
    announced: list[str] = []

    async def _commit(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace()

    async def _publish(*_a: Any, wait: bool, **_k: Any) -> None:
        waited.append(wait)
        raise pending

    async def _announce(_w: Any) -> None:
        announced.append("announced")

    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(set_commit, "commit_set_rows", _commit)
    monkeypatch.setattr(pg_publish, "publish", _publish)
    monkeypatch.setattr(set_commit, "_announce_set", _announce)
    row = {"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"}
    w: Any = SimpleNamespace(
        memory_row=row | {"memory_key": "k", "version": 7, "sensitivity": "normal"},
        policy={},
        had_existing_row=True,
        input=SimpleNamespace(relations=[]),
        version=7,
        pending=None,
    )
    assert await set_commit._land_plain_row(w, None, high=False) == 7
    assert (waited, announced, w.pending) == ([True], ["announced"], pending)

    followed: list[str] = []

    async def _follow_ups() -> str:
        followed.append("ran")
        return "answer"

    with pytest.raises(pg_lease.PublicationPending) as raised:
        await set_commit.finished(w, _follow_ups())
    assert raised.value is pending and followed == ["ran"]


async def test_a_bulk_set_whose_flush_goes_pending_is_announced_and_records_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The batch's follow-ups run on a pending publication: `_land_batch` keeps it for
    `_bulk_set_once` to raise last."""
    from server.memory.app import after_write, bulk_set

    pending = pg_lease.PublicationPending()
    announced: list[str] = []

    async def _publish(*_a: Any, **_k: Any) -> None:
        raise pending

    async def _announce(*_a: Any, **_k: Any) -> None:
        announced.append("announced")

    monkeypatch.setattr(pg_publish, "publish", _publish)
    monkeypatch.setattr(bulk_set, "_fence_and_announce", _announce)
    monkeypatch.setattr(after_write, "index_late_rows", lambda *_a, **_k: None)
    state: Any = SimpleNamespace(embedding_rows=[], unpublished=None)
    await bulk_set._land_batch(None, state, SimpleNamespace(), {})
    assert (state.unpublished, announced) == (pending, ["announced"])


async def test_a_refused_flush_hands_over_what_it_will_not_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ClickHouse refuses a row of the flush, the commit it refuses and the stale
    document of one it published are handed to the delivery worker at once: no search
    waits out their writers' leases."""
    batch = _Batching(monkeypatch)
    batch.refuse_memory = {"bad"}
    batch.moved = {"stale"}
    done = await asyncio.gather(
        batch.submit("stale", 1), batch.submit("bad", 2), return_exceptions=True
    )
    assert done[0] is None and isinstance(done[1], pg_lease.PublicationPending)
    assert sorted(batch.handed_over) == [("o", "n", 1), ("o", "n", 2)]


async def test_a_flush_partly_unanswered_is_left_whole_and_handed_over_to_nobody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A documents insert that timed out may still land, whatever else was refused: the
    group is not sent again one by one, and no lease is ended under it."""
    batch = _Batching(monkeypatch)
    batch.refuse_memory = {"bad"}
    batch.refuse_documents = TimeoutError("the class deadline")
    done = await asyncio.gather(
        batch.submit("good", 1), batch.submit("bad", 2), return_exceptions=True
    )
    assert all(isinstance(d, pg_lease.PublicationPending) for d in done)
    assert batch.memory == [] and batch.handed_over == []


def test_rows_spanning_more_partitions_than_a_block_takes_are_split() -> None:
    """loom.memory is partitioned by month of created_at: no insert block spans more
    months than ClickHouse takes in one."""
    cap = MAX_PARTITIONS_PER_INSERT
    rows = [
        _row("o", "n", f"k{i}", month=f"{2000 + i // 12}-{i % 12 + 1:02d}") for i in range(cap + 5)
    ]
    rows.append(_row("o", "n", "again", month="2000-01"))
    blocks = partition_blocks(rows, created_month)
    assert [len({created_month(r) for r in b}) for b in blocks] == [cap, 5]
    assert sum(len(b) for b in blocks) == len(rows)
    assert partition_blocks(rows[:3], created_month) == [rows[:3]]
