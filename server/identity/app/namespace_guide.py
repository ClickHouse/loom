"""namespace.guide_get / guide_set / guide_history.

A namespace's guide says what the namespace holds and how to use it: its key
layout, what its tags mean, which ops to call. Agents read it before their
first write and update it when they change a convention. Each edit names the
revision it was made from; of two edits made from one revision the second is
refused with the current guide, which its writer merges into and sends again.
The server never merges text itself.

A save's audit row records the revision it was made from and the text's length
and hash (`guide_audit_subject`): the audit log outlives an erasure, so the text
itself stays out of it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from server.auth.sharing import is_personal
from server.core import instances, leases
from server.core.ch import own_write_reads
from server.core.datetimes import naive_utc_now
from server.core.errors import OverloadedError, ValidationError
from server.identity.api.models_namespace_guide import (
    NamespaceGuideGetInput,
    NamespaceGuideGetOutput,
    NamespaceGuideHistoryInput,
    NamespaceGuideHistoryOutput,
    NamespaceGuideSetInput,
    NamespaceGuideSetOutput,
    _GuideRevision,
)
from server.identity.app.request_scope import request_narrowed
from server.identity.store import namespace_guides as repo
from server.memory.app.provenance import writer_actor

# 072_namespace_guides.sql made the table: a build below it cannot erase a guide.
_GUIDES_MIGRATION = 72


async def handle_namespace_guide_get(input: NamespaceGuideGetInput) -> NamespaceGuideGetOutput:
    """The current guide, or the revision asked for."""
    with own_write_reads():  # an agent reads right after another one saved, on any replica
        if input.revision:
            row = await repo.at_revision(input.org, input.namespace, input.revision)
        else:
            row = await repo.latest(input.org, input.namespace)
    return _output(row)


async def handle_namespace_guide_set(input: NamespaceGuideSetInput) -> NamespaceGuideSetOutput:
    """Write a new revision when `expected_revision` is the current one. The
    check and the insert run under a deployment-wide claim on the namespace's
    guide, so two replicas cannot both pass the check. A retry of a save that
    landed finds its own text current and answers without writing again."""
    if is_personal(input.namespace):
        # One person's namespace needs no guide, and only a user erasure removes it.
        raise ValidationError("a personal namespace has no guide")
    if not _every_replica_erases_guides():
        raise OverloadedError(
            "not every replica serves namespace guides yet; retry once the rollout is done",
            retry_after_s=30,
        )
    name = "namespace-guide:" + json.dumps([input.org, input.namespace])
    # A save is one read and one insert; the TTL is margin for a slow insert.
    async with leases.holding(name, ttl_s=30, wait_s=5.0) as held:
        if not held:
            raise OverloadedError("another edit of this guide is being saved; retry shortly")
        with own_write_reads():
            row = await repo.latest(input.org, input.namespace)
        current = int(row["revision"]) if row else 0
        summary = _stored_summary(input, row)
        if row is not None and (row["content"], row["summary"]) == (input.content, summary):
            return NamespaceGuideSetOutput(written=False, revision=current)
        if input.expected_revision != current:
            return NamespaceGuideSetOutput(
                written=False, conflict=True, revision=current, current=_output(row)
            )
        if row is None and not input.content:
            return NamespaceGuideSetOutput(written=False, revision=0)
        await repo.insert_row(
            {
                "org": input.org,
                "namespace": input.namespace,
                "revision": current + 1,
                "content": input.content,
                "summary": summary,
                "note": input.note,
                "updated_by": writer_actor(),
                "updated_at": naive_utc_now(),
            }
        )
    return NamespaceGuideSetOutput(written=True, revision=current + 1)


async def handle_namespace_guide_history(
    input: NamespaceGuideHistoryInput,
) -> NamespaceGuideHistoryOutput:
    """The newest revisions, newest first, without their text."""
    with own_write_reads():
        rows = await repo.history(input.org, input.namespace, input.limit)
    return NamespaceGuideHistoryOutput(
        revisions=[
            _GuideRevision(
                revision=int(r["revision"]),
                summary=str(r["summary"]),
                note=str(r["note"]),
                updated_by=str(r["updated_by"]),
                updated_at=r["updated_at"],
                chars=int(r["chars"]),
            )
            for r in rows
        ]
    )


async def current_guide(org: str, namespace: str) -> NamespaceGuideGetOutput:
    """The current guide as other reads show it (`namespace.get`, the session-start
    pack): from any replica, without the sequentially consistent read a get makes."""
    return _output(await repo.latest(org, namespace))


def guide_audit_subject(input: NamespaceGuideSetInput) -> dict[str, Any]:
    """What a save's audit row records: never the text, summary or note."""
    return {
        "org": input.org,
        "namespace": input.namespace,
        "expected_revision": input.expected_revision,
        "chars": len(input.content),
        "content_sha256": hashlib.sha256(input.content.encode()).hexdigest(),
    }


def _every_replica_erases_guides() -> bool:
    """Whether every live replica runs a build with loom.namespace_guides: an
    older one's namespace erasure has no step for the table, so a guide written
    while it serves could outlive an erasure it runs. Unknown (no recent
    heartbeat read) counts as no."""
    live = instances.live_rows()
    return bool(live) and all(int(r["schema_version"] or 0) >= _GUIDES_MIGRATION for r in live)


def _stored_summary(input: NamespaceGuideSetInput, row: dict[str, Any] | None) -> str:
    """The summary the save stores: none for a cleared guide, the current one
    when the save leaves it out, and otherwise the one sent, on one line."""
    if not input.content:
        return ""
    if input.summary is None:
        return str(row["summary"]) if row else ""
    return " ".join(input.summary.split())


def _output(row: dict[str, Any] | None) -> NamespaceGuideGetOutput:
    # A token narrowed to one user reads the guide (guide_get, namespace.get, the
    # session-start pack) but not who wrote it.
    if row is None:
        return NamespaceGuideGetOutput(found=False)
    return NamespaceGuideGetOutput(
        found=bool(row["content"]),
        revision=int(row["revision"]),
        content=str(row["content"]),
        summary=str(row["summary"]),
        note=str(row["note"]),
        updated_by="" if request_narrowed() else str(row["updated_by"]),
        updated_at=row["updated_at"],
    )
