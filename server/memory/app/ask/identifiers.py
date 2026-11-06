"""Exact-identifier evidence: the identifier shapes a question names, the
exact-substring reads that inject the rows citing them, the chase through
the identifiers those rows name, and the mention-time transcript chunks."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.ask.question_shapes import (
    is_decisive_question,
    is_latest_word_question,
    is_mention_time_question,
)
from server.memory.app.ask.result import _is_chunk_hit
from server.memory.domain.search_types import row_identity
from server.memory.domain.sensitivity import HELD_MARGIN, without_high

logger = logging.getLogger(__name__)


# Broader entity shapes for INJECTION only (not the abstain hedge). All are
# high-precision, low-false-positive: a snake_case setting/symbol, a PascalCase
# 2+-hump handle/type, or a zero-padded test id. Each is exact-substring matched
# in find_by_identifier and additive, so a stray match only mildly dilutes.
_INJECT_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+){1,}\b")
# The humps after the first are one run: a word splits into humps in many
# ways, and trying each one is exponential in the word's length.
_INJECT_HANDLE_RE = re.compile(r"\b[A-Z][a-z]++[A-Z][a-zA-Z0-9]++\b")
# A named account or handle written without its "@" —
# "the GitHub account octodev", "reporter janedoe" — is the identifier
# of every row about that account; nothing above caught a bare lowercase name.
_INJECT_ACCOUNT_RE = re.compile(
    r"\b(?:account|user|handle|reporter|author|contributor|reviewer|bot|maintainer)\s+@?"
    r"([a-z][a-z0-9_-]{4,})\b",
    re.IGNORECASE,
)
_INJECT_ZEROID_RE = re.compile(r"\b0\d{3,}\b")
# A ClickHouse/CI "STID" (sighting/trace id), e.g. 3782-2f00 — a digit run then a
# hex quartet. It is the load-bearing key of a failure a query is about, yet the
# other injectable shapes miss it entirely (digits+hyphen+hex isn't snake_case,
# a PascalCase hump, a zero-id, or a bare number), so a query naming ONLY a STID
# extracted no identifier at all and injection fell back to topic words.
_INJECT_STID_RE = re.compile(r"\b\d{3,4}-[0-9a-f]{4}\b")
# file:line (e.g. MergeTreeData.cpp:3539) — a per-site anchor a query names and
# the root-cause memory repeats verbatim; also unmatched by the shapes above.
# Group 1, read from a path's first word only, so a long path costs one pass.
_INJECT_FILELINE_RE = re.compile(
    r"(?<![\w/])/*+\b([\w/]+\.(?:cpp|hpp|cc|cxx|h|py|sql|go|rs|cmake|sh|yml|yaml):\d+)\b"
)
# An ordinal in the question ("the 27th parameter", "the fourth
# option") names a LINE of a list the store holds verbatim — "\n27. Sound
# effects …" — and no embedding puts that line first among a hundred. The
# line marker is an exact substring of the chunk, so it rides the identifier
# read: "27th" → "\n27." (the newline keeps "127." and "$27.99" out).
_INJECT_ORDINAL_RE = re.compile(r"\b(\d{1,3})(?:st|nd|rd|th)\b", re.IGNORECASE)
_WORD_ORDINALS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "twentieth": 20,
}
_INJECT_WORD_ORDINAL_RE = re.compile(
    r"\b(" + "|".join(_WORD_ORDINALS) + r")\b(?=\s+(?:one|item|option|parameter|point|step|"
    r"entry|suggestion|recommendation|tip|book|song|dish|app|place|thing|element|bullet))",
    re.IGNORECASE,
)


async def _plain_chunks(
    find: Callable[..., Awaitable[list[dict[str, Any]]]], *, limit: int, **kwargs: Any
) -> list[dict[str, Any]]:
    """`find` (a chunk lookup), without the chunks whose text reads as high. A chunk
    written as one has no vector and the lookup's `embedding_dim > 0` filter leaves it out;
    this holds back one an earlier build embedded."""
    rows = await find(limit=limit + HELD_MARGIN, **kwargs)
    return without_high(rows, lambda r: str(r.get("content_excerpt") or ""), limit)


def ordinal_line_markers(query_text: str) -> list[str]:
    """The list-line markers a question's ordinals name: "27th" → "\n27.";
    "fourth option" → "\n4."; none for "first" without a list noun."""
    out: list[str] = []
    for m in _INJECT_ORDINAL_RE.finditer(query_text or ""):
        n = int(m.group(1))
        if 1 <= n <= 300 and f"\n{n}." not in out:
            out.append(f"\n{n}.")
    for m in _INJECT_WORD_ORDINAL_RE.finditer(query_text or ""):
        n = _WORD_ORDINALS[m.group(1).lower()]
        if f"\n{n}." not in out:
            out.append(f"\n{n}.")
    return out


async def _mention_window_chunks(search_input: Any, question: str, ranked: list[Any]) -> list[Any]:
    """Chunks from the sessions dated inside the question's resolved relative
    window (±1 day), cosine-ranked, as injectable hits. Empty
    when the question resolves to no date. Fail-open."""
    from server.settings import settings as _s3000

    _n = int(_s3000.memory_ask_mention_window_chunks)
    if _n <= 0 or not is_mention_time_question(question):
        return []
    try:
        from datetime import date as _date
        from datetime import timedelta as _td

        import server.memory.store as memory_repo
        from server.memory.domain.query_intent import parse_query_intent as _pqi

        obs = getattr(search_input, "observation_date", None) or None
        intent = _pqi(question, obs)
        dates = [str(d)[:10] for d in (getattr(intent, "resolved_dates", ()) or ()) if d]
        if not dates:
            return []
        lo = min(_date.fromisoformat(d) for d in dates) - _td(days=1)
        hi = max(_date.fromisoformat(d) for d in dates) + _td(days=1)
        qvec: list[float] = []
        try:
            from server.embedding import get_embedder

            qvec = list(await get_embedder().embed(question))
        except Exception:  # noqa: BLE001 — length order is the fallback
            qvec = []
        rows = await _plain_chunks(
            memory_repo.find_chunks_by_session_date,
            org=getattr(search_input, "org", ""),
            namespace=getattr(search_input, "namespace", ""),
            start_date=lo.isoformat(),
            end_date=hi.isoformat(),
            limit=_n,
            query_embedding=qvec or None,
            scope=getattr(search_input, "scope", "private"),
            agent=getattr(search_input, "agent", "*"),
            user_id=getattr(search_input, "user_id", None),
        )
    except Exception as e:  # noqa: BLE001 — additive, never fatal
        logger.debug("mention-window chunks skipped: %s", e)
        return []

    have = {getattr(h, "memory_key", "") for h in ranked}
    out: list[Any] = []
    for r in rows:
        k = str(r.get("memory_key") or "")
        if not k or k in have:
            continue
        have.add(k)
        try:
            out.append(
                _MemorySearchHit(
                    memory_key=k,
                    memory_type="episodic",
                    content_excerpt=str(r.get("content_excerpt") or ""),
                    distance=0.05,
                    similarity=0.95,
                    rank_score=0.95,
                    source="chunk",
                    valid_at=str(r.get("valid_at") or "")[:19],
                )
            )
        except Exception:  # noqa: BLE001 — skip a row that won't validate
            continue
    return out


# The humps are one run, as in _INJECT_HANDLE_RE.
_TECH_PASCAL_RE = re.compile(r"\b[A-Z][a-z0-9]++[A-Z][a-zA-Z0-9]++(?:::\w+)?\b")
_TECH_CAMEL_RE = re.compile(r"\b[a-z]{2,}[A-Z][A-Za-z0-9]+\b")
_TECH_FILE_RE = re.compile(
    r"\b[\w/.-]{1,255}\.(?:cpp|hpp|cc|cxx|h|py|sql|go|rs|cmake|sh|yml|yaml|reference|xml)(?::\d+)?\b"
)
_TECH_QUOTE_RE = re.compile(r"[\"“'‘]([^\"”'’\n]{8,120})[\"”'’]")
_TECH_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+){1,}\b")
# Product and prose words that match the shapes above but name nothing.
_TECH_STOP = {
    "clickhouse",
    "github",
    "zookeeper",
    "postgresql",
    "clickstack",
    "youtube",
    "javascript",
    "typescript",
    "pull request",
    "linkedin",
    "wikipedia",
    "stackoverflow",
}


def _technical_identifiers(text: str) -> list[str]:
    """Identifier shapes a coding agent's questions and rows carry that the
    number/@handle set (retrieval_calibration._salient_identifiers) and the
    planner's UUID/hex/mixed tokens both miss: PascalCase and
    camelCase symbols, bare file names, snake_case names and quoted phrases
    (an error string is the identifier of its bug). First-seen order, deduped
    case-insensitively, product words dropped. Pure."""
    t = text or ""
    out: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        k = tok.strip().lower()
        if len(k) < 5 or k in seen or k in _TECH_STOP:
            return
        seen.add(k)
        out.append(tok.strip())

    for m in _TECH_QUOTE_RE.finditer(t):
        _add(m.group(1))
    for pat in (_TECH_FILE_RE, _TECH_PASCAL_RE, _TECH_CAMEL_RE, _TECH_SNAKE_RE):
        for m in pat.finditer(t):
            _add(m.group(0))
    return out[:12]


def _injectable_identifiers(query_text: str) -> list[str]:
    """The salient identifiers (numbers + @handles) PLUS entity shapes semantic
    recall tends to miss when the query is about the topic, not the entity:
    snake_case settings/symbols, PascalCase handles/types, zero-padded test ids.
    Injection-only (additive/fail-open); the abstain hedge stays on the narrower
    _salient_identifiers to avoid over-abstaining."""
    from server.memory.domain.retrieval_calibration import _salient_identifiers

    out = list(_salient_identifiers(query_text))
    seen = {o.lower() for o in out}
    for pat in (
        _INJECT_SNAKE_RE,
        _INJECT_HANDLE_RE,
        _INJECT_ZEROID_RE,
        _INJECT_STID_RE,
        _INJECT_FILELINE_RE,
    ):
        for m in pat.finditer(query_text or ""):
            t = m.group(1) if pat.groups else m.group(0)
            # A product word is not an identifier: OR-ed into the exact fetch
            # it would crowd the numbered rows out.
            if len(t) >= 5 and t.lower() not in seen and t.lower() not in _TECH_STOP:
                seen.add(t.lower())
                out.append(t)
    for marker in ordinal_line_markers(query_text):
        if marker.lower() not in seen:
            seen.add(marker.lower())
            out.append(marker)
    for m in _INJECT_ACCOUNT_RE.finditer(query_text or ""):
        t = m.group(1)
        if len(t) >= 5 and t.lower() not in seen and t.lower() not in _TECH_STOP:
            seen.add(t.lower())
            out.append(t)
    # The planner's identifier shapes — UUIDs, hash-like hex
    # runs, letter+digit tokens — are exactly what a key-value or commit
    # chain is made of, and none of the patterns above catches a UUID.
    try:
        from server.memory.domain.query_planner import identifier_tokens

        for t in identifier_tokens(query_text or ""):
            if len(t) >= 5 and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
    except Exception:  # noqa: BLE001 — injection is additive; the shapes above still apply
        pass
    # Symbols, file names and quoted phrases.
    for t in _technical_identifiers(query_text or ""):
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _cited_hits(hits: list[Any], idents: list[str]) -> list[Any]:
    """The hits whose text or key carries one of the question's identifiers —
    the rows a chase starts from when the search already ranked them in."""
    keys = [str(i).lower().lstrip("@") for i in (idents or []) if str(i).strip()]
    if not keys:
        return []
    out: list[Any] = []
    for h in hits:
        excerpt = str(getattr(h, "content_excerpt", "") or "")
        text = (excerpt + " " + str(getattr(h, "memory_key", "") or "")).lower()
        if any(k in text for k in keys):
            out.append(h)
    return out


def next_identifiers(hits: list[Any], *, seen: list[str]) -> list[str]:
    """Identifier-shaped tokens the hits' text names that the question (and
    earlier hops) did not: the frontier of the next hop. Pure. Order is first
    seen across the hits; tokens already chased, and tokens that are
    substrings of a seen token (a UUID's own segments), are skipped."""
    from server.memory.domain.query_planner import identifier_tokens

    seen_l = [str(x).lower() for x in seen if x]
    out: list[str] = []
    for h in hits:
        text = str(getattr(h, "content_excerpt", "") or "")
        # A symbol, file or quoted error string the window names
        # is a hop too — that is where a coding agent's mechanism rows live.
        for tok in list(identifier_tokens(text)) + _technical_identifiers(text):
            t = tok.lower()
            if any(t == x or t in x for x in seen_l) or t in (o.lower() for o in out):
                continue
            out.append(tok)
    return out[:12]


async def _chase_identifiers(
    search_input: Any,
    *,
    seen: list[str],
    frontier: list[Any],
    ranked: list[Any],
    hops: int,
    cap: int,
) -> tuple[list[Any], int]:
    """Walk identifier → memories → new identifiers for up to `hops` hops.
    Each hop is one `_inject_identifier_memories` read over
    the identifiers the previous hop's memories named; chased rows rank just
    under the direct injections (0.9) so the direct hit stays first. Stops
    when a hop names nothing new, adds nothing new, or `cap` rows are held.
    Returns (rows added, hops walked). Fail-open: an error ends the walk."""
    added: list[Any] = []
    seen_all = list(seen)
    walked = 0
    have = list(ranked)
    for _ in range(max(0, int(hops))):
        idents = next_identifiers(frontier, seen=seen_all)
        if not idents:
            break
        seen_all.extend(idents)
        try:
            rows = await _inject_identifier_memories(search_input, idents, have + added)
        except Exception as e:  # noqa: BLE001 — additive, never fatal
            logger.debug("identifier chase stopped: %s", e)
            break
        walked += 1
        if not rows:
            break
        # Chunk rows have their own cap; sharing the fact cap would let the
        # facts fill it first.
        from server.settings import settings as _chase_settings

        _chunk_cap = int(_chase_settings.memory_ask_identifier_chase_chunk_cap)
        raw_rows = list(rows)
        facts_added = sum(1 for r in added if not _is_chunk_hit(r))
        chunks_added = sum(1 for r in added if _is_chunk_hit(r))
        fact_rows = [r for r in raw_rows if not _is_chunk_hit(r)][: max(0, int(cap) - facts_added)]
        chunk_rows = [r for r in raw_rows if _is_chunk_hit(r)][: max(0, _chunk_cap - chunks_added)]
        rows = fact_rows + chunk_rows
        if not rows:
            # The caps left nothing to add: the hop did no work.
            walked -= 1
            break
        for r in rows:
            try:
                r.rank_score = 0.9
                r.similarity = 0.9
                r.explanation = f"identifier chase hop {walked}"
            except Exception:  # noqa: BLE001 — a frozen hit type still counts
                pass
        added.extend(rows)
        facts_now = sum(1 for r in added if not _is_chunk_hit(r))
        chunks_now = sum(1 for r in added if _is_chunk_hit(r))
        if facts_now >= int(cap) and (
            chunks_now >= _chunk_cap or not any(_is_chunk_hit(r) for r in raw_rows)
        ):
            break
        frontier = rows
    return added, walked


def _identifier_groups(idents: list[str]) -> list[list[str]]:
    """Fetch groups for the exact-identifier read: the salient
    identifiers (bare numbers and @handles — each names one thing) as one
    group first, then the remaining shapes as another. A group is never empty.
    Pure."""
    salient = [i for i in idents if str(i).lstrip("@").isdigit() or str(i).startswith("@")]
    other = [i for i in idents if i not in salient]
    return [g for g in (salient, other) if g]


_DIRECTIVE_Q_RE = re.compile(
    r"\b(expectations?|directives?|doctrine|rules?|lessons?|conventions?|dispositions?|"
    r"how should|what should)\b",
    re.IGNORECASE,
)

_DECISIVE_MARKERS = [
    "root cause",
    "closes #",
    "fixes #",
    "the fix",
    "fixed by",
    "fixed in",
    "fix is",
    "backtrace",
    "stack trace",
    "gdb",
    "livelock",
    "deadlock",
    "starvation",
    "reverted",
    "refute",
    "the bug is",
    "because",
    "verdict",
]


def _injection_limits(
    query: str, base_facts: int, base_chunks: int, set_facts: int
) -> tuple[int, int]:
    """Row limits for the exact-identifier fetch: the
    single-fact defaults, or the set limits when the question asks for a SET
    about the identifier — a set question by shape (set_expansion) or a
    directive / expectation / disposition question. Pure."""
    from server.memory.domain import set_expansion as _se

    q = query or ""
    if set_facts > 0 and (_se.is_set_question(q) or _DIRECTIVE_Q_RE.search(q)):
        return max(base_facts, set_facts), max(base_chunks, base_chunks * 2)
    return base_facts, base_chunks


async def _read_decisive_chunks(
    search_input: Any,
    idents: list[str],
    qvec: list[float],
    case_insensitive: bool,
    settings: Any,
) -> list[Any]:
    """The decisive chunks of a bug thread — the ones that cite
    the identifier AND carry fix-or-cause vocabulary — get their own read; by
    cosine alone they rank at the tail behind the thread's status chatter.
    [] when the knob is 0 or the question is not bug-shaped; fail-open."""
    import server.memory.store as memory_repo

    n = int(settings.memory_ask_decisive_chunks)
    q = getattr(search_input, "query", "") or ""
    if n <= 0 or not is_decisive_question(q):
        return []
    try:
        return await _plain_chunks(
            memory_repo.find_chunks_by_identifier,
            org=getattr(search_input, "org", ""),
            namespace=getattr(search_input, "namespace", ""),
            identifiers=idents,
            limit=n,
            query_embedding=qvec or None,
            case_insensitive=case_insensitive,
            scope=getattr(search_input, "scope", "private"),
            agent=getattr(search_input, "agent", "*"),
            user_id=getattr(search_input, "user_id", None),
            markers=list(_DECISIVE_MARKERS),
        )
    except Exception as e:  # noqa: BLE001 — additive
        logger.debug("decisive chunk injection failed: %s", e)
        return []


async def _read_latest_word(
    search_input: Any, idents: list[str], case_insensitive: bool, settings: Any
) -> list[Any]:
    """The latest word — the newest facts and chunks that cite
    the identifier, for a question about the current position. [] when the
    knob is 0 or the question is not of that shape; fail-open."""
    import server.memory.store as memory_repo

    n = int(settings.memory_ask_identifier_inject_recent)
    q = getattr(search_input, "query", "") or ""
    if n <= 0 or not is_latest_word_question(q):
        return []
    rows: list[Any] = []
    try:
        for group in _identifier_groups(idents):
            rows += list(
                await memory_repo.find_by_identifier(
                    org=getattr(search_input, "org", ""),
                    namespace=getattr(search_input, "namespace", ""),
                    identifiers=group,
                    limit=n,
                    query_embedding=None,
                    case_insensitive=case_insensitive,
                    scope=getattr(search_input, "scope", "private"),
                    agent=getattr(search_input, "agent", "*"),
                    user_id=getattr(search_input, "user_id", None),
                    order="recency",
                )
            )
        rows += await _plain_chunks(
            memory_repo.find_chunks_by_identifier,
            org=getattr(search_input, "org", ""),
            namespace=getattr(search_input, "namespace", ""),
            identifiers=idents,
            limit=n,
            query_embedding=None,
            case_insensitive=case_insensitive,
            scope=getattr(search_input, "scope", "private"),
            agent=getattr(search_input, "agent", "*"),
            user_id=getattr(search_input, "user_id", None),
            order="recency",
        )
    except Exception as e:  # noqa: BLE001 — additive
        logger.debug("recent identifier injection failed: %s", e)
    return rows


async def _inject_identifier_memories(
    search_input: Any, idents: list[str], ranked: list[Any], *, case_insensitive: bool = False
) -> list[Any]:
    """New _MemorySearchHit objects for memories that cite `idents` but are
    absent from `ranked` (deduped by memory_key). Loom's lexical leg can't
    match bare PR/issue numbers, so this exact-substring fetch is the only way
    those memories reach synthesis. Fail-open to [] — injection is additive.

    NB: we do NOT skip when the identifier already appears in `ranked`. A thin
    memory ("User's PR number is 107091") often ranks in semantically and would
    "cover" the identifier by mere string presence, suppressing injection of the
    substantive memories that actually cite it (root-cause triage, CR summary).
    So we always fetch the most-substantive identifier memories and let the
    dedup-by-key below add only those not already present."""
    try:
        import server.memory.store as memory_repo

        # Embed the query so find_by_identifier ranks an entity's many facts by
        # relevance to THIS query, not by length (a reviewer with 160 memories
        # otherwise injects its 10 longest, not the on-topic ones — dilution).
        qvec: list[float] = []
        try:
            from server.embedding import get_embedder

            q = getattr(search_input, "query", "") or ""
            if q:
                qvec = list(await get_embedder().embed(q))
        except Exception:  # noqa: BLE001 — relevance ranking is best-effort
            qvec = []
        # A fresh namespace's rows may sit on a replica this query has not seen
        # yet (SharedMergeTree): the main leg read with sequential
        # consistency, and this exact-substring read feeds the same answer — a
        # miss here is 'the option was never stated'.
        from contextlib import nullcontext

        from server.core.ch import sequential_reads
        from server.observability.otel import traced as _traced

        # The injection reads are the ask path's largest read (about 45% of
        # ask bytes on a production workload), so they get a span and a counter.
        with (
            _traced("ch.query.identifier_injection", identifiers=len(idents)),
            sequential_reads() if memory_repo.read_your_writes_enabled() else nullcontext(),
        ):
            # An exact identifier gets its own read. The fetch
            # OR-s its identifiers, orders by cosine and takes eight, so one
            # broad co-identifier ("GitHub") fills the eight with rows that
            # never name the number. Salient identifiers (numbers, @handles)
            # and the other shapes are fetched as separate groups.
            from server.settings import settings as _lim_settings

            _fact_limit, _chunk_limit = _injection_limits(
                getattr(search_input, "query", "") or "",
                8,
                int(_lim_settings.memory_ask_identifier_inject_chunks),
                int(_lim_settings.memory_ask_identifier_inject_set_facts),
            )
            rows: list[Any] = []
            for _group in _identifier_groups(idents):
                _got = await memory_repo.find_by_identifier(
                    org=getattr(search_input, "org", ""),
                    namespace=getattr(search_input, "namespace", ""),
                    identifiers=_group,
                    limit=_fact_limit,
                    query_embedding=qvec or None,
                    case_insensitive=case_insensitive,
                    # Identity eligibility must match the main retrieval leg — else the
                    # injected rows (prepended to synthesis at rank 1.0) can carry another
                    # agent's/user's private or expired memories on a shared namespace.
                    scope=getattr(search_input, "scope", "private"),
                    agent=getattr(search_input, "agent", "*"),
                    user_id=getattr(search_input, "user_id", None),
                )
                rows = list(rows) + list(_got or [])
            # The same exact-substring read over verbatim chunks.

            _n_chunks = _chunk_limit
            if _n_chunks > 0:
                try:
                    rows = list(rows) + await _plain_chunks(
                        memory_repo.find_chunks_by_identifier,
                        org=getattr(search_input, "org", ""),
                        namespace=getattr(search_input, "namespace", ""),
                        identifiers=idents,
                        limit=_n_chunks,
                        query_embedding=qvec or None,
                        case_insensitive=case_insensitive,
                        scope=getattr(search_input, "scope", "private"),
                        agent=getattr(search_input, "agent", "*"),
                        user_id=getattr(search_input, "user_id", None),
                    )
                except Exception as e:  # noqa: BLE001 — chunks are additive too
                    logger.debug("identifier chunk injection failed: %s", e)
            rows = list(rows) + await _read_decisive_chunks(
                search_input, idents, qvec, case_insensitive, _lim_settings
            )
            rows = list(rows) + await _read_latest_word(
                search_input, idents, case_insensitive, _lim_settings
            )
    except Exception as e:  # noqa: BLE001 — additive, never fatal
        logger.debug("identifier injection failed: %s", e)
        return []

    have = {getattr(h, "memory_key", "") for h in ranked}
    out: list[Any] = []
    for r in rows:
        k = str(r.get("memory_key") or "")
        if not k or k in have:
            continue
        have.add(k)
        try:
            hit = _MemorySearchHit(
                memory_key=k,
                memory_type=str(r.get("memory_type") or "semantic"),
                content_excerpt=str(r.get("content_excerpt") or ""),
                distance=0.0 if not k.startswith("chunk:") else 0.05,
                similarity=1.0 if not k.startswith("chunk:") else 0.95,
                # Injected chunks rank just under the injected facts.
                rank_score=1.0 if not k.startswith("chunk:") else 0.95,
                source="chunk" if k.startswith("chunk:") else "fact",
                # The fact's own clock (valid_at), not the ingest wall-clock:
                # the latest-statement verifier compares this against the
                # `[user @ ...]` turn stamps in chunks, and created_at is
                # always later than every turn.
                valid_at=str(r.get("valid_at") or r.get("created_at") or "")[:19],
                version=int(r.get("version") or 0),
            )
        except Exception:  # noqa: BLE001 — skip a row that won't validate
            continue
        hit._row_identity = row_identity(r)  # the row read (none for a chunk), for the authority
        out.append(hit)
    try:
        from server.core import request_work as _rw

        _n_chunk_rows = sum(
            1 for h in out if str(getattr(h, "memory_key", "")).startswith("chunk:")
        )
        _rw.note_injected(facts=len(out) - _n_chunk_rows, chunks=_n_chunk_rows)
    except Exception:  # noqa: BLE001 — a counter never fails an answer
        pass
    return out
