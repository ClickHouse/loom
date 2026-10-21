"""`append_only`: a namespace whose writes are events.

Under it the gate answers `clear` before the peer statement: no merge,
duplicate or conflict verdict against a peer. The policy is read from the
prefetched write config when the caller had it, else once; an unreadable
policy keeps the cosine path.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app import conflicts as cf
from server.memory.domain import conflicts as conflict_rules
from tests.unit.test_conflict_check import _P1, _row, _RowsCH


def _peer() -> list[dict[str, Any]]:
    return [_row(_P1, "run-1/report", "DC change impact: cluster-a REGRESSED", dist=0.01)]


async def _check(**kw: Any) -> conflict_rules.ConflictResult:
    return await cf.check_on_commit(
        org="o",
        namespace="n",
        agent="ag",
        memory_type="semantic",
        memory_key="run-2/report",
        new_value="DC change impact: cluster-a IMPROVED",
        new_embedding=[0.1, 0.2, 0.3],
        **kw,
    )


def test_the_policy_is_a_declared_value() -> None:
    from server.identity.api.models import NamespaceRegisterInput

    inp = NamespaceRegisterInput(
        org="o", namespace="n", owner_email="x@y", default_conflict_policy="append_only"
    )
    assert inp.default_conflict_policy == conflict_rules.APPEND_ONLY_POLICY == "append_only"


@pytest.mark.asyncio
async def test_prefetched_append_only_answers_clear_with_no_statement(monkeypatch) -> None:
    ch = _RowsCH(_peer())
    monkeypatch.setattr(cf, "get_client", lambda: ch)
    cfg = conflict_rules.WriteConfig(0.85, 0.97, "append_only", set(), None, "semantic")
    res = await _check(**cfg.check_kwargs())
    assert res.verdict == "clear" and res.peers == [] and res.duplicate_of_id is None
    assert ch.sql == []  # the peer statement was never issued


@pytest.mark.asyncio
async def test_unprefetched_policy_is_read_once_then_clear(monkeypatch) -> None:
    ch = _RowsCH(_peer())
    monkeypatch.setattr(cf, "get_client", lambda: ch)
    calls: list[str] = []

    async def policy(org: str, namespace: str, *, ch: Any) -> str:
        calls.append(namespace)
        return "append_only"

    monkeypatch.setattr("server.memory.app.conflicts.namespace_conflict_policy", policy)
    res = await _check()
    assert res.verdict == "clear" and calls == ["n"] and ch.sql == []


@pytest.mark.asyncio
async def test_other_policies_still_reach_a_verdict(monkeypatch) -> None:
    """The near-identical peer at cosine 0.99 is a duplicate under
    auto_reinforce — the verdict this decision switches off, still there."""
    ch = _RowsCH(_peer())
    monkeypatch.setattr(cf, "get_client", lambda: ch)
    cfg = conflict_rules.WriteConfig(0.85, 0.97, "auto_reinforce", set(), None, "semantic")
    res = await _check(**cfg.check_kwargs())
    assert res.verdict != "clear" and len(ch.sql) >= 1


@pytest.mark.asyncio
async def test_an_unreadable_policy_keeps_the_gate(monkeypatch) -> None:
    ch = _RowsCH(_peer())
    monkeypatch.setattr(cf, "get_client", lambda: ch)

    async def boom(org: str, namespace: str, *, ch: Any) -> str:
        raise RuntimeError("directory away")

    monkeypatch.setattr("server.memory.app.conflicts.namespace_conflict_policy", boom)

    async def thr(*a: Any, **k: Any) -> float:
        return 0.85

    monkeypatch.setattr(cf, "_resolve_threshold", thr)
    res = await _check(resolved_duplicate_threshold=0.97, allowlisted_keys=set())
    assert res.verdict != "clear"  # the cosine path ran against the peer
