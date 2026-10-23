"""A delayed retry changes existing rows and cannot restore erased transcripts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.unit.clickhouse_local import CLICKHOUSE, run
from tests.unit.test_extraction_retry_mine import _MutationCH, _requeue
from tests.unit.test_extraction_status_mine import _queued

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


class _DelayedMutationCH(_MutationCH):
    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.commands.append((sql, dict(params or {})))

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        if "system.mutations" not in sql:
            return await super().query_all(sql, params)
        # clickhouse-local keeps no unfinished mutation: the unapplied commands stand in.
        pending = {b[3] for _, p in self.commands for b in p.get("batches", [])}
        return [{"qid": qid} for qid in (params or {})["qids"] if qid in pending]

    def finish(self) -> None:
        for sql, params in self.commands:
            self.apply(sql, params)
        self.commands.clear()


def _delete(ch: _MutationCH, scope: str) -> None:
    predicate = "user_id = 'u1'" if scope == "user" else "namespace = 'n'"
    ch.apply(
        f"ALTER TABLE loom.memory_extraction_queue DELETE WHERE org = 'o' AND {predicate} SETTINGS mutations_sync = 2"
    )


@pytest.mark.parametrize("scope", ["user", "namespace"])
@pytest.mark.parametrize("order", ["retry-first", "erase-first"])
def test_delayed_retry_and_erasure_leave_no_transcript(
    tmp_path: Path, scope: str, order: str
) -> None:
    ch = _DelayedMutationCH(
        tmp_path / "ch",
        _queued("q1", by="process:sa:a", status="failed"),
        _queued("other-user", by="process:sa:a", user_id="u2", status="failed"),
        _queued("other-ns", by="process:sa:a", namespace="other", status="failed"),
        _queued("other-org", by="process:sa:a", org="other", status="failed"),
    )
    assert _requeue(ch, ["q1"]).requeued == ["q1"]
    assert next(r for r in ch.stored() if r["queue_id"] == "q1")["status"] == "failed"
    if order == "retry-first":
        ch.finish()
    _delete(ch, scope)
    ch.finish()
    ids = {r["queue_id"] for r in ch.stored()}
    assert "q1" not in ids and "other-org" in ids
    assert ("other-user" in ids) == (scope == "user")
    assert ("other-ns" in ids) == (scope == "namespace")


@pytest.mark.parametrize("failure", [asyncio.CancelledError, ConnectionError])
def test_a_lost_response_cannot_restore_an_erased_row(
    tmp_path: Path, failure: type[BaseException]
) -> None:
    class _LostResponse(_DelayedMutationCH):
        async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
            await super().execute(sql, params)
            raise failure()

    ch = _LostResponse(tmp_path / "ch", _queued("q1", by="process:sa:a", status="failed"))
    with pytest.raises(failure):
        _requeue(ch, ["q1"])
    _delete(ch, "user")
    ch.finish()
    assert ch.stored() == []


@pytest.mark.parametrize("status", ["pending", "running", "done", "failed"])
def test_a_stale_retry_cannot_change_a_newer_worker_version(tmp_path: Path, status: str) -> None:
    ch = _DelayedMutationCH(tmp_path / "ch", _queued("q1", by="process:sa:a", status="failed"))
    _requeue(ch, ["q1"])
    run([_queued("q1", by="process:sa:a", status=status, version=2)], "SELECT 1", path=ch.path)
    before = ch.stored()
    ch.finish()
    assert ch.stored() == before


def test_duplicate_acceptances_submit_one_update(tmp_path: Path) -> None:
    ch = _DelayedMutationCH(tmp_path / "ch", _queued("q1", by="process:sa:a", status="failed"))
    assert _requeue(ch, ["q1"]).requeued == ["q1"]
    assert _requeue(ch, ["q1"]).requeued == ["q1"]
    assert len(ch.commands) == 1
    ch.finish()
    (row,) = ch.stored()
    assert (row["status"], row["attempts"], row["version"]) == ("pending", 2, 1)
    assert _requeue(ch, ["q1"]) == ([], ["q1"], [], [])
