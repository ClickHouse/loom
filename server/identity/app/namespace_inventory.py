"""namespace.inventory — what one namespace holds and who appears in it, read-only.

Each section and population runs on its own: one that fails, or that the caller may
not read, is listed with `complete: false` and its reason, and the rest still answer.
Registered members and the identities observed on rows are separate populations.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import datetime
from typing import cast

from server.auth.authz import check_authorized
from server.auth.context import current_auth
from server.core.datetimes import naive_utc_now_seconds
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
from server.identity.store import inventory as inventory_repo
from server.llm.client import llm_is_fake
from server.operations.base import Role

_NARROWED = "not counted under a read scope bound to one user or session"
_OBSERVED: tuple[ObservedPopulation, ...] = ("agents", "authors", "subjects")


async def handle_namespace_inventory(input: NamespaceInventoryInput) -> NamespaceInventoryOutput:
    computed_at = naive_utc_now_seconds()
    scope = request_scope()
    org, ns = input.org, input.namespace

    def scoped(plane: InventoryPlane) -> Awaitable[InventorySection]:
        return _section(plane, inventory_repo.plane_count(org, ns, plane, scope=scope))

    sections = [
        scoped("memories"),
        scoped("records"),
        _documents(org, ns, scope),
        scoped("runs"),
        _integrations(org, ns, scope),
    ]
    populations = [
        _members(org, ns, scope),
        *(_observed(org, ns, p, scope, input.population_limit) for p in _OBSERVED),
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
    except Exception as e:  # noqa: BLE001 — one plane's failure leaves the others standing
        return _missing(plane, _failed(e))
    return InventorySection(plane=plane, count=n, latest_at=latest, complete=True)


async def _documents(org: str, ns: str, scope: ReadFilters) -> InventorySection:
    # Document rows are not bounded by the read scope, so they are counted namespace-wide only.
    if scope.narrowed:
        return _missing("documents", _NARROWED)
    return await _section("documents", inventory_repo.document_count(org, ns))


async def _integrations(org: str, ns: str, scope: ReadFilters) -> InventorySection:
    """Webhooks are listed to admins only (`webhook.list`), so only they see the count."""
    if scope.narrowed:
        return _missing("integrations", _NARROWED)
    try:
        await check_authorized(current_auth(), Role.ADMIN, org=org, namespace=ns)
    except PermissionError:
        return _missing("integrations", "counted for namespace admins only")
    except Exception as e:  # noqa: BLE001 — a failed grant read leaves the other sections standing
        return _missing("integrations", _failed(e))
    return await _section("integrations", inventory_repo.webhook_count(org, ns))


def _missing(plane: InventoryPlane, reason: str) -> InventorySection:
    return InventorySection(plane=plane, count=None, latest_at=None, complete=False, reason=reason)


async def _members(org: str, ns: str, scope: ReadFilters) -> InventoryPopulation:
    if scope.narrowed:
        return InventoryPopulation(
            population="members", total=None, complete=False, reason=_NARROWED
        )
    try:
        total = await grants_repo.member_count(org, ns)
    except Exception as e:  # noqa: BLE001 — one population's failure leaves the others standing
        return InventoryPopulation(
            population="members", total=None, complete=False, reason=_failed(e)
        )
    return InventoryPopulation(population="members", total=total, complete=True)


async def _observed(
    org: str, ns: str, population: ObservedPopulation, scope: ReadFilters, limit: int
) -> InventoryPopulation:
    try:
        total, top = await inventory_repo.observed_identities(
            org, ns, population, scope=scope, limit=limit
        )
    except Exception as e:  # noqa: BLE001 — one population's failure leaves the others standing
        return InventoryPopulation(
            population=population, total=None, complete=False, reason=_failed(e)
        )
    return InventoryPopulation(
        population=population,
        total=total,
        top=[InventoryIdentity(name=name, rows=n) for name, n in top],
        complete=True,
    )


def _failed(e: Exception) -> str:
    return f"read failed: {type(e).__name__}"
