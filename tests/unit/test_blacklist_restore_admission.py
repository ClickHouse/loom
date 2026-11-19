"""Restoration admission follows its own grant writes and keeps rollback reads bounded."""

from collections import OrderedDict, defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from server.core import ch as ch_module
from server.core import grant_cache, pg
from server.identity.api.models import NamespaceRestoreInput, NamespaceSnapshotOutput
from server.identity.app import namespace_snapshot as namespaces
from server.memory import store
from server.memory.app import blacklist_guard, namespace_rollback
from server.memory.domain.rollback import RollbackAction, RollbackPlan
from server.memory.store import pg_memory, pg_publish
from tests.unit.test_blacklist_restore_paths import row


@pytest.mark.parametrize("count,change", [(0, "none"), (1, "none"), (1, "grant"), (1, "commit")])
async def test_restore_rechecks_policy_after_its_real_admin_grant(monkeypatch, count, change):
    @asynccontextmanager
    async def lock(*args):
        yield

    generation = 4
    committed = []

    async def generations(org):
        return {("", "*"): 0, (org, "*"): generation}

    async def bump(org, scope="*"):
        nonlocal generation
        generation += 1
        if change == "grant":
            monkeypatch.setattr(
                blacklist_guard,
                "_cache",
                [
                    blacklist_guard.BlacklistPattern(
                        "p",
                        "o",
                        "n",
                        "allowed",
                        "substring",
                        "namespace",
                        "",
                    )
                ],
            )

    async def commit(rows, *, admission=None, **kwargs):
        wanted = admission.generations if admission else {}
        if change == "commit":
            await bump("o")
        if wanted.get(("o", "*"), generation) != generation:
            raise pg_memory.AdmissionMoved({("o", "*"): generation})
        committed.extend(rows)

    ch = SimpleNamespace(query_one=AsyncMock(return_value=None), insert=AsyncMock())
    publish = AsyncMock()
    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg, "bump_policy", bump)
    monkeypatch.setattr(pg_memory, "policy_generations", generations)
    monkeypatch.setattr(pg_memory, "erased_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(blacklist_guard, "_ensure_loaded", AsyncMock())
    monkeypatch.setattr(blacklist_guard, "note_hit", AsyncMock())
    monkeypatch.setattr(blacklist_guard, "_cache", [])
    monkeypatch.setattr(grant_cache, "_generation", defaultdict(int))
    monkeypatch.setattr(grant_cache, "_entries", OrderedDict())
    monkeypatch.setattr(grant_cache, "_loading", {})
    monkeypatch.setattr(namespaces, "account_change", lock)
    monkeypatch.setattr(namespaces, "get_client", lambda: ch)
    monkeypatch.setattr(ch_module, "get_client", lambda: ch)
    monkeypatch.setattr(namespaces._registry, "resolve_subject", lambda: "owner")
    monkeypatch.setattr(namespaces.registration, "refused_as_in_use", AsyncMock(return_value=False))
    monkeypatch.setattr(pg_publish, "commit", commit)
    monkeypatch.setattr(pg_publish, "publish", publish)
    monkeypatch.setattr(namespaces.audit, "emit_strict", AsyncMock(return_value="receipt"))
    payload = NamespaceSnapshotOutput(
        snapshot_id="22222222-2222-2222-2222-222222222222",
        org="o",
        source_namespace="source",
        created_at=datetime.now(UTC),
        memory_count=count,
        type_count=0,
        grant_count=0,
        audit_tail_count=0,
        include_embeddings=False,
        memories=[namespaces.snapshot_memory_row(store.build_memory_version_row(row("allowed")))]
        * count,
        types=[],
        grants=[],
        audit_tail=[],
    )
    call = namespaces.handle_namespace_restore(
        NamespaceRestoreInput(org="o", target_namespace="n", payload=payload)
    )
    if change == "grant":
        with pytest.raises(HTTPException, match="BLACKLISTED"):
            await call
    elif change == "commit":
        with pytest.raises(pg_memory.AdmissionContendedError, match="policy changed"):
            await call
    else:
        assert (await call).restored
    assert generation == (6 if change == "commit" else 5)
    assert len(committed) == (count if change == "none" else 0)
    assert publish.await_count == (1 if change == "none" and count else 0)
    assert any(args[0] == "loom.namespace_grants" for args, _ in ch.insert.call_args_list)
    assert any(args[0] == "loom.acl_versions" for args, _ in ch.insert.call_args_list)


@pytest.mark.parametrize("count", [2, 3])
async def test_rollback_preflight_reads_only_one_batch_at_a_time(monkeypatch, count):
    monkeypatch.setattr(pg, "enabled", lambda: False)
    monkeypatch.setattr(namespace_rollback, "_INSERT_BATCH", 2)
    seen = []

    async def reinstated(org, namespace, actions, **kwargs):
        seen.append(len(actions))
        assert len(actions) <= 2
        return []

    async def write(*args, **kwargs):
        assert len(seen) > (count + 1) // 2, "all preflight slices run before any write"

    monkeypatch.setattr(namespace_rollback, "_reinstated", reinstated)
    monkeypatch.setattr(namespace_rollback, "_write", write)
    monkeypatch.setattr(blacklist_guard, "enforce_rows", AsyncMock(return_value={}))
    monkeypatch.setattr("server.embedding.get_embedder", lambda: SimpleNamespace(model_name="fake"))
    plan = RollbackPlan(
        reinstate=[
            RollbackAction(
                agent="a",
                user_id="-",
                session_id="-",
                memory_key=str(i),
                action="reinstate",
                version_from=2,
                version_to=1,
            )
            for i in range(count)
        ],
        remove=[],
    )
    await namespace_rollback.apply_rollback(
        "o",
        "n",
        plan,
        applied_at=datetime.now(UTC),
        ch=object(),
        applied=namespace_rollback.Applied(),
    )
    assert seen == ([2, 1] if count == 3 else [2]) * 2
