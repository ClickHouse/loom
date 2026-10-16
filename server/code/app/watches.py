"""Coverage watches: a push when every index tier covers a commit.

A consumer that reviews merged PRs would otherwise poll
`code.index_status commit=<sha>` until `commit_coverage.ready`. A watch
names the commit and one of the consumer's task inboxes; once the commit is
covered, one `code.coverage.ready` row lands in `loom.task_events` for that
inbox, so `/v1/task/watch`, `task.updates` and the inbox's signed webhook
carry it like any other task event.

Exactly once: the event's `task_id` is derived from (org, namespace, commit,
inbox), and the delivery checks for that id before inserting. A crash between
the event insert and the watch's delivered version leaves a pending watch
whose event already exists; the next pass writes the delivered version and
emits nothing.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from server.code.domain.coverage_watch import CoverageWatch
from server.code.domain.index.state import IndexState
from server.code.store import coverage_watches as repo
from server.core.datetimes import naive_utc_now
from server.tasks.app.inboxes import next_version, server_version

log = logging.getLogger(__name__)

ACTOR = "process:code_coverage_notify"


async def coverage_for(cfg: Any, commit: str, state: IndexState) -> dict[str, Any]:
    """`code.index_status`'s `commit_coverage` block for `commit`, judged
    against the heads in `state` (the namespace's `code_index_state` rows)."""
    from server.code.app.index.commit_coverage import commit_coverage
    from server.code.app.index.mirrors import history_dir
    from server.code.domain.index.compiler_tiers import compiler_tiers, state_value

    tiers = compiler_tiers(cfg.scip_producer, state, {})
    git_dir, gap = await history_dir(cfg)
    head = state_value(state, "git_head")
    return await commit_coverage(git_dir, commit, head, tiers, gap=gap)


def tier_summary(coverage: dict[str, Any]) -> list[dict[str, Any]]:
    """The tiers as the event carries them: which producer, at which watermark."""
    return [
        {
            "producer": str(t.get("producer") or ""),
            "label": str(t.get("label") or ""),
            "watermark": str(t.get("watermark") or ""),
        }
        for t in coverage.get("tiers") or []
    ]


@dataclass(frozen=True, slots=True)
class NotifyResult:
    """One delivery pass: the pending watches it read, how many it delivered,
    how many of those posted their event now, and how many name a namespace
    that is no longer configured."""

    pending: int
    delivered: int = 0
    emitted: int = 0
    unknown_namespace: int = 0


def new_watch(
    org: str, namespace: str, commit_sha: str, inbox: str, created_by: str
) -> CoverageWatch:
    return CoverageWatch(
        org=org,
        namespace=namespace,
        commit_sha=commit_sha,
        inbox=inbox,
        created_by=created_by,
        created_at=naive_utc_now(),
        delivered_at=None,
        is_deleted=0,
        version=next_version(),
    )


def next_watch_version(
    watch: CoverageWatch,
    *,
    delivered_at: Any = None,
    is_deleted: int | None = None,
) -> CoverageWatch:
    """The watch's next version: delivered at `delivered_at` and/or deleted."""
    return dataclasses.replace(
        watch,
        delivered_at=delivered_at if delivered_at is not None else watch.delivered_at,
        is_deleted=watch.is_deleted if is_deleted is None else is_deleted,
        version=next_version(),
    )


def _event_row(
    watch: CoverageWatch, task_id: str, tiers: list[dict[str, Any]], seq: int
) -> dict[str, Any]:
    """`seq` is from the server's clock (`server_version`), as every task event's:
    the task cursors order by it across replicas."""
    payload = {"namespace": watch.namespace, "commit": watch.commit_sha, "tiers": tiers}
    return {
        "org": watch.org,
        "inbox": watch.inbox,
        "namespace": watch.namespace,
        "task_id": task_id,
        "seq": seq,
        "event": repo.READY_EVENT,
        "from_status": "",
        "to_status": "",
        "actor": ACTOR,
        "agent": "",
        "from_subject": watch.created_by,
        "fence": 0,
        "version": seq,
        "reason": "",
        "patch": json.dumps(payload, sort_keys=True),
        "note": f"{watch.namespace}@{watch.commit_sha[:12]} is covered by every index tier",
        "ts": datetime.fromtimestamp(seq / 1_000_000, UTC).replace(tzinfo=None),
    }


async def deliver(watch: CoverageWatch, coverage: dict[str, Any]) -> tuple[bool, CoverageWatch]:
    """Post the ready event (unless it already exists) and write the watch's
    delivered version. Returns (emitted now, the delivered row)."""
    from server.tasks.app import wakeups as task_wakeups

    org, inbox = watch.org, watch.inbox
    task_id = repo.event_id(org, watch.namespace, watch.commit_sha, inbox)
    emitted = False
    if not await repo.ready_event_exists(org, inbox, task_id):
        seq = await server_version()
        await repo.insert_ready_event(_event_row(watch, task_id, tier_summary(coverage), seq))
        # The task id is the insert's dedup token, so when another delivery
        # posted first ClickHouse dropped ours: the stored seq is then not ours.
        emitted = await repo.ready_event_seq(org, inbox, task_id) == seq
        if emitted:
            task_wakeups.notify(
                task_wakeups.inbox_key(org, inbox),
                task_wakeups.poster_key(org, watch.created_by),
            )
    delivered = next_watch_version(watch, delivered_at=naive_utc_now())
    await repo.insert_watch_version(delivered)
    return emitted, delivered


async def notify_once(*, limit: int = 5000) -> NotifyResult:
    """One pass: every pending watch, grouped by namespace with one state
    read per namespace; the covered ones are delivered."""
    from server.code.domain.index.repos import repo_for_namespace
    from server.code.store.index import CodeRepo

    watches = await repo.pending_watches(limit=limit)
    if not watches:
        return NotifyResult(pending=0)
    delivered = emitted = unknown = 0
    groups: dict[tuple[str, str], list[CoverageWatch]] = {}
    for w in watches:
        groups.setdefault((w.org, w.namespace), []).append(w)
    states = await CodeRepo().all_state_many(list(groups))
    for (org, ns), rows in groups.items():
        cfg = repo_for_namespace(org, ns)
        if cfg is None:
            unknown += len(rows)
            continue
        state = states.get((org, ns)) or {}
        for w in rows:
            try:
                coverage = await coverage_for(cfg, w.commit_sha, state)
                if not coverage.get("ready"):
                    continue
                posted, _ = await deliver(w, coverage)
            except Exception as e:  # noqa: BLE001 — one watch must not stop the pass
                log.warning("code_coverage_notify: %s/%s@%s: %s", org, ns, w.commit_sha, e)
                continue
            delivered += 1
            emitted += int(posted)
    return NotifyResult(
        pending=len(watches), delivered=delivered, emitted=emitted, unknown_namespace=unknown
    )
