"""The apply worker serves every namespace holding a complete
pending batch, not only the registry's, in one statement."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.code.workers import scip_apply as w


def _batch(ns: str, batch_id: str) -> dict:
    return {
        "org": "clickhouse",
        "namespace": ns,
        "batch_id": batch_id,
        "commit_sha": b"c" * 40,
        "base_sha": b"\x00" * 40,
        "total_parts": 1,
        "producer": "scip-python",
        "created_at": None,
    }


class _Repo:
    def __init__(self, batches: list[dict]) -> None:
        self.batches = batches
        self.polls: list[dict] = []
        self.upserts: list[tuple[str, str, str]] = []
        self.states: list[tuple[str, str, str]] = []
        self.fail_mark: set[str] = set()

    async def pending_scip_batches(self, **kw):
        self.polls.append(kw)
        return self.batches

    async def scip_parts(self, org, ns, batch_id):
        return [{"seq": 0, "payload": "{}", "payload_hash": b""}]

    async def upsert_scip_batch(self, org, ns, batch_id, **kw):
        if kw["status"] == "failed" and batch_id in self.fail_mark:
            raise RuntimeError("insert timed out")
        self.upserts.append((ns, batch_id, kw["status"]))

    async def set_state(self, org, ns, key, value, detail=""):
        self.states.append((ns, key, value))


def _patch(monkeypatch, repo, *, apply=None) -> None:
    monkeypatch.setattr("server.code.store.index.CodeRepo", lambda: repo)
    import server.code.app.index.scip_delta as sd

    monkeypatch.setattr(
        sd.ScipDelta,
        "model_validate_json",
        classmethod(lambda cls, payload: SimpleNamespace(delta_version=1)),
    )

    async def fake_apply(repo_, org, ns, delta):
        return SimpleNamespace(files=3, symbols_new=2, edges_new=1)

    monkeypatch.setattr(sd, "apply_delta", apply or fake_apply)


@pytest.mark.asyncio
async def test_an_unregistered_namespaces_batch_is_applied_from_one_poll(monkeypatch):
    repo = _Repo([_batch("code-oss-fastapi", "b1")])
    _patch(monkeypatch, repo)
    result = w.CodeScipApplyResult()
    await w._run(result)
    assert repo.polls == [{"per_namespace": 3}], "one statement for every namespace"
    assert repo.upserts == [("code-oss-fastapi", "b1", "applied")]
    assert repo.states and repo.states[0][:2] == ("code-oss-fastapi", "scip_commit_py")
    assert result.batches_applied == 1 and result.files == 3


@pytest.mark.asyncio
async def test_a_failed_failure_mark_does_not_stop_the_next_batch(monkeypatch):
    repo = _Repo([_batch("ns-a", "bad"), _batch("ns-b", "good")])
    repo.fail_mark = {"bad"}

    async def apply(repo_, org, ns, delta):
        if ns == "ns-a":
            raise ValueError("corrupt delta")
        return SimpleNamespace(files=1, symbols_new=0, edges_new=0)

    _patch(monkeypatch, repo, apply=apply)
    result = w.CodeScipApplyResult()
    await w._run(result)
    assert repo.upserts == [("ns-b", "good", "applied")]
    assert result.batches_failed == 1 and result.batches_applied == 1


def test_the_poll_caps_batches_per_namespace_in_the_statement() -> None:
    import inspect

    from server.code.store.index.scip_staging import ScipStagingMixin

    src = inspect.getsource(ScipStagingMixin.pending_scip_batches)
    assert "LIMIT {per_ns:UInt32} BY b.org, b.namespace" in src
    assert "parts.org = b.org AND parts.namespace = b.namespace" in src
