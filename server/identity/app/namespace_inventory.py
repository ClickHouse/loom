"""namespace.inventory — what one namespace holds and who appears in it, read-only.

Each section and population runs on its own: one that fails, or that the caller may
not read, is listed with `complete: false` and its reason, and the rest still answer.
Registered members and the identities observed on rows are separate populations.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from server.auth.authz import check_authorized
from server.auth.context import current_auth
from server.core.datetimes import naive_utc_now_seconds
from server.identity.api.models_inventory import (
    InventoryCapability,
    InventoryIdentity,
    InventoryPopulation,
    InventoryScope,
    InventorySection,
    NamespaceInventoryInput,
    NamespaceInventoryOutput,
)
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import ReadFilters
from server.identity.store import grants as grants_repo
from server.identity.store import inventory as inventory_repo
from server.operations.base import Role
from server.settings import settings

_NARROWED = "not counted under a read scope bound to one user or session"


async def handle_namespace_inventory(input: NamespaceInventoryInput) -> NamespaceInventoryOutput:
    computed_at = naive_utc_now_seconds()
    scope = request_scope()
    org, ns = input.org, input.namespace

    def scoped(plane: str) -> Awaitable[InventorySection]:
        return _section(plane, lambda: inventory_repo.plane_count(org, ns, plane, scope=scope))

    sections = [
        scoped("memories"),
        scoped("records"),
        _documents(org, ns, scope),
        scoped("runs"),
        _integrations(org, ns, scope),
    ]
    populations: list[Awaitable[InventoryPopulation]] = [_members(org, ns, scope)]
    populations += [
        _observed(org, ns, p, scope, input.population_limit)
        for p in ("agents", "authors", "subjects")
    ]
    section_rows, population_rows = await asyncio.gather(
        asyncio.gather(*sections), asyncio.gather(*populations)
    )
    return NamespaceInventoryOutput(
        scope=InventoryScope(
            org=org,
            namespace=ns,
            read_scope=scope.scope,
            user_id=scope.user_id if scope.narrowed else None,
            session_id=scope.session_id if scope.scope == "own_session" else None,
        ),
        sections=list(section_rows),
        populations=list(population_rows),
        capabilities=_capabilities(),
        computed_at=computed_at,
    )


async def _section(
    plane: Any, read: Callable[[], Awaitable[tuple[int, datetime | None]]]
) -> InventorySection:
    try:
        n, latest = await read()
    except Exception as e:  # noqa: BLE001 — one plane's failure leaves the others standing
        return _missing(plane, _failed(e))
    return InventorySection(plane=plane, count=n, latest_at=latest, complete=True)


async def _documents(org: str, ns: str, scope: ReadFilters) -> InventorySection:
    # Document rows are not bounded by the read scope, so they are counted namespace-wide only.
    if scope.narrowed:
        return _missing("documents", _NARROWED)
    return await _section("documents", lambda: inventory_repo.document_count(org, ns))


async def _integrations(org: str, ns: str, scope: ReadFilters) -> InventorySection:
    """Webhooks are listed to admins only (`webhook.list`), so only they see the count."""
    if scope.narrowed:
        return _missing("integrations", _NARROWED)
    try:
        await check_authorized(current_auth(), Role.ADMIN, org=org, namespace=ns)
    except PermissionError:
        return _missing("integrations", "counted for namespace admins only")
    return await _section("integrations", lambda: inventory_repo.webhook_count(org, ns))


def _missing(plane: Any, reason: str) -> InventorySection:
    return InventorySection(plane=plane, count=None, latest_at=None, complete=False, reason=reason)


async def _members(org: str, ns: str, scope: ReadFilters) -> InventoryPopulation:
    if scope.narrowed:
        return InventoryPopulation(
            population="members", total=None, complete=False, reason=_NARROWED
        )
    try:
        total = await grants_repo.people_count(org, ns)
    except Exception as e:  # noqa: BLE001 — one population's failure leaves the others standing
        return InventoryPopulation(
            population="members", total=None, complete=False, reason=_failed(e)
        )
    return InventoryPopulation(population="members", total=total, complete=True)


async def _observed(
    org: str, ns: str, population: Any, scope: ReadFilters, limit: int
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


def _capabilities() -> list[InventoryCapability]:
    """Whether a real model and a real embedder are configured (as `/v1/health` reads it)."""
    from server.llm.client import provider_key_missing

    llm = settings.llm_provider
    return [
        InventoryCapability(name="llm", enabled=llm != "fake" and not provider_key_missing(llm)),
        InventoryCapability(
            name="embedder", enabled=not settings.embedding_model.startswith("fake")
        ),
    ]


def _failed(e: Exception) -> str:
    return f"read failed: {type(e).__name__}"
