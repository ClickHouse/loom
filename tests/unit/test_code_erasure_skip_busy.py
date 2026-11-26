"""A code namespace being erased, on best effort (`server.code.app.index.erasing`).

The purge refuses, retryably, while work begun before the erasure was recorded may still
write the namespace, and succeeds once it has ended; a pass that starts after the record
skips the namespace; a refusal on a worker's pass ends at that pass's deadline even when
the worker never reports again. CH-free: the ledger, the leases and the worker-status
rows are fakes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.app.index import erasing
from server.code.app.index import ingest_pass as w
from server.code.app.index import pass_budget as ingest_budget
from server.core import ch, leases
from server.core.errors import OverloadedError
from server.erasure.app import ledger_reads, saga_steps
from server.erasure.store import ledger
from server.erasure.store import mutations as erasure_mutations
from server.erasure.store import steps_memory as memory_steps
from server.workers import registry, status_store
from tests.unit.test_code_ingest_pass_budget import _cfg, _Clock, _wire_run

# The real ones: the unit conftest stubs them for every other test.
BEING_ERASED, HOLDER = erasing.being_erased, erasing._holder


class _World:
    """The code index of `indexed`, the leases held, and the workers' newest rows."""

    def __init__(self, recorded: datetime) -> None:
        self.recorded = recorded
        self.indexed = ["code-a"]
        self.held: set[str] = set()
        self.rows: list[dict[str, Any]] = []
        self.purged: list[Any] = []
        self.unreadable = False
        self.enabled = ["code_ingest"]  # the enabled code workers

    def ingest_row(self, finished: datetime, **kw: Any) -> None:
        self.rows.append(
            {
                "worker": "code_ingest",
                "pass_finished": finished.isoformat(),
                "status": "ok",
                "trigger": "interval",
                "interval_s": 120.0,
                "deadline_s": 900.0,
                **kw,
            }
        )

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        assert "code_index_state" in sql
        return [{"namespace": ns} for ns in self.indexed if ns in params["ns"]]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    recorded = datetime.now(UTC) - timedelta(seconds=30)
    world = _World(recorded)
    monkeypatch.setattr(erasing, "_holder", HOLDER)
    monkeypatch.setattr(ch, "get_client", lambda: world)

    async def has_live_holder(name: str, **_: Any) -> bool:
        return name in world.held

    async def latest_rows(**kw: Any) -> list[dict[str, Any]]:
        if world.unreadable:
            if kw.get("strict"):
                raise ConnectionError("status store down")
            return []  # what the lenient read answers
        return list(world.rows)

    def enabled_workers() -> list[Any]:
        return [SimpleNamespace(name=n, deadline_seconds=lambda: 900.0) for n in world.enabled]

    async def request(erasure_id: str, **_: Any) -> dict[str, Any]:
        return {"requested_at": world.recorded.replace(tzinfo=None)}

    async def purge(org: str, namespace: Any, **_: Any) -> int:
        world.purged.append(namespace)
        return 7

    monkeypatch.setattr(leases, "has_live_holder", has_live_holder)
    monkeypatch.setattr(status_store, "latest_rows", latest_rows)
    monkeypatch.setattr(registry, "enabled_workers", enabled_workers)
    monkeypatch.setattr(ledger, "fetch_request_status", request)
    monkeypatch.setattr(memory_steps, "purge_code_index_by_namespace", purge)
    return world


async def _purge(namespaces: Any = ("code-a",)) -> int:
    with erasure_mutations.saga_scope("00000000-0000-0000-0000-000000000001"):
        return await saga_steps._step_purge_code_index_by_ns("o", list(namespaces))


async def test_a_pass_in_flight_refuses_the_purge_until_it_ends(world: _World) -> None:
    # The incident: the pass began before the erasure was recorded and is still running
    # (its worker's newest row finished more than an interval before the record).
    world.ingest_row(world.recorded - timedelta(seconds=200))
    with pytest.raises(OverloadedError) as refused:
        await _purge()
    assert "code_ingest pass that began before the erasure" in refused.value.message
    assert "repeat it" in refused.value.message
    assert world.purged == []
    world.ingest_row(world.recorded + timedelta(seconds=20))  # that pass has ended
    assert await _purge() == 7
    assert world.purged == [["code-a"]]


@pytest.mark.parametrize(
    ("lease", "says"),
    [("scip_entry:o/code-a", "entry lock"), ("code_refresh:o/code-a", "code.refresh")],
)
async def test_a_lease_on_the_namespace_refuses_the_purge(
    world: _World, lease: str, says: str
) -> None:
    world.ingest_row(world.recorded + timedelta(seconds=20))  # no pass in flight
    world.held.add(lease)
    with pytest.raises(OverloadedError, match=says):
        await _purge(["code-b", "code-a"])
    world.held.clear()
    assert await _purge(["code-b", "code-a"]) == 7


async def test_a_worker_idle_at_the_record_or_a_memory_namespace_does_not_hold_it(
    world: _World,
) -> None:
    world.ingest_row(world.recorded - timedelta(seconds=60))  # next pass after the record
    assert await _purge() == 7
    world.rows.clear()
    world.ingest_row(world.recorded - timedelta(seconds=200))
    world.indexed = []  # no code index: a memory namespace
    assert await _purge() == 7


async def test_an_unreadable_status_store_refuses_the_purge_retryably(world: _World) -> None:
    world.ingest_row(world.recorded + timedelta(seconds=20))
    world.unreadable = True
    with pytest.raises(OverloadedError, match="worker state unknown") as refused:
        await _purge()
    assert refused.value.extra["retry_after_s"] == 60 and world.purged == []
    world.unreadable = False
    assert await _purge() == 7


async def test_an_enabled_worker_with_no_finished_row_holds_the_purge_to_its_deadline(
    world: _World,
) -> None:
    world.enabled = ["code_ingest", "code_summary"]  # its first pass: no row yet
    world.ingest_row(world.recorded + timedelta(seconds=20))
    with pytest.raises(OverloadedError, match="code_summary pass that began before"):
        await _purge()
    world.recorded = datetime.now(UTC) - timedelta(seconds=920)  # its deadline is past
    assert await _purge() == 7


async def test_the_refusal_ends_at_the_pass_deadline(world: _World) -> None:
    # A worker that never reports again (crashed, stopped): its pass was cut by now.
    world.recorded = datetime.now(UTC) - timedelta(seconds=880)
    world.ingest_row(world.recorded - timedelta(seconds=200))
    with pytest.raises(OverloadedError):
        await _purge()
    world.recorded = datetime.now(UTC) - timedelta(seconds=920)
    assert await _purge() == 7


async def test_a_pass_that_starts_after_the_record_skips_the_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(erasing, "being_erased", BEING_ERASED)

    async def unfinished(org: str, ns: str, user: Any, *_: Any, **__: Any) -> list[Any]:
        assert user == ""  # no user scope: a user erasure never purges the code index
        erasure = {"scope_type": "namespace", "scope_value": "code-a", "purge": 1}
        return [erasure] if ns == "code-a" else []

    monkeypatch.setattr(ledger_reads, "unfinished_reaching", unfinished)
    order = _wire_run(monkeypatch, [_cfg("code-a"), _cfg("code-b")], {}, _Clock())
    await w._run(ingest_budget.CodeIngestResult())
    assert order == ["commits:code-b"]


async def test_an_unreadable_ledger_skips_the_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(erasing, "being_erased", BEING_ERASED)

    async def down(*_: Any, **__: Any) -> list[Any]:
        raise ConnectionError("ledger down")

    monkeypatch.setattr(ledger_reads, "unfinished_reaching", down)
    assert await erasing.being_erased("o", "code-a") is True


def _entries(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import server.code.domain.index.repos as repos

    monkeypatch.setattr(
        repos,
        "configured_repos",
        lambda: [
            SimpleNamespace(org="o", namespace=n, label=f"o/{n}") for n in ("code-a", "code-b")
        ],
    )

    async def being_erased(org: str, ns: str) -> bool:
        return ns == "code-a"

    monkeypatch.setattr(erasing, "being_erased", being_erased)


async def test_the_summary_worker_skips_a_namespace_being_erased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.workers import summary

    _entries(monkeypatch)
    seen: list[str] = []

    async def fake_repo(org: str, ns: str, result: Any, **kw: Any) -> None:
        seen.append(ns)

    monkeypatch.setattr(summary, "_summarize_repo", fake_repo)
    await summary._run(summary.CodeSummaryResult())
    assert seen == ["code-b"]


async def test_embed_recovery_skips_a_namespace_being_erased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.store import index as code_store
    from server.code.workers import embed_recovery as w
    from server.embedding import embedder

    _entries(monkeypatch)
    monkeypatch.setattr(code_store, "CodeRepo", lambda: object())
    monkeypatch.setattr(embedder, "get_embedder", lambda: object())
    seen: list[list[tuple[str, str]]] = []

    async def fake_recover(table: str, repos: Any, *a: Any, **kw: Any) -> int:
        seen.append(list(repos))
        return 0

    monkeypatch.setattr(w, "_recover_table", fake_recover)
    await w._run(w.CodeEmbedRecoveryResult())
    assert seen and all(r == [("o", "code-b")] for r in seen)


async def test_graph_stats_does_not_refit_the_risk_of_a_namespace_being_erased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.workers import graph_stats as gs

    _entries(monkeypatch)

    async def none_due() -> set[str]:
        return set()

    async def unit_risk(*a: Any, **kw: Any) -> None:
        return None

    refit: list[list[str]] = []

    async def fake_refit(repos: list[Any], result: Any) -> None:
        refit.append([c.namespace for c in repos])

    monkeypatch.setattr(gs, "due_namespaces", none_due)
    monkeypatch.setattr(gs, "_run_unit_risk", unit_risk)
    monkeypatch.setattr(gs, "_refit_jit_risk", fake_refit)
    await gs._run(gs.CodeGraphStatsResult())
    assert refit == [["code-b"]]
