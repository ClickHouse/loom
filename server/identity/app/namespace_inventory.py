"""namespace.inventory — what one namespace holds and who appears in it, read-only.

Each count is read through the plane that owns its table, so each table's liveness
rule stays in one store. Each section and population runs on its own: one that fails,
or that the caller may not read, is listed with `complete: false` and its reason, and
the rest still answer. A store that is down or overloaded fails the whole call, as any
read does. Registered members and the identities observed on rows are separate
populations.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from datetime import datetime
from typing import cast, get_args

from server.auth import sharing
from server.auth.authz import check_authorized
from server.auth.context import current_auth
from server.auth.fail_closed import note_answered_unverified
from server.core.datetimes import naive_utc_now_seconds
from server.core.errors import BackendTimeoutError, BackendUnavailableError, OverloadedError
from server.embedding.embedder import embedder_is_fake
from server.identity.api.models_inventory import (
    InventoryCapability,
    InventoryIdentity,
    InventoryPlane,
    InventoryPopulation,
    InventoryScope,
    InventorySection,
    NamespaceInventoryInput,
    NamespaceInventoryOutput,
    ObservedPopulation,
    ReadScope,
)
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import ReadFilters
from server.identity.store import grants as grants_repo
from server.knowledge.app.document_reads import current_document_count
from server.llm.client import llm_is_fake
from server.memory.app.query import identity_tally, memory_tally
from server.memory.app.records import record_tally
from server.memory.app.webhooks import namespace_webhook_count
from server.operations.base import Role
from server.tasks.app.run import run_tally

log = logging.getLogger(__name__)

_NARROWED = "not counted under a read scope bound to one user or session"
# The store is down or overloaded: the call fails, as every read does, so an outage is
# not answered as a successful report of empty sections.
_NOT_NOW = (BackendUnavailableError, BackendTimeoutError, OverloadedError)
# Observed population → the memory column that names it.
_COLUMN: dict[ObservedPopulation, str] = {
    "agents": "agent",
    "authors": "actor",
    "subjects": "user_id",
}


async def handle_namespace_inventory(input: NamespaceInventoryInput) -> NamespaceInventoryOutput:
    computed_at = naive_utc_now_seconds()
    scope = request_scope()
    org, ns = input.org, input.namespace
    sections = [
        _section("memories", memory_tally(org, ns)),
        _section("records", record_tally(org, ns)),
        _documents(org, ns, scope),
        _section("runs", run_tally(org, ns)),
        _integrations(org, ns, scope),
    ]
    populations = [
        _members(org, ns, scope),
        *(_observed(org, ns, p, input.population_limit) for p in get_args(ObservedPopulation)),
    ]
    section_rows, population_rows = await asyncio.gather(
        asyncio.gather(*sections), asyncio.gather(*populations)
    )
    return NamespaceInventoryOutput(
        scope=InventoryScope(
            org=org,
            namespace=ns,
            read_scope=cast(ReadScope, scope.scope),
            user_id=scope.user_id if scope.narrowed else None,
            session_id=scope.session_id if scope.scope == "own_session" else None,
        ),
        sections=list(section_rows),
        populations=list(population_rows),
        capabilities=[
            InventoryCapability(name="llm", enabled=not llm_is_fake()),
            InventoryCapability(name="embedder", enabled=not embedder_is_fake()),
        ],
        computed_at=computed_at,
    )


async def _section(
    plane: InventoryPlane, read: Awaitable[tuple[int, datetime | None]]
) -> InventorySection:
    try:
        n, latest = await read
    except _NOT_NOW:
        raise
    except Exception as e:  # noqa: BLE001 — one plane's failure leaves the others standing
        return _missing(plane, _failed(plane, e))
    return InventorySection(plane=plane, count=n, latest_at=latest, complete=True)


async def _documents(org: str, ns: str, scope: ReadFilters) -> InventorySection:
    # Document rows are not bounded by the read scope, so they are counted namespace-wide only.
    if scope.narrowed:
        return _missing("documents", _NARROWED)
    return await _section("documents", current_document_count(org, ns))


async def _integrations(org: str, ns: str, scope: ReadFilters) -> InventorySection:
    """Webhooks are listed to admins only (`webhook.list`), so only they see the count."""
    if scope.narrowed:
        return _missing("integrations", _NARROWED)
    try:
        await check_authorized(current_auth(), Role.ADMIN, org=org, namespace=ns)
    except PermissionError as e:
        if not getattr(e, "unverified", False):
            return _missing("integrations", "counted for namespace admins only")
        # The grant read failed: the request is the server's failure, as in row_in_scope.
        note_answered_unverified()
        return _missing("integrations", _failed("integrations", e))
    return await _section("integrations", namespace_webhook_count(org, ns))


def _missing(plane: InventoryPlane, reason: str) -> InventorySection:
    return InventorySection(plane=plane, count=None, latest_at=None, complete=False, reason=reason)


async def _members(org: str, ns: str, scope: ReadFilters) -> InventoryPopulation:
    if scope.narrowed:
        return InventoryPopulation(
            population="members", total=None, complete=False, reason=_NARROWED
        )
    if sharing.is_personal(ns):
        # A personal collection takes no grant: its owner reaches it by name.
        reason = "a personal collection: its owner holds no grant"
        return InventoryPopulation(population="members", total=None, complete=False, reason=reason)
    try:
        people, shared = await grants_repo.member_count(org, ns)
        # Only a collection inherits: a parent's grants reach every child.
        inherited = sharing.is_collection(ns) and len((await sharing.tree(org)).lineage(ns)) > 1
    except _NOT_NOW:
        raise
    except Exception as e:  # noqa: BLE001 — one population's failure leaves the others standing
        reason = _failed("members", e)
        return InventoryPopulation(population="members", total=None, complete=False, reason=reason)
    wider = []
    if shared:
        wider.append("shared with everyone or a group")
    if inherited:
        wider.append("inherits the grants of a parent collection")
    return InventoryPopulation(
        population="members",
        total=people,
        complete=not wider,
        reason=f"{'; '.join(wider)}: those members are not counted" if wider else "",
    )


async def _observed(
    org: str, ns: str, population: ObservedPopulation, limit: int
) -> InventoryPopulation:
    try:
        total, top = await identity_tally(org, ns, _COLUMN[population], limit=limit)
    except _NOT_NOW:
        raise
    except Exception as e:  # noqa: BLE001 — one population's failure leaves the others standing
        reason = _failed(population, e)
        return InventoryPopulation(population=population, total=None, complete=False, reason=reason)
    return InventoryPopulation(
        population=population,
        total=total,
        top=[InventoryIdentity(name=name, rows=n) for name, n in top],
        complete=True,
    )


def _failed(what: str, e: Exception) -> str:
    log.warning("namespace.inventory: %s read failed: %s", what, type(e).__name__)
    return f"read failed: {type(e).__name__}"
