from dataclasses import replace
from hashlib import sha256
from uuid import UUID

import pytest

from server.knowledge.domain.mcp_identity import ResourceIdentity, selection_sha256
from server.knowledge.domain.mcp_worker_permissions import (
    ReaderConnection,
    StoredCopyRevision,
    VerifiedWorkerReadEvidence,
    can_read_stored_copy,
)
from server.knowledge.domain.mcp_worker_setup import (
    MAX_SELECTION_GRANT_AGE_SECONDS,
    MAX_SETUP_RESOURCES,
    ApprovedWorkerAccount,
    SetupCaller,
    SetupDestination,
    SetupIntent,
    SetupState,
    VerifiedSelectionGrant,
    WorkerSetupRejected,
    confirm_worker_setup,
    plan_worker_setup,
)
from server.knowledge.domain.mcp_worker_source import decode_worker_source, encode_worker_source

NOW = 1_800_000_000


def _resource(identifier: str) -> ResourceIdentity:
    return ResourceIdentity(type="resource", id=identifier)


@pytest.fixture
def intent():
    return SetupIntent((_resource("doc:one"), _resource("doc:two")), "snapshot")


def _grant(account: ApprovedWorkerAccount, intent: SetupIntent) -> VerifiedSelectionGrant:
    return VerifiedSelectionGrant(
        account.org,
        account.connection,
        account.approval_generation,
        selection_sha256(intent.selection),
        NOW - 1,
        NOW + 10,
        "team",
        UUID(int=5),
        UUID(int=8),
        9,
    )


@pytest.fixture
def state(intent):
    caller = SetupCaller("org", "alice", "assistant")
    account = ApprovedWorkerAccount(
        "org",
        ReaderConnection("alice", UUID(int=1), UUID(int=2), 3, "a" * 64),
        approval_generation=4,
        active=True,
        modes=frozenset(("live", "snapshot", "selected_refresh")),
        verified_resource_space="realm",
        exact_reader_checks=True,
    )
    return SetupState(
        caller=caller,
        destination=SetupDestination(
            "org",
            "team",
            UUID(int=5),
            "team",
            6,
            7,
            caller,
            ordinary=True,
            can_write=True,
            team_approved=True,
        ),
        account=account,
        selection_grant=_grant(account, intent),
        source_id=UUID(int=8),
        selection_generation=9,
        policy_generation=10,
        now=NOW,
        store_qualified=True,
        erasure_qualified=True,
    )


def _bob_state(state: SetupState, intent: SetupIntent) -> SetupState:
    caller = replace(state.caller, subject="bob")
    account = replace(
        state.account,
        connection=replace(state.account.connection, owner="bob", connection_id=UUID(int=11)),
    )
    return replace(
        state,
        caller=caller,
        destination=replace(state.destination, grantee=caller),
        account=account,
        selection_grant=_grant(account, intent),
    )


def test_confirmation_recomputes_registration_without_accepting_preview_fields(state, intent):
    plan = plan_worker_setup(state, intent)
    current = replace(state, now=NOW + 1)
    assert plan.preview.expires_at == state.selection_grant.expires_at
    confirmed = confirm_worker_setup(plan.fingerprint, state=current, intent=intent)
    assert confirmed == plan
    assert confirmed is not plan
    row = encode_worker_source(
        org=confirmed.caller.org,
        namespace=confirmed.namespace,
        source_id=confirmed.source_id,
        owner=confirmed.caller.subject,
        name="selection",
        config=confirmed.config,
        selection=confirmed.selection,
    )
    assert decode_worker_source(row) == (confirmed.config, intent.selection)


def test_bob_needs_own_account_and_exact_selection_grant(state, intent):
    plan = plan_worker_setup(state, intent)
    bob = _bob_state(state, intent)
    with pytest.raises(WorkerSetupRejected, match="^authority_unavailable$"):
        plan_worker_setup(replace(bob, account=state.account), intent)
    with pytest.raises(WorkerSetupRejected, match="^selection_unverified$"):
        plan_worker_setup(replace(bob, selection_grant=state.selection_grant), intent)
    assert (
        plan_worker_setup(bob, intent).config.owner_connection_id != plan.config.owner_connection_id
    )
    with pytest.raises(WorkerSetupRejected, match="^stale_preview$"):
        confirm_worker_setup(plan.fingerprint, state=bob, intent=intent)


def test_personal_namespace_stays_owner_only_even_when_team_requested(state, intent):
    personal = replace(
        state.destination, kind="automatic_personal", personal_owner="alice", namespace="personal"
    )
    state = replace(
        state,
        destination=personal,
        selection_grant=replace(state.selection_grant, namespace=personal.namespace),
    )
    plan = plan_worker_setup(state, replace(intent, share_with_team=True))
    assert plan.config.reader_policy == plan.preview.reader_policy == "owner_only"
    assert plan.preview.requires_collection_grant
    bob = _bob_state(state, intent)
    with pytest.raises(WorkerSetupRejected, match="^authority_unavailable$"):
        plan_worker_setup(bob, intent)


def test_team_policy_requires_opt_in_and_does_not_grant_readers_access(state, intent):
    assert plan_worker_setup(state, intent).preview.reader_policy == "owner_only"
    plan = plan_worker_setup(state, replace(intent, share_with_team=True))
    assert plan.config.reader_policy == plan.preview.reader_policy == "current_team_access"
    assert plan.preview.requires_collection_grant
    assert plan.preview.requires_exact_reader_evidence
    assert not plan.preview.syncs_upstream_acls


def test_team_preview_and_owner_grant_do_not_replace_bobs_exact_read_evidence(state, intent):
    plan = plan_worker_setup(state, replace(intent, share_with_team=True))
    revision = StoredCopyRevision(
        plan.caller.org,
        plan.namespace,
        plan.source_id,
        plan.config.selection_generation,
        sha256(plan.selection[0].id.encode()).hexdigest(),
        UUID(int=12),
        "b" * 64,
    )
    alice_proof = VerifiedWorkerReadEvidence(revision, state.account.connection, NOW, NOW, NOW + 1)
    bob = _bob_state(state, intent).account.connection
    bob_proof = replace(alice_proof, connection=bob)

    def check(granted, proof):
        return can_read_stored_copy(
            collection_granted=granted,
            requester=bob.owner,
            current_revision=revision,
            active_connection=bob,
            evidence=proof,
            now=NOW,
            max_evidence_age_seconds=1,
        )

    assert not check(True, None)
    assert not check(True, alice_proof)
    other_revision = replace(revision, revision_id=UUID(int=13))
    assert not check(True, replace(bob_proof, confirmed_revision=other_revision))
    assert not check(False, bob_proof)
    assert check(True, bob_proof)


@pytest.mark.parametrize(
    ("record", "changes"),
    [
        ("destination", {"kind": "private"}),
        ("destination", {"team_approved": False}),
        ("account", {"verified_resource_space": None}),
        ("account", {"exact_reader_checks": False}),
    ],
)
def test_team_requires_approval_realm_and_per_reader_capability(state, intent, record, changes):
    state = replace(state, **{record: replace(getattr(state, record), **changes)})
    assert plan_worker_setup(state, intent).preview.reader_policy == "owner_only"
    with pytest.raises(WorkerSetupRejected, match="^team_unavailable$"):
        plan_worker_setup(state, replace(intent, share_with_team=True))


@pytest.mark.parametrize(
    ("record", "changes", "reason"),
    [
        ("destination", {"can_write": False}, "authority_unavailable"),
        ("destination", {"ordinary": False}, "authority_unavailable"),
        ("destination", {"org": "other"}, "authority_unavailable"),
        ("account", {"active": False}, "authority_unavailable"),
        ("selection_grant", {"expires_at": NOW}, "selection_unverified"),
        ("selection_grant", {"confirmed_at": NOW + 1}, "selection_unverified"),
        (
            "selection_grant",
            {"confirmed_at": NOW - MAX_SELECTION_GRANT_AGE_SECONDS},
            "selection_unverified",
        ),
        ("selection_grant", {"org": "other"}, "selection_unverified"),
        ("selection_grant", {"approval_generation": 1}, "selection_unverified"),
    ],
)
def test_prior_preview_does_not_override_current_grants(state, intent, record, changes, reason):
    preview = plan_worker_setup(state, intent)
    current = replace(state, **{record: replace(getattr(state, record), **changes)})
    with pytest.raises(WorkerSetupRejected, match=f"^{reason}$"):
        confirm_worker_setup(preview.fingerprint, state=current, intent=intent)


def test_denied_or_revoked_selection_has_no_grant(state, intent):
    preview = plan_worker_setup(state, intent)
    with pytest.raises(WorkerSetupRejected, match="^selection_unverified$"):
        confirm_worker_setup(
            preview.fingerprint, state=replace(state, selection_grant=None), intent=intent
        )


@pytest.mark.parametrize(
    ("record", "changes"),
    [
        ("destination", {"grant_generation": 101}),
        ("destination", {"generation": 101}),
        ("destination", {"destination_id": UUID(int=101)}),
        ("destination", {"namespace": "other"}),
        ("destination", {"kind": "private"}),
        ("account", {"approval_generation": 101}),
        ("account", {"verified_resource_space": "other"}),
        ("connection", {"connection_generation": 101}),
        ("connection", {"profile_digest": "b" * 64}),
        ("connection", {"worker_id": UUID(int=101)}),
        ("state", {"policy_generation": 101}),
        ("state", {"selection_generation": 101}),
        ("state", {"source_id": UUID(int=101)}),
        ("caller", {"agent": "other"}),
    ],
)
def test_confirmation_rejects_changed_authority_even_when_still_eligible(
    state, intent, record, changes
):
    preview = plan_worker_setup(state, intent)
    if record == "state":
        current = replace(state, **changes)
    elif record == "connection":
        account = replace(state.account, connection=replace(state.account.connection, **changes))
        current = replace(state, account=account, selection_grant=_grant(account, intent))
    elif record == "account":
        account = replace(state.account, **changes)
        current = replace(state, account=account, selection_grant=_grant(account, intent))
    elif record == "caller":
        caller = replace(state.caller, **changes)
        current = replace(
            state, caller=caller, destination=replace(state.destination, grantee=caller)
        )
    else:
        current = replace(state, destination=replace(state.destination, **changes))
    current = replace(
        current,
        selection_grant=replace(
            current.selection_grant,
            namespace=current.destination.namespace,
            destination_id=current.destination.destination_id,
            source_id=current.source_id,
            selection_generation=current.selection_generation,
        ),
    )
    assert plan_worker_setup(current, intent).fingerprint != preview.fingerprint
    with pytest.raises(WorkerSetupRejected, match="^stale_preview$"):
        confirm_worker_setup(preview.fingerprint, state=current, intent=intent)


def test_selection_evidence_cannot_move_to_a_different_collection(state, intent):
    destination = replace(state.destination, namespace="other", destination_id=UUID(int=101))
    with pytest.raises(WorkerSetupRejected, match="^selection_unverified$"):
        plan_worker_setup(replace(state, destination=destination), intent)


def test_new_selection_evidence_requires_a_new_preview(state, intent):
    plan = plan_worker_setup(state, intent)
    renewed = replace(
        state,
        now=NOW + 1,
        selection_grant=replace(state.selection_grant, confirmed_at=NOW + 1, expires_at=NOW + 11),
    )
    with pytest.raises(WorkerSetupRejected, match="^stale_preview$"):
        confirm_worker_setup(plan.fingerprint, state=renewed, intent=intent)


@pytest.mark.parametrize("fingerprint", [None, "a" * 63, "é" * 64, "a" * 64])
def test_confirmation_rejects_invalid_fingerprint(state, intent, fingerprint):
    with pytest.raises(WorkerSetupRejected, match="^stale_preview$"):
        confirm_worker_setup(fingerprint, state=state, intent=intent)


@pytest.mark.parametrize("change", ["reorder", "replace"])
def test_exact_selection_must_be_reviewed_again(state, intent, change):
    initial = plan_worker_setup(state, intent)
    selected = intent.selection[::-1] if change == "reorder" else (_resource("doc:three"),)
    changed = replace(intent, selection=selected)
    with pytest.raises(WorkerSetupRejected, match="^selection_unverified$"):
        plan_worker_setup(state, changed)
    current = replace(state, selection_grant=_grant(state.account, changed))
    with pytest.raises(WorkerSetupRejected, match="^stale_preview$"):
        confirm_worker_setup(initial.fingerprint, state=current, intent=changed)


@pytest.mark.parametrize(
    "selection",
    [
        (),
        (_resource("doc:duplicate"),) * 2,
        tuple(_resource(f"doc:{index}") for index in range(MAX_SETUP_RESOURCES + 1)),
        tuple(_resource(f"{index}:" + "é" * 1000) for index in range(70)),
    ],
    ids=["empty", "duplicate", "count", "bytes"],
)
def test_selection_size_limits(state, intent, selection):
    with pytest.raises(WorkerSetupRejected, match="^invalid_selection$"):
        plan_worker_setup(state, replace(intent, selection=selection))


def test_refresh_rejects_resource_id_not_representable_in_job(state, intent):
    intent = replace(
        intent, selection=(_resource("é" * 513),), mode="selected_refresh", interval_seconds=300
    )
    with pytest.raises(WorkerSetupRejected, match="^invalid_selection$"):
        plan_worker_setup(state, intent)


@pytest.mark.parametrize("qualification", ["store_qualified", "erasure_qualified"])
def test_only_durable_modes_require_store_and_erasure_qualification(state, intent, qualification):
    current = replace(state, **{qualification: False})
    for mode, interval in (("snapshot", None), ("selected_refresh", 300)):
        with pytest.raises(WorkerSetupRejected, match="^storage_unqualified$"):
            plan_worker_setup(current, replace(intent, mode=mode, interval_seconds=interval))
    preview = plan_worker_setup(current, replace(intent, mode="live")).preview
    assert not preview.stores_copy
    assert not preview.writes_memory
    assert not preview.scheduled_refresh
    assert not preview.checks_explicit_deletion


def test_profile_must_approve_exact_mode(state, intent):
    account = replace(state.account, modes=frozenset(("snapshot",)))
    current = replace(state, account=account)
    assert plan_worker_setup(current, intent).preview.stores_copy
    with pytest.raises(WorkerSetupRejected, match="^unsupported_mode$"):
        plan_worker_setup(current, replace(intent, mode="live"))


@pytest.mark.parametrize(
    ("mode", "interval"),
    [
        ("live", 300),
        ("snapshot", 300),
        ("selected_refresh", None),
        ("selected_refresh", 299),
        ("selected_refresh", 86_401),
        ("selected_refresh", 300.0),
    ],
)
def test_cadence_is_only_for_selected_refresh_and_bounded(state, intent, mode, interval):
    with pytest.raises(WorkerSetupRejected, match="^invalid_setup$"):
        plan_worker_setup(state, replace(intent, mode=mode, interval_seconds=interval))


def test_confirmation_requires_same_mode_and_cadence(state, intent):
    refresh = replace(intent, mode="selected_refresh", interval_seconds=300)
    plan = plan_worker_setup(state, refresh)
    for changed in (intent, replace(refresh, interval_seconds=86_400)):
        with pytest.raises(WorkerSetupRejected, match="^stale_preview$"):
            confirm_worker_setup(plan.fingerprint, state=state, intent=changed)


def test_refresh_without_delete_evidence_does_not_claim_a_delete_path(state, intent):
    refresh = replace(intent, mode="selected_refresh", interval_seconds=300)
    plan = plan_worker_setup(state, refresh)
    assert plan.preview.stores_copy and plan.preview.scheduled_refresh
    assert not plan.preview.checks_explicit_deletion
    assert not plan.preview.syncs_upstream_acls
    current = replace(state, account=replace(state.account, explicit_deletion_checks=True))
    assert plan_worker_setup(current, refresh).preview.checks_explicit_deletion
    assert not plan_worker_setup(current, intent).preview.checks_explicit_deletion
    with pytest.raises(WorkerSetupRejected, match="^stale_preview$"):
        confirm_worker_setup(plan.fingerprint, state=current, intent=refresh)
