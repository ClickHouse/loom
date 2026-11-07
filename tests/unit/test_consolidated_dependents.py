"""memory.delete and summaries built from the deleted row: a dependent whose
sources are all gone goes too; one
with surviving sources is downgraded to `conflicted` with a stale-source tag,
on both tables."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.core import rmt_carry
from server.memory import store as memory_repo
from server.memory.store import lifecycle, pg_memory, pg_publish
from server.memory.store.rows import identity_key


class _CH:
    def __init__(self) -> None:
        self.all_answers: list[list[dict[str, Any]]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params or {}))
        return self.all_answers.pop(0) if self.all_answers else []


def _head(
    mid: str, key: str, srcs: list[str], version: int = 1, **fields: Any
) -> memory_repo.MemoryRow:
    fields.setdefault("content_sha256", "a" * 64)
    return memory_repo.MemoryRow(
        memory_id=mid,
        org="o",
        namespace="ns",
        agent="a",
        user_id="-",
        session_id="-",
        memory_key=key,
        memory_type="semantic",
        value="v",
        confidence=1.0,
        version=version,
        pinned=False,
        created_at=datetime(2026, 1, 1),
        expires_at=None,
        tags=["consolidated", *[f"src:{s}" for s in srcs]],
        **fields,
    )


def _scan(*heads: memory_repo.MemoryRow) -> list[dict[str, Any]]:
    """Either store's answer to the dependents read: each summary's key and tags."""
    cols = ("org", "namespace", "agent", "user_id", "session_id", "memory_key", "tags")
    return [{c: getattr(h, c) for c in cols} for h in heads]


class _Written:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.over: list[memory_repo.MemoryRow] = []  # the head each row was committed over
        self.documented: list[bool] = []
        self.reemits: list[tuple[str, dict[str, Any]]] = []
        self.of_content: dict[str, str] = {}
        self.retired: list[tuple[str, int]] = []  # (key, version), retired with no text


def _wire(monkeypatch: pytest.MonkeyPatch, *over: memory_repo.MemoryRow) -> _Written:
    """Each summary written over the head `over` holds for its key (the write's own
    strong read), its vectors re-emitted within the commit's follow-up."""
    from server.core.ch import sequential_reads_active

    out = _Written()
    following: list[bool] = []

    async def _by_keys(keys, *, ch=None, sequential=False, **_):
        assert sequential, "the head written over is read strongly"
        return {identity_key(h): h for h in over if identity_key(h) in keys}

    async def _insert(rows, read, *, ch=None, then=None, documented=None, on_commit=None, **_):
        out.rows.extend(rows)
        out.over.extend(read)
        out.documented.extend(documented or [])
        if on_commit is not None:
            on_commit()
        following.append(True)
        try:
            return 0 if then is None else (await then() or 0)
        finally:
            following.pop()

    async def _reemit(items, *, ch, of_content=None):
        assert following, "the vectors are copied in the commit's follow-up"
        assert sequential_reads_active(), "the vectors are read strongly"
        out.reemits.extend((key[-1], delta) for key, delta in items)
        out.of_content.update({key[-1]: sha for key, sha in (of_content or {}).items()})

    async def _retire(items, *, ch, bare=False):
        assert following and bare, "a tombstone retires the vectors with no text, leased"
        assert sequential_reads_active(), "the vectors are read strongly"
        out.retired.extend((key[-1], version) for key, version in items)
        return len(items)

    monkeypatch.setattr(lifecycle, "memory_rows_by_identity_keys", _by_keys)
    monkeypatch.setattr(lifecycle, "insert_versions_over_heads", _insert)
    monkeypatch.setattr(lifecycle, "reemit_embedding_rows_for_keys", _reemit)
    monkeypatch.setattr(lifecycle, "retire_vectors_for_keys", _retire)
    return out


@pytest.mark.asyncio
async def test_dependents_are_found_by_the_src_tag_with_their_sources() -> None:
    ch = _CH()
    summary = _head("m1", "s:a", ["k1", "k2"])
    ch.all_answers.append(_scan(summary))
    deps = await memory_repo.consolidated_dependents_of("o", "ns", "k1", ch=ch)
    assert deps == {identity_key(summary): frozenset({"k1", "k2"})}
    [(sql, params)] = ch.calls
    assert "SELECT org, namespace, agent, user_id, session_id, memory_key, tags" in sql
    assert "has(tags, 'consolidated') AND has(tags, {srctag:String})" in sql
    assert params["srctag"] == "src:k1" and "select_sequential_consistency = 1" in sql


@pytest.mark.asyncio
async def test_delete_tombstones_orphaned_summaries_and_downgrades_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    only_k1, k1_k2 = _head("m1", "s:only_k1", ["k1"]), _head("m2", "s:k1_k2", ["k1", "k2"])
    ch = _CH()
    ch.all_answers.append(_scan(only_k1, k1_k2))
    ch.all_answers.append([{"memory_key": "k2"}])  # the live sources: k2 only
    out = _wire(monkeypatch, only_k1, k1_k2)

    assert await memory_repo.downgrade_consolidated_dependents("o", "ns", "k1", ch=ch) == (1, 1)
    tomb, down = out.rows
    assert tomb["memory_id"] == "m1" and tomb["tombstoned_at"] is not None
    assert down["memory_id"] == "m2" and down["status"] == "conflicted"
    assert down["tags"][-1] == "stale-source:k1" and "src:k2" in down["tags"]
    live_sql, live_params = ch.calls[1]  # one count of the sources, all summaries'
    assert live_params["keys"] == ["k1", "k2"] and "select_sequential_consistency = 1" in live_sql
    # both tables: the embeddings table (what recall reads) takes each change at its row's
    # version, the tombstone with no text; under Postgres only the downgrade owes its
    # search document
    assert out.retired == [("s:only_k1", tomb["version"])]
    assert out.reemits == [
        (
            "s:k1_k2",
            {
                "invalid_at": None,
                "status": "conflicted",
                "tags": down["tags"],
                "version": down["version"],
            },
        ),
    ]
    assert out.documented == [False, True]
    # the downgrade copies only the vectors of its head's own hash
    assert out.of_content == {"s:k1_k2": "a" * 64}


@pytest.mark.asyncio
async def test_no_dependents_is_a_noop() -> None:
    ch = _CH()
    assert await memory_repo.downgrade_consolidated_dependents("o", "ns", "k1", ch=ch) == (0, 0)
    assert len(ch.calls) == 1


@pytest.mark.parametrize("orphaned", [True, False])
@pytest.mark.parametrize(
    ("over", "lands"),
    [
        (_head("m1", "s:x", ["k1", "k9"], version=250), True),  # clock ahead: above it
        (_head("m1", "s:x", ["k1", "k9"], version=400), True),  # a copy since: above it
        (_head("m1", "s:x", ["k3"], version=400), False),  # rebuilt from other sources
    ],
)
@pytest.mark.asyncio
async def test_a_summary_is_written_while_it_cites_the_sources_judged(
    monkeypatch: pytest.MonkeyPatch, over: memory_repo.MemoryRow, lands: bool, orphaned: bool
) -> None:
    ch = _CH()
    ch.all_answers.append(_scan(_head("m1", "s:x", ["k1", "k9"], version=250)))
    ch.all_answers.append([] if orphaned else [{"memory_key": "k9"}])
    out = _wire(monkeypatch, over)
    monkeypatch.setattr("server.memory.store.rows.next_version", lambda: 100)
    counts = await memory_repo.downgrade_consolidated_dependents("o", "ns", "k1", ch=ch)
    expected = [over.version + 2] if lands else []
    assert counts == ((len(expected), 0) if orphaned else (0, len(expected)))
    assert [w["version"] for w in out.rows] == expected
    # committed over the head it was built from, so a change since refuses it
    assert [h.version for h in out.over] == ([over.version] if lands else [])
    copied = [v for _, v in out.retired] if orphaned else [d["version"] for _, d in out.reemits]
    assert copied == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("orphaned", [False, True], ids=["downgraded", "tombstoned"])
async def test_a_redacted_summary_is_written_with_no_vectors_copied(
    monkeypatch: pytest.MonkeyPatch, orphaned: bool
) -> None:
    """A head redacted in place keeps no vectors: the redaction's purge is the only
    writer of them, so no copy lands above it."""
    summary = _head("m1", "s:x", ["k1", "k2"], redacted_at=datetime(2026, 1, 2))
    ch = _CH()
    ch.all_answers.extend([_scan(summary), [] if orphaned else [{"memory_key": "k2"}]])
    out = _wire(monkeypatch, summary)
    counts = await memory_repo.downgrade_consolidated_dependents("o", "ns", "k1", ch=ch)
    assert counts == ((1, 0) if orphaned else (0, 1))
    assert out.reemits == out.retired == [] and out.documented == [False]


@pytest.mark.asyncio
async def test_a_summary_written_before_hashes_is_downgraded_with_no_vectors_copied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A head with no content hash cannot tell its own vectors from an older version's,
    so it copies none (as a reject keeps none); its document stays owed."""
    summary = _head("m1", "s:x", ["k1", "k2"], content_sha256="0")
    ch = _CH()
    ch.all_answers.extend([_scan(summary), [{"memory_key": "k2"}]])
    out = _wire(monkeypatch, summary)
    assert await memory_repo.downgrade_consolidated_dependents("o", "ns", "k1", ch=ch) == (0, 1)
    assert out.rows[0]["status"] == "conflicted"
    assert out.reemits == out.retired == [] and out.of_content == {}
    assert out.documented == [True]


@pytest.mark.asyncio
async def test_a_failed_batch_counts_none_and_the_other_batch_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    only_k1, k1_k2 = _head("m1", "s:only_k1", ["k1"]), _head("m2", "s:k1_k2", ["k1", "k2"])
    ch = _CH()
    ch.all_answers.append(_scan(only_k1, k1_k2))
    ch.all_answers.append([{"memory_key": "k2"}])
    _wire(monkeypatch, only_k1, k1_k2)
    real = lifecycle.insert_versions_over_heads

    async def _first_fails(rows, read, **kw):
        if rows[0]["memory_id"] == "m1":
            raise RuntimeError("Failing row contains (secret)")
        return await real(rows, read, **kw)

    monkeypatch.setattr(lifecycle, "insert_versions_over_heads", _first_fails)
    assert await memory_repo.downgrade_consolidated_dependents("o", "ns", "k1", ch=ch) == (0, 1)


@pytest.mark.asyncio
async def test_a_failed_vector_copy_still_counts_the_rows_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = _head("m1", "s:x", ["k1", "k2"])
    ch = _CH()
    ch.all_answers.extend([_scan(summary), [{"memory_key": "k2"}]])
    out = _wire(monkeypatch, summary)

    async def _refused(items, *, ch, of_content=None):
        raise RuntimeError("ClickHouse refused the copy")

    monkeypatch.setattr(lifecycle, "reemit_embedding_rows_for_keys", _refused)
    assert await memory_repo.downgrade_consolidated_dependents("o", "ns", "k1", ch=ch) == (0, 1)
    assert out.rows[0]["status"] == "conflicted" and out.documented == [True]


@pytest.mark.asyncio
async def test_on_clickhouse_a_failed_vector_copy_still_counts_the_row_inserted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is in before the copy runs: a refused copy is logged, not a batch lost."""
    summary = _head("m1", "s:x", ["k1", "k2"])
    inserted: list[dict[str, Any]] = []

    async def _by_keys(keys, **_):
        return {identity_key(summary): summary}

    async def _insert_rows(rows, *, ch=None):
        inserted.extend(rows)

    async def _refused(items, *, ch, of_content=None):
        raise RuntimeError("ClickHouse refused the copy")

    monkeypatch.setattr(lifecycle, "memory_rows_by_identity_keys", _by_keys)
    monkeypatch.setattr(lifecycle, "insert_memory_versions", _insert_rows)
    monkeypatch.setattr(lifecycle, "reemit_embedding_rows_for_keys", _refused)
    ch = _CH()
    ch.all_answers.extend([_scan(summary), [{"memory_key": "k2"}]])
    assert await memory_repo.downgrade_consolidated_dependents("o", "ns", "k1", ch=ch) == (0, 1)
    assert [r["status"] for r in inserted] == ["conflicted"]


@pytest.mark.asyncio
async def test_under_postgres_the_summaries_and_their_sources_are_read_on_the_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[Any] = []
    held = _head("m1", "s:x", ["k1", "k2"])

    async def _heads(org, ns, key):
        asked.append(("heads", org, ns, key))
        return _scan(held)

    async def _live(org, ns, keys):
        asked.append(("live", org, ns, list(keys)))
        return {"k2"}

    monkeypatch.setattr("server.core.pg.enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "consolidated_heads", _heads)
    monkeypatch.setattr(pg_memory, "live_keys", _live)
    out = _wire(monkeypatch, held)
    ch = _CH()  # answers nothing: ClickHouse is not read
    assert await memory_repo.downgrade_consolidated_dependents("o", "ns", "k1", ch=ch) == (0, 1)
    assert asked == [("heads", "o", "ns", "k1"), ("live", "o", "ns", ["k1", "k2"])]
    assert ch.calls == [] and out.rows[0]["status"] == "conflicted"


# What a downgrade's commit owes: its row, its audit row and its search document.
_OWED = pg_memory.MEMORY_ROW | pg_memory.AUDIT_ROW | pg_memory.SEARCH_DOC

# What ClickHouse answers a held-documents read: (committed rows, ids asked) -> ids.
_Held = Callable[[list[dict[str, Any]], list[Any]], set[Any]]


@dataclass
class _Postgres:
    """What a commit under the Postgres authority did: the steps in order, the documents
    settled, the rows committed and the targets each owes."""

    order: list[str] = field(default_factory=list)
    settled: list[Any] = field(default_factory=list)
    committed: list[dict[str, Any]] = field(default_factory=list)
    targets: list[int] = field(default_factory=list)


def _wire_postgres(
    monkeypatch: pytest.MonkeyPatch,
    *heads: memory_repo.MemoryRow,
    held: _Held,
    refused: bool = False,
) -> _Postgres:
    """The summaries `heads`, each keeping source k2, written through the real commit
    path with Postgres faked: `held(committed rows, asked ids)` is what ClickHouse holds,
    and `refused` makes the vector copy fail."""
    out = _Postgres()

    async def _heads(org, ns, key):
        return _scan(*heads)

    async def _live(org, ns, keys):
        return {"k2"}

    async def _by_keys(keys, **_):
        return {identity_key(h): h for h in heads}

    async def _commit(rows, *, targets, **_):
        out.order.append("commit")
        out.targets.extend(targets)
        out.committed.extend(rows)
        seqs = [("o", "ns", i) for i in range(len(rows))]
        return SimpleNamespace(rows=rows, seqs=seqs, deadline=time.monotonic() + 60)

    async def _reemit(items, *, ch, of_content=None):
        out.order.append("leased" if rmt_carry.sent_under_lease() else "unleased")
        if refused:
            raise RuntimeError("ClickHouse refused the copy")

    async def _held(org, ns, ids, *, ch):
        return held(out.committed, ids)

    async def _complete(seqs, done, **_):
        assert done == pg_memory.SEARCH_DOC
        out.settled.extend(seqs)

    async def _publish(*_, **__):
        out.order.append("publish")

    monkeypatch.setattr("server.core.pg.enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "consolidated_heads", _heads)
    monkeypatch.setattr(pg_memory, "live_keys", _live)
    monkeypatch.setattr(pg_memory, "complete", _complete)
    monkeypatch.setattr(lifecycle, "memory_rows_by_identity_keys", _by_keys)
    monkeypatch.setattr(lifecycle, "check_memory_rows", lambda rows: None)
    monkeypatch.setattr(lifecycle, "reemit_embedding_rows_for_keys", _reemit)
    monkeypatch.setattr(pg_publish, "commit", _commit)
    monkeypatch.setattr(pg_publish, "publish", _publish)
    monkeypatch.setattr(pg_publish, "held_documents", _held)
    return out


def _holding(*keys: str) -> _Held:
    """ClickHouse holds the document of each committed row of these keys, at its version."""
    return lambda rows, ids: (
        {pg_publish.document_id(r) for r in rows if r["memory_key"] in keys} & set(ids)
    )


def _unanswered(rows: list[dict[str, Any]], ids: list[Any]) -> set[Any]:
    raise ConnectionError("ClickHouse did not answer")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refused", "held", "settled"),
    [
        (False, _holding("s:x"), [("o", "ns", 0)]),
        (False, _holding(), []),
        (True, _holding("s:x"), []),
        (False, _unanswered, []),
    ],
    ids=["copied", "nothing-to-copy", "copy-refused", "held-read-fails"],
)
async def test_under_postgres_the_vectors_are_copied_under_the_commits_lease(
    monkeypatch: pytest.MonkeyPatch, refused: bool, held: _Held, settled: list[Any]
) -> None:
    """The copy runs under the commit's lease, before the publish clears its delivery, so
    a redaction waits it out; the downgrade's document is settled only once ClickHouse
    holds it at the row's version, else it stays owed for the delivery worker."""
    pg = _wire_postgres(monkeypatch, _head("m1", "s:x", ["k1", "k2"]), held=held, refused=refused)
    assert await memory_repo.downgrade_consolidated_dependents("o", "ns", "k1", ch=_CH()) == (0, 1)
    assert pg.order == ["commit", "leased", "publish"]
    assert pg.settled == settled
    assert pg.targets == [_OWED]


@pytest.mark.asyncio
async def test_under_postgres_only_the_documents_clickhouse_holds_are_settled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Of two downgrades, the one whose copy ClickHouse holds at its row's version is
    settled; the other stays owed even if the held read named it at an older version."""
    copied, missed = _head("m1", "s:x", ["k1", "k2"]), _head("m2", "s:y", ["k1", "k2"])

    def held(rows: list[dict[str, Any]], ids: list[Any]) -> set[Any]:
        older = {pg_publish.document_id({**r, "version": int(r["version"]) - 1}) for r in rows}
        return _holding("s:x")(rows, ids) | older

    pg = _wire_postgres(monkeypatch, copied, missed, held=held)
    assert await memory_repo.downgrade_consolidated_dependents("o", "ns", "k1", ch=_CH()) == (0, 2)
    assert pg.settled == [("o", "ns", 0)]
    assert pg.targets == [_OWED, _OWED]


@pytest.mark.asyncio
async def test_a_batch_whose_head_moved_is_judged_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under Postgres another source's delete judging the summary at once moves its head:
    the batch is refused whole, read again, and the summary judged on the new read."""
    summary = _head("m1", "s:x", ["k1"])
    ch = _CH()
    ch.all_answers.extend([_scan(summary), [], _scan(summary), []])  # read twice, no live source
    _wire(monkeypatch, summary)
    real = lifecycle.insert_versions_over_heads
    tries: list[int] = []

    async def _moved_once(rows, read, **kw):
        tries.append(1)
        if len(tries) == 1:
            raise pg_memory.AdmissionContendedError("the memory changed while it was read")
        return await real(rows, read, **kw)

    monkeypatch.setattr(lifecycle, "insert_versions_over_heads", _moved_once)
    assert await memory_repo.downgrade_consolidated_dependents("o", "ns", "k1", ch=ch) == (1, 0)
    assert len(tries) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("postgres", [False, True], ids=["clickhouse", "postgres"])
async def test_a_summary_held_under_two_keys_is_written_under_each(
    monkeypatch: pytest.MonkeyPatch, postgres: bool
) -> None:
    """A namespace restored into itself holds one memory id under two keys: each key's
    head is written and takes its own vectors."""
    one, other = _head("m1", "s:x", ["k1", "k2"]), _head("m1", "s:y", ["k1", "k2"])
    ch = _CH()
    if postgres:

        async def _heads(org, ns, key):
            return _scan(one, other)

        async def _live(org, ns, keys):
            return {"k2"}

        monkeypatch.setattr("server.core.pg.enabled", lambda: True)
        monkeypatch.setattr(pg_memory, "consolidated_heads", _heads)
        monkeypatch.setattr(pg_memory, "live_keys", _live)
    else:
        ch.all_answers.extend([_scan(one, other), [{"memory_key": "k2"}]])
    out = _wire(monkeypatch, one, other)
    assert await memory_repo.downgrade_consolidated_dependents("o", "ns", "k1", ch=ch) == (0, 2)
    assert [(w["memory_key"], w["status"]) for w in out.rows] == [
        ("s:x", "conflicted"),
        ("s:y", "conflicted"),
    ]
    assert [key for key, _ in out.reemits] == ["s:x", "s:y"]
