"""knowledge.source.* — the document sources a namespace's admins register for
Loom to pull from.

Every op is addressed by the namespace the source's documents live in, so the
transport's namespace gate decides who may call:
readers list and inspect and parse links, the namespace's admins add, update,
check, rotate, remove and ask for a sync. The credential is sealed at rest under
its own purpose (one for a source a build without items can read, another for the
rest, see `store`), held in memory only for a probe, and never returned or
audited.

Nothing here fetches documents: the sync worker does, outside the request path
(`server.knowledge.app.source_sync`). `sync_now` records what the worker should do
next in the source's sync state; `remove(delete_documents=true)` records it on the
inactive registry row it writes.

Every write of a namespace's sources, registry rows and sync state alike (add,
update, rotate, remove, sync_now, and the retirement when the namespace goes:
code.repo.remove, namespace.delete, collection.delete and the erasure step
`knowledge_sources.retire`), runs under `sources_lease`, which serializes them; the
probes run outside it.

Which documents are a source's: see `SYNC_WORKER_ACTOR`.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
import uuid
from collections.abc import AsyncIterator, Container, Iterator
from datetime import UTC, datetime
from typing import Any, NamedTuple

from server.auth import machines
from server.auth.context import current_caller
from server.auth.personal_namespace import is_own_namespace
from server.auth.sharing import is_collection
from server.core import ch, leases
from server.core.ch import sequential_reads
from server.core.datetimes import as_aware_utc
from server.core.errors import (
    BadConfigError,
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
    KnowledgeSourceParsedLink,
    KnowledgeSourceParseLinksInput,
    KnowledgeSourceParseLinksOutput,
    KnowledgeSourceProbe,
    KnowledgeSourceRemoveInput,
    KnowledgeSourceRemoveOutput,
    KnowledgeSourceRotateInput,
    KnowledgeSourceRotateOutput,
    KnowledgeSourceSync,
    KnowledgeSourceSyncNowInput,
    KnowledgeSourceSyncNowOutput,
    KnowledgeSourceUpdateInput,
    KnowledgeSourceUpdateOutput,
)
from server.knowledge.app import source_probes
from server.knowledge.domain import sources as domain
from server.knowledge.domain.source_links import parse_links
from server.knowledge.domain.sources import KnowledgeSource
from server.knowledge.store import sources as store
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
    if src.secret_enc:
        return unseal(_SECRET_PURPOSE, src.secret_enc)
    return unseal(_CREDENTIAL_PURPOSE, src.credential_enc)


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
    """`src` with `credential` sealed where its storage rule puts it."""
    if domain.id_list_config(src) is not None:
        return dataclasses.replace(
            src, credential_enc=_sealed(_CREDENTIAL_PURPOSE, credential), secret_enc=""
        )
    return dataclasses.replace(
        src, credential_enc="", secret_enc=_sealed(_SECRET_PURPOSE, credential)
    )


def _stored_options(kind: str, items: list[dict[str, Any]], include: list[str]) -> dict[str, Any]:
    """The config a source of `items` stores: a `gdrive` source's `include`, checked
    against its filters; nothing for the other kinds."""
    if kind != "gdrive":
        return {}
    domain.check_filters(items, include)
    return {"include": include}


async def _require_fleet_for(kind: str, items: list[dict[str, Any]]) -> None:
    """Refuse a source that would be stored with `kind` empty while a live process of
    any deployment may run a build without items: it would list the source with no kind
    (docs/architecture/rolling-upgrades.md). An unread fleet refuses too."""
    if kind == "recording" or domain.fits_id_list(kind, items):
        return
    if not await machines.fleet_at(ch.get_client(), _ITEMS_MIGRATION):
        raise OverloadedError(
            "not every Loom process reads a source of these items yet; retry once the "
            "rollout is done",
            retry_after_s=30,
        )


def _items_of(
    kind: str,
    items: list[dict[str, Any]],
    links: list[str],
    prefix: str = "",
    held: KnowledgeSource | None = None,
) -> list[dict[str, Any]]:
    """`items` and the items `links` name, checked, a refusal naming the fields they
    came in (`<prefix>items`, `<prefix>links`); BAD_LINK names the first links that
    name none or one of another kind, and counts the rest. A link to something `held`
    holds takes the held form (a recursive folder, a drive at its root); an item given
    for the same thing wins over the link."""
    given = domain.parse_items(kind, items, f"{prefix}items") if items else []
    linked: list[dict[str, Any]] = []
    unread: list[str] = []
    for parsed in parse_links(links):
        if parsed.unreadable:
            unread.append(f"{domain.shown(parsed.link)}: {parsed.unreadable[:100]}")
        elif parsed.kind != kind:
            unread.append(f"{domain.shown(parsed.link)}: a {parsed.kind} link, not {kind}")
        else:
            linked.append(parsed.item)
    if unread:
        more = f"; and {len(unread) - _LINKS_NAMED} more" if len(unread) > _LINKS_NAMED else ""
        message = f"unreadable {prefix}links: " + "; ".join(unread[:_LINKS_NAMED]) + more
        raise ValidationError(message, code="BAD_LINK")
    if held is not None:
        linked = _as_held(held, linked)
    keys = {domain.item_key(i) for i in given}
    return domain.unique_items(given + [i for i in linked if domain.item_key(i) not in keys])


def _without_drive_folders(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`items` less each folder a shared drive of its id is given beside: the drive reads
    all the folder does."""
    drives = {i["id"] for i in items if i["type"] == "shared_drive"}
    return [i for i in items if not (i["type"] == "folder" and i.get("id") in drives)]


def _named(items: list[dict[str, Any]]) -> int:
    """How many things `items` (each once) name at least: a short id and an id may name
    the same project."""
    slugs = sum(1 for i in items if "slug_id" in i)
    return max(len(items) - slugs, slugs)


def _require_count(kind: str, items: list[dict[str, Any]], fields: list[str]) -> None:
    """BAD_CONFIG naming `fields` when `items` name more than a source of `kind` holds; a
    lower bound before short ids are resolved (`_named`)."""
    if _named(items) > domain.max_items(kind):
        sent = " and ".join(fields)
        raise BadConfigError(f"{sent}: at most {domain.max_items(kind)} items for a {kind} source")


def _include(include: list[str] | None, kind: str) -> list[str] | None:
    if include is None:
        return None
    if kind != "gdrive":
        raise BadConfigError("include: a gdrive source's option")
    return domain.parse_include(include)


def _registration(
    input: KnowledgeSourceAddInput, kind: str, sent: list[str]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The config and items an add registers, from its config or its items and links
    (the fields `sent`)."""
    if input.config is None and not sent:
        raise BadConfigError("give config, or items or links")
    if input.config is not None and sent:
        raise BadConfigError("config takes no items or links")
    include = _include(input.include, kind)
    if input.config is not None:
        if include is not None:
            raise BadConfigError("with config, give include inside it")
        config = domain.parse_config(kind, input.config)
        return domain.options_in(kind, config), domain.items_from_config(kind, config)
    if kind == "recording":
        raise BadConfigError("a recording source takes config, not items")
    items = _without_drive_folders(_items_of(kind, input.items or [], input.links or []))
    if not items:
        raise BadConfigError(f"{' and '.join(sent)}: 1 to {domain.max_items(kind)} items")
    _require_count(kind, items, sent)
    return _stored_options(kind, items, include or list(domain.GDRIVE_DEFAULT_INCLUDE)), items


async def _probe_items(
    kind: str,
    items: list[dict[str, Any]],
    config: dict[str, Any],
    credential: str,
    suffix: str = "",
) -> tuple[KnowledgeSourceProbe, list[dict[str, Any]]]:
    """Probe the sample of `items` (`source_probes.sample`); the probe and `items` as
    the store names them, each once (a recursive folder the probe found is a shared
    drive's root becomes that drive). A refusal about one item ends with `suffix`."""
    checked = source_probes.sample(items)
    with _store_refusals(suffix):
        found = await source_probes.probe(kind, config, credential, items=checked)
    if found.trashed:
        message = f"the source could not be listed: file {found.trashed[0]}: in the trash"
        raise ValidationError(f"{message}; restore it first{suffix}", code="SOURCE_UNREACHABLE")
    if found.foreign:
        fid, mime = next(iter(found.foreign.items()))
        message = f"file {fid}: {domain.unread_file_note(mime)}"
        raise BadConfigError(message + suffix)
    if found.items:
        items = domain.unique_items(items[: len(items) - len(checked)] + found.items)
    return _probe_answer(found, len(checked), len(items)), items


def _probe_answer(found: source_probes.Probe, checked: int, of: int) -> KnowledgeSourceProbe:
    """What a probe of `checked` of a source's `of` items found, as an op answers it."""
    return KnowledgeSourceProbe(
        documents=found.documents,
        more=found.more,
        sample=found.sample,
        checked=checked,
        sampled=checked < of,
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


async def _require_no_erasure(org: str, namespace: str) -> None:
    """Refuse a new source while an erasure of the namespace will still run: its first step
    may have read the sources already, so what a later source wrote would outlast it."""
    # Imported here: the erasure saga imports this module.
    from server.erasure.app import ledger_reads
    from server.erasure.domain.scopes import may_cover

    for row in await ledger_reads.open_namespace_erasures(org):
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
    if src is None:
        raise NotFoundError(
            f"no document source {domain.shown(name)} in {namespace!r}", code="SOURCE_NOT_FOUND"
        )
    return src


def _require_served(src: KnowledgeSource) -> None:
    """This build syncs only sources shown to every reader of the namespace: a mirrored
    one, which a later build writes, is that build's to read."""
    if src.access != "namespace":
        raise ValidationError(
            f"source {src.name!r} is mirrored, which this server does not serve; "
            "use a later version",
            code="BAD_KIND",
        )


@contextlib.contextmanager
def _store_refusals(suffix: str = "", about: Container[str] | None = None) -> Iterator[None]:
    """A probe's or a lookup's refusal, as the op answers it; one about an item (of
    `about`, when given) ends with `suffix`, which carries its own separator."""
    try:
        yield
    except source_probes.CredentialRefused as e:
        raise ValidationError(
            f"the source refused the credential: {e}", code="CREDENTIAL_REFUSED"
        ) from None
    except source_probes.SourceUnreachable as e:
        named = e.item_id and (about is None or e.item_id in about)
        message = f"the source could not be listed: {e}"
        raise ValidationError(
            message + (suffix if named else ""), code="SOURCE_UNREACHABLE"
        ) from None


def _answered_include(src: KnowledgeSource) -> list[str]:
    """`include` as the ops answer it: [] for a kind without file kinds."""
    return domain.include_in(src.config) if src.kind == "gdrive" else []


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
                items=s.items,
                include=_answered_include(s),
                config=domain.id_list_config(s) or {},
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
        items=src.items,
        include=_answered_include(src),
        config=domain.id_list_config(src) or {},
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
    sent = [f for f in ("items", "links") if getattr(input, f) is not None]
    config, items = _registration(input, kind, sent)
    if not input.credential.strip():
        raise ValidationError("a credential is required", code="CREDENTIAL_REFUSED")
    await _require_source_namespace(input.org, input.namespace)
    await _require_no_erasure(input.org, input.namespace)
    await _require_free(input.org, input.namespace, name)
    # A deployment that cannot store the credential says so before the probe.
    _require_sealable(input.credential)
    # A project may be named by its short id (a link's, or an item's `slug_id`); the
    # store's id is the UUID.
    items = domain.unique_items(await _resolved(kind, items, input.credential))
    _require_count(kind, items, sent)
    if not input.dry_run:
        # The probe only turns a recursive folder into a drive, neither of which the
        # id-list form holds; a dry run stores nothing.
        await _require_fleet_for(kind, items)
    probe, items = await _probe_items(kind, items, config, input.credential)
    subject, _org = current_caller()
    src = KnowledgeSource(
        org=input.org,
        namespace=input.namespace,
        name=name,
        kind=kind,
        source_id=str(uuid.uuid4()),
        config=config,
        items=items,
        created_by=subject,
    )
    src = _with_credential(src, input.credential)
    out = KnowledgeSourceAddOutput(
        name=name,
        kind=kind,
        namespace=input.namespace,
        items=src.items,
        include=_answered_include(src),
        config=domain.id_list_config(src) or {},
        probe=probe,
        dry_run=input.dry_run,
    )
    if input.dry_run:
        return out
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
        await store.insert_source(src)
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
    _require_served(src)
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
    checked = source_probes.sample(src.items)
    try:
        found = await source_probes.probe(src.kind, src.config, credential, items=checked)
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
        checked=len(checked),
        sampled=len(checked) < len(src.items),
        summary=f"{found.documents}{plus} documents listed",
        checked_at=checked_at,
    )


async def handle_knowledge_source_rotate(
    input: KnowledgeSourceRotateInput,
) -> KnowledgeSourceRotateOutput:
    src = await _source_or_404(input.org, input.namespace, input.name)
    _require_served(src)
    if not input.credential.strip():
        raise ValidationError("a credential is required", code="CREDENTIAL_REFUSED")
    # A deployment that cannot store the credential says so before the probe.
    _require_sealable(input.credential)
    await _require_source_namespace(input.org, input.namespace)
    checked = source_probes.sample(src.items)
    with _store_refusals():
        found = await source_probes.probe(src.kind, src.config, input.credential, items=checked)
    probe = _probe_answer(found, len(checked), len(src.items))
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
        # Only the credential changes; every other column is carried forward.
        await store.insert_source(_with_credential(fresh, input.credential))
    return KnowledgeSourceRotateOutput(name=src.name, probe=probe, rotated_at=_now_iso())


async def handle_knowledge_source_update(
    input: KnowledgeSourceUpdateInput,
) -> KnowledgeSourceUpdateOutput:
    src = await _source_or_404(input.org, input.namespace, input.name)
    _require_served(src)
    if src.kind not in domain.ITEM_FIELDS:
        raise ValidationError(f"a {src.kind} source has no items", code="BAD_KIND")
    plan = await _update_plan(src, input)
    if plan is None:
        return KnowledgeSourceUpdateOutput(
            name=src.name,
            items=src.items,
            include=_answered_include(src),
            config=domain.id_list_config(src) or {},
        )
    async with sources_lease(input.org, input.namespace):
        # The probe ran outside the lease: what it checked holds only for the source as
        # it was read, so a source another change moved meanwhile is retried.
        with sequential_reads():
            fresh = await _source_or_404(input.org, input.namespace, src.name)
            # A namespace that went, or an erasure that started, during the probe.
            await _require_source_namespace(input.org, input.namespace)
            await _require_no_erasure(input.org, input.namespace)
        if fresh.source_id != src.source_id:
            raise SourceReplacedError(
                f"source {src.name!r} was removed and added again while its new items were "
                "probed; update the current source"
            )
        if _moved(fresh, src):
            raise OverloadedError("the source changed during the probe; retry", retry_after_s=1)
        change = _updated(fresh, plan.added, plan.removed, plan.include)
        updated = change.source
        # The probe can name an added folder as a drive the source already holds.
        if updated.items != fresh.items or plan.include is not None:
            # Sealed again: the change may move the credential to the other column.
            await store.insert_source(_with_credential(updated, _stored_credential(fresh)))
            # A fresh full request, so the next turn lists the added items' older files;
            # to the microsecond, so it never equals one a running turn read and clears.
            asked = datetime.now(UTC).isoformat()
            state = await _read_state(updated)
            await _update_state(updated, state, {"requested_at": asked, "full": True})
    return KnowledgeSourceUpdateOutput(
        name=updated.name,
        items=updated.items,
        include=_answered_include(updated),
        config=domain.id_list_config(updated) or {},
        added=change.added,
        removed=change.removed,
        probe=plan.probe,
    )


class _UpdatePlan(NamedTuple):
    """An update's change, checked and probed before the lease."""

    added: list[dict[str, Any]]
    removed: list[dict[str, Any]]
    include: list[str] | None
    probe: KnowledgeSourceProbe


async def _update_plan(
    src: KnowledgeSource, input: KnowledgeSourceUpdateInput
) -> _UpdatePlan | None:
    """The change `input` asks of `src`, refused before any probe when it cannot be
    made, its added items (and, for a new `include`, the kept file items) probed; None
    when it changes nothing (an `include` restated, items added as they are held)."""
    # A link sent again names what the source holds, so it changes nothing.
    added = _items_of(src.kind, input.add_items, input.add_links, "add_", held=src)
    # A folder entry and a drive entry of one id both name the held drive: once.
    removed = domain.unique_items(
        _as_held(src, _items_of(src.kind, input.remove_items, input.remove_links, "remove_"))
    )
    added_fields = [f for f in ("add_items", "add_links") if getattr(input, f)]
    _require_count(src.kind, added, added_fields)
    # Each removed entry must match a held item, so this bounds the short-id lookup.
    if _named(removed) > len(src.items):
        fields = " and ".join(f for f in ("remove_items", "remove_links") if getattr(input, f))
        raise BadConfigError(f"{fields}: names more items than the source holds")
    include = _include(input.include, src.kind)
    if include == domain.include_in(src.config):
        include = None  # restated as it is: no change to check or store
    if not added and not removed and input.include is None:
        raise BadConfigError("name an item, a link or include to change")
    await _require_source_namespace(src.org, src.namespace)
    await _require_no_erasure(src.org, src.namespace)
    if not added and not removed and include is None:
        return None
    credential = _stored_credential(src)
    removing = {i["slug_id"] for i in removed if "slug_id" in i}
    # Short ids to UUIDs, the added and the removed in one lookup; a held project is
    # removed by its id, as knowledge.source.list shows.
    resolved = await _resolved(
        src.kind, added + removed, credential, "; remove a source's own project by its id", removing
    )
    n = len(added)
    added, removed = domain.unique_items(resolved[:n]), domain.unique_items(resolved[n:])
    _require_count(src.kind, added, added_fields)
    # Refuses a change it cannot make, before the probe: a rollout one too.
    change = _updated(src, added, removed, include)
    pending, new = change.source, change.added
    if pending.items == src.items and include is None:
        return None  # every added item is held as it is, as a retried call sends them
    await _require_fleet_for(pending.kind, pending.items)
    probe = KnowledgeSourceProbe()
    options = {"include": include} if include is not None else src.config
    probed = new
    if new:
        # The items as the probe names them (a recursive folder at a drive's root: the drive).
        probe, probed = await _probe_items(src.kind, new, options, credential)
    # A new `include` that names no kind of a file the source keeps would leave it
    # listing nothing: the kept files, not the added ones or the removed.
    kept = _kept_files(pending, probed) if include is not None else []
    if kept:
        kept_probe, _ = await _probe_items(
            src.kind, kept, options, credential, ", or remove it with remove_items"
        )
        if not probed:
            probe = kept_probe
    return _UpdatePlan(probed, removed, include, probe)


def _stored_credential(src: KnowledgeSource) -> str:
    credential = credential_of(src)
    if not credential:
        raise ValidationError(
            "the stored credential no longer opens; rotate it first", code="CREDENTIAL_REFUSED"
        )
    return credential


async def _resolved(
    kind: str,
    items: list[dict[str, Any]],
    credential: str,
    suffix: str = "",
    about: Container[str] | None = None,
) -> list[dict[str, Any]]:
    """`items` with each tracker project's short id resolved, in the same order and
    number; the caller drops what then names the same project twice (`domain.unique_items`).
    A short id the tracker does not know (one of `about`) ends its refusal with `suffix`."""
    with _store_refusals(suffix, about):
        return await source_probes.resolve(kind, items, credential)


def _moved(fresh: KnowledgeSource, src: KnowledgeSource) -> bool:
    """Whether another write changed the source between `src`'s read and `fresh`'s:
    its items, config or sealed credential, which a write rewrites and nothing else."""
    return (fresh.items, fresh.config, fresh.credential_enc, fresh.secret_enc) != (
        src.items,
        src.config,
        src.credential_enc,
        src.secret_enc,
    )


def _kept_files(src: KnowledgeSource, added: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The file items `src` holds that `added` did not bring."""
    return [i for i in src.items if i["type"] == "file" and i not in added]


def _as_held(src: KnowledgeSource, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`items` as `src` holds them, so an answer and an audit row name the stored item:
    a folder link parses as `recursive: false`, so it takes the held folder's form, or
    the held drive's at a shared drive's root."""
    held = {domain.item_key(i): i for i in src.items}

    def held_form(item: dict[str, Any]) -> dict[str, Any]:
        key = domain.item_key(item)
        if key in held:
            return held[key]
        if item["type"] == "folder":
            return held.get(domain.item_key({"type": "shared_drive", "id": item["id"]}), item)
        return item

    return [held_form(i) for i in items]


class _Change(NamedTuple):
    """A source after an update, the items it added, and every held item it dropped."""

    source: KnowledgeSource
    added: list[dict[str, Any]]
    removed: list[dict[str, Any]]


def _updated(
    src: KnowledgeSource,
    added: list[dict[str, Any]],
    removed: list[dict[str, Any]],
    include: list[str] | None,
) -> _Change:
    """`src` changed: `removed` out and `added` in, answering every held item the change
    drops as removed. BAD_CONFIG for an item it does not hold, one both added and
    removed, no item left and too many."""
    held = {domain.item_key(i) for i in src.items}
    both = {domain.item_key(i) for i in added} & {domain.item_key(i) for i in removed}
    if both:
        raise BadConfigError(f"an item both added and removed: {sorted(both)[0]}")
    absent = [i for i in removed if domain.item_key(i) not in held]
    if absent:
        raise BadConfigError(f"not an item of the source: {domain.item_key(absent[0])}")
    # An added item held as it is changes nothing; one in another form (a folder made
    # recursive) replaces the held one, and a drive drops a folder of its id beside it.
    incoming = [i for i in added if i not in src.items]
    gone = {domain.item_key(i) for i in removed + incoming}
    kept = [i for i in src.items if domain.item_key(i) not in gone]
    items = _without_drive_folders(kept + incoming)
    added_now = [i for i in incoming if i in items]
    dropped = [i for i in src.items if i not in items]
    if not items:
        raise BadConfigError("that removes every item; knowledge.source.remove ends a source")
    if len(items) > domain.max_items(src.kind):
        raise BadConfigError(f"at most {domain.max_items(src.kind)} items for a {src.kind} source")
    config = _stored_options(src.kind, items, include or domain.include_in(src.config))
    return _Change(dataclasses.replace(src, items=items, config=config), added_now, dropped)


async def handle_knowledge_source_parse_links(
    input: KnowledgeSourceParseLinksInput,
) -> KnowledgeSourceParseLinksOutput:
    return KnowledgeSourceParseLinksOutput(
        links=[
            KnowledgeSourceParsedLink(
                link=p.link, kind=p.kind, item=p.item, unreadable=p.unreadable
            )
            for p in parse_links(input.links)
        ]
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
            pending = 0
            if input.delete_documents:
                pending = await store.count_documents(src.org, src.namespace, src.name)
        # The delete request rides on the inactive registry row, which no sync-state
        # write touches. It is recorded even with no documents counted now: a sync
        # turn running meanwhile may still write one, and the worker clears the
        # request once none is left.
        retired = dataclasses.replace(
            src,
            active=False,
            credential_enc="",
            secret_enc="",
            pending_delete=input.delete_documents,
        )
        await store.insert_source(retired)
    return KnowledgeSourceRemoveOutput(name=src.name, documents_pending=pending)


async def handle_knowledge_source_sync_now(
    input: KnowledgeSourceSyncNowInput,
) -> KnowledgeSourceSyncNowOutput:
    src = await _source_or_404(input.org, input.namespace, input.name)
    _require_served(src)
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
    for src in found:
        await store.insert_source(
            dataclasses.replace(src, active=False, credential_enc="", secret_enc="")
        )
    return sorted(src.name for src in found)


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
    """What an add's audit row records: the source as stored, never the
    credential. `loom.audit_log` outlives the source and must hold no secret."""
    return {
        "org": input.org,
        "namespace": input.namespace,
        "name": response["name"],
        "kind": response["kind"],
        "items": response["items"],
        "include": response["include"],
        "config": response["config"],
        "dry_run": input.dry_run,
    }


def source_update_audit_subject(input: KnowledgeSourceUpdateInput, response: Any) -> dict[str, Any]:
    """Which source changed and the items added and removed, never the credential."""
    return {
        "org": input.org,
        "namespace": input.namespace,
        "name": response["name"],
        "added": response["added"],
        "removed": response["removed"],
        "include": response["include"] if input.include is not None else None,
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
    "handle_knowledge_source_parse_links",
    "handle_knowledge_source_remove",
    "handle_knowledge_source_rotate",
    "handle_knowledge_source_sync_now",
    "handle_knowledge_source_update",
    "source_add_audit_subject",
    "source_rotate_audit_subject",
    "source_update_audit_subject",
]
