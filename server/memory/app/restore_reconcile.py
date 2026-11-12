"""The memory plane's part of `loom reconcile`: after a restore, what the restored store
lost is made whole from the other, and nothing forgotten after the restore point comes
back (docs/operations/backup-and-recovery.md).

After a ClickHouse restore Postgres holds every revision. Each one committed after the
point is owed again (`hold`, held parked until the erasures ran, so none of it lands
before the erasures it predates are run again), then published (`publish_held`); each
redaction made after the point is run again (`replay_redaction`), which scrubs what
ClickHouse restored and the caches and precedents quoting it. After a Postgres restore
each redaction ClickHouse records after the point is run again on the restored copy.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from server.memory.store import pg_memory
from server.memory.store import redaction as redaction_store

if TYPE_CHECKING:
    from server.erasure.app.replay import Restored

log = logging.getLogger(__name__)


async def hold(after: datetime) -> int:
    """Owe again every memory revision committed after `after`, held parked; returns how
    many it marked."""
    return await pg_memory.hold_for_republish(after)


async def hold_heads_behind(ch: Any, *, page: int = 2000) -> int:
    """After a ClickHouse restore, owe again every Postgres head ClickHouse holds at an
    older version, or not at all: a revision committed before the point and published
    after it (a backlog, a ClickHouse outage) is in neither the restored copy nor `hold`'s
    window. Every head is compared, a page at a time; returns how many it held."""
    from server.memory.store.rows import identity_key

    held = 0
    after: tuple[str, ...] | None = None
    while True:
        heads = await pg_memory.head_page(after, page)
        if not heads:
            return held
        keys = [identity_key(h) for h in heads]
        rows = await ch.query_all(
            "SELECT org, namespace, agent, user_id, session_id, memory_key, max(version) AS v "
            "FROM loom.memory WHERE (org, namespace, agent, user_id, session_id, memory_key) "
            "IN {keys:Array(Tuple(String, String, String, String, String, String))} "
            "GROUP BY org, namespace, agent, user_id, session_id, memory_key",
            params={"keys": keys},
        )
        published = {identity_key(r): int(r["v"]) for r in rows}
        behind = [
            h for h, k in zip(heads, keys, strict=True) if published.get(k, -1) < int(h["version"])
        ]
        if behind:
            n = await pg_memory.hold(
                [(h["org"], h["namespace"], int(h["seq"])) for h in behind],
                [pg_memory.republish_targets(h, is_head=True) for h in behind],
            )
            if n < len(behind):
                log.warning(
                    "loom reconcile: heads ClickHouse lacks or holds older that cannot be sent "
                    "again (the revision erased, or superseded with its payload cleared): %d",
                    len(behind) - n,
                )
            held += n
        after = keys[-1]


async def publish_held(*, batch: int = 200, passes: int = 1000) -> int:
    """Release what the holds held and publish every memory delivery due, as the delivery
    worker would (none runs during a reconcile); returns the revisions published. Stops
    when a pass claims nothing, or after `passes`: a delivery ClickHouse keeps refusing
    parks after its attempts and is reported by health."""
    from server.memory.app.pg_delivery import publish_claims

    await pg_memory.release_held()
    published = 0
    for _ in range(passes):
        claims = await pg_memory.claim_due(batch)
        if not claims:
            return published
        published += (await publish_claims(claims, worker=False))[0]
    log.warning("loom reconcile: memory deliveries still due after %d passes", passes)
    return published


async def redactions_lost_by(restored: Restored, after: datetime) -> list[dict[str, Any]]:
    """The redactions a restore of `restored` to `after` may have lost, from the other
    store: (org, memory_id, version, redaction_reason), oldest first."""
    if restored == "clickhouse":
        return await pg_memory.redacted_since(after)
    return await redaction_store.redacted_since(after)


async def replay_redaction(entry: Mapping[str, Any], restored: Restored) -> str:
    """The redaction of the version run again with the reason and actor it was made with
    (stored as `reason|by`), past the op's gates (no lock refuses it: it was allowed when
    made): "redacted", "gone" when the version no longer exists, or "covered" for a
    redacted head the replay of its named version scrubs. After a Postgres restore the
    version may be the restored copy's live head again (the write that replaced it was
    lost): it is scrubbed in place, so its text is not served."""
    import server.memory.store as memory_repo
    from server.core.datetimes import naive_utc_now
    from server.memory.app.curation import redact_version

    org, memory_id, version = entry["org"], str(entry["memory_id"]), int(entry["version"])
    blob = str(entry.get("redaction_reason") or "")
    reason, _, by = blob.rpartition("|")
    target = await memory_repo.find_history_target(org, memory_id, version)
    if target is None:
        return "gone"
    out = await redact_version(
        org, target, version, memory_id=memory_id, reason=reason, redacted_by=by, gated=False
    )
    if out.redacted:
        return "redacted"
    if restored != "postgres":
        # Postgres holds the version as a redacted head (a profile key built from the
        # memory, which the redaction reached): the replay of the version it named
        # scrubs ClickHouse's copies of it.
        return "covered"
    key = (
        org,
        target.namespace,
        target.agent,
        target.user_id,
        target.session_id,
        target.memory_key,
    )
    await memory_repo.redact_versions(
        key, memory_id, version, when=naive_utc_now(), reason_blob=blob, head=(memory_id, version)
    )
    return "redacted"
