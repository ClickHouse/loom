"""Expiry has a post-action.

The plan for each action, the sweep with the repo stubbed (one batched
insert, locks honoured, dry run writes nothing), and update_by_id's
EXPIRED refusal with revive.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from server.memory.api.models_crud import MemoryUpdateByIdInput
from server.memory.app import expiry as X
from server.memory.app import handlers as mem_handlers
from server.memory.app import restore as _restore_mod
from server.memory.app import stack as _stack_mod
from server.memory.app import update as update_mod
from server.memory.store import MemoryRow
from tests.unit.memory_store import one_holder

_NOW = datetime(2026, 9, 12, 12, 0, 0)
# A row the handlers see as expired has to be expired by the wall clock.
_REAL_NOW = datetime.now(UTC).replace(tzinfo=None)


def _row(**overrides: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": "22222222-2222-2222-2222-222222222222",
        "org": "test",
        "namespace": "personal",
        "agent": "cursor",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "churn_note",
        "memory_type": "semantic",
        "value": "Customer churned after the outage",
        "confidence": 0.8,
        "version": 100,
        "pinned": False,
        "created_at": _NOW - timedelta(days=30),
        "expires_at": _REAL_NOW - timedelta(minutes=5),
        "entities": ["acme"],
    }
    base.update(overrides)
    return MemoryRow(**base)


def test_effective_action_defaults_to_tombstone() -> None:
    assert X.effective_action(_row()) == "tombstone"
    assert X.effective_action(_row(on_expire="tombstone")) == "tombstone"
    assert X.effective_action(_row(on_expire="demote")) == "demote"
    assert X.effective_action(_row(on_expire="bogus")) == "tombstone"


def test_plans() -> None:
    cfg = X.ExpiryConfig(demote_factor=0.5, demote_floor=0.05)
    t = X.plan(_row(), now=_NOW, cfg=cfg)
    assert t["tombstoned_at"] == _NOW and t["expires_at"] is None
    d = X.plan(_row(on_expire="demote", expire_reason="stale runbook"), now=_NOW, cfg=cfg)
    assert d["expires_at"] is None and d["confidence"] == 0.4 and d["on_expire"] == ""
    assert d["expire_reason"] == "demoted on expiry: stale runbook"
    # the floor holds
    d2 = X.plan(_row(on_expire="demote", confidence=0.05), now=_NOW, cfg=cfg)
    assert d2["confidence"] == 0.05
    a = X.plan(_row(on_expire="anonymize", expire_reason="gdpr"), now=_NOW, cfg=cfg)
    assert a["value"] == '"[EXPIRED]"' and a["entities"] == [] and a["expires_at"] is None
    assert a["redacted_at"] == _NOW and a["redaction_reason"] == "expired:anonymize|gdpr"
    assert a["content_sha256"] and a["on_expire"] == ""


class _CH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.executes: list[str] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        self.inserts.append((table, rows))

    async def execute(self, sql: str, **_: Any) -> None:
        self.executes.append(sql)


_DEMOTE_REEMITS: list[tuple[str, dict[str, Any]]] = []
reemit_calls: list[int] = []
retired: list[tuple[str, int]] = []


def _stub(monkeypatch: pytest.MonkeyPatch, rows: list[MemoryRow]) -> tuple[_CH, list[str]]:
    ch = _CH()
    tombstoned_keys: list[str] = []
    _DEMOTE_REEMITS.clear()

    async def _reemit(org, ns, agent, uid, sid, key, *, ch=None, **overrides) -> int:
        # The demote mirror is a versioned re-emit, never an UPDATE.
        _DEMOTE_REEMITS.append((key, overrides))
        return 1

    async def _find(**_: Any) -> list[MemoryRow]:
        return rows

    async def _tomb_emb(org, ns, agent, uid, sid, key, *, ts, version, ch=None) -> int:
        tombstoned_keys.append(key)
        return 1

    monkeypatch.setattr("server.memory.store.find_expired_candidates", _find)
    monkeypatch.setattr("server.memory.store.tombstone_embeddings_for_key", _tomb_emb)
    monkeypatch.setattr("server.memory.store.reemit_embedding_rows_for_key", _reemit)
    reemit_calls.clear()

    async def _reemit_many(items, *, ch=None, batch=500) -> int:
        # The pass's mirrors arrive as ONE call; each key keeps
        # its own overrides (tombstone/anonymize vs demote).
        reemit_calls.append(len(items))
        for (_o, _n, _a, _u, _s, key), overrides in items:
            if "tombstoned_at" in overrides:
                tombstoned_keys.append(key)
            else:
                _DEMOTE_REEMITS.append((key, overrides))
        return len(items)

    monkeypatch.setattr("server.memory.store.reemit_embedding_rows_for_keys", _reemit_many)
    retired.clear()

    async def _retire_many(items, *, ch=None) -> int:
        # Tombstone and anonymize retire memory_embeddings AND entity_vectors.
        for (_o, _n, _a, _u, _s, key), version in items:
            tombstoned_keys.append(key)
            retired.append((key, version))
        return len(items)

    monkeypatch.setattr("server.memory.store.retire_vectors_for_keys", _retire_many)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    return ch, tombstoned_keys


@pytest.mark.asyncio
async def test_sweep_writes_one_batched_insert_and_mirrors(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _row(memory_key="a"),
        _row(memory_key="b", on_expire="demote", confidence=0.9),
        _row(memory_key="c", on_expire="anonymize"),
        _row(memory_key="d", lock_mode="read_only", lock_reason="hold"),  # tombstone refused
        _row(memory_key="e", lock_mode="no_delete", on_expire="demote"),  # value write allowed
    ]
    ch, tomb_keys = _stub(monkeypatch, rows)
    res = await X.run_pass(X.ExpiryConfig())
    assert (res.scanned, res.tombstoned, res.demoted, res.anonymized, res.locked_skipped) == (
        5,
        1,
        2,
        1,
        1,
    )
    assert len(ch.inserts) == 1 and ch.inserts[0][0] == "loom.memory"
    written = {r["memory_key"]: r for r in ch.inserts[0][1]}
    assert set(written) == {"a", "b", "c", "e"}
    assert written["a"]["tombstoned_at"] is not None
    assert written["b"]["expires_at"] is None and written["b"]["confidence"] == 0.45
    assert written["c"]["value"] == '"[EXPIRED]"' and written["c"]["redacted_at"] is not None
    assert written["e"]["lock_mode"] == "no_delete"  # the lock rides the version row
    assert all(r["version"] > 100 for r in written.values())
    assert sorted(tomb_keys) == ["a", "c"]  # tombstone + anonymize drop the embeddings
    assert ch.executes == []  # no UPDATE on the vector table
    assert sorted(_DEMOTE_REEMITS) == [  # demote mirror: expiry cleared, confidence lowered
        ("b", {"expires_at": None, "confidence": 0.45, "version": written["b"]["version"]}),
        ("e", {"expires_at": None, "confidence": 0.4, "version": written["e"]["version"]}),
    ]
    assert res.failures == [] and len(res.sample) == 4
    # One batched retire and one batched demote copy for the pass, never one per key;
    # each retire carries its memory row's version.
    assert reemit_calls == [2]
    assert sorted(retired) == sorted((k, written[k]["version"]) for k in ("a", "c"))


@pytest.mark.asyncio
async def test_dry_run_counts_but_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    ch, tomb_keys = _stub(
        monkeypatch, [_row(memory_key="a"), _row(memory_key="b", on_expire="demote")]
    )
    res = await X.run_pass(X.ExpiryConfig(dry_run=True))
    assert res.dry_run and res.tombstoned == 1 and res.demoted == 1
    assert ch.inserts == [] and ch.executes == [] and tomb_keys == []


@pytest.mark.asyncio
async def test_update_by_id_refuses_an_expired_row_unless_revived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StubCH:
        def __init__(self) -> None:
            self.inserts: list[Any] = []

        async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
            self.inserts.append((table, rows))

        async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
            return []

        async def execute(self, *_: Any, **__: Any) -> None:
            return None

    ch = _StubCH()
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)
    row = _row(on_expire="demote")

    async def _get_by_id(*_: Any, **__: Any) -> MemoryRow:
        return row

    monkeypatch.setattr("server.memory.store.get_by_id", _get_by_id)

    monkeypatch.setattr("server.memory.store.namespaces_of_id", one_holder)

    async def _enforce(**_: Any) -> None:
        return None

    monkeypatch.setattr("server.memory.app.blacklist_guard.enforce_on_insert", _enforce)

    # The shared write path checks the new value for conflicts
    # and runs same-key supersession; both need ClickHouse, so keep them inert.
    from server.memory.app import conflicts as _cf
    from server.memory.app import key_supersession as _ks
    from server.memory.domain.conflicts import ConflictResult as _CR

    async def _clear(**_: Any) -> _CR:
        return _CR(verdict="clear", peers=[])

    async def _zero(**_: Any) -> int:
        return 0

    monkeypatch.setattr(_cf, "check_on_commit", _clear)
    monkeypatch.setattr(_ks, "supersede_same_key_rows", _zero)
    with pytest.raises(HTTPException) as e:
        await update_mod.handle_memory_update_by_id(
            MemoryUpdateByIdInput(org="test", memory_id=row.memory_id, value="edited")
        )
    assert e.value.status_code == 409
    d = e.value.detail
    assert d["code"] == "EXPIRED" and d["on_expire"] == "demote" and d["memory_key"] == "churn_note"
    assert ch.inserts == []
    out = await update_mod.handle_memory_update_by_id(
        MemoryUpdateByIdInput(org="test", memory_id=row.memory_id, value="edited", revive=True)
    )
    assert out.updated
    written = ch.inserts[0][1][0]
    assert written["expires_at"] is None and written["value"] == '"edited"'


def test_sweep_op_is_admin_and_worker_is_registered() -> None:
    from server.operations.base import Role
    from server.operations.registry import ALL_OPERATIONS
    from server.settings import Settings

    by_name = {o.name: o for o in ALL_OPERATIONS}
    assert by_name["memory.sweep_expired"].required_role == Role.ADMIN
    s = Settings()
    assert s.memory_expiry_sweep_enabled is True and s.memory_expiry_sweep_interval_seconds == 300


@pytest.mark.parametrize(
    "body",
    [
        {"org": "acme"},
        {"org": "acme", "namespace": ""},
        {"org": "acme", "namespace": "-"},
        {"org": "acme", "namespace": "  "},
    ],
)
def test_sweep_op_names_the_namespace_it_sweeps(body: dict[str, str]) -> None:
    """With no namespace the gate checked the caller's role only, and an admin of
    one namespace swept, and sampled, every namespace of the org. The op sweeps
    one named namespace, which the gate checks; the worker sweeps them all."""
    from pydantic import ValidationError

    from server.memory.api.models_crud import MemorySweepExpiredInput

    with pytest.raises(ValidationError):
        MemorySweepExpiredInput(**body)
    assert MemorySweepExpiredInput(org="acme", namespace="ns").namespace == "ns"


@pytest.mark.parametrize("named", ["ns-a", "ns-b"])
def test_the_gate_holds_a_sweep_to_the_namespace_the_admin_token_covers(
    monkeypatch: pytest.MonkeyPatch, named: str
) -> None:
    """Through the transport gate: an admin token of ns-a sweeps ns-a, and naming
    ns-b (where it holds no grant) is refused before the handler runs."""
    import asyncio

    from server.auth import authz
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role
    from server.operations.registry import ALL_OPERATIONS
    from server.transport import gates

    async def no_cap(*a: object, **k: object) -> None:
        return None

    async def standard(org: str, namespace: str) -> bool:
        return False

    async def no_grant(org: str, namespace: str, subject: str) -> None:
        return None

    async def admitted(*a: object, **k: object) -> None:
        return None

    monkeypatch.setattr(authz, "claim_cap", no_cap)
    monkeypatch.setattr(authz, "may_be_restricted", standard)
    monkeypatch.setattr(authz, "grant_role_for", no_grant)
    # What an admitted call leaves (the restricted-access audit, first-write
    # registration) writes to ClickHouse; the gate's decision is what is tested.
    monkeypatch.setattr(gates, "_admitted", admitted)
    op = next(o for o in ALL_OPERATIONS if o.name == "memory.sweep_expired")
    token = AuthInfo(
        subject="sa:ops",
        org="acme",
        namespaces=("ns-a",),
        roles=(Role.ADMIN,),
        is_service_account=True,
    )
    target = gates.authz_target(op, org="acme", namespace=named, auth=token)

    async def run() -> None:
        await gates.authorize(op, token, target, transport="http")

    if named == "ns-a":
        asyncio.run(run())
    else:
        with pytest.raises(PermissionError):
            asyncio.run(run())


# --- a namespace's table TTL owns its rows; loom sets no expiry ---


def test_there_is_no_age_based_sweep() -> None:
    """Loom keeps a memory until its writer asked otherwise: the sweep acts on
    an expiry a writer set, never on a row's age."""
    from server.memory import store as memory_repo
    from server.settings import settings as s

    assert not hasattr(memory_repo, "find_retention_candidates")
    assert "memory_retention_sweep_enabled" not in type(s).model_fields
    assert s.memory_sensitive_retention_days == 0 and s.memory_run_complete_retain == "keep"


@pytest.mark.asyncio
async def test_expired_candidates_leave_ttl_managed_namespaces_to_their_ttl() -> None:
    """A version row written in a namespace with `retention_days` is past that
    namespace's table TTL at birth and the next merge drops it — the live row
    stays, and a pass that read it would rewrite it in a loop. Both reads skip them."""
    from server.memory import store as memory_repo

    seen: list[str] = []

    class _CH:
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            seen.append(sql)
            return [{"1": 1}] if "SELECT 1" in sql else []

    await memory_repo.find_expired_candidates(batch_size=5, ch=_CH())
    assert len(seen) == 2
    for sql in seen:
        assert (
            "NOT IN (SELECT org, namespace FROM loom.namespaces FINAL WHERE retention_days > 0)"
            in sql
        )


@pytest.mark.asyncio
async def test_expired_candidates_probe_before_the_final_read() -> None:
    """An empty narrow probe ends the expiry read before the
    sixty-column FINAL projection is touched; a hit lets it run."""
    from server.memory import store as memory_repo

    class _ECH:
        def __init__(self, hit: bool) -> None:
            self.hit = hit
            self.sqls: list[str] = []

        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            self.sqls.append(sql)
            if "SELECT 1" in sql:
                assert "loom.memory FINAL" not in sql
                assert params is not None and "lim" not in params
                return [{"1": 1}] if self.hit else []
            return []

    quiet = _ECH(hit=False)
    assert await memory_repo.find_expired_candidates(batch_size=5, ch=quiet) == []
    assert len(quiet.sqls) == 1 and "SELECT 1" in quiet.sqls[0]

    due = _ECH(hit=True)
    await memory_repo.find_expired_candidates(batch_size=5, ch=due, org_filter="o")
    assert len(due.sqls) == 2
    assert "FINAL" in due.sqls[1] and "expires_at <= now()" in due.sqls[1]
    assert "org = {org_filter:String}" in due.sqls[0]  # the probe keeps the scope


class _BatchCH:
    """Answers the batched keyed read with one live row per requested key."""

    def __init__(self) -> None:
        self.reads: list[dict[str, Any]] = []
        self.inserts: list[list[dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        assert "IN arrayZip(" in sql and "FINAL" in sql and "tombstoned_at IS NULL" in sql
        self.reads.append(params or {})
        p = params or {}
        return [
            {
                "org": o,
                "namespace": n,
                "agent": a,
                "user_id": u,
                "session_id": s_,
                "memory_key": k,
                "scope": "private",
                "embedding_model": "m",
                "embedding": [0.1],
                "embedding_dim": 1,
                "confidence": 0.9,
                "expires_at": None,
                "tombstoned_at": None,
                "version": 1,
            }
            for o, n, a, u, s_, k in zip(
                p["o"], p["n"], p["a"], p["u"], p["s"], p["k"], strict=True
            )
        ]


@pytest.mark.asyncio
async def test_batched_reemit_reads_and_inserts_once_per_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1,200 keys = 3 keyed reads and 3 inserts at batch 500,
    each key re-emitted with its own overrides and a fresh version."""
    from server.memory import store as memory_repo
    from server.memory.store import embeddings

    ch = _BatchCH()

    async def _insert(rows, *, ch=None):
        ch_.inserts.append(rows)

    ch_ = ch
    monkeypatch.setattr(embeddings, "insert_embedding_rows", _insert)
    items = [
        (
            ("o", "n", "a", "u", "s", f"k{i}"),
            {"tombstoned_at": _NOW} if i % 2 else {"confidence": 0.4},
        )
        for i in range(1200)
    ]
    n = await memory_repo.reemit_embedding_rows_for_keys(items, ch=ch)
    assert n == 1200
    assert len(ch.reads) == 3 and [len(r) for r in ch.inserts] == [500, 500, 200]
    flat = {r["memory_key"]: r for batch in ch.inserts for r in batch}
    assert flat["k1"]["tombstoned_at"] == _NOW and flat["k2"]["confidence"] == 0.4
    assert all(r["version"] > 1 for r in flat.values())


@pytest.mark.asyncio
async def test_rows_whose_lock_refuses_their_expiry_are_left_out_before_the_limit() -> None:
    """The sweep skips a row whose lock refuses its post-action; if the batch
    held 500 of them it would re-read them every pass and never reach anyone
    else's expiries. Both reads exclude them, the lock rules mirrored."""
    from server.memory import store as memory_repo
    from server.memory.app.expiry import _LOCK_CLASS, ACTIONS, DEFAULT_ACTION
    from server.memory.domain import lock as memory_lock

    seen: list[str] = []

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            seen.append(" ".join(sql.split()))
            return [{"1": 1}] if "SELECT 1" in sql else []

    await memory_repo.find_expired_candidates(batch_size=5, ch=_CH())
    assert len(seen) == 2, seen
    for sql in seen:
        cut = sql.index("AND NOT (lock_mode")
        assert cut < sql.index("LIMIT")
        assert "(lock_until IS NULL OR lock_until > now())" in sql
    # the SQL's refusal table is memory_lock's: read_only refuses every action,
    # no_delete only the tombstone (the default action)
    assert DEFAULT_ACTION == "tombstone" and set(ACTIONS) == {"tombstone", "demote", "anonymize"}
    for action in ACTIONS:
        assert _LOCK_CLASS[action] in memory_lock.REFUSES["read_only"]
        assert (_LOCK_CLASS[action] in memory_lock.REFUSES["no_delete"]) == (action == "tombstone")


@pytest.mark.asyncio
async def test_a_candidate_is_expired_from_the_authoritys_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the Postgres authority each candidate is expired from the head Postgres
    holds: one renewed or deleted since is left alone and counted as moved, one only
    rewritten is still expired (its newer head past its expiry too), so a key whose
    delivery is parked is not passed over on every run."""
    rows = [_row(memory_key=k, version=100) for k in ("same", "renewed", "deleted", "rewritten")]
    _stub(monkeypatch, rows)
    heads = {
        "same": rows[0],
        "renewed": _row(
            memory_key="renewed", version=101, expires_at=_REAL_NOW + timedelta(days=1)
        ),
        "deleted": None,
        "rewritten": _row(memory_key="rewritten", version=101, value="newer"),
    }

    async def _authority(found: list[MemoryRow]) -> list[MemoryRow | None]:
        return [heads[r.memory_key] for r in found]

    monkeypatch.setattr("server.memory.store.lifecycle.authority_heads", _authority)
    res = await X.run_pass(X.ExpiryConfig(dry_run=True))
    assert (res.scanned, res.moved_skipped, res.tombstoned) == (4, 2, 2)
    assert sorted(s["memory_key"] for s in res.sample) == ["rewritten", "same"]
    events: list[Any] = []
    monkeypatch.setattr(
        "server.observability.events.get_emitter", lambda: SimpleNamespace(emit=events.append)
    )
    X.emit_sweep_event(res, latency_ms=1)
    assert events[0].args["moved_skipped"] == "2"


async def test_a_failed_scan_records_the_error_class_for_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The sweep covers every org and an org admin reads the registry's kind: a Loom
    # error's message can name another org, so the kind is the class.
    import server.memory.store as memory_repo
    from server.core.errors import NotFoundError
    from server.workers.status import get_registry

    async def _boom(**_kw: Any) -> list[MemoryRow]:
        raise NotFoundError("namespace other-org/secret is not configured")

    monkeypatch.setattr(memory_repo, "find_expired_candidates", _boom)
    await X.run_pass(X.ExpiryConfig())
    got = get_registry().snapshot()["memory_expiry_sweep"]
    assert got["last_error_kind"] == "NotFoundError"


async def test_a_failed_mirror_is_recorded_per_writer_and_the_repair_applies_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The next pass never selects a key the sweep wrote, so the mirror it missed is
    # recorded as a projection failure and replayed from there.
    from server.memory.app import projection_failures as pf
    from tests.unit.claims_fake import no_erasure

    rows = [
        _row(memory_key="a"),
        _row(memory_key="b", on_expire="demote", confidence=0.9),
        _row(memory_key="c", on_expire="demote", sensitivity="high"),  # keeps no vector
        _row(memory_key="d", user_id="u2"),  # another writer: its own failure
    ]
    ch, _ = _stub(monkeypatch, rows)

    async def _down(items, *, ch=None) -> int:
        raise ConnectionError("insert refused")

    monkeypatch.setattr("server.memory.store.retire_vectors_for_keys", _down)
    recorded: list[dict[str, Any]] = []

    async def _record(rows, *, ch=None) -> None:
        recorded.extend(rows)

    monkeypatch.setattr(pf, "insert_projection_failures", _record)
    res = await X.run_pass(X.ExpiryConfig())

    assert res.failures == ["mirror:3 keys: ConnectionError"]
    written = {r["memory_key"]: r["version"] for r in ch.inserts[0][1]}
    # One failure per writer, its keys' items in the payload.
    failure, other = recorded
    assert (failure["projection"], failure["memory_keys"]) == (pf.EXPIRY_MIRROR, ["a", "b"])
    assert (other["memory_keys"], json.loads(other["payload"])["user_id"]) == (["d"], "u2")
    assert json.loads(failure["payload"]) == {
        "items": [
            [["test", "personal", "cursor", "-", "-", "a"], written["a"], None],
            [
                ["test", "personal", "cursor", "-", "-", "b"],
                written["b"],
                {"expires_at": None, "confidence": 0.45},
            ],
        ],
        "user_id": "-",
    }

    # The repair replays each at the memory row's version: a later write's vectors,
    # at or above it, are left alone.
    retired.clear()
    _DEMOTE_REEMITS.clear()
    monkeypatch.setattr("server.memory.store.retire_vectors_for_keys", _retire_many_ok)
    no_erasure(monkeypatch)
    results = await pf.retry_failures([pf._row_from_ch(r) for r in recorded], ch=ch)

    assert [r.resolved for r in results] == [True, True]
    assert retired == [("a", written["a"]), ("d", written["d"])]
    assert _retire_clients[-2:] == [ch, ch]  # the repair's client, not the default
    assert _DEMOTE_REEMITS == [
        ("b", {"expires_at": None, "confidence": 0.45, "version": written["b"]})
    ]


_retire_clients: list[Any] = []


async def _retire_many_ok(items, *, ch=None) -> int:
    _retire_clients.append(ch)
    for (_o, _n, _a, _u, _s, key), version in items:
        retired.append((key, version))
    return len(items)


async def test_a_late_mirror_leaves_a_later_writes_vectors_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A replay runs after the sweep: only vector rows below the memory row's version
    # are copied or retired, never one a write since then landed.
    from server.memory.store import embeddings

    def _emb(key: str, version: int) -> dict[str, Any]:
        return {
            "org": "o",
            "namespace": "n",
            "agent": "a",
            "user_id": "-",
            "session_id": "-",
            "scope": "agent",
            "embedding_model": "m",
            "memory_key": key,
            "content": "text",
            "embedding": [0.1],
            "version": version,
            "created_at": _NOW,
        }

    live = [_emb("old", 100), _emb("new", 300), _emb("gone", 100), _emb("kept", 300)]

    async def _live(ch, keys, **_: Any) -> list[dict[str, Any]]:
        wanted = set(keys)
        return [r for r in live if embeddings.identity_key(r) in wanted]

    inserted: list[dict[str, Any]] = []

    async def _insert(rows, *, ch=None) -> None:
        inserted.extend(rows)

    async def _none(*_a: Any, **_k: Any) -> int:
        return 0

    cutoffs: list[dict[Any, datetime]] = []

    async def _entity_retire(keys, *, ts, ch=None, created_by=None) -> int:
        cutoffs.append(created_by)
        return 0

    monkeypatch.setattr(embeddings, "_live_rows_for_keys", _live)
    monkeypatch.setattr(embeddings, "insert_embedding_rows", _insert)
    monkeypatch.setattr(embeddings, "retire_copies_under_unembedded_heads", _none)
    monkeypatch.setattr(embeddings, "tombstone_entity_vectors_for_keys", _entity_retire)
    monkeypatch.setattr(embeddings, "_default_client", lambda: object())

    def key(k: str) -> tuple[str, str, str, str, str, str]:
        return ("o", "n", "a", "-", "-", k)

    demote = {"expires_at": None, "confidence": 0.4}
    await X.apply_mirror(
        [
            (key("old"), 200, demote),
            (key("new"), 200, demote),
            (key("gone"), 200, None),
            (key("kept"), 200, None),
        ]
    )

    got = {r["memory_key"]: r for r in inserted}
    assert set(got) == {"old", "gone"}
    assert got["old"]["version"] == 200 and got["old"]["confidence"] == 0.4
    assert got["old"]["expires_at"] is None and got["old"]["tombstoned_at"] is None
    assert got["gone"]["version"] == 200 and got["gone"]["tombstoned_at"] is not None
    # entity_vectors has no version: a retired key keeps the rows created after the
    # second of the memory row's version, a later write's.
    assert cutoffs == [
        {key("gone"): embeddings.version_second(200), key("kept"): embeddings.version_second(200)}
    ]


async def test_a_malformed_expiry_mirror_failure_is_abandoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import projection_failures as pf
    from tests.unit.claims_fake import no_erasure

    no_erasure(monkeypatch)

    def failure(item: list[Any]) -> Any:
        return pf.ProjectionFailureRow(
            failure_id="f1",
            org="o",
            namespace="n",
            agent="a",
            projection=pf.EXPIRY_MIRROR,
            source_operation="memory.sweep_expired",
            source_resource_id="",
            memory_keys=["k"],
            error_type="ConnectionError",
            error_message="insert refused",
            payload={"items": [item]},
        )

    for item in (
        [["o", "n", "k"], 200, None],  # not an identity key
        [["o", "n", "a", "-", "-", "k"], 200, [1]],  # not a demote's overrides
    ):
        result = await pf._retry(failure(item), ch=object())
        assert (result.abandoned, result.reason) == (True, "malformed_payload")
