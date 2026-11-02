"""The evidence pool of one ask, phase by phase: sizing, the primary retrieval,
the question-shaped widening, ordering, identifier injection, completion,
code grounding and the similarity knee, the settled window and how it reads
time."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from server.memory.app.ask.completion import (
    _bridge_complete_memories,
    _facet_complete_memories,
    _insert_at_window,
    bridge_entities,
    bridge_rows_by_key,
    chain_first,
    derive_key_facets,
    derive_key_prefixes,
    kinship_prefixes,
    row_object_prefixes,
)
from server.memory.app.ask.identifiers import (
    _chase_identifiers,
    _cited_hits,
    _inject_identifier_memories,
    _injectable_identifiers,
    _mention_window_chunks,
)
from server.memory.app.ask.question_shapes import (
    _HOW_MANY_Q_RE,
    is_chain_question,
    is_count_intent,
    is_inference_question,
    is_when_question,
    looks_temporal,
    split_compound_question,
)
from server.memory.app.ask.rendering import evidence_order
from server.memory.app.ask.retrieval_legs import (
    _pending_evidence_stage,
    _recency_rerank,
    _time_cue_first,
    _with,
    decomposed_retrieval,
)
from server.memory.app.ask.supersession import (
    _drop_superseded,
    mask_stale_sentences,
    resolve_templated_conflicts,
    split_pool_by_state,
    superseded_rows_for_pool,
)
from server.memory.app.ask.synthesis import _publish_window_keys
from server.memory.domain.search_types import hit_row_key

logger = logging.getLogger(__name__)


# Grounding window: the synthesizer sees at most this many top memories. The
# retrieval still returns the full top_k to the caller in `results`.


def _full_scan_window(
    question: str, window: int, pool_k: int, settings_obj: Any
) -> tuple[int, int, dict[str, Any]]:
    """When `memory_ask_set_full_scan` is N > 0 and the
    question is a count or a set, the window and the pool both grow to N so
    the synthesizer reads every candidate row instead of the 40 the cosine
    put first: on multi-session count questions the items a count misses are in
    the store and outside the window, and a namespace holds ~215 user facts.
    Off (0) leaves window and pool as computed. The third value is the change
    set for the pool search (the chunk cap, `_full_scan_changes`)."""
    n = int(settings_obj.memory_ask_set_full_scan or 0)
    if n <= 0 or n <= window:
        return window, pool_k, {}
    from server.memory.domain.query_intent import parse_query_intent as _pqi
    from server.memory.domain.synth_policy import is_set_question as _is_set_q

    try:
        intent = _pqi(question, None)
    except Exception:  # noqa: BLE001 — the grammar abstains, never blocks
        intent = None
    if not (
        is_count_intent(intent) or _is_set_q(question) or _HOW_MANY_Q_RE.search(question or "")
    ):
        return window, pool_k, {}
    return n, max(pool_k, n), _full_scan_changes(pool_k, max(pool_k, n), settings_obj)


def _full_scan_changes(pool_k_cos: int, pool_k: int, settings_obj: Any) -> dict[str, Any]:
    """The whole set is the FACT rows. When the scan widened the pool past
    the cosine's size, cap the chunk share of that pool so the 300 rows are
    mostly rows and not the raw turns the words also match: uncapped, a
    300-row pool carries 69–128 chunks and 51–57k synthesis tokens (4.5× the
    40-row ask). 0 = uncapped."""
    if pool_k <= pool_k_cos:
        return {}
    share = float(settings_obj.memory_ask_set_full_scan_chunk_share or 0.0)
    return {"chunk_cap_share": share} if share > 0 else {}


@dataclass(slots=True)
class _AskRun:
    """One memory.ask request's evidence pool as it moves through the phases
    of `ask` — sized, retrieved, widened, injected, completed, cut and
    settled into the synthesis window — plus the counters those phases leave
    for the answer's telemetry. Request-scoped and mutable; it holds only
    what flows between two or more phases."""

    search_input: Any
    question: str
    settings: Any
    target_k: int
    window: int
    recency_weight: float
    pool_k_used: int = 0
    ranked: list[Any] = dc_field(default_factory=list)
    results: list[Any] = dc_field(default_factory=list)
    synth_pool: list[Any] = dc_field(default_factory=list)
    retrieved_at: Any = None
    retrieval: Any = None
    # Rows dropped as superseded, sentences masked as stale,
    # and the superseded rows that masked them (the final pass reuses them).
    superseded_dropped: int = 0
    stale_sentences_masked: int = 0
    stale_masks: list[Any] = dc_field(default_factory=list)
    # The question-shaped widening legs: decomposition parts and the bridge
    # entities followed.
    decomposed: list[str] = dc_field(default_factory=list)
    decomp_stats: dict[str, int] = dc_field(default_factory=lambda: {"failed": 0})
    entity_followed: list[str] = dc_field(default_factory=list)
    pending_evidence_n: int = 0
    model: str = ""  # the retrieval's embedding model
    query_text: str = ""  # the retrieval text (the stem under a scaffold)
    idents: list[str] = dc_field(default_factory=list)  # the hedge's identifiers
    # The identifier chase, as it ran.
    chase_hops_done: int = 0
    chase_added: list[Any] = dc_field(default_factory=list)
    intent: Any = None  # the parsed query intent, read by completion and synthesis
    # Facet completion: the key facets pulled and the rows they added.
    facets: list[str] = dc_field(default_factory=list)
    facet_added: int = 0
    # The similarity knee over the window, as it cut.
    gate_mode: str = ""
    gate_kept: int = 0
    gate_dropped: int = 0
    # The settled synthesis window (what the reader reads) and what shaped it.
    memories: list[Any] = dc_field(default_factory=list)
    context_tokens_est: int = 0
    context_trimmed: int = 0
    old_late: list[Any] = dc_field(default_factory=list)  # superseded rows found by the final pass
    summary_hits: int = 0
    timeline_rows: int = 0  # dated rows in the computed timeline


def _ask_pool_size(
    search_input: Any,
    question: str,
    *,
    set_window: int | None,
    settings: Any,
) -> _AskRun:
    """Size the ask: how many rows retrieval fetches (the pool) and how many
    the synthesizer reads (the window), and widen the search input to the
    pool when a later phase needs more than top_k."""
    _settings = settings
    # Recency-aware retrieval: an agent's memory holds stale copies of the same
    # fact across sessions, and the freshest can rank below them on cosine
    # alone. Over-fetch and re-rank by (similarity + w*recency) so the current
    # fact reaches the synthesis window; it wins more judged pairs than
    # cosine alone. w=0 restores cosine-only.
    w_rec = float(_settings.memory_ask_recency_weight)
    target_k = int(getattr(search_input, "top_k", 15) or 15)
    window = int(_settings.memory_ask_context_memories)
    # Count-shaped questions read a wider window: the answer is a set whose
    # members are scattered across sessions (the fifth heirloom sat at rank 18
    # of 15 in the loss analysis). The parser is LLM-free, so asking it early
    # costs nothing; the same intent is parsed again for synthesis below.
    _count_window = int(_settings.memory_ask_count_window)
    if _count_window > window:
        try:
            from server.memory.domain.query_intent import parse_query_intent as _pqi

            _early = _pqi(question, getattr(search_input, "observation_date", None) or None)
        except Exception:  # noqa: BLE001 — the grammar abstains, never blocks
            _early = None
        if is_count_intent(_early):
            window = _count_window
    # A set question reads a wider window the same way a count
    # does; its items are one per session and the question's cosine reaches
    # only some of them.
    _set_window = (
        int(set_window) if set_window is not None else int(_settings.memory_ask_set_window)
    )
    if _set_window > window:
        from server.memory.domain.synth_policy import is_set_question as _is_set_q

        if _is_set_q(question):
            window = _set_window
    # Over-fetch to cover BOTH the 2x recency-rerank pool AND the synthesis
    # window (breadth queries must SEE more than top_k); the full-scan window
    # below may widen both.
    pool_k = min(max(target_k * 2, window), 40)
    window, pool_k, _fs_changes = _full_scan_window(question, window, pool_k, _settings)
    pool_k_used = 0
    if w_rec > 0 or window > target_k:
        try:
            search_input = _with(search_input, top_k=pool_k, **_fs_changes)
            pool_k_used = int(pool_k)
        except (TypeError, ValueError):
            pass  # unknown input shape: as-is retrieval, and pool_k stays 0
    return _AskRun(
        search_input=search_input,
        question=question,
        settings=_settings,
        target_k=target_k,
        window=window,
        recency_weight=w_rec,
        pool_k_used=pool_k_used,
    )


async def _retrieve(run: _AskRun) -> None:
    """The primary retrieval: one memory.search over the sized input, then
    the rows the synthesis pool must not hold (unverified external rows,
    superseded rows, stale sentences) leave it."""
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from server.memory.app.search import handle_memory_search

    search_input, _settings = run.search_input, run.settings
    run.retrieved_at = _dt.now(_UTC)
    run.retrieval = await handle_memory_search(search_input)
    run.ranked = list(run.retrieval.results or [])
    # Probation: declared-external rows nobody attested stay
    # out of the synthesis pool; they still reach the caller as hits.
    if _settings.memory_ask_exclude_unverified_external:
        from server.memory.domain.probation import exclude_unverified_external

        run.ranked, _held = exclude_unverified_external(run.ranked)
        if _held:
            logger.info("memory.ask held %d unverified external row(s) out of synthesis", _held)
    # Superseded rows leave the pool; the sentences in chunks
    # and summaries that restate them are masked.
    run.superseded_dropped = 0
    run.stale_sentences_masked = 0
    run.stale_masks = []
    if _drop_superseded(search_input):
        run.ranked, _old_rows = split_pool_by_state(run.ranked)
        run.ranked, _tpl = resolve_templated_conflicts(run.ranked)
        _old_rows = list(_old_rows) + _tpl
        run.superseded_dropped = len(_old_rows)
        run.stale_masks = list(_old_rows) + await superseded_rows_for_pool(search_input, run.ranked)
        if run.stale_masks:
            run.ranked, run.stale_sentences_masked = mask_stale_sentences(
                run.ranked, run.stale_masks
            )
        if run.superseded_dropped or run.stale_sentences_masked:
            logger.info(
                "memory.ask dropped %d superseded row(s), masked %d stale sentence(s)",
                run.superseded_dropped,
                run.stale_sentences_masked,
            )


async def _widen_by_question(
    run: _AskRun, *, llm: Any, llm_cheap: Any, decompose: str | None
) -> tuple[bool, bool, bool]:
    """The legs a question's shape asks for on top of the primary retrieval:
    each part of a compound question, the decomposed hops of a multi-hop one,
    and the bridge entities of a chain question join the pool. Returns the
    reader notes the same shapes switch on: (inference, temporal derive,
    chain)."""
    from server.memory.app.search import handle_memory_search

    search_input, question, _settings = run.search_input, run.question, run.settings
    # Two-part questions ("How many X? Additionally, how many Y?") retrieve
    # each part on its own: one embedding of the whole sits
    # between the halves and the window is spent on whichever half it leans
    # to — the loss ledger's "no ages in memories" with all eight ages stored.
    _parts = split_compound_question(getattr(search_input, "query", "") or "")
    if len(_parts) > 1:
        ns = str(search_input.namespace)
        _have = {hit_row_key(h, ns) for h in run.ranked}
        for _part in _parts[1:]:
            try:
                _sub = await handle_memory_search(_with(search_input, query=_part))
            except Exception:  # noqa: BLE001 — additive
                continue
            for h in _sub.results or []:
                row = hit_row_key(h, ns)
                if row.memory_key and row not in _have:
                    _have.add(row)
                    run.ranked.append(h)
    # Multi-hop decomposition: a question whose answer needs
    # two facts in sequence gets one embedding that sits between them and a
    # window spent on whichever fact is closer. Split it, retrieve each part,
    # union. The mode is the request's (off when unset: paired runs on the
    # production synthesizer lost answers); fail-open.
    run.decomposed = []
    run.decomp_stats = {"failed": 0}
    _dmode = (decompose or "off").lower()
    if _dmode in ("auto", "always") and (llm_cheap is not None or llm is not None):
        run.decomposed, run.ranked = await decomposed_retrieval(
            search_input,
            question,
            run.ranked,
            llm=llm_cheap or llm,
            mode=_dmode,
            max_parts=int(_settings.memory_ask_decompose_max_parts or 4),
            part_top_k=int(_settings.memory_ask_decompose_part_top_k or 8),
            stats=run.decomp_stats,
        )
    # An inference question is answered as an inference.
    inference_note = is_inference_question(question)
    # A when-question's reader derives from relative phrases.
    temporal_derive = is_when_question(question)
    # A chain question follows the first hop's answer entity
    # into one more retrieval per bridge entity, so the second-hop memory
    # enters the pool. LLM-free, bounded, fail-open.
    run.entity_followed = []
    chain_question = False
    if is_chain_question(question):
        chain_question = True
        ns = str(search_input.namespace)
        _have_rows = {hit_row_key(h, ns) for h in run.ranked}
        for _bridge in bridge_entities(
            run.ranked,
            question,
            limit=int(_settings.memory_ask_entity_follow_max or 2),
        ):
            _ftop = int(_settings.memory_ask_entity_follow_top_k or 6)
            # Revision: the bridge's own rows by key first (its attributes are
            # what the second hop needs); the semantic follow only when the
            # store keys the entity some other way.
            _sub_hits: list[Any] = await bridge_rows_by_key(search_input, _bridge, top_k=_ftop)
            _path = "key"
            if not _sub_hits:
                _path = "semantic"
                try:
                    _sub = await handle_memory_search(
                        _with(
                            search_input,
                            query=f"{_bridge}: {question}",
                            top_k=_ftop,
                        )
                    )
                    _sub_hits = list(_sub.results or [])
                except Exception:  # noqa: BLE001 — additive
                    continue
            added = 0
            for h in _sub_hits:
                row = hit_row_key(h, ns)
                if row.memory_key and row not in _have_rows:
                    _have_rows.add(row)
                    run.ranked.append(h)
                    added += 1
            run.entity_followed.append(_bridge)
            logger.info("memory.ask entity-follow %r via %s added %d hit(s)", _bridge, _path, added)
    return inference_note, temporal_derive, chain_question


async def _order_pool(run: _AskRun) -> None:
    """Put the pool in reading order: pending turns join it, then the
    recency rerank and the time-cue rows set the order the window slice takes."""
    search_input, question, _settings = run.search_input, run.question, run.settings
    # Pending evidence: a deferred batch has written nothing
    # yet; while it waits, the turns of it that speak to the question join the
    # pool as raw rows, so the answer knows what was just said. LLM-free,
    # bounded, fail-open; only when the scope has batches in flight.
    # The ask's own stage partition rides the request's work
    # bag onto the ledger row (`out_stage_ms`), as memory.search's does.
    from server.core import request_work as _rw

    _rw.mark_stage("retrieval")
    run.ranked, run.pending_evidence_n = await _pending_evidence_stage(
        search_input, question, run.ranked
    )
    _rw.mark_stage("pending")
    if run.recency_weight > 0 and run.ranked:
        run.ranked = _recency_rerank(run.ranked, run.recency_weight)
    run.ranked = _time_cue_first(
        run.ranked,
        run.window,
        question,
        getattr(search_input, "observation_date", None) or getattr(search_input, "as_of", None),
    )
    run.results = run.ranked[: run.target_k]
    run.synth_pool = run.ranked[: run.window]
    run.model = getattr(run.retrieval, "embedding_model", "") or ""


async def _inject_named_evidence(run: _AskRun) -> None:
    """The rows the question names outright: exact-identifier injection,
    the identifier chase from them, and the transcript turns of a
    mention-time question go ahead of the pool."""
    from server.core import request_work as _rw
    from server.memory.domain.retrieval_calibration import _salient_identifiers

    search_input, _settings = run.search_input, run.settings
    # Exact-identifier injection: loom's lexical leg is number-blind, so a
    # "what do we know about PR 109741" query can miss the very memories that
    # cite it (recall returns a different PR, and the grounding leg then
    # abstains). Surface them by exact substring and prepend to the pool.
    query_text = getattr(search_input, "query", "") or ""
    idents = _salient_identifiers(query_text)
    # Injection uses a BROADER entity set than the hedge: a query naming a
    # person/handle (JaneDoe), a setting/symbol (optimize_distinct_in_order),
    # or a zero-padded test id (05059) has facts in the corpus that semantic
    # recall ranks below the window when the query is about the TOPIC not the
    # entity — measured: a "replying to <handle>" query retrieved 0 of that
    # handle's 160 facts. Injection is additive/fail-open so broadening it here is
    # safe; the hedge stays on the narrow `idents` to avoid over-abstaining.
    inject_idents = _injectable_identifiers(query_text)
    chase_hops_done = 0
    chase_added: list[Any] = []
    if inject_idents and _settings.memory_ask_identifier_injection:
        injected = await _inject_identifier_memories(search_input, inject_idents, run.ranked)
        if injected:
            run.ranked = injected + run.ranked
            run.results = run.ranked[: run.target_k]
            run.synth_pool = run.ranked[: run.window]
        # Identifier chase: the memories the question's
        # identifier reached may name further identifiers — a value that is
        # itself a key, a PR that names a commit. One pass answers the first
        # hop only; each further hop is one more exact-substring read, no
        # model call, bounded by hops and rows. The frontier is every window
        # row that cites a question identifier — injected here, or already
        # ranked in by the search (the planner's identifier pin puts the exact
        # match first, so injection often dedups to nothing).
        _hops = int(_settings.memory_ask_identifier_chase_hops)
        frontier = injected + _cited_hits(run.ranked[len(injected) : run.window], inject_idents)
        if _hops > 0 and frontier:
            chase_added, chase_hops_done = await _chase_identifiers(
                search_input,
                seen=[query_text, *inject_idents],
                frontier=frontier,
                ranked=run.ranked,
                hops=_hops,
                cap=int(_settings.memory_ask_identifier_chase_cap or 8),
            )
            if chase_added:
                run.ranked = injected + chase_added + run.ranked[len(injected) :]
                run.results = run.ranked[: run.target_k]
                run.synth_pool = run.ranked[: run.window]
    # A mention-time question also reads the transcript clock.
    _mention_added = await _mention_window_chunks(search_input, query_text, run.ranked)
    if _mention_added:
        run.ranked = _mention_added + run.ranked
        run.results = run.ranked[: run.target_k]
        run.synth_pool = run.ranked[: run.window]

    _rw.mark_stage("injection")
    run.query_text, run.idents = query_text, idents
    run.chase_hops_done, run.chase_added = chase_hops_done, chase_added


async def _complete_options(run: _AskRun) -> None:
    """Parse the query intent once, then fetch the rows that state each
    option of a multiple-choice question."""
    search_input, question, _settings = run.search_input, run.question, run.settings
    # Intent is parsed once, here, from the retrieval text (the stem when the caller's query
    # carried an answer scaffold; the grammar abstains on multi-line text, so a scaffolded
    # question would never reach count mode). Reused by facet completion and synthesis
    # below.
    from server.memory.domain.query_intent import parse_query_intent

    try:
        intent = parse_query_intent(
            run.query_text, getattr(search_input, "observation_date", None) or None
        )
    except Exception:  # noqa: BLE001 — the grammar abstains, it never blocks
        intent = None

    # Option completion: a multiple-choice question
    # names its candidate answers; the memories that state an option's text
    # are the evidence that decides between them, and a thin similarity pool
    # can miss the one that carries the CURRENT value (the fact "corporate
    # attorney" ranked out while a raw chunk with the superseded "paralegal" ranked in). Fetch the
    # memories citing each short option text, exactly as identifier injection
    # does, so the latest-statement verifier and the reader see them.
    if not is_count_intent(intent):
        from server.memory.domain.ask_option_guard import parse_options

        _opt_texts = [
            v
            for v in parse_options(question).values()
            if 3 <= len(v) <= 60 and not v.lower().startswith("both ")
        ]
        if len(_opt_texts) >= 2:
            # Option texts are Title Case ("Corporate Attorney"); the memory says
            # "corporate attorney" — match without case.
            _opt_hits = await _inject_identifier_memories(
                search_input, _opt_texts, run.ranked, case_insensitive=True
            )
            if _opt_hits:
                # Prepend, as identifier injection does: appended rows sit past
                # the synthesis window whenever recall returned more than
                # `window` rows, and neither the reader nor the latest-statement
                # verifier would see them.
                run.ranked = _opt_hits + run.ranked
                run.results = run.ranked[: run.target_k]
                run.synth_pool = run.ranked[: run.window]
                logger.info(
                    "memory.ask: option completion added %d memories %s",
                    len(_opt_hits),
                    [getattr(h, "memory_key", "") for h in _opt_hits],
                )
    run.intent = intent


async def _complete_bridges(run: _AskRun) -> None:
    """Bridge completion: the key-prefix siblings of the
    top hits' entities, then up to two row hops over the proper nouns those
    rows name, join the pool and widen the window by what they add."""
    search_input, _settings = run.search_input, run.settings
    # Bridge completion: a question that names an entity by one attribute ("the event that
    # lasts one day", "the person with the contact number …") retrieves the row for THAT
    # attribute; the answer is another attribute of the same entity, whose row shares the
    # key prefix and rarely the query's words (`user_success_gala_duration` in the pool,
    # `…_location` not, answer "no memory gives its location" with the row stored). Pull the
    # siblings of the top hits' entities — one indexed range per prefix, no LLM.
    bridge_added = 0
    if run.ranked and not is_count_intent(run.intent):
        _top_n = int(_settings.memory_ask_bridge_completion_top_hits or 12)
        _prefixes = derive_key_prefixes(
            [getattr(h, "memory_key", "") for h in run.ranked[:_top_n]], run.query_text
        )
        # A chunk hit names the entity by a kinship or role word ("My mom's
        # phone number is …"); the extractor keys that person's facts
        # `user_mom_*` / `user_mother_*`. Those prefixes join too.
        for _kp in kinship_prefixes(
            [
                getattr(h, "content_excerpt", "")
                for h in run.ranked[:_top_n]
                if str(getattr(h, "memory_key", "")).startswith("chunk:")
            ]
        ):
            if _kp not in _prefixes:
                _prefixes.append(_kp)
        if _prefixes:
            _bridge_max = int(_settings.memory_ask_bridge_completion_max or 24)
            _bridge_extra = await _bridge_complete_memories(
                search_input, _prefixes, run.ranked, limit=_bridge_max
            )
            if _bridge_extra:
                bridge_added = len(_bridge_extra)
                run.ranked = _insert_at_window(run.ranked, _bridge_extra, run.window)
                run.results = run.ranked[: run.target_k]
                run.window = max(run.window, min(len(run.ranked), run.window + bridge_added))
                run.synth_pool = run.ranked[: run.window]
                logger.info(
                    "memory.ask: bridge completion added %d memories for %s",
                    bridge_added,
                    _prefixes,
                )
            # Row hops: the proper nouns the top hits and their bridged siblings name as
            # objects ("The director of The Beatles is Joe Hockey") are the next hop when
            # the triple plane has no path — a sweep-recovered fact with no triple, a seed
            # the graph spells with an accent (`joe_hockey_citizen_of` and
            # `italy_capital_duluth` active in the store, the reader told "no memory records
            # Joe Hockey's country"). Two hops at most, eight prefixes each, the same row
            # cap; the chain bridge below repeats none of these.
            _hop_rows: list[Any] = chain_first(
                list(run.ranked[:_top_n]) + list(_bridge_extra or []), run.query_text
            )
            _hop_seen: set[str] = set(_prefixes)
            _row_hops_added = 0
            for _hop_no in range(int(_settings.memory_ask_bridge_row_hops)):
                _hop_prefixes = row_object_prefixes(_hop_rows, exclude=_hop_seen)
                if not _hop_prefixes:
                    break
                _hop_seen.update(_hop_prefixes)
                _hop_extra = await _bridge_complete_memories(
                    search_input, _hop_prefixes, run.ranked, limit=_bridge_max
                )
                if not _hop_extra:
                    break
                _row_hops_added += len(_hop_extra)
                run.ranked = _insert_at_window(run.ranked, _hop_extra, run.window)
                run.results = run.ranked[: run.target_k]
                run.window = max(run.window, min(len(run.ranked), run.window + len(_hop_extra)))
                run.synth_pool = run.ranked[: run.window]
                logger.info(
                    "memory.ask: row hop %d added %d memories for %s",
                    _hop_no + 1,
                    len(_hop_extra),
                    _hop_prefixes,
                )
                _hop_rows = list(_hop_extra)
            bridge_added += _row_hops_added


async def _complete_facets(run: _AskRun) -> None:
    """A count question reads the whole set: facet completion pulls
    every row sharing the hit keys' attribute facet and literal completion
    the raw turns naming the asked value, each widening the window."""
    search_input, _settings = run.search_input, run.settings
    # Facet completion: a count question is answered from the
    # WHOLE set of one attribute; top-k similarity returns the top k of it and
    # the reader counts a slice ("no ages in memories" with 8 age facts stored;
    # under noise the window fills with unrelated facts). Pull every in-scope
    # memory sharing the hit keys' attribute facet and widen the window.
    facets: list[str] = []
    facet_added = 0
    if run.ranked and is_count_intent(run.intent):
        facets = derive_key_facets(
            [getattr(h, "memory_key", "") for h in run.ranked[:12]], run.query_text
        )
        if facets:
            extra = await _facet_complete_memories(
                search_input,
                facets,
                run.ranked,
                limit=int(_settings.memory_ask_facet_completion_max or 60),
            )
            if extra:
                facet_added = len(extra)
                _prev_window = run.window
                run.window = max(
                    run.window,
                    int(_settings.memory_ask_aggregational_window or 40),
                )
                run.ranked = _insert_at_window(run.ranked, extra, _prev_window)
                run.results = run.ranked[: run.target_k]
                run.synth_pool = run.ranked[: run.window]
        # Literal completion: the raw turns that name the asked value — a fact the extractor
        # dropped is still a turn.
        from server.memory.app.ask_literal_evidence import literal_complete

        turns = await literal_complete(
            search_input,
            run.query_text,
            run.ranked,
            max_hits=int(_settings.memory_ask_literal_completion_max or 40),
        )
        if turns:
            run.window = max(
                run.window + len(turns),
                int(_settings.memory_ask_aggregational_window or 40),
            )
            run.ranked = run.ranked + turns
            run.results = run.ranked[: run.target_k]
            run.synth_pool = run.ranked[: run.window]
    run.facets, run.facet_added = facets, facet_added


async def _ground_and_cut(run: _AskRun, window_rel_floor: float | None) -> None:
    """Reconcile the synthesis pool with the bound source of record (code
    grounding), then cut the rows far below the best one's cosine."""
    search_input, _settings = run.search_input, run.settings
    # Grounding: reconcile the synthesis pool against an authoritative external
    # source of record so the answer doesn't drift (stale / wrong / not-yet-true
    # claims corrected or dropped). This is the pluggable seam — the memory core
    # stays domain-agnostic; the code/GitHub grounder is the one built-in
    # provider (PR-temporal gate + code enrichment + PR-fact grounding +
    # staleness oracle, in that order). A complete no-op unless an explicit code
    # index is bound, so non-code deployments are unaffected.
    from server.memory.app.code_grounding import apply_code_grounding
    from server.observability.otel import traced as _traced

    # Traced because the step sits between the retrieval legs and synthesis
    # (443 ms p50; 1.8 s on a code-bound ask).
    with _traced("ask.code_grounding", pool=len(run.synth_pool)):
        run.synth_pool = await apply_code_grounding(
            run.query_text, search_input, run.synth_pool, _settings
        )

    run.gate_mode, run.gate_kept, run.gate_dropped = "", 0, 0
    # ---- similarity knee ------------------------------------
    # LLM-free window cut: drop window rows whose cosine is far below the best
    # row's, never below the `memory_ask_relevance_gate_min_keep` floor. Off
    # unless the request sets `window_rel_floor`.
    rel_floor = float(window_rel_floor) if window_rel_floor is not None else 0.0
    if rel_floor > 0:
        from server.memory.domain.relevance_gate import similarity_floor_cut

        knee = similarity_floor_cut(
            run.synth_pool,
            rel_floor=rel_floor,
            min_keep=int(_settings.memory_ask_relevance_gate_min_keep or 3),
        )
        if knee.mode != "skipped":
            run.gate_mode = knee.mode
            run.gate_kept = len(knee.kept)
            run.gate_dropped = len(knee.dropped)
        if knee.dropped:
            _knee_ids = {id(m) for m in knee.dropped}
            run.ranked = [m for m in run.ranked if id(m) not in _knee_ids]
            run.synth_pool = knee.kept


async def _settle_window(run: _AskRun, *, token_budget: int | None) -> bool:
    """Settle the window the synthesizer reads: the token budget and the final
    superseded/stale pass over it. Returns whether the reader gets the record
    note (something superseded was dropped or masked)."""
    search_input, _settings = run.search_input, run.settings
    run.memories = _publish_window_keys(run.synth_pool)  # The cache's key axis

    # Synthesis-window token budget (memory.domain.ask_budget): the caller fixes the
    # INPUT the synthesizer reads instead of a count of fifteen excerpts. Applied
    # after every widening step (injection, knee cut) so the budget is what the
    # model actually sees; drops from the tail, never cuts an excerpt. None =
    # unbounded.
    from server.memory.domain.ask_budget import trim_to_token_budget

    run.memories, run.context_tokens_est, run.context_trimmed = trim_to_token_budget(
        list(run.memories), token_budget
    )
    # Superseded rows, final pass: every stage above may have added rows the
    # first pass never saw (decomposition, chase, bridges, recent turns,
    # digest descent). The window is settled here; drop and mask once
    # more over what the synthesizer will read.
    run.old_late = []
    if _drop_superseded(search_input):
        run.memories, run.old_late = split_pool_by_state(list(run.memories))
        run.memories, _tpl_late = resolve_templated_conflicts(run.memories)
        run.old_late = list(run.old_late) + _tpl_late
        run.superseded_dropped += len(run.old_late)
        _masks = list(run.stale_masks) + list(run.old_late)
        _masks += await superseded_rows_for_pool(search_input, run.memories)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "stale-mask final pass: %d mask row(s) %s over %d pool rows",
                len(_masks),
                [
                    (getattr(m, "memory_key", ""), str(getattr(m, "content_excerpt", ""))[:60])
                    for m in _masks
                ][:6],
                len(run.memories),
            )
        if _masks:
            run.memories, _late_masked = mask_stale_sentences(run.memories, _masks)
            run.stale_sentences_masked += _late_masked
    if logger.isEnabledFor(logging.DEBUG):
        for _m in run.memories:
            logger.debug(
                "ask pool row src=%s key=%s status=%s | %s",
                getattr(_m, "source", ""),
                getattr(_m, "memory_key", ""),
                getattr(_m, "status", ""),
                str(getattr(_m, "content_excerpt", "") or "")[:220].replace("\n", " "),
            )
    record_note = run.superseded_dropped > 0 or run.stale_sentences_masked > 0
    run.summary_hits = sum(
        1 for m in run.memories if str(getattr(m, "source", "") or "") == "summary"
    )
    return record_note


def _read_time(run: _AskRun) -> tuple[str, bool, Any]:
    """How the ask reads time: whether the question is temporal, the
    synthesis "now" (as_of) and the computed timeline block. Returns
    (timeline text, temporal question, as_of)."""
    search_input, _settings = run.search_input, run.settings
    # Computed timeline: for a question that reads time, the
    # dates and their deltas to the question date are rendered for the
    # synthesizer instead of being left to its arithmetic. Deterministic, no
    # LLM call.
    timeline_text = ""
    timeline_rows = 0
    # A question that reads time takes the timeline block and the raw turns
    # of its top rows.
    _, _order_mode0 = (
        evidence_order(run.memories, run.intent, run.query_text) if run.memories else ([], "")
    )
    temporal_question = looks_temporal(run.query_text, run.intent, _order_mode0)
    as_of = getattr(search_input, "as_of", None)
    if temporal_question and run.memories:
        from server.memory.domain import ask_timeline as _atl

        timeline_text, timeline_rows = _atl.timeline_block(
            run.memories,
            now=_atl.question_date(
                as_of,
                str(getattr(search_input, "observation_date", "") or ""),
            ),
        )
    run.timeline_rows = timeline_rows
    return timeline_text, temporal_question, as_of


def _omitted_rows(run: _AskRun) -> list[Any]:
    """The ranked rows the settled window does not show."""
    shown = {id(m) for m in run.memories}
    return [m for m in run.ranked if id(m) not in shown]


async def _tag_belt(ranked: list[Any], search_input: Any) -> list[Any]:
    """The last pool step before the window is cut: the hard tag belt.
    The ask's own legs — exact-identifier injection, the identifier chase's
    bridge completions and content fallbacks, entity-follow, the
    recommendation user rows, mention-time chunks — read
    `loom.memory` by key facet, substring or session and never carried the
    request's `tags`; on tagged subsystem questions they could fill the whole
    window with untagged rows.
    The belt runs after the last of them, reads the injected rows' tags in
    one statement and drops the ones outside the filter; a request with no
    hard filter is untouched. The count is logged, not on the wire."""
    from server.memory.app.search_tag_filter import enforce_hard_tag_filter

    work: dict[str, Any] = {}
    ranked = await enforce_hard_tag_filter(
        ranked, search_input, work, [str(getattr(search_input, "namespace", "") or "")]
    )
    if work.get("tag_filter_dropped"):
        logger.info(
            "memory.ask: %d injected row(s) outside the hard tag filter dropped",
            work["tag_filter_dropped"],
        )
    return ranked
