"""Namespace registry handlers: register, list, get, delete, browse.

Self-service membership is namespace_membership.py, snapshot/restore
namespace_snapshot.py, taxonomy and categories namespace_catalog.py.

Wire models live in server/identity/api/models.py; the Operation registry is
in server/identity/api/ops_namespace.py.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from server.auth.aliases import subject_aliases
from server.auth.claim_codes import CLAIMED_TAG
from server.auth.grant_claims import account_change
from server.auth.sharing import PERSONAL_ALIAS, is_collection, is_personal
from server.core import grant_cache
from server.core.ch import CHClient, get_client
from server.core.datetimes import naive_utc_now_seconds
from server.core.errors import PreconditionFailedError, ValidationError
from server.core.naming import SSO_NAMESPACE_PREFIX, reserved_for_sso
from server.identity.api.models import (
    NamespaceBrowseInput,
    NamespaceBrowseOutput,
    NamespaceDeleteInput,
    NamespaceDeleteOutput,
    NamespaceGetInput,
    NamespaceGetOutput,
    NamespaceListInput,
    NamespaceListOutput,
    NamespaceRegisterInput,
    NamespaceRegisterOutput,
    _BrowseEntry,
    _NamespaceEntry,
)
from server.identity.app import registration
from server.identity.domain import join_policy as _jp
from server.identity.domain.account_ties import owner_moved
from server.identity.store import grants as grants_repo
from server.identity.store import namespace_guides
from server.identity.store import namespaces as namespaces_repo
from server.memory.app import namespace_retrieval_defaults as _rd
from server.memory.domain import code_change
from server.operations.base import Role

if TYPE_CHECKING:
    from server.auth.jwt import AuthInfo


def resolve_subject(input_subject: str | None = None) -> str:
    """Return the caller's identity for a self-service op.

    Reads the request-scoped auth ContextVar; falls back to an explicit
    `subject` field in dev mode. Raises `ValidationError` when neither
    is available.
    """
    from server.auth.context import current_auth

    auth = current_auth()
    if auth is not None and auth.subject:
        return auth.subject
    if input_subject:
        return input_subject
    # Single-user mode has no authenticated caller; synthesize a stable
    # identity so self-service ops work without auth scaffolding.
    from server.settings import settings

    if settings.effective_identity_provider() == "single-user":
        return "you"
    from server.core.errors import ValidationError

    raise ValidationError(
        "no authenticated caller; either supply a JWT (with "
        "LOOM_AUTH_REQUIRED=true) or pass `subject` in the request body"
    )


async def _is_admin(auth: AuthInfo | None, org: str, namespace: str, caller: str) -> bool:
    """ADMIN on (org, namespace). An authenticated caller is decided by the
    canonical check, so a scoped token's ns_roles entry is its ceiling there;
    with auth off the caller's own grant decides, as it always has."""
    from server.auth.authz import _lookup_grant, row_in_scope

    if auth is not None:
        return await row_in_scope(org, namespace, Role.ADMIN)
    grant_role = await _lookup_grant(org, namespace, caller)
    return grant_role is not None and grant_role.value >= Role.ADMIN.value


async def insert_admin_grant(ch: CHClient, org: str, namespace: str, subject: str) -> None:
    """Insert an admin grant row for `subject` on `(org, namespace)`.

    Idempotent — re-inserting replaces the prior row on RMT merge.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    await grants_repo.insert_rows(
        [
            {
                "org": org,
                "namespace": namespace,
                "subject": subject,
                "role": "admin",
                "granted_at": now,
                "granted_by": subject,
                "revoked_at": None,
                "version": time.time_ns(),
            }
        ],
        ch=ch,
    )


def registry_row(
    input: NamespaceRegisterInput,
    existing: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    """The loom.namespaces row `namespace.register` writes for `input`.

    One builder for every path that creates a registry row — the register handler and the
    first-write auto-registration — so a row born from a bare write carries the same
    defaults and preserve-on-update rules as one born from the form. The row is the whole
    write: per-namespace TTL policy is never applied here, so inserting it issues no ALTER.
    """
    # The traffic class rides the row as the onboarding kit's tag `traffic_class:<value>`
    # (one tag; a re-registration replaces it); a bench class with no explicit kind registers
    # the namespace as bench, so the namespace gc worker retires it by idleness.
    # Lazy: the auto-register module imports the operation registry, which imports this one.
    from server.auth.agents import TAG_PREFIX as MACHINE_TAG
    from server.identity.app.namespace_autoregister import classify_kind

    # A workload trust's mark is the trust's to give, and an agent claim's mark the
    # claim's: dropped from the form, carried forward from the row. Only a trust's
    # mark binds the owner.
    stored = [str(t) for t in (existing or {}).get("tags") or [] if str(t).startswith(MACHINE_TAG)]
    trust_made = [t for t in stored if t != CLAIMED_TAG]
    tags = [t for t in input.tags if not str(t).startswith(MACHINE_TAG)] + stored
    kind = input.kind
    if input.traffic_class:
        from server.observability.traffic_class import TAG_PREFIX

        tags = [t for t in tags if not str(t).startswith(TAG_PREFIX)]
        tags.append(f"{TAG_PREFIX}{input.traffic_class}")
        if kind is None and input.traffic_class == "bench":
            kind = "bench"
    return {
        "org": input.org,
        "namespace": input.namespace,
        # An agent's namespace is bound to the agent (its exchange checks it).
        "owner_email": existing["owner_email"] if existing and trust_made else input.owner_email,
        "description": _set_or_stored(input.description, existing, "description", ""),
        # A fresh row with no explicit kind is classified by name, the same call the
        # first-write hook makes, so registering a bench-prefixed name first and writing
        # to it second lands the same row as the bare write would.
        "kind": kind or (existing or {}).get("kind") or classify_kind(input.namespace),
        "tags": tags,
        # The classification is carried forward, never set from the form. A restricted row
        # stays restricted through every settings write; widening is a separate, audited
        # operator step.
        "classification": str((existing or {}).get("classification") or "standard"),
        # The join policy is carried forward unless the form set it; a settings-only write
        # never resets how a namespace is joined.
        "join_policy": (
            input.join_policy
            if input.join_policy is not None
            else str((existing or {}).get("join_policy") or "inherit")
        ),
        # The retrieval defaults are carried forward unless the form set them; `{}` (all
        # knobs unset) stores '' and clears them.
        "retrieval_defaults": (
            _rd.serialize(input.retrieval_defaults)
            if input.retrieval_defaults is not None
            else str((existing or {}).get("retrieval_defaults") or "")
        ),
        # …and a restricted row is never public, whatever the form said.
        "visibility": (
            "private"
            if str((existing or {}).get("classification") or "standard") == "restricted"
            else _set_or_stored(input.visibility, existing, "visibility", "private")
        ),
        "active": _set_or_stored(input.active, existing, "active", True),
        # No reader left; the full row still carries the stored value.
        "auto_promote_enabled": bool((existing or {}).get("auto_promote_enabled") or False),
        # Preserve the logging toggle on omit (None) — a settings-only
        # re-register must not silently re-enable a disabled namespace.
        # A BRAND-NEW namespace starts False: transcript capture is
        # opt-in, so it waits for someone to ask for it rather than
        # arriving on and relying on the owner to notice and refuse.
        "conversation_logging_enabled": (
            input.conversation_logging_enabled
            if input.conversation_logging_enabled is not None
            else (bool(existing["conversation_logging_enabled"]) if existing else False)
        ),
        # Tri-state (None = inherit the server default): an omitted value keeps the
        # stored choice, so an unrelated settings write never resets someone's
        # privacy choice; an explicit null returns it to inherit. The write below
        # calls `capture_consent.invalidate`, so a toggle skips the 60 s cache.
        "capture_generation_io": _sent_or_stored(input, existing, "capture_generation_io"),
        "coverage_sweep": _sent_or_stored(input, existing, "coverage_sweep"),
        "consolidation_enabled": _set_or_stored(
            input.consolidation_enabled, existing, "consolidation_enabled", False
        ),
        "daily_cost_cap_usd": _set_or_stored(
            input.daily_cost_cap_usd, existing, "daily_cost_cap_usd", 0.0
        ),
        # Same tri-state rule: an explicit null inherits LOOM_CONFLICT_THRESHOLD.
        "conflict_threshold": _sent_or_stored(input, existing, "conflict_threshold"),
        "retention_days": _set_or_stored(input.retention_days, existing, "retention_days", 0),
        "warm_days": _set_or_stored(input.warm_days, existing, "warm_days", 0),
        "requires_approval": _set_or_stored(
            input.requires_approval, existing, "requires_approval", False
        ),
        "rerank_endpoint": _set_or_stored(input.rerank_endpoint, existing, "rerank_endpoint", ""),
        "rerank_prompt_template": _set_or_stored(
            input.rerank_prompt_template, existing, "rerank_prompt_template", ""
        ),
        "classify_endpoint": _set_or_stored(
            input.classify_endpoint, existing, "classify_endpoint", ""
        ),
        "classify_prompt_template": _set_or_stored(
            input.classify_prompt_template, existing, "classify_prompt_template", ""
        ),
        "default_conflict_policy": _set_or_stored(
            input.default_conflict_policy, existing, "default_conflict_policy", "auto_reinforce"
        ),
        "auto_promote_gate": str((existing or {}).get("auto_promote_gate") or "k_supporters"),
        "evidence_threshold": int((existing or {}).get("evidence_threshold") or 5),
        # Preserved unless the form set it.
        "proposal_policy": (
            _serialize_proposal_policy(input.proposal_policy)
            if input.proposal_policy is not None
            else str((existing or {}).get("proposal_policy") or "")
        ),
        "code_change_policy": _set_or_stored(
            input.code_change_policy, existing, "code_change_policy", ""
        ),
        # Preserve auth fields written by self_signup, else a
        # settings-only update wipes the user's login silently.
        "contact_email": ((existing or {}).get("contact_email") or ""),
        "password_hash": ((existing or {}).get("password_hash") or ""),
        "released_aliases": list((existing or {}).get("released_aliases") or []),
        # Not on the form: a re-register keeps the namespace's own override and birth date.
        "duplicate_threshold": (existing or {}).get("duplicate_threshold"),
        "created_at": (existing or {}).get("created_at") or now,
        "updated_at": now,
    }


def _set_or_stored(value: Any, existing: dict[str, Any] | None, column: str, fresh: Any) -> Any:
    """`value` when the caller set it, else the stored column, else a fresh row's `fresh`."""
    if value is not None:
        return value
    stored = (existing or {}).get(column)
    return fresh if stored is None else stored


def _sent_or_stored(
    input: NamespaceRegisterInput, existing: dict[str, Any] | None, field: str
) -> Any:
    """A tri-state setting whose None means inherit: the sent value, even an
    explicit null, else the stored one (None on a fresh row)."""
    if field in input.model_fields_set:
        return getattr(input, field)
    return (existing or {}).get(field)


def _serialize_proposal_policy(policy: Any) -> str:
    from server.memory.domain.proposal_policy import serialize_policy

    return serialize_policy(policy)


def _parse_proposal_policy(raw: Any) -> Any:
    from server.memory.domain.proposal_policy import parse_policy

    return parse_policy(raw)


def _is_auto_registered_owner(existing: dict[str, Any], caller: str) -> bool:
    """True when `existing` was written by the first-write hook and `caller` is the owner it
    named."""
    from server.identity.app.namespace_autoregister import AUTO_REGISTER_TAG

    tags = [str(t) for t in (existing.get("tags") or [])]
    owner = str(existing.get("owner_email") or "")
    return AUTO_REGISTER_TAG in tags and bool(caller) and owner == caller


async def require_admin_of_existing(
    existing: dict[str, Any],
    org: str,
    namespace: str,
    caller: str,
    *,
    visibility: str | None,
    owner_may_complete: bool = True,
) -> None:
    """Refuse `caller` unless it may rewrite the registered (active or soft-deleted) row
    `existing`: the check `namespace.register` and `namespace.restore` both apply.
    `owner_may_complete=False` (a restore, which writes grants) refuses the
    auto-registered owner who holds no admin: completing the registration grants
    nothing, a restore would grant them, and anyone else, any role."""
    from server.auth.context import current_auth

    auth = current_auth()
    # A restricted row is updated only by a principal who passes the two-factor ADMIN
    # check for it — a flat admin whose claim is `*`, the auto-registered owner and an
    # inherited grant do not qualify — and it never becomes public.
    if str(existing.get("classification") or "standard") == "restricted":
        from server.auth.authz import restricted_allows
        from server.core.errors import AuthInsufficientError

        if auth is not None and not await restricted_allows(auth, Role.ADMIN, org, namespace):
            raise AuthInsufficientError(f"{caller!r} cannot update {org}/{namespace}")
        if visibility == "public":
            raise AuthInsufficientError(
                f"{org}/{namespace} is restricted and cannot be made public"
            )
    # A first write's row names its writer owner (none on a name in use), who may complete
    # the form while nobody else holds a live grant there (the form sets who may join);
    # anyone else needs admin.
    owner_completes = (
        owner_may_complete
        and _is_auto_registered_owner(existing, caller)
        and not await grants_repo.held_by_another(
            org, namespace, await subject_aliases(org, caller), ch=get_client()
        )
    )
    if not owner_completes and not await _is_admin(auth, org, namespace, caller):
        raise registration.not_admin_error(org, namespace, caller)


def refuse_sso_name(namespace: str) -> None:
    """A new namespace may not take a name single sign-on creates from an email."""
    if reserved_for_sso(namespace):
        raise ValidationError(
            f"'{SSO_NAMESPACE_PREFIX}' names are reserved for the accounts single sign-on creates"
        )


def refuse_collection_name(namespace: str) -> None:
    """A collection, an end user's personal one included, is reached only through its
    grants; no namespace operation writes its rows. `@me` names one only once the gate
    maps it, so the literal name is refused too."""
    if is_collection(namespace) or namespace == PERSONAL_ALIAS:
        raise ValidationError("a collection is changed with the collection operations only")


async def handle_namespace_register(
    input: NamespaceRegisterInput,
) -> NamespaceRegisterOutput:
    ch = get_client()
    caller = resolve_subject()
    refuse_collection_name(input.namespace)

    # Create-or-update. Fresh row ⇒ caller becomes admin; existing row ⇒
    # only an admin may update it. The whole stored row is read, so every setting the
    # call omits (and the login fields the form never carries) is written forward.
    # Held from the read to the write: a release or a password set rewrites the row too.
    async with account_change(input.org, input.namespace):
        existing = await namespaces_repo.fetch_row(input.org, input.namespace, ch=ch)

        if existing is not None:
            await require_admin_of_existing(
                existing, input.org, input.namespace, caller, visibility=input.visibility
            )
            if owner_moved(existing, input.owner_email):
                # The account's login signs in as its owner, and the claim cap ties the
                # owner to the email: neither may move to someone else here.
                raise PreconditionFailedError(
                    f"{input.namespace!r} is an account with a login, owned by "
                    f"{existing.get('owner_email')!r}: send that owner_email to update its "
                    "settings. To hand it to someone else, release it (loom claim-release), "
                    "which ends its grants and its login."
                )
        else:
            refuse_sso_name(input.namespace)
            if await registration.refused_as_in_use(input.org, input.namespace, ch=ch):
                raise registration.not_admin_error(input.org, input.namespace, caller)

        now = naive_utc_now_seconds()
        row = registry_row(input, existing, now)
        await ch.insert("loom.namespaces", [row])
    # The claim cap reads which accounts tie a person's spellings from this table.
    await grant_cache.changed(input.org)
    # The class this row implies is what the next request in this process runs as — no
    # re-read.
    from server.identity.app.traffic_class_registry import note_registered

    note_registered(input.org, input.namespace, row)
    # The next search in this process runs the defaults just written — no re-read, no wait
    # for the cache to expire.
    _rd.note_registered(input.org, input.namespace, row)

    if existing is None:
        await insert_admin_grant(ch, input.org, input.namespace, caller)

    # Per-tag config registry; the underlying column is `memory_type`.
    if input.tag_registry:
        rows = [
            {
                "org": input.org,
                "namespace": input.namespace,
                "memory_type": cfg.memory_type,
                "retention_days": cfg.retention_days,
                "dedup_threshold": cfg.dedup_threshold,
                "hot_quota": cfg.hot_quota,
                "conflict_policy": cfg.conflict_policy or "",
                "created_at": now,
                "updated_at": now,
            }
            for cfg in input.tag_registry
        ]
        await ch.insert(
            "loom.namespace_types",
            rows,
            settings={"async_insert": 0, "wait_for_async_insert": 1},
        )

    # Drop the cached capture-consent for this namespace so a user who just
    # toggled it in the UI sees it apply on the next request instead of waiting
    # out the 60s TTL. Non-fatal: the cache expires on its own regardless.
    try:
        from server.observability import capture_consent as _consent

        _consent.invalidate(input.org, input.namespace)
    except Exception:  # noqa: BLE001 — the cache expires on its own
        pass
    try:
        from server.memory.app import coverage_sweep_gate as _sweep_gate

        _sweep_gate.invalidate(input.org, input.namespace)
    except Exception:  # noqa: BLE001 — the cache expires on its own
        pass
    from server.memory.app.proposals import invalidate_policy

    invalidate_policy(input.org, input.namespace)

    # Per-namespace TTL policy is NOT re-applied here. The namespace row is the source of
    # truth; the supervisor's retention_reconcile worker recomposes the table TTLs on its
    # timer and alters only the tables whose clause changed. Inline, it would be one
    # full-table mutation per managed table per registration (hundreds an hour on ClickHouse
    # Cloud from bench namespaces alone).

    return NamespaceRegisterOutput(
        registered=True,
        namespace=input.namespace,
        updated_at=now,
        visibility=row["visibility"],
    )


async def handle_namespace_list(input: NamespaceListInput) -> NamespaceListOutput:
    from server.auth.context import collection_clause

    # A collection shows only to whom its grants reach, org admins included;
    # a platform admin sees every one.
    collections_where, params = await collection_clause(input.org)
    where = ["org = {org:String}" + collections_where]
    params.update({"org": input.org, "lim": input.limit})
    if input.active_only:
        where.append("active = true")
    if input.tags:
        where.append("hasAny(tags, {tags:Array(String)})")
        params["tags"] = input.tags
    rows = await get_client().query_all(
        f"""
        SELECT namespace, kind, owner_email, description, tags, active,
               conversation_logging_enabled,
               capture_generation_io,
               coverage_sweep,
               consolidation_enabled,
               daily_cost_cap_usd,
               conflict_threshold,
               retention_days, warm_days,
               toString(visibility) AS visibility,
               created_at, updated_at
        FROM loom.namespaces FINAL
        WHERE {" AND ".join(where)}
        ORDER BY namespace
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    return NamespaceListOutput(
        rows=[
            _NamespaceEntry(
                kind=str(r.get("kind") or "memory"),
                namespace=r["namespace"],
                owner_email=r["owner_email"],
                description=r["description"],
                tags=list(r["tags"]),
                active=bool(r["active"]),
                consolidation_enabled=bool(r.get("consolidation_enabled") or False),
                daily_cost_cap_usd=float(r.get("daily_cost_cap_usd") or 0.0),
                conflict_threshold=(
                    float(r["conflict_threshold"]) if r["conflict_threshold"] is not None else None
                ),
                retention_days=int(r.get("retention_days") or 0),
                warm_days=int(r.get("warm_days") or 0),
                visibility=r.get("visibility") or "private",
                created_at=r["created_at"],
                # An auto-registered row's version is the epoch; it was last changed when made.
                updated_at=max(r["updated_at"], r["created_at"]),
            )
            for r in rows
        ]
    )


async def handle_namespace_get(input: NamespaceGetInput) -> NamespaceGetOutput:
    row = await get_client().query_one(
        """
        SELECT namespace, kind, owner_email, description, tags, active,
               conversation_logging_enabled,
               capture_generation_io,
               coverage_sweep,
               consolidation_enabled,
               daily_cost_cap_usd,
               conflict_threshold,
               retention_days, warm_days,
               toString(visibility) AS visibility,
               requires_approval,
               toString(join_policy) AS join_policy,
               retrieval_defaults,
               ifNull(rerank_endpoint, '') AS rerank_endpoint,
               ifNull(classify_endpoint, '') AS classify_endpoint,
               ifNull(default_conflict_policy, '') AS default_conflict_policy,
               proposal_policy,
               code_change_policy,
               toString(classification) AS classification,
               created_at, updated_at
        FROM loom.namespaces FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
        LIMIT 1
        """,
        params={"org": input.org, "ns": input.namespace},
    )
    if row is None:
        return NamespaceGetOutput(found=False, namespace=input.namespace)
    # Per-tag registry rows live on a sibling table (loom.namespace_types).
    # Fetch them out-of-band so the caller sees the full config in one
    # round-trip.
    from server.memory.app.namespace_types import list_type_configs

    tag_registry, guides = await asyncio.gather(
        list_type_configs(input.org, input.namespace),
        namespace_guides.summaries(input.org, [input.namespace]),
    )
    guide = guides.get(input.namespace) or {"revision": 0, "summary": ""}
    return NamespaceGetOutput(
        found=True,
        namespace=row["namespace"],
        owner_email=row["owner_email"],
        kind=str(row.get("kind") or "memory"),
        description=row["description"],
        tags=list(row["tags"]),
        active=bool(row["active"]),
        capture_generation_io=(
            None
            if row.get("capture_generation_io") is None
            else bool(row.get("capture_generation_io"))
        ),
        coverage_sweep=(
            None if row.get("coverage_sweep") is None else bool(row.get("coverage_sweep"))
        ),
        conversation_logging_enabled=(
            # Absent column reads as off, matching the gate in
            # conversation_repo.namespace_logging_enabled — the settings view
            # must not show "Recording on" for a namespace that isn't recording.
            False
            if row.get("conversation_logging_enabled") is None
            else bool(row["conversation_logging_enabled"])
        ),
        consolidation_enabled=bool(row.get("consolidation_enabled") or False),
        daily_cost_cap_usd=float(row.get("daily_cost_cap_usd") or 0.0),
        conflict_threshold=(
            float(row["conflict_threshold"]) if row["conflict_threshold"] is not None else None
        ),
        retention_days=int(row.get("retention_days") or 0),
        warm_days=int(row.get("warm_days") or 0),
        visibility=row.get("visibility") or "private",
        requires_approval=bool(row.get("requires_approval", False)),
        join_policy=str(row.get("join_policy") or "inherit"),
        effective_join_policy=_jp.effective_join_policy(row),
        retrieval_defaults=_rd.parse(row.get("retrieval_defaults")) or None,
        rerank_endpoint=row.get("rerank_endpoint") or "",
        classify_endpoint=row.get("classify_endpoint") or "",
        default_conflict_policy=row.get("default_conflict_policy") or "auto_reinforce",
        proposal_policy=_parse_proposal_policy(row.get("proposal_policy")),
        code_change_policy=code_change.policy_of(str(row.get("code_change_policy") or "")),
        tag_registry=tag_registry,
        # Only on the found branch: the transport's restricted gate has already admitted the
        # caller to this row, so the word says nothing the caller could not read.
        classification=("restricted" if row.get("classification") == "restricted" else "standard"),
        guide_revision=int(guide["revision"]),
        guide_summary=str(guide["summary"]),
        created_at=row["created_at"],
        updated_at=max(row["updated_at"], row["created_at"]),
    )


async def handle_namespace_delete(
    input: NamespaceDeleteInput,
) -> NamespaceDeleteOutput:
    """Soft-delete a namespace.

    Writes a new ReplacingMergeTree row with `active=false`. Existing
    data in `loom.memory` / `loom.knowledge` etc. is untouched —
    callers who want the data deleted should issue an
    `erasure.request(scope_type='namespace', scope_value=<ns>)` first.

    Idempotent: deleting an already-inactive namespace returns
    `deleted=False`. Deleting a namespace that was never registered
    is a no-op (returns deleted=False). A collection is refused: it goes with
    what it holds, through `collection.delete`.
    """
    if is_personal(input.namespace):
        # It goes with its user's erasure, which also takes the user's id off its row.
        raise ValidationError("a personal collection goes only with its user's erasure")
    if is_collection(input.namespace):
        raise ValidationError("a collection is deleted with collection.delete, with what it holds")
    return await delete_namespace(input.org, input.namespace)


async def delete_namespace(org: str, namespace: str) -> NamespaceDeleteOutput:
    """`namespace.delete`'s write, and `collection.delete`'s once its purge ran."""
    ch = get_client()
    # SELECT * so the soft-delete row mirrors EVERY column forward; a
    # partial RMT insert would reset omitted columns to schema defaults.
    async with account_change(org, namespace):  # as register holds it
        existing = await namespaces_repo.fetch_row(org, namespace, ch=ch)
        now = naive_utc_now_seconds()
        if existing is None:
            # A collection whose create stopped before its directory row still
            # has a tree row the gate admits by: retire it.
            from server.identity.app.collections import retire

            closed = is_collection(namespace) and await retire(org, namespace)
            return NamespaceDeleteOutput(deleted=closed, namespace=namespace, updated_at=now)
        active = bool(existing.get("active", False))
        if active:
            row = dict(existing)
            row["active"] = False
            row["updated_at"] = now
            await ch.insert("loom.namespaces", [row])
        if existing.get("kind") == "collection":
            # The tree last, and on every delete: only it decides access, so a
            # delete cut short before it is retried (the gate still admits it)
            # and closes the collection then. Retiring twice changes nothing.
            from server.identity.app.collections import retire

            await retire(org, namespace)
        if not active:
            return NamespaceDeleteOutput(deleted=False, namespace=namespace, updated_at=now)
    # The claim cap reads which accounts tie a person's spellings from this table.
    await grant_cache.changed(org)
    return NamespaceDeleteOutput(
        deleted=True,
        namespace=namespace,
        updated_at=now,
    )


_Standing = Literal["none", "pending", "member"]


async def _my_standing(org: str, subject: str) -> dict[str, tuple[_Standing, str]]:
    """namespace → (my_status, my_role) from the caller's live grants:
    `member` for an active grant, `pending` for a request on file."""
    rows = await get_client().query_all(
        """
        SELECT namespace, role, ifNull(status, '') AS status, pending_role
        FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND subject = {subject:String} AND revoked_at IS NULL
        """,
        params={"org": org, "subject": subject},
    )
    out: dict[str, tuple[_Standing, str]] = {}
    for r in rows:
        status = str(r.get("status") or "active")
        if status == "pending":
            out[r["namespace"]] = ("pending", "")
        else:
            out[r["namespace"]] = ("member", str(r.get("role") or ""))
    return out


async def _restricted_names(org: str) -> set[str] | None:
    """Every namespace of `org` restricted by its directory row or its repository
    config; None when the directory cannot be read."""
    from server.auth.directory import ClassificationUnavailable, directory

    try:
        names = set(await directory().restricted_set(org))
    except ClassificationUnavailable:
        return None
    return names | directory().restricted_code_namespaces(org)


def _guide_shown(
    row: dict[str, Any], standing: tuple[_Standing, str], hidden: set[str] | None
) -> bool:
    """Whether a directory row shows its guide's summary: a public row's to the
    org, a private code row's (listed to everyone) to its members, and never a
    restricted row's, whose reads take a token naming it. Unknown restrictions
    (`hidden` None) show none."""
    if hidden is None or str(row["namespace"]) in hidden:
        return False
    return row.get("visibility") == "public" or standing[0] == "member"


async def handle_namespace_browse(
    input: NamespaceBrowseInput,
) -> NamespaceBrowseOutput:
    """List the namespaces of an org a person may join — the directory users
    page through.

    Discoverable rows are `visibility = public` plus every standard code
    namespace (a code index is joined, not inherited, so it must be findable even where an
    operator left the row private). A `closed` row is omitted unless `include_closed`; a
    restricted row is never listed (its existence is part of what is kept). Each row
    carries the resolved join policy and the caller's standing on
    it, so a client can render Join / Request / Pending without a second
    call. Member counts come from a LEFT JOIN against `namespace_grants
    FINAL`, so an empty namespace shows `0` rather than omitting the row.
    """
    where = [
        "n.org = {org:String}",
        "n.active = true",
        "(toString(n.visibility) = 'public' OR (ifNull(n.kind, 'memory') = 'code'))",
        # Restricted rows stay out of the directory, except a restricted *repository* whose
        # rule is approval, so a person can find it and ask — its contents stay behind the
        # grant.
        "(toString(n.classification) != 'restricted' "
        "OR (ifNull(n.kind, 'memory') = 'code' AND toString(n.join_policy) = 'approval'))",
    ]
    from server.auth.sharing import COLLECTION_PREFIX

    # A collection is never joined (its grants open it), so it is not in the directory.
    where.append("NOT startsWith(n.namespace, {col_prefix:String})")
    params: dict[str, Any] = {
        "org": input.org,
        "lim": input.limit,
        "col_prefix": COLLECTION_PREFIX,
    }
    hidden = await _restricted_names(input.org)
    if input.query:
        match = [
            "positionCaseInsensitive(n.namespace, {q:String}) > 0",
            "positionCaseInsensitive(n.description, {q:String}) > 0",
        ]
        # A guide summary matches only where the row shows it, in SQL, before the
        # LIMIT: whether a row (or a page's other rows) comes back must not spell out
        # a summary the caller may not see. Unknown restrictions match none.
        if hidden is not None:
            match.append(
                "(toString(n.visibility) = 'public' AND n.namespace NOT IN {hidden:Array(String)} "
                f"AND n.namespace IN ({namespace_guides.SUMMARY_MATCH_SQL}))"
            )
            params["hidden"] = sorted(hidden)
        where.append("(" + " OR ".join(match) + ")")
        params["q"] = input.query
    if input.kinds:
        where.append("ifNull(n.kind, 'memory') IN {kinds:Array(String)}")
        params["kinds"] = list(input.kinds)
    rows = await get_client().query_all(
        f"""
        SELECT
            n.namespace AS namespace,
            n.description AS description,
            n.kind AS kind,
            n.owner_email AS owner_email,
            n.requires_approval AS requires_approval,
            toString(n.visibility) AS visibility,
            toString(n.join_policy) AS join_policy,
            n.created_at AS created_at,
            ifNull(g.member_count, 0) AS member_count
        FROM (SELECT * FROM loom.namespaces FINAL) AS n
        LEFT JOIN (
            SELECT org, namespace, count(DISTINCT subject) AS member_count
            FROM loom.namespace_grants FINAL
            WHERE revoked_at IS NULL
            GROUP BY org, namespace
        ) AS g
        ON g.org = n.org AND g.namespace = n.namespace
        WHERE {" AND ".join(where)}
        ORDER BY member_count DESC, namespace
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    guides = await namespace_guides.summaries(input.org, [str(r["namespace"]) for r in rows])
    caller = resolve_subject(None)
    mine: dict[str, tuple[_Standing, str]] = await _my_standing(input.org, caller) if caller else {}
    from server.code.domain.index.repos import config_entry

    out: list[_BrowseEntry] = []
    for r in rows:
        policy = _jp.effective_join_policy(r)
        if policy == _jp.CLOSED and not input.include_closed:
            continue
        standing: tuple[_Standing, str] = mine.get(r["namespace"], ("none", ""))
        is_code = str(r.get("kind") or "memory") == "code"
        cfg = config_entry(input.org, r["namespace"]) if is_code else None
        guide = guides.get(r["namespace"]) if _guide_shown(r, standing, hidden) else None
        out.append(
            _BrowseEntry(
                kind=str(r.get("kind") or "memory"),
                namespace=r["namespace"],
                description=r["description"],
                owner_email=r["owner_email"],
                member_count=int(r["member_count"]),
                requires_approval=bool(r.get("requires_approval", False)),
                created_at=r["created_at"],
                join_policy=policy,
                visibility=str(r.get("visibility") or "public"),
                my_status=standing[0],
                my_role=standing[1],
                repo=str(getattr(cfg, "repo", "") or ""),
                branch=str(getattr(cfg, "git_branch", "") or ""),
                guide_revision=int(guide["revision"]) if guide else 0,
                guide_summary=str(guide["summary"]) if guide else "",
            )
        )
    return NamespaceBrowseOutput(rows=out)
