"""An admin's approved definition draft, through the ordinary write and promote paths."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import server.memory.store as memory_repo
from server.core import lease_writes
from server.core.ch import get_client
from server.core.datetimes import naive_utc_now
from server.core.errors import (
    OverloadedError,
    PreconditionFailedError,
    SensitiveDefinitionRefusedError,
)
from server.core.hashing import served_content_sha256
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.api.models_crud import MemorySetInput
from server.memory.api.models_lifecycle import MemoryPromoteInput
from server.memory.app import promote
from server.memory.app.sensitivity_level import decoded_text, detect_level
from server.memory.domain import definition_review as plan
from server.memory.domain.definition_check import sensitivity_lapse
from server.memory.domain.definitions import definition_names, fold_name
from server.memory.store import shared as shared_repo
from server.settings import settings

# The first build whose readers and erasure know private definition candidates.
DEFINITION_DRAFT_MIGRATION = 102


async def require_draft_fleet() -> None:
    from server.auth.machines import fleet_at

    if not await fleet_at(get_client(), DEFINITION_DRAFT_MIGRATION):
        raise OverloadedError(
            "not every replica supports private definition drafts; retry after the rollout",
            retry_after_s=30,
        )


async def promote_candidate(
    org: str,
    namespace: str,
    row: dict[str, Any],
    reason: str,
    *,
    uncertain: Callable[[bool], None],
) -> None:
    """Preflight routine refusals before writing; an interrupted promotion leaves a draft."""
    from server.memory.app.write import handle_memory_set

    await require_draft_fleet()
    value = json.loads(row["proposed_value"])
    shared = await shared_repo.shared_definitions(
        org, namespace, settings.memory_shared_definitions_max + 1
    )
    key = plan.target_key(value, row["memory_key"], shared)
    names = definition_names(value)
    assert names is not None  # target_key validated the same value
    await promote._check_shared_definitions(org, namespace, names, key)
    graded = await detect_level(await decoded_text(value))
    if sensitivity_lapse("", graded) is not None:
        raise SensitiveDefinitionRefusedError("a sensitive definition draft is not promoted")
    identity = memory_repo.Identity(org=org, namespace=namespace, agent="-")
    head = await memory_repo.get_by_key(
        identity, key, scope=WHOLE_NAMESPACE, fresh=True, strong=True
    )
    copy = await shared_repo.fetch_shared(org, namespace, key, strong=True)
    shared_value = (
        json.loads(copy["value"]) if copy is not None and copy["tombstoned_at"] is None else None
    )
    selected = next((r for r in shared if r["memory_key"] == key), None)
    if selected is not None and (
        shared_value is None
        or fold_name(value["term"]) not in (definition_names(shared_value) or set())
    ):
        raise PreconditionFailedError("the shared definition changed while planning this review")
    if plan.write_needed(head, shared_value, value, now=naive_utc_now()):
        uncertain(True)
        await handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=namespace,
                agent="-",
                memory_key=key,
                memory_type="definition",
                tags=[f"candidate-id:{row['candidate_id']}"],
                value=value,
                clear_expiry=True,
                source_class="derived",
                source_trace=f"learning:{row['candidate_id']}",
                source_resource_id=str(row.get("source_resource_id") or ""),
                if_match_sha256=(
                    served_content_sha256(head.value) if head is not None else "absent"
                ),
            )
        )
        uncertain(False)
    uncertain(True)
    result = await promote.handle_memory_promote(
        MemoryPromoteInput(
            org=org,
            namespace=namespace,
            agent="-",
            memory_key=key,
            approved_sha256=served_content_sha256(value),
            rationale=reason,
        ),
        reviewed_candidate_id=str(row["candidate_id"]),
    )

    uncertain(False)

    from server.memory.app.provenance import writer_actor
    from server.observability import audit

    await audit.emit(
        "memory.promote",
        org=org,
        namespace=namespace,
        actor=writer_actor("learnings-reviewer"),
        request_id=result.promotion_event,
        subject={
            "memory_key": key,
            "promotion_event": result.promotion_event,
            "promoted_from_candidate_id": str(row["candidate_id"]),
            "approved_sha256": served_content_sha256(value),
        },
    )


async def redact_candidates(org: str, target: Any, head: Any) -> None:
    """Scrub private drafts of the redacted text, serially with their review decisions."""
    from server.core import leases
    from server.core.ch import set_query_tag
    from server.memory.store import learnings

    if (target.agent, target.user_id, target.session_id) != (
        "-",
        "-",
        "-",
    ) or not target.memory_key.startswith("definition/"):
        return
    if head is not None and head.content_sha256 == target.content_sha256:
        return
    tagged = {t.removeprefix("candidate-id:") for t in target.tags if t.startswith("candidate-id:")}
    after = ""
    while True:
        set_query_tag("memory.redact.candidates")
        rows = await learnings.private_candidates_after(org, target.namespace, after)
        if not rows:
            return
        for row in rows:
            cid = row["candidate_id"]
            try:
                matches = (
                    served_content_sha256(json.loads(row["proposed_value"]))
                    == target.content_sha256
                )
            except (ValueError, TypeError):
                matches = False
            if not matches and cid not in tagged:
                continue
            async with leases.holding(
                f"definition.review/{org}/{target.namespace}/{cid}", ttl_s=30
            ) as held:
                if not held:
                    raise OverloadedError(
                        "a draft is being reviewed; retry redaction", retry_after_s=1
                    )
                async with lease_writes.fenced(f"definition.writer/{org}") as uncertain:
                    uncertain(True)
                    await learnings.scrub_private_candidate(org, target.namespace, cid)
                    uncertain(False)
        after = rows[-1]["candidate_id"]
