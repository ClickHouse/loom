"""knowledge.source.* — the document sources a namespace's admins register for
Loom to pull from.

Every op is addressed by the namespace the source's documents live in, so the
transport's namespace gate decides who may call:
readers list and inspect, the namespace's admins add, check, rotate, remove and
ask for a sync. The credential is sealed at rest under its own purpose, held in
memory only for a probe, and never returned or audited.

Nothing here fetches documents: the sync worker does, outside the request path
(`server.knowledge.app.source_sync`). `sync_now` records what the worker should do
next in the source's sync state; `remove(delete_documents=true)` records it on the
inactive registry row it writes.

Every write of a namespace's sources, registry rows and sync state alike (add,
rotate, remove, sync_now, and the retirement when the namespace goes: code.repo.remove,
namespace.delete, collection.delete and the erasure step `knowledge_sources.retire`),
runs under `sources_lease`, which serializes them; the probes run outside it.

Which documents are a source's: see `SYNC_WORKER_ACTOR`.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from server.auth.context import current_caller
from server.auth.personal_namespace import is_own_namespace
from server.auth.sharing import is_collection
from server.core import leases
from server.core.ch import sequential_reads
from server.core.datetimes import as_aware_utc
from server.core.errors import (
    ErasureRunningError,
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


async def _source_refusal(org: str, namespace: str) -> ValidationError | None:
    """Why `namespace` takes no source, or None. A person's own place (PERSONAL_NAMESPACE):
    a user erasure erases what the person wrote there, and a source's documents are the
    sync worker's, so they would stay. A namespace whose directory row is inactive, or a
    collection the tree no longer holds (NAMESPACE_INACTIVE): the sync skips it and no
    delete reaches it."""
    # Through identity's app layer (the package contract allows no cross-plane store
    # import), imported here since server.identity.app.namespace imports this module.
    from server.identity.app.collections import in_tree

    personal = ValidationError(
        f"{namespace!r} is a person's own collection or namespace and holds no "
        "document source; register it on a shared one",
        code="PERSONAL_NAMESPACE",
    )
    if is_own_namespace(namespace):
        return personal
    row = await store.directory_row(org, namespace)
    if row.claimed_at_signup:
        return personal
    gone = is_collection(namespace) and not await in_tree(org, namespace)
    if gone or row.inactive:
        return ValidationError(
            f"{namespace!r} is not an active namespace", code="NAMESPACE_INACTIVE"
        )
    return None


async def _require_no_erasure(org: str, namespace: str) -> None:
    """Refuse a new source while an erasure of the namespace runs: its first step read the
    sources already, so what a later source wrote would outlast the erasure."""
    # Imported here: the erasure saga imports this module.
    from server.erasure.app import ledger_reads
    from server.erasure.domain.scopes import may_cover

    for row in await ledger_reads.open_namespace_erasures(org):
        if may_cover(str(row["scope_type"]), str(row["scope_value"]), namespace):
            raise ErasureRunningError(
                f"an erasure of {namespace!r} is running; add the source once it finishes"
            )


async def _require_source_namespace(org: str, namespace: str) -> None:
    if refusal := await _source_refusal(org, namespace):
        raise refusal


def _require_kind(kind: str) -> None:
    if kind not in domain.KINDS:
        raise ValidationError(
            f"kind {kind!r} is not served; one of: {', '.join(domain.KINDS)}", code="BAD_KIND"
        )


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
        accepts_sources=await _source_refusal(input.org, input.namespace) is None,
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
    await _require_source_namespace(input.org, input.namespace)
    await _require_no_erasure(input.org, input.namespace)
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
        # A namespace deleted during the probe (code.repo.remove deletes its own) has
        # retired its sources under this lease; the add must not land after it.
        with sequential_reads():
            await _require_source_namespace(input.org, input.namespace)
            await _require_no_erasure(input.org, input.namespace)
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


def _sources_lease_name(org: str, namespace: str) -> str:
    return f"knowledge-source:{org}:{namespace}"


@contextlib.asynccontextmanager
async def sources_lease(org: str, namespace: str, *, wait_s: float = 5.0) -> AsyncIterator[None]:
    """Hold the lease that serializes every write of a namespace's sources. Busy
    past `wait_s`: OVERLOADED, retry; a lease store that cannot answer refuses
    (LeaseUnavailable), never writes unlocked."""
    # The TTL outlives the few reads and inserts of one write with room to spare (a
    # holder that dies is taken over after it).
    async with leases.holding(
        _sources_lease_name(org, namespace), ttl_s=60.0, wait_s=wait_s
    ) as held:
        if not held:
            raise OverloadedError(
                f"another change to the document sources of {namespace!r} is running; retry"
            )
        yield


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
            f"the removed source {name!r} kept its documents ({documents} left); retire them "
            "with knowledge.source.remove and delete_documents=true before reusing the "
            "name, or register the source under another name",
            code="SOURCE_DOCUMENTS_KEPT",
        )


async def handle_knowledge_source_check(
    input: KnowledgeSourceCheckInput,
) -> KnowledgeSourceCheckOutput:
    src = await _source_or_404(input.org, input.namespace, input.name)
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
    src = await _source_or_404(input.org, input.namespace, input.name)
    if not input.credential.strip():
        raise ValidationError("a credential is required", code="CREDENTIAL_REFUSED")
    credential_enc = _seal(input.credential)
    await _require_source_namespace(input.org, input.namespace)
    probe = await _probe_or_refuse(src.kind, src.config, input.credential)
    async with sources_lease(input.org, input.namespace):
        # The probe ran outside the lease: the source may have been removed, or removed
        # and added again as another source this credential was not probed against.
        with sequential_reads():
            fresh = await _source_or_404(input.org, input.namespace, src.name)
            # A source of a namespace that is gone gets no new credential.
            await _require_source_namespace(input.org, input.namespace)
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
            src = await store.get_source(input.org, input.namespace, name)
            if src is None and input.delete_documents:
                # A retired source (removed keeping its documents, or retired with its
                # namespace) can still have its documents retired.
                src = await store.latest_source(input.org, input.namespace, name)
            if src is None:
                raise NotFoundError(
                    f"no document source {name!r} in {input.namespace!r}",
                    code="SOURCE_NOT_FOUND",
                )
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
    src = await _source_or_404(input.org, input.namespace, input.name)
    await _require_source_namespace(input.org, input.namespace)
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
        state = await _read_state(fresh)
        await _update_state(fresh, state, {"requested_at": _now_iso(), "full": bool(input.full)})
    return KnowledgeSourceSyncNowOutput(
        name=src.name, enabled=bool(settings.knowledge_source_sync_enabled)
    )


async def request_full_sync(src: KnowledgeSource) -> None:
    """Record a full sync request for `src`, as `sync_now(full=true)` does, unless
    one is already waiting: `loom knowledge sync --full` run again while a turn
    has not finished the request goes on with it rather than starting over."""
    async with sources_lease(src.org, src.namespace):
        state = await _read_state(src)
        detail = domain.json_object(state.get("detail", ""))
        if detail.get("full") and detail.get("requested_at"):
            return
        await _update_state(src, state, {"requested_at": _now_iso(), "full": True})


async def _read_state(src: KnowledgeSource) -> dict[str, str]:
    """The source's stored sync state, {} when it has none."""
    with sequential_reads():
        states = await store.read_states(src.org, src.namespace, [src.name])
    return states.get(src.name) or {}


async def _update_state(
    src: KnowledgeSource, state: dict[str, str], changes: dict[str, Any]
) -> None:
    """Merge `changes` into the sync state `state` read under the lease, keeping its
    stamp."""
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
    """Retire every active source of a namespace that is going away (its repository
    removed, the namespace deleted or erased): an inactive row with the credential
    cleared, so nothing reads or opens it again. The caller holds `sources_lease`.
    The names retired."""
    with sequential_reads():
        found = await store.list_sources(org, namespace)
    for src in found:
        await store.insert_source(dataclasses.replace(src, active=False, credential_enc=""))
    return sorted(src.name for src in found)


async def retire_sources_of(org: str, namespaces: list[str], *, wait_s: float) -> int:
    """Retire the sources of `namespaces` (an erasure's), then wait up to `wait_s` in all
    for every sync turn of theirs still running. A source add while the saga runs is
    refused (`ERASURE_RUNNING`) under the namespace's lease, so once no add holds that
    lease, one sequential read finds every source an add wrote."""
    deadline = time.monotonic() + wait_s
    with sequential_reads():
        for namespace in sorted(set(namespaces)):
            await _wait_until_free(
                _sources_lease_name(org, namespace),
                deadline,
                f"a change to the document sources of {namespace!r} is running; retry",
            )
        found = await store.sources_in(org, namespaces)
    waits = set(found)
    retired = 0
    for namespace in sorted({ns for ns, _ in found}):
        # The lease wait counts against the step's one deadline too.
        async with sources_lease(org, namespace, wait_s=max(0.0, deadline - time.monotonic())):
            names = await deactivate_namespace_sources(org, namespace)
        retired += len(names)
        waits.update((namespace, name) for name in names)
    # Wait out turns that read their source before the retirement: inactive names too,
    # since a retried step finds none active.
    for namespace, name in sorted(waits):
        await _wait_until_free(
            domain.turn_lease_name(org, namespace, name),
            deadline,
            f"a sync turn of {name!r} in {namespace!r} is running; retry",
        )
    return retired


async def _wait_until_free(lease_name: str, deadline: float, busy: str) -> None:
    """Return once no live holder keeps `lease_name`; past `deadline`, OVERLOADED."""
    while await leases.has_live_holder(lease_name):
        if time.monotonic() >= deadline:
            raise OverloadedError(busy)
        await asyncio.sleep(0.5)


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
    "retire_sources_of",
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
