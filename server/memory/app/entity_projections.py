"""memory.set entity projection, derived facts and triples-follow-rows for the
memory write path. The entity edge / triple / vector writers they reuse live
in server.memory.app.projections.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from server.core.ch import get_client
from server.core.datetimes import naive_utc_now, parse_iso_dt
from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry, _BulkSetResult
from server.memory.app.projections import (
    close_triples,
    entity_triple_row,
    resolve_projection_entity_map,
    write_entity_edges,
)

log = logging.getLogger(__name__)


# --- Deterministic LLM-free entity projection for memory.set ---
# memory.set carries no `entities` column, so a point write is invisible to
# the graph until a bulk path or worker re-extracts it. This closes the gap
# without an LLM call: tokenize the value text into candidate phrases,
# canonicalize through the same `canonical_entity_text` as write_entity_edges
# (so write/read land on one node), and reuse write_entity_edges for the
# pairing / cap / temporal-window / insert path.

# Min length 3 skips "is/at/of" noise; the canonicalizer collapses i/me/my -> user.
_SET_ENTITY_MIN_LEN = 3
_SET_ENTITY_MAX_CANDIDATES = 24
# Words that cannot denote a thing: closed-class function words (prepositions,
# determiners, quantifiers, wh-words, conjunctions, pronouns) plus high-frequency
# degree/manner adverbs. Domain stopwords are deliberately NOT here —
# over-filtering drops real entities ("record", "show" as products; "bug", "fix"
# and "column" are legitimately the busiest nodes in a code-review namespace).
# Measured on a real entity graph without the later groups: `why` was the 7th
# highest-degree node of 19,897 (degree 19,083), with `all` (17,431), `explicitly` (10,009)
# and `what` (7,851) close behind, and the 90 words below held 5.4% of all edge degree while
# being 0.5% of nodes — function words appear in nearly every memory, so they pair with
# everything and concentrate degree far above their information content.
# These are canonical lowercase forms. A multi-word proper noun containing one
# ("Down Syndrome", "Per Andersson") still survives as a phrase node via
# _SET_ENTITY_PROPER_RE, so filtering the bare token costs no real entity.
_SET_ENTITY_STOPWORDS = frozenset(
    {
        # -- core closed-class set
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "if",
        "then",
        "else",
        "for",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "do",
        "does",
        "did",
        "has",
        "have",
        "had",
        "will",
        "would",
        "can",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "they",
        "them",
        "their",
        "he",
        "she",
        "his",
        "her",
        "we",
        "us",
        "our",
        "you",
        "your",
        "not",
        "no",
        "yes",
        "so",
        "than",
        "too",
        "very",
        "just",
        "also",
        "about",
        "into",
        "over",
        "after",
        "before",
        "when",
        "while",
        "because",
        "there",
        "here",
        # -- further prepositions and particles
        "near",
        "between",
        "under",
        "above",
        "through",
        "throughout",
        "along",
        "around",
        "across",
        "among",
        "against",
        "behind",
        "below",
        "beneath",
        "beside",
        "beyond",
        "during",
        "within",
        "without",
        "toward",
        "towards",
        "upon",
        "onto",
        "via",
        "per",
        "since",
        "until",
        "off",
        "out",
        "down",
        # -- determiners, quantifiers, wh-words, conjunctions
        "all",
        "any",
        "each",
        "every",
        "both",
        "some",
        "such",
        "same",
        "other",
        "another",
        "more",
        "most",
        "less",
        "least",
        "many",
        "much",
        "few",
        "several",
        "own",
        "none",
        "who",
        "whom",
        "whose",
        "which",
        "what",
        "where",
        "why",
        "how",
        "whether",
        "although",
        "though",
        "however",
        "therefore",
        "unless",
        "whereas",
        # -- degree and manner adverbs
        "only",
        "even",
        "still",
        "yet",
        "again",
        "once",
        "ever",
        "never",
        "always",
        "often",
        "sometimes",
        "usually",
        "already",
        "almost",
        "quite",
        "rather",
        "perhaps",
        "maybe",
        "instead",
        "indeed",
        "thus",
        "hence",
        "otherwise",
        "explicitly",
        "implicitly",
        "simply",
        "actually",
        "basically",
        "essentially",
        "generally",
        "specifically",
        "particularly",
        "especially",
        "currently",
        "previously",
        "recently",
        "initially",
        "finally",
        "eventually",
        "immediately",
        "directly",
        "exactly",
        "clearly",
        "obviously",
        "apparently",
        "possibly",
        "probably",
        "certainly",
        "definitely",
    }
)
# Runs of TitleCase words are strong proper-noun candidates. Applied to the
# RAW value text before lowercasing so casing survives.
_SET_ENTITY_PROPER_RE = re.compile(r"\b[A-Z][\w'’.-]*(?:\s+[A-Z][\w'’.-]*)*")
_SET_ENTITY_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’.-]*")


def _candidate_entities_from_value(value: Any) -> list[str]:
    """Deterministic candidate-entity extraction from a stored memory value.

    Returns canonicalized entity texts (>= 2 chars), deduped,
    order-preserving, capped. Coerces value to text (dict/list via leaf
    scalars, skipping keys), grabs proper-noun phrases first, then stoplisted
    word tokens, and canonicalizes each so it lands on the same node a query
    seed would. Pure: no I/O, no LLM.
    """
    from server.memory.domain.graph_quality import canonical_entity_text

    surfaces: list[str] = []

    def _walk(v: Any) -> None:
        if isinstance(v, str):
            if v.strip():
                surfaces.append(v)
        elif isinstance(v, (int, float, bool)):
            return  # bare scalars carry no entity signal
        elif isinstance(v, dict):
            for vv in v.values():
                _walk(vv)
        elif isinstance(v, (list, tuple)):
            for vv in v:
                _walk(vv)

    _walk(value)
    text = " ".join(surfaces)
    if not text.strip():
        return []

    raw_candidates: list[str] = []
    for m in _SET_ENTITY_PROPER_RE.finditer(text):
        phrase = m.group(0).strip()
        if len(phrase) >= _SET_ENTITY_MIN_LEN:
            raw_candidates.append(phrase)
    for m in _SET_ENTITY_WORD_RE.finditer(text):
        tok = m.group(0)
        if len(tok) < _SET_ENTITY_MIN_LEN:
            continue
        if tok.lower() in _SET_ENTITY_STOPWORDS:
            continue
        raw_candidates.append(tok)

    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_candidates:
        canon = canonical_entity_text(raw)
        # Re-apply the stoplist AFTER canonicalization: a sentence-initial
        # "The" reaches the proper-noun grabber and canonicalizes to "the".
        if len(canon) < 2 or canon in seen or canon in _SET_ENTITY_STOPWORDS:
            continue
        seen.add(canon)
        out.append(canon)
        if len(out) >= _SET_ENTITY_MAX_CANDIDATES:
            break
    return out


def set_entities_for_projection(input: Any, value: Any) -> list[str]:
    """The graph nodes a point write projects. The caller's own `entities` win: they are what
    the writer said the row is about, and mining the text beside them would add every
    content word as a node. A trajectory transcript row is never text-mined — on one
    namespace 65% of all edges came from mining step transcripts, making `command`, `step`,
    `bash`, `after bash`, `revealed`, `content` its top entities. Otherwise the value is
    mined."""
    from server.memory.domain.graph_quality import canonical_entity_text
    from server.memory.domain.transcript_rows import is_transcript_row

    declared: list[str] = []
    for raw in getattr(input, "entities", None) or []:
        canon = canonical_entity_text(str(raw))
        if len(canon) >= 2 and canon not in declared:
            declared.append(canon)
    transcript = is_transcript_row(getattr(input, "tags", None), getattr(input, "source_trace", ""))
    if declared or transcript:
        return declared[:_SET_ENTITY_MAX_CANDIDATES]
    return _candidate_entities_from_value(value)


async def project_memory_set_entities(
    input: Any,  # MemorySetInput
    *,
    memory_key: str,
    value: Any,
    ch: Any = None,
    wait_visible: bool = False,
) -> int:
    """Fail-open entity projection for a single `memory.set`.

    Derives candidate entities from `value`, then reuses write_entity_edges
    for the same pairing / cap / window / insert path as the bulk write.
    Returns the count of distinct candidates projected (0 when <2).
    `wait_visible` is for the projection that runs beside the response: its
    edges are readable once it returns.
    """
    from server.observability.otel import traced

    with traced("entities.extract"):
        ents = set_entities_for_projection(input, value)
    if len(ents) < 2:
        # <2 entities = no co-occurrence pair. Lone nodes come from the
        # bulk/worker path; edges are what point-writes were missing.
        return 0

    with traced("entities.build_input", entities=len(ents)):
        bulk_like = MemoryBulkSetInput(
            org=input.org,
            namespace=input.namespace,
            agent=input.agent,
            user_id=input.user_id,
            session_id=input.session_id,
            entries=[
                _BulkSetEntry(
                    memory_key=memory_key,
                    value=value,
                    memory_type=getattr(input, "memory_type", "semantic"),
                    entities=ents,
                    valid_at=getattr(input, "valid_at", None),
                )
            ],
        )
    result = _BulkSetResult(memory_key=memory_key, version=0)
    with traced("entities.write_edges", entities=len(ents)):
        await write_entity_edges(
            bulk_like, [result], ch=ch or get_client(), wait_visible=wait_visible
        )
    return len(ents)


async def _write_derived_facts(
    input: MemoryBulkSetInput,
    results: list[_BulkSetResult],
    *,
    ch: Any,
    entity_map: dict[str, str] | None = None,
) -> None:
    """Persist per-occurrence derived operands to loom.derived_facts.

    ONE row per occurrence — never deduped/summed at write time — so
    cross-session SUM / COUNT / max-min(occurred_at) stay correct at read
    time. Mirrors write_entity_triples: shared canonicalizer, fail-open,
    async insert. Empty `derived` produces zero rows.
    """
    from server.memory.app.entity_resolution import apply_entity_resolution
    from server.memory.app.extract import MAX_DERIVED_PER_FACT
    from server.memory.domain.graph_quality import canonical_entity_text
    from server.memory.domain.search_text import canonicalize_verb

    now = naive_utc_now()
    rows: list[dict[str, Any]] = []
    # Same cap the extractor applies when emitting `derived`; a lower cap here would silently
    # drop the operands past it.
    _MAX_DERIVED_PER_MEMORY = MAX_DERIVED_PER_FACT

    if entity_map is None:
        objs: list[str] = []
        for entry in input.entries:
            for d in list(getattr(entry, "derived", None) or []):
                if isinstance(d, dict) and d.get("object"):
                    objs.append(str(d["object"]))
        entity_map = await resolve_projection_entity_map(input, objs, ch=ch)

    for entry, result in zip(input.entries, results, strict=True):
        derived = list(getattr(entry, "derived", None) or [])
        if not derived:
            continue
        _conf = float(getattr(entry, "confidence", 1.0) or 1.0)
        kept = 0
        for d in derived:
            if kept >= _MAX_DERIVED_PER_MEMORY:
                break
            if not isinstance(d, dict):
                continue
            predicate = canonicalize_verb(str(d.get("predicate", "")).strip())
            obj = apply_entity_resolution(str(d.get("object", "")).strip(), entity_map)
            if len(predicate) < 2 or len(obj) < 1:
                continue
            # subject defaults to "user"; the extractor may override for
            # third-party occurrences.
            subject = apply_entity_resolution(str(d.get("subject") or "user").strip(), entity_map)
            nv = d.get("numeric_value")
            try:
                numeric_value = None if nv is None else float(nv)
            except (TypeError, ValueError):
                numeric_value = None
            occurred_at = parse_iso_dt(d.get("occurred_at") or "") or parse_iso_dt(
                getattr(entry, "temporal_anchor", "") or ""
            )
            rows.append(
                {
                    "org": input.org,
                    "namespace": input.namespace,
                    "agent": input.agent or "",
                    "user_id": input.user_id or "",
                    "memory_key": result.memory_key,
                    "subject": subject,
                    "predicate": predicate,
                    "object": obj,
                    # Fold category surface variance via the same canonicalizer
                    # the object gets, so surfaces don't split read-time COUNT/SUM.
                    "category": canonical_entity_text(str(d.get("category") or "").strip()),
                    "numeric_value": numeric_value,
                    "unit": str(d.get("unit") or "").strip(),
                    "occurred_at": occurred_at,
                    "session_id": input.session_id or "",
                    "confidence": _conf,
                    "created_at": now,
                }
            )
            kept += 1

    if not rows:
        return

    await ch.insert(
        "loom.derived_facts",
        rows,
        settings={"async_insert": 1, "wait_for_async_insert": 0},
    )


async def follow_row_supersessions(
    input: Any, pairs: list[tuple[str, str]], *, ch: Any
) -> tuple[int, int]:
    """Triples follow rows: for every (superseded
    row, successor row) pair this write produced, close the open triples the
    superseded row attests and the successor does not, and write the
    successor triple under the predecessor's subject and relation when the
    two rows differ by one substituted span. One SELECT for the rows, one for
    the candidate open triples (bounded to the namespace and to triples whose
    subject and object both occur in the pair texts), one UPDATE for the
    closes, one INSERT for the successors. Returns (closed, inserted)."""
    from server.memory.domain import triples_follow_rows as tfr

    ids = sorted({i for pair in pairs for i in pair if i})
    if not ids:
        return (0, 0)
    rows = await ch.query_all(
        """
        SELECT toString(memory_id) AS id, memory_key, value, valid_at
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND toString(memory_id) IN {ids:Array(String)}
        SETTINGS select_sequential_consistency = 1
        """,
        params={"org": input.org, "ns": input.namespace, "ids": ids},
    )
    by_id = {str(r["id"]): r for r in rows}
    sups: list[tfr.RowSupersession] = []
    for loser_id, winner_id in pairs:
        lo, wi = by_id.get(str(loser_id)), by_id.get(str(winner_id))
        if not lo or not wi:
            continue
        sups.append(
            tfr.RowSupersession(
                loser_text=str(lo.get("value") or ""),
                winner_text=str(wi.get("value") or ""),
                winner_key=str(wi.get("memory_key") or ""),
                winner_valid_from=wi.get("valid_at"),
            )
        )
    if not sups:
        return (0, 0)
    text = " ".join(f"{s.loser_text} {s.winner_text}" for s in sups)
    open_triples = await ch.query_all(
        """
        SELECT subject, relation, object, confidence, provenance
        FROM loom.entity_triples
        WHERE org = {org:String} AND namespace = {ns:String}
          AND valid_to IS NULL AND invalid_at IS NULL
          AND positionCaseInsensitive({txt:String}, subject) > 0
          AND positionCaseInsensitive({txt:String}, object) > 0
        SETTINGS select_sequential_consistency = 1
        """,
        params={"org": input.org, "ns": input.namespace, "txt": text},
    )
    plan = tfr.plan_follow(list(open_triples or []), sups)
    if not plan.closes and not plan.inserts:
        return (0, 0)
    now = naive_utc_now()
    closed = 0
    if plan.closes:
        # valid_to = when the successor holds from, else now; invalid_at = now.
        pair_clock = next((s.winner_valid_from for s in sups if s.winner_valid_from), None)
        vt = pair_clock if isinstance(pair_clock, datetime) else now
        closes: list[tuple[str, str, str, datetime, datetime | None]] = [
            (s, r, o, vt, now) for s, r, o in plan.closes
        ]
        await close_triples(ch, org=input.org, namespace=input.namespace, closes=closes)
        closed = len(closes)
        try:
            from server.observability import audit as _audit

            for s, r, o in plan.closes[:16]:
                await _audit.emit(
                    event_type="triple.follows_row_supersession",
                    org=input.org,
                    namespace=input.namespace,
                    actor=str(getattr(input, "agent", "-") or "-"),
                    subject={"closed": f"{s} {r} {o}"},
                )
        except Exception as e:  # noqa: BLE001 — the closes are in; the trail is best effort
            log.debug("triples follow rows: audit skipped: %s", e)
    inserted = 0
    if plan.inserts:
        adapter = "row-supersession"
        t_rows: list[dict[str, Any]] = []
        m_rows: list[dict[str, Any]] = []
        for s, r, o, key, vf, conf, prov in plan.inserts:
            t_rows.append(
                entity_triple_row(
                    input.org,
                    input.namespace,
                    s,
                    r,
                    o,
                    provenance=prov,
                    confidence=conf,
                    adapter_name=adapter,
                    last_seen=now,
                    valid_from=vf if isinstance(vf, datetime) else None,
                )
            )
            if key:
                m_rows.append(
                    {
                        "org": input.org,
                        "namespace": input.namespace,
                        "subject": s,
                        "relation": r,
                        "object": o,
                        "memory_key": key,
                        "seen_at": now,
                        "confidence": conf,
                        "adapter_name": adapter,
                    }
                )
        await ch.insert(
            "loom.entity_triples", t_rows, settings={"async_insert": 1, "wait_for_async_insert": 1}
        )
        if m_rows:
            await ch.insert(
                "loom.entity_triple_memories",
                m_rows,
                settings={"async_insert": 1, "wait_for_async_insert": 1},
            )
        inserted = len(t_rows)
    log.info(
        "triples follow rows: %d pair(s) → closed %d, wrote %d successor triple(s)",
        len(sups),
        closed,
        inserted,
    )
    return (closed, inserted)
