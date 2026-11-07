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
from server.auth.authz import check_authorized, is_org_readable_code_namespace
from server.auth.context import current_auth
from server.auth.fail_closed import note_answered_unverified
from server.core.datetimes import naive_utc_now_seconds
from server.core.errors import BACKEND_NOT_NOW
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
    Population,
    ReadScope,
)
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import ReadFilters
from server.identity.store import grants as grants_repo
from server.knowledge.app.document_reads import document_tally
from server.llm.client import llm_is_fake
from server.memory.app.query import identity_tally, memory_tally
from server.memory.app.records import record_tally
from server.memory.app.search import pg_validation
from server.memory.app.webhooks import webhook_tally
from server.memory.domain.identity_columns import IdentityColumn
from server.operations.base import Role
from server.tasks.app.run import run_tally

log = logging.getLogger(__name__)

_NARROWED = "not counted under a read scope bound to one user or session"
# Observed population → the memory column that names it.
_COLUMN: dict[ObservedPopulation, IdentityColumn] = {
    "agents": "agent",
    "authors": "actor",
    "subjects": "user_id",
}


async def handle_namespace_inventory(input: NamespaceInventoryInput) -> NamespaceInventoryOutput:
    async with pg_validation.published_reads(input.org, [input.namespace]):
        return await _inventory(input)


async def _inventory(input: NamespaceInventoryInput) -> NamespaceInventoryOutput:
    computed_at = naive_utc_now_seconds()
    scope = request_scope()
    org, ns = input.org, input.namespace
    sections = [
        _section("memories", memory_tally(org, ns)),
        _records(org, ns, scope),
        # Documents are the namespace's under any scope, as `knowledge.*` reads them.
        _section("documents", document_tally(org, ns)),
        _section("runs", run_tally(org, ns)),
        _integrations(org, ns, scope),
    ]
    populations = [
        _members(org, ns),
        *(_observed(org, ns, p, input.population_limit) for p in get_args(ObservedPopulation)),
    ]
    # A task group: a store outage in one leg cancels the others' reads rather than
    # leaving them running, and fails the call with that leg's error.
    try:
        async with asyncio.TaskGroup() as tg:
            section_tasks = [tg.create_task(c) for c in sections]
            population_tasks = [tg.create_task(c) for c in populations]
    except ExceptionGroup as eg:
        raise eg.exceptions[0] from None
    section_rows = [t.result() for t in section_tasks]
    population_rows = [t.result() for t in population_tasks]
    return NamespaceInventoryOutput(
        scope=InventoryScope(
            org=org,
            namespace=ns,
            read_scope=cast(ReadScope, scope.scope),
            user_id=scope.user_id if scope.narrowed else None,
            session_id=scope.session_id if scope.scope == "own_session" else None,
        ),
        sections=section_rows,
        populations=population_rows,
        capabilities=[
            InventoryCapability(name="llm", enabled=not llm_is_fake()),
            InventoryCapability(name="embedder", enabled=not embedder_is_fake()),
        ],
        computed_at=computed_at,
    )


async def _section(
    plane: InventoryPlane, read: Awaitable[tuple[int, datetime | None]]
) -> InventorySection:
    tally, failed = await _attempt(plane, read)
    if tally is None:
        return _missing(plane, failed)
    n, latest = tally
    return InventorySection(plane=plane, count=n, latest_at=latest, complete=True)


async def _records(org: str, ns: str, scope: ReadFilters) -> InventorySection:
    # A record key holds one active record whoever writes it, so a narrowed count would
    # show a user the keys someone else wrote last, not what is theirs.
    if scope.narrowed:
        return _missing("records", _NARROWED)
    return await _section("records", record_tally(org, ns))


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
    except BACKEND_NOT_NOW:
        raise
    except Exception as e:  # noqa: BLE001 — a failed admin check leaves the other legs standing
        return _missing("integrations", _failed("integrations", e))
    return await _section("integrations", webhook_tally(org, ns))


def _missing(plane: InventoryPlane, reason: str) -> InventorySection:
    return InventorySection(plane=plane, count=None, latest_at=None, complete=False, reason=reason)


async def _members(org: str, ns: str) -> InventoryPopulation:
    # Grants hold no user's memory: counted under any scope, as `grants.list` lists them.
    if sharing.is_personal(ns):
        # A personal collection takes no grant: its owner reaches it by name.
        return _not_counted("members", "a personal collection: its owner holds no grant")

    # A collection's readers hold a grant on it or on an ancestor (`lineage`, it first).
    lineage = [ns]
    if sharing.is_collection(ns):
        tree, failed = await _attempt("members", sharing.tree(org))
        if tree is None:
            return _not_counted("members", failed)
        lineage = tree.lineage(ns)
    counted, failed = await _attempt("members", grants_repo.member_count(org, lineage))
    if counted is None:
        return _not_counted("members", failed)
    people, shared = counted
    wider = []
    if await is_org_readable_code_namespace(org, ns):
        wider.append("readable by everyone in the org")
    if shared:
        wider.append("shared with everyone, a group or an agent")
    return InventoryPopulation(
        population="members",
        total=people,
        complete=not wider,
        reason=f"{'; '.join(wider)}: those members are not counted" if wider else "",
    )


async def _observed(
    org: str, ns: str, population: ObservedPopulation, limit: int
) -> InventoryPopulation:
    tally, failed = await _attempt(
        population, identity_tally(org, ns, _COLUMN[population], limit=limit)
    )
    if tally is None:
        return _not_counted(population, failed)
    total, top = tally
    return InventoryPopulation(
        population=population,
        total=total,
        top=[InventoryIdentity(name=name, rows=n) for name, n in top],
        complete=True,
    )


def _not_counted(population: Population, reason: str) -> InventoryPopulation:
    return InventoryPopulation(population=population, total=None, complete=False, reason=reason)


async def _attempt[T](what: str, read: Awaitable[T]) -> tuple[T | None, str]:
    """`read`'s result, or None and why it failed. A store outage is raised (the call
    fails, as any read does); any other failure is logged and labeled, and the other
    legs still answer."""
    try:
        return await read, ""
    except BACKEND_NOT_NOW:
        raise
    except Exception as e:  # noqa: BLE001 — one leg's failure leaves the others standing
        return None, _failed(what, e)


def _failed(what: str, e: Exception) -> str:
    log.warning("namespace.inventory: %s read failed: %s", what, e, exc_info=True)
    return f"read failed: {type(e).__name__}"
