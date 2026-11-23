"""Restore finds generic connection history without opening credentials or payloads."""

from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from server.core.errors import ValidationError
from server.knowledge.app import connected_restore as restore
from server.knowledge.domain.sources import KnowledgeSource

SLOT = "mcp:" + "a" * 32
OTHER_SLOT = "mcp:" + "b" * 32
CONNECTION = "c" * 32 + "/1" + "d" * 31
OWNER = ("org-one", "owner-one", SLOT)


def test_restore_keeps_owner_and_slot_identity_separate() -> None:
    groups: dict[Any, set[str]] = defaultdict(set)
    prefixes: dict[str, Any] = {}
    rows = [
        (*OWNER, CONNECTION),
        ("org-one", "owner-one", OTHER_SLOT, "e" * 32 + "/1" + "f" * 31),
        ("org-one", "owner-two", SLOT, "0" * 32 + "/1" + "1" * 31),
    ]
    restore._include(groups, prefixes, rows)
    assert set(groups) == {row[:3] for row in rows}
    assert all(groups[row[:3]] == {row[3]} for row in rows)
    assert len(prefixes) == 3


@pytest.mark.parametrize("slot", ["mcp:", "mcp:abc", "mcp:" + "A" * 32, SLOT + "/other"])
def test_restore_refuses_malformed_generic_slot(slot: str) -> None:
    with pytest.raises(ValidationError, match="the connected account is unavailable"):
        restore._include(defaultdict(set), {}, [("org-one", "owner-one", slot, CONNECTION)])


def test_restore_still_refuses_a_fence_prefix_shared_by_two_owners() -> None:
    groups: dict[Any, set[str]] = defaultdict(set)
    prefixes: dict[str, Any] = {}
    with pytest.raises(ValidationError, match="the connected account is unavailable"):
        restore._include(
            groups,
            prefixes,
            [(*OWNER, CONNECTION), ("org-one", "owner-two", SLOT, CONNECTION)],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, OWNER])
async def test_source_only_restore_history_uses_slot_without_decoding_payload(
    monkeypatch: pytest.MonkeyPatch, owner: tuple[str, str, str] | None
) -> None:
    source = SimpleNamespace(
        org=OWNER[0],
        namespace="collection-one",
        name="selected-documents",
        source_id="source-one",
        owner_subject=OWNER[1],
        kind="mcp",
        credential_mode="mcp",
        connection_slot=SLOT,
        connection_id=CONNECTION,
        source_contract_version=65535,
        generic_config_json="unparseable payload",
        generic_items_json="unparseable selection",
    )

    async def no_accounts(**kwargs: Any) -> list[Any]:
        assert kwargs["owner"] == owner
        return []

    async def history(**kwargs: Any) -> list[Any]:
        assert kwargs["owner"] == owner
        return [source]

    monkeypatch.setattr(restore.account_store, "restore_keys", no_accounts)
    monkeypatch.setattr(restore.source_store, "connected_history", history)
    keys, sources = await restore._metadata(object(), owner=owner)
    assert keys == [(*OWNER, CONNECTION)]
    assert sources == [source]


@pytest.mark.asyncio
@pytest.mark.parametrize("generic", [True, False])
@pytest.mark.parametrize("changed", ["same", "slot", "owner", "source"])
async def test_restore_inactivates_only_current_exact_account_binding(
    monkeypatch: pytest.MonkeyPatch, generic: bool, changed: str
) -> None:
    source = KnowledgeSource(
        "org-one",
        "collection-one",
        "selected-documents",
        "mcp" if generic else "gdrive",
        source_id="source-one",
        credential_mode="mcp" if generic else "connected",
        owner_subject="owner-one",
        connection_slot=SLOT if generic else "",
        connection_id=CONNECTION,
        source_contract_version=65535 if generic else 0,
        generic_config_json="opaque corrupt config",
        generic_items_json="opaque corrupt items",
        credential_enc="sealed-envelope",
        secret_enc="sealed-secret",
        active=True,
    )
    changes: dict[str, Any] = {}
    if changed == "slot":
        changes = {"connection_slot": OTHER_SLOT} if generic else {"kind": "notion"}
    elif changed == "owner":
        changes = {"owner_subject": "owner-two"}
    elif changed == "source":
        changes = {"source_id": "source-two"}
    current = replace(source, **changes)
    written: list[KnowledgeSource] = []
    client = object()

    @asynccontextmanager
    async def lease(org: str, namespace: str) -> AsyncIterator[None]:
        assert (org, namespace) == (source.org, source.namespace)
        yield

    async def latest(org: str, namespace: str, name: str, *, ch: Any) -> KnowledgeSource:
        assert (org, namespace, name, ch) == (source.org, source.namespace, source.name, client)
        return current

    async def put(value: KnowledgeSource, *, ch: Any) -> None:
        assert ch is client
        written.append(value)

    queue = AsyncMock(return_value=True)
    retire = AsyncMock()
    monkeypatch.setattr(restore.source_ops, "sources_lease", lease)
    monkeypatch.setattr(restore.source_ops, "_queue_retirement", queue)
    monkeypatch.setattr(restore.source_push, "retire", retire)
    monkeypatch.setattr(restore.source_store, "latest_source", latest)
    monkeypatch.setattr(restore.source_store, "insert_source", put)
    await restore._retire_history([source], client)
    queue.assert_awaited_once_with(source)
    retire.assert_awaited_once_with(source)
    if changed == "same":
        assert written == [
            replace(current, active=False, credential_enc="", secret_enc="", pending_delete=True)
        ]
        assert written[0].generic_config_json == source.generic_config_json
        assert written[0].generic_items_json == source.generic_items_json
        assert written[0].connection_slot == source.connection_slot
        assert written[0].source_contract_version == source.source_contract_version
    else:
        assert written == []
    assert source.active and source.credential_enc == "sealed-envelope"


class _MarkerWritten(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [SLOT, "gdrive"])
async def test_retry_reuses_valid_stopped_fence_without_same_value_cas(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    accounts = restore.accounts
    account = restore.ConnectedAccount(
        "org-one", "owner-one", kind, CONNECTION, credential_enc="unreadable-envelope", active=True
    )
    landed = CONNECTION.partition("/")[0] + "/0" + "e" * 31
    client = object()
    get = AsyncMock(return_value=landed)
    swap = AsyncMock(side_effect=AssertionError("same-value CAS is unsupported"))
    written: list[Any] = []

    async def put(value: Any) -> None:
        written.append(value)
        raise _MarkerWritten

    monkeypatch.setattr(accounts, "get_client", lambda: client)
    monkeypatch.setattr(accounts.claims, "get", get)
    monkeypatch.setattr(accounts.claims, "swap", swap)
    monkeypatch.setattr(accounts.store, "put", put)
    with pytest.raises(_MarkerWritten):
        await accounts._stop_locked(account)
    get.assert_awaited_once_with(client, accounts._fence_key(CONNECTION))
    swap.assert_not_awaited()
    assert len(written) == 1
    assert written[0].connection_id == landed
    assert not written[0].active
    assert written[0].credential_enc == written[0].account_email_enc == ""
    assert account.active and account.credential_enc == "unreadable-envelope"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "strict,previous",
    [
        (False, "other-prefix"),
        (False, "different-live"),
        (True, "other-prefix"),
        (True, "different-live"),
        (True, "stopped"),
    ],
)
async def test_stop_refuses_foreign_or_live_fences_and_strict_recovery(
    monkeypatch: pytest.MonkeyPatch, strict: bool, previous: str
) -> None:
    accounts = restore.accounts
    account = restore.ConnectedAccount("org-one", "owner-one", SLOT, CONNECTION, active=True)
    prefix = "f" * 32 if previous == "other-prefix" else CONNECTION.partition("/")[0]
    tail = "1" if previous == "different-live" else "0"
    observed = prefix + "/" + tail + "e" * 31
    client = object()
    get = AsyncMock(return_value=observed)
    swap = AsyncMock()
    put = AsyncMock()
    monkeypatch.setattr(accounts, "get_client", lambda: client)
    monkeypatch.setattr(accounts.claims, "get", get)
    monkeypatch.setattr(accounts.claims, "swap", swap)
    monkeypatch.setattr(accounts.store, "put", put)
    with pytest.raises(ValidationError, match="the connected account is unavailable"):
        await accounts._stop_locked(account, strict=strict)
    swap.assert_not_awaited()
    put.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("already_stopped", [False, True])
async def test_restore_reuses_durable_inactive_fence_without_same_value_cas(
    monkeypatch: pytest.MonkeyPatch, already_stopped: bool
) -> None:
    landed = CONNECTION.partition("/")[0] + "/0" + "e" * 31
    connection = landed if already_stopped else CONNECTION
    client = object()
    get = AsyncMock(return_value=landed)
    swap = AsyncMock(side_effect=AssertionError("same-value CAS is unsupported"))
    monkeypatch.setattr(restore.claims, "get", get)
    monkeypatch.setattr(restore.claims, "swap", swap)
    assert await restore._stop_key(connection, client) == landed
    get.assert_awaited_once_with(client, restore.accounts._fence_key(connection))
    swap.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [SLOT, "mcp", "unknown-source"])
async def test_generic_credential_never_enters_native_revocation_factory(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    factory = Mock(
        side_effect=AssertionError("native revocation factory must not receive this kind")
    )
    monkeypatch.setattr(restore.accounts, "client", factory)
    account = restore.ConnectedAccount("org-one", "owner-one", kind, CONNECTION)
    await restore.accounts._revoke(account, credential="nonempty-generic-envelope")
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_native_revocation_control_still_uses_its_existing_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revoke = AsyncMock()
    factory = Mock(return_value=SimpleNamespace(revoke=revoke))
    monkeypatch.setattr(restore.accounts, "client", factory)
    account = restore.ConnectedAccount("org-one", "owner-one", "gdrive", CONNECTION)
    await restore.accounts._revoke(account, credential="fixture-native-credential")
    factory.assert_called_once_with("gdrive")
    revoke.assert_awaited_once()
    assert revoke.await_args.args == ("fixture-native-credential",)
    assert revoke.await_args.kwargs["refresh"] is True


@pytest.fixture
def owner_history(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """I/O seams retain identities and lease order; the lifecycle handlers stay real."""
    accounts = restore.accounts
    state = SimpleNamespace(
        history={}, rows={}, fences={}, events=[], held=[], cutoffs={"owner-one": None}
    )

    @asynccontextmanager
    async def account_lease(*owner: str, **kwargs: Any) -> AsyncIterator[None]:
        assert not state.held
        state.held.append(owner)
        try:
            yield
        finally:
            state.held.pop()

    async def owned(subjects: list[str], *, org: str | None = None, **kwargs: Any) -> list[Any]:
        return [v for k, v in state.rows.items() if k[1] in subjects and org in (None, k[0])]

    async def source_owners(
        subjects: list[str] | None = None, *, org: str | None = None, **kwargs: Any
    ) -> list[Any]:
        return [
            k
            for k in state.history
            if org in (None, k[0]) and (subjects is None or k[1] in subjects)
        ]

    async def identities(*, org: str | None = None, **kwargs: Any) -> list[Any]:
        return sorted({k[:2] for k in state.rows if org in (None, k[0])})

    async def get(*owner: str, **kwargs: Any) -> Any:
        return state.rows.get(owner)

    async def history(*, owner: Any = None, **kwargs: Any) -> list[Any]:
        assert owner is not None
        return state.history.get(owner, [])

    async def keys(*, owner: Any = None, **kwargs: Any) -> list[Any]:
        row = state.rows.get(owner)
        return [(*owner, row.connection_id)] if row else []

    monkeypatch.setattr(accounts, "get_client", lambda: state)
    monkeypatch.setattr(accounts, "account_lease", account_lease)
    monkeypatch.setattr(accounts.store, "owned", owned)
    monkeypatch.setattr(accounts.store, "owner_identities", identities)
    monkeypatch.setattr(accounts.store, "get", get)
    monkeypatch.setattr(accounts.store, "restore_keys", keys)
    monkeypatch.setattr(restore.source_store, "connected_owners", source_owners, raising=False)
    monkeypatch.setattr(restore.source_store, "connected_history", history)
    monkeypatch.setattr(
        accounts.aliases, "verified_spellings", AsyncMock(side_effect=lambda o, s: [s])
    )
    monkeypatch.setattr(
        accounts.revocation, "cutoffs", AsyncMock(side_effect=lambda *a, **k: state.cutoffs)
    )
    monkeypatch.setattr(accounts, "unseal", Mock(side_effect=AssertionError("unseal not required")))
    monkeypatch.setattr(restore, "unseal", Mock(side_effect=AssertionError("unseal not required")))
    monkeypatch.setattr(accounts, "_revoke", AsyncMock())
    _wire_owner_history_writes(monkeypatch, state)
    return state


def _wire_owner_history_writes(monkeypatch: pytest.MonkeyPatch, state: SimpleNamespace) -> None:
    async def fence_get(ch: Any, key: str) -> str | None:
        assert ch is state
        return state.fences.get(key)

    async def swap(ch: Any, key: str, old: Any, new: str, expiry: int) -> str:
        assert state.held and state.fences.get(key) == old and old != new
        state.fences[key] = new
        state.events.append(("fence", state.held[0]))
        return new

    async def put(account: Any, **kwargs: Any) -> None:
        owner = account.org, account.subject, account.kind
        assert state.held == [owner] and not account.active and not account.credential_enc
        state.rows[owner] = account
        state.events.append(("marker", owner))

    async def retire(sources: list[Any], ch: Any) -> None:
        assert ch is state
        for source in sources:
            owner = source.org, source.owner_subject, source.connection_slot or source.kind
            assert state.held == [owner]
            assert (
                state.fences[restore.accounts._fence_key(source.connection_id)].split("/")[1][0]
                == "0"
            )
            state.events.append(("retire", owner))

    async def erase(account: Any, *, keep_marker: bool) -> None:
        owner = account.org, account.subject, account.kind
        assert not keep_marker and state.held == [owner]
        assert ("retire", owner) in state.events
        state.history.pop(owner, None)
        state.rows.pop(owner, None)
        state.events.append(("erase", owner))

    monkeypatch.setattr(restore.claims, "get", fence_get)
    monkeypatch.setattr(restore.claims, "swap", swap)
    monkeypatch.setattr(restore.account_store, "put", put)
    monkeypatch.setattr(restore, "_retire_history", retire)
    monkeypatch.setattr(restore.accounts, "_erase_locked", erase)


def _owner_source(owner: tuple[str, str, str], prefix: str) -> KnowledgeSource:
    return KnowledgeSource(
        owner[0],
        "collection-one",
        "selected",
        "mcp",
        source_id="source-" + prefix,
        credential_mode="mcp",
        owner_subject=owner[1],
        connection_slot=owner[2],
        connection_id=prefix * 32 + "/1" + "d" * 31,
        source_contract_version=65535,
        generic_config_json="unknown {",
        generic_items_json="corrupt [",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("org", [None, "org-one"])
@pytest.mark.parametrize("respect_cutoff", [False, True])
async def test_owner_erasure_discovers_source_only_history_without_accounts(
    owner_history: SimpleNamespace, org: str | None, respect_cutoff: bool
) -> None:
    state = owner_history
    owners = [OWNER, ("org-two", OWNER[1], OTHER_SLOT), (OWNER[0], "neighbor", SLOT)]
    state.history = {
        owner: [_owner_source(owner, prefix)] for owner, prefix in zip(owners, "abc", strict=True)
    }
    retained = {
        k: v for k, v in state.history.items() if k[1] != OWNER[1] or org not in (None, k[0])
    }
    await restore.accounts.stop_subjects(
        [OWNER[1]], org=org, erase_owner=True, respect_cutoff=respect_cutoff, revoke=False
    )
    assert state.history == retained
    selected = set(owners) - set(retained)
    assert {owner for event, owner in state.events if event == "erase"} == selected
    assert not state.rows and len(state.fences) == len(selected)
    restore.unseal.assert_not_called()
    restore.accounts.unseal.assert_not_called()


@pytest.mark.asyncio
async def test_owner_erasure_refuses_dated_source_only_standing(
    owner_history: SimpleNamespace,
) -> None:
    state = owner_history
    state.history = {OWNER: [_owner_source(OWNER, "a")]}
    state.cutoffs = {OWNER[1]: 200}
    with pytest.raises(ValidationError, match="the connected account is unavailable"):
        await restore.accounts.stop_subjects(
            [OWNER[1]], erase_owner=True, respect_cutoff=True, revoke=False
        )
    assert OWNER in state.history and not state.events


@pytest.mark.asyncio
@pytest.mark.parametrize("cutoffs", [{}, {OWNER[1]: 100}, {OWNER[1]: 200}])
async def test_owner_erasure_keeps_lifted_or_newer_account_standing(
    owner_history: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, cutoffs: dict[str, int | None]
) -> None:
    from datetime import UTC, datetime

    state = owner_history
    state.history = {OWNER: [_owner_source(OWNER, "a")]}
    state.rows[OWNER] = restore.ConnectedAccount(
        *OWNER, CONNECTION, active=True, connected_at=datetime.fromtimestamp(200, UTC)
    )
    state.cutoffs = cutoffs
    # The inventory saw an older account; the account lease must protect the re-read authority.
    stale = replace(state.rows[OWNER], connected_at=datetime.fromtimestamp(50, UTC))
    monkeypatch.setattr(restore.account_store, "owned", AsyncMock(return_value=[stale]))
    await restore.accounts.stop_subjects(
        [OWNER[1]], erase_owner=True, respect_cutoff=True, revoke=False
    )
    assert OWNER in state.history and OWNER in state.rows and not state.events


@pytest.mark.asyncio
async def test_owner_erasure_keeps_source_only_history_when_cutoff_is_lifted(
    owner_history: SimpleNamespace,
) -> None:
    state = owner_history
    state.history = {OWNER: [_owner_source(OWNER, "a")]}
    state.cutoffs = {}
    await restore.accounts.stop_subjects([OWNER[1]], erase_owner=True, respect_cutoff=True)
    assert OWNER in state.history and not state.events


@pytest.mark.asyncio
async def test_owner_erasure_alias_is_verified_separately_in_each_org(
    owner_history: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = owner_history
    other = ("org-two", OWNER[1], OTHER_SLOT)
    state.history = {OWNER: [_owner_source(OWNER, "a")], other: [_owner_source(other, "b")]}

    async def spellings(org: str, subject: str) -> list[str]:
        assert subject == OWNER[1]
        return [subject, "verified-alias"] if org == OWNER[0] else [subject]

    monkeypatch.setattr(restore.accounts.aliases, "verified_spellings", spellings)
    await restore.accounts.stop_subjects(
        ["verified-alias"], erase_owner=True, respect_cutoff=True, revoke=False
    )
    assert set(state.history) == {other}
    assert {owner for event, owner in state.events if event == "erase"} == {OWNER}


@pytest.mark.asyncio
async def test_owner_erasure_retries_marker_after_retirement_before_erase_failure(
    owner_history: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = owner_history
    state.history = {OWNER: [_owner_source(OWNER, "a")]}
    erase = restore.accounts._erase_locked
    attempted = False

    async def interrupted(account: Any, *, keep_marker: bool) -> None:
        nonlocal attempted
        if not attempted:
            attempted = True
            assert not keep_marker and ("retire", OWNER) in state.events
            raise _MarkerWritten
        await erase(account, keep_marker=keep_marker)

    monkeypatch.setattr(restore.accounts, "_erase_locked", interrupted)
    with pytest.raises(_MarkerWritten):
        await restore.accounts.stop_subjects(
            [OWNER[1]], erase_owner=True, respect_cutoff=True, revoke=False
        )
    stopped = dict(state.fences)
    assert OWNER in state.history and OWNER in state.rows
    await restore.accounts.stop_subjects(
        [OWNER[1]], erase_owner=True, respect_cutoff=True, revoke=False
    )
    assert not state.history and not state.rows and state.fences == stopped
    assert state.events.count(("fence", OWNER)) == 1


@pytest.mark.asyncio
async def test_owner_erasure_revokes_only_after_all_local_history_is_retired(
    owner_history: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = owner_history
    native = (OWNER[0], OWNER[1], "gdrive")
    state.rows[OWNER] = restore.ConnectedAccount(*OWNER, CONNECTION, active=True)
    state.rows[native] = restore.ConnectedAccount(*native, "b" * 32 + "/1" + "e" * 31, active=True)
    state.history = {
        OWNER: [_owner_source(OWNER, "a")],
        native: [
            replace(
                _owner_source(native, "b"),
                kind="gdrive",
                credential_mode="connected",
                connection_slot="",
            )
        ],
    }
    revoked = []

    async def revoke(account: Any, **kwargs: Any) -> None:
        assert not state.history and not state.rows and not state.held
        revoked.append((account.org, account.subject, account.kind))

    monkeypatch.setattr(restore.accounts, "_revoke", revoke)
    await restore.accounts.stop_subjects([OWNER[1]], erase_owner=True, respect_cutoff=True)
    assert set(revoked) == {OWNER, native}
    assert all(
        event != "erase" or ("retire", owner) in state.events for event, owner in state.events
    )


@pytest.mark.asyncio
async def test_owner_erasure_refuses_aggregate_inventory_overflow_before_fencing(
    owner_history: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = owner_history
    source = _owner_source(OWNER, "a")
    state.history = {OWNER: [source, replace(source, connection_id="b" * 32 + "/1" + "e" * 31)]}
    monkeypatch.setattr(restore.settings, "knowledge_connected_registry_scan_limit", 1)
    with pytest.raises(ValidationError, match="the connected account is unavailable"):
        await restore.accounts.stop_subjects([OWNER[1]], erase_owner=True, revoke=False)
    assert not state.events and OWNER in state.history


@pytest.mark.asyncio
@pytest.mark.parametrize("overflow", [False, True])
async def test_owner_history_identity_query_is_bounded_scoped_and_payload_free(
    monkeypatch: pytest.MonkeyPatch, overflow: bool
) -> None:
    statements = []

    async def query(sql: str, *, params: dict[str, Any]) -> list[dict[str, str]]:
        statements.append(sql)
        assert "org = {org:String}" in sql and "owner_subject IN {subjects:Array(String)}" in sql
        assert " FINAL" not in sql
        assert all(name not in sql for name in ("config_json", "items_json", "credential_enc"))
        assert params == {"org": OWNER[0], "subjects": [OWNER[1]], "limit": 2}
        return [{"org": OWNER[0], "owner_subject": OWNER[1], "slot": SLOT}] * (2 if overflow else 1)

    monkeypatch.setattr(restore.settings, "knowledge_connected_registry_scan_limit", 1)
    ch = SimpleNamespace(query_all=query)
    if overflow:
        with pytest.raises(RuntimeError, match="connected_source_owner_bound"):
            await restore.source_store.connected_owners([OWNER[1]], org=OWNER[0], ch=ch)
    else:
        assert await restore.source_store.connected_owners([OWNER[1]], org=OWNER[0], ch=ch) == [
            OWNER
        ]
    assert len(statements) == 1


@pytest.mark.asyncio
async def test_owner_erasure_refuses_history_growth_past_bound_under_lease(
    owner_history: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = owner_history
    source = _owner_source(OWNER, "a")
    calls = 0

    async def history(*, owner: Any = None, **kwargs: Any) -> list[Any]:
        nonlocal calls
        assert owner == OWNER
        calls += 1
        if calls == 1:
            assert not state.held
            return [source]
        assert state.held == [OWNER]
        return [source, replace(source, connection_id="b" * 32 + "/1" + "e" * 31)]

    state.history = {OWNER: [source]}
    monkeypatch.setattr(restore.source_store, "connected_history", history)
    monkeypatch.setattr(restore.settings, "knowledge_connected_registry_scan_limit", 1)
    with pytest.raises(ValidationError, match="the connected account is unavailable"):
        await restore.accounts.stop_subjects([OWNER[1]], erase_owner=True, revoke=False)
    assert calls == 2 and not state.events


@pytest.mark.asyncio
async def test_owner_erasure_bounds_generations_sharing_each_owners_prefix(
    owner_history: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = owner_history
    owners = [OWNER, (OWNER[0], "owner-two", SLOT)]
    for owner, prefix in zip(owners, "ab", strict=True):
        source = _owner_source(owner, prefix)
        state.history[owner] = [
            source,
            replace(source, connection_id=prefix * 32 + "/1" + "e" * 31),
        ]
    monkeypatch.setattr(restore.settings, "knowledge_connected_registry_scan_limit", 2)
    with pytest.raises(ValidationError, match="the connected account is unavailable"):
        await restore.accounts.stop_subjects(
            [owner[1] for owner in owners], erase_owner=True, revoke=False
        )
    assert not state.events and not state.fences and not state.rows
    assert set(state.history) == set(owners)


@pytest.mark.asyncio
async def test_owner_erasure_bounds_new_generation_on_final_owners_existing_prefix(
    owner_history: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = owner_history
    owners = [OWNER, (OWNER[0], "owner-two", SLOT)]
    state.history = {
        owner: [_owner_source(owner, prefix)] for owner, prefix in zip(owners, "ab", strict=True)
    }
    calls: dict[Any, int] = defaultdict(int)

    async def history(*, owner: Any = None, **kwargs: Any) -> list[Any]:
        calls[owner] += 1
        sources = list(state.history[owner])
        if owner == owners[1] and calls[owner] == 2:
            assert state.held == [owner] and ("erase", owners[0]) in state.events
            sources.append(replace(sources[0], connection_id="b" * 32 + "/1" + "e" * 31))
        assert len(sources) <= 2
        return sources

    monkeypatch.setattr(restore.source_store, "connected_history", history)
    monkeypatch.setattr(restore.settings, "knowledge_connected_registry_scan_limit", 2)
    with pytest.raises(ValidationError, match="the connected account is unavailable"):
        await restore.accounts.stop_subjects(
            [owner[1] for owner in owners], erase_owner=True, revoke=False
        )
    assert ("erase", owners[0]) in state.events
    assert not any(owner == owners[1] for _event, owner in state.events)
    assert set(state.history) == {owners[1]}


@pytest.mark.asyncio
async def test_offline_restore_dry_run_bounds_generations_not_only_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owners = [OWNER, (OWNER[0], "owner-two", SLOT)]
    keys = [
        (*owner, prefix * 32 + "/1" + tail * 31)
        for owner, prefix in zip(owners, "ab", strict=True)
        for tail in "de"
    ]
    client = object()
    monkeypatch.setattr(restore.ch_module, "get_client", lambda: client)
    monkeypatch.setattr(restore, "offline_refusal", AsyncMock(return_value=""))
    monkeypatch.setattr(restore, "_metadata", AsyncMock(return_value=(keys, [])))
    monkeypatch.setattr(restore.settings, "knowledge_connected_registry_scan_limit", 2)
    mutate = AsyncMock(side_effect=AssertionError("dry run must not mutate"))
    monkeypatch.setattr(restore, "_restore_owner", mutate)
    with pytest.raises(ValidationError, match="the connected account is unavailable"):
        await restore.restore_all(apply=False, ch=client)
    mutate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("generic", [False, True])
async def test_restore_inventory_normalizes_driver_uuid_without_weakening_binding(
    monkeypatch: pytest.MonkeyPatch, generic: bool
) -> None:
    source_id = UUID("abcd1234-1234-4234-8234-123456789abc")

    async def query(sql: str, *, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "org": OWNER[0],
                "namespace": "collection-one",
                "name": "selected-documents",
                "source_id": source_id,
                "source_kind": "mcp" if generic else "gdrive",
                "owner_subject": OWNER[1],
                "connection_id": CONNECTION,
                "credential_mode": "mcp" if generic else "connected",
                "connection_slot": SLOT if generic else "",
                "source_contract_version": 65535 if generic else 0,
            }
        ]

    monkeypatch.setattr(restore.account_store, "restore_keys", AsyncMock(return_value=[]))
    keys, sources = await restore._metadata(SimpleNamespace(query_all=query))
    assert keys == [(OWNER[0], OWNER[1], SLOT if generic else "gdrive", CONNECTION)]
    assert len(sources) == 1 and sources[0].source_id == str(source_id)
    assert isinstance(sources[0].source_id, str)
