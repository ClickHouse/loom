"""loom_search's profile leg.

A profile section is always-loaded prompt context, so it stays out of
unfiltered memory.search (``NON_SEARCHABLE_TYPES``). When a question is ABOUT
the user ("what do I like to drink?") the section that answers it is still the
right hit, so the leg finds it by meaning as well as by words:

1. **Locate** the namespaces' profile keys without FINAL. That reads the small
   sort-key columns and lets the ``memory_type`` skip index prune, where a
   ``SELECT * FROM loom.memory FINAL`` would read every column of every row in
   the searched namespaces.
2. **Fetch** the live rows of those keys alone, under FINAL.
3. **Score by cosine** against each section's current vector. The query
   vector is the one the other legs embed at the same moment: ``cached_embed``
   runs one provider call per text in flight. The gate is ``semantic_pass``:
   a hard floor, or a soft floor for a question about the user when the
   section beats its namespace's runner-up by a margin.
4. **Fall back to words** (``profile_hit_matches``) only for a
   section with no current vector: the word rule matches unrelated questions
   on common words ("should", "prefer") and misses most paraphrases.

memory.set gives a profile row no inline embedding. The embedding worker's
reconcile embeds a section once, from its FIRST value. A section's vector
embeds ``profile_embed_text`` (label + current value); a vector whose excerpt
is anything else is stale: it scores nothing, and a re-embed stub is queued off
the read path (the worker's drain fills it). Every profile write queues the same stub
(``schedule_reembed_after_write``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from server.identity.domain.read_scope import ReadFilters, scope_and

log = logging.getLogger(__name__)

Ident = tuple[str, str, str, str, str]  # namespace, agent, user_id, session_id, memory_key

# Step 1: no FINAL, sort-key columns only. A tombstoned or superseded key can
# appear here; step 2 drops it.
PROFILE_LOCATE_SQL = """
SELECT DISTINCT namespace, agent, user_id, session_id, memory_key
FROM loom.memory
PREWHERE org = {org:String} AND namespace IN {ns:Array(String)} AND memory_type = 'profile'
  {read_scope}
LIMIT {lim:UInt32}
"""

# Step 2: the live rows of exactly those keys. The key tuple is the sort key,
# so PREWHERE is safe under FINAL: every version of a key
# shares it. memory_type and tombstoned_at are not key columns, so they stay in
# WHERE, after the merge.
PROFILE_FETCH_SQL = """
SELECT namespace, agent, user_id, session_id, memory_key, value, created_at,
       cityHash64(concat(memory_key, '\\n', value)) AS text_hash
FROM loom.memory FINAL
PREWHERE org = {org:String}
  AND (namespace, agent, user_id, session_id, memory_key)
      IN {keys:Array(Tuple(String, String, String, String, String))}
WHERE memory_type = 'profile' AND tombstoned_at IS NULL
  -- a 'high' section is never embedded or ranked: no stub, no vector, no hit
  AND sensitivity != 'high'
"""

# Step 4: similarity against the stored vectors, and the hash of the text each
# vector embeds (the staleness check).
PROFILE_VECTORS_SQL = """
SELECT namespace, agent, user_id, session_id, memory_key, scope,
       cityHash64(content_excerpt) AS excerpt_hash,
       embedding_dim,
       if(embedding_dim = {dim:UInt32},
          1 - cosineDistance(embedding, {qv:Array(Float32)}), 0) AS similarity
FROM loom.memory_embeddings FINAL
PREWHERE org = {org:String}
  AND (namespace, agent, user_id, session_id, memory_key)
      IN {keys:Array(Tuple(String, String, String, String, String))}
  AND embedding_model = {model:String}
WHERE tombstoned_at IS NULL
"""


def _ident(row: dict[str, Any]) -> Ident:
    return (
        str(row["namespace"]),
        str(row["agent"]),
        str(row["user_id"]),
        str(row["session_id"]),
        str(row["memory_key"]),
    )


def _clean(value: Any) -> str:
    return str(value or "").strip('"')  # values are JSON-canonicalized


_SELF = re.compile(r"\b(i|i'm|i've|my|me|mine|myself)\b", re.IGNORECASE)


def profile_embed_text(label: str, value_json: str) -> str:
    """What a profile section's vector embeds: its label, then its stored
    (canonical JSON) value. The label names the topic ("drinks", "schedule")
    the value's prose often never says; with it, recall on the calibration
    set rose from 0.52 to 0.70 at the same floor."""
    return f"{label}\n{value_json}"


def semantic_pass(
    query: str, sim: float, runner_up: float | None, *, hard: float, soft: float, margin: float
) -> bool:
    """The gate a section the words missed must clear. The
    soft tier needs a question about the user AND a runner-up to beat: a
    namespace with one section has no contrast, so only the hard floor
    admits it there."""
    if hard <= 0:
        return False
    if sim >= hard:
        return True
    if sim < soft or runner_up is None or not _SELF.search(query or ""):
        return False
    return sim - runner_up >= margin


def rank_profile_hits(
    query: str,
    rows: list[dict[str, Any]],
    sims: dict[Ident, float],
    *,
    hard: float,
    soft: float,
    margin: float,
    top_k: int,
    lexical: Callable[[str, str, str], bool],
) -> list[dict[str, Any]]:
    """A section with a current vector (in `sims`) passes on the semantic gate
    alone; one without passes on the word rule. Semantic hits rank by
    similarity, then the word matches in row order. The runner-up is the best
    OTHER scored section of the same namespace. Pure."""
    by_ns: dict[str, list[float]] = {}
    for ident, score in sims.items():
        by_ns.setdefault(ident[0], []).append(score)
    lex: list[dict[str, Any]] = []
    sem: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        label = str(row.get("memory_key") or "")
        value = _clean(row.get("value"))
        hit = {
            "label": label,
            "value": value,
            "namespace": row.get("namespace"),
            "agent": row.get("agent"),
        }
        sim = sims.get(_ident(row))
        if sim is None:
            if lexical(query, label, value):
                lex.append({**hit, "match": "lexical"})
            continue
        peers = sorted(by_ns.get(str(row.get("namespace")), []), reverse=True)
        peers.remove(sim)
        runner_up = peers[0] if peers else None
        if semantic_pass(query, sim, runner_up, hard=hard, soft=soft, margin=margin):
            sem.append((sim, {**hit, "match": "semantic", "similarity": round(sim, 3)}))
    sem.sort(key=lambda t: -t[0])
    return ([h for _, h in sem] + lex)[:top_k]


async def search_profile(
    org: str,
    namespaces: list[str],
    query: str,
    *,
    top_k: int,
    lexical: Callable[[str, str, str], bool],
    scope: ReadFilters,
    ch: Any = None,
    embedder: Any = None,
) -> list[dict[str, Any]]:
    """The profile leg's hits for `query` over `namespaces`, inside the read
    `scope` (the located keys bound every later statement)."""
    from server.settings import settings

    if ch is None:
        from server.core.ch import get_client

        ch = get_client()
    from server.core.ch import own_write_reads

    # The section a call ago appended to is read, not missed
    # (profile rows are a handful per namespace; the consistent read is ~10 ms
    # and runs beside the search's vector legs).
    with own_write_reads():
        params: dict[str, Any] = {"org": org, "ns": namespaces, "lim": 200}
        locate_sql = PROFILE_LOCATE_SQL.replace("{read_scope}", scope_and(scope, params))
        located = await ch.query_all(locate_sql, params=params)
        if not located:
            return []  # the common case: no profile, no fetch, no embed
        keys = [_ident(r) for r in located]
        rows = list(await ch.query_all(PROFILE_FETCH_SQL, params={"org": org, "keys": keys}) or [])
    if not rows:
        return []
    hard = float(settings.mcp_loom_search_profile_min_similarity)
    soft = float(settings.mcp_loom_search_profile_soft_similarity)
    margin = float(settings.mcp_loom_search_profile_margin)
    sims: dict[Ident, float] = {}
    if hard > 0:
        sims = await _similarities(ch, org, rows, query, embedder=embedder)
    return rank_profile_hits(
        query, rows, sims, hard=hard, soft=soft, margin=margin, top_k=top_k, lexical=lexical
    )


async def _similarities(
    ch: Any, org: str, rows: list[dict[str, Any]], query: str, *, embedder: Any = None
) -> dict[Ident, float]:
    """Cosine of the query against each row's CURRENT vector. A stale or
    missing vector scores nothing and is queued for a re-embed."""
    if embedder is None:
        from server.embedding import get_embedder

        embedder = get_embedder()
    try:
        qv = await embedder.embed(query)
    except Exception as e:  # noqa: BLE001 — the leg degrades to lexical, never fails the search
        log.debug("profile leg: query embed failed: %s", e)
        return {}
    by_ident = {_ident(r): r for r in rows}
    vecs = await ch.query_all(
        PROFILE_VECTORS_SQL,
        params={
            "org": org,
            "keys": list(by_ident),
            "model": embedder.model_name,
            "dim": len(qv),
            "qv": [float(x) for x in qv],
        },
    )
    sims: dict[Ident, float] = {}
    settled: set[Ident] = set()  # fresh, or a re-embed already queued
    scope: dict[Ident, str] = {}
    for v in vecs or []:
        ident = _ident(v)
        row = by_ident.get(ident)
        if row is None:
            continue
        scope[ident] = str(v.get("scope") or "private")
        current = int(v.get("excerpt_hash") or 0) == int(row.get("text_hash") or -1)
        dim = int(v.get("embedding_dim") or 0)
        if current and dim == 0:
            settled.add(ident)  # the stub for this value waits on the worker
        elif current and dim == len(qv):
            settled.add(ident)
            sims[ident] = float(v.get("similarity") or 0.0)
    stale = [
        {**r, "scope": scope.get(i, "private")} for i, r in by_ident.items() if i not in settled
    ]
    if stale:
        _queue_detached(org, stale, embedder)
    sims.update(await _score_unvectored(by_ident, sims, qv, embedder))
    return sims


# The most sections one search embeds on the fly.
_ON_THE_FLY_CAP = 8


async def _score_unvectored(
    by_ident: dict[Ident, dict[str, Any]],
    sims: dict[Ident, float],
    qv: list[float],
    embedder: Any,
) -> dict[Ident, float]:
    """Cosine of the query against the CURRENT text of each section that has
    no current vector — just written, or its stub waiting on the worker.
    The re-embed is queued detached, so without this a section appended to a
    second ago would score nothing and fall back to the word rule ('likes tea'
    never matches 'what do I like to drink?'). One batched embed of those sections' own
    text (the embed cache serves repeats), bounded; the heal still runs."""
    todo = [i for i in by_ident if i not in sims][:_ON_THE_FLY_CAP]
    if not todo:
        return {}
    texts = [
        profile_embed_text(
            str(by_ident[i].get("memory_key") or ""), str(by_ident[i].get("value") or "")
        )
        for i in todo
    ]
    try:
        vecs = await embedder.embed_many(texts)
    except Exception as e:  # noqa: BLE001 — the word rule still answers
        log.debug("profile leg: on-the-fly embed failed: %s", e)
        return {}
    out: dict[Ident, float] = {}
    for ident, v in zip(todo, vecs, strict=False):
        if v is not None and len(v) == len(qv):
            out[ident] = _cosine(qv, v)
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return float(dot / (na * nb)) if na and nb else 0.0


def _queue_detached(org: str, rows: list[dict[str, Any]], embedder: Any) -> None:
    try:
        from server.core.background import detached_task, register_deferred_task

        register_deferred_task(
            detached_task(
                queue_profile_reembed(org, rows, embedder=embedder), name="loom-profile-reembed"
            )
        )
    except Exception as e:  # noqa: BLE001 — a missed heal costs one more stale read
        log.debug("profile leg: re-embed not scheduled: %s", e)


async def queue_profile_reembed(
    org: str, rows: list[dict[str, Any]], *, embedder: Any = None, ch: Any = None
) -> int:
    """Write a re-embed stub (``embedding_dim = 0``) for each profile row, from
    its current value. The embedding worker's drain embeds it. The stub carries
    the memory row's ``created_at`` and the model the worker embeds with, so it
    lands in the partition and on the sort key of the vector it replaces (RMT
    collapses only there). Returns the stubs written; never raises."""
    if not rows:
        return 0
    try:
        import server.memory.store as memory_repo

        if embedder is None:
            from server.embedding import get_embedder

            embedder = get_embedder()
        stubs = [
            memory_repo.build_embedding_row(
                org=org,
                namespace=str(r["namespace"]),
                agent=str(r["agent"]),
                user_id=str(r["user_id"]),
                session_id=str(r["session_id"]),
                memory_key=str(r["memory_key"]),
                memory_type="profile",
                scope=str(r.get("scope") or "private"),
                content_excerpt=profile_embed_text(str(r["memory_key"]), str(r.get("value") or "")),
                embedding_model=embedder.model_name,
                embedding_dim=0,
                embedding=[],  # the insert pads it to the column's width
                created_at=r.get("created_at"),
                content_sha256=str(r.get("content_sha256") or ""),
            )
            for r in rows
        ]
        await memory_repo.insert_embedding_rows(stubs, ch=ch)
        return len(stubs)
    except Exception as e:  # noqa: BLE001 — the read-side check heals it later
        log.warning("profile re-embed stub write failed: %s", e)
        return 0


# The scope of the vector a key already has (the stub must replace it on the
# same sort key); a key with none gets the reconcile's default, 'private'.
PROFILE_SCOPE_SQL = """
SELECT scope
FROM loom.memory_embeddings FINAL
PREWHERE org = {org:String}
  AND (namespace, agent, user_id, session_id, memory_key)
      IN {keys:Array(Tuple(String, String, String, String, String))}
WHERE tombstoned_at IS NULL
LIMIT 1
"""


async def reembed_after_write(org: str, ident: Ident, *, ch: Any = None) -> int:
    """Queue the re-embed stub of one just-written section from what is
    STORED (the canonical value the staleness check hashes), read with
    sequential consistency so the write is visible on any replica."""
    try:
        from server.core.ch import sequential_reads

        if ch is None:
            from server.core.ch import get_client

            ch = get_client()
        with sequential_reads():
            rows = await ch.query_all(PROFILE_FETCH_SQL, params={"org": org, "keys": [ident]})
            if not rows:
                return 0
            scoped = await ch.query_all(PROFILE_SCOPE_SQL, params={"org": org, "keys": [ident]})
        scope = str(scoped[0].get("scope") or "private") if scoped else "private"
        return await queue_profile_reembed(org, [{**rows[0], "scope": scope}], ch=ch)
    except Exception as e:  # noqa: BLE001 — the read-side check heals it later
        log.warning("profile re-embed after write failed: %s", e)
        return 0


def schedule_reembed_after_write(org: str, ident: Ident) -> None:
    """`reembed_after_write`, detached: the profile write never waits on it."""
    try:
        from server.core.background import detached_task, register_deferred_task

        register_deferred_task(
            detached_task(reembed_after_write(org, ident), name="loom-profile-reembed-write")
        )
    except Exception as e:  # noqa: BLE001 — a missed stub costs one stale read, then heals
        log.debug("profile re-embed not scheduled: %s", e)
