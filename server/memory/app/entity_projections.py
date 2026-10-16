"""memory.set entity projection, derived facts and triples-follow-rows for the
memory write path. The entity edge / triple / vector writers they reuse live
in server.memory.app.projections.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from collections.abc import Mapping
from datetime import datetime
from types import SimpleNamespace
from typing import Any, NamedTuple

from server.core.ch import get_client
from server.core.datetimes import naive_utc_now, parse_iso_dt
from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry, _BulkSetResult
from server.memory.app.projection_errors import (
    bulk_projection_payload,
    record_input_projection_failure,
)
from server.memory.app.projection_failures import TRIPLES_RECONCILE, TRIPLES_REOPEN
from server.memory.app.projections import (
    close_triples,
    end_edges,
    end_triples_now,
    entity_triple_row,
    open_triples_among,
    reopen_triples,
    resolve_projection_entity_map,
    triple_row_link,
    write_entity_edges,
    write_entity_triples,
)
from server.memory.domain import triples_follow_rows as tfr
from server.memory.domain.temporal_sql import GRAPH_HOLDER_SQL, LIVE_STATUSES

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
# Longer than a name in wide characters: a run of unspaced text ("東京に住んでいます").
_SET_ENTITY_MAX_WIDE_LEN = 6


def _not_a_name(word: str) -> bool:
    """Shorter than a thing's name: "is", "at", "of", "3pm". Wide characters (CJK,
    Hangul) write a name in two: "東京", "서울"."""
    if not word[0].isalpha():
        return True
    if all(unicodedata.east_asian_width(c) in ("W", "F") for c in word):
        return len(word) < 2 or len(word) > _SET_ENTITY_MAX_WIDE_LEN
    return len(word) < _SET_ENTITY_MIN_LEN


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
    for tok in tfr.split_words(text, dots=True):
        if _not_a_name(tok):
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


async def project_stated_relations(
    row: Mapping[str, Any],
    value: Any,
    relations: list[tfr.Triple],
    *,
    revised: bool,
    op: str,
    ch: Any,
) -> None:
    """Write the `relations` a point write states, as a `memory.bulk_set` entry's are:
    linked to `row` (the `loom.memory` row holding `value`), and through the interval
    repair, which may close an older single-valued fact and, under `newest_wins`,
    retire the rows behind it. A row that may not hold graph facts (`holds_graph_facts`)
    states none: a proposal is one, and accepting it later projects nothing, so its
    facts are stated again once it is established. Then, for a row `revised`
    (it held a value before this write), the facts it held are reconciled with its
    new value (`close_dropped_facts`). It runs after the same-key supersession, which
    reads no triples: point writes run no row follow. Fail-open: a failed write is
    recorded for the projection repair to replay."""
    if relations and holds_graph_facts(row):
        await _write_stated_relations(row, value, relations, op=op, ch=ch)
    if revised:
        scope = SimpleNamespace(org=row["org"], namespace=row["namespace"], agent=row["agent"])
        await reconcile_row_facts(scope, [str(row["memory_id"])], reopen=False, op=op, ch=ch)


def holds_graph_facts(row: Mapping[str, Any]) -> bool:
    """`GRAPH_HOLDER_SQL` for a row in hand: live, not deleted, not ended, not 'high'."""
    if row.get("status") not in LIVE_STATUSES or row.get("tombstoned_at") is not None:
        return False
    ends = [parse_iso_dt(row.get(c)) for c in ("expires_at", "valid_to")]
    if any(end is not None and end <= naive_utc_now() for end in ends):
        return False
    return row.get("sensitivity") != "high"


async def _write_stated_relations(
    row: Mapping[str, Any], value: Any, relations: list[tfr.Triple], *, op: str, ch: Any
) -> None:
    try:
        write, results = _as_bulk_write(row, value, relations)
    except Exception as e:  # noqa: BLE001 — a projection; the write stands
        log.warning("%s: relations not projected: %s", op, e)
        return
    try:
        await write_entity_triples(write, results, ch=ch)
    except Exception as e:  # noqa: BLE001 — a projection; the write stands
        log.warning("%s: relations projection failed (recorded for repair): %s", op, e)
        await record_input_projection_failure(
            write,
            projection="entity_triples",
            error=e,
            memory_keys=[results[0].memory_key],
            payload=bulk_projection_payload(write, results, error=e),
            source_operation=op,
            source_resource_id=f"{op}:{results[0].memory_id}",
        )


async def reconcile_row_facts(
    input: Any,
    ids: list[str],
    *,
    reopen: bool,
    op: str,
    ch: Any,
    hashes: Mapping[str, str] | None = None,
    restored_at: datetime | None = None,
) -> None:
    """Fail-open `reopen_held_facts` (`reopen`) or `close_dropped_facts` for the rows
    `ids` of `input`'s namespace. An Undo passes the time it restored at and the
    `content_sha256` each row had (`hashes`): a row revised since is left to its
    revision, now and on a replay. A failure is recorded for the projection repair with
    both and the Undo's id, which its replay finds its rows by."""
    restored_at, restore_id = restored_at or naive_utc_now(), uuid.uuid4().hex
    try:
        if reopen:
            # Lazy: graph_undo reads row links through this module.
            from server.memory.app import graph_undo

            await graph_undo.reopen_held_facts(
                input, ids, ch=ch, restored_at=restored_at, restore_id=restore_id, hashes=hashes
            )
        else:
            await close_dropped_facts(input, ids, ch=ch)
    except Exception as e:  # noqa: BLE001 — a projection; the write stands
        log.warning("%s: facts not reconciled (recorded for repair): %s", op, e)
        payload: dict[str, Any] = {"memory_ids": ids, "reopen": reopen}
        if reopen:
            payload["restored_at"] = restored_at.isoformat()
            payload["restore_id"] = restore_id
            payload["content_sha256"] = dict(hashes or {})
        await record_input_projection_failure(
            input,
            projection=TRIPLES_RECONCILE,
            error=e,
            payload=payload,
            source_operation=op,
            source_resource_id=f"{op}:{ids[0]}",
        )


async def linked_triples(input: Any, ids: list[str], *, ch: Any) -> list[tfr.Triple]:
    rows = await ch.query_all(
        """
        SELECT DISTINCT subject, relation, object
        FROM loom.entity_triple_rows
        WHERE org = {org:String} AND namespace = {ns:String} AND memory_id IN {ids:Array(UUID)}
        SETTINGS select_sequential_consistency = 1
        """,
        params={"org": input.org, "ns": input.namespace, "ids": ids},
    )
    return sorted((str(r["subject"]), str(r["relation"]), str(r["object"])) for r in rows)


async def close_dropped_facts(input: Any, ids: list[str], *, ch: Any) -> None:
    """Close the facts linked to the rows `ids` that no row holds any longer: a
    revision that no longer states them (`tfr.carried`), or that made its row one
    that holds none. One still valid later ends now."""
    linked = await linked_triples(input, ids, ch=ch)
    held = await stated_by_live_rows(input.org, input.namespace, linked, ch=ch)
    gone = [t for t in linked if t not in held]
    opened = await open_triples_among(ch, org=input.org, namespace=input.namespace, triples=gone)
    if opened:
        await _write_follow_plan(input, tfr.FollowPlan(closes=sorted(opened), inserts=[]), ch=ch)
    await end_triples_now(ch, org=input.org, namespace=input.namespace, triples=gone)
    await _after_the_close(input, ids, gone, held, ch=ch)


async def _after_the_close(
    input: Any,
    changed: list[str],
    gone: list[tfr.Triple],
    held: frozenset[tfr.Triple],
    *,
    ch: Any,
) -> None:
    """End the edges of the `gone` facts and keep one value current for each
    single-valued relation they touch, from one read of the triples.

    changed: ids of the rows revised or retired.
    gone: the facts those rows no longer keep, closed now or before (a replay
        finds the close already landed and still checks).
    held: the facts the caller found a live row still holds."""
    from server.identity.app.namespace_ontology import load_relation_cardinality
    from server.memory.domain.graph_quality import is_single_valued_relation

    if not gone:
        return
    names = sorted({e for s, _r, o in gone for e in (s, o)})
    declared = await load_relation_cardinality(input.org, input.namespace)
    single = sorted({(s, r) for s, r, _o in gone if is_single_valued_relation(r, declared)})
    # On the fly: the closes just made are mutations that may still be pending.
    rows = await ch.query_all(
        """
        SELECT subject, relation, object, valid_from, valid_to, invalid_at, seen_at
        FROM loom.entity_triples
        WHERE org = {org:String} AND namespace = {ns:String}
          AND ((subject IN {names:Array(String)} AND object IN {names:Array(String)})
               OR (subject, relation) IN {single:Array(Tuple(String, String))})
        SETTINGS select_sequential_consistency = 1, apply_mutations_on_fly = 1
        """,
        params={"org": input.org, "ns": input.namespace, "names": names, "single": single},
    )
    between = [r for r in rows if r["subject"] in names and r["object"] in names]
    await _close_dropped_edges(input, changed, gone, held, between, ch=ch)
    await _keep_one_current(
        input, [r for r in rows if (r["subject"], r["relation"]) in single], ch=ch
    )


async def _close_dropped_edges(
    input: Any,
    changed: list[str],
    gone: list[tfr.Triple],
    held: frozenset[tfr.Triple],
    rows: list[Mapping[str, Any]],
    *,
    ch: Any,
) -> None:
    """End the edges of the `gone` facts with them (`tfr.edge_ends`), unless a live row
    still holds another fact between the same two entities, or a live row other than
    the `changed` ones names both (`end_edges`). `rows` are the stored triples between
    the entities; only those neither in `gone` nor `held` are asked about."""
    known = set(gone) | held
    others = sorted(
        {(str(r["subject"]), str(r["relation"]), str(r["object"])) for r in rows} - known
    )
    held = held | await stated_by_live_rows(input.org, input.namespace, others, ch=ch)
    ends = tfr.edge_ends(gone, held, rows, naive_utc_now())
    await end_edges(ch, org=input.org, namespace=input.namespace, ends=ends, changed=changed)


async def _keep_one_current(input: Any, rows: list[Mapping[str, Any]], *, ch: Any) -> None:
    """One value of a single-valued relation stays current, never neither. For each
    relation of `rows` (the stored triples of the relations a close touched) with no
    value valid now, the newest value a live row still holds is stated again from now,
    as an Undo states it, edge included: London undone over Paris, then revised away,
    gives Paris back from then on."""
    from server.memory.app import graph_undo
    from server.memory.domain.graph_quality import observation_key

    now = naive_utc_now()

    # When each relation's open values start ("moving to Berlin on Dec 1").
    open_starts: dict[tuple[str, str], list[Any]] = {}
    for r in rows:
        if r["valid_to"] is None:
            open_starts.setdefault((r["subject"], r["relation"]), []).append(r["valid_from"])

    def valid_now(r: Mapping[str, Any]) -> bool:
        started = r["valid_from"] is None or r["valid_from"] <= now
        ends = r["valid_to"]
        if not started or (ends is not None and ends <= now):
            return False
        if ends is None or r["invalid_at"] is None:
            return True
        # A close gave it a later end (`invalid_at` set): it holds on only while an open
        # value starts by then to take over, and not once that value is gone too.
        starts = open_starts.get((r["subject"], r["relation"]), [])
        return any(start is None or start <= ends for start in starts)

    current = {(r["subject"], r["relation"]) for r in rows if valid_now(r)}
    # A value still open starts later ("moving on Dec 1"): it is not stated again.
    closed = [
        r
        for r in rows
        if (r["subject"], r["relation"]) not in current and r["valid_to"] is not None
    ]
    if not closed:
        return
    held = await stated_by_live_rows(
        input.org,
        input.namespace,
        sorted({(r["subject"], r["relation"], r["object"]) for r in closed}),
        ch=ch,
    )
    # Oldest first, so each relation ends up with its newest held row.
    newest: dict[tuple[str, str], Mapping[str, Any]] = {}
    for r in sorted(
        closed, key=lambda r: observation_key(r["valid_from"], r["seen_at"], r["object"])
    ):
        if (r["subject"], r["relation"], r["object"]) in held:
            newest[(r["subject"], r["relation"])] = r
    # A value that ended on its own stays ended; a close stamps `invalid_at`.
    back = sorted(
        (r["subject"], r["relation"], r["object"], str(r["invalid_at"]), str(r["seen_at"]))
        for r in newest.values()
        if r["invalid_at"] is not None
    )
    if not back:
        return
    # Named by the closes it answers, so a replay finds the markers an attempt wrote.
    # The row's own seen_at (microseconds) too: a marker closed again in the second of
    # the close it answered is another row, and needs a marker of its own.
    restore_id = uuid.uuid5(uuid.NAMESPACE_OID, repr(back)).hex
    await graph_undo.restate_facts(
        input,
        frozenset(t[:3] for t in back),
        ch=ch,
        restore_id=restore_id,
        restored_at=now,
        adapter_name="row-fallback",
    )


def _as_bulk_write(
    row: Mapping[str, Any], value: Any, relations: list[tfr.Triple]
) -> tuple[MemoryBulkSetInput, list[_BulkSetResult]]:
    """A point write's row as the one-entry bulk write the triple writer reads."""
    anchor = row.get("temporal_anchor")
    entry = _BulkSetEntry(
        memory_key=row["memory_key"],
        value=value,
        relations=relations,
        confidence=row["confidence"],
        temporal_anchor=anchor.isoformat() if isinstance(anchor, datetime) else anchor,
        valid_at=row.get("valid_at"),
        valid_to=row.get("valid_to"),
    )
    write = MemoryBulkSetInput(
        org=row["org"],
        namespace=row["namespace"],
        agent=row["agent"],
        user_id=row["user_id"],
        session_id=row["session_id"],
        entries=[entry],
    )
    result = _BulkSetResult(
        memory_key=entry.memory_key, version=int(row["version"]), memory_id=str(row["memory_id"])
    )
    return write, [result]


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


def _next_link(row: dict[str, Any]) -> str:
    """The id a retired, undeleted row's chain continues at; "" for any other row,
    and for one retired behind no UUID."""
    if str(row.get("status")) != "superseded" or row.get("tombstoned_at") is not None:
        return ""
    return _canonical_id(str(row.get("superseded_by") or "")) or ""


def _canonical_id(value: str) -> str | None:
    """`value` as `toString(memory_id)` spells it, or None when it is not a UUID in
    the dashed or bare hex form ClickHouse parses."""
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    # Python also reads braces, a urn prefix and moved dashes, which ClickHouse does not.
    return str(parsed) if value.lower() in (str(parsed), parsed.hex) else None


# Supersession pairs one read of the rows and their open triples covers.
_FOLLOW_CHUNK = 64
# Links followed past a pair's winner toward the live end of its chain; the repair,
# off the write's path, reads to the end of any chain that ends.
_CHAIN_HOPS = 8
REPAIR_CHAIN_HOPS = 1024


async def _rows_along_chains(
    input: Any, ids: list[str], *, ch: Any, hops: int = _CHAIN_HOPS
) -> dict[str, dict[str, Any]]:
    """The rows `ids` name, and those their `superseded_by` chains lead to, by id. An
    id no row answers (erased) maps to `{}`: it states nothing."""
    by_id: dict[str, dict[str, Any]] = {}
    for _ in range(hops + 1):
        rows = await ch.query_all(
            f"""
            SELECT toString(memory_id) AS id, memory_key, value, user_id,
                   toString(content_sha256) AS content_sha256, valid_at, updated_at,
                   status, superseded_by, tombstoned_at, {GRAPH_HOLDER_SQL} AS holds_facts
            FROM loom.memory FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND memory_id IN {{ids:Array(UUID)}}
            SETTINGS select_sequential_consistency = 1
            """,
            params={"org": input.org, "ns": input.namespace, "ids": ids},
        )
        by_id.update({str(r["id"]): r for r in rows})
        for i in ids:
            by_id.setdefault(i, {})
        ids = sorted({_next_link(r) for r in by_id.values()} - {""} - by_id.keys())
        if not ids:
            break
    return by_id


def _chain_head(row: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Where a chain of retirements starting at `row` ends: a row not retired, a
    deleted one, one retired behind no UUID, or `{}` for one no longer stored. None
    for a cycle, or a chain longer than was read."""
    seen: set[str] = set()
    while link := _next_link(row):
        seen.add(str(row["id"]))
        if link in seen:
            log.warning("triples follow rows: a cycle of retirements through %s", link)
            return None
        if link not in by_id:
            log.warning("triples follow rows: chain longer than was read, at %s", link)
            return None
        row = by_id[link]
    return row


class FollowOutcome(NamedTuple):
    closed: int
    inserted: int
    # Pairs whose winner's chain could not be read to its end (a cycle, or longer
    # than the hops read): not followed.
    unfollowed: list[tuple[str, str]]


async def follow_row_supersessions(
    input: Any, pairs: list[tuple[str, str]], *, ch: Any, hops: int = _CHAIN_HOPS
) -> FollowOutcome:
    """Triples follow rows: for every (superseded row, successor row) pair, close
    the open triples the superseded row attests and the successor does not, and
    write the successor triple under the predecessor's subject and relation when
    the two rows differ by one substituted span. The successor is the live end of
    the winner's chain, so a value retired since never comes back as current and a
    value a live row restates never closes. A pair is followed only while the
    loser is still retired behind that winner (an Undo or a delete may come in
    between when the repair replays it). The rows and open triples are read
    `_FOLLOW_CHUNK` pairs at a time; the successors are then inserted and the
    closes are one UPDATE for the lot. Following a pair again is harmless: a closed
    triple is no longer open, and a successor already open is not repeated."""
    canonical_pairs = [(_canonical_id(lo), _canonical_id(wi)) for lo, wi in pairs]
    followed = [(lo, wi) for lo, wi in canonical_pairs if lo and wi]
    plans: list[tfr.FollowPlan] = []
    unfollowed: list[tuple[str, str]] = []
    for i in range(0, len(followed), _FOLLOW_CHUNK):
        chunk = followed[i : i + _FOLLOW_CHUNK]
        plan, unread = await _plan_chunk(input, chunk, ch=ch, hops=hops)
        plans.append(plan)
        unfollowed += unread
    closing = sorted({t for plan in plans for t in plan.closes})
    plan = tfr.merge_plans(
        plans, kept=await stated_by_live_rows(input.org, input.namespace, closing, ch=ch)
    )
    closed, inserted = await _write_follow_plan(input, plan, ch=ch)
    # Read again, not taken from the plan: a replay plans no close for a fact an
    # attempt closed, and must still end what came after it.
    losers = sorted({lo for lo, _wi in followed})
    linked = sorted({*await linked_triples(input, losers, ch=ch), *plan.closes})
    held = await stated_by_live_rows(input.org, input.namespace, linked, ch=ch)
    gone = [t for t in linked if t not in held]
    # A fact the loser stated until later ends now, or when a winner dated ahead
    # starts, as an open one closes; a close's own later end stays.
    await end_triples_now(
        ch,
        org=input.org,
        namespace=input.namespace,
        triples=gone,
        stated_ends_only=True,
        ends=plan.ends,
    )
    await _after_the_close(input, losers, gone, held, ch=ch)
    if closed or inserted:
        log.info(
            "triples follow rows: of %d pair(s), closed %d and wrote %d successor triple(s)",
            len(followed),
            closed,
            inserted,
        )
    return FollowOutcome(closed, inserted, unfollowed)


class _Held(NamedTuple):
    """A pair still retired behind its winner; `head` is the live end of the
    winner's chain, None when that end is deleted or erased."""

    loser: dict[str, Any]
    winner: dict[str, Any]
    head: dict[str, Any] | None


async def _plan_chunk(
    input: Any, pairs: list[tuple[str, str]], *, ch: Any, hops: int = _CHAIN_HOPS
) -> tuple[tfr.FollowPlan, list[tuple[str, str]]]:
    """The `FollowPlan` for `pairs`, and the pairs whose chain could not be read to
    its end: one SELECT per chain link for the rows, one for the rows' links, one
    for the candidate open triples (bounded to the namespace and to the triples
    the rows link or whose subject and object both occur in the pair texts)."""
    ids = sorted({i for pair in pairs for i in pair})
    by_id = await _rows_along_chains(input, ids, ch=ch, hops=hops)
    held: list[_Held] = []
    unread: list[tuple[str, str]] = []
    for loser_id, winner_id in pairs:
        # Still retired behind that winner: not undone, deleted or re-pointed since.
        lo = by_id[loser_id]
        if not lo or _next_link(lo) != winner_id:
            continue
        winner = by_id[winner_id]  # `{}` when erased: nothing holds in the loser's place
        head = _chain_head(winner, by_id)
        if head is None:
            unread.append((loser_id, winner_id))
            continue
        # The loser is judged against what holds now: the live end of its chain. An
        # end that may hold no graph facts (deleted, erased, ended or 'high') attests
        # nothing, so the loser's triples close and none follow.
        held.append(_Held(lo, winner, head if head.get("holds_facts") else None))
    if not held:
        return tfr.FollowPlan(closes=[], inserts=[]), unread
    rows = [r for h in held for r in (h.loser, h.head) if r]
    stated, hashes = await _triples_stated(input, rows, ch=ch)
    holds = await _carried_by(
        input.org, input.namespace, [h.head for h in held if h.head], stated, hashes, ch=ch
    )
    sups = [_supersession(h, stated, hashes, holds) for h in held]
    text = " ".join(f"{s.loser_text} {s.winner_text}" for s in sups)
    linked = sorted(
        {
            t
            for s in sups
            for t in s.loser_triples | s.winner_triples | s.loser_inferred | s.winner_inferred
        }
    )
    open_triples = await ch.query_all(
        """
        SELECT subject, relation, object, confidence, provenance
        FROM loom.entity_triples
        WHERE org = {org:String} AND namespace = {ns:String}
          AND valid_to IS NULL AND invalid_at IS NULL
          -- Lowercase matching, not casefolding: "STRASSE" misses "straße" here
          -- unless a row links it.
          AND ((positionCaseInsensitiveUTF8({txt:String}, subject) > 0
                AND positionCaseInsensitiveUTF8({txt:String}, object) > 0)
               OR (subject, relation, object)
                  IN {linked:Array(Tuple(String, String, String))})
        SETTINGS select_sequential_consistency = 1
        """,
        params={"org": input.org, "ns": input.namespace, "txt": text, "linked": linked},
    )
    return tfr.plan_follow(list(open_triples or []), sups), unread


def _supersession(
    held: _Held,
    stated: dict[str, frozenset[tfr.Triple]],
    hashes: dict[str, dict[tfr.Triple, set[str]]],
    holds: dict[str, frozenset[tfr.Triple]],
) -> tfr.RowSupersession:
    """The pair as the domain planner reads it: the loser attests every successor
    written for it, the winner those it still holds (`holds`)."""
    lo, head = held.loser, held.head or {}
    head_id = str(head.get("id") or "")
    return tfr.RowSupersession(
        loser_text=_text(lo.get("value")),
        winner_text=_text(head.get("value")),
        winner_key=str(head.get("memory_key") or ""),
        winner_valid_from=head.get("valid_at"),
        # Undated: when the winner was written. A replay reads its latest
        # write, so a lock or revision since moves the date.
        loser_valid_to=held.winner.get("valid_at") or held.winner.get("updated_at"),
        winner_id=head_id,
        winner_sha256=str(head.get("content_sha256") or ""),
        winner_user_id=str(head.get("user_id") or ""),
        loser_triples=stated.get(str(lo["id"]), frozenset()),
        winner_triples=stated.get(head_id, frozenset()),
        loser_inferred=frozenset(hashes.get(str(lo["id"]), {})),
        winner_inferred=holds.get(head_id, frozenset()),
    )


async def _triples_stated(
    input: Any, rows: list[dict[str, Any]], *, ch: Any
) -> tuple[dict[str, frozenset[tfr.Triple]], dict[str, dict[tfr.Triple, set[str]]]]:
    """By row id: what the extractor stated for its current value, and the
    successors written for it with the hashes of the values each was written for
    (a revision keeps the id, so they are read whatever value it holds now). A row
    written before rows were linked has neither."""
    keys = sorted({(str(r["id"]), str(r.get("content_sha256") or "")) for r in rows})
    if not keys:
        return {}, {}
    found = await ch.query_all(
        """
        SELECT toString(memory_id) AS id, toString(content_sha256) AS sha, inferred,
               subject, relation, object
        FROM loom.entity_triple_rows
        WHERE org = {org:String} AND namespace = {ns:String}
          AND ((NOT inferred AND (memory_id, content_sha256) IN {keys:Array(Tuple(UUID, String))})
               OR (inferred AND memory_id IN {ids:Array(UUID)}))
        SETTINGS select_sequential_consistency = 1
        """,
        params={
            "org": input.org,
            "ns": input.namespace,
            "keys": keys,
            "ids": sorted({k[0] for k in keys}),
        },
    )
    stated: dict[str, set[tfr.Triple]] = {}
    hashes: dict[str, dict[tfr.Triple, set[str]]] = {}
    for f in found:
        mid, triple = str(f["id"]), (str(f["subject"]), str(f["relation"]), str(f["object"]))
        if f["inferred"]:
            hashes.setdefault(mid, {}).setdefault(triple, set()).add(str(f["sha"]))
        else:
            stated.setdefault(mid, set()).add(triple)
    return {i: frozenset(t) for i, t in stated.items()}, hashes


def _text(raw: Any) -> str:
    """The prose a stored `value` carries, decoded: names are matched in it."""
    from server.memory.domain.triple_provenance import carrier_text
    from server.memory.store.rows import parse_memory_value

    return carrier_text(parse_memory_value(raw))


async def _carried_by(
    org: str,
    namespace: str,
    heads: list[dict[str, Any]],
    stated_now: dict[str, frozenset[tfr.Triple]],
    hashes: dict[str, dict[tfr.Triple, set[str]]],
    *,
    ch: Any,
) -> dict[str, frozenset[tfr.Triple]]:
    """The successors each live row holds now (`tfr.carried`), by row id.
    `stated_now`: what the extractor stated for each row's current value; `hashes`:
    the hashes of the values each successor was written for. Those values are read
    from `loom.memory_versions`, the latest unredacted version with that hash;
    a hash none still holds (erased or redacted) holds nothing."""
    from server.identity.app.namespace_ontology import load_relation_cardinality

    current = {str(h["id"]): str(h.get("content_sha256") or "") for h in heads}
    old = sorted(
        {
            (mid, sha)
            for mid in current
            for shas in hashes.get(mid, {}).values()
            for sha in shas
            if sha != current[mid]
        }
    )
    old_text: dict[tuple[str, str], str] = {}
    if old:
        versions = await ch.query_all(
            """
            SELECT toString(memory_id) AS id, toString(content_sha256) AS sha,
                   argMax(value, version) AS value
            FROM loom.memory_versions
            WHERE org = {org:String} AND namespace = {ns:String}
              AND (memory_id, content_sha256) IN {keys:Array(Tuple(UUID, String))}
              AND redacted_at IS NULL
            GROUP BY id, sha
            """,
            params={"org": org, "ns": namespace, "keys": old},
        )
        old_text = {(str(v["id"]), str(v["sha"])): _text(v["value"]) for v in versions}
    declared = await load_relation_cardinality(org, namespace) if old else {}
    return {
        mid: tfr.carried(
            _text(h.get("value")),
            current[mid],
            stated_now.get(mid, frozenset()),
            {
                t: {sha: old_text.get((mid, sha), "") for sha in shas}
                for t, shas in hashes.get(mid, {}).items()
            },
            declared,
        )
        for h in heads
        if (mid := str(h["id"])) in hashes
    }


async def rows_holding(
    org: str, namespace: str, versions: list[tuple[str, str]], *, ch: Any
) -> set[str]:
    """Of `versions`, (memory_id, content_sha256) pairs, the ids of the rows whose
    current value is still that version and may hold graph facts (`GRAPH_HOLDER_SQL`)."""
    if not versions:
        return set()
    found = await ch.query_all(
        f"""
        SELECT DISTINCT toString(memory_id) AS id
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND (memory_id, content_sha256) IN {{versions:Array(Tuple(UUID, String))}}
          AND {GRAPH_HOLDER_SQL}
        SETTINGS select_sequential_consistency = 1
        """,
        params={"org": org, "ns": namespace, "versions": sorted(set(versions))},
    )
    return {str(r["id"]) for r in found}


async def stated_by_live_rows(
    org: str,
    namespace: str,
    triples: list[tfr.Triple],
    *,
    ch: Any,
    rows: list[str] | None = None,
) -> frozenset[tfr.Triple]:
    """Of `triples`, those a row that may hold graph facts (`GRAPH_HOLDER_SQL`) holds:
    stated for its current value, or carried from a value it was revised from
    (`tfr.carried`). `rows` (ids) limits the rows asked; without it, rows that predate
    row links count too (`held_by_unlinked_rows`)."""
    if not triples:
        return frozenset()
    only_rows = "AND memory_id IN {rows:Array(UUID)}" if rows is not None else ""
    found = await ch.query_all(
        f"""
        WITH links AS (
            SELECT memory_id, content_sha256, subject, relation, object
            FROM loom.entity_triple_rows
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND (subject, relation, object) IN {{triples:Array(Tuple(String, String, String))}}
              {only_rows}
        )
        SELECT toString(l.memory_id) AS id, toString(m.content_sha256) AS current,
               toString(l.content_sha256) AS sha, m.value AS value,
               l.subject AS subject, l.relation AS relation, l.object AS object
        FROM links AS l
        INNER JOIN (
            SELECT memory_id, content_sha256, value
            FROM loom.memory FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND memory_id IN (SELECT memory_id FROM links)
              AND {GRAPH_HOLDER_SQL}
        ) AS m ON l.memory_id = m.memory_id
        SETTINGS select_sequential_consistency = 1
        """,
        params={"org": org, "ns": namespace, "triples": triples, "rows": rows or []},
    )
    # A link to the row's current value holds; one to a value the row was revised
    # from is judged against the value it has now.
    held = set()
    heads: dict[str, dict[str, Any]] = {}
    hashes: dict[str, dict[tfr.Triple, set[str]]] = {}
    for f in found:
        triple = (str(f["subject"]), str(f["relation"]), str(f["object"]))
        if f["sha"] == f["current"]:
            held.add(triple)
        else:
            mid = str(f["id"])
            heads[mid] = {"id": mid, "content_sha256": f["current"], "value": f["value"]}
            hashes.setdefault(mid, {}).setdefault(triple, set()).add(str(f["sha"]))
    held_triples = frozenset(held)
    if rows is None:
        held_triples |= await held_by_unlinked_rows(org, namespace, triples, ch=ch)
    if not heads:
        return held_triples
    stated_now = await _stated_links(org, namespace, list(heads.values()), ch=ch)
    holds = await _carried_by(org, namespace, list(heads.values()), stated_now, hashes, ch=ch)
    return held_triples.union(*holds.values())


# The migration that started linking rows to triples: a value first written before it
# was applied has no links of its own.
_ROW_LINKS_MIGRATION = "033_entity_triple_rows.sql"


async def held_by_unlinked_rows(
    org: str,
    namespace: str,
    triples: list[tfr.Triple] | None,
    *,
    ch: Any,
    rows: list[str] | None = None,
) -> frozenset[tfr.Triple]:
    """Of `triples`, those a row written before rows were linked to triples holds: a
    live row whose value was first written before the row-links migration, of a key the
    triple is linked to (`entity_triple_memories`), that still names it
    (`tfr.still_states`). A value written since with no link stated no fact, and holds
    none. With `rows` (ids) and no `triples`: what those rows hold of every triple
    their keys are linked to."""
    if triples is None:
        # The triples of the rows' keys, as the follow matched them by text.
        wanted = """memory_key IN (
                  SELECT memory_key FROM loom.memory
                  WHERE org = {org:String} AND namespace = {ns:String}
                    AND memory_id IN {rows:Array(UUID)})"""
    else:
        wanted = "(subject, relation, object) IN {triples:Array(Tuple(String, String, String))}"
    only_rows = "AND memory_id IN {rows:Array(UUID)}" if rows is not None else ""
    found = await ch.query_all(
        f"""
        WITH keyed AS (
            SELECT memory_key, subject, relation, object
            FROM loom.entity_triple_memories
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND {wanted}
        ),
        live AS (
            SELECT memory_id, memory_key, value
            FROM loom.memory FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND memory_key IN (SELECT memory_key FROM keyed)
              AND {GRAPH_HOLDER_SQL}
              {only_rows}
              -- Its current value was first written before rows were linked: a pin, a
              -- lock or an Undo writes the same value again and keeps it a legacy row.
              AND (memory_id, content_sha256) NOT IN (
                  SELECT memory_id, content_sha256 FROM loom.memory_versions
                  WHERE org = {{org:String}} AND namespace = {{ns:String}}
                    AND memory_id IN (
                        SELECT memory_id FROM loom.memory
                        WHERE org = {{org:String}} AND namespace = {{ns:String}}
                          AND memory_key IN (SELECT memory_key FROM keyed)
                    )
                  GROUP BY memory_id, content_sha256
                  HAVING min(updated_at) >= (
                      SELECT min(applied_at) FROM loom._migrations
                      WHERE filename = {{row_links:String}}
                  )
              )
        )
        SELECT l.value AS value, k.subject AS subject, k.relation AS relation,
               k.object AS object
        FROM keyed AS k
        INNER JOIN live AS l ON k.memory_key = l.memory_key
        WHERE l.memory_id NOT IN (
            SELECT memory_id FROM loom.entity_triple_rows
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND memory_id IN (SELECT memory_id FROM live)
        )
        SETTINGS select_sequential_consistency = 1
        """,
        params={
            "org": org,
            "ns": namespace,
            "triples": triples or [],
            "rows": rows or [],
            "row_links": _ROW_LINKS_MIGRATION,
        },
    )
    held = set()
    for f in found:
        triple = (str(f["subject"]), str(f["relation"]), str(f["object"]))
        if tfr.still_states(_text(f["value"]), triple):
            held.add(triple)
    return frozenset(held)


async def _stated_links(
    org: str, namespace: str, rows: list[dict[str, Any]], *, ch: Any
) -> dict[str, frozenset[tfr.Triple]]:
    """What the extractor stated for each row's current value, by row id."""
    found = await ch.query_all(
        """
        SELECT toString(memory_id) AS id, subject, relation, object
        FROM loom.entity_triple_rows
        WHERE org = {org:String} AND namespace = {ns:String}
          AND NOT inferred AND (memory_id, content_sha256) IN {keys:Array(Tuple(UUID, String))}
        SETTINGS select_sequential_consistency = 1
        """,
        params={
            "org": org,
            "ns": namespace,
            "keys": sorted({(str(r["id"]), str(r["content_sha256"])) for r in rows}),
        },
    )
    stated: dict[str, set[tfr.Triple]] = {}
    for f in found:
        stated.setdefault(str(f["id"]), set()).add(
            (str(f["subject"]), str(f["relation"]), str(f["object"]))
        )
    return {i: frozenset(t) for i, t in stated.items()}


async def _write_follow_plan(input: Any, plan: tfr.FollowPlan, *, ch: Any) -> tuple[int, int]:
    """Write the plan's successors, then close its triples: a close that lands after
    a failed insert would leave a replay nothing open to follow. Returns (closed,
    inserted), not counting a triple opened again after its close."""
    arrived = naive_utc_now()
    # Whole seconds, as `invalid_at` stores it: a reopen finds this close by it.
    now = arrived.replace(microsecond=0)
    # The plan read open triples by their raw text and so can miss a successor a
    # failed close left open ("Berlin, Germany" is `berlin germany`): look it up.
    already = await open_triples_among(
        ch, org=input.org, namespace=input.namespace, triples=[s.triple for s in plan.inserts]
    )
    inserted = await _insert_successors(input, plan.inserts, already, at=arrived, ch=ch)
    if not plan.closes:
        return 0, inserted

    # valid_to = when its own row was replaced (an erased winner's: now);
    # invalid_at = now.
    def _until(t: tfr.Triple) -> datetime:
        at = plan.closed_at.get(t)
        return at if isinstance(at, datetime) else now

    until = [(s, r, o, _until((s, r, o))) for s, r, o in plan.closes]
    closes: list[tuple[str, str, str, datetime, datetime | None]] = [(*c, now) for c in until]
    await close_triples(ch, org=input.org, namespace=input.namespace, closes=closes)
    try:
        held = await reopen_held(
            org=input.org, namespace=input.namespace, closes=until, closed_at=now, ch=ch
        )
    except Exception as e:  # noqa: BLE001 — the closes are in; the repair re-checks them
        log.warning("triples follow rows: re-check after the close failed (recorded): %s", e)
        await record_input_projection_failure(
            input,
            projection=TRIPLES_REOPEN,
            error=e,
            payload={
                "closes": [[s, r, o, at.isoformat()] for s, r, o, at in until],
                "closed_at": now.isoformat(),
            },
            source_operation="triples.follow_rows",
            source_resource_id=f"triples.follow_rows:{input.namespace}",
        )
        # Audited as closed; the repair logs what it opens again.
        held = frozenset()
    closed = [t for t in plan.closes if t not in held]
    try:
        from server.observability import audit as _audit

        for s, r, o in closed[:16]:
            await _audit.emit(
                event_type="triple.follows_row_supersession",
                org=input.org,
                namespace=input.namespace,
                actor=str(getattr(input, "agent", "-") or "-"),
                subject={"closed": f"{s} {r} {o}"},
            )
    except Exception as e:  # noqa: BLE001 — the closes are in; the trail is best effort
        log.debug("triples follow rows: audit skipped: %s", e)
    return len(closed), inserted


async def reopen_held(
    *,
    org: str,
    namespace: str,
    closes: list[tuple[str, str, str, datetime]],
    closed_at: datetime,
    ch: Any,
) -> frozenset[tfr.Triple]:
    """Undo the close at `closed_at` (`closes` as `reopen_triples` takes them) for the
    triples a live row holds, and return those. A row written while a follow ran may
    state a triple it closed ("back to London"): the close also ends the open row that
    write inserted. Every writer lands a triple's row links first, so once the close is
    queued they are found here."""
    held = await stated_by_live_rows(org, namespace, [c[:3] for c in closes], ch=ch)
    await reopen_triples(
        ch,
        org=org,
        namespace=namespace,
        closes=[c for c in closes if c[:3] in held],
        closed_at=closed_at,
    )
    return held


async def _insert_successors(
    input: Any,
    successors: list[tfr.Successor],
    already: set[tfr.Triple],
    *,
    at: datetime,
    ch: Any,
) -> int:
    """Insert the successor triples not `already` open, after every successor's key
    and row links: a triple that landed without them would never get them, as a
    replay sees it open and skips it. Each triple is written once, though several
    winners may be linked to it; a repeated link is the same row. `at` is the follow's
    arrival, in microseconds. Returns how many triples were written."""
    now = at.replace(microsecond=0)
    new: dict[tfr.Triple, tfr.Successor] = {}
    for ins in successors:
        if ins.triple not in already:
            new.setdefault(ins.triple, ins)
    keyed = {(ins.triple, ins.memory_key): ins for ins in successors if ins.memory_key}
    adapter = "row-supersession"
    triples = [
        entity_triple_row(
            input.org,
            input.namespace,
            ins.subject,
            ins.relation,
            ins.object,
            provenance=ins.provenance,
            confidence=ins.confidence,
            adapter_name=adapter,
            last_seen=now,
            seen_at=at,
            valid_from=ins.valid_from if isinstance(ins.valid_from, datetime) else None,
        )
        for ins in new.values()
    ]
    provenance = [
        {
            "org": input.org,
            "namespace": input.namespace,
            "subject": ins.subject,
            "relation": ins.relation,
            "object": ins.object,
            "memory_key": ins.memory_key,
            "seen_at": now,
            "confidence": ins.confidence,
            "adapter_name": adapter,
        }
        for ins in keyed.values()
    ]
    links = [
        triple_row_link(
            input.org,
            input.namespace,
            ins.memory_id,
            ins.content_sha256,
            ins.triple,
            user_id=ins.user_id,
            inferred=True,
        )
        for ins in successors
        if ins.memory_id
    ]
    settings = {"async_insert": 1, "wait_for_async_insert": 1}
    if provenance:
        await ch.insert("loom.entity_triple_memories", provenance, settings=settings)
    if links:
        await ch.insert("loom.entity_triple_rows", links, settings=settings)
    if triples:
        await ch.insert("loom.entity_triples", triples, settings=settings)
    return len(triples)
