"""memory.recall — the raw turns behind the facts, searchable by text and by
time.

Every `memory.set_from_messages` call leaves the turns it extracted from in
`loom.memory_evidence`. The other readers are shaped by a question: the deep
pass opens the turns behind a fact key it already retrieved, the literal
completion scans for a value a count question names, the time-window
completion pulls the turns around a named minute. A fact the extractor never
wrote — the statement that became nothing, the date the fact lost — is in the
store and reachable only here.

This is the plain door: text match (every query token, or any) with an
optional time window and role filter, keyset-paginated newest-first, or
ranked by how many query tokens a turn carries. No model call. The SQL is
built here as pure functions so the shape is unit-testable without a
database; the handler owns the client and the read scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

MAX_QUERY_TOKENS = 32
MIN_TOKEN_LEN = 2

# Words that are in nearly every turn. `hasAnyTokens` on one of them is a
# scan of the namespace, and a relevance score that counts them says nothing.
_STOP: frozenset[str] = frozenset(
    """a an the and or but if then so of to in on at by for from with about
    as into like through after over between out against during without before
    under around among is am are was were be been being do does did have has
    had will would shall should can could may might must i me my we our you
    your he him his she her it its they them their this that these those what
    which who whom whose when where why how not no yes ok okay just also very
    really there here up down""".split()
)


def query_tokens(text: str) -> list[str]:
    """Lowercased alphanumeric tokens of length >= 2, stop words dropped,
    order kept, deduplicated, capped at MAX_QUERY_TOKENS. Mirrors the
    `splitByNonAlpha` tokenizer `hasAnyTokens` uses closely enough that a
    token here is a token there."""
    out: list[str] = []
    cur: list[str] = []
    for ch in (text or "").lower():
        if ch.isalnum():
            cur.append(ch)
        else:
            if len(cur) >= MIN_TOKEN_LEN:
                out.append("".join(cur))
            cur = []
    if len(cur) >= MIN_TOKEN_LEN:
        out.append("".join(cur))
    seen: set[str] = set()
    kept: list[str] = []
    for t in out:
        if t in _STOP or t in seen:
            continue
        seen.add(t)
        kept.append(t)
    return kept[:MAX_QUERY_TOKENS]


def resolve_order(order: str, tokens: list[str]) -> str:
    """`auto` is relevance when there is something to be relevant to."""
    if order in ("recent", "relevance"):
        return order
    return "relevance" if tokens else "recent"


@dataclass(frozen=True)
class RecallFilter:
    """Everything the page query and the count query share, so the two can
    never disagree about which turns are in scope."""

    tokens: list[str]
    match: str = "all"
    since: datetime | None = None
    until: datetime | None = None
    role: str = ""
    agent: str = "*"
    user_id: str | None = None
    session_id: str | None = None

    @property
    def settings_sql(self) -> str:
        # Guards the direct-read shortcut should a text index be added on content_lc.
        if self.tokens:
            return "\n        SETTINGS query_plan_direct_read_from_text_index = 0"
        return ""


def _where(f: RecallFilter) -> tuple[list[str], dict[str, Any]]:
    tokens, match, since, until, role = f.tokens, f.match, f.since, f.until, f.role
    agent, user_id, session_id = f.agent, f.user_id, f.session_id
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "tombstoned_at IS NULL",
    ]
    params: dict[str, Any] = {}
    if agent and agent != "*":
        where.append("agent = {agent:String}")
        params["agent"] = agent
    if user_id:
        where.append("user_id = {uid:String}")
        params["uid"] = user_id
    if session_id:
        where.append("session_id = {sid:String}")
        params["sid"] = session_id
    if role:
        where.append("role = {role:String}")
        params["role"] = role
    if since is not None:
        where.append("observed_at >= {since:DateTime}")
        params["since"] = since
    if until is not None:
        where.append("observed_at <= {until:DateTime}")
        params["until"] = until
    if tokens:
        fn = "hasAllTokens" if match == "all" else "hasAnyTokens"
        where.append(f"{fn}(content_lc, {{tokens:Array(String)}})")
        params["tokens"] = list(tokens)
    return where, params


def build_recall_sql(
    f: RecallFilter,
    *,
    order: str = "recent",
    cursor: tuple[datetime, str] | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[str, dict[str, Any]]:
    """The page query. `limit` rows plus one: the caller learns whether a next
    page exists without a second round trip.

    recent: newest first, keyset on (observed_at, evidence_id) so a page is
    stable while new turns land. The projected columns are aliased `eid` /
    `observed` — an alias that shadows a column name is what ClickHouse
    resolves in WHERE and ORDER BY, and a keyset compared against a
    stringified column is a NO_COMMON_TYPE error. relevance: distinct query
    tokens present, then newest; offset-paginated because a score is not a
    keyset.

    The token predicate reads `content_lc`; the table has no text index, so
    the (org, namespace) key prefix does the pruning.
    """
    where, params = _where(f)
    score = "0"
    if f.tokens:
        score = "arraySum(t -> position(content_lc, t) > 0, {tokens:Array(String)})"
    order_sql = "observed_at DESC, evidence_id DESC"
    if order == "relevance":
        order_sql = "matched DESC, observed_at DESC, evidence_id DESC"
    elif cursor is not None:
        where.append("(observed_at, evidence_id) < ({cur_ts:DateTime}, {cur_id:UUID})")
        params["cur_ts"], params["cur_id"] = cursor
    params["lim"] = int(limit) + 1
    params["off"] = int(offset) if order == "relevance" else 0
    # Versioned, not FINAL: the latest row per
    # evidence_id inside, the tombstone judged outside the LIMIT BY.
    inner_where = [w for w in where if w != "tombstoned_at IS NULL"]
    sql = f"""
        SELECT toString(evidence_id) AS eid, session_id, role, actor,
               toString(observed_at) AS observed, content, matched
        FROM (
            SELECT evidence_id, session_id, role, actor, observed_at, content,
                   tombstoned_at, toUInt32({score}) AS matched
            FROM loom.memory_evidence
            WHERE {" AND ".join(inner_where)}
            ORDER BY evidence_id, created_at DESC
            LIMIT 1 BY evidence_id
        )
        WHERE tombstoned_at IS NULL
        ORDER BY {order_sql}
        LIMIT {{lim:UInt32}} OFFSET {{off:UInt32}}{f.settings_sql}
    """
    return sql, params


def build_count_sql(f: RecallFilter) -> tuple[str, dict[str, Any]]:
    """How many turns match in total — the "showing 20 of 124" number the
    caller needs to decide whether to page."""
    where, params = _where(f)
    inner_where = [w for w in where if w != "tombstoned_at IS NULL"]
    sql = f"""
        SELECT count() AS n
        FROM (
            SELECT evidence_id, tombstoned_at
            FROM loom.memory_evidence
            WHERE {" AND ".join(inner_where)}
            ORDER BY evidence_id, created_at DESC
            LIMIT 1 BY evidence_id
        )
        WHERE tombstoned_at IS NULL{f.settings_sql}
    """
    return sql, params


def page_rows(rows: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], bool]:
    """Split a limit+1 read into the page and the has-more flag."""
    rows = list(rows or [])
    more = len(rows) > limit
    return rows[:limit], more
