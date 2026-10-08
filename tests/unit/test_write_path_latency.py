"""A single-fact write pays for its own rows, not its side effects.

- memory.set's entity-graph projection runs beside the response (canonical
  read + insert + edge inserts, off the serial tail); supersession and the
  audit row share one round trip.
- memory.update embeds value and key in one round trip, defers the LLM judge
  like memory.set, writes its two rows concurrently, and a pending
  verdict clears the old value's stale mark (the settle re-marks it).
- The HTTP transport's post-handler rows (budget violation, op audit,
  idempotency replay) are written concurrently.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from server.memory.app import after_write
from server.memory.app import conflict_deferred as cd
from server.memory.app import set_commit as set_commit
from server.memory.app import update as update_mod
from server.memory.domain.conflicts import ConflictResult, PendingJudge
from tests.unit.test_memory_update_conflict import _ID_A, _input, _row, wired  # noqa: F401

# ---- after_write -------------------------------------------------------------


@pytest.mark.asyncio
async def test_after_write_runs_beside_the_caller_and_drains() -> None:
    gate = asyncio.Event()
    done: list[str] = []

    async def _side_effect() -> None:
        await gate.wait()
        done.append("landed")

    after_write.schedule(_side_effect(), name="t")
    assert after_write.pending() == 1 and done == []  # the caller did not wait
    gate.set()
    assert await after_write.drain(timeout_s=1.0) == 1
    assert done == ["landed"] and after_write.pending() == 0


def test_after_write_without_a_loop_closes_the_coroutine() -> None:
    async def _never() -> None:  # pragma: no cover — closed, never run
        raise AssertionError

    assert after_write.schedule(_never(), name="t") is None


# ---- memory.set post-insert ------------------------------------------------------


@pytest.fixture
def post_insert(monkeypatch: pytest.MonkeyPatch):
    from server.memory.app import key_supersession as ks
    from server.observability import audit

    seen: dict[str, Any] = {"order": [], "scheduled": []}
    gate = asyncio.Event()

    async def _project(inp: Any, **kw: Any) -> None:
        seen["project_kw"] = kw
        await gate.wait()  # a slow projection: would block an awaited tail
        seen["order"].append("entities")

    async def _supersede(**_: Any) -> int:
        seen["order"].append("supersede")
        return 0

    async def _emit(**kw: Any) -> None:
        seen["order"].append("audit")
        seen["audit"] = kw

    real_schedule = after_write.schedule

    def _schedule(coro: Any, *, name: str, scope: Any = None) -> Any:
        seen["scheduled"].append(name)
        seen["scope"] = scope
        return real_schedule(coro, name=name, scope=scope)

    monkeypatch.setattr(set_commit, "_project_set_entities_fail_open", _project)
    monkeypatch.setattr(ks, "supersede_same_key_rows", _supersede)
    monkeypatch.setattr(audit, "emit", _emit)
    monkeypatch.setattr(after_write, "schedule", _schedule)
    return seen, gate


def _set_input() -> Any:
    from server.memory.api.models_crud import MemorySetInput

    return MemorySetInput(
        org="o", namespace="n", agent="a", memory_key="deploy_day", value="Tuesday"
    )


async def _post_insert() -> None:
    await set_commit._set_post_insert(
        _set_input(),
        ch=object(),
        cfg=type("Cfg", (), {"policy": "newest_wins"})(),
        memory_id="m1",
        content_sha256="sha",
        source_class="asserted",
        verdict="clear",
        actor="u",
    )


@pytest.mark.asyncio
async def test_set_returns_before_its_entity_projection_lands(post_insert) -> None:
    seen, gate = post_insert
    await asyncio.wait_for(_post_insert(), timeout=1.0)  # the projection is still blocked
    assert seen["scheduled"] == ["loom-set-entities"]
    # Scoped to its namespace, so a walk there waits for it; its edges wait for their flush.
    assert seen["scope"] == ("o", "n") and seen["project_kw"] == {"wait_visible": True}
    assert "entities" not in seen["order"]
    assert {"supersede", "audit"} <= set(seen["order"])
    # The audit row is the write's receipt: hashes, never the value.
    subject = seen["audit"]["subject"]
    assert subject["memory_id"] == "m1" and subject["content_sha256"] == "sha"
    assert "value" not in subject
    gate.set()
    await after_write.drain(timeout_s=1.0)
    assert seen["order"][-1] == "entities"


# ---- memory.update ----------------------------------------------------------------


class _OneCallEmbedder:
    model_name = "fake"
    dim = 3

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.1, 0.2, 0.3] for _ in texts]

    async def embed(self, text: str) -> list[float]:  # pragma: no cover — not the path
        self.calls.append([text])
        return [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_value_and_key_embed_in_one_round_trip() -> None:
    e = _OneCallEmbedder()
    value, key = await update_mod._embed_value_and_key(e, "value text", "deploy day")
    assert e.calls == [["value text", "deploy day"]] and value and key

    class _Plain:
        def __init__(self) -> None:
            self.live = 0
            self.peak = 0

        async def embed(self, text: str) -> list[float]:
            self.live += 1
            self.peak = max(self.peak, self.live)
            await asyncio.sleep(0.01)
            self.live -= 1
            return [1.0]

    p = _Plain()
    await update_mod._embed_value_and_key(p, "v", "k")
    assert p.peak == 2, "without embed_many the two calls overlap"
    assert await update_mod._embed_value_and_key(p, "v", "") == ([1.0], [])


@pytest.mark.asyncio
async def test_update_defers_the_judge_and_clears_the_stale_mark(wired, monkeypatch) -> None:  # noqa: F811
    pending = ConflictResult(
        verdict=cd.PENDING, peers=[], pending_judge=PendingJudge.__new__(PendingJudge)
    )
    ch, calls = wired(pending)
    scheduled: list[tuple[Any, str]] = []
    monkeypatch.setattr(
        cd,
        "schedule_settle",
        lambda ident, result, *, memory_id, cfg=None: scheduled.append((ident, memory_id)),
    )
    out = await update_mod.handle_memory_update(_input(run_id="run-7"))
    assert calls.check[0]["defer_judge"] is True
    assert out.updated and out.conflict_verdict == cd.PENDING
    # The old value's `conflicted` mark is not carried onto the new value:
    # the settle re-marks the row if the new value still conflicts.
    ((_, rows),) = ch.inserts
    assert rows[0]["status"] == "active" and rows[0]["conflict_with"] == []
    ((ident, memory_id),) = scheduled
    assert memory_id == _ID_A and ident.memory_key == "deploy_day"
    assert ident.memory_type == "semantic" and ident.run_id == "run-7" and ident.org == "test"


@pytest.mark.asyncio
async def test_update_writes_its_two_rows_in_one_round_trip(wired, monkeypatch) -> None:  # noqa: F811
    ch, calls = wired(ConflictResult(verdict="clear", peers=[]))
    both = asyncio.Event()
    started: list[str] = []

    async def _insert(table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        started.append(table)
        if len(started) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), timeout=1.0)  # sequential writes time out here
        ch.inserts.append((table, rows))

    async def _emb_rows(rows: list[dict[str, Any]], **_: Any) -> None:
        await _insert("loom.memory_embeddings", rows)
        calls.embedding_rows.extend(rows)

    monkeypatch.setattr(ch, "insert", _insert)
    monkeypatch.setattr("server.memory.store.insert_embedding_rows", _emb_rows)
    out = await update_mod.handle_memory_update(_input())
    assert out.updated and sorted(started) == ["loom.memory", "loom.memory_embeddings"]


# ---- HTTP transport tail -------------------------------------------------------


def test_http_post_handler_rows_are_written_concurrently(monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.core import idempotency
    from server.operations.registry import ALL_OPERATIONS
    from server.settings import settings
    from server.transport import http as http_mod

    base = next(o for o in ALL_OPERATIONS if o.name == "memory.unlock")

    async def _handler(inp: Any) -> Any:
        await asyncio.sleep(0.02)  # over its 1 ms budget: the violation row is due
        return base.output_model.model_construct(unlocked=True)

    op = dataclasses.replace(base, handler=_handler, budget_p99_ms=1)
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", [op])
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    monkeypatch.setattr(settings, "latency_budget_multiplier", 1.0)
    monkeypatch.setattr("server.observability.budgets.effective_budget_ms", lambda name, budget: 1)
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: None)

    async def _miss(*_: Any, **__: Any) -> Any:
        return idempotency.CacheResult(hit=None, conflict=False)

    live = {"now": 0, "peak": 0}

    async def _slow_row(**_: Any) -> None:
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.05)
        live["now"] -= 1

    monkeypatch.setattr(idempotency, "check", _miss)
    monkeypatch.setattr(idempotency, "store", _slow_row)
    monkeypatch.setattr("server.transport._common.emit_op_audit", _slow_row)
    monkeypatch.setattr("server.transport._common.emit_budget_violation", _slow_row)
    app = FastAPI()
    app.include_router(http_mod.build_router())
    r = TestClient(app).post(
        "/v1/memory.unlock",
        json={"org": "o", "namespace": "n", "agent": "a", "memory_key": "k", "reason": "t"},
        headers={"Idempotency-Key": "idem-1"},
    )
    assert r.status_code == 200, r.text
    assert live["peak"] == 3, "budget row, audit row and replay row share one round trip"


@pytest.mark.asyncio
async def test_update_reads_its_write_config_in_one_prefetch(wired, monkeypatch) -> None:  # noqa: F811
    from server.memory.app import conflicts as cf
    from server.memory.app import key_supersession as ks
    from server.memory.domain import conflicts as conflict_rules

    ch, calls = wired(ConflictResult(verdict="clear", peers=[]))
    fetched: list[tuple[str, str, str]] = []

    async def _prefetch(org: str, namespace: str, memory_type: str, memory_key: str) -> Any:
        fetched.append((namespace, memory_type, memory_key))
        return conflict_rules.WriteConfig(0.9, 0.97, "newest_wins", {"allowed"}, None, memory_type)

    policies: list[Any] = []

    async def _supersede(*, policy: Any = None, **_: Any) -> int:
        policies.append(policy)
        return 0

    monkeypatch.setattr(cf, "prefetch_write_config", _prefetch)
    monkeypatch.setattr(ks, "supersede_same_key_rows", _supersede)
    await update_mod.handle_memory_update(_input())
    assert fetched == [("personal", "semantic", "deploy_day")]
    kw = calls.check[0]
    assert kw["resolved_threshold"] == 0.9 and kw["resolved_duplicate_threshold"] == 0.97
    assert kw["resolved_policy"] == "newest_wins" and kw["allowlisted_keys"] == {"allowed"}
    assert policies == ["newest_wins"], "supersession takes the prefetched policy, no re-read"
