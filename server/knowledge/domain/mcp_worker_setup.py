"""Plan external worker setup from current core authority, without I/O."""

from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
from json import dumps
from typing import Literal
from uuid import UUID

from server.knowledge.domain.mcp_identity import ResourceIdentity, selection_sha256
from server.knowledge.domain.mcp_worker_permissions import ReaderConnection
from server.knowledge.domain.mcp_worker_source import WORKER_SOURCE_VERSION, WorkerSourceConfig

MAX_SETUP_RESOURCES = 1000  # not a setting: bounds one source selection across modes
_MAX_SETUP_ID_BYTES = 1024  # not a setting: bounds the encoded identity in a selection
MAX_SETUP_SELECTION_BYTES = 128 * 1024  # not a setting: bounds the stored selection payload
# not a setting: the verifier never accepts a setup proof older than this
MAX_SELECTION_GRANT_AGE_SECONDS = 300

type WorkerMode = Literal["live", "snapshot", "selected_refresh"]
type ReaderPolicy = Literal["owner_only", "current_team_access"]


class WorkerSetupRejected(ValueError):
    """A setup input or current authority does not allow this plan."""


@dataclass(frozen=True, slots=True, repr=False)
class SetupCaller:
    org: str
    subject: str
    agent: str


@dataclass(frozen=True, slots=True, repr=False)
class SetupDestination:
    """Current ordinary destination and resolved, inherited-or-direct grants."""

    org: str
    namespace: str
    destination_id: UUID
    kind: Literal["automatic_personal", "private", "team"]
    generation: int
    grant_generation: int
    grantee: SetupCaller
    ordinary: bool
    can_write: bool
    team_approved: bool = False
    personal_owner: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class ApprovedWorkerAccount:
    """Core's account and current worker/profile approval, not discovery claims.

    Approved modes cover owner reads; team checks cover a separate reader's account.
    """

    org: str
    connection: ReaderConnection
    approval_generation: int
    active: bool
    modes: frozenset[WorkerMode]
    verified_resource_space: str | None = None
    exact_reader_checks: bool = False
    explicit_deletion_checks: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedSelectionGrant:
    """Core-verified current access to every exact item via the owner's account."""

    org: str
    connection: ReaderConnection
    approval_generation: int
    resources_sha256: str
    confirmed_at: int
    expires_at: int
    namespace: str
    destination_id: UUID
    source_id: UUID
    selection_generation: int


@dataclass(frozen=True, slots=True, repr=False)
class SetupState:
    """Resolve afresh in core; generations must advance even on revoke and restore.

    Resolve the source ID and selection generation for this owner and destination.
    Pass no grant if its evidence is absent or superseded by denial or revocation.
    """

    caller: SetupCaller
    destination: SetupDestination
    account: ApprovedWorkerAccount
    selection_grant: VerifiedSelectionGrant | None
    source_id: UUID
    selection_generation: int
    policy_generation: int
    now: int
    store_qualified: bool = False
    erasure_qualified: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class SetupIntent:
    selection: tuple[ResourceIdentity, ...]
    mode: WorkerMode
    interval_seconds: int | None = None
    share_with_team: bool = False


@dataclass(frozen=True, slots=True)
class SetupPreview:
    """Team reads use each reader's own approved account in the verified realm."""

    reader_policy: ReaderPolicy
    stores_copy: bool
    scheduled_refresh: bool
    checks_explicit_deletion: bool
    expires_at: int
    requires_collection_grant: Literal[True] = True
    requires_exact_reader_evidence: Literal[True] = True
    syncs_upstream_acls: Literal[False] = False
    writes_memory: Literal[False] = False


@dataclass(frozen=True, slots=True, repr=False)
class WorkerSetupPlan:
    caller: SetupCaller
    namespace: str
    destination_id: UUID
    source_id: UUID
    config: WorkerSourceConfig
    selection: tuple[ResourceIdentity, ...]
    preview: SetupPreview
    fingerprint: str


def _known(value: str | None) -> bool:
    return isinstance(value, str) and value not in ("", "-") and value == value.strip()


def _positive(value: int) -> bool:
    return type(value) is int and value > 0


def _nonzero(value: UUID) -> bool:
    return isinstance(value, UUID) and value.int != 0


def _require_authority(state: SetupState) -> None:
    caller, destination, account = state.caller, state.destination, state.account
    if not (
        _known(caller.org)
        and _known(caller.subject)
        and _known(caller.agent)
        and caller.org == destination.org == account.org
        and caller.subject == account.connection.owner
        and destination.grantee == caller
        and _known(destination.namespace)
        and _nonzero(destination.destination_id)
        and _nonzero(state.source_id)
        and destination.ordinary is True
        and destination.can_write is True
        and destination.kind in ("automatic_personal", "private", "team")
        and (
            destination.kind != "automatic_personal" or destination.personal_owner == caller.subject
        )
        and account.active is True
        and _positive(destination.generation)
        and _positive(destination.grant_generation)
        and _positive(account.approval_generation)
        and _positive(state.policy_generation)
    ):
        raise WorkerSetupRejected("authority_unavailable")


def checked_worker_selection(
    selection: tuple[ResourceIdentity, ...], mode: WorkerMode
) -> tuple[ResourceIdentity, ...]:
    if not isinstance(selection, tuple) or not 0 < len(selection) <= MAX_SETUP_RESOURCES:
        raise WorkerSetupRejected("invalid_selection")
    try:
        items = tuple(ResourceIdentity.model_validate(resource) for resource in selection)
        if len({item.id for item in items}) != len(items) or (
            1 + len(items) + sum(len(item.model_dump_json().encode()) for item in items)
            > MAX_SETUP_SELECTION_BYTES
        ):
            raise WorkerSetupRejected("invalid_selection")
        if mode == "selected_refresh" and any(
            not item.id.isprintable() or len(item.id.encode()) > _MAX_SETUP_ID_BYTES
            for item in items
        ):
            raise WorkerSetupRejected("invalid_selection")
    except ValueError:
        raise WorkerSetupRejected("invalid_selection") from None
    return items


def _require_selection_grant(state: SetupState, resources_digest: str) -> VerifiedSelectionGrant:
    grant = state.selection_grant
    if not (
        isinstance(grant, VerifiedSelectionGrant)
        and grant.org == state.caller.org
        and grant.connection == state.account.connection
        and grant.approval_generation == state.account.approval_generation
        and grant.resources_sha256 == resources_digest
        and grant.namespace == state.destination.namespace
        and grant.destination_id == state.destination.destination_id
        and grant.source_id == state.source_id
        and grant.selection_generation == state.selection_generation
        and _positive(state.now)
        and _positive(grant.confirmed_at)
        and _positive(grant.expires_at)
        and grant.confirmed_at <= state.now < grant.expires_at
        and grant.expires_at - grant.confirmed_at <= MAX_SELECTION_GRANT_AGE_SECONDS
    ):
        raise WorkerSetupRejected("selection_unverified")
    return grant


def _reader_policy(state: SetupState, intent: SetupIntent) -> ReaderPolicy:
    if type(intent.share_with_team) is not bool:
        raise WorkerSetupRejected("invalid_setup")
    if not intent.share_with_team or state.destination.kind == "automatic_personal":
        return "owner_only"
    if not (
        state.destination.kind == "team"
        and state.destination.team_approved is True
        and state.account.exact_reader_checks is True
        and _known(state.account.verified_resource_space)
    ):
        raise WorkerSetupRejected("team_unavailable")
    return "current_team_access"


def plan_worker_setup(state: SetupState, intent: SetupIntent) -> WorkerSetupPlan:
    """Preview requirements, not a grant to a collection reader or a write."""
    _require_authority(state)
    if (
        intent.mode not in ("live", "snapshot", "selected_refresh")
        or intent.mode not in state.account.modes
    ):
        raise WorkerSetupRejected("unsupported_mode")
    stores_copy = intent.mode != "live"
    if stores_copy and not (state.store_qualified is True and state.erasure_qualified is True):
        raise WorkerSetupRejected("storage_unqualified")
    reader_policy = _reader_policy(state, intent)
    items = checked_worker_selection(intent.selection, intent.mode)
    resources_digest = selection_sha256(items)
    grant = _require_selection_grant(state, resources_digest)
    binding = state.account.connection
    try:
        config = WorkerSourceConfig(
            version=WORKER_SOURCE_VERSION,
            worker_id=binding.worker_id,
            profile_digest=binding.profile_digest,
            approval_generation=state.account.approval_generation,
            owner_connection_id=binding.connection_id,
            owner_connection_generation=binding.connection_generation,
            selection_generation=state.selection_generation,
            mode=intent.mode,
            reader_policy=reader_policy,
            interval_seconds=intent.interval_seconds,
        )
    except ValueError:
        raise WorkerSetupRejected("invalid_setup") from None
    scheduled = intent.mode == "selected_refresh"
    preview = SetupPreview(
        reader_policy=reader_policy,
        stores_copy=stores_copy,
        scheduled_refresh=scheduled,
        checks_explicit_deletion=scheduled and state.account.explicit_deletion_checks is True,
        expires_at=grant.expires_at,
    )
    caller, destination = state.caller, state.destination
    fingerprint = sha256(
        dumps(
            (
                "mcp-worker-setup-v1",
                caller.org,
                caller.subject,
                caller.agent,
                destination.namespace,
                str(destination.destination_id),
                destination.kind,
                destination.generation,
                destination.grant_generation,
                str(state.source_id),
                state.policy_generation,
                state.account.approval_generation,
                state.account.verified_resource_space,
                config.model_dump(mode="json"),
                resources_digest,
                intent.share_with_team,
                preview.checks_explicit_deletion,
                grant.confirmed_at,
                grant.expires_at,
            ),
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return WorkerSetupPlan(
        caller,
        destination.namespace,
        destination.destination_id,
        state.source_id,
        config,
        items,
        preview,
        fingerprint,
    )


def confirm_worker_setup(
    expected_fingerprint: str, *, state: SetupState, intent: SetupIntent
) -> WorkerSetupPlan:
    """Revalidate before mutation; the fingerprint is only an equality precondition.

    Use the returned plan, not submitted preview fields. The mutation must be
    fenced against changes to the current inputs used here.
    """
    plan = plan_worker_setup(state, intent)
    if not (
        isinstance(expected_fingerprint, str)
        and len(expected_fingerprint) == 64
        and expected_fingerprint.isascii()
        and compare_digest(plan.fingerprint, expected_fingerprint)
    ):
        raise WorkerSetupRejected("stale_preview")
    return plan
