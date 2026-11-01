"""What each served hit carries: bodies (unit, source, excerpt, none), resolved
relative dates, and the opt-in tags and reliance counters."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from server.core.ch import get_client
from server.identity.app.request_scope import request_scope
from server.memory.app.search.request import _apply_explain_default
from server.memory.app.search.tiers import _reader_user_id
from server.memory.app.search_tag_filter import read_row_tags as _read_row_tags
from server.memory.domain.sensitivity import reads_high
from server.memory.store.shared import SHARED_AGENT

log = logging.getLogger(__package__)


def _resolve_relative_words(hits: list[Any]) -> None:
    """Rewrite each fact hit's excerpt so a relative day phrase reads as the
    resolved EVENT date the extractor anchored (temporal_anchor). Facts only:
    a chunk is verbatim conversation and keeps the speaker's words, and a
    session summary's only date is the session's (valid_at), against which
    "yesterday" would resolve a day late. In place; never raises."""
    from server.memory.domain.extraction_temporal import resolve_relative_phrase

    for h in hits:
        try:
            if str(getattr(h, "source", "") or "fact") != "fact":
                continue
            anchor = str(getattr(h, "temporal_anchor", "") or "")
            if not anchor:
                continue
            text = str(getattr(h, "content_excerpt", "") or "")
            # No session date here: valid_at is the anchor for anchored
            # facts. The write-time note carries the session-relative words.
            new = resolve_relative_phrase(text, anchor)
            if new != text:
                h.content_excerpt = new
        except Exception:  # noqa: BLE001 — a rendering nicety never breaks a search
            continue


# `include_usage`: the served fact keys' newest live row per key (the row recall credit
# lands on), its pin and write-time counters, and every live row id of the key so the
# ledger sums cover the session copies too — the copies the read may see: a user- or
# session-scoped read sums only its own user's (session's) rows.
def _usage_rows_sql(input: Any) -> tuple[str, dict[str, Any]]:
    scope = ""
    params: dict[str, Any] = {}
    if getattr(input, "user_id", None) is not None:
        scope += " AND user_id = {uid:String}"
        params["uid"] = str(input.user_id)
    if getattr(input, "session_id", None) is not None:
        scope += " AND session_id = {sid:String}"
        params["sid"] = str(input.session_id)
    sql = f"""
    SELECT namespace, memory_key,
           groupArray(toString(memory_id)) AS mids,
           max(pinned) AS pinned,
           max(evidence_count) AS evidence_count,
           max(reinforcement_count) AS reinforcement_base
    FROM loom.memory FINAL
    WHERE org = {{org:String}} AND namespace IN {{nss:Array(String)}}
      AND memory_key IN {{keys:Array(String)}} AND tombstoned_at IS NULL{scope}
    GROUP BY namespace, memory_key
"""
    return sql, params


# The usage ledger's sums for those ids: a primary-key read on
# (org, namespace, memory_id); `recall` (the bulk of the table) is not summed.
_USAGE_COUNTS_SQL = """
    SELECT namespace, toString(memory_id) AS mid,
           sumIf(event_count, event_type = 'used') AS used_count,
           sumIf(event_count, event_type = 'used_cited') AS used_cited_count,
           sumIf(event_count, event_type = 'harm') AS harm_count,
           sumIf(event_count, event_type = 'evidence') AS evidence_count,
           sumIf(event_count, event_type = 'acted') AS acted_count
    FROM loom.memory_usage_events
    WHERE org = {org:String} AND namespace IN {nss:Array(String)}
      AND memory_id IN {ids:Array(UUID)}
      AND event_type IN ('used', 'used_cited', 'harm', 'evidence', 'acted')
    GROUP BY namespace, memory_id
"""


_USAGE_COUNTERS = ("used_count", "used_cited_count", "harm_count", "evidence_count", "acted_count")


def _blank_usage(h: Any) -> None:
    for name in _USAGE_COUNTERS:
        setattr(h, name, 0)
    h.reinforcement_count = 0
    h.pinned = False


async def _fill_hit_usage(hits: list[Any], input: Any, namespaces: list[str] | None = None) -> int:
    """Under `include_usage`, every served hit carries its reliance counters —
    `reinforcement_count` (the folded reinforcement signal the fact leg ranks on,
    `greatest(folded, write-time base)` like the leg), `used_count` (an agent's label),
    `used_cited_count` (memory.ask's own credit), `harm_count`, `evidence_count`,
    `acted_count` and `pinned`. Three keyed statements over the served fact keys only; chunk /
    digest / derived hits read 0 / false. Off (the default) this touches nothing, so a client
    that never asked sees its payload unchanged. Fail-open: a failed read leaves zeros rather
    than failing the search."""
    if not bool(getattr(input, "include_usage", False)) or not hits:
        return 0
    for h in hits:
        _blank_usage(h)
    facts = [
        h
        for h in hits
        if (getattr(h, "source", "fact") or "fact") == "fact"
        and not str(getattr(h, "memory_key", "") or "").startswith("chunk:")
    ]
    if not facts:
        return 0
    nss = sorted(
        {str(getattr(h, "namespace", "") or input.namespace) for h in facts} | set(namespaces or [])
    )
    keys = sorted({str(h.memory_key) for h in facts})
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    counts_by_key: dict[tuple[str, str], dict[str, int]] = {}
    folded_by_key: dict[tuple[str, str], int] = {}
    try:
        ch = get_client()
        rows_sql, scope_params = _usage_rows_sql(input)
        rows = await ch.query_all(
            rows_sql, {"org": input.org, "nss": nss, "keys": keys, **scope_params}
        )
        key_of_mid: dict[tuple[str, str], tuple[str, str]] = {}
        for r in rows:
            ns, key = str(r["namespace"]), str(r["memory_key"])
            rows_by_key[(ns, key)] = r
            for mid in r.get("mids") or []:
                key_of_mid[(ns, str(mid))] = (ns, key)
        if key_of_mid:
            counts = await ch.query_all(
                _USAGE_COUNTS_SQL,
                {"org": input.org, "nss": nss, "ids": [m for _ns, m in key_of_mid]},
            )
            for c in counts:
                owner = key_of_mid.get((str(c["namespace"]), str(c["mid"])))
                if owner is None:
                    continue
                acc = counts_by_key.setdefault(owner, dict.fromkeys(_USAGE_COUNTERS, 0))
                for name in _USAGE_COUNTERS:
                    acc[name] += int(c.get(name) or 0)
        from server.memory.app.reinforcement import fetch_reinforcement

        for ns in sorted({ns for ns, _k in rows_by_key}):
            folded = await fetch_reinforcement(
                ch,
                org=input.org,
                namespace=ns,
                memory_keys=[k for n, k in rows_by_key if n == ns],
            )
            for (_agent, uid, key), (count, _last) in folded.items():
                if "uid" in scope_params and str(uid) != scope_params["uid"]:
                    continue  # another user's reinforcement of the key
                prev = folded_by_key.get((ns, key), 0)
                folded_by_key[(ns, key)] = max(prev, int(count))
    except Exception as e:  # noqa: BLE001 — counters are additive, never a failure
        log.warning("memory.search: include_usage read failed: %s", e)
        return 0
    filled = 0
    for h in facts:
        ident = (str(getattr(h, "namespace", "") or input.namespace), str(h.memory_key))
        row = rows_by_key.get(ident)
        if row is None:
            continue
        summed = counts_by_key.get(ident) or {}
        for name in _USAGE_COUNTERS:
            setattr(h, name, int(summed.get(name) or 0))
        # The promote gate's counter carries a +1 write-time credit on the
        # row; the ledger's sum is what the fold adds. Serve the larger.
        h.evidence_count = max(int(row.get("evidence_count") or 0), int(h.evidence_count or 0))
        h.reinforcement_count = max(
            int(row.get("reinforcement_base") or 1), folded_by_key.get(ident, 0)
        )
        h.pinned = bool(int(row.get("pinned") or 0))
        filled += 1
    return filled


async def _fill_optional_hit_fields(
    hits: list[Any], input: Any, namespaces: list[str] | None = None
) -> dict[str, int]:
    """The opt-in hit fields — `tags` and the reliance counters — each read only when its flag
    is set; the returned counters land on `retrieval_work`."""
    work: dict[str, int] = {}
    if getattr(input, "include_tags", False):
        work["tags_filled"] = await _fill_hit_tags(hits, input, namespaces)
    if getattr(input, "include_usage", False):
        work["usage_filled"] = await _fill_hit_usage(hits, input, namespaces)
    return work


async def _fill_hit_tags(hits: list[Any], input: Any, namespaces: list[str] | None = None) -> int:
    """Under `include_tags`, every served hit carries `tags`.
    Fact hits from the dense leg already have them (the leg selects `e.tags`);
    the hits another leg produced (lexical, key-match, anchor) are point-read
    here in one statement over the served keys only, and chunk / digest /
    derived hits read as []. Off (the default) this touches nothing, so a
    client that never asked sees its payload unchanged. Fail-open: a failed
    read leaves [] on the gaps rather than failing the search."""
    if not bool(getattr(input, "include_tags", False)) or not hits:
        return 0
    # A hit whose leg read the row's tags (`_row_tags`) needs no
    # point read; only rows no leg stamped are read here.
    gaps = [
        h
        for h in hits
        if getattr(h, "tags", None) is None
        and getattr(h, "_row_tags", None) is None
        and (getattr(h, "source", "fact") or "fact") == "fact"
        and not str(getattr(h, "memory_key", "") or "").startswith("chunk:")
    ]
    by_key: dict[tuple[str, str], list[str]] = {}
    if gaps:
        nss = sorted(
            {str(getattr(h, "namespace", "") or input.namespace) for h in gaps}
            | set(namespaces or [])
        )
        try:
            by_key = await _read_row_tags(input.org, nss, [str(h.memory_key) for h in gaps])
        except Exception as e:  # noqa: BLE001 — tags are additive, never a failure
            log.warning("memory.search: include_tags read failed: %s", e)
            by_key = {}
    filled = 0
    for h in hits:
        if getattr(h, "tags", None) is not None:
            continue
        ns = str(getattr(h, "namespace", "") or input.namespace)
        stamped = getattr(h, "_row_tags", None)
        got = list(stamped) if stamped is not None else by_key.get((ns, str(h.memory_key)))
        try:
            h.tags = list(got) if got else []
        except Exception:  # noqa: BLE001
            continue
        if got:
            filled += 1
    return filled


async def _expand_unit_bodies(
    hits: list[Any], input: Any, prefer_entities: list[str] | None = None
) -> tuple[int, int, int]:
    """bodies="unit": replace each hit's clip with
    the STORED VALUE, in rank order, until `body_budget_tokens` (len/4) is
    spent; hits past the budget keep the clip, get body_truncated=True and a
    `next` = the memory.get call that fetches the rest. Only rows of
    loom.memory qualify (a chunk / summary / code hit keeps its excerpt).
    One bulk read per distinct (namespace, identity). Fail-open: a read error
    leaves every hit as it was. Returns (bodies_returned, bodies_elided,
    tokens_returned)."""
    if (getattr(input, "bodies", "full") or "full") not in ("unit", "source") or not hits:
        return (0, 0, 0)
    from server.memory.store import Identity, get_many_by_keys

    budget = int(getattr(input, "body_budget_tokens", 4000) or 4000)
    read_sid = str(getattr(input, "session_id", None) or "-")

    def _sid(h: Any) -> str:
        # The body lives under the session the row was WRITTEN in (the hit carries it). Search
        # fans out across sessions, so a point read under the caller's session misses every row
        # another session wrote — and silently: no body returned, no flag.
        return str(getattr(h, "session_id", "") or "") or read_sid

    def _writer(h: Any) -> tuple[str, str, str, str]:
        # A hit that names its row is read under that row's identity, so two writers of
        # one key in one session each get their own body; otherwise under the caller's.
        # A shared row names no row of loom.memory, so it is read as one that names none.
        ns = str(getattr(h, "namespace", "") or input.namespace)
        who = getattr(h, "_row_identity", None)
        if who and who[0] != SHARED_AGENT:
            return (ns, *who)
        return (ns, str(getattr(input, "agent", "*") or "*"), _reader_user_id(input), _sid(h))

    by_writer: dict[tuple[str, str, str, str], list[Any]] = {}
    for h in hits:
        if str(getattr(h, "source", "fact") or "fact") in ("fact", "memory", "record"):
            by_writer.setdefault(_writer(h), []).append(h)

    async def _read(writer: tuple[str, str, str, str], group: list[Any]) -> list[Any]:
        ns, agent, user_id, sid = writer
        try:
            return await get_many_by_keys(
                Identity(org=input.org, namespace=ns, agent=agent, user_id=user_id, session_id=sid),
                [str(h.memory_key) for h in group],
                scope=request_scope(),
                exclude_high_sensitivity=True,
            )
        except Exception as e:  # noqa: BLE001 — a body read never fails a search
            log.debug("unit bodies read failed for %s/%s: %s", ns, sid, e)
            return []

    # one read per writer the hits span, overlapped
    reads = await asyncio.gather(*(_read(w, group) for w, group in by_writer.items()))
    # A hit that names no row is read under the caller's identity, where the read picks one
    # row per key after leaving 'high' rows out, so the plain row the search matched answers.
    stored: dict[tuple[str, ...], Any] = {
        (*writer, str(r.memory_key)): r
        for writer, rows in zip(by_writer, reads, strict=True)
        for r in rows
    }

    def _body_row(h: Any) -> Any:
        return stored.get((*_writer(h), str(h.memory_key)))

    returned = elided = tokens = 0
    spent = 0
    # When the query names entities, the budget buys one whole
    # body per cited entity before it buys a second body for any of them — a
    # constraint that cites three earlier steps needs all three, not the top
    # one twice. Output order is untouched; only the spend order changes.
    order = list(hits)
    if prefer_entities:
        first: list[Any] = []
        for ent in prefer_entities:
            el = str(ent).lower()
            for h in hits:
                if h in first:
                    continue
                if el in {str(x).lower() for x in (getattr(h, "entities", None) or [])}:
                    first.append(h)
                    break
        order = first + [h for h in hits if h not in first]
    for h in order:
        if str(getattr(h, "source", "fact") or "fact") not in ("fact", "memory", "record"):
            continue
        body_row = _body_row(h)
        if body_row is None:
            # The row was not readable under any session we could name: say so. A clip that
            # claims to be the whole body is an invisible failure; body_truncated + next make it
            # visible and give the caller the one call that fetches it.
            try:
                if len(str(getattr(h, "content_excerpt", "") or "")) >= 1200:
                    h.body_truncated = True
                    h.next = f'memory.get(memory_key="{h.memory_key}")'
                    elided += 1
            except Exception:  # noqa: BLE001
                pass
            continue
        v = body_row.value
        text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
        cost = max(1, len(text) // 4)
        if spent + cost <= budget:
            try:
                h.content_excerpt = text
                h.body_truncated = False
            except Exception:  # noqa: BLE001
                continue
            spent += cost
            returned += 1
            tokens += cost
        else:
            try:
                h.body_truncated = True
                h.next = f'memory.get(memory_key="{h.memory_key}")'
            except Exception:  # noqa: BLE001
                continue
            elided += 1
    return (returned, elided, tokens)


async def _expand_source_bodies(
    hits: list[Any], input: Any, prefer_entities: list[str] | None = None
) -> tuple[int, int]:
    """bodies="source": a fact hit that links to a source
    evidence row is served that passage — the trace, the transcript turn, the
    tool output the fact was extracted from — instead of the fact. One passage
    per evidence id (the first hit by rank that links to it; later hits keep
    their fact text), spent against `body_budget_tokens` (len/4) in rank
    order, cited entities' hits first as `_expand_unit_bodies` does. The
    evidence read carries the request's run. Fail-open.
    Returns (passages_served, hits_linked_but_over_budget)."""
    if (getattr(input, "bodies", "full") or "full") != "source" or not hits:
        return (0, 0)
    fact_hits = [
        h
        for h in hits
        if str(getattr(h, "source", "fact") or "fact") in ("fact", "memory", "record")
    ]
    if not fact_hits:
        return (0, 0)
    namespaces = sorted({str(getattr(h, "namespace", "") or input.namespace) for h in fact_hits})
    keys = sorted({str(h.memory_key) for h in fact_hits})
    run_id = str(getattr(input, "run_id", "") or "")
    try:
        ch = get_client()
        # No FINAL on either read. A link is written once per (memory_key, evidence_id) and an
        # evidence row's id is minted per insert, so there is nothing for FINAL to collapse except
        # a tombstone re-insert; FINAL still merges every selected granule across parts and reads
        # the extra key columns (ClickHouse, SELECT … FINAL). Measured over the same 8 granules:
        # 20.28 MiB → 2.71 MiB for the evidence read, 161 KiB → 69 KiB for the links. GROUP BY
        # does the collapse; the latest version (created_at) decides the tombstone.
        # Every live link of each key, oldest first: a key's oldest passage may be another
        # user's or session's (the link table has no identity columns), so the read below is
        # scoped to the request's user / session and the next link stands in.
        links = await ch.query_all(
            """
            SELECT namespace, memory_key, toString(evidence_id) AS evidence_id,
                   min(created_at) AS first_at,
                   argMin(char_start, created_at) AS char_start,
                   argMin(char_end, created_at) AS char_end
            FROM loom.memory_belief_sources
            WHERE org = {org:String} AND namespace IN {nss:Array(String)}
              AND memory_key IN {keys:Array(String)} AND tombstoned_at IS NULL
            GROUP BY namespace, memory_key, evidence_id
            ORDER BY first_at, evidence_id
            """,
            {"org": input.org, "nss": namespaces, "keys": keys},
        )
        linked: dict[tuple[str, str], list[tuple[str, tuple[int, int]]]] = {}
        for r in links:
            k = (str(r["namespace"]), str(r["memory_key"]))
            span = (int(r.get("char_start") or 0), int(r.get("char_end") or 0))
            linked.setdefault(k, []).append((str(r["evidence_id"]), span))
        if not linked:
            return (0, 0)
        scope_sql = ""
        params: dict[str, Any] = {
            "org": input.org,
            "nss": namespaces,
            "ids": sorted({eid for ls in linked.values() for eid, _ in ls}),
        }
        if run_id:
            scope_sql += " AND run_id = {run_id:String}"
            params["run_id"] = run_id
        # The read scope the hits were served under binds their passages too.
        if getattr(input, "user_id", None) is not None:
            scope_sql += " AND user_id = {uid:String}"
            params["uid"] = str(input.user_id)
        if getattr(input, "session_id", None) is not None:
            scope_sql += " AND session_id = {sid:String}"
            params["sid"] = str(input.session_id)
        rows = await ch.query_all(
            f"""
            SELECT toString(evidence_id) AS eid,
                   argMax(content, created_at) AS body,
                   argMax(tombstoned_at, created_at) AS tomb_latest
            FROM loom.memory_evidence
            WHERE org = {{org:String}} AND namespace IN {{nss:Array(String)}}
              AND evidence_id IN {{ids:Array(UUID)}}{scope_sql}
            GROUP BY evidence_id
            HAVING tomb_latest IS NULL
            """,
            params,
        )
        # A passage has no sensitivity of its own; one that reads as `high` is not served.
        passages = {
            str(r["eid"]): str(r["body"] or "")
            for r in rows
            if not reads_high(str(r["body"] or ""))
        }
        by_key: dict[tuple[str, str], str] = {}
        spans: dict[tuple[str, str], tuple[int, int]] = {}
        for k, ls in linked.items():
            for linked_eid, span in ls:
                if passages.get(linked_eid):
                    by_key[k], spans[k] = linked_eid, span
                    break
    except Exception as e:  # noqa: BLE001 — a body read never fails a search
        log.debug("source bodies read failed: %s", e)
        return (0, 0)
    budget = int(getattr(input, "body_budget_tokens", 4000) or 4000)
    order = list(fact_hits)
    if prefer_entities:
        first: list[Any] = []
        for ent in prefer_entities:
            el = str(ent).lower()
            for h in fact_hits:
                if h not in first and el in {
                    str(x).lower() for x in (getattr(h, "entities", None) or [])
                }:
                    first.append(h)
                    break
        order = first + [h for h in fact_hits if h not in first]
    served: set[str] = set()
    spent = n_served = n_over = 0
    span_chars = int(getattr(input, "source_span_chars", 0) or 0)
    for h in order:
        ns = str(getattr(h, "namespace", "") or input.namespace)
        eid = by_key.get((ns, str(h.memory_key)))
        if not eid or eid in served:
            continue
        text = passages.get(eid, "")
        if not text:
            continue
        # A capsule around the span the fact came from, when the caller asked for one and the
        # link carries a span.
        cs, ce = spans.get((ns, str(h.memory_key)), (0, 0))
        if span_chars > 0 and ce > cs and len(text) > span_chars:
            mid = (cs + ce) // 2
            lo = max(0, min(mid - span_chars // 2, len(text) - span_chars))
            text = text[lo : lo + span_chars]
        cost = max(1, len(text) // 4)
        if spent + cost > budget:
            n_over += 1
            continue
        try:
            h.content_excerpt = text
            h.body_truncated = False
            h.score_components = {**(getattr(h, "score_components", {}) or {}), "source_body": 1.0}
        except Exception:  # noqa: BLE001
            continue
        served.add(eid)
        spent += cost
        n_served += 1
    return (n_served, n_over)


def _apply_bodies(hits: list[Any], input: Any) -> None:
    """Apply the request's `bodies` mode in place: "full" is a no-op; "excerpt" clips each
    body to LOOM_MEMORY_SEARCH_EXCERPT_CHARS; "none" blanks it. Keys, types and scores are
    untouched, so a caller can still cite and memory.get what it needs. The wire tier of the
    per-hit explanation is decided here too — both are "what a hit carries", on the main and
    the read-your-writes path alike."""
    _apply_explain_default(hits, input)
    bodies = getattr(input, "bodies", "full") or "full"
    if bodies in ("full", "unit", "source") or not hits:
        return
    from server.settings import settings as _s

    limit = int(_s.memory_search_excerpt_chars)
    for h in hits:
        body = getattr(h, "content_excerpt", "") or ""
        if bodies == "none":
            new = ""
        else:
            new = body if len(body) <= limit else body[: limit - 1] + "…"
        if new != body:
            try:
                h.content_excerpt = new
            except Exception:  # noqa: BLE001
                pass
