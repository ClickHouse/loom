"""Pure SQL-fragment builders for memory.search.

State-free, ClickHouse-client-free helpers that translate a SearchQuery
into WHERE clauses + bound params.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from server.core.tag_sql import tag_predicate
from server.memory.domain.search_types import (
    SearchHit,
    SearchQuery,
    hit_row_key,
    names_row,
    writers_covered,
)
from server.memory.domain.temporal_sql import as_of_predicate, status_predicate, tombstone_predicate


def _plane_enabled(q: SearchQuery, plane: str) -> bool:
    """Whether an execution plane is enabled. An empty mask = all enabled."""
    return not q.execution_planes or plane in set(q.execution_planes)


def hard_tag_filter(tags: Any, tags_soft: Any) -> list[str]:
    """The tags a request's HARD filter names: its `tags`,
    unless `tags_soft` turned them into a rank boost. [] = no hard filter.
    One reading of the request for every candidate-producing leg, so the
    predicate cannot be honoured by the fact leg and forgotten by another."""
    if bool(tags_soft):
        return []
    return [str(t) for t in (tags or []) if t]


def agent_filter(agent: Any) -> str:
    """The agent a request names for a per-agent predicate:
    the value, or "" when the request names none or a cross-agent sentinel —
    the `*` wildcard and the `-` identity default `_build_where` also skips."""
    value = str(agent or "")
    return "" if value in ("*", "-") else value


def agent_or_shared_predicate(alias: str = "e") -> str:
    """`(<alias>.agent = {agent:String} OR <alias>.scope = 'shared')` — the
    additive legs' agent clause (the agent's own rows plus the namespace's
    shared rows), spelled once so the entity / temporal
    anchor pool, the key-match arm and the digest descent read the same
    clause with the same parameter name. `alias=""` renders bare columns."""
    agent_col = f"{alias}.agent" if alias else "agent"
    scope_col = f"{alias}.scope" if alias else "scope"
    return f"({agent_col} = {{agent:String}} OR {scope_col} = 'shared')"


def exclude_pooled(where: list[str], params: dict[str, Any], hits: Sequence[SearchHit]) -> None:
    """Leave the rows `hits` already pooled out of a leg over `loom.memory_embeddings AS e`:
    each row a hit names, and each writer's rows of the key a folded set chunk covers. A
    record or a summary names no memory row and leaves out none."""
    rows = sorted(
        (r.agent, r.user_id, r.session_id, r.memory_key)
        for r in {hit_row_key(h) for h in hits if names_row(h)}
    )
    writers = sorted({(a, u, k) for _ns, a, u, k in writers_covered(hits)})
    if rows:
        where.append(
            "(e.agent, e.user_id, e.session_id, e.memory_key)"
            " NOT IN {pooled_rows:Array(Tuple(String, String, String, String))}"
        )
        params["pooled_rows"] = rows
    if writers:
        where.append(
            "(e.agent, e.user_id, e.memory_key)"
            " NOT IN {pooled_writers:Array(Tuple(String, String, String))}"
        )
        params["pooled_writers"] = writers


def _build_where(q: SearchQuery) -> tuple[list[str], dict[str, Any]]:
    replaying = q.as_of is not None
    where = [
        "e.org = {org:String}",
        "e.namespace = {ns:String}",
        "e.scope = {scope:String}",
        "e.embedding_model = {model:String}",
        "e.embedding_dim > 0",
        # Deleted rows are gone — except under knowledge_time replay, where a
        # row deleted AFTER the anchor was still known at the anchor.
        tombstone_predicate(q.as_of_basis, alias="e", replaying=replaying),
        # TTL drop; NULL = no expiry.
        "(e.expires_at IS NULL OR e.expires_at > now())",
        # Stated validity window: a fact whose own end date has
        # passed ("on antibiotics until the 14th", "worked at Acme until 2024")
        # is not current. Under as_of the valid_time predicate below owns the
        # window instead, so a closed interval stays readable at a date inside
        # it. NULL = open interval.
        *(() if replaying else ("(e.valid_to IS NULL OR e.valid_to > now())",)),
        # Exclude superseded / rejected by default; knowledge_time replay
        # re-admits a row superseded after the anchor (server/temporal_sql).
        status_predicate(
            q.as_of_basis,
            alias="e",
            replaying=replaying,
            include_duplicates=bool(getattr(q, "include_duplicates", False)),
            include_superseded=bool(getattr(q, "include_superseded", False)),
            include_proposed=bool(getattr(q, "include_proposed", False)),
        ),
    ]
    params: dict[str, Any] = {
        "org": q.org,
        "ns": q.namespace,
        "scope": q.scope,
        "model": q.embedding_model,
        "q": q.query_vec,
        "top_k": int(q.top_k),
    }
    # Private is per-agent; shared rows carry sentinel agent='_shared', so the
    # scope filter alone is right for shared reads. Wildcard `*` AND the `-`
    # identity sentinel (the default when no agent is set) both drop the
    # per-agent filter for cross-agent search — `-` is never a stored agent
    # value, so filtering on it returns zero real rows (same convention as
    # memory.list).
    if q.scope == "private" and q.agent not in ("*", "-"):
        where.append("e.agent = {agent:String}")
        params["agent"] = q.agent
    if q.memory_type:
        where.append("e.memory_type = {type:String}")
        params["type"] = q.memory_type
    else:
        # NON_SEARCHABLE_TYPES (episodic = session replay, profile =
        # always-loaded context) pollute NL-query ranking, and
        # HIDDEN_FROM_UNFILTERED_SEARCH (reflection = process lessons, not
        # facts) must not be synthesized into an answer as if they were
        # claims; both are excluded unless a memory_type asks for them.
        from server.memory.domain.memory_types import excluded_from_unfiltered_search

        where.append("e.memory_type NOT IN {non_searchable:Array(String)}")
        params["non_searchable"] = excluded_from_unfiltered_search()
    # Hard tag filter — unless tags_soft, in which case tags become a ranking
    # boost (built in vector_search) rather than an eligibility gate.
    hard_tags = hard_tag_filter(q.tags, q.tags_soft)
    if hard_tags:
        where.append(tag_predicate("e"))
        params["tags"] = hard_tags
    if q.user_id is not None:
        where.append("e.user_id = {user_id:String}")
        params["user_id"] = q.user_id
    # Read scope. `session_id` sits in the sort key of memory_embeddings next
    # to user_id, so both predicates prune parts before the distance sort —
    # the cheapest place to bound how far one read can reach.
    if q.session_id is not None:
        where.append("e.session_id = {session_id:String}")
        params["session_id"] = q.session_id
    if getattr(q, "session_ids", None):
        # The caller's session set; same sort-key prune.
        where.append("e.session_id IN {session_ids:Array(String)}")
        params["session_ids"] = [str(s) for s in q.session_ids]
    if q.run_id is not None:
        where.append("e.run_id = {run_id:String}")
        params["run_id"] = q.run_id
    if q.as_of is not None:
        # Both axes come from server/temporal_sql so every leg agrees:
        # knowledge_time = written by T and not retired/deleted by T;
        # valid_time = valid_at, else the event date (temporal_anchor), else
        # the ingest clock — OR-form so the created_at prune stays fast.
        where.append(
            as_of_predicate(
                q.as_of_basis,
                alias="e",
                invalid_col="invalid_at",
                tomb_col="tombstoned_at",
            )
        )
        params["as_of"] = q.as_of
    if q.category and not q.category_soft:
        # Hard-filter to memory_keys linked to a category with this name;
        # subquery resolves name → id then joins through memory_categories.
        # (category_soft turns this into a rank boost instead — see vector_search.)
        where.append(
            "e.memory_key IN ("
            "  SELECT memory_key FROM loom.memory_categories FINAL"
            "  WHERE org = {org:String} AND namespace = {ns:String}"
            "    AND tombstoned_at IS NULL"
            "    AND category_id = ("
            "      SELECT category_id FROM loom.categories FINAL"
            "      WHERE org = {org:String} AND namespace = {ns:String}"
            "        AND name = {category:String}"
            "        AND tombstoned_at IS NULL"
            "      LIMIT 1"
            "    )"
            ")"
        )
        params["category"] = q.category
    return where, params
