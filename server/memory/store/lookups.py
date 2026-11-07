"""Targeted finders behind memory.ask's injections: exact identifiers over
facts and chunks, session dates, key facets, key prefixes and content terms.
"""

from __future__ import annotations

import re
from typing import Any

from server.core.repo_base import _CHClient, _default_client

# --- The cosine-ordered fact read reaches the vector through a keyed re-read ---
#
# The identifier reads order their matches by cosine to the question, and
# the `position()` OR-list that selects them is not a sorting-key predicate,
# so as one plain FINAL statement ClickHouse never moves it ahead of the 6 KB
# `embedding` column: every granule of the namespace is materialised before
# the filter runs (on a large tenant, 384 MB per fact read for eight rows).
#
# Facts: an identity subquery (no FINAL, text and sorting-key columns only)
# feeds a keyed FINAL re-read with the same predicate, the cosine order and
# the LIMIT; `optimize_move_to_prewhere_if_final = 1` moves the identity IN
# (sorting-key columns only) ahead of the vector (379 -> 14 MB per read). Exact
# by construction: the subquery sees every version, so an identity whose
# current version matches is always in it; the outer read re-applies the
# predicate under FINAL, so a superseded version cannot resurrect and a stale
# match drops. The length and recency orders never read `embedding` and stay
# one plain statement.
# Chunks: the read has no FINAL, and without FINAL the planner already moves
# the whole predicate, position() included, ahead of the vector; its bytes
# are the vector of the granules that hold a match (a hand-written PREWHERE
# reads identical bytes in the same time).


def _fact_identifier_one_trip_sql(where_sql: str, order: str) -> str:
    """The cosine-ordered fact read: the identity pass is the subquery of the
    keyed FINAL re-read (see the note at the top of this module). The
    restriction names the whole sorting key in key order (a partial tuple
    breaks the prefix and the primary index cannot prune); skip indexes are
    off because this is a primary-key read."""
    return f"""
        SELECT agent, user_id, session_id, memory_key, memory_type, content_excerpt,
               toString(created_at) AS created_at,
               toString(valid_at) AS valid_at, version
        FROM loom.memory_embeddings FINAL
        WHERE {where_sql}
          AND (org, namespace, agent, user_id, session_id, scope, embedding_model, memory_key)
              IN (
                  SELECT DISTINCT org, namespace, agent, user_id, session_id,
                         scope, embedding_model, memory_key
                  FROM loom.memory_embeddings
                  WHERE {where_sql}
              )
        ORDER BY {order}
        LIMIT {{lim:UInt32}}
        SETTINGS use_skip_indexes = 0, optimize_move_to_prewhere_if_final = 1
        """


async def find_by_identifier(
    *,
    org: str,
    namespace: str,
    identifiers: list[str],
    limit: int = 6,
    query_embedding: list[float] | None = None,
    scope: str = "private",
    agent: str = "*",
    user_id: str | None = None,
    ch: _CHClient | None = None,
    case_insensitive: bool = False,
    order: str = "cosine",
) -> list[dict[str, Any]]:
    """Exact-substring lookup for memories citing a salient identifier.

    Loom's lexical/BM25 leg only indexes alpha tokens >=3 chars, so bare
    PR/issue numbers ("109741") are invisible to both lexical AND (often)
    semantic recall — a "what do we know about PR 109741" query then misses
    the very memories that name it. This backs memory.ask's identifier
    injection: a guaranteed exact-substring match over active heads in the
    namespace, so those memories reach the synthesis window.

    Ordering: when `query_embedding` is given, by cosine RELEVANCE to the query
    — a heavily-referenced entity (e.g. a reviewer with 160 memories) has many
    facts, and the ones useful for THIS query are the on-topic ones, not the
    longest; length-only ordering injects long-but-irrelevant facts that dilute
    the synthesis pool. Falls back to content length (most substantive first),
    NOT recency, when no query vector is supplied — every row already cites the
    identifier, so length beats the one-line status pings a newest-first order
    surfaces.
    """
    _order_mode = str(order or "cosine")
    ch = ch or _default_client()
    # Strip a leading "@" from @handles: the substring match must land on every
    # mention form (bare "octocat", glued "@octocat", space-broken "@ octocat"
    # all contain the bare run). Bare numeric idents are unaffected.
    ids = [str(i).strip().lstrip("@") for i in (identifiers or []) if str(i).strip().lstrip("@")][
        :8
    ]
    if not ids:
        return []
    # A long number is matched on digits alone: the question writes
    # 20202201042, the memory "202-022-01042" (otherwise the person behind a
    # phone number is never found).
    fn = "positionCaseInsensitive" if case_insensitive else "position"
    conds = " OR ".join(
        (
            f"position(replaceRegexpAll(content_excerpt, '[^0-9]', ''), {{id{i}:String}}) > 0"
            if v.isdigit() and len(v) >= 7
            else f"{fn}(content_excerpt, {{id{i}:String}}) > 0"
        )
        for i, v in enumerate(ids)
    )
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "lim": int(limit),
        "scope": scope,
    }
    for i, v in enumerate(ids):
        params[f"id{i}"] = v
    # Identity eligibility MUST match the main vector leg (_build_where): this
    # feeds memory.ask synthesis at rank 1.0, so a missing scope/agent/user
    # predicate would inject another agent's or user's private memories into a
    # grounded answer on a shared namespace. Private is per-agent (agent '*'/'-'
    # = cross-agent read); shared rows carry the sentinel agent, so the scope
    # predicate alone is right there. TTL drop mirrors the main leg too.
    ident_where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "scope = {scope:String}",
        "tombstoned_at IS NULL",
        "embedding_dim > 0",
        "status IN ('active', 'duplicate', 'conflicted')",
        "(expires_at IS NULL OR expires_at > now())",
    ]
    if scope == "private" and agent not in ("*", "-"):
        ident_where.append("agent = {agent:String}")
        params["agent"] = agent
    if user_id is not None:
        ident_where.append("user_id = {user_id:String}")
        params["user_id"] = user_id
    ident_where.append(f"({conds})")
    where_sql = " AND ".join(ident_where)
    qv = [float(x) for x in (query_embedding or []) if isinstance(x, (int, float))]
    if qv:
        params["qv"] = qv
        order = "cosineDistance(embedding, {qv:Array(Float32)}) ASC"
    else:
        order = "length(content_excerpt) DESC, created_at DESC"
    if _order_mode == "recency":  # The latest word on the identifier
        order = "valid_at DESC, created_at DESC"
    # Only a cosine-ordered read touches the vector; the length and recency
    # orders never materialise `embedding` and keep one statement.
    if qv and _order_mode != "recency":
        return await ch.query_all(_fact_identifier_one_trip_sql(where_sql, order), params=params)
    return await ch.query_all(
        f"""
        SELECT agent, user_id, session_id, memory_key, memory_type, content_excerpt,
               toString(created_at) AS created_at,
               toString(valid_at) AS valid_at, version
        FROM loom.memory_embeddings FINAL
        WHERE {where_sql}
        ORDER BY {order}
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


# loom.memory_chunks' sorting key: one chunk's identity across its versions.
_CHUNK_KEY = "org, namespace, session_id, chunk_index, content_sha256"


async def find_chunks_by_identifier(
    *,
    org: str,
    namespace: str,
    identifiers: list[str],
    limit: int = 4,
    query_embedding: list[float] | None = None,
    scope: str = "private",
    agent: str = "*",
    user_id: str | None = None,
    ch: _CHClient | None = None,
    case_insensitive: bool = False,
    markers: list[str] | None = None,
    order: str = "cosine",
) -> list[dict[str, Any]]:
    """`find_by_identifier` over the verbatim-chunk plane.

    `markers` keeps only chunks that also carry one of the given substrings,
    case-insensitively — the fix / cause vocabulary that marks the decisive
    chunk of a bug thread. `order="recency"` returns the newest chunks
    instead of the closest — the latest word
    on an identifier for a disposition or premise question.

    The fact rows of an agent-operational namespace are one-line summaries;
    the root cause, file:line and fix PR sit in the chunk the fact was cut
    from. Same exact-substring predicate, same identity eligibility as the
    chunk leg (org, namespace, agent when the read is private and agent-bound,
    user when one is named — a chunk stamped '-' is then excluded; chunks carry
    no scope column), ordered by cosine relevance to
    the question when a vector is given, else longest first. Rows come back in
    the hit shape the injector reads: `memory_key` is the chunk key
    `chunk:{session}:{idx}`, `content_excerpt` the chunk text."""
    ch = ch or _default_client()
    ids = [str(i).strip().lstrip("@") for i in (identifiers or []) if str(i).strip().lstrip("@")][
        :8
    ]
    if not ids or int(limit) <= 0:
        return []
    fn = "positionCaseInsensitive" if case_insensitive else "position"
    conds = " OR ".join(
        (
            f"position(replaceRegexpAll(content, '[^0-9]', ''), {{id{i}:String}}) > 0"
            if v.isdigit() and len(v) >= 7
            else f"{fn}(content, {{id{i}:String}}) > 0"
        )
        for i, v in enumerate(ids)
    )
    params: dict[str, Any] = {"org": org, "ns": namespace, "lim": int(limit)}
    for i, v in enumerate(ids):
        params[f"id{i}"] = v
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "tombstoned_at IS NULL",
        "embedding_dim > 0",
    ]
    if scope == "private" and agent not in ("*", "-"):
        where.append("agent = {agent:String}")
        params["agent"] = agent
    if user_id is not None:
        where.append("user_id = {user_id:String}")
        params["user_id"] = user_id
    where.append(f"({conds})")
    marks = [str(m) for m in (markers or []) if str(m).strip()][:16]
    if marks:
        params["marks"] = marks
        where.append("multiSearchAnyCaseInsensitive(content, {marks:Array(String)}) > 0")
    qv = [float(x) for x in (query_embedding or []) if isinstance(x, (int, float))]
    if order == "recency":
        order_by = "created_at DESC, length(content) DESC"
    elif qv:
        params["qv"] = qv
        order_by = "cosineDistance(embedding, {qv:Array(Float32)}) ASC"
    else:
        order_by = "length(content) DESC, created_at DESC"
    # A chunk's versions can sit in two monthly partitions (a re-set written in a
    # later month), which background merges never collapse, so the rows are read
    # under FINAL — keyed: the subquery finds the matching chunk identities
    # without FINAL (every version, the planner moving position() ahead of the
    # wide columns), and the outer read re-applies the predicate to those keys
    # only, where the tombstone of a retired chunk wins.
    where_sql = " AND ".join(where)
    return await ch.query_all(
        f"""
        SELECT concat('chunk:', session_id, ':', toString(chunk_index)) AS memory_key,
               'episodic' AS memory_type,
               content AS content_excerpt,
               toString(created_at) AS created_at,
               toString(created_at) AS valid_at
        FROM loom.memory_chunks FINAL
        WHERE {where_sql}
          AND ({_CHUNK_KEY}) IN (
              SELECT DISTINCT {_CHUNK_KEY} FROM loom.memory_chunks WHERE {where_sql}
          )
        ORDER BY {order_by}
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


async def find_chunks_by_session_date(
    *,
    org: str,
    namespace: str,
    start_date: str,
    end_date: str,
    limit: int = 4,
    query_embedding: list[float] | None = None,
    scope: str = "private",
    agent: str = "*",
    user_id: str | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Verbatim chunks whose SESSION date (the session digest's valid_at, the
    conversation's observation date, the chunk clock) falls in
    [start_date, end_date], ordered by cosine to the question.

    A fact row carries the EVENT date its anchor pass resolved ("baked a cake
    last weekend" -> the weekend); a question about what was MENTIONED on a
    day needs the transcript clock instead. Same identity eligibility as the
    chunk leg (agent when the read is private and agent-bound, user when one
    is named). Rows come back
    in the injector's hit shape: `memory_key` = `chunk:{session}:{idx}`."""
    ch = ch or _default_client()
    if int(limit) <= 0 or not start_date or not end_date:
        return []
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "lim": int(limit),
        "d0": str(start_date)[:10],
        "d1": str(end_date)[:10],
    }
    where = [
        "c.org = {org:String}",
        "c.namespace = {ns:String}",
        "c.tombstoned_at IS NULL",
        "c.embedding_dim > 0",
        "toDate(ss.session_date) BETWEEN toDate({d0:String}) AND toDate({d1:String})",
    ]
    if scope == "private" and agent not in ("*", "-"):
        where.append("c.agent = {agent:String}")
        params["agent"] = agent
    if user_id is not None:
        where.append("c.user_id = {user_id:String}")
        params["user_id"] = user_id
    qv = [float(x) for x in (query_embedding or []) if isinstance(x, (int, float))]
    if qv:
        params["qv"] = qv
        order = "cosineDistance(c.embedding, {qv:Array(Float32)}) ASC"
    else:
        order = "length(c.content) DESC"
    # Read under FINAL, keyed to the identities the subquery finds without it
    # (see find_chunks_by_identifier); the subquery keeps to the sessions with
    # a digest dated in range, a superset of the join's max(valid_at) rule.
    chunk_where = [w for w in where if not w.startswith("toDate(ss.")]
    return await ch.query_all(
        f"""
        SELECT concat('chunk:', c.session_id, ':', toString(c.chunk_index)) AS memory_key,
               'episodic' AS memory_type,
               c.content AS content_excerpt,
               toString(ss.session_date) AS created_at,
               toString(ss.session_date) AS valid_at
        FROM loom.memory_chunks AS c FINAL
        INNER JOIN (
            SELECT session_id AS ss_session_id, max(valid_at) AS session_date
            FROM loom.session_summaries FINAL
            WHERE org = {{org:String}}
              AND namespace = {{ns:String}}
              AND tombstoned_at IS NULL
              AND valid_at IS NOT NULL
            GROUP BY session_id
        ) AS ss ON ss.ss_session_id = c.session_id
        WHERE {" AND ".join(where)}
          AND (c.org, c.namespace, c.session_id, c.chunk_index, c.content_sha256) IN (
              SELECT DISTINCT {_CHUNK_KEY} FROM loom.memory_chunks AS c
              WHERE {" AND ".join(chunk_where)}
                AND c.session_id IN (
                    SELECT session_id FROM loom.session_summaries
                    WHERE org = {{org:String}} AND namespace = {{ns:String}}
                      AND toDate(valid_at) BETWEEN toDate({{d0:String}}) AND toDate({{d1:String}})
                )
          )
        ORDER BY {order}
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


# Suffixes a row may carry for an attribute facet the extractor did not
# canonicalise to the key on the left (see memory_extract.normalize_attribute_keys).
FACET_SUFFIX_SYNONYMS: dict[str, tuple[str, ...]] = {
    "birthday": ("birthdate", "born"),
    "duration": ("length",),
    "location": ("venue", "city", "place"),
    "hometown": ("origin",),
    "attendance": ("scale", "capacity", "size", "crowd", "attendees", "turnout"),
}


async def find_by_key_facet(
    *,
    org: str,
    namespace: str,
    facets: list[str],
    limit: int = 60,
    scope: str = "private",
    agent: str = "*",
    user_id: str | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Every active memory whose key ends in one of `facets` ("_age",
    "_location", "_duration") — the extractor names keys `<entity>_<attribute>`,
    so the suffix is the attribute facet. Backs memory.ask's facet
    completion: a count or list question needs the WHOLE set of one
    attribute in the window, and top-k similarity returns the top k of it.

    Identity eligibility mirrors `find_by_identifier` and the main vector leg
    (scope / agent / user_id / TTL / status): these rows reach synthesis, so a
    missing predicate would leak another agent's or user's private facts on a
    shared namespace. Ordered by key so one entity's facts sit together and a
    truncated list is deterministic.
    """
    ch = ch or _default_client()
    fs = []
    for f in facets or []:
        f = str(f).strip().strip("_").lower()
        if f and re.fullmatch(r"[a-z][a-z0-9]{1,31}", f):
            fs.append(f)
    fs = fs[:6]
    if not fs:
        return []
    # A row may carry a synonym (`_venue`, `_city` for location); match
    # those too.
    suffixes: list[str] = []
    for f in fs:
        for alt in (f, *FACET_SUFFIX_SYNONYMS.get(f, ())):
            if alt not in suffixes:
                suffixes.append(alt)
    suffixes = suffixes[:12]
    conds = " OR ".join(f"endsWith(memory_key, {{fx{i}:String}})" for i in range(len(suffixes)))
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "lim": int(limit),
        "scope": scope,
    }
    for i, f in enumerate(suffixes):
        params[f"fx{i}"] = f"_{f}"
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "scope = {scope:String}",
        "tombstoned_at IS NULL",
        "embedding_dim > 0",
        "status IN ('active', 'duplicate', 'conflicted')",
        "(expires_at IS NULL OR expires_at > now())",
    ]
    if scope == "private" and agent not in ("*", "-"):
        where.append("agent = {agent:String}")
        params["agent"] = agent
    if user_id is not None:
        where.append("user_id = {user_id:String}")
        params["user_id"] = user_id
    where.append(f"({conds})")
    return await ch.query_all(
        f"""
        SELECT agent, user_id, session_id, memory_key, memory_type, content_excerpt,
               toString(created_at) AS created_at, version
        FROM loom.memory_embeddings FINAL
        WHERE {" AND ".join(where)}
        ORDER BY memory_key ASC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


# RE2 (ClickHouse) has no lookaround: the connective is replaced together
# with its underscores by one underscore, twice, so `_of_the_` also folds.
_KEY_CONNECTIVES_RE = re.compile(r"_(?:and|the|of|for|at|in|a|an)_")


_KEY_ACTOR_PREFIX_RE = re.compile(r"^(?:user|the)_")


# The `user_` the extractor sometimes leads with names the speaker, not the
# entity: "The capital of Italy is Duluth" keyed `user_italy_capital` would
# miss the chain's `italy_` prefix. Both sides drop it before comparing.
_KEY_NORM_SQL = (
    "replaceRegexpAll(replaceRegexpAll(replaceRegexpOne(memory_key, '^(user|the)_', ''),"
    " '_(and|the|of|for|at|in|a|an)_', '_'), '_(and|the|of|for|at|in|a|an)_', '_')"
)


def normalize_key_prefix(prefix: str) -> str:
    """`serve_and_connect` → `serve_connect`: the connective segments the
    extractor sometimes keeps and the relation backfill drops; a leading
    `user_` / `the_` names the speaker or an article, not the entity."""
    out = _KEY_ACTOR_PREFIX_RE.sub("", prefix or "")
    for _ in range(2):
        out = _KEY_CONNECTIVES_RE.sub("_", out)
    return out


async def find_by_key_prefix(
    *,
    org: str,
    namespace: str,
    prefixes: list[str],
    limit: int = 48,
    per_prefix: int = 6,
    scope: str = "private",
    agent: str = "*",
    user_id: str | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Every active memory whose key starts with one of `prefixes` followed
    by "_" — the extractor names keys `<entity>_<attribute>`, so the prefix
    is the entity and its siblings are that entity's other attributes. Backs
    memory.ask's bridge completion: a question that names an entity by one
    attribute ("the event that lasts one day")
    retrieves that attribute's row and needs the entity's OTHER attributes
    (its location, its time) to answer.

    Identity eligibility mirrors `find_by_key_facet`; ordered by key so one
    entity's facts sit together and a truncated list is deterministic.
    """
    ch = ch or _default_client()
    ps: list[str] = []
    for pf in prefixes or []:
        pf = str(pf).strip().strip("_").lower()
        if pf and re.fullmatch(r"[a-z][a-z0-9_]{1,63}", pf) and pf not in ps:
            ps.append(pf)
    ps = ps[:12]
    if not ps:
        return []
    # The extractor and the relation backfill spell one entity two ways —
    # `user_serve_and_connect_duration` and `serve_connect_locat_in_location`
    # — so both sides are compared with the connective segments removed.
    conds = " OR ".join(f"startsWith({_KEY_NORM_SQL}, {{px{i}:String}})" for i in range(len(ps)))
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "lim": int(limit),
        "scope": scope,
    }
    for i, pf in enumerate(ps):
        params[f"px{i}"] = f"{normalize_key_prefix(pf)}_"
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "scope = {scope:String}",
        "tombstoned_at IS NULL",
        "embedding_dim > 0",
        "status IN ('active', 'duplicate', 'conflicted')",
        "(expires_at IS NULL OR expires_at > now())",
    ]
    if scope == "private" and agent not in ("*", "-"):
        where.append("agent = {agent:String}")
        params["agent"] = agent
    if user_id is not None:
        where.append("user_id = {user_id:String}")
        params["user_id"] = user_id
    where.append(f"({conds})")
    # Rows are shared out per prefix (LIMIT n BY): with twelve prefixes and one
    # cap ordered by key, the entities late in the alphabet would lose every
    # slot. Each entity gets up to `per_prefix` rows,
    # the whole read stays under `limit`.
    idx_expr = (
        "multiIf("
        + ", ".join(f"startsWith({_KEY_NORM_SQL}, {{px{i}:String}}), {i}" for i in range(len(ps)))
        + f", {len(ps)})"
    )
    params["per"] = max(1, int(per_prefix))
    # A key written from two sessions is two rows here (the table's identity
    # carries the session); the key names one attribute and its newest row is
    # the live one, so only that row is read, with its write stamp.
    return await ch.query_all(
        f"""
        SELECT agent, user_id, session_id, memory_key, memory_type, content_excerpt,
               created_at, valid_at, version, status
        FROM (
            SELECT agent, user_id, session_id, memory_key, memory_type, content_excerpt,
                   toString(created_at) AS created_at,
                   toString(valid_at) AS valid_at,
                   version, toString(status) AS status
            FROM loom.memory_embeddings FINAL
            WHERE {" AND ".join(where)}
            ORDER BY memory_key ASC, version DESC
            LIMIT 1 BY memory_key
        )
        ORDER BY memory_key ASC
        LIMIT {{per:UInt32}} BY {idx_expr}
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


async def find_by_content_terms(
    *,
    org: str,
    namespace: str,
    terms: list[str],
    limit: int = 12,
    per_term: int = 3,
    scope: str = "private",
    agent: str = "*",
    user_id: str | None = None,
    cues: list[str] | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Active memories whose text names one of `terms` (case-insensitive
    substring), up to `per_term` each — the fallback behind
    `find_by_key_prefix` for an entity the extractor did not put first in the
    key ("The capital of Soviet Union is Russellville" keyed
    `capital_of_soviet`). One
    namespace range on the primary key, the excerpt column read once; the
    caller sends the few prefixes that matched no key, never the whole list.
    `cues` — the question's own content words ("capital") — order each
    term's share so the row that answers comes before the rows that merely
    name the entity (e.g. six rows name "Italy" and the capital row sorts
    fourth by key)."""
    ch = ch or _default_client()
    ts: list[str] = []
    for t in terms or []:
        t = " ".join(str(t).replace("_", " ").split()).strip().lower()
        if len(t) >= 3 and t not in ts:
            ts.append(t)
    ts = ts[:4]
    if not ts:
        return []
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "lim": int(limit),
        "per": max(1, int(per_term)),
        "scope": scope,
    }
    for i, t in enumerate(ts):
        params[f"t{i}"] = t
    conds = " OR ".join(
        f"positionCaseInsensitive(content_excerpt, {{t{i}:String}}) > 0" for i in range(len(ts))
    )
    idx_expr = (
        "multiIf("
        + ", ".join(
            f"positionCaseInsensitive(content_excerpt, {{t{i}:String}}) > 0, {i}"
            for i in range(len(ts))
        )
        + f", {len(ts)})"
    )
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "scope = {scope:String}",
        "tombstoned_at IS NULL",
        "embedding_dim > 0",
        "status IN ('active', 'duplicate', 'conflicted')",
        "(expires_at IS NULL OR expires_at > now())",
    ]
    if scope == "private" and agent not in ("*", "-"):
        where.append("agent = {agent:String}")
        params["agent"] = agent
    if user_id is not None:
        where.append("user_id = {user_id:String}")
        params["user_id"] = user_id
    where.append(f"({conds})")
    cue_list = [
        " ".join(str(c).split()).strip().lower() for c in (cues or []) if len(str(c).strip()) >= 3
    ][:8]
    order = "memory_key ASC"
    if cue_list:
        params["cues"] = cue_list
        order = (
            "multiSearchAnyCaseInsensitive(content_excerpt, {cues:Array(String)}) DESC, "
            "memory_key ASC"
        )
    return await ch.query_all(
        f"""
        SELECT agent, user_id, session_id, memory_key, memory_type, content_excerpt,
               toString(created_at) AS created_at,
               toString(valid_at) AS valid_at, version
        FROM loom.memory_embeddings FINAL
        WHERE {" AND ".join(where)}
        ORDER BY {order}
        LIMIT {{per:UInt32}} BY {idx_expr}
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
