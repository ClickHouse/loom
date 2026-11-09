"""Rows go to ClickHouse only once the synchronous standbys hold them.

A commit whose standby wait was cancelled is visible on the primary alone; a failover
loses it. A ClickHouse send fed by a Postgres read must therefore send only what the
caller committed or claimed, or read durably first (`pg.durable_transaction`,
`pg_memory._read`). These tests pin who reads what, and the gate's placement.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from typing import Any

import pytest

from server.core import pg
from server.memory.app import pg_delivery
from server.memory.store import pg_batch, pg_memory
from server.settings import settings

ROOT = pathlib.Path(__file__).resolve().parents[2]

# The functions of the two journal stores that write, claim, complete, sweep or only
# encode: everything else is a read.
_NOT_READS = {
    "pg_memory": {
        "encode_row", "decode_row", "encode_audit", "decode_audit", "protection",
        "heads_unchanged", "unchanged", "policy_scopes", "policy_part", "deleted_decision",
        "embedded", "newer_copy", "commit_versions", "complete", "complete_each", "hand_over",
        "claim_due", "defer_audits", "release", "renew", "sweep_settled", "claim_outbox",
        "drop_outbox", "fail_outbox", "sweep_erasure_evidence", "record_failure", "redact",
        "fold_usage",
    },
    "pg_documents": {
        "refuse_unstorable_keys", "encode_row", "decode_row", "commit", "complete",
        "complete_own", "drop_unbacked", "claim_due", "failed", "release", "drop_version",
        "sweep_erasure_evidence", "sweep_settled",
    },
}  # fmt: skip

_STORES = {
    "pg_memory": "server/memory/store/pg_memory.py",
    "pg_documents": "server/knowledge/store/pg_documents.py",
}

# Every module that calls a read of the journal stores, and why what it reads reaches
# ClickHouse only durably. A new caller is classified here before it ships.
_CALLERS = {
    "server/health.py": "counts for the health report; sends nothing",
    "server/knowledge/app/inspect.py": "a report to the caller; sends nothing",
    "server/knowledge/store/blobs.py": "keeps originals a commit owes; deletes none of them",
    "server/knowledge/store/documents.py": "reads beside the writer's own commit; "
    "the worker's sends are its durable claims",
    "server/memory/app/blacklist_sweep.py": "tombstones through its own commit",
    "server/memory/app/bulk_set.py": "decides a write, which commits",
    "server/memory/app/conflicts.py": "refuses a write before its commit",
    "server/memory/app/curation.py": "scrubs delete; a row kept for a head a failover "
    "loses is residue the reconcile removes",
    "server/memory/app/high_fence.py": "the outcome of the caller's own commit",
    "server/memory/app/key_supersession.py": "decides a write, which commits",
    "server/memory/app/namespace_rollback.py": "plans a rollback, which commits",
    "server/memory/app/period_digest.py": "decides a write, which commits",
    "server/memory/app/pg_admission.py": "the barrier and the admission; a write commits",
    "server/memory/app/pg_delivery.py": "a head newer than the claim is read again durably",
    "server/memory/app/restore.py": "the outcome of the caller's own commit",
    "server/memory/app/search/pg_validation.py": "filters what a search serves; sends nothing",
    "server/memory/app/sensitivity_sweep.py": "closes facts only of a version ClickHouse holds",
    "server/memory/app/snapshot_rows.py": "a snapshot to the caller; a restore commits",
    "server/memory/app/update.py": "a write's guard; the write commits",
    "server/memory/app/write.py": "a write's guard; the write commits",
    "server/memory/store/embeddings.py": "filters rows ClickHouse already holds",
    "server/memory/store/history.py": "history to the caller; sends nothing",
    "server/memory/store/lifecycle.py": "decides a delete, which commits",
    "server/memory/store/pg_batch.py": "a newer head copy is read again durably",
    "server/memory/store/pg_publish.py": "live decision writers are read durably",
    "server/memory/store/reads.py": "point reads to the caller; send nothing",
    "server/memory/store/records.py": "live decision writers are read durably",
    "server/memory/store/redaction.py": "decides a redaction, which commits",
}


def _reads() -> dict[str, set[str]]:
    out = {}
    for alias, path in _STORES.items():
        tree = ast.parse((ROOT / path).read_text())
        public = {
            n.name
            for n in tree.body
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and not n.name.startswith("_")
        }
        assert _NOT_READS[alias] <= public, f"{alias}: listed but gone"
        out[alias] = public - _NOT_READS[alias]
    return out


def test_every_caller_of_a_journal_read_is_classified() -> None:
    reads = _reads()
    modules = {f"{path[:-3].replace('/', '.')}": alias for alias, path in _STORES.items()}
    callers = set()
    for path in (ROOT / "server").rglob("*.py"):
        rel = str(path.relative_to(ROOT))
        if rel in _STORES.values():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            # The scan sees `pg_memory.<read>`: every other way to reach a read is refused.
            if isinstance(node, ast.ImportFrom):
                assert not (node.level and node.module and node.module.split(".")[-1] in _STORES), (
                    f"{rel}: import the store absolutely"
                )
                if node.module in modules:
                    named = {a.name for a in node.names} & reads[modules[node.module]]
                    assert not named, f"{rel}: call {sorted(named)} through the module"
                for a in node.names:
                    assert a.name not in _STORES or a.asname in (None, a.name), f"{rel}: {a.asname}"
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name not in modules, f"{rel}: import {a.name} with from"
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.attr in reads.get(node.value.id, ())
            ):
                callers.add(rel)
    assert callers - _CALLERS.keys() == set(), "classify the new caller in _CALLERS"
    assert _CALLERS.keys() - callers == set(), "a classified caller reads no more: drop it"


class _Conn:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.log.append("read")
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        assert "pg_logical_emit_message(true" in sql
        self.log.append("message")
        return "SELECT 1"


def _fake_transaction(monkeypatch: pytest.MonkeyPatch, log: list[str]) -> None:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def transaction():  # type: ignore[no-untyped-def]
        log.append("begin")
        yield _Conn(log)
        log.append("commit")

    monkeypatch.setattr(pg, "transaction", transaction)


def test_durable_transaction_writes_its_message_last_before_the_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    _fake_transaction(monkeypatch, log)

    async def run() -> None:
        async with pg.durable_transaction() as conn:
            await conn.fetch("SELECT 1")

    asyncio.run(run())
    assert log == ["begin", "read", "message", "commit"]


def _claim(version: int, sha: str = "a") -> pg_memory.Claimed:
    row = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "u",
        "session_id": "s",
        "memory_key": "k",
        "version": version,
        "content_sha256": sha,
        "memory_id": "m",
    }
    return pg_memory.Claimed("o", "n", 1, 2, 1, 0, row, {}, deadline=1e18)


def _head(version: int, sha: str = "a" * 64) -> dict[str, Any]:
    return {**_claim(version, sha).row, "embedding_status": "embedded"}


@pytest.mark.parametrize(
    "first, durable_reads",
    [
        pytest.param(lambda c: c.row, 0, id="own revision: no durable read"),
        pytest.param(lambda c: _head(9), 1, id="newer copy: read again durably"),
        pytest.param(
            lambda c: None, 1, id="no head (an erasure): settled only from a durable read"
        ),
        pytest.param(
            lambda c: {**c.row, "redacted_at": "2026-01-01 00:00:00"},
            1,
            id="redacted in place: settled only from a durable read",
        ),
        pytest.param(
            lambda c: {**_head(9), "tombstoned_at": "2026-01-01 00:00:00"},
            1,
            id="newer delete: settled unsent only from a durable read",
        ),
    ],
)
def test_still_heads_reads_durably_only_for_another_commit(
    monkeypatch: pytest.MonkeyPatch, first: Any, durable_reads: int
) -> None:
    claim = _claim(5, "a" * 64)
    calls: list[bool] = []

    async def heads_by_keys(keys, *, include_tombstoned=False, durable=False):  # type: ignore[no-untyped-def]
        calls.append(durable)
        head = first(claim)
        return {} if head is None else {pg_memory.identity_key(claim.row): head}

    monkeypatch.setattr(pg_memory, "heads_by_keys", heads_by_keys)
    monkeypatch.setattr(pg_memory, "embedded", lambda row: True)
    asyncio.run(pg_delivery._still_heads([claim], deadline=1e18))
    assert calls.count(True) == durable_reads


@pytest.mark.parametrize(
    "plain, again, built",
    [
        pytest.param("delete", "own", [5], id="a delete only on the primary: the claim's own"),
        pytest.param("copy", "delete", [], id="a copy deleted since: nothing built"),
        pytest.param("delete", "copy", [9], id="a delete read again as a copy: built onto it"),
    ],
)
def test_still_heads_decides_from_the_durable_read(
    monkeypatch: pytest.MonkeyPatch, plain: str, again: str, built: list[int]
) -> None:
    claim = _claim(5, "a" * 64)
    heads = {
        "own": claim.row,
        "copy": _head(9),
        "delete": {**_head(9), "tombstoned_at": "2026-01-01 00:00:00"},
    }

    async def heads_by_keys(keys, *, include_tombstoned=False, durable=False):  # type: ignore[no-untyped-def]
        return {pg_memory.identity_key(claim.row): heads[again if durable else plain]}

    monkeypatch.setattr(pg_memory, "heads_by_keys", heads_by_keys)
    monkeypatch.setattr(pg_memory, "embedded", lambda row: True)
    out = asyncio.run(pg_delivery._still_heads([claim], deadline=1e18))
    assert [int(c.row["version"]) for c in out] == built


class _Clock:
    """`pg_lease`'s clock: 0 until the durable read, then past any lease."""

    now = 0.0

    def monotonic(self) -> float:
        return self.now


def _lease_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    from server.core import pg_lease

    clock = _Clock()
    monkeypatch.setattr(pg_lease, "time", clock)
    return clock


def test_still_heads_gives_back_claims_a_standby_wait_left_no_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim(5, "a" * 64)
    clock = _lease_clock(monkeypatch)

    async def heads_by_keys(keys, *, include_tombstoned=False, durable=False):  # type: ignore[no-untyped-def]
        if durable:
            clock.now = 100.0  # the standby wait outlasts the lease
        return {pg_memory.identity_key(claim.row): _head(9)}

    monkeypatch.setattr(pg_memory, "heads_by_keys", heads_by_keys)
    monkeypatch.setattr(pg_memory, "embedded", lambda row: True)
    # An outage, which the pass releases uncounted, not an attempt.
    with pytest.raises(pg.PostgresUnavailableError):
        asyncio.run(pg_delivery._still_heads([claim], deadline=10.0))


def test_flush_decides_again_from_the_durable_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A copy the first read showed and the durable read no longer holds (it was on the
    primary alone, or deleted since) sends nothing; its delivery goes to `stale`."""
    doc = {**_claim(5, "a" * 64).row}
    reads: list[bool] = []

    async def heads_owing(keys, *, durable=False):  # type: ignore[no-untyped-def]
        reads.append(durable)
        return {} if durable else {pg_memory.identity_key(doc): (_head(9), 0)}

    class _Committed:
        seqs = [("o", "n", 1)]
        rows = [doc]

    class _Entry:
        documents_sent = False
        documents = [doc]
        committed = _Committed()
        stale: list[Any] = []

    entry = _Entry()
    monkeypatch.setattr(pg_memory, "heads_owing", heads_owing)
    monkeypatch.setattr(pg_memory, "embedded", lambda row: True)
    sent = asyncio.run(pg_batch._served_documents([entry]))  # type: ignore[list-item]
    assert reads == [False, True]
    assert sent[id(entry)] == {}
    assert entry.stale == [("o", "n", 1)]


def test_a_decision_read_for_a_request_waits_for_no_standby(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a record retirement reads its writers durably: a request reading the live
    decision writers stays up while the standby is down."""
    from server.memory.store import records

    seen: list[bool] = []

    async def live_decision_writers(org, namespace, keys, *, durable=False):  # type: ignore[no-untyped-def]
        seen.append(durable)
        return []

    monkeypatch.setattr(pg_memory, "live_decision_writers", live_decision_writers)
    monkeypatch.setattr(pg, "enabled", lambda: True)

    class _CH:
        async def query_all(self, *a: Any, **k: Any) -> list[Any]:
            return []

        async def query_one(self, *a: Any, **k: Any) -> None:
            return None

    asyncio.run(records.live_writer_record("o", "n", "k", ch=_CH()))  # type: ignore[arg-type]
    assert seen == [False]


def test_a_retirement_gives_back_claims_a_standby_wait_left_no_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.store import pg_publish

    clock = _lease_clock(monkeypatch)

    async def live_decision_writers(org, namespace, keys, *, durable=False):  # type: ignore[no-untyped-def]
        assert durable
        clock.now = 100.0  # the standby wait outlasts the lease
        return []

    monkeypatch.setattr(pg_memory, "live_decision_writers", live_decision_writers)
    owed = pg_publish.OwedRecord({"memory_key": "k"}, ("o", "n", 1), 1)
    error = asyncio.run(pg_publish.retire_decision_records([owed], deadline=10.0, ch=None))  # type: ignore[arg-type]
    # An outage: the pass releases its claims uncounted.
    assert isinstance(error, pg.PostgresUnavailableError)


def _a_lease() -> float:
    """A claim's deadline as the worker's lease sets it, from now."""
    import time

    return time.monotonic() + settings.pg_delivery_lease_seconds


def test_a_copy_read_durably_within_its_lease_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    claim = _claim(5, "a" * 64)

    async def heads_by_keys(keys, *, include_tombstoned=False, durable=False):  # type: ignore[no-untyped-def]
        return {pg_memory.identity_key(claim.row): _head(9)}

    monkeypatch.setattr(pg_memory, "heads_by_keys", heads_by_keys)
    monkeypatch.setattr(pg_memory, "embedded", lambda row: True)
    built = asyncio.run(pg_delivery._still_heads([claim], deadline=_a_lease()))
    assert [c.row["version"] for c in built] == [9]


def test_a_retirement_read_durably_within_its_lease_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.store import pg_publish

    sent: list[str] = []

    async def live_decision_writers(org, namespace, keys, *, durable=False):  # type: ignore[no-untyped-def]
        return []

    async def tombstone(org, namespace, keys, *, version, ch, live):  # type: ignore[no-untyped-def]
        sent.append("retired")

    async def complete(*a: Any, **k: Any) -> None:
        sent.append("cleared")

    monkeypatch.setattr(pg_memory, "live_decision_writers", live_decision_writers)
    monkeypatch.setattr(pg_publish, "tombstone_decision_records", tombstone)
    monkeypatch.setattr(pg_memory, "complete", complete)
    owed = pg_publish.OwedRecord({"memory_key": "k"}, ("o", "n", 1), 1)
    error = asyncio.run(
        pg_publish.retire_decision_records([owed], deadline=_a_lease(), ch=None)  # type: ignore[arg-type]
    )
    assert error is None and sent == ["retired", "cleared"]


def test_a_retirement_that_reads_its_own_writers_reads_them_durably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.store import records

    seen: list[bool] = []

    async def live_decision_writers(org, namespace, keys, *, durable=False):  # type: ignore[no-untyped-def]
        seen.append(durable)
        raise RuntimeError("stop before ClickHouse")

    monkeypatch.setattr(pg_memory, "live_decision_writers", live_decision_writers)
    monkeypatch.setattr(pg, "enabled", lambda: True)
    with pytest.raises(RuntimeError):
        asyncio.run(records.tombstone_decision_records("o", "n", ["k"], version=1, ch=object()))  # type: ignore[arg-type]
    assert seen == [True]


def test_boot_refuses_a_role_that_may_not_write_the_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncpg

    from server.core import pg_profile

    async def refused() -> None:
        raise asyncpg.InsufficientPrivilegeError("permission denied for function")

    monkeypatch.setattr(pg, "check_durable_message", refused)
    with pytest.raises(pg_profile.DurabilityRefused, match="GRANT EXECUTE"):
        asyncio.run(pg_profile.check_durability())


def test_a_lease_clickhouse_spent_before_the_durable_read_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lease gone before the durable read is the send's own (PublicationPending,
    counted), not a standby outage."""
    from server.core import pg_lease

    claim = _claim(5, "a" * 64)
    reads: list[bool] = []

    async def heads_by_keys(keys, *, include_tombstoned=False, durable=False):  # type: ignore[no-untyped-def]
        reads.append(durable)
        return {pg_memory.identity_key(claim.row): _head(9)}

    monkeypatch.setattr(pg_memory, "heads_by_keys", heads_by_keys)
    monkeypatch.setattr(pg_memory, "embedded", lambda row: True)
    with pytest.raises(pg_lease.PublicationPending):
        asyncio.run(pg_delivery._still_heads([claim], deadline=0.0))
    assert reads == [False]  # no durable read was made


@pytest.mark.parametrize(
    "head",
    [
        pytest.param({"redacted_at": "2026-01-01 00:00:00"}, id="a redacted copy"),
        pytest.param({"tombstoned_at": "2026-01-01 00:00:00"}, id="a deleted copy"),
    ],
)
def test_the_flush_builds_no_document_onto_a_redacted_or_deleted_head(
    monkeypatch: pytest.MonkeyPatch, head: dict[str, str]
) -> None:
    """A redaction keeps the content hash: a redacted head is not a copy to build the
    writer's document onto, or the redacted text would be searchable again."""
    doc = {**_claim(5, "a" * 64).row}

    async def heads_owing(keys, *, durable=False):  # type: ignore[no-untyped-def]
        return {pg_memory.identity_key(doc): ({**_head(9), **head}, 0)}

    class _Committed:
        seqs = [("o", "n", 1)]
        rows = [doc]

    class _Entry:
        documents_sent = False
        documents = [doc]
        committed = _Committed()
        stale: list[Any] = []

    entry = _Entry()
    monkeypatch.setattr(pg_memory, "heads_owing", heads_owing)
    monkeypatch.setattr(pg_memory, "embedded", lambda row: True)
    sent = asyncio.run(pg_batch._served_documents([entry]))  # type: ignore[list-item]
    assert sent[id(entry)] == {}
    assert entry.stale == [("o", "n", 1)]
