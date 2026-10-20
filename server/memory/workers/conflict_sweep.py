"""Re-judge deferred conflict checks lost with their process.

`memory.set` / `memory.update_by_id` write a deferred row's embedding with
the pending mark (`store.conflict_marks`) and the settle stamps it. A mark
older than `GRACE_SECONDS` is a judge that died with its replica (SIGKILL,
OOM, a drain that ran out): this pass runs the write's conflict check again
on the stored value and vector and lands the verdict the way the settle
does. Rows past `LOOKBACK_SECONDS` stay as written (the fail-open the judge
always had). Leader-only like every worker.

The writer's read scope is not stored, so the re-check reads as narrowly as
the row's identity allows: a row with a `session_id` sees only that session's
peers, one with only a `user_id` that user's. A narrower check finds fewer
verdicts, never one the writer could not have reached.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from server.core.repo_base import _CHClient
from server.workers.status import log_pass_errors, record_pass

log = logging.getLogger(__name__)

INTERVAL_SECONDS = 30.0
MAX_ROWS_PER_PASS = 50


def _key(r: Any) -> tuple[str, str, str, str, str, str]:
    return (
        str(r["org"]),
        str(r["namespace"]),
        str(r["agent"]),
        str(r["user_id"]),
        str(r["session_id"]),
        str(r["memory_key"]),
    )


def _known(value: str) -> str | None:
    return value if value and value != "-" else None


def _scope_for(user_id: str, session_id: str) -> Any:
    from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters

    uid, sid = _known(user_id), _known(session_id)
    if sid is not None:
        return ReadFilters(scope="own_session", user_id=uid, session_id=sid)
    if uid is not None:
        return ReadFilters(scope="own_user", user_id=uid, session_id=None)
    return WHOLE_NAMESPACE


async def rejudge(r: dict[str, Any], *, ch: _CHClient) -> str:
    """Run the check again for one marked embedding row and land its verdict;
    the verdict, `gone` when the memory row is no longer live and active, or
    `moved` when the memory row is a newer write than the vector (before the
    judge, or by the time its verdict would land)."""
    import server.memory.store as memory_repo
    from server.auth.context import current_auth
    from server.identity.domain.read_scope import read_filters_bound
    from server.memory.app import conflict_deferred as cd
    from server.memory.app import conflicts as cf

    key = _key(r)
    version, created_at = int(r["version"]), r["created_at"]
    sha = str(r.get("content_sha256") or "")
    mem = (await memory_repo.memory_rows_by_identity_keys([key], ch=ch, sequential=True)).get(key)
    ident = SimpleNamespace(
        org=key[0],
        namespace=key[1],
        agent=key[2],
        user_id=key[3],
        session_id=key[4],
        memory_key=key[5],
        memory_type=str(r.get("memory_type") or ""),
        run_id=str(getattr(mem, "run_id", "-") or "-"),
    )
    if mem is None or str(mem.status or "") != "active":
        await cd.stamp_checked(ident, version=version, created_at=created_at, content_sha256=sha)
        return "gone"
    if sha and str(mem.content_sha256 or "") != sha:
        # A newer write's vector replaces this row, with its own mark or verdict.
        return cd.MOVED
    with read_filters_bound(current_auth(), _scope_for(key[3], key[4])):
        result = await cf.check_on_commit(
            org=key[0],
            namespace=key[1],
            agent=key[2],
            memory_type=str(mem.memory_type),
            memory_key=key[5],
            new_value=memory_repo.stored_value_text(mem),
            new_embedding=[float(x) for x in (r.get("embedding") or [])],
            new_run_id=ident.run_id,
            new_valid_at=getattr(mem, "valid_at", None),
            defer_judge=True,
        )
        if result.verdict == cd.PENDING and result.pending_judge is not None:
            # settle stamps the row; an `error` leaves the mark for the next pass.
            return await cd.settle(
                ident,
                result.pending_judge,
                memory_id=str(mem.memory_id),
                version=version,
                created_at=created_at,
                content_sha256=sha,
            )
        if not await cd.still_current(ident, sha):
            return cd.MOVED
        verdict = await cd.apply(ident, result, memory_id=str(mem.memory_id), content_sha256=sha)
        if verdict == cd.MOVED:
            return verdict
    await cd.stamp_checked(ident, version=version, created_at=created_at, content_sha256=sha)
    return verdict or "clear"


async def run_pass(ch: _CHClient | None = None) -> dict[str, Any]:
    from server.core.repo_base import _default_client
    from server.llm import customer
    from server.memory.store import conflict_marks

    ch = ch or _default_client()
    with record_pass("conflict_sweep") as rec:
        # A switched-off org's marks are not re-judged while it is off. One older
        # than the lookback when it comes back is past the sweep, as a judge lost
        # in an outage is: the write stands as made, unjudged.
        rows = await conflict_marks.stale_pending_marks(
            grace_s=conflict_marks.GRACE_SECONDS,
            lookback_s=conflict_marks.LOOKBACK_SECONDS,
            limit=MAX_ROWS_PER_PASS,
            exclude_orgs=await customer.switched_off_orgs(),
            ch=ch,
        )
        verdicts: dict[str, int] = {}
        errors: list[str] = []
        for r in rows:
            try:
                v = await rejudge(r, ch=ch)
            except customer.HOLDS:
                continue  # a held call is no failure: the mark is re-judged once the org is back
            except Exception as e:  # noqa: BLE001 — one row's failure never stops the pass
                errors.append(f"{r.get('namespace')}/{r.get('memory_key')}: {e}")
                continue
            verdicts[v] = verdicts.get(v, 0) + 1
        outcome = {"marked": len(rows), "verdicts": verdicts, "errors": len(errors)}
        rec.outcome = outcome
        if rows:
            log.info("conflict_sweep: %d lost judges re-run %s", len(rows), verdicts)
        log_pass_errors(log, "conflict_sweep", errors)
    return outcome


__all__ = ["INTERVAL_SECONDS", "MAX_ROWS_PER_PASS", "rejudge", "run_pass"]
