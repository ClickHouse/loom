"""conflicts.resolve acts only on rows of the body's namespace.

The transport authorizes ADMIN on the body's `namespace`, and a conflict is
surfaced within one namespace, so every row the resolve reads, tombstones,
re-activates or pairs in the allowlist is read and written there. A target
elsewhere reads like an unknown id: resolved=False, nothing written; a peer
elsewhere is left as it is.

`peer_id` narrows a resolve to one peer. A peer_id outside the target's
conflict_with answers resolved=False rather than widening to every peer.

The repository is faked as one table keyed by memory_id.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.core.ch import sequential_reads_active
from server.memory.api.models_conflicts import ConflictsResolveInput
from server.memory.app import conflicts_ops as h
from server.memory.store.conflicts import ConflictTarget

T_OWN, P_OWN, P_OWN2 = (f"1111111{i}-1111-1111-1111-111111111111" for i in range(3))
T_FOREIGN, P_FOREIGN = (f"2222222{i}-2222-2222-2222-222222222222" for i in range(2))
T_MIXED = "55555555-5555-5555-5555-555555555555"


@dataclass
class _Table:
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    tombstoned: list[str] = field(default_factory=list)
    activated: list[str] = field(default_factory=list)
    allowlisted: list[dict[str, Any]] = field(default_factory=list)
    count_reads_consistent: list[bool] = field(default_factory=list)
    # Deleted in the authority, while ClickHouse's copy still reads live.
    deleted: set[str] = field(default_factory=set)


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> _Table:
    from server.memory.store import conflicts as repo

    t = _Table(
        rows={
            T_OWN: {"namespace": "a", "key": "deploy_day", "conflict_with": [P_OWN, P_OWN2]},
            P_OWN: {"namespace": "a", "key": "deploy_day_2", "conflict_with": [T_OWN]},
            P_OWN2: {"namespace": "a", "key": "deploy_day_3", "conflict_with": [T_OWN]},
            T_FOREIGN: {"namespace": "b", "key": "payroll", "conflict_with": [P_FOREIGN]},
            P_FOREIGN: {"namespace": "b", "key": "payroll_2", "conflict_with": [T_FOREIGN]},
            # A target in the caller's namespace whose conflict set reaches into b.
            T_MIXED: {"namespace": "a", "key": "salary", "conflict_with": [P_OWN, P_FOREIGN]},
        }
    )

    async def _lookup(org: str, namespace: str, memory_id: str, **_: Any) -> ConflictTarget | None:
        row = t.rows.get(memory_id)
        if row is None or namespace != row["namespace"]:
            return None
        return ConflictTarget.from_row(
            {
                "memory_id": memory_id,
                "conflict_with": row["conflict_with"],
                "status": "conflicted",
                "namespace": row["namespace"],
            }
        )

    def _in(namespace: str, ids: list[str]) -> list[str]:
        return [i for i in ids if i in t.rows and t.rows[i]["namespace"] == namespace]

    async def _tombstone(
        org: str, ids: list[str], *_: Any, namespace: str, **__: Any
    ) -> list[dict[str, Any]]:
        t.tombstoned.extend(_in(namespace, ids))
        return []

    async def _activate(org: str, ids: list[str], *, namespace: str, **_: Any) -> None:
        t.activated.extend(_in(namespace, ids))

    async def _keys(org: str, ids: list[str], *, namespace: str, **_: Any) -> dict[str, str]:
        return {i: t.rows[i]["key"] for i in _in(namespace, ids)}

    async def _count(org: str, namespace: str, **_: Any) -> int:
        t.count_reads_consistent.append(sequential_reads_active())
        decided = {*t.tombstoned, *t.activated}
        return sum(1 for i, r in t.rows.items() if r["namespace"] == namespace and i not in decided)

    async def _allow(rows: list[dict[str, Any]], **_: Any) -> None:
        t.allowlisted.extend(rows)

    async def _get_by_id(memory_id: str, *, namespace: str | None = None, **_: Any) -> Any:
        row = t.rows.get(memory_id)
        if row is None or namespace not in (None, row["namespace"]) or memory_id in t.deleted:
            return None
        return SimpleNamespace(namespace=row["namespace"])

    monkeypatch.setattr("server.memory.store.get_by_id", _get_by_id)
    monkeypatch.setattr(repo, "lookup_target", _lookup)
    monkeypatch.setattr(repo, "tombstone_ids", _tombstone)
    monkeypatch.setattr(repo, "mark_active_by_ids", _activate)
    monkeypatch.setattr(repo, "lookup_keys_for_ids", _keys)
    monkeypatch.setattr(repo, "allowlist_insert", _allow)
    monkeypatch.setattr(repo, "count_conflicted", _count)
    h.reset_conflicts_count_cache()
    return t


@pytest.fixture
def admin_of_a() -> Iterator[None]:
    set_current_auth(
        AuthInfo(
            subject="sa:bot",
            org="acme",
            namespaces=["a"],
            roles=(),
            is_service_account=True,
            ns_roles=(("a", "admin"),),
        )
    )
    try:
        yield
    finally:
        set_current_auth(None)


def _resolve(memory_id: str, decision: str, peer_id: str | None = None) -> Any:
    return h.handle_conflicts_resolve(
        ConflictsResolveInput(
            org="acme",
            namespace="a",
            memory_id=memory_id,
            decision=decision,  # type: ignore[arg-type]
            peer_id=peer_id,
            reason="checked",
            resolved_by="ops",
        )
    )


async def test_revise_tombstones_the_peers_and_reactivates_the_winner(
    table: _Table, admin_of_a: None
) -> None:
    out = await _resolve(T_OWN, "revise")
    assert out.resolved is True
    assert table.tombstoned == [P_OWN, P_OWN2] and table.activated == [T_OWN]
    assert out.affected_memory_ids == [P_OWN, P_OWN2, T_OWN]


async def test_contract_tombstones_the_target_and_restores_its_peers(
    table: _Table, admin_of_a: None
) -> None:
    out = await _resolve(T_OWN, "contract")
    assert table.tombstoned == [T_OWN] and table.activated == [P_OWN, P_OWN2]
    assert out.affected_memory_ids == [T_OWN, P_OWN, P_OWN2]


async def test_allow_reactivates_all_and_records_each_pair_in_key_order(
    table: _Table, admin_of_a: None
) -> None:
    await _resolve(T_OWN, "allow")
    assert table.tombstoned == [] and table.activated == [T_OWN, P_OWN, P_OWN2]
    pairs = [(r["memory_key_a"], r["memory_key_b"]) for r in table.allowlisted]
    assert pairs == [("deploy_day", "deploy_day_2"), ("deploy_day", "deploy_day_3")]
    assert all(r["namespace"] == "a" and r["allowed_by"] == "ops" for r in table.allowlisted)


async def test_a_named_peer_narrows_the_resolve_to_that_peer(
    table: _Table, admin_of_a: None
) -> None:
    await _resolve(T_OWN, "revise", peer_id=P_OWN2)
    assert table.tombstoned == [P_OWN2] and table.activated == [T_OWN]


async def test_an_unknown_target_resolves_nothing(table: _Table, admin_of_a: None) -> None:
    out = await _resolve("33333333-3333-3333-3333-333333333333", "contract")
    assert out.resolved is False and out.affected_memory_ids == []
    assert table.tombstoned == table.activated == []


@pytest.mark.parametrize("decision", ["revise", "contract", "allow"])
async def test_a_row_outside_the_callers_namespace_reads_like_an_unknown_id(
    table: _Table, admin_of_a: None, decision: str
) -> None:
    out = await _resolve(T_FOREIGN, decision)
    assert out.resolved is False and out.affected_memory_ids == []
    assert table.tombstoned == table.activated == [] and table.allowlisted == []


async def test_a_peer_id_outside_the_conflict_set_resolves_nothing(
    table: _Table, admin_of_a: None
) -> None:
    out = await _resolve(T_OWN, "revise", peer_id="44444444-4444-4444-4444-444444444444")
    assert out.resolved is False and out.affected_memory_ids == []
    assert table.tombstoned == table.activated == []


@pytest.mark.parametrize("decision", ["revise", "contract", "allow"])
async def test_a_peer_outside_the_body_namespace_is_never_written(
    table: _Table, admin_of_a: None, decision: str
) -> None:
    """A conflict is surfaced within one namespace: a peer id another namespace holds
    (a restored copy keeps the ids) is not the resolve's to write."""
    out = await _resolve(T_MIXED, decision)
    assert out.resolved is True and P_FOREIGN not in out.affected_memory_ids
    assert P_FOREIGN not in {*table.tombstoned, *table.activated}


async def test_a_target_outside_the_body_namespace_resolves_nothing(
    table: _Table, admin_of_a: None
) -> None:
    # The body names namespace a and the row lives in b.
    out = await _resolve(T_FOREIGN, "contract")
    assert out.resolved is False and table.tombstoned == []


async def test_a_mixed_conflict_writes_only_the_body_namespace(
    table: _Table, admin_of_a: None
) -> None:
    out = await _resolve(T_MIXED, "revise")
    assert out.resolved is True and table.tombstoned == [P_OWN]


async def test_a_resolve_answers_the_namespaces_conflicted_count_after_its_writes(
    table: _Table, admin_of_a: None
) -> None:
    out = await _resolve(T_OWN, "revise")
    assert out.resolved is True
    # T_OWN is active and its two peers tombstoned: T_MIXED is the one left in a.
    assert out.total == 1


async def test_the_count_after_a_resolve_reads_its_own_writes(
    table: _Table, admin_of_a: None
) -> None:
    # A replica that has not applied the writes yet would answer a stale count that is
    # then cached for a minute.
    assert (await _resolve(T_OWN, "revise")).resolved is True
    assert table.count_reads_consistent == [True]


async def test_a_count_that_fails_after_the_writes_still_answers_the_applied_resolve(
    table: _Table, admin_of_a: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.store import conflicts as repo

    async def _count_fails(*_: Any, **__: Any) -> int:
        raise RuntimeError("ClickHouse unavailable")

    monkeypatch.setattr(repo, "count_conflicted", _count_fails)
    out = await _resolve(T_OWN, "revise")
    assert out.resolved is True and out.total is None
    assert table.tombstoned == [P_OWN, P_OWN2] and table.activated == [T_OWN]


async def test_a_target_the_authority_deleted_resolves_nothing(
    table: _Table, admin_of_a: None
) -> None:
    """ClickHouse's copy may not hold the authority's delete of the target yet."""
    table.deleted.add(T_OWN)
    out = await _resolve(T_OWN, "revise")
    assert out.resolved is False and table.tombstoned == [] and table.activated == []
