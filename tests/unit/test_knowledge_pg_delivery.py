"""The knowledge_pg_delivery worker: publishes the knowledge documents a request
committed to the Postgres authority and did not publish, and parks one that keeps
failing."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

import pytest

from server.core import ch_errors, pg
from server.core.ch import DEADLINE_MESSAGE, late_send
from server.core.errors import BackendUnavailableError
from server.knowledge.app import pg_delivery
from server.knowledge.store import chunks as chunks_repo
from server.knowledge.store import documents as documents_repo
from server.knowledge.store import pg_documents
from server.knowledge.workers import pg_delivery as worker
from server.settings import settings
from server.workers.registry import _WORKERS


def _delivery(uri: str, *, targets: int, attempts: int = 0) -> pg_documents.Delivery:
    row = {"org": "o", "namespace": "n", "source_uri": uri, "version": 7}
    return pg_documents.Delivery("o", "n", uri, 7, row, targets, 2, attempts)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    seen: dict[str, list[Any]] = {"published": [], "completed": [], "failed": [], "released": []}
    due: list[pg_documents.Delivery] = []
    seen["due"] = due
    seen["lease_s"] = [60.0]

    async def _claim(limit: int) -> tuple[list[pg_documents.Delivery], float]:
        return list(due), time.monotonic() + seen["lease_s"][0]

    async def _publish(rows: Any, *, retire_older: Any = frozenset()) -> None:
        if rows[0]["source_uri"] == "bad":
            raise RuntimeError("refused")
        if rows[0]["source_uri"] == "down":
            raise BackendUnavailableError("ClickHouse unreachable")
        if rows[0]["source_uri"] == "retire-down":  # the row in, then ClickHouse gone
            raise documents_repo.OlderNotRetired(BackendUnavailableError("unreachable"))
        if rows[0]["source_uri"] == "retire-refused":
            raise documents_repo.OlderNotRetired(RuntimeError("refused"))
        if rows[0]["source_uri"] == "outlasts":  # a send the lease's end cut off
            late = late_send(RuntimeError(f"DB::Exception: {DEADLINE_MESSAGE}"), "a write")
            assert late is not None
            raise late
        seen["published"].append((rows[0]["source_uri"], set(retire_older)))

    async def _complete(org: str, ns: str, uri: str, version: int, *, token: int) -> None:
        seen["completed"].append((uri, version, token))

    async def _failed(d: pg_documents.Delivery, error: str, *, park: bool) -> None:
        seen["failed"].append((d.source_uri, park))

    async def _release(claims: Any) -> None:
        seen["released"] += [d.source_uri for d in claims]

    async def _probe() -> bool:
        return False

    monkeypatch.setattr(pg_documents, "claim_due", _claim)
    monkeypatch.setattr(pg_documents, "release", _release)
    monkeypatch.setattr(ch_errors, "clickhouse_answers", _probe)
    monkeypatch.setattr(pg_documents, "complete", _complete)
    monkeypatch.setattr(pg_documents, "failed", _failed)
    monkeypatch.setattr(documents_repo, "publish", _publish)
    return seen


def test_a_due_delivery_is_published_and_completed_under_its_token(store: Any) -> None:
    store["due"] += [
        _delivery("a", targets=pg_documents.DOCUMENT_ROW),
        _delivery("b", targets=pg_documents.DOCUMENT_ROW | pg_documents.RETIRE_OLDER),
    ]
    assert asyncio.run(pg_delivery.publish_due(10)) == (2, 2, 0)
    assert store["published"] == [("a", set()), ("b", {"b"})]
    assert store["completed"] == [("a", 7, 2), ("b", 7, 2)]


def test_two_deletes_of_a_namespace_keep_their_own_retirement_stamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restore finds a delete's chunks by the stamp its tombstone retired them at: two
    tombstones published in one pass each retire their source at their own delete's
    time, not the first one's, each bounded by its own version."""

    def tomb(uri: str, version: int, at: datetime) -> pg_documents.Delivery:
        row = {"org": "o", "namespace": "n", "source_uri": uri, "version": version}
        row["superseded_at"] = at
        targets = pg_documents.DOCUMENT_ROW | pg_documents.RETIRE_OLDER
        return pg_documents.Delivery("o", "n", uri, version, row, targets, 2, 0)

    first, second = datetime(2026, 5, 1, 10, 0, 1), datetime(2026, 5, 1, 10, 0, 5)
    due = [tomb("a", 7, first), tomb("b", 9, second)]
    stamps: list[tuple[dict[str, int], Any]] = []

    async def claim(limit: int) -> tuple[list[pg_documents.Delivery], float]:
        return due, time.monotonic() + 60

    async def retire(org: str, ns: str, before: dict[str, int], **kw: Any) -> None:
        stamps.append((dict(before), kw.get("when")))

    class _CH:
        async def insert(self, *a: Any, **kw: Any) -> None:
            return None

    async def noop(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(pg_documents, "claim_due", claim)
    monkeypatch.setattr(pg_documents, "complete", noop)
    monkeypatch.setattr(pg_documents, "release", noop)
    monkeypatch.setattr(pg_documents, "failed", noop)
    monkeypatch.setattr(chunks_repo, "mark_docs_superseded", retire)
    monkeypatch.setattr(documents_repo, "_default_client", lambda: _CH())
    assert asyncio.run(pg_delivery.publish_due(10)) == (2, 2, 0)
    assert stamps == [({"a": 7}, first), ({"b": 9}, second)]


@pytest.mark.parametrize("attempts", [1, 2])
def test_a_failing_delivery_holds_back_only_itself_and_parks_at_the_limit(
    store: Any, monkeypatch: pytest.MonkeyPatch, attempts: int
) -> None:
    monkeypatch.setattr(settings, "pg_delivery_max_attempts", 3)
    store["due"] += [
        _delivery("bad", targets=pg_documents.DOCUMENT_ROW, attempts=attempts),
        _delivery("a", targets=pg_documents.DOCUMENT_ROW),
    ]
    assert asyncio.run(pg_delivery.publish_due(10)) == (2, 1, 1)
    assert store["failed"] == [("bad", attempts + 1 >= 3)]  # the third refusal parks it
    assert store["completed"] == [("a", 7, 2)]


def test_clickhouse_unavailable_counts_no_attempt_and_releases_the_rest(store: Any) -> None:
    store["due"] += [
        _delivery("a", targets=pg_documents.DOCUMENT_ROW),
        _delivery("down", targets=pg_documents.DOCUMENT_ROW),
        _delivery("b", targets=pg_documents.DOCUMENT_ROW),
    ]
    assert asyncio.run(pg_delivery.publish_due(10)) == (3, 1, 0)
    assert store["failed"] == [] and store["released"] == ["down", "b"]
    assert store["completed"] == [("a", 7, 2)]


def test_clickhouse_gone_during_the_retirement_counts_no_attempt(store: Any) -> None:
    store["due"].append(
        _delivery("retire-down", targets=pg_documents.DOCUMENT_ROW | pg_documents.RETIRE_OLDER)
    )
    assert asyncio.run(pg_delivery.publish_due(10)) == (1, 0, 0)
    assert store["failed"] == [] and store["released"] == ["retire-down"]


def test_a_retirement_clickhouse_refused_counts_an_attempt(store: Any) -> None:
    """Judged by the retirement's own error, not by the wrapper the row's success left."""
    store["due"].append(
        _delivery("retire-refused", targets=pg_documents.DOCUMENT_ROW | pg_documents.RETIRE_OLDER)
    )
    assert asyncio.run(pg_delivery.publish_due(10)) == (1, 0, 1)
    assert store["failed"] == [("retire-refused", False)] and store["released"] == []


def test_claims_whose_lease_is_about_to_end_are_released_unpublished(store: Any) -> None:
    """A publish past its lease could land after an erasure that waited the lease out."""
    store["lease_s"][0] = -1.0
    store["due"] += [_delivery("a", targets=pg_documents.DOCUMENT_ROW)]
    assert asyncio.run(pg_delivery.publish_due(10)) == (1, 0, 0)
    assert store["published"] == [] and store["released"] == ["a"]


def test_a_send_the_leases_end_cut_off_counts_an_attempt(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A send that would start past the lease is refused by its start guard as a late
    send while ClickHouse answers: it counts, so one that always outlasts the lease
    parks instead of being claimed again on every pass."""

    async def answers() -> bool:
        return True

    monkeypatch.setattr(ch_errors, "clickhouse_answers", answers)
    row = pg_documents.DOCUMENT_ROW
    store["due"][:] = [_delivery("outlasts", targets=row, attempts=9), _delivery("b", targets=row)]
    assert asyncio.run(pg_delivery.publish_due(10)) == (2, 1, 1)
    assert store["failed"] == [("outlasts", True)]
    assert [u for u, _ in store["published"]] == ["b"] and store["released"] == []


def test_the_worker_is_idle_under_the_clickhouse_authority(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:

    monkeypatch.setattr(pg, "enabled", lambda: False)
    store["due"].append(_delivery("a", targets=pg_documents.DOCUMENT_ROW))
    assert asyncio.run(worker.run_pass()) == worker.KnowledgeDeliveryResult()
    assert store["published"] == []


def test_the_worker_runs_on_the_postgres_delivery_cadence() -> None:

    row = next(w for w in _WORKERS if w.name == "knowledge_pg_delivery")
    assert row.module == "server.knowledge.workers.pg_delivery"
    assert row.idle_interval == "pg_delivery_idle_interval_seconds"


def test_a_claim_is_leased_for_the_delivery_workers_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker's lease (LOOM_PG_DELIVERY_LEASE_SECONDS), as the memory worker's, not a
    request's: one lease covers a whole pass's claims."""

    leases: list[float] = []

    async def fetch(sql: str, *args: Any) -> list[Any]:
        leases.append(args[1])
        return []

    monkeypatch.setattr(pg, "fetch", fetch)
    monkeypatch.setattr(settings, "pg_delivery_lease_seconds", 30)
    monkeypatch.setattr(settings, "pg_request_lease_seconds", 5)
    claims, deadline = asyncio.run(pg_documents.claim_due(10))
    assert claims == [] and leases == [30.0] and deadline > time.monotonic() + 20


def test_the_pass_expires_the_jobs_no_run_saved_for_30_days(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under Postgres, whether or not the ingest worker runs: the delivery pass deletes
    them, bounded by its batch, and reports how many."""
    from server.knowledge.store import pg_jobs

    limits: list[int] = []

    async def expire(limit: int) -> int:
        limits.append(limit)
        return 3

    async def none(limit: int) -> int:
        return 0

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_documents, "sweep_erasure_evidence", none)
    monkeypatch.setattr(pg_documents, "sweep_settled", none)
    monkeypatch.setattr(pg_jobs, "expire", expire)
    result = asyncio.run(worker.run_pass())
    assert result.expired_jobs == 3 and limits == [settings.pg_delivery_batch_rows]
