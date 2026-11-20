"""A repeated code.search is a point read, not two namespace scans.

Most code.search calls repeat an earlier query text, many of them within
minutes and with an unchanged result set; without a memo each one re-runs
the vector leg, the lexical scan and the risk annotation.

This is the fleet tier for the served response: the shape of the query-embedding cache
applied one level up.

  key      sha256 over every request field that shapes the result plus the
           embedding model and the effective reranker — never over fields
           that are attribution only (agent, consumer). `user_id` enters
           only through the recent-directory prior it selects.
  get      one point read under the identity prefix, bounded by the
           read-side TTL; fail-open to a computed search.
  put      fire-and-forget async insert after a computed search; a lost
           write costs one recomputation later.
  replay   the stored JSON becomes a fresh CodeSearchOutput with the route
           suffixed `+memo`, a new trace run id, the CURRENT `as_of`, and
           two disclosure fields inside it: `memo_commit` (the index commit
           the result was computed at) and `memo_age_s`.

The payload is the handler's own response (bodies included), scoped by
(org, namespace) in the table key, so a hit serves exactly what the same
caller would have been served up to `ttl` seconds earlier. A namespace
that re-indexes inside the TTL is disclosed, not refused (`memo_stale`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from server.code.api.models_search import CodeSearchInput
from server.core.background import TaskSet
from server.core.datetimes import naive_utc_now
from server.core.llm_free import reads_llm_free

logger = logging.getLogger("loom.code_search_memo")

MEMO_VERSION = 2  # bump when the served shape changes incompatibly
hits = 0
misses = 0
errors = 0
_TASKS = TaskSet()

# Request fields that shape the result. Anything not listed is attribution
# (agent, consumer) or identity (org, namespace: part of the table key).
_SHAPING_FIELDS = (
    "query",
    "top_k",
    "path_prefix",
    "min_fan_in",
    "sort_by",
    "expand",
    "annotate",
    "bodies",
    # constrained search: the predicate shapes the pool
    "pattern",
    "regex",
    "case_sensitive",
    "path_globs",
    "exclude_globs",
    "langs",
    "kind_prior",
    "legs",
    "pr_leg_before",
    # second vector model: another pool, another key
    "vector_model",
    "vector_shortlist",
)


def enabled(inp: CodeSearchInput) -> bool:
    try:
        from server.settings import settings

        # a track_path diagnostic must compute: it reports this call's pools
        return (
            bool(settings.code_search_memo_enabled)
            and not bool(getattr(inp, "fresh", False))
            and not getattr(inp, "track_path", "")
        )
    except Exception:  # noqa: BLE001 — settings unavailable = tier off
        return False


def _ranking_fingerprint() -> str:
    """The server build and every `code_search_*` setting: another build or
    another ranking configuration on the same tables (a second colour, a
    canary) must compute its own answer, not replay one ranked differently."""
    try:
        from server.settings import settings

        knobs = {k: v for k, v in settings.model_dump().items() if k.startswith("code_search_")}
        knobs["build_commit"] = settings.build_commit
    except Exception:  # noqa: BLE001 — settings unavailable = an unshared key
        return "unknown"
    return hashlib.sha256(json.dumps(knobs, sort_keys=True, default=str).encode()).hexdigest()[:16]


def key_for(
    inp: CodeSearchInput,
    *,
    embedding_model: str,
    rerank: str,
    token_budget: int,
    recent_dirs: list[str],
) -> str:
    parts: dict[str, Any] = {f: getattr(inp, f, None) for f in _SHAPING_FIELDS}
    parts["query"] = " ".join(str(parts["query"]).split())
    # `llm_legs="none"` is another pool; auto / unset keep the key
    if getattr(inp, "llm_legs", None) == "none":
        parts["llm_legs"] = "none"
    # An LLM-free read also drops query expansion and the default rerank: its own pool
    if reads_llm_free():
        parts["llm_free"] = True
    parts.update(
        v=MEMO_VERSION,
        ranking=_ranking_fingerprint(),
        embedding_model=embedding_model,
        rerank=rerank,
        token_budget=int(token_budget),
        recent_dirs=sorted(recent_dirs),
    )
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


async def get(org: str, ns: str, key: str) -> dict[str, Any] | None:
    """The newest memo row for this key inside the read-side TTL, or None."""
    global hits, misses, errors
    try:
        from server.code.store.index import CodeRepo
        from server.settings import settings

        row = await CodeRepo().search_memo_row(
            org, ns, key, ttl_s=int(settings.code_search_memo_ttl_s)
        )
    except Exception:  # noqa: BLE001 — fail open to a computed search
        errors += 1
        return None
    if not row or not row.get("payload"):
        misses += 1
        return None
    hits += 1
    return row


def put(org: str, ns: str, key: str, output: Any, *, indexed_commit: str) -> None:
    """Fire-and-forget insert of a served response; never awaited by the read."""
    from server.settings import settings

    cap = int(settings.code_search_memo_max_payload_bytes)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    # lessons are the caller's, read after the memo (`result_lessons`): never stored
    payload = output.model_dump_json(
        exclude={"trace_run_id", "as_of", "lessons", "lessons_left_out"}
    )
    if cap and len(payload) > cap:
        return
    row = {
        "org": org,
        "namespace": ns,
        "key": key,
        "indexed_commit": (indexed_commit or "")[:40],
        "route": str(getattr(output, "route", "") or ""),
        "payload": payload,
        "created_at": naive_utc_now(),
    }

    async def _bg() -> None:
        global errors
        try:
            from server.code.store.index import CodeRepo

            await CodeRepo().insert_search_memo_row(row)
        except Exception:  # noqa: BLE001 — a lost memo costs one recomputation later
            errors += 1

    # A write for this request: the task keeps the request's query tag
    # (`create_task` copies the context), so the row is attributed to it.
    _TASKS.spawn(_bg(), name="code-search-memo-put")


def replay(
    row: dict[str, Any], output_cls: Any, *, as_of: dict[str, Any], trace_run_id: str
) -> Any:
    """Rebuild the served response from a memo row. The route is suffixed
    `+memo` so traces count hits; `as_of` is the CURRENT freshness block
    with the memo's own commit and age added for disclosure."""
    out = output_cls.model_validate_json(str(row["payload"]))
    out.route = f"{out.route}+memo" if not str(out.route).endswith("+memo") else out.route
    out.trace_run_id = trace_run_id
    stored = row.get("indexed_commit") or b""
    stored_commit = (
        stored.decode("ascii", "ignore") if isinstance(stored, bytes) else str(stored)
    ).strip("\x00")
    created = row.get("created_at")
    age = 0
    if isinstance(created, datetime):
        now = naive_utc_now()
        age = max(0, int((now - created.replace(tzinfo=None)).total_seconds()))
    current = dict(as_of or {})
    current["memo_commit"] = stored_commit
    current["memo_age_s"] = age
    current["memo_stale"] = bool(
        stored_commit and current.get("commit") and stored_commit != current.get("commit")
    )
    out.as_of = current
    # the stored stage wall describes the computation that was memoised, not this
    # replay: the handler stamps this call's own
    work = {k: v for k, v in (out.retrieval_work or {}).items() if not k.startswith("stage_ms_")}
    work["memo_hit"] = 1
    out.retrieval_work = work
    return out


def stats() -> dict[str, int]:
    return {"hits": hits, "misses": misses, "errors": errors}
