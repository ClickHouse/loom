"""Rows that complete an entity the pool already names: the bridge entities
of a chain question, key-prefix siblings, attribute facets, kinship prefixes
and the proper-noun objects rows name."""

from __future__ import annotations

import logging
import re
from typing import Any

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.ask.identity import memory_identity_where
from server.memory.app.search_tag_filter import stamp_row_tags
from server.memory.domain.search_types import row_identity

logger = logging.getLogger(__name__)


_BRIDGE_STOP = frozenset(
    "user assistant the a an of in on at to for with and or is was were be been by from "
    "as that this these those it its his her their who whom whose which what where when "
    "how does did do has have had current former new old".split()
)

_PROPER_RUN_RE = re.compile(
    r"\b[A-Z][\w'\-]+(?:\s+(?:of|de|van|der|del|the|and|&)\s+)?(?:\s*[A-Z][\w'\-]+)*"
)
_BRIDGE_GENERIC = frozenset(
    "user assistant the a an i he she they it this that these those there here memory memories "
    "monday tuesday wednesday thursday friday saturday sunday january february march april may "
    "june "
    "july august september october november december yes no none unknown "
    # conversational fillers a turn opens with ("Please memorize it", "Thanks")
    "please thanks thank hello hi okay ok sure sorry great".split()
)


def key_slug(name: str) -> str:
    """The form a subject takes inside a memory key: lower-case, every
    non-alphanumeric run one underscore, a leading article dropped
    ("Carlos Gómez" -> "carlos_g_mez", the way the extractor keys it). Pure."""
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    for art in ("the_", "a_", "an_"):
        if slug.startswith(art) and len(slug) > len(art) + 2:
            slug = slug[len(art) :]
    return slug


async def bridge_rows_by_key(search_input: Any, bridge: str, *, top_k: int = 6) -> list[Any]:
    """The second hop's rows are the bridge entity's
    own attributes, and the store keys them by subject (`bernard_arnault_
    citizen_of`). One FINAL read of the active rows whose key carries the
    bridge's slug beats a second embedding of the same question, which
    returns the first hop's rows again (125 of 165 follows added nothing).
    Rows come back as wire hits; fails open to []."""
    slug = key_slug(bridge)
    identity = memory_identity_where(search_input)
    if len(slug) < 4 or identity is None:
        return []
    where, params = identity
    try:
        from server.core.ch import get_client
        from server.settings import settings

        ch = get_client()
        rows = await ch.query_all(
            f"""
            SELECT agent, user_id, session_id, memory_key, memory_type, value, status, tags,
                   version, toString(created_at) AS created_at, toString(valid_at) AS valid_at
            FROM loom.memory FINAL
            WHERE {" AND ".join(where)}
              AND status = 'active'
              AND tombstoned_at IS NULL
              -- a high value is readable by key only, not as an answer's excerpt
              AND sensitivity != 'high'
              AND memory_key LIKE {{pat:String}}
            ORDER BY version DESC
            -- one attribute written in many sessions leaves room for the bridge's others
            LIMIT {{per_key:UInt32}} BY memory_key
            LIMIT {{lim:UInt32}}
            """,
            params={
                **params,
                "pat": f"%{slug}%",
                "per_key": int(settings.memory_search_per_key_cap) or int(top_k),
                "lim": int(top_k),
            },
        )
        out: list[Any] = []
        for r in rows:
            hit = _MemorySearchHit(
                memory_key=str(r.get("memory_key") or ""),
                memory_type=str(r.get("memory_type") or "fact"),
                content_excerpt=str(r.get("value") or "").strip('"'),
                distance=0.0,
                similarity=1.0,
                rank_score=0.0,
                valid_at=str(r.get("valid_at") or "")[:10],
                created_at=str(r.get("created_at") or "")[:10],
                status=str(r.get("status") or ""),
                version=int(r.get("version") or 0),
                source="fact",
                session_id=str(r.get("session_id") or ""),
                explanation=f"bridge:{bridge}",
            )
            hit._row_identity = row_identity(r)  # so the pool knows the row it already holds
            stamp_row_tags(hit, r.get("tags"))  # so the hard tag belt judges this row
            out.append(hit)
        return out
    except Exception as e:  # noqa: BLE001 — additive, never fatal
        logger.warning("memory.ask bridge key read failed (open): %s", e)
        return []


def _names_in_text(text: str) -> list[str]:
    """Capitalised runs in a hit's text — the names it states — with the
    citation tail (`(#memory_key)`) and sentence-initial function words
    dropped. Pure."""
    body = re.sub(r"\(#[^)]*\)|\[[^\]]*\]", " ", text or "")
    out: list[str] = []
    for m in _PROPER_RUN_RE.finditer(body):
        name = m.group(0).strip()
        toks = re.findall(r"[A-Za-z0-9'\-]+", name)
        if not toks:
            continue
        # A lone sentence-initial function word ("The", "User") is not a name.
        if len(toks) == 1 and (toks[0].lower() in _BRIDGE_GENERIC or len(toks[0]) < 4):
            continue
        if all(t.lower() in _BRIDGE_GENERIC for t in toks):
            continue
        out.append(name)
    return out


def bridge_entities(hits: list[Any], question: str, *, limit: int = 2, top: int = 6) -> list[str]:
    """Names the top hits state that the question does not: the candidates
    for the first hop's answer. A hit's `entities` list is used when it
    carries one; otherwise the capitalised runs of its text (the wire hit
    model carries text, not entities). Ranked by the hit that carries them;
    stopword-grade tokens are skipped. Pure."""
    q_tokens = {w for w in re.findall(r"[a-z0-9]+", (question or "").lower())}
    out: list[str] = []
    seen: set[str] = set()
    # Fact rows first: they carry the settled first-hop
    # value, while a chunk or digest in the window may still name the value
    # the store retired ("Jack Dorsey" beside the recorded "Bernard Arnault").
    window = list(hits)[:top]
    ordered = [h for h in window if str(getattr(h, "source", "fact") or "fact") == "fact"]
    ordered += [h for h in window if str(getattr(h, "source", "fact") or "fact") != "fact"]
    for h in ordered:
        names = [str(e) for e in (getattr(h, "entities", None) or [])]
        if not names:
            names = _names_in_text(
                str(getattr(h, "content_excerpt", "") or getattr(h, "value", "") or "")
            )
        for name in names:
            name = name.strip()
            low = name.lower()
            if not name or low in seen or low in _BRIDGE_STOP or len(low) < 3:
                continue
            toks = [t for t in re.findall(r"[a-z0-9]+", low) if t not in _BRIDGE_STOP]
            if not toks or all(t in q_tokens for t in toks):
                continue  # the question already names it — not a bridge
            seen.add(low)
            out.append(name)
            if len(out) >= limit:
                return out
    return out


# Attribute facets the extractor uses as key suffixes; cue words in a count
# question that name them. A facet also qualifies when >=2 of the top hits
# already end in it (the extractor's own vocabulary for this namespace).
_FACET_CUES: dict[str, tuple[str, ...]] = {
    "age": ("age", "aged", "years old", "older", "younger", "year-old"),
    "birthday": ("birthday", "birthdays", "born"),
    "height": ("height", "tall", "taller", "shorter", "cm"),
    "location": (
        "location",
        "located",
        "happening in",
        "live in",
        "lives in",
        "based in",
        "city",
        "where",
    ),
    "duration": ("duration", "last", "lasts", "lasted", "long", "week", "weeks", "days"),
    "attendance": ("attend", "attendance", "people involved", "participants", "scale", "audience"),
    "scale": ("scale", "people involved", "attendees", "size"),
    "date": ("date", "when", "scheduled"),
    "email": ("email",),
    "workplace": ("work", "works", "workplace", "employer"),
    "hometown": ("from", "hails from", "hometown", "grew up", "originally from", "come from"),
}
_FACET_STOP = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "user",
        "event",
        "events",
        "info",
        "note",
        "notes",
        "fact",
        "summary",
        "chunk",
    }
)


def derive_key_facets(keys: list[str], stem: str, *, min_hits: int = 2) -> list[str]:
    """Attribute facets worth completing for a count question: suffixes that
    >=`min_hits` of the top hit keys share, plus cue-named facets the stem
    mentions. Projection keys (`chunk:`, `summary:`) never contribute."""
    counts: dict[str, int] = {}
    for k in keys:
        k = str(k or "")
        if not k or ":" in k or "_" not in k:
            continue
        suf = k.rsplit("_", 1)[-1].lower()
        if len(suf) < 3 or not suf.isalpha() or suf in _FACET_STOP:
            continue
        counts[suf] = counts.get(suf, 0) + 1
    q = f" {stem.lower()} "
    # A facet the question names wins ("taller than" → height, "live in" →
    # location); only facets this namespace's keys actually use are pulled.
    cued = [
        facet
        for facet, cues in _FACET_CUES.items()
        if counts.get(facet, 0) >= 1 and any(f" {c} " in q or q.strip().endswith(c) for c in cues)
    ]
    if cued:
        return cued[:4]
    # No cue: the attribute >=min_hits of the top hits share is the one the
    # question is about (the retriever already agreed on it).
    return [f for f, n in sorted(counts.items(), key=lambda x: -x[1]) if n >= min_hits][:4]


_BRIDGE_GENERIC_PREFIXES = frozenset(
    {
        "user",
        "users",
        "assistant",
        "the",
        "my",
        "event",
        "events",
        "person",
        "people",
        "friend",
        "coworker",
        "colleague",
        "info",
        "note",
        "fact",
    }
)


def derive_key_prefixes(keys: list[str], stem: str = "", *, max_prefixes: int = 12) -> list[str]:
    """Entity prefixes of the top hit keys: `user_success_gala` from
    `user_success_gala_duration`, `nolan` from `nolan_profession`. Hits whose
    key carries the attribute the question names come first ("the activity
    that lasts one day" → the `_duration` rows are the entities in question;
    measured: Success Gala's duration row sat at rank 7 behind six other
    events' rows and its prefix was never taken). A trailing numeric suffix
    (`_2`) is dropped first; a bare generic segment (`user`) names no entity
    and is skipped; projection keys never count."""
    from server.memory.store import FACET_SUFFIX_SYNONYMS

    cued: set[str] = set()
    q = f" {(stem or '').lower()} "
    for facet, cues in _FACET_CUES.items():
        if any(f" {c} " in q or q.strip().endswith(c) for c in cues):
            cued.add(facet)
            cued.update(FACET_SUFFIX_SYNONYMS.get(facet, ()))
    if cued:
        # The three best hits keep their place whatever their suffix — they
        # are the entities the question is most about (measured: the cue for
        # "lasts one day" put six other events' `_duration` rows ahead of
        # `user_run_for_good_one_day_event`, and its slot was gone).
        head, tail = list(keys[:3]), list(keys[3:])
        keys = head + sorted(
            tail,
            key=lambda k: 0 if str(k or "").lower().rsplit("_", 1)[-1] in cued else 1,
        )
    out: list[str] = []
    for k in keys:
        k = str(k or "").strip().lower()
        if not k or ":" in k or "_" not in k:
            continue
        k = re.sub(r"_\d+$", "", k)
        if "_" not in k:
            continue
        segs = k.split("_")
        # An attribute may be two words (`attendance_count`): take the
        # prefixes one and two segments short of the key, longest first, so
        # `user_sci_method_lab_attendance_count` yields `user_sci_method_lab`.
        # Attributes run to three words (`one_day_event`, `attendance_count`):
        # take the prefixes one, two and three segments short of the key —
        # measured: `user_run_for_good_one_day_event` never yielded
        # `user_run_for_good`, and the event's time row (a sibling) was never
        # in the pool while the reader said no time was recorded.
        candidates = ["_".join(segs[:-d]) for d in (1, 2, 3) if len(segs) - d >= 1 and segs[:-d]]
        # The SHORTEST prefix that still names an entity: its siblings are a
        # superset of any longer prefix's, and one entity costs one slot —
        # measured: two hits' six variants each filled the eight slots before
        # `user_run_for_good_one_day_event` was reached, and the event's time
        # row never joined the pool. One segment short
        # of the key always qualifies (`nolan`); shorter only when what
        # remains still names one (`user_sci_method_lab`, not `user_success`
        # from `…_gala_duration`), a kinship word counting as a name
        # (`user_mother`).
        chosen = ""
        for depth, prefix in enumerate(candidates, start=1):
            if len(prefix) < 3 or prefix in _BRIDGE_GENERIC_PREFIXES:
                continue
            bare_segs = [
                b for b in (prefix[5:] if prefix.startswith("user_") else prefix).split("_") if b
            ]
            if depth > 1 and len(bare_segs) < 2 and "_".join(bare_segs) not in _KINSHIP:
                continue
            chosen = prefix
        if not chosen:
            continue
        variants = [chosen]
        # The extractor spells one entity two ways across sessions
        # (`user_sci_method_lab_*` and `sci_method_lab_*`): match both.
        if chosen.startswith("user_"):
            bare = chosen[5:]
            if len(bare) >= 3 and bare not in _BRIDGE_GENERIC_PREFIXES and bare not in _KINSHIP:
                variants.append(bare)
        for v in variants:
            if v not in out:
                out.append(v)
        if len(out) >= max_prefixes:
            break
    return out


# A count is only as complete as the slice its read sees. The empty-pool retry
# in memory.search catches a replica that has none of a fresh namespace's
# parts; a replica that has SOME of them returns a shorter list and nothing
# looks wrong (measured: a 318-message session written 20 s earlier, four
# summer birthdays stored, one returned, answer "3 (closest)" against 4). The
# completion statements are one indexed range per facet or prefix, so they
# always read with sequential consistency: the metadata round trip is cheap
# next to the synthesis call it feeds, and these are the reads where a missing
# row changes the number. Fresh parts also raise ClickHouse code 10 ("Not
# found column … size = 0") on a first read; a fail-open [] there silently
# counts a slice (measured: 2 of 10 `_location` rows returned, answer
# "4 events" against 9 stored), so each statement runs under memory.search's
# fresh-part retry.


async def _completion_read(read: Any, *, leg: str) -> list[dict[str, Any]] | None:
    """`await read()` under sequential consistency (when read-your-writes is
    on) and the fresh-part retry; None when it still fails — the caller adds
    nothing. A fresh consistency scope per attempt: a context manager that
    exited on one attempt's error cannot be entered again."""
    from contextlib import nullcontext

    import server.memory.store as memory_repo
    from server.core.ch import sequential_reads
    from server.memory.app.retrieval.retry import _call_retrying_empty_block

    sequential = memory_repo.read_your_writes_enabled()

    async def _once() -> list[dict[str, Any]]:
        with sequential_reads() if sequential else nullcontext():
            return list(await read())

    try:
        return await _call_retrying_empty_block(_once, leg=leg)
    except Exception as e:  # noqa: BLE001 — additive, never fatal: fail open to no rows
        logger.debug("%s failed: %s", leg, e)
        return None


def _completion_hits(rows: list[dict[str, Any]], ranked: list[Any], explanation: str) -> list[Any]:
    """`rows` as wire hits, skipping keys already in `ranked` (and repeats)."""
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
                    memory_type=str(r.get("memory_type") or "semantic"),
                    content_excerpt=str(r.get("content_excerpt") or ""),
                    distance=0.0,
                    similarity=0.0,
                    rank_score=0.0,
                    valid_at=str(r.get("created_at") or "")[:19],
                    created_at=str(r.get("created_at") or "")[:10],
                    version=int(r.get("version") or 0),
                    status=str(r.get("status") or "active"),
                    source="fact",
                    explanation=explanation,
                )
            )
        except Exception:  # noqa: BLE001 — skip a row that won't validate
            continue
    return out


def _identity_kwargs(search_input: Any) -> dict[str, Any]:
    """The identity arguments of the memory_repo completion reads."""
    return {
        "org": getattr(search_input, "org", ""),
        "namespace": getattr(search_input, "namespace", ""),
        "scope": getattr(search_input, "scope", "private"),
        "agent": getattr(search_input, "agent", "*"),
        "user_id": getattr(search_input, "user_id", None),
    }


async def _bridge_complete_memories(
    search_input: Any, prefixes: list[str], ranked: list[Any], *, limit: int
) -> list[Any]:
    """New hits for every in-scope memory whose key starts with one of
    `prefixes` + "_" (the entity\'s other attributes) and is not already in
    `ranked`. Fail-open to []."""
    import server.memory.store as memory_repo
    from server.settings import settings as _bs

    ident = _identity_kwargs(search_input)
    rows = await _completion_read(
        lambda: memory_repo.find_by_key_prefix(
            prefixes=prefixes,
            limit=limit,
            per_prefix=int(_bs.memory_ask_bridge_completion_per_prefix),
            **ident,
        ),
        leg="bridge completion",
    )
    if rows is None:
        return []
    logger.info(
        "memory.ask: bridge completion %s → %d row(s) (%d already ranked)",
        prefixes,
        len(rows),
        len(ranked),
    )
    rows += await _bridge_content_fallback(search_input, prefixes, rows, ident)
    return _completion_hits(rows, ranked, "bridge completion")


async def _bridge_content_fallback(
    search_input: Any, prefixes: list[str], rows: list[dict[str, Any]], ident: dict[str, Any]
) -> list[dict[str, Any]]:
    """Bridge completion by text: an entity that no key starts with is looked
    for in the rows' text — the extractor keyed "The capital of Soviet Union is
    Russellville" as `capital_of_soviet`, and `soviet_union_` matched nothing
    while the row sat in the namespace. At most four such entities, three rows
    each; fail-open to [] (the prefix rows stand)."""
    import server.memory.store as memory_repo
    from server.memory.domain.ask_option_guard import parse_options as _parse_opts

    try:
        q = str(getattr(search_input, "query", "") or "")
        cues = question_cues(q)
        missed = unmatched_prefixes(prefixes, rows, cues, list(_parse_opts(q).values()))
    except Exception as e:  # noqa: BLE001 — the prefix rows stand
        logger.debug("bridge content fallback failed: %s", e)
        return []
    if not missed:
        return []
    more = await _completion_read(
        lambda: memory_repo.find_by_content_terms(
            terms=missed, limit=12, per_term=3, cues=cues, **ident
        ),
        leg="bridge content fallback",
    )
    if more:
        logger.info("memory.ask: bridge content fallback %s → %d row(s)", missed, len(more))
    return more or []


async def _facet_complete_memories(
    search_input: Any, facets: list[str], ranked: list[Any], *, limit: int
) -> list[Any]:
    """New hits for every in-scope memory whose key carries one of `facets`
    and is not already in `ranked`. Fail-open to []."""
    import server.memory.store as memory_repo

    rows = await _completion_read(
        lambda: memory_repo.find_by_key_facet(
            facets=facets, limit=limit, **_identity_kwargs(search_input)
        ),
        leg="facet completion",
    )
    if rows is None:
        return []
    logger.info(
        "memory.ask: facet completion %s → %d row(s) (%d already ranked)",
        facets,
        len(rows),
        len(ranked),
    )
    return _completion_hits(rows, ranked, "facet completion")


_KINSHIP = {
    "mom": ("user_mom", "user_mother"),
    "mother": ("user_mother", "user_mom"),
    "dad": ("user_dad", "user_father"),
    "father": ("user_father", "user_dad"),
    "sister": ("user_sister",),
    "brother": ("user_brother",),
    "cousin": ("user_cousin",),
    "aunt": ("user_aunt",),
    "uncle": ("user_uncle",),
    "grandma": ("user_grandma", "user_grandmother"),
    "grandmother": ("user_grandmother", "user_grandma"),
    "grandpa": ("user_grandpa", "user_grandfather"),
    "grandfather": ("user_grandfather", "user_grandpa"),
    "boss": ("user_boss",),
    "coworker": ("user_coworker",),
    "colleague": ("user_colleague",),
    "friend": ("user_friend",),
    "nephew": ("user_nephew",),
    "niece": ("user_niece",),
    "wife": ("user_wife",),
    "husband": ("user_husband",),
}
_KINSHIP_RE = re.compile(
    r"\bmy (" + "|".join(sorted(_KINSHIP, key=len, reverse=True)) + r")\b", re.I
)


def kinship_prefixes(chunk_texts: list[str], *, max_prefixes: int = 4) -> list[str]:
    """Key prefixes for the people a chunk names by relation to the user."""
    out: list[str] = []
    for text in chunk_texts:
        for m in _KINSHIP_RE.finditer(text or ""):
            for pref in _KINSHIP[m.group(1).lower()]:
                if pref not in out:
                    out.append(pref)
                if len(out) >= max_prefixes:
                    return out
    return out


_ROW_OBJECT_RE = re.compile(
    r"\b(?:in|of|to|by|is|was|at)\s+(?:the\s+)?(?:country|city|continent|state|religion|sport|field)?\s*(?:of\s+)?"
    r"([A-Z][\w'&.-]*(?:\s+(?:of|the|and)?\s*[A-Z][\w'&.-]*){0,3})"
)


_CUE_STOP = frozenset(
    {
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "where",
        "when",
        "how",
        "why",
        "does",
        "did",
        "was",
        "were",
        "the",
        "that",
        "this",
        "with",
        "from",
        "into",
        "for",
        "and",
        "are",
        "current",
        "date",
        "question",
        "choose",
        "answer",
        "option",
        "options",
        "please",
        "name",
        "city",
        "country",
        "person",
        "place",
        "state",
        "is",
        "of",
        "in",
        "on",
        "to",
        "a",
        "an",
        "by",
        "at",
        "as",
        "or",
        "it",
        "its",
        "his",
        "her",
        "their",
        "has",
        "have",
        "had",
        "user",
        "memories",
        "memory",
        "know",
        "tell",
        "about",
        "there",
        "here",
    }
)


def question_cues(question: str, *, max_cues: int = 6) -> list[str]:
    """The question's lower-case content words that are not proper nouns —
    "capital", "religion", "job title" — which order a text fallback's rows
    (`find_by_content_terms(cues=)`): the row that carries the asked attribute
    comes before the rows that merely name the entity."""
    stem = next(
        (
            ln
            for ln in (question or "").split("\n")
            if ln.strip() and not ln.lower().startswith(("current date", "question date"))
        ),
        "",
    )
    out: list[str] = []
    for m in re.finditer(r"[A-Za-z][A-Za-z'\-]{2,}", stem):
        w = m.group(0)
        low = w.lower()
        if w[0].isupper() and m.start() > 0:
            continue  # a proper noun names the entity, not the attribute
        if low in _CUE_STOP or low in out:
            continue
        out.append(low)
        if len(out) >= max_cues:
            break
    return out


_KEY_FRAGMENT_TAILS = frozenset({"of", "in", "by", "to", "the", "and", "at", "for", "with", "on"})


def _insert_at_window(ranked: list[Any], extra: list[Any], window: int) -> list[Any]:
    """Completion rows join the pool at the window's edge — after the rows the
    reader already sees, before the rows it does not — so widening the window
    by their count shows them. Appending to the pool's end left them past the
    window whenever the search returned more rows than the window holds."""
    cut = max(0, min(int(window), len(ranked)))
    return list(ranked[:cut]) + list(extra) + list(ranked[cut:])


def unmatched_prefixes(
    prefixes: list[str],
    rows: list[dict[str, Any]],
    cues: list[str] | None = None,
    option_texts: list[str] | None = None,
) -> list[str]:
    """The bridge prefixes the key read did not satisfy, in order, at most
    four — the entities whose rows the key naming hid. Unsatisfied means no
    returned key starts with the prefix (both sides with the connective
    segments folded, as the read compares them) or, when the question has
    cue words, none of the prefix's rows carries one: `soviet_union_` matched
    `soviet_union_head_of_state` while the capital sat under `capital_soviet`
    and the question asked for a capital."""
    from server.memory.store import normalize_key_prefix

    keyed = [
        (
            normalize_key_prefix(str(r.get("memory_key") or "").lower()),
            str(r.get("content_excerpt") or "").lower(),
        )
        for r in rows
    ]
    cue_list = [c.lower() for c in (cues or []) if c]
    options = " | ".join(str(o).lower() for o in (option_texts or []) if o)
    out: list[str] = []
    for p in prefixes:
        raw = str(p).strip().strip("_").lower()
        norm = normalize_key_prefix(raw)
        if not norm or norm in _BRIDGE_GENERIC_PREFIXES or ("_" not in norm and len(norm) < 5):
            continue
        # a name the question offers as an OPTION ("C) Chicago, IL") is a
        # candidate answer, not an entity to expand: searching the text for
        # it returns every event in that city (45 fallbacks in 30 asks, most of
        # them option cities; 6/15 against 11/15 without them)
        if options and raw.replace("_", " ") in options:
            continue
        # a key fragment cut at a connective (`capital_of`, `author_of`,
        # `user_adalbert_of`) names a relation, not an entity: searching the
        # text for "capital of" returns any capital (measured: 36 fallbacks
        # in one twenty-question run, a third of them such fragments)
        if norm.rsplit("_", 1)[-1] in _KEY_FRAGMENT_TAILS or norm.startswith("user_"):
            continue
        mine = [text for k, text in keyed if k.startswith(norm + "_")]
        satisfied = bool(mine) and (
            not cue_list or any(c in text for text in mine for c in cue_list)
        )
        # the text fallback searches the entity as written, connectives kept
        if not satisfied and raw not in out:
            out.append(raw)
        if len(out) >= 4:
            break
    return out


# Role nouns a question names in place of an entity ("the country", "its
# founder"): never a bridge prefix.
_GENERIC_SEEDS = frozenset(
    {
        "country",
        "city",
        "person",
        "place",
        "sport",
        "religion",
        "organization",
        "team",
        "company",
        "someone",
        "something",
        "location",
        "continent",
        "capital",
        "spouse",
        "author",
        "founder",
        "director",
        "ceo",
        "chairperson",
        "head of state",
        "partner",
    }
)


def _key_subject(key: str, text: str) -> str:
    """The longest leading run of a key's words (all but the last, its
    attribute) that `text` names: `association_football_created_in` in "…the
    sport of association football." -> "association football"; '' when none."""
    words = [w for w in str(key or "").lower().split("_") if w]
    for n in range(len(words) - 1, 0, -1):
        phrase = " ".join(words[:n])
        if len(phrase) >= 4 and re.search(rf"\b{re.escape(phrase)}\b", text):
            return phrase
    return ""


def _norm_words(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())


def chain_first(rows: list[Any], query: str) -> list[Any]:
    """The rows on the question's chain first, in chain order: a row whose key
    subject the question names, then each row whose subject an already
    chained row names; the other rows after, in their order. The next hop
    then follows the chain's objects before an unrelated top hit's. Pure."""
    known = _norm_words(query)
    chained: list[Any] = []
    rest = list(rows)
    grew = True
    while grew and rest:
        grew = False
        for r in list(rest):
            if _key_subject(getattr(r, "memory_key", ""), known):
                chained.append(r)
                rest.remove(r)
                known += " " + _norm_words(getattr(r, "content_excerpt", ""))
                grew = True
    return chained + rest


def row_object_prefixes(
    rows: list[Any], *, exclude: set[str] | None = None, max_prefixes: int = 8
) -> list[str]:
    """Key prefixes for the proper-noun objects the given rows name ("… was
    created in the country of Soviet Union" → `soviet_union`), skipping the
    prefixes already bridged and the generic role nouns. The next hop of a
    bridge, taken over the rows rather than the triples."""
    seen = set(exclude or ())
    out: list[str] = []
    for r in rows:
        text = str(getattr(r, "content_excerpt", "") or "")
        for m in _ROW_OBJECT_RE.finditer(text):
            name = m.group(1).strip(" .,'\"")
            low = name.lower()
            key = re.sub(r"[^a-z0-9]+", "_", low).strip("_")
            if (
                len(key) < 3
                or key in seen
                or key in _BRIDGE_GENERIC_PREFIXES
                or low in _GENERIC_SEEDS
                or low in ("user", "users")
            ):
                continue
            seen.add(key)
            out.append(key)
            if len(out) >= max_prefixes:
                return out
    return out
