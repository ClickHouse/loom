"""Shared recording setup and generic source retirement under destination leases.

MCP connection and selected-resource controls use their dedicated common-protocol
operations. Unsupported stored registrations remain opaque; retiring them and
explicitly erasing their copies require the existing destination authority.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

from server.auth import authz
from server.auth.context import current_auth, current_caller
from server.auth.personal_namespace import is_own_namespace
from server.auth.sharing import is_collection
from server.core import ch, leases, request_work
from server.core.ch import sequential_reads
from server.core.datetimes import as_aware_utc
from server.core.diagnostics import private_work
from server.core.errors import (
    AuthInsufficientError,
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
from server.knowledge.app import source_access, source_probes, source_publication, source_titles
from server.knowledge.domain import sources as domain
from server.knowledge.domain.sources import KnowledgeSource
from server.knowledge.store import sources as store
from server.operations.base import Role
from server.settings import settings

_CREDENTIAL_PURPOSE = "knowledge_sources.credential"
# The purpose of a credential in `secret_enc`, which a build without items never reads.
_SECRET_PURPOSE = "knowledge_sources.secret"
# The migration that adds `source_kind`, `items_json` and `secret_enc`: a source stored
# with `kind` empty waits until every live process runs it.
_ITEMS_MIGRATION = 96
# Unreadable links a BAD_LINK message names, at most; the rest are counted.
_LINKS_NAMED = 10  # not a setting: a bound on one message


def _iso(value: Any) -> str:
    return as_aware_utc(value).isoformat() if isinstance(value, datetime) else ""


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def credential_of(src: KnowledgeSource) -> str:
    """The source's credential, '' when it no longer opens."""
    if not domain.served(src):
        return ""
    return unseal(_CREDENTIAL_PURPOSE, src.credential_enc)


async def credential_for(src: KnowledgeSource, *, deadline: float) -> str:
    del deadline
    if not domain.served(src):
        raise ValidationError("this source credential is unavailable", code="CREDENTIAL_REFUSED")
    return credential_of(src)


def _sealed(purpose: str, credential: str) -> str:
    """`credential` sealed for `purpose`; NO_ROOT_SECRET when the deployment cannot."""
    try:
        return seal(purpose, credential)
    except RuntimeError:
        raise ValidationError(
            "this deployment has no LOOM_JWT_SECRET, so a credential cannot be stored",
            code="NO_ROOT_SECRET",
        ) from None


def _require_sealable(credential: str) -> None:
    """NO_ROOT_SECRET before a probe when this deployment cannot store a credential."""
    _sealed(_CREDENTIAL_PURPOSE, credential)


def _with_credential(src: KnowledgeSource, credential: str) -> KnowledgeSource:
    if not domain.served(src):
        raise ValidationError("this source kind is unavailable", code="BAD_KIND")
    return dataclasses.replace(
        src, credential_enc=_sealed(_CREDENTIAL_PURPOSE, credential), secret_enc=""
    )


def _probe_answer(found: source_probes.Probe) -> KnowledgeSourceProbe:
    return KnowledgeSourceProbe(
        documents=found.documents,
        more=found.more,
        sample=found.sample,
    )


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


async def _require_no_erasure(org: str, namespace: str, *, ch: Any = None) -> None:
    """Refuse a new source while an erasure of the namespace will still run: its first step
    may have read the sources already, so what a later source wrote would outlast it."""
    # Imported here: the erasure saga imports this module.
    from server.erasure.app import ledger_reads
    from server.erasure.domain.scopes import may_cover

    for row in await ledger_reads.open_namespace_erasures(org, ch=ch):
        if may_cover(str(row["scope_type"]), str(row["scope_value"]), namespace):
            raise ErasureRunningError(
                f"an erasure of {namespace!r} will still run; change its sources once it finishes"
            )


async def _require_source_namespace(org: str, namespace: str) -> None:
    if refusal := await _source_refusal(org, namespace):
        raise refusal


def _require_kind(kind: str) -> None:
    if kind not in domain.KINDS:
        raise ValidationError(
            f"kind {domain.shown(kind)} is not served; one of: {', '.join(domain.KINDS)}",
            code="BAD_KIND",
        )


async def _source_or_404(org: str, namespace: str, name: str) -> KnowledgeSource:
    src = await store.get_source(org, namespace, domain.normalize_name(name))
    if src is not None and src.kind == "mcp":
        raise NotFoundError("no document source", code="SOURCE_NOT_FOUND")
    if src is None:
        raise NotFoundError(
            f"no document source {domain.shown(name)} in {namespace!r}", code="SOURCE_NOT_FOUND"
        )
    if domain.opaque_payload(
        kind=src.kind, mode=src.credential_mode, version=src.source_contract_version
    ):
        _require_served(src)
    return src


def _require_served(src: KnowledgeSource) -> None:
    """This build's ops serve only sources shown to every reader of the namespace; its
    sync worker writes a mirrored `gdrive` one into the mirror (`mirror_synced`), and
    a later build serves it."""
    if not domain.served(src):
        raise ValidationError(
            f"source {src.name!r} is not served by this server yet (access "
            f"{src.access!r}); retry once the deployment's upgrade completes",
            code="SOURCE_NOT_SERVED",
        )


@contextlib.contextmanager
def _store_refusals() -> Iterator[None]:
    try:
        yield
    except source_probes.CredentialRefused:
        raise ValidationError(
            "the source refused the credential", code="CREDENTIAL_REFUSED"
        ) from None
    except source_probes.SourceUnreachable:
        raise ValidationError("the source could not be listed", code="SOURCE_UNREACHABLE") from None


def _sync(src: KnowledgeSource, state: dict[str, str] | None) -> KnowledgeSourceSync:
    return KnowledgeSourceSync(**domain.sync_shown(src, (state or {}).get("detail", "")))


async def _details_allowed(src: KnowledgeSource) -> bool:
    if src.kind == "mcp":
        return False
    if src.credential_mode == "shared":
        return True
    if src.credential_mode != "connected" or not request_work.mark_mirrored():
        return False
    from server.knowledge.app import connected_accounts as accounts

    with private_work():
        try:
            auth = current_auth()
            org, owner = await accounts.own_subject(auth)
            return org == src.org and owner == src.owner_subject and current_auth() is auth
        except Exception:  # noqa: BLE001 — unverifiable retained source details stay private
            return False


async def handle_knowledge_source_list(
    input: KnowledgeSourceListInput,
) -> KnowledgeSourceListOutput:
    ch.set_query_tag("knowledge.source.list")
    found = await store.list_sources(input.org, input.namespace, include_mcp=False)
    states = await store.read_states(input.org, input.namespace, [s.name for s in found])
    entries = []
    for s in found:
        details = await _details_allowed(s)
        entries.append(
            KnowledgeSourceEntry(
                name=s.name,
                kind=s.kind,
                credential_mode=s.credential_mode,
                access=s.access,
                owner_subject=s.owner_subject if details else "",
                config=(domain.id_list_config(s) or {}) if details else {},
                created_by=s.created_by if details else "",
                created_at=_iso(s.created_at),
                sync=_sync(s, states.get(s.name)) if details else KnowledgeSourceSync(),
            )
        )
    return KnowledgeSourceListOutput(
        accepts_sources=await _source_refusal(input.org, input.namespace) is None,
        sources=entries,
        enabled=bool(settings.knowledge_source_sync_enabled),
    )


async def handle_knowledge_source_inspect(
    input: KnowledgeSourceInspectInput,
) -> KnowledgeSourceInspectOutput:
    plan = await source_access.prepare(input.org, [input.namespace])
    with private_work() if plan.marked else contextlib.nullcontext():
        src = await _source_or_404(input.org, input.namespace, input.name)
        states = await store.read_states(src.org, src.namespace, [src.name])
        details = await _details_allowed(src)
        output = KnowledgeSourceInspectOutput(
            name=src.name,
            kind=src.kind,
            credential_mode=src.credential_mode,
            access=src.access,
            owner_subject=src.owner_subject if details else "",
            config=(domain.id_list_config(src) or {}) if details else {},
            created_by=src.created_by if details else "",
            created_at=_iso(src.created_at),
            sync=_sync(src, states.get(src.name)) if details else KnowledgeSourceSync(),
        )
        if src.access == domain.MIRRORED_ACCESS and details:
            titles = await source_titles.collect(
                plan, src.namespace, limit=200, source_id=src.source_id
            )
            output.documents = [
                KnowledgeSourceDocument(
                    source_uri=title.source_uri,
                    title=title.title,
                    source_revision=title.source_revision,
                    ingested_at=_iso(title.ingested_at),
                )
                for title in titles
            ]
            for title in titles:
                source_publication.retain(title.proof)
        elif domain.served(src):
            docs = await store.source_documents(src.org, src.namespace, domain.source_tag(src.name))
            output.documents = [
                KnowledgeSourceDocument(
                    source_uri=str(d.get("source_uri") or ""),
                    title=str(d.get("title") or ""),
                    source_revision=str(d.get("source_revision") or ""),
                    ingested_at=_iso(d.get("ingested_at")),
                )
                for d in docs
            ]
        return output


async def handle_knowledge_source_add(input: KnowledgeSourceAddInput) -> KnowledgeSourceAddOutput:
    name = domain.normalize_name(input.name)
    if not domain.valid_name(name):
        raise ValidationError(
            "source name must be 2-32 lower-case letters, digits, - or _", code="BAD_NAME"
        )
    kind = input.kind
    _require_kind(kind)
    config = domain.parse_config(kind, input.config)
    if not await authz.row_in_scope(input.org, input.namespace, Role.ADMIN):
        raise AuthInsufficientError("a namespace administrator is required for a shared source")
    if not input.credential.strip():
        raise ValidationError("a credential is required", code="CREDENTIAL_REFUSED")
    await _require_source_namespace(input.org, input.namespace)
    await _require_no_erasure(input.org, input.namespace)
    await _require_free(input.org, input.namespace, name)
    _require_sealable(input.credential)
    with _store_refusals():
        found = await source_probes.probe(kind, config, input.credential)
    probe = _probe_answer(found)
    subject, _org = current_caller()
    src = _with_credential(
        KnowledgeSource(
            org=input.org,
            namespace=input.namespace,
            name=name,
            kind=kind,
            source_id=str(uuid.uuid4()),
            config=config,
            created_by=subject,
        ),
        input.credential,
    )
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
    async with sources_lease(input.org, input.namespace):
        with sequential_reads():
            await _require_source_namespace(input.org, input.namespace)
            await _require_no_erasure(input.org, input.namespace)
        if not await authz.row_in_scope(input.org, input.namespace, Role.ADMIN):
            raise AuthInsufficientError("a namespace administrator is required for a shared source")
        await _require_free(input.org, input.namespace, name)
        await store.write_state(input.org, input.namespace, name, state_value="", detail={})
        await store.insert_source(src)
    return out


def _sources_lease_name(org: str, namespace: str) -> str:
    return f"knowledge-source:{org}:{namespace}"


@contextlib.asynccontextmanager
async def sources_lease(
    org: str, namespace: str, *, wait_s: float = 5.0, ch: Any = None
) -> AsyncIterator[None]:
    """Hold the lease that serializes every write of a namespace's sources. Busy
    past `wait_s`: OVERLOADED, retry; a lease store that cannot answer refuses
    (LeaseUnavailable), never writes unlocked."""
    # The TTL outlives the few reads and inserts of one write with room to spare (a
    # holder that dies is taken over after it).
    async with leases.holding(
        _sources_lease_name(org, namespace), ttl_s=60.0, wait_s=wait_s, ch=ch
    ) as held:
        if not held:
            raise OverloadedError(
                f"another change to the document sources of {namespace!r} is running; retry"
            )
        yield


async def _require_free(org: str, namespace: str, name: str, *, owner_subject: str = "") -> None:
    """Refuse a taken name, a full namespace, and a name an earlier source's live
    documents still carry."""
    with sequential_reads():
        if await store.get_source(org, namespace, name) is not None:
            raise ValidationError(
                f"a source named {name!r} exists in {namespace!r}", code="SOURCE_EXISTS"
            )
        limit = (
            settings.knowledge_connected_sources_max_per_namespace
            if owner_subject
            else settings.knowledge_sources_max_per_namespace
        )
        if await store.active_count(org, namespace, connected=bool(owner_subject)) >= limit:
            raise ValidationError(
                f"{namespace!r} has {limit} sources of this credential class; remove one first",
                code="SOURCE_QUOTA",
            )
        if (
            owner_subject
            and await store.active_count(
                org, namespace, connected=True, owner_subject=owner_subject
            )
            >= settings.knowledge_connected_sources_max_per_person
        ):
            raise ValidationError(
                "the person's connected source quota for this namespace is full",
                code="SOURCE_QUOTA",
            )
        documents = await store.count_documents(org, namespace, domain.source_tag(name))
        earlier = await store.latest_source(org, namespace, name)
    # A new row under the name would orphan a removed mirrored source's documents and
    # any delete request for them, which only the build that serves mirrored sources
    # (their ops and their delete requests) acts on.
    if earlier is not None and not domain.served(earlier):
        raise ValidationError(
            f"the name {name!r} belongs to a removed source this server does not serve "
            "yet; register the new source under another name, or retry once the "
            "deployment's upgrade completes",
            code="SOURCE_NOT_SERVED",
        )
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


def _require_supported_recording_provider(src: KnowledgeSource) -> None:
    if src.kind == "recording" and src.config.get("provider") == "meet":
        raise ValidationError("this retired recording provider is unavailable", code="BAD_CONFIG")


async def handle_knowledge_source_check(
    input: KnowledgeSourceCheckInput,
) -> KnowledgeSourceCheckOutput:
    src = await _source_or_404(input.org, input.namespace, input.name)
    if not await authz.row_in_scope(input.org, input.namespace, Role.ADMIN):
        raise AuthInsufficientError("a namespace administrator is required for a shared source")
    _require_served(src)
    _require_supported_recording_provider(src)
    checked_at = _now_iso()
    credential = credential_of(src)
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
    _require_served(src)
    _require_supported_recording_provider(src)
    if not input.credential.strip():
        raise ValidationError("a credential is required", code="CREDENTIAL_REFUSED")
    # A deployment that cannot store the credential says so before the probe.
    _require_sealable(input.credential)
    await _require_source_namespace(input.org, input.namespace)
    with _store_refusals():
        found = await source_probes.probe(src.kind, src.config, input.credential)
    probe = _probe_answer(found)
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
        if _moved(fresh, src):
            # Another write changed what this credential was probed against.
            raise OverloadedError("the source changed during the probe; retry", retry_after_s=1)
        # A provider binding must be set up again for the new credential.
        from server.knowledge.app.source_push import retire

        await retire(fresh)
        # Only the credential changes; every other column is carried forward.
        await store.insert_source(_with_credential(fresh, input.credential))
    return KnowledgeSourceRotateOutput(name=src.name, probe=probe, rotated_at=_now_iso())


def _stored_credential(src: KnowledgeSource) -> str:
    credential = credential_of(src)
    if not credential:
        raise ValidationError(
            "the stored credential no longer opens; rotate it first", code="CREDENTIAL_REFUSED"
        )
    return credential


def _moved(fresh: KnowledgeSource, src: KnowledgeSource) -> bool:
    """Whether another write changed the source between `src`'s read and `fresh`'s:
    its items, config or sealed credential, which a write rewrites and nothing else."""
    return (fresh.items, fresh.config, fresh.credential_enc, fresh.secret_enc) != (
        src.items,
        src.config,
        src.credential_enc,
        src.secret_enc,
    )


async def handle_knowledge_source_remove(
    input: KnowledgeSourceRemoveInput,
) -> KnowledgeSourceRemoveOutput:
    name = domain.normalize_name(input.name)
    if domain.normalize_name(input.confirm) != name:
        raise ValidationError(
            f"type the source name ({domain.shown(name)}) in `confirm` to retire it",
            code="CONFIRM_REQUIRED",
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
                    f"no document source {domain.shown(name)} in {input.namespace!r}",
                    code="SOURCE_NOT_FOUND",
                )
            if src.source_contract_version != 0 or src.kind == "mcp":
                raise NotFoundError("no document source", code="SOURCE_NOT_FOUND")
            if src.credential_mode == "connected":
                await _require_connection_operator(src)
            elif not await authz.row_in_scope(input.org, input.namespace, Role.ADMIN):
                raise AuthInsufficientError(
                    "a namespace administrator is required for a shared source"
                )
            pending = 0
            if input.delete_documents and src.access != domain.MIRRORED_ACCESS:
                pending = await store.count_documents(
                    src.org, src.namespace, domain.source_tag(src.name)
                )
        # The delete request rides on the inactive registry row, which no sync-state
        # write touches. It is recorded even with no documents counted now: a sync
        # turn running meanwhile may still write one, and the worker clears the
        # request once none is left.
        pending_delete = input.delete_documents
        if src.access == domain.MIRRORED_ACCESS:
            pending_delete = await _queue_retirement(src)
        retired = dataclasses.replace(
            src,
            active=False,
            credential_enc="",
            secret_enc="",
            pending_delete=pending_delete,
        )
        from server.knowledge.app.source_push import retire

        await retire(src)
        await store.insert_source(retired)
    return KnowledgeSourceRemoveOutput(name=src.name, documents_pending=pending)


async def handle_knowledge_source_sync_now(
    input: KnowledgeSourceSyncNowInput,
) -> KnowledgeSourceSyncNowOutput:
    src = await _source_or_404(input.org, input.namespace, input.name)
    if src.credential_mode == "connected":
        await _require_connection_operator(src)
    else:
        if not await authz.row_in_scope(input.org, input.namespace, Role.ADMIN):
            raise AuthInsufficientError("a namespace administrator is required for a shared source")
        _require_served(src)
    _require_supported_recording_provider(src)
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
        if fresh.credential_mode == "connected":
            await _require_connection_operator(fresh)
        state = await _read_state(fresh)
        # A full request waiting (an update's) already asks for more: a plain sync now
        # leaves it as it is, neither narrowed nor started over.
        if input.full or not _full_request_waiting(state):
            await _update_state(
                fresh, state, {"requested_at": _now_iso(), "full": bool(input.full)}
            )
    return KnowledgeSourceSyncNowOutput(
        name=src.name, enabled=bool(settings.knowledge_source_sync_enabled)
    )


async def request_full_sync(src: KnowledgeSource) -> None:
    """Record a full sync request for `src`, as `sync_now(full=true)` does, unless
    one is already waiting: `loom knowledge sync --full` run again while a turn
    has not finished the request goes on with it rather than starting over."""
    async with sources_lease(src.org, src.namespace):
        state = await _read_state(src)
        if _full_request_waiting(state):
            return
        await _update_state(src, state, {"requested_at": _now_iso(), "full": True})


def _full_request_waiting(state: dict[str, str]) -> bool:
    """Whether a full sync request waits in `state` that no turn has finished yet."""
    detail = domain.json_object(state.get("detail", ""))
    return bool(detail.get("full") and detail.get("requested_at"))


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
    from server.knowledge.app.source_push import retire

    for src in found:
        pending = await _queue_retirement(src) if src.access == domain.MIRRORED_ACCESS else False
        await retire(src)
        await store.insert_source(
            dataclasses.replace(
                src,
                active=False,
                credential_enc="",
                secret_enc="",
                pending_delete=pending or src.pending_delete,
            )
        )
    return sorted(src.name for src in found)


async def _queue_retirement(src: KnowledgeSource) -> bool:
    """Called under the sources lease, before inactivation; never creates a mirror."""
    from server.knowledge.app.mirrors import live_mirror
    from server.knowledge.store.source_retirements import Retirement, write

    mirror = await live_mirror(src.org, src.namespace)
    if not mirror:
        return False  # inactivation fences the first mirror's creation under this lease
    await write(
        Retirement(src.org, src.namespace, src.name, src.source_id, src.kind, mirror), pending=True
    )
    return True


async def retire_sources_of(org: str, namespaces: list[str], *, wait_s: float) -> int:
    """Retire the sources of `namespaces` (an erasure's) and wait for every sync turn of
    theirs still running; every lease wait (an add's, the retirement's, each sync turn's)
    shares one `wait_s` deadline. A source add while the saga runs is refused
    (`ERASURE_RUNNING`) under the namespace's lease, so once no add holds that lease, one
    sequential read finds every source an add wrote."""
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
    return {
        "org": input.org,
        "namespace": input.namespace,
        "name": response["name"],
        "kind": "recording",
        "dry_run": input.dry_run,
    }


def source_rotate_audit_subject(input: KnowledgeSourceRotateInput, response: Any) -> dict[str, Any]:
    """Which source's credential was replaced, never the credential."""
    return {"org": input.org, "namespace": input.namespace, "name": response["name"]}


__all__ = [
    "credential_of",
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


async def _require_connection_operator(source: KnowledgeSource) -> None:
    from server.knowledge.app import connected_accounts as accounts

    auth = current_auth()
    if source.credential_mode != "connected" or not request_work.mark_mirrored():
        raise accounts.unavailable()
    if await authz.row_in_scope(source.org, source.namespace, Role.ADMIN):
        if current_auth() is not auth:
            raise accounts.unavailable()
        return
    org, owner = await accounts.own_subject(auth)
    if (
        org != source.org
        or owner != source.owner_subject
        or not await authz.row_in_scope(org, source.namespace, Role.WRITER)
        or current_auth() is not auth
    ):
        raise accounts.unavailable()
