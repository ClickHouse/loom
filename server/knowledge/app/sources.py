"""knowledge.source.* — the document sources a repository's maintainers register
for Loom to pull from.

Every op is addressed by the repository's trunk code namespace (the one its
documents live in), so the transport's namespace gate decides who may call:
readers list and inspect, the namespace's admins add, check, rotate, remove and
ask for a sync. The credential is sealed at rest under its own purpose, held in
memory only for a probe, and never returned or audited.

Nothing here fetches documents: the sync worker does, outside the request path
(`server.knowledge.app.source_sync`). `sync_now` records what the worker should do
next in the source's sync state; `remove(delete_documents=true)` records it on the
inactive registry row it writes.

Every write of a namespace's sources, registry rows and sync state alike (add,
rotate, remove, sync_now and code.repo.remove's retirement), runs under
`sources_lease`, which serializes them; the probes run outside it.

Which documents are a source's: see `SYNC_WORKER_ACTOR`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from server.auth.context import current_caller
from server.core.ch import sequential_reads
from server.core.datetimes import as_aware_utc
from server.core.errors import (
    NotFoundError,
    OverloadedError,
    SourceReplacedError,
    ValidationError,
)
from server.core.secrets_at_rest import seal, unseal
from server.knowledge.api.models_sources import (
    KnowledgeSourceAddInput,
    KnowledgeSourceAddOutput,
    KnowledgeSourceCheckInput,
    KnowledgeSourceCheckOutput,
    KnowledgeSourceDocument,
    KnowledgeSourceEntry,
    KnowledgeSourceInspectInput,
    KnowledgeSourceInspectOutput,
    KnowledgeSourceListInput,
    KnowledgeSourceListOutput,
    KnowledgeSourceProbe,
    KnowledgeSourceRemoveInput,
    KnowledgeSourceRemoveOutput,
    KnowledgeSourceRotateInput,
    KnowledgeSourceRotateOutput,
    KnowledgeSourceSync,
    KnowledgeSourceSyncNowInput,
    KnowledgeSourceSyncNowOutput,
)
from server.knowledge.app import source_probes
from server.knowledge.domain import sources as domain
from server.knowledge.domain.sources import KnowledgeSource
from server.knowledge.store import sources as store
from server.settings import settings

_CREDENTIAL_PURPOSE = "knowledge_sources.credential"


def _iso(value: Any) -> str:
    return as_aware_utc(value).isoformat() if isinstance(value, datetime) else ""


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _seal(credential: str) -> str:
    try:
        return seal(_CREDENTIAL_PURPOSE, credential)
    except RuntimeError:
        raise ValidationError(
            "this deployment has no LOOM_JWT_SECRET, so a credential cannot be stored",
            code="NO_ROOT_SECRET",
        ) from None


async def is_trunk_namespace(org: str, namespace: str) -> bool:
    """Whether `namespace` is a repository's trunk code namespace, the one its
    documents live in."""
    from server.code.app.index import repo_registry
    from server.code.domain.index.repos import docs_namespaces

    # The trunk is decided across all of a repository's branches and the operator's
    # entries, so it takes the whole registry: the cached snapshot, not a forced read.
    await repo_registry.refresh_db_repos()
    return namespace in {ns for (o, _repo), ns in docs_namespaces().items() if o == org}


async def _require_trunk_namespace(org: str, namespace: str) -> None:
    """Refuse to register a source on a namespace that is no repository's trunk."""
    if not await is_trunk_namespace(org, namespace):
        raise ValidationError(
            f"{namespace!r} is not a repository's trunk code namespace; sources are "
            "registered on the namespace the repository's documents live in",
            code="NOT_A_CODE_NAMESPACE",
        )


def _require_kind(kind: str) -> None:
    if kind not in domain.KINDS:
        raise ValidationError(
            f"kind {kind!r} is not served; one of: {', '.join(domain.KINDS)}", code="BAD_KIND"
        )


async def _source_in_repository(org: str, namespace: str, name: str) -> KnowledgeSource:
    """The source, for an op that uses it: refused when its namespace is no longer
    a repository's trunk code namespace (a repository retired outside
    `code.repo.remove`, or a branch that now outranks it)."""
    src = await _source_or_404(org, namespace, name)
    if not await is_trunk_namespace(org, namespace):
        raise ValidationError(
            f"the repository of {namespace!r} is gone or no longer has its trunk there, so "
            f"source {src.name!r} is not used; knowledge.source.remove retires it",
            code="NOT_A_CODE_NAMESPACE",
        )
    return src


async def _source_or_404(org: str, namespace: str, name: str) -> KnowledgeSource:
    src = await store.get_source(org, namespace, domain.normalize_name(name))
    if src is None:
        raise NotFoundError(
            f"no document source {name!r} in {namespace!r}", code="SOURCE_NOT_FOUND"
        )
    return src


async def _probe_or_refuse(
    kind: str, config: dict[str, Any], credential: str
) -> KnowledgeSourceProbe:
    try:
        found = await source_probes.probe(kind, config, credential)
    except source_probes.CredentialRefused as e:
        raise ValidationError(
            f"the source refused the credential: {e}", code="CREDENTIAL_REFUSED"
        ) from None
    except source_probes.SourceUnreachable as e:
        raise ValidationError(
            f"the source could not be listed: {e}", code="SOURCE_UNREACHABLE"
        ) from None
    return KnowledgeSourceProbe(documents=found.documents, more=found.more, sample=found.sample)


def _sync(state: dict[str, str] | None) -> KnowledgeSourceSync:
    return KnowledgeSourceSync(**domain.sync_of((state or {}).get("detail", "")))


async def handle_knowledge_source_list(
    input: KnowledgeSourceListInput,
) -> KnowledgeSourceListOutput:
    found = await store.list_sources(input.org, input.namespace)
    states = await store.read_states(input.org, input.namespace, [s.name for s in found])
    return KnowledgeSourceListOutput(
        sources=[
            KnowledgeSourceEntry(
                name=s.name,
                kind=s.kind,
                config=s.config,
                created_by=s.created_by,
                created_at=_iso(s.created_at),
                sync=_sync(states.get(s.name)),
            )
            for s in found
        ],
        enabled=bool(settings.knowledge_source_sync_enabled),
    )


async def handle_knowledge_source_inspect(
    input: KnowledgeSourceInspectInput,
) -> KnowledgeSourceInspectOutput:
    src = await _source_or_404(input.org, input.namespace, input.name)
    states = await store.read_states(src.org, src.namespace, [src.name])
    docs = await store.source_documents(src.org, src.namespace, src.name)
    return KnowledgeSourceInspectOutput(
        name=src.name,
        kind=src.kind,
        config=src.config,
        created_by=src.created_by,
        created_at=_iso(src.created_at),
        sync=_sync(states.get(src.name)),
        documents=[
            KnowledgeSourceDocument(
                source_uri=str(d.get("source_uri") or ""),
                title=str(d.get("title") or ""),
                source_revision=str(d.get("source_revision") or ""),
                ingested_at=_iso(d.get("ingested_at")),
            )
            for d in docs
        ],
    )


async def handle_knowledge_source_add(input: KnowledgeSourceAddInput) -> KnowledgeSourceAddOutput:
    name = domain.normalize_name(input.name)
    if not domain.valid_name(name):
        raise ValidationError(
            "source name: 2-32 lower-case letters, digits, - or _, starting with a letter",
            code="BAD_NAME",
        )
    kind = input.kind.strip().lower()
    _require_kind(kind)
    try:
        config = domain.parse_config(kind, input.config)
    except domain.SourceConfigError as e:
        raise ValidationError(str(e), code="BAD_CONFIG") from None
    if not input.credential.strip():
        raise ValidationError("a credential is required", code="CREDENTIAL_REFUSED")
    await _require_trunk_namespace(input.org, input.namespace)
    await _require_free(input.org, input.namespace, name)
    # Sealed before the probe, so a deployment that cannot store it says so first.
    credential_enc = _seal(input.credential)
    probe = await _probe_or_refuse(kind, config, input.credential)
    out = KnowledgeSourceAddOutput(
        name=name,
        kind=kind,
        namespace=input.namespace,
        config=config,
        probe=probe,
        dry_run=input.dry_run,
    )
    if input.dry_run:
        return out
    subject, _org = current_caller()
    async with sources_lease(input.org, input.namespace):
        # Under the lease nothing else writes this namespace's sources, so the checks
        # hold until the insert: the probe took seconds and ran outside it.
        if not await _registered(input.org, input.namespace):
            raise ValidationError(
                f"the repository of {input.namespace!r} was removed; no source was registered",
                code="NOT_A_CODE_NAMESPACE",
            )
        await _require_trunk_namespace(input.org, input.namespace)
        await _require_free(input.org, input.namespace, name)
        # A new source starts with a full listing, never on the stamp an earlier
        # source of this name left; reset before the row goes active.
        await store.write_state(input.org, input.namespace, name, state_value="", detail={})
        await store.insert_source(
            KnowledgeSource(
                org=input.org,
                namespace=input.namespace,
                name=name,
                kind=kind,
                source_id=str(uuid.uuid4()),
                config=config,
                credential_enc=credential_enc,
                created_by=subject,
            )
        )
    return out


@contextlib.asynccontextmanager
async def sources_lease(org: str, namespace: str) -> AsyncIterator[None]:
    """Hold the lease that serializes every write of a namespace's sources. Busy
    past the wait: OVERLOADED, retry; a lease store that cannot answer refuses
    (LeaseUnavailable), never writes unlocked."""
    from server.core import leases

    # The TTL outlives the few reads and inserts of one write with room to spare (a
    # holder that dies is taken over after it); a second write waits up to 5 s.
    async with leases.holding(
        f"knowledge-source:{org}:{namespace}", ttl_s=60.0, wait_s=5.0
    ) as held:
        if not held:
            raise OverloadedError(
                f"another change to the document sources of {namespace!r} is running; retry"
            )
        yield


async def _registered(org: str, namespace: str) -> bool:
    """Whether the namespace's repository is still registered: its loom.code_repos
    row for one a maintainer added; one the operator's configuration owns cannot be
    removed by code.repo.remove, so it counts as registered."""
    from server.code.app.index import repo_registry
    from server.code.domain.index.repos import repo_for_namespace

    cfg = repo_for_namespace(org, namespace)
    if cfg is None:
        return False
    if cfg.source != "registry":
        return True
    return await repo_registry.row_active(org, namespace)


async def _require_free(org: str, namespace: str, name: str) -> None:
    """Refuse a taken name, a full namespace, and a name an earlier source's live
    documents still carry."""
    with sequential_reads():
        if await store.get_source(org, namespace, name) is not None:
            raise ValidationError(
                f"a source named {name!r} exists in {namespace!r}", code="SOURCE_EXISTS"
            )
        limit = int(settings.knowledge_sources_max_per_namespace)
        if await store.active_count(org, namespace) >= limit:
            raise ValidationError(
                f"{namespace!r} has {limit} sources, the most it may "
                "(LOOM_KNOWLEDGE_SOURCES_MAX_PER_NAMESPACE); remove one first",
                code="SOURCE_QUOTA",
            )
        documents = await store.count_documents(org, namespace, name)
        earlier = await store.latest_source(org, namespace, name)
    # A new source of the same name would take the earlier source's documents over:
    # its sync would rewrite or retire them.
    if documents and earlier is not None and earlier.pending_delete:
        raise ValidationError(
            f"documents of the removed source {name!r} are still waiting to be retired "
            f"({documents} left); register the new source under another name, or once "
            "they are gone",
            code="SOURCE_DELETE_PENDING",
        )
    if documents:
        raise ValidationError(
            f"the removed source {name!r} kept its documents ({documents} left); delete them "
            "(knowledge.delete) before reusing the name, or register the source under "
            "another name",
            code="SOURCE_DOCUMENTS_KEPT",
        )


async def handle_knowledge_source_check(
    input: KnowledgeSourceCheckInput,
) -> KnowledgeSourceCheckOutput:
    src = await _source_in_repository(input.org, input.namespace, input.name)
    checked_at = _now_iso()
    credential = unseal(_CREDENTIAL_PURPOSE, src.credential_enc)
    if not credential:
        # Fail closed: a credential that no longer opens is never read as "no auth".
        return KnowledgeSourceCheckOutput(
            name=src.name,
            reachable=False,
            summary="credential_invalid: the stored credential no longer opens; rotate it",
            checked_at=checked_at,
        )
    try:
        found = await source_probes.probe(src.kind, src.config, credential)
    except source_probes.CredentialRefused as e:
        return KnowledgeSourceCheckOutput(
            name=src.name,
            reachable=False,
            summary=f"credential refused: {e}",
            checked_at=checked_at,
        )
    except source_probes.SourceUnreachable as e:
        return KnowledgeSourceCheckOutput(
            name=src.name, reachable=False, summary=f"unreachable: {e}", checked_at=checked_at
        )
    plus = "+" if found.more else ""
    return KnowledgeSourceCheckOutput(
        name=src.name,
        reachable=True,
        documents=found.documents,
        more=found.more,
        sample=found.sample,
        summary=f"{found.documents}{plus} documents listed",
        checked_at=checked_at,
    )


async def handle_knowledge_source_rotate(
    input: KnowledgeSourceRotateInput,
) -> KnowledgeSourceRotateOutput:
    src = await _source_in_repository(input.org, input.namespace, input.name)
    if not input.credential.strip():
        raise ValidationError("a credential is required", code="CREDENTIAL_REFUSED")
    credential_enc = _seal(input.credential)
    probe = await _probe_or_refuse(src.kind, src.config, input.credential)
    async with sources_lease(input.org, input.namespace):
        # The probe ran outside the lease: the source may have been removed, or removed
        # and added again as another source this credential was not probed against.
        with sequential_reads():
            fresh = await _source_or_404(input.org, input.namespace, src.name)
        if fresh.source_id != src.source_id:
            raise SourceReplacedError(
                f"source {src.name!r} was removed and added again while its new credential "
                "was probed; rotate the current source"
            )
        # Only the credential changes; every other column is carried forward.
        await store.insert_source(dataclasses.replace(fresh, credential_enc=credential_enc))
    return KnowledgeSourceRotateOutput(name=src.name, probe=probe, rotated_at=_now_iso())


async def handle_knowledge_source_remove(
    input: KnowledgeSourceRemoveInput,
) -> KnowledgeSourceRemoveOutput:
    name = domain.normalize_name(input.name)
    if domain.normalize_name(input.confirm) != name:
        raise ValidationError(
            f"type the source name ({name}) in `confirm` to retire it", code="CONFIRM_REQUIRED"
        )
    async with sources_lease(input.org, input.namespace):
        with sequential_reads():
            src = await _source_or_404(input.org, input.namespace, name)
            pending = 0
            if input.delete_documents:
                pending = await store.count_documents(src.org, src.namespace, src.name)
        # The delete request rides on the inactive registry row, which no sync-state
        # write touches. It is recorded even with no documents counted now: a sync
        # turn running meanwhile may still write one, and the worker clears the
        # request once none is left.
        retired = dataclasses.replace(
            src, active=False, credential_enc="", pending_delete=input.delete_documents
        )
        await store.insert_source(retired)
    return KnowledgeSourceRemoveOutput(name=src.name, documents_pending=pending)


async def handle_knowledge_source_sync_now(
    input: KnowledgeSourceSyncNowInput,
) -> KnowledgeSourceSyncNowOutput:
    src = await _source_in_repository(input.org, input.namespace, input.name)
    async with sources_lease(input.org, input.namespace):
        # A request meant for the source this call read must not be recorded on another
        # source added under the name meanwhile.
        with sequential_reads():
            fresh = await _source_or_404(input.org, input.namespace, src.name)
        if fresh.source_id != src.source_id:
            raise SourceReplacedError(
                f"source {src.name!r} was removed and added again; call "
                "knowledge.source.sync_now again for the current source"
            )
        await _update_state(fresh, {"requested_at": _now_iso(), "full": bool(input.full)})
    return KnowledgeSourceSyncNowOutput(
        name=src.name, enabled=bool(settings.knowledge_source_sync_enabled)
    )


async def request_full_sync(src: KnowledgeSource) -> None:
    """Record a full sync request for `src`, as `sync_now(full=true)` does, unless
    one is already waiting: `loom knowledge sync --full` run again while a turn
    has not finished the request goes on with it rather than starting over."""
    async with sources_lease(src.org, src.namespace):
        with sequential_reads():
            states = await store.read_states(src.org, src.namespace, [src.name])
        detail = domain.json_object((states.get(src.name) or {}).get("detail", ""))
        if detail.get("full") and detail.get("requested_at"):
            return
        detail.update(requested_at=_now_iso(), full=True)
        await store.write_state(
            src.org,
            src.namespace,
            src.name,
            state_value=(states.get(src.name) or {}).get("state_value", ""),
            detail=detail,
        )


async def _update_state(src: KnowledgeSource, changes: dict[str, Any]) -> None:
    """Merge `changes` into the source's sync state, keeping its stamp."""
    with sequential_reads():
        states = await store.read_states(src.org, src.namespace, [src.name])
    state = states.get(src.name) or {}
    detail = domain.json_object(state.get("detail", ""))
    detail.update(changes)
    await store.write_state(
        src.org,
        src.namespace,
        src.name,
        state_value=state.get("state_value", ""),
        detail=detail,
    )


async def deactivate_namespace_sources(org: str, namespace: str) -> list[str]:
    """Retire every active source of a namespace whose repository is being removed:
    an inactive row with the credential cleared, so nothing reads or opens it again.
    The caller holds `sources_lease`. The names retired."""
    with sequential_reads():
        found = await store.list_sources(org, namespace)
    for src in found:
        await store.insert_source(dataclasses.replace(src, active=False, credential_enc=""))
    return sorted(src.name for src in found)


def source_add_audit_subject(input: KnowledgeSourceAddInput, response: Any) -> dict[str, Any]:
    """What an add's audit row records: the source as stored, never the
    credential. `loom.audit_log` outlives the source and must hold no secret."""
    return {
        "org": input.org,
        "namespace": input.namespace,
        "name": response["name"],
        "kind": response["kind"],
        "config": response["config"],
        "dry_run": input.dry_run,
    }


def source_rotate_audit_subject(input: KnowledgeSourceRotateInput, response: Any) -> dict[str, Any]:
    """Which source's credential was replaced, never the credential."""
    return {"org": input.org, "namespace": input.namespace, "name": response["name"]}


__all__ = [
    "deactivate_namespace_sources",
    "is_trunk_namespace",
    "request_full_sync",
    "sources_lease",
    "handle_knowledge_source_add",
    "handle_knowledge_source_check",
    "handle_knowledge_source_inspect",
    "handle_knowledge_source_list",
    "handle_knowledge_source_remove",
    "handle_knowledge_source_rotate",
    "handle_knowledge_source_sync_now",
    "source_add_audit_subject",
    "source_rotate_audit_subject",
]
