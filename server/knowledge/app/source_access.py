"""Explicit checked mirror legs; ordinary stores do not widen their reads."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from server.auth.aliases import canonical_subject
from server.auth.answer_tokens import is_answer_token
from server.auth.authz import check_authorized
from server.auth.context import current_auth
from server.auth.jwt import AuthInfo
from server.auth.reader_identity import ReaderIdentity, reader_subject, resolve_reader
from server.core import pg, request_work
from server.core.ch import sequential_reads, set_query_tag
from server.core.diagnostics import private_work
from server.core.naming import is_mirror_name
from server.knowledge.domain.access_list import UNREAD, AccessList, allows, from_json
from server.knowledge.domain.document_access import fresh, source_id
from server.knowledge.domain.serving import Serving
from server.knowledge.domain.sources import KnowledgeSource, opaque_payload
from server.knowledge.store import mirrors as mirrors_repo
from server.knowledge.store import source_access as store
from server.operations.base import Role
from server.settings import settings


def now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Admitted:
    namespace: str
    candidate: dict[str, Any]
    document: dict[str, Any]
    access: AccessList = UNREAD
    canonical_owner: str = ""
    groups: tuple[tuple[str, datetime], ...] = ()
    reader_emails: frozenset[str] = frozenset()
    serving: Serving | None = None


@dataclass(frozen=True)
class AccessEvidence:
    namespace: str
    source_id: str
    access: AccessList
    canonical_owner: str
    groups: tuple[tuple[str, datetime], ...]
    reader_emails: frozenset[str]

    @classmethod
    def from_admitted(cls, item: Admitted) -> AccessEvidence:
        doc = item.document
        return cls(
            doc["namespace"],
            source_id(doc["source_uri"], doc["tags"]),
            item.access,
            item.canonical_owner,
            item.groups,
            item.reader_emails,
        )


@dataclass
class ReadPlan:
    org: str
    sources: dict[tuple[str, str], str] = field(default_factory=dict)
    listing_starts: dict[tuple[str, str], datetime] = field(default_factory=dict)
    reader: ReaderIdentity | None = None
    marked: bool = False
    unread: bool = False
    failed: bool = False
    _bindings: tuple[tuple[str, str, str], ...] = field(default=(), repr=False)
    _reader_subject: str = field(default="", repr=False)
    discovery_reader: ReaderIdentity | None = field(default=None, repr=False)
    _checking: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _auth: AuthInfo | None = field(default=None, repr=False)
    _scope_frozen: bool = field(default=False, repr=False)
    _connected_pins: dict[tuple[str, str], KnowledgeSource] = field(
        default_factory=dict, repr=False
    )

    @property
    def discovery_sources(self) -> dict[tuple[str, str], str]:
        """The request's fixed discovery scope, unaffected by conditional checks."""
        return {(mirror, source): original for mirror, source, original in self._bindings}

    def listing_current(self, namespace: str, source: str, *, at: datetime | None = None) -> bool:
        return fresh(
            self.listing_starts.get((namespace, source)),
            at or now(),
            settings.knowledge_source_acl_stale_seconds,
        )

    def current(self, item: Admitted) -> bool:
        """Recheck retained time bounds without discarding independent permission arms."""
        return self.current_evidence(AccessEvidence.from_admitted(item))

    def current_evidence(self, item: AccessEvidence) -> bool:
        """Evaluate retained permission evidence against the latest shared reader view."""
        at = now()
        if not self.listing_current(item.namespace, item.source_id, at=at):
            return False
        reader = self.reader
        return allows(
            item.access,
            subject=reader.subject if reader else "",
            canonical_owner=item.canonical_owner,
            emails=reader.emails if reader else frozenset(),
            domains=reader.domains if reader else frozenset(),
            groups=frozenset(
                name
                for name, read_at in item.groups
                if reader
                and item.reader_emails == reader.emails
                and fresh(read_at, at, settings.knowledge_source_acl_stale_seconds)
            ),
        )

    async def check(
        self, candidates: list[dict[str, Any]], *, chunks: bool = False
    ) -> list[Admitted]:
        """Check a bounded batch; any candidate read failure invalidates the whole plan."""
        async with self._checking:
            # Serialize shared evidence so an older refresh cannot overwrite a newer check.
            if self.failed or self.unread or not self.sources or not candidates:
                return []
            set_query_tag("knowledge.mirror.check")
            try:
                with sequential_reads(), private_work():
                    return await self._check(candidates, chunks=chunks)
            except Exception:  # noqa: BLE001 — a failed candidate read must disclose no partial leg
                self.failed = True
                return []

    async def _refresh_scope(self) -> None:
        """Refresh conditional evidence without adding a discovery leg to this request."""
        initial = self.discovery_sources
        sources = await store.active_sources(self.org, sorted(set(initial.values())))
        snapshot = ReadPlan(self.org)
        snapshot._auth = self._auth
        snapshot._scope_frozen = True
        snapshot._connected_pins = self._connected_pins
        await _prepare(snapshot, sources, self._reader_subject)
        self.sources = {
            key: original
            for key, original in snapshot.sources.items()
            if initial.get(key) == original
        }
        self.listing_starts = {
            key: started for key, started in snapshot.listing_starts.items() if key in self.sources
        }
        self.reader = snapshot.reader

    async def _check(self, candidates: list[dict[str, Any]], *, chunks: bool) -> list[Admitted]:
        await self._refresh_scope()
        if not self.sources:
            return []
        reader = self.reader
        pairs = sorted({(r["namespace"], r["source_uri"]) for r in candidates})
        heads = await store.heads(self.org, pairs) if pg.enabled() else {}
        rows = (
            [h.row for h in heads.values()]
            if pg.enabled()
            else await store.metadata(self.org, pairs)
        )
        docs = {
            (r["namespace"], r["source_uri"]): r
            for r in rows
            if r.get("org") == self.org and (r["namespace"], r["source_uri"]) in pairs
        }
        permissions = {p: from_json(r.get("access_json") or "") for p, r in docs.items()}
        groups = await self._groups(docs, permissions, reader)
        out = []
        owners: dict[str, str] = {}
        for candidate in candidates:
            pair = (candidate["namespace"], candidate["source_uri"])
            doc = docs.get(pair)
            if doc is None or doc.get("superseded_at") is not None:
                continue
            sid = source_id(doc["source_uri"], doc["tags"])
            key = (doc["namespace"], sid)
            original = self.sources.get(key)
            if not self.listing_current(*key):
                continue
            if not original or (pg.enabled() and not _head_serves(heads[pair], candidate, chunks)):
                continue
            if chunks and not pg.enabled() and int(candidate["revision"]) != doc["revision"]:
                continue
            access = permissions[pair]
            owner = ""
            if access.owner and reader:
                if access.owner not in owners:
                    owners[access.owner] = await canonical_subject(self.org, access.owner)
                owner = owners[access.owner]
            item = Admitted(
                original,
                candidate,
                doc,
                access,
                owner,
                tuple(groups.get(key, {}).items()),
                reader.emails if reader else frozenset(),
                heads[pair].serving if pg.enabled() else None,
            )
            if self.current(item):
                out.append(item)
        return out

    async def _groups(
        self,
        docs: dict[tuple[str, str], dict[str, Any]],
        permissions: dict[tuple[str, str], AccessList],
        reader: ReaderIdentity | None,
    ) -> dict[tuple[str, str], dict[str, datetime]]:
        if reader is None or not reader.emails:
            return {}
        wanted = [
            (ns, source_id(uri, docs[(ns, uri)]["tags"]), group)
            for (ns, uri), access in permissions.items()
            for group in access.groups
            if (ns, source_id(uri, docs[(ns, uri)]["tags"])) in self.sources
        ]
        rows = await store.group_members(self.org, wanted) if wanted else []
        found: dict[tuple[str, str], dict[str, datetime]] = {}
        requested = set(wanted)
        for row in rows:
            key = (row["namespace"], row["source_id"], row["group"])
            if (
                key not in requested
                or row["deleted"]
                or not fresh(row["read_at"], now(), settings.knowledge_source_acl_stale_seconds)
            ):
                continue
            members = {email.strip().lower() for email in row["members"]}
            if reader.emails.intersection(members):
                found.setdefault(key[:2], {})[row["group"]] = row["read_at"]
        return found


def _head_serves(head: store.ReadHead, candidate: dict[str, Any], chunks: bool) -> bool:
    state = head.serving
    if state.deleted or head.row.get("revision") != state.revision:
        return False
    if not chunks:
        return True
    return (
        state.chunks_revision is not None
        and state.chunks_version is not None
        and state.chunks_revision == state.revision
        and int(candidate["revision"]) == state.revision
        and int(candidate["doc_version"]) == state.chunks_version
    )


async def has_active_mirrored_sources(org: str, namespace: str) -> bool:
    """Read public registration presence after the caller authorizes the namespace."""
    return bool(await store.active_sources(org, [namespace]))


async def prepare(org: str, namespaces: list[str]) -> ReadPlan:
    """Read public registrations first, then bind one marked, fail-closed mirror plan."""
    plan = ReadPlan(org)
    auth = current_auth()
    plan._auth = auth
    if (
        auth is None
        or not settings.auth_actually_required()
        or any(is_mirror_name(ns) for ns in namespaces)
        or (is_answer_token(auth) and not (auth.session_protection and auth.answer_mirrors))
    ):
        return plan
    set_query_tag("knowledge.mirror.prepare")
    try:
        with sequential_reads():
            for namespace in sorted(set(namespaces)):
                await check_authorized(auth, Role.VIEWER, org, namespace)
            sources = await store.active_sources(org, namespaces)
            if not sources:
                return plan
            if not request_work.mark_mirrored():
                plan.failed = True  # an unbound direct helper cannot serve a mirror
                return plan
            plan.marked = True
            with private_work():
                plan._reader_subject = reader_subject(auth)
                await _prepare(plan, sources, plan._reader_subject)
                plan.discovery_reader = plan.reader
                plan._bindings = tuple(
                    sorted(
                        (mirror, source, original)
                        for (mirror, source), original in plan.sources.items()
                    )
                )
    except Exception:  # noqa: BLE001 — unreadable unconditional evidence must fail closed uniformly
        plan.marked = plan.unread = True
        request_work.mark_mirrored(unread=True)
    return plan


async def _prepare(plan: ReadPlan, sources: list[dict[str, Any]], subject: str) -> None:
    from server.knowledge.app.connected_accounts import validate_indexed
    from server.knowledge.store import sources as source_store

    eligible = []
    for row in sources:
        key = row["namespace"], row["source_id"]
        mode = row.get("credential_mode", "shared")
        if opaque_payload(
            kind=str(row.get("source_kind") or ""),
            mode=mode,
            version=int(row.get("source_contract_version") or 0),
        ):
            continue
        if mode == "shared":
            if key not in plan._connected_pins:
                eligible.append(row)
            continue
        if mode != "connected" or (plan._scope_frozen and key not in plan._connected_pins):
            continue
        try:
            source = await source_store.latest_source(plan.org, row["namespace"], row["name"])
            if (
                source is None
                or source.source_id != row["source_id"]
                or opaque_payload(
                    kind=source.kind,
                    mode=source.credential_mode,
                    version=source.source_contract_version,
                )
            ):
                continue
            pinned = plan._connected_pins.get(key)
            if pinned is not None and (
                source.connection_id != pinned.connection_id
                or source.owner_subject != pinned.owner_subject
                or source.kind != pinned.kind
                or source.items != pinned.items
                or source.config != pinned.config
            ):
                continue
            await validate_indexed(source, plan._auth)
            eligible.append(row)
            if not plan._scope_frozen:
                plan._connected_pins[key] = copy.deepcopy(source)
        except Exception:  # noqa: BLE001 — one unverifiable connection admits no private leg
            continue
    sources = eligible
    names = sorted({s["namespace"] for s in sources})
    mirrors = await mirrors_repo.mirrors_of(plan.org, names, live_only=True)
    by_original: dict[str, list[str]] = {}
    for mirror, original in mirrors.items():
        if is_mirror_name(mirror):
            by_original.setdefault(original, []).append(mirror)
    pairs = {
        (by_original[s["namespace"]][0], s["source_id"]): s["namespace"]
        for s in sources
        if len(by_original.get(s["namespace"], [])) == 1
    }
    rows = await store.listings(plan.org, list(pairs))
    plan.reader = await resolve_reader(plan.org, subject) if subject else None
    plan.listing_starts = {
        (r["namespace"], r["source_id"]): r["started_at"]
        for r in rows
        if (r["namespace"], r["source_id"]) in pairs
        and fresh(r["started_at"], now(), settings.knowledge_source_acl_stale_seconds)
    }
    plan.sources = {key: pairs[key] for key in plan.listing_starts}
