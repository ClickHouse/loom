"""Admin registration of an opt-in, read-only query-history source."""

from __future__ import annotations

import time
import uuid
from typing import Any

from server.core import lease_writes, leases
from server.core.datetimes import naive_utc_now_seconds
from server.core.errors import OverloadedError, ValidationError
from server.core.secrets_at_rest import seal
from server.knowledge.api.models_query_history import (
    QueryHistoryAddInput,
    QueryHistoryListInput,
    QueryHistoryListOutput,
    QueryHistoryRemoveInput,
    QueryHistoryRemoveOutput,
    QueryHistorySource,
)
from server.knowledge.app.gaps import erasure_since
from server.knowledge.store import query_history as repo
from server.knowledge.store import query_history_remote as remote
from server.memory.app.definition_review import require_draft_fleet
from server.memory.app.provenance import writer_actor
from server.memory.domain.provenance import source_class_for
from server.operations.read_projection import project_rows
from server.settings import settings
from server.tracker.app.onboarding import review_grants


def source_view(row: dict[str, Any]) -> QueryHistorySource:
    return QueryHistorySource.model_validate({**row, "source_id": str(row["source_id"])})


def source_audit_subject(input: Any, response: Any) -> dict[str, str]:
    return {"org": input.org, "namespace": input.namespace, "name": input.name}


async def probe(row: dict[str, Any]) -> None:
    async with remote.connection(row) as client:
        direct = await remote.query(client, "SHOW GRANTS")
        inherited = await remote.query(client, "SHOW GRANTS FINAL")
        roles = await remote.query(
            client, "SELECT role_name, with_admin_option FROM system.enabled_roles"
        )
        grants = review_grants(
            [str(next(iter(r.values()))) for r in direct + inherited if r],
            roles,
            allow_remote=bool(row["cluster"]),
        )
        if not grants.read_only:
            raise ValidationError(
                "query history requires a read-only credential: " + grants.refusal(row["user"])
            )
        # Verify this credential can read the required log columns and the chosen cluster.
        await remote.query(
            client,
            remote.group_sql(bool(row["cluster"])),
            {
                "cluster": row["cluster"],
                "databases": row["databases"],
                "days": 1,
                "k": 3,
                "lim": 1,
            },
        )


async def handle_add(input: QueryHistoryAddInput) -> QueryHistorySource:
    started = int(time.time())
    await require_draft_fleet()
    if not settings.knowledge_query_history_enabled:
        raise ValidationError("query history is disabled on this deployment")
    if not input.name.strip() or any(not d.strip() for d in input.databases):
        raise ValidationError("a source name and database names must not be blank")
    row = {
        "org": input.org,
        "namespace": input.namespace,
        "agent": "-",
        "user_id": "-",
        "session_id": "-",
        "name": input.name,
        "source_id": str(uuid.uuid4()),
        "host": input.host,
        "port": input.port,
        "secure": input.secure,
        "user": input.user,
        "password_enc": seal(remote.PASSWORD_PURPOSE, input.password),
        "cluster": input.cluster,
        "databases": sorted(set(input.databases)),
        "actor": writer_actor("explicit"),
        "source_class": source_class_for(default="asserted"),
        "created_at": naive_utc_now_seconds(),
        "tombstoned_at": None,
        "version": time.time_ns(),
    }
    await probe(row)
    async with leases.holding(
        f"query_history.sources/{input.org}/{input.namespace}", ttl_s=30
    ) as held:
        if not held:
            raise OverloadedError("query-history sources are being changed", retry_after_s=1)
        old = await repo.find(input.org, input.namespace, input.name)
        if old is not None and old["tombstoned_at"] is None:
            raise ValidationError(
                "that source is registered; remove it before registering it again"
            )
        if (
            len(
                await repo.list_sources(
                    input.org, input.namespace, settings.knowledge_query_history_max_sources
                )
            )
            >= settings.knowledge_query_history_max_sources
        ):
            raise ValidationError("the namespace has reached its query-history source limit")
        await require_draft_fleet()
        row["version"] = await repo.next_source_version(input.org, input.namespace, input.name)
        await _persist(row, started)
    return source_view(row)


async def handle_list(input: QueryHistoryListInput) -> QueryHistoryListOutput:
    return QueryHistoryListOutput(
        enabled=settings.knowledge_query_history_enabled,
        sources=project_rows(
            [
                source_view(r)
                for r in await repo.list_sources(input.org, input.namespace, input.limit)
            ],
            QueryHistorySource,
            input.fields,
            always=frozenset({"name", "source_id"}),
        ),
    )


async def handle_remove(input: QueryHistoryRemoveInput) -> QueryHistoryRemoveOutput:
    started = int(time.time())
    async with leases.holding(
        f"query_history.sources/{input.org}/{input.namespace}", ttl_s=30
    ) as held:
        if not held:
            raise OverloadedError("query-history sources are being changed", retry_after_s=1)
        row = await repo.find(input.org, input.namespace, input.name)
        if row is None or row["tombstoned_at"] is not None:
            return QueryHistoryRemoveOutput(removed=False)
        await _persist(
            {
                **row,
                "actor": writer_actor("explicit"),
                "source_class": source_class_for(default="asserted"),
                "password_enc": "",
                "tombstoned_at": naive_utc_now_seconds(),
                "version": await repo.next_source_version(input.org, input.namespace, input.name),
            },
            started,
        )
    return QueryHistoryRemoveOutput(removed=True)


async def _persist(row: dict[str, Any], started: int) -> None:
    org, namespace = row["org"], row["namespace"]
    async with lease_writes.fenced(f"query_history.writer/{org}") as uncertain:
        if await erasure_since(org, namespace, started, set()):
            raise ValidationError("an erasure reached this namespace; retry the source change")
        uncertain(True)
        await repo.insert(row)
        uncertain(False)
