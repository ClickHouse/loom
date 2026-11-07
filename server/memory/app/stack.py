"""memory.stack: the layered, token-budgeted recall bundle (profile, pinned,
recent recall, procedures, knowledge) one call assembles."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from server.core.ch import get_client
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import scope_and
from server.memory.api.models_lifecycle import MemoryStackInput, MemoryStackOutput, _StackEntry
from server.memory.app.agent_rules import request_rule_agent, served_hits

log = logging.getLogger(__name__)


def recent_recall_admits_medium(seed_query: str) -> bool:
    """memory.stack's recent_recall section carries `medium` (contact-detail or IBAN) rows
    only when the query that seeded the stack asks for a contact detail or a bank account.
    An empty seed query (a startup/bootstrap pack) never does."""
    from server.memory.domain.sensitivity import contact_intent

    return bool(seed_query) and contact_intent(seed_query)


def _value_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return json.dumps(v, default=str)


def _est_tokens(s: str) -> int:
    return max(1, len(s) // 4)


@dataclass
class _StackFill:
    """The bundle as it fills: entries, tokens used against the budget, and
    whether a section was cut."""

    budget: int
    entries: list[_StackEntry] = field(default_factory=list)
    used: int = 0
    truncated: bool = False
    section_counts: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(
            ("profile", "pinned", "recent_recall", "procedure", "knowledge"), 0
        )
    )

    def fits(self, cost: int) -> bool:
        """Whether `cost` more tokens fit; a miss marks the bundle truncated."""
        if self.used + cost > self.budget:
            self.truncated = True
            return False
        return True

    def add(self, entry: _StackEntry) -> None:
        self.entries.append(entry)
        self.used += entry.tokens
        self.section_counts[entry.section] += 1


async def _stack_profile(input: MemoryStackInput, fill: _StackFill) -> None:
    """Section 1: profile via profile.render. Delegate so the fragment matches
    the SPA (handles agent fan-out, char_limit ordering, formatting)."""
    try:
        from server.memory.api.models_profile import ProfileRenderInput
        from server.memory.app.profile import handle_profile_render

        prof_input = ProfileRenderInput(
            org=input.org,
            namespace=input.namespace,
            agent=input.agent,
            user_id=input.user_id,
            session_id=input.session_id,
            format="markdown",
            scope="namespace",
        )
        prof_out = await handle_profile_render(prof_input)
        rendered = prof_out.rendered.strip()
        if rendered:
            cost = _est_tokens(rendered)
            if fill.fits(cost):
                fill.add(
                    _StackEntry(
                        section="profile",
                        memory_key="profile.render",
                        memory_type="profile",
                        value=rendered,
                        tokens=cost,
                    )
                )
                fill.section_counts["profile"] = prof_out.section_count
    except Exception as e:  # noqa: BLE001 — a failed profile section must not fail the stack
        log.warning("memory.stack: profile.render fan-out failed: %s", e)


async def _stack_pinned(ch: Any, input: MemoryStackInput, fill: _StackFill) -> None:
    """Section 2: pinned memories."""
    params: dict[str, Any] = {
        "org": input.org,
        "ns": input.namespace,
        "agent": input.agent,
        "uid": input.user_id or "-",
        "sid": input.session_id or "-",
        "lim": int(input.pinned_limit),
    }
    scope = scope_and(request_scope(), params)
    pinned_rows = await ch.query_all(
        f"""
        SELECT memory_key, memory_type, value, recall_count, last_recalled_at, actor
        FROM loom.memory FINAL
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND agent = {{agent:String}}
          AND user_id = {{uid:String}}
          AND session_id = {{sid:String}}
          AND pinned = 1
          AND tombstoned_at IS NULL
          AND (expires_at IS NULL OR expires_at > now())
          AND status IN ('active', 'conflicted')
          AND sensitivity != 'high'
          {scope}
        ORDER BY version DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    for r in pinned_rows:
        val = _value_str(r.get("value"))
        cost = _est_tokens(val)
        if not fill.fits(cost):
            break
        fill.add(
            _StackEntry(
                section="pinned",
                memory_key=r["memory_key"],
                memory_type=str(r.get("memory_type") or ""),
                value=val,
                tokens=cost,
                pinned=True,
                recall_count=int(r.get("recall_count") or 0),
                last_recalled_at=r.get("last_recalled_at"),
                actor=str(r.get("actor") or ""),
            )
        )


async def _stack_recent(ch: Any, input: MemoryStackInput, fill: _StackFill) -> str:
    """Section 3: recent recall (excludes pinned, already returned). Returns
    the most recent recalled excerpt, the knowledge section's fallback query."""
    pinned_keys = {e.memory_key for e in fill.entries if e.section == "pinned"}
    # Recent recall is query-independent: it replays whatever was recalled
    # lately into every pack. A `medium` row (a contact detail) recalled by an
    # earlier question is collateral here, not context: unfiltered, a plain question's stack
    # carries a neighbour's contact detail, more with every contact question asked before it.
    # Admit `medium` only when this pack's own query asks how to reach
    # someone; `high` never rides the stack.
    medium_ok = recent_recall_admits_medium(str(getattr(input, "seed_query", "") or ""))
    params: dict[str, Any] = {
        "org": input.org,
        "ns": input.namespace,
        "agent": input.agent,
        "uid": input.user_id or "-",
        "sid": input.session_id or "-",
        "lim": int(input.recent_limit),
        "medium_ok": 1 if medium_ok else 0,
    }
    scope = scope_and(request_scope(), params)
    recent_rows = await ch.query_all(
        f"""
        SELECT memory_key, memory_type, value, recall_count,
               evidence_count, last_recalled_at, actor
        FROM loom.memory FINAL
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND agent = {{agent:String}}
          AND user_id = {{uid:String}}
          AND session_id = {{sid:String}}
          AND tombstoned_at IS NULL
          AND (expires_at IS NULL OR expires_at > now())
          AND status IN ('active', 'conflicted')
          AND sensitivity != 'high'
          AND (sensitivity != 'medium' OR {{medium_ok:UInt8}} = 1)
          AND pinned = 0
          AND recall_count > 0
          {scope}
        ORDER BY (recall_count + evidence_count * 0.5) DESC,
                 coalesce(last_recalled_at, created_at) DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    most_recent_excerpt = ""
    for r in recent_rows:
        if r["memory_key"] in pinned_keys:
            continue
        val = _value_str(r.get("value"))
        cost = _est_tokens(val)
        if not fill.fits(cost):
            break
        if not most_recent_excerpt:
            most_recent_excerpt = val[:200]
        fill.add(
            _StackEntry(
                section="recent_recall",
                memory_key=r["memory_key"],
                memory_type=str(r.get("memory_type") or ""),
                value=val,
                tokens=cost,
                recall_count=int(r.get("recall_count") or 0),
                last_recalled_at=r.get("last_recalled_at"),
                actor=str(r.get("actor") or ""),
            )
        )
    return most_recent_excerpt


async def _stack_procedures(input: MemoryStackInput, fill: _StackFill) -> None:
    """Section 4 (optional): matched procedures via procedure.match. Only when
    seed_query is set — the recall-excerpt fallback is too vague for
    procedural matching to be worth the embed + cosine."""
    try:
        from server.memory.api.models_procedure import ProcedureMatchInput
        from server.memory.app.procedure import handle_procedure_match, own_procedures

        pm_out = await handle_procedure_match(
            ProcedureMatchInput(
                org=input.org,
                namespace=input.namespace,
                agent=input.agent,
                user_id=input.user_id or "-",
                session_id=input.session_id or "-",
                situation=input.seed_query,
                top_k=int(input.procedure_limit),
            )
        )
        for m in own_procedures(list(pm_out.matches)):
            steps_text = "\n".join(m.steps) if m.steps else ""
            val = (f"{m.situation}\n{steps_text}").strip()
            if not val:
                continue
            cost = _est_tokens(val)
            if not fill.fits(cost):
                break
            fill.add(
                _StackEntry(
                    section="procedure",
                    memory_key=m.memory_key,
                    memory_type="procedure",
                    value=val,
                    tokens=cost,
                    actor=m.actor,
                )
            )
    except Exception as e:  # noqa: BLE001 — a failed procedure section must not fail the stack
        log.warning("memory.stack: procedure.match fan-out failed (non-fatal): %s", e)


async def _stack_knowledge(input: MemoryStackInput, fill: _StackFill, seed: str) -> None:
    """Section 5 (optional): the top-K knowledge hits keyed on `seed` that serve this
    agent (agent_rules.served_hits drops another bot's promoted rules), noise sections
    left out with `knowledge_noise_filter` on."""
    try:
        from server.embedding import get_embedder as _gemb
        from server.knowledge.app.knowledge_search import not_noise
        from server.knowledge.app.search import HybridSearchQuery as _KSQ
        from server.knowledge.app.search import hybrid_search

        embedder = _gemb()
        q_vec = await embedder.embed(seed)
        agent = request_rule_agent(input.agent)
        top_k = int(input.knowledge_top_k)

        async def search(k: int) -> list[Any]:
            return await hybrid_search(
                _KSQ(
                    org=input.org,
                    namespaces=(input.namespace,),
                    embedding_model=embedder.model_name,
                    query_vec=q_vec,
                    top_k=k,
                )
            )

        # Another bot's promoted rule is not a rule about this agent, and a noise section
        # (meeting notes, a status report) stays out of a bundle handed over unasked.
        khits = await served_hits(
            await search(top_k),
            search,
            lambda h: h.tags,
            agent,
            top_k,
            keep=not_noise(),
        )
        for h in khits:
            val = str(getattr(h, "content", "") or "")
            if not val:
                continue
            cost = _est_tokens(val)
            if not fill.fits(cost):
                break
            fill.add(
                _StackEntry(
                    section="knowledge",
                    memory_key=str(getattr(h, "source_uri", "") or ""),
                    memory_type="knowledge",
                    value=val,
                    tokens=cost,
                )
            )
    except Exception as e:  # noqa: BLE001 — a failed knowledge section must not fail the stack
        log.warning("memory.stack: knowledge fan-out failed (non-fatal): %s", e)


async def handle_memory_stack(input: MemoryStackInput) -> MemoryStackOutput:
    """Compose a token-budgeted recall bundle of profile + pinned +
    recent_recall (+ optional procedure + optional knowledge).

    Per-section ordering:
      - profile: delegates to profile.render (same format as the SPA)
      - pinned:  by version DESC
      - recent_recall: by recall_count + evidence_count, last_recalled_at tiebreak
      - procedure: top-K via procedure.match when seed_query is set
      - knowledge: top-K hybrid search keyed on seed_query (else the most-recent
        recall excerpt), without other bots' promoted rules

    Token estimate is `len(value) // 4` (rough char-to-token approximation).
    """
    ch = get_client()
    fill = _StackFill(budget=input.token_budget)
    await _stack_profile(input, fill)
    await _stack_pinned(ch, input, fill)
    most_recent_excerpt = await _stack_recent(ch, input, fill)
    # Knowledge/procedure query: seed_query wins, else the most-recent
    # recall excerpt.
    seed = (input.seed_query or "").strip() or most_recent_excerpt
    if input.seed_query and input.procedure_limit > 0:
        await _stack_procedures(input, fill)
    if input.include_knowledge and input.knowledge_top_k > 0 and seed:
        await _stack_knowledge(input, fill, seed)
    return MemoryStackOutput(
        entries=fill.entries,
        tokens_used=fill.used,
        tokens_budget=fill.budget,
        truncated=fill.truncated,
        section_counts=fill.section_counts,
    )
