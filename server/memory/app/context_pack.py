"""Handler for context.pack (see memory/api/context.py).

`context.pack` builds the bounded startup context an agent wants at the
beginning of a session: editable working memory, always-loaded profile,
relevant durable memories, matching procedures, and optional knowledge /
conversation snippets.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from server.core.naming import SENTINEL as _WM_SENTINEL
from server.core.naming import named
from server.identity.app.request_scope import request_scope
from server.memory.api.models_context import ContextBlock, ContextPackInput, ContextPackOutput
from server.memory.api.models_lifecycle import MemoryStackInput
from server.memory.api.models_procedure import ProcedureMatchInput
from server.memory.api.models_profile import ProfileRenderInput
from server.memory.api.models_search import MemorySearchInput
from server.memory.api.models_working_memory import WorkingMemoryRenderInput
from server.memory.app.agent_rules import request_rule_agent, served_hits
from server.memory.domain.context_layout import (
    _BLOCK_LABELS,
    _DEFAULT_SECTION_ORDER,
    _DEFAULT_SECTION_TOKENS,
    RECALLED_SECTIONS,
    STACK_ROW_LAYERS,
    _ordered_section_names,
    _own_bookmark,
    _procedure_line,
    _render_pack,
    _section_budget,
    _stable_prefix,
    _tokens_estimate,
    _trim,
    author_label,
    escape_data,
    label_part,
    one_item,
    served_rows,
)
from server.memory.domain.decision_statement import states_reason
from server.memory.domain.evidence_pack import EvidenceItem, pack_evidence
from server.memory.domain.memory_types import excluded_from_unfiltered_search
from server.memory.domain.search_types import hit_row_key, names_row
from server.memory.domain.served import hit_row
from server.memory.store.shared import SHARED_AGENT

log = logging.getLogger(__name__)


def _without_transcript_rows(hits: list[Any]) -> list[Any]:
    """The pack is ambient recall (the prompt hook and every session start read it), so the
    agent's own trajectory transcript (rows like "Step 213/278 · After Bash `command=…`")
    stays out of it; `include_runs` is the pack's path to the trajectory, and `loom_search`
    still finds it. The hits' tags are asked for and dropped from the wire after the filter."""
    from server.memory.domain.transcript_rows import is_transcript_row

    kept = [h for h in hits if not is_transcript_row(getattr(h, "tags", None), "")]
    for h in kept:
        h.tags = None
    return kept


def _explore_this_pack(input: ContextPackInput, situation: str) -> bool:
    """Withhold the top procedure on this pack? The rate is the request's, else the
    deployment setting; the draw is a hash of the pack's identity and situation, so the same
    session asking the same thing gets the same answer and the rate holds across sessions."""
    import hashlib

    rate = float(input.procedure_explore_rate or 0.0)
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    seed = "|".join(
        [
            input.org,
            input.namespace,
            str(input.session_id or ""),
            str(input.run_id or ""),
            situation.strip(),
        ]
    )
    draw = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) / float(2**32)
    return draw < rate


async def _advisory_health(
    input: ContextPackInput, wanted: Callable[[str], bool], put: Callable[..., None]
) -> None:
    """The store tells the agent when its namespace is being used as something other than a
    memory, at session start, without being asked. Cached per (org, namespace); nothing
    rendered when healthy; a failed lookup is an empty section, never an error."""
    from server.memory.app import namespace_health as _nh

    if not wanted("namespace_health"):
        return
    health = await _nh.namespace_health(input.org, input.namespace)
    if health is None:
        return
    body = _nh.render_health(input.namespace, health)
    if body:
        put(
            "namespace_health",
            body,
            source_count=len(health.findings),
            extra_meta={
                "criticals": health.criticals,
                "warnings": health.warnings,
                "codes": [f.code for f in health.findings],
            },
        )


async def _pack_namespace_guide(st: _PackState) -> None:
    """The namespace's guide (`namespace.guide_get`): what the namespace holds and
    how to use it, cut to the section budget, under a line naming its revision,
    writer and summary. Nothing when the namespace has none."""
    from server.identity.app.namespace_guide import current_guide
    from server.identity.app.request_scope import request_narrowed

    input = st.input
    if not st.wanted("namespace_guide"):
        return
    guide = await current_guide(input.org, input.namespace)
    if not guide.found:
        return
    # A token narrowed to one user is not told the writer; say so rather than `unknown`.
    writer = " by=hidden" if request_narrowed() else author_label([guide.updated_by])
    head = f"- [guide/{guide.revision}{writer}] {guide.summary}"
    # One item: every line of the guide indented under its head, so a line of the
    # stored text cannot pass as an item with its own by=.
    body = one_item(f"{head}\n{guide.content}")
    budget = _section_budget(input, "namespace_guide", st.default_section_tokens["namespace_guide"])
    # Measured as `put` stores it (escaped), which is what its trim cuts.
    if len(escape_data(body)) > budget:
        body = one_item(f"{head} (full text: namespace.guide_get)\n{guide.content}")
    st.put(
        "namespace_guide",
        body,
        citations=[f"guide:{guide.revision}"],
        source_count=1,
        extra_meta={"revision": guide.revision},
    )


async def _advisory_mentor(
    input: ContextPackInput, wanted: Callable[[str], bool], put: Callable[..., None]
) -> None:
    """The store names the plane this agent is not using, from its own ledger profile, at
    session start. Cached per (org, namespace, agent); nothing rendered when no rule fires; a
    failed read is an empty section, never an error."""
    from server.insights.app import mentor as _mentor_svc

    if not wanted("mentor"):
        return
    profile = await _mentor_svc.mentor_profile(input.org, input.namespace, input.agent or "-")
    body = _mentor_svc.render_pack_section(profile)
    if body and profile is not None:
        put(
            "mentor",
            body,
            source_count=len(profile.hints),
            extra_meta={
                "codes": [h.code for h in profile.hints],
                "shown": [h.code for h in profile.hints[: _mentor_svc.PACK_MAX_HINTS]],
                "sessions": profile.sessions,
            },
        )


@dataclass(slots=True)
class _PackState:
    """What the section producers write and the assembly reads: the rendered
    sections and their meta (in completion order), whether any section was
    trimmed, and the procedure keys shown / withheld."""

    input: ContextPackInput
    default_section_tokens: dict[str, int]
    sections: dict[str, str] = field(default_factory=dict)
    section_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    truncated: bool = False
    shown_procedures: list[str] = field(default_factory=list)
    withheld_procedures: list[str] = field(default_factory=list)
    # Per section, the stored row (namespace, memory_key) behind each item line, in line
    # order; None for an item that is no stored row. The rows whose line the rendered pack
    # still holds are what the op records as served (`served_rows`).
    item_rows: dict[str, list[tuple[str, str] | None]] = field(default_factory=dict)

    def put(
        self,
        name: str,
        body: str,
        *,
        citations: list[str] | None = None,
        source_count: int = 0,
        extra_meta: dict[str, Any] | None = None,
        rows: list[tuple[str, str] | None] | None = None,
    ) -> None:
        input = self.input
        default_section_tokens = self.default_section_tokens
        if not body:
            return
        self.item_rows[name] = list(rows or [])
        if name in RECALLED_SECTIONS:
            body = escape_data(body)
        trimmed, was_truncated = _trim(
            body,
            _section_budget(input, name, default_section_tokens.get(name, 300)),
        )
        self.sections[name] = trimmed
        self.truncated = self.truncated or was_truncated
        _budget_chars = _section_budget(input, name, default_section_tokens.get(name, 300))
        meta = {
            "source_count": source_count,
            "citations": citations or [],
            "truncated": was_truncated,
            "char_count": len(trimmed),
            # Per-section pressure: how much of this section's own budget the
            # body used BEFORE trimming (>1.0 means it was cut).
            "tokens_budget": _budget_chars // 4,
            "fill_ratio": round(len(body) / _budget_chars, 3) if _budget_chars else 0.0,
        }
        if extra_meta:
            meta.update(extra_meta)
        self.section_meta[name] = meta

    def wanted(self, name: str) -> bool:
        return not self.input.only_sections or name in self.input.only_sections


async def _pack_working_memory(st: _PackState) -> None:
    input = st.input
    if not st.wanted("working_memory"):
        return
    blocks, rendered = await _working_memory_layers(input)
    if rendered:
        st.put(
            "working_memory",
            rendered,
            citations=[f"working_memory:{b.block_key}" for b in blocks],
            source_count=len(blocks),
        )


async def _pack_memory_stack(st: _PackState) -> None:
    input = st.input
    default_section_tokens = st.default_section_tokens
    if not st.wanted("memory_stack"):
        return
    from server.memory.app.stack import handle_memory_stack

    stack_budget_chars = _section_budget(
        input,
        "memory_stack",
        default_section_tokens.get("memory_stack", 700),
    )
    out = await handle_memory_stack(
        MemoryStackInput(
            org=input.org,
            namespace=input.namespace,
            agent=input.agent,
            user_id=input.user_id,
            session_id=input.session_id,
            run_id=input.run_id,
            token_budget=max(200, stack_budget_chars // 4),
            include_knowledge=input.include_knowledge,
            knowledge_top_k=min(input.top_k, 5),
            seed_query=input.situation or input.query,
            procedure_limit=min(input.top_k, 5),
        )
    )
    if not out.entries:
        return
    lines: list[str] = []
    citations: list[str] = []
    rows: list[tuple[str, str] | None] = []
    layers: list[str] = []
    for entry in out.entries:
        layer = entry.section
        if layer not in layers:
            layers.append(layer)
        cite = f"stack:{layer}/{entry.memory_key or entry.memory_type}"
        citations.append(cite)
        # the stack reads the request's namespace only
        stored = layer in STACK_ROW_LAYERS and entry.memory_key
        rows.append((input.namespace, entry.memory_key) if stored else None)
        author = author_label([entry.actor]) if layer in STACK_ROW_LAYERS else ""
        label = label_part(f"{layer}/{entry.memory_key}")
        lines.append(one_item(f"- [{label}{author}] {entry.value}"))
    st.put(
        "memory_stack",
        "\n".join(lines),
        citations=citations,
        rows=rows,
        source_count=len(out.entries),
        extra_meta={
            "stack_layers": layers,
            "tokens_used": out.tokens_used,
            "tokens_budget": out.tokens_budget,
            "stack_truncated": out.truncated,
        },
    )


async def _pack_profile(st: _PackState) -> None:
    input = st.input
    if not st.wanted("profile"):
        return
    from server.memory.app.profile import handle_profile_render

    out = await handle_profile_render(
        ProfileRenderInput(
            org=input.org,
            namespace=input.namespace,
            agent=input.agent,
            user_id=input.user_id,
            session_id=input.session_id,
            run_id=input.run_id,
            format="xml",
            scope="namespace",
            read_scope=input.read_scope,
        )
    )
    if out.rendered:
        st.put(
            "profile",
            out.rendered,
            citations=[f"profile:{label}" for label in out.labels],
            source_count=out.section_count,
        )


async def _pack_startup_recall(st: _PackState) -> None:
    """Zero-query startup recall: at session start an agent wants cheap
    pins + recency, not a paid semantic search. Pull pinned memories first,
    then the most-recent active ones (recency-ordered), dedup, and render
    the memories block — no embedding round-trip. Fires only for
    mode='startup' with no query; real queries still go through search."""
    input = st.input
    default_section_tokens = st.default_section_tokens
    import json as _json

    import server.memory.store as memory_repo

    def _vtext(v: Any) -> str:
        # Stored values are often JSON-encoded strings ('"…"'); decode those
        # to the bare text, pass dicts/lists through as compact JSON.
        if isinstance(v, str):
            try:
                dec = _json.loads(v)
                return dec if isinstance(dec, str) else v
            except Exception:  # noqa: BLE001 — not JSON: the raw text
                return v
        return _json.dumps(v, default=str)

    # The identity SENTINEL ("-") means unset, as on the query path; "*" drops the axis.
    ident = memory_repo.Identity(
        org=input.org,
        namespace=input.namespace,
        agent="*",
        user_id=named(input.user_id) or "*",
        session_id=named(input.session_id) or "*",
    )
    k = input.top_k
    # Transcripts leave in the statement, before the limit: every session's tool calls
    # would otherwise fill the window. Across sessions the recall also leaves out what an
    # unfiltered memory.search does (session replays, profiles, reflections).
    shape: dict[str, Any] = {
        "exclude_high_sensitivity": True,
        "exclude_transcripts": True,
        "exclude_memory_types": (
            excluded_from_unfiltered_search() if ident.session_id == "*" else []
        ),
        "order_by": "created_at_desc",
        "scope": request_scope(),
    }
    try:
        pinned = await memory_repo.list_by_namespace(ident, pinned_only=True, limit=k, **shape)
        recent = await memory_repo.list_by_namespace(ident, limit=k * 2, **shape)
    except Exception as e:  # noqa: BLE001 — recall is best-effort
        log.warning("context.pack startup recall failed: %s", e)
        return

    seen: set[str] = set()
    rows: list[Any] = []
    for r in [*pinned, *recent]:  # pins first, then recency; dedup by key
        if r.memory_key in seen or r.status not in ("active", "conflicted"):
            continue
        seen.add(r.memory_key)
        rows.append(r)
        if len(rows) >= k:
            break
    if not rows:
        return

    evidence = [
        EvidenceItem(
            citation=f"memory:{input.namespace}/{r.memory_key}",
            text=_vtext(r.value),
            source="fact",
            group=f"fact:{input.namespace}",
            score=float(r.confidence or 0.0),
            temporal_anchor=(
                r.valid_at.isoformat()
                if r.valid_at
                else r.created_at.isoformat()
                if r.created_at
                else ""
            ),
            conflict=(r.status == "conflicted"),
            author=author_label([getattr(r, "actor", "")]),
        )
        for r in rows
    ]
    packed = pack_evidence(
        evidence,
        char_budget=_section_budget(
            input,
            "memories",
            default_section_tokens.get("memories", 300),
        ),
    )
    row_of = {f"memory:{input.namespace}/{r.memory_key}": r.memory_key for r in rows}
    st.put(
        "memories",
        packed.rendered,
        citations=packed.citations,
        rows=[(input.namespace, row_of[c]) for c in packed.citations],
        source_count=packed.source_count,
        extra_meta={
            "recall_mode": "startup_pins_recency",
            "query_free": True,
            "evidence_groups": packed.groups,
            "evidence_truncated": packed.truncated,
        },
    )


def _hit_ns_key(input: ContextPackInput, h: Any) -> tuple[str, str]:
    """The (namespace, memory_key) of the stored row a hit came from: a chunk's parent."""
    return hit_row(h, input.namespace)


_KeyWriters = dict[tuple[str, str], list[dict[str, str]]]


def _hit_author(actors: _KeyWriters, input: ContextPackInput, h: Any) -> str:
    """` by=` for the row a hit came from. Rows under the hit's key are narrowed by the
    identity the hit carries (user, session, agent, where the leg read them); when the rows
    left were written by more than one actor the label says `unknown`, never a writer whose
    row this may not be."""
    rows = actors.get(_hit_ns_key(input, h)) or []
    is_fact_row = names_row(h) and getattr(h, "source", "fact") != "chunk"
    row = hit_row_key(h, input.namespace) if is_fact_row else None
    # A fact hit names its memory row. A chunk's identity is its chunk row's, and a shared
    # hit's the promoted row's, which has no actor here: both narrow by the hit's own
    # user, session and agent fields.
    narrow: list[tuple[str, Any]]
    if row is not None and row.agent != SHARED_AGENT:
        narrow = [("agent", row.agent), ("user_id", row.user_id), ("session_id", row.session_id)]
    else:
        narrow = [
            ("user_id", getattr(h, "user_id", "")),
            ("session_id", getattr(h, "session_id", "")),
            ("agent", getattr(h, "_row_agent", None)),
        ]
    for col, value in narrow:
        if value:
            rows = [r for r in rows if r[col] == value]
    names = {r["actor"] for r in rows}
    return author_label(names if len(names) == 1 else [])


async def _hit_actors(input: ContextPackInput, hits: list[Any]) -> _KeyWriters:
    """Each hit's recorded writers, read inside the request's read scope. {} when the
    read fails: the lines then say by=unknown rather than naming anyone."""
    import server.memory.store as memory_repo

    try:
        return await memory_repo.actors_by_key(
            input.org,
            [_hit_ns_key(input, h) for h in hits],
            scope=request_scope(),
        )
    except Exception as e:  # noqa: BLE001 — authorship labels are best-effort; lines say by=unknown
        log.warning("context.pack: memory authors unavailable: %s", e)
        return {}


async def _pack_decisions_from_hits(
    st: _PackState, decision_hits: list[Any], actors: _KeyWriters, *, abstained: bool
) -> None:
    """Decision-typed hits leave the memories block and render as their
    own tagged section with the structured record (scenario, outcome,
    realized outcome). One search, two planes on the wire: the reader
    sees which plane each line came from. A decision recorded as proposed
    reads "proposed" instead of "decided"."""
    from server.memory.app.search.scoring import one_per_key

    input = st.input
    # a decision recorded in several sessions is one line
    decision_hits = one_per_key(decision_hits, input.namespace)
    keys = [str(h.memory_key) for h in decision_hits]
    records: dict[str, dict[str, Any]] = {}
    # The records are read by key alone, across users: a bounded read keeps the hit's excerpt.
    narrowed = request_scope().narrowed
    try:
        from server.core.ch import get_client
        from server.memory.app.decision import _decision_records

        if not narrowed:
            records = await _decision_records(get_client(), input.org, input.namespace, keys)
    except Exception as e:  # noqa: BLE001 — the excerpt is the fallback
        log.warning("context.pack: decision records unavailable: %s", e)
    lines: list[str] = []
    citations: list[str] = []
    for h in decision_hits:
        ns = getattr(h, "namespace", "") or input.namespace
        rec = records.get(str(h.memory_key)) or {}
        scenario = (
            str(rec.get("scenario") or "").strip()
            or str(getattr(h, "content_excerpt", "") or "").strip()
        )
        outcome = str(rec.get("outcome") or "").strip()
        realized = str(rec.get("realized_outcome") or "").strip()
        reasoning = str(rec.get("reasoning") or "").strip()
        made = str(rec.get("made_at") or "")[:10]
        author = _hit_author(actors, input, h)
        line = f"- [{label_part(f'{ns}/{h.memory_key}')}{author}]"
        line += (f" {made}" if made else "") + f" {scenario}"
        if outcome:
            line += f" → {rec['stage']}: {outcome}"
        if states_reason(rec):
            line += f" (because {reasoning[:160]})"
        if realized:
            succ = rec.get("success")
            verdict = " (succeeded)" if succ is True else " (failed)" if succ is False else ""
            line += f"; realized: {realized}{verdict}"
        lines.append(one_item(line))
        citations.append(f"decision:{ns}/{h.memory_key}")
    st.put(
        "decisions",
        "\n".join(lines),
        citations=citations,
        # An abstaining search's rows are no answer: the answer marks them so, and a
        # client drops them (the prompt-recall hook does), so none counts as served.
        rows=None
        if abstained
        else [(h.namespace or input.namespace, str(h.memory_key)) for h in decision_hits],
        source_count=len(decision_hits),
    )


async def _pack_memories(st: _PackState) -> None:
    input = st.input
    default_section_tokens = st.default_section_tokens
    if not st.wanted("memories"):
        return
    if not input.query:
        # Zero-query startup pack: opinionated pins+recency recall so a
        # session bootstrap gets useful memories without a paid search.
        # Other modes stay query-driven (no query → no memories section).
        if input.mode == "startup":
            await _pack_startup_recall(st)
        return
    from server.memory.app.search import handle_memory_search

    # The block assembler runs inside memory.search, so thread pack's
    # per-section budget down in TOKENS to match it. _section_budget
    # returns chars (tokens*4); convert back.
    _memories_token_budget = (
        _section_budget(input, "memories", default_section_tokens.get("memories", 300)) // 4
    )
    # The identity SENTINEL ("-") means unset; memory.search reads None as
    # "no filter", so map it rather than filtering on a literal "-".
    search_input = MemorySearchInput(
        org=input.org,
        namespace=input.namespace,
        agent="*",
        user_id=named(input.user_id),
        session_id=named(input.session_id),
        read_scope=input.read_scope,
        query=input.query,
        top_k=input.top_k,
        auto_touch=False,
        quality_filter=True,  # facts, not junk
        bodies="full",  # the pack budgets tokens itself, not the excerpt tier
        recipe=(
            "quality"
            if input.mode == "debug"
            else "fast"
            if input.mode == "startup"
            else "balanced"
        ),
        include_top_n_unmatched=20 if input.include_chunks else 0,
        include_tags=True,
    )
    search_input._context_pack_block_tokens = int(_memories_token_budget)
    # A pack runs on every chat turn: its read costs embeddings and ClickHouse reads,
    # and no LLM call in any mode, while the recipe keeps its recall width. A caller
    # that wants the recipe's LLM legs calls memory.search.
    search_input._llm_free = True
    out = await handle_memory_search(search_input)
    hits = _without_transcript_rows(list(out.results or []))
    actors = await _hit_actors(input, hits) if hits else {}
    if input.include_decisions and st.wanted("decisions"):
        decision_hits = [h for h in hits if str(getattr(h, "memory_type", "") or "") == "decision"]
        if decision_hits:
            dec_ids = {id(h) for h in decision_hits}
            hits = [h for h in hits if id(h) not in dec_ids]
            await _pack_decisions_from_hits(st, decision_hits, actors, abstained=out.abstain)
    if hits:
        evidence: list[EvidenceItem] = []
        row_of: dict[str, tuple[str, str]] = {}
        for h in hits:
            ns = h.namespace or input.namespace
            source = getattr(h, "source", "fact") or "fact"
            resource = getattr(h, "source_resource_id", "") or ""
            cite = f"memory:{ns}/{h.memory_key}"
            if resource:
                cite = f"{cite}#{resource}"
            row_of[cite] = _hit_ns_key(input, h)  # a chunk's parent
            group = resource or f"{source}:{ns}"
            evidence.append(
                EvidenceItem(
                    citation=cite,
                    text=h.content_excerpt,
                    source=source,
                    group=group,
                    score=float(getattr(h, "rank_score", 0.0) or 0.0),
                    # Prefer the in-text event date (temporal_anchor)
                    # over the session date (valid_at): the rendered date
                    # is when the event happened, falling back to when it
                    # was discussed.
                    temporal_anchor=(
                        getattr(h, "temporal_anchor", "") or getattr(h, "valid_at", "") or ""
                    ),
                    # the row state rides `status`; `memory_type` is never "conflicted"
                    conflict=getattr(h, "status", "") == "conflicted",
                    author=_hit_author(actors, input, h),
                    derived_from=tuple(getattr(h, "derived_from", None) or ()),
                )
            )
        packed = pack_evidence(
            evidence,
            char_budget=_section_budget(
                input,
                "memories",
                default_section_tokens.get("memories", 300),
            ),
            # Keep relevance (rank) order in the packed block; the event
            # date rides each line as a label, not as a sort key.
        )
        st.put(
            "memories",
            packed.rendered,
            citations=packed.citations,
            # see _pack_decisions_from_hits: an abstaining search shows no answer
            rows=None if out.abstain else [row_of[c] for c in packed.citations],
            source_count=packed.source_count,
            extra_meta={
                "evidence_groups": packed.groups,
                "evidence_truncated": packed.truncated,
                "abstain": out.abstain,
                "abstain_reason": out.abstain_reason,
                "retrieval_confidence": out.retrieval_confidence,
                "retrieval_risk": out.retrieval_risk,
                "route_agreement": out.route_agreement,
            },
        )


async def _pack_procedures(st: _PackState) -> None:
    input = st.input
    if not st.wanted("procedures"):
        return
    situation = input.situation or input.query
    if not situation:
        return
    from server.memory.app.procedure import handle_procedure_match, own_procedures

    # Two-factor match when the caller separated the state it is in from
    # the objective it pursues: the situation scores the precondition
    # axis, the query the goal axis. One string alone matches by situation.
    goal = (
        input.query.strip()
        if input.situation and input.query and input.query.strip() != situation.strip()
        else None
    )
    out = await handle_procedure_match(
        ProcedureMatchInput(
            org=input.org,
            namespace=input.namespace,
            agent="*",
            user_id=input.user_id,
            session_id=input.session_id,
            situation=situation,
            goal=goal,
            top_k=min(input.top_k, 5),
        )
    )
    matches = own_procedures(list(out.matches))
    explore_meta: dict[str, Any] = {}
    if matches and _explore_this_pack(input, situation):
        # Exploration: withhold the top match so the ledger learns how the run goes without
        # it. Deterministic per (org, namespace, session, run, situation): a session that
        # asks twice gets the same answer twice.
        held = matches.pop(0)
        st.withheld_procedures.append(str(held.memory_key))
        explore_meta = {"explore": True, "withheld": [str(held.memory_key)]}
    if matches:
        lines = []
        citations = []
        for m in matches:
            steps = "; ".join(m.steps[:6])
            citations.append(f"procedure:{m.namespace}/{m.memory_key}")
            st.shown_procedures.append(str(m.memory_key))
            # Trust labels ride the line: an unexercised machine-mined
            # procedure is advice to weigh, not a script; fail-prone steps
            # are pitfalls the runs exposed.
            status = getattr(m, "status", "authored")
            label = " (unproven)" if status == "candidate" else ""
            prone = list(getattr(m, "fail_prone_steps", []) or [])
            avoid = f" | pitfalls: {'; '.join(prone[:3])}" if prone else ""
            lines.append(_procedure_line(m, label, steps, avoid))
        st.put(
            "procedures",
            "\n".join(lines),
            citations=citations,
            rows=[(str(m.namespace or input.namespace), str(m.memory_key)) for m in matches],
            source_count=len(matches),
            extra_meta=explore_meta or None,
        )


async def _pack_knowledge(st: _PackState) -> None:
    input = st.input
    default_section_tokens = st.default_section_tokens
    if not st.wanted("knowledge"):
        return
    if not input.include_knowledge or not input.query:
        return
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput
    from server.knowledge.app.knowledge_search import handle_knowledge_search

    agent = request_rule_agent(input.agent)

    async def search(top_k: int) -> list[Any]:
        out = await handle_knowledge_search(
            KnowledgeSearchInput(
                org=input.org,
                namespace=input.namespace,
                query=input.query,
                top_k=top_k,
                namespace_scope="all_mine",
                # A pack runs on every chat turn: its knowledge read makes no rerank call.
                rerank="none",
            )
        )
        return list(out.results)

    # Another bot's promoted rule is not a rule about this agent.
    results = await served_hits(
        await search(input.top_k), search, lambda h: h.tags, agent, input.top_k
    )
    if results:
        evidence = [
            EvidenceItem(
                # Cite the section, not just the document, when the chunk
                # knows where it sits: knowledge:ns/uri#A > B > C.
                citation=(
                    f"knowledge:{h.namespace or input.namespace}/{h.source_uri}"
                    + (f"#{h.section}" if getattr(h, "section", "") else "")
                ),
                text=h.content,
                source="knowledge",
                group=h.source_uri,
                score=float(getattr(h, "score", 0.0) or 0.0),
            )
            for h in results
        ]
        packed = pack_evidence(
            evidence,
            char_budget=_section_budget(
                input,
                "knowledge",
                default_section_tokens.get("knowledge", 300),
            ),
        )
        st.put(
            "knowledge",
            packed.rendered,
            citations=packed.citations,
            source_count=packed.source_count,
            extra_meta={
                "evidence_groups": packed.groups,
                "evidence_truncated": packed.truncated,
            },
        )


async def _pack_conversations(st: _PackState) -> None:
    input = st.input
    default_section_tokens = st.default_section_tokens
    if not st.wanted("conversations"):
        return
    if not input.include_conversations or not input.query:
        return
    if request_scope().narrowed:
        return  # conversations carry no user bound this leg can apply
    from server.knowledge.api.models_conversations import ConversationSearchInput
    from server.knowledge.app.conversations import handle_conversation_search

    out = await handle_conversation_search(
        ConversationSearchInput(
            org=input.org,
            namespace=input.namespace,
            agent="*",
            query=input.query,
            top_k=input.top_k,
            namespace_scope="all_mine",
        )
    )
    if out.hits:
        evidence = [
            EvidenceItem(
                citation=f"conversation:{h.thread_id}#{h.msg_id}",
                text=h.content_excerpt,
                source="conversation",
                group=str(h.thread_id),
                # conversation.search hits carry no score.
                score=0.0,
            )
            for h in out.hits
        ]
        packed = pack_evidence(
            evidence,
            char_budget=_section_budget(
                input,
                "conversations",
                default_section_tokens.get("conversations", 300),
            ),
        )
        st.put(
            "conversations",
            packed.rendered,
            citations=packed.citations,
            source_count=packed.source_count,
            extra_meta={
                "evidence_groups": packed.groups,
                "evidence_truncated": packed.truncated,
            },
        )


async def _pack_runs(st: _PackState) -> None:
    input = st.input
    # The manifest of recent sessions and runs, for the agent that reads its own history.
    # Opt-in; identity-scoped; fail-open inside runs_manifest.
    if not st.wanted("runs") or not input.include_runs:
        return
    if request_scope().narrowed:
        return  # the manifest reads every session of a user, past a session bound
    from server.memory.app.runs_manifest import runs_manifest

    body, count = await runs_manifest(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        limit=int(input.runs_limit),
    )
    if body:
        st.put("runs", body, source_count=count, extra_meta={"sessions": count})


async def _gather_sections(st: _PackState) -> None:
    """Run every section producer concurrently; each writes its own block
    into `st`. A producer that raises is logged and leaves its block empty."""
    import asyncio

    input = st.input
    # Best-effort assembly: one section failing (a table a pending migration has
    # not created yet, a slow upstream) degrades that block to empty rather than
    # 500-ing the whole pack. return_exceptions keeps the others' results.
    # gather() runs in _DEFAULT_SECTION_ORDER; zip the results back to it.
    _results = await asyncio.gather(
        _pack_working_memory(st),
        _pack_profile(st),
        _advisory_health(input, st.wanted, st.put),
        _pack_namespace_guide(st),
        _advisory_mentor(input, st.wanted, st.put),
        _pack_memory_stack(st),
        _pack_memories(st),
        _pack_procedures(st),
        _pack_knowledge(st),
        _pack_conversations(st),
        _pack_runs(st),
        return_exceptions=True,
    )
    # `decisions` is rendered by the memories coroutine (same search), so it
    # has no coroutine of its own in the gather.
    _gathered = [n for n in _DEFAULT_SECTION_ORDER if n != "decisions"]
    for _name, _res in zip(_gathered, _results, strict=True):
        if isinstance(_res, Exception):
            log.warning("context.pack: section %s failed: %s", _name, _res)


def _in_block_order(
    st: _PackState,
) -> tuple[dict[str, str], dict[str, dict[str, Any]], list[tuple[str, str]]]:
    """The sections and their meta in block order, and every section name
    in that order paired with its body (empty when it rendered nothing)."""
    input = st.input
    ordered_names = _ordered_section_names(input.block_order)
    # The section coroutines run concurrently and each writes its own key into
    # `sections` / `section_meta` when it finishes, so insertion order is completion order:
    # two identical calls could serialise the same content with the keys in a different
    # order. Provider prompt caches (and any KV-cache layer in front of a self-hosted engine)
    # match on a byte-identical prefix, so a reordered key is a cache miss on everything
    # after it. Rebuild both dicts in block order so the wire bytes are a function of the
    # content alone.
    sections = {name: st.sections[name] for name in ordered_names if st.sections.get(name)}
    section_meta = {
        name: st.section_meta[name] for name in ordered_names if name in st.section_meta
    }
    ordered = [(name, sections.get(name, "")) for name in ordered_names]
    return sections, section_meta, ordered


def _context_blocks(
    ordered: list[tuple[str, str]], section_meta: dict[str, dict[str, Any]]
) -> list[ContextBlock]:
    """One ContextBlock per section that rendered, priority = block order."""

    blocks = [
        ContextBlock(
            label=_BLOCK_LABELS.get(name, name),
            section=name,
            rendered=body,
            tokens_estimate=_tokens_estimate(body),
            citations=list(section_meta.get(name, {}).get("citations") or []),
            priority=idx,
            truncated=bool(section_meta.get(name, {}).get("truncated")),
            source_count=int(section_meta.get(name, {}).get("source_count") or 0),
        )
        for idx, (name, body) in enumerate(ordered)
        if body
    ]
    return blocks


async def handle_context_pack(input: ContextPackInput) -> ContextPackOutput:
    st = _PackState(input=input, default_section_tokens=_DEFAULT_SECTION_TOKENS[input.mode])
    char_budget = int(input.token_budget) * 4

    await _gather_sections(st)

    sections, section_meta, ordered = _in_block_order(st)
    blocks = _context_blocks(ordered, section_meta)
    present, rendered_parts, rendered, was_truncated = _render_pack(ordered, char_budget)
    truncated = st.truncated or was_truncated
    stable_sections, prefix_len, stable_hash = _stable_prefix(
        input, present, rendered_parts, rendered
    )

    out = ContextPackOutput(
        procedure_keys=list(dict.fromkeys(st.shown_procedures)),
        procedure_keys_withheld=list(dict.fromkeys(st.withheld_procedures)),
        rendered=rendered,
        sections=sections,
        blocks=blocks,
        section_meta=section_meta,
        section_count=len([v for v in sections.values() if v]),
        truncated=truncated,
        token_budget=input.token_budget,
        mode=input.mode,
        tokens_estimate=_tokens_estimate(rendered),
        fill_ratio=round(_tokens_estimate(rendered) / input.token_budget, 3)
        if input.token_budget
        else 0.0,
        stable_sections=stable_sections,
        stable_prefix_chars=prefix_len,
        stable_hash=stable_hash,
    )
    out._served_rows = served_rows(rendered, [name for name, _ in present], st.item_rows)
    return out


async def _working_memory_layers(input: Any) -> tuple[list[Any], str]:
    """The pack's working memory, narrowest scope first, one block per key.

    A pack for a RUN also carries the session's blocks (run `-`). A pack for a SESSION also
    carries the agent's blocks (session `-`, run `-`), where the plugin's "where was I" note
    (`session:<project>`) and any note an MCP client left for the next agent live. The
    SessionStart primer names its new session, so the agent layer is the only one holding
    anything the first time; without it the primer would never show the note it was written
    for."""
    from html import escape as _esc

    from server.memory.app.working_memory import handle_working_memory_render

    mine = str(getattr(input, "session_block", "") or "")
    run = input.run_id or _WM_SENTINEL
    session = input.session_id or _WM_SENTINEL
    layers = [(session, run)]
    if run != _WM_SENTINEL:
        layers.append((session, _WM_SENTINEL))
    if session != _WM_SENTINEL:
        layers.append((_WM_SENTINEL, _WM_SENTINEL))
    blocks: list[Any] = []
    for sid, rid in layers:
        out = await handle_working_memory_render(
            WorkingMemoryRenderInput(
                org=input.org,
                namespace=input.namespace,
                agent=input.agent,
                user_id=input.user_id,
                session_id=sid,
                run_id=rid,
                format="xml",
                limit=20,
            )
        )
        seen = {b.block_key for b in blocks}
        blocks.extend(b for b in out.blocks if b.block_key not in seen and _own_bookmark(b, mine))
    # Each block is rendered whole, so a multi-line note (the plugin's session state) keeps
    # its body.
    rendered = "\n".join(
        f'<working-memory block="{_esc(b.block_key, quote=True)}">'
        f"{escape_data(b.content)}</working-memory>"
        for b in blocks
    )
    return blocks, rendered
