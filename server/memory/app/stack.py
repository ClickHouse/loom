"""memory.stack: the layered, token-budgeted recall bundle (profile, pinned,
recent recall, procedures, knowledge) one call assembles."""

from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from typing import Any

from server.core.ch import get_client
from server.core.diagnostics import private_work
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import scope_and
from server.knowledge.app.source_access import ReadPlan
from server.knowledge.app.source_search_proofs import SearchProofs
from server.memory.api.models_lifecycle import MemoryStackInput, MemoryStackOutput, _StackEntry
from server.memory.app.stack_knowledge import StackKnowledge

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
                    # a procedure can be matched in another of the caller's namespaces
                    namespace=m.namespace if m.namespace != input.namespace else "",
                )
            )
    except Exception as e:  # noqa: BLE001 — a failed procedure section must not fail the stack
        log.warning("memory.stack: procedure.match fan-out failed (non-fatal): %s", e)


def _render(fill: _StackFill, hits: list[Any]) -> MemoryStackOutput:
    fill = replace(fill, entries=list(fill.entries), section_counts=dict(fill.section_counts))
    for hit in hits:
        value = str(getattr(hit, "content", "") or "")
        if not value:
            continue
        cost = _est_tokens(value)
        if not fill.fits(cost):
            break
        fill.add(
            _StackEntry(
                section="knowledge",
                memory_key=str(getattr(hit, "source_uri", "") or ""),
                memory_type="knowledge",
                value=value,
                tokens=cost,
            )
        )
    return MemoryStackOutput(
        entries=fill.entries,
        tokens_used=fill.used,
        tokens_budget=fill.budget,
        truncated=fill.truncated,
        section_counts=fill.section_counts,
    )


@dataclass
class CheckedStack:
    _prefix: _StackFill
    _knowledge: StackKnowledge | None
    marked: bool
    _knowledge_used: bool = field(default=False, init=False)

    @property
    def lost(self) -> bool:
        return self._knowledge is not None and self._knowledge.lost

    @property
    def output(self) -> MemoryStackOutput:
        output = _render(self._prefix, self._knowledge.hits if self._knowledge else [])
        self._knowledge_used = output != _render(self._prefix, [])
        return output

    def publication_proofs(self) -> tuple[SearchProofs, ...]:
        if not self._knowledge_used or self._knowledge is None or self._knowledge.lost:
            return ()
        return (self._knowledge.proofs,) if self._knowledge.proofs is not None else ()

    def fresh(self) -> bool:
        return self._knowledge is None or self._knowledge.fresh()

    async def ordinary(self) -> MemoryStackOutput:
        if self._knowledge is not None:
            self._knowledge.ordinary()
        return self.output

    async def current(self) -> MemoryStackOutput:
        with private_work() if self.marked else nullcontext():
            if self._knowledge is not None:
                await self._knowledge.current()
            return self.output


async def run_checked_stack(input: MemoryStackInput, plan: ReadPlan) -> CheckedStack:
    if input.org != plan.org:
        raise ValueError("document read scope changed")
    input = input.model_copy(deep=True)
    with private_work() if plan.marked else nullcontext():
        ch = get_client()
        fill = _StackFill(budget=input.token_budget)
        await _stack_profile(input, fill)
        await _stack_pinned(ch, input, fill)
        most_recent_excerpt = await _stack_recent(ch, input, fill)
        seed = (input.seed_query or "").strip() or most_recent_excerpt
        if input.seed_query and input.procedure_limit > 0:
            await _stack_procedures(input, fill)
        knowledge = None
        if input.include_knowledge and input.knowledge_top_k > 0 and seed:
            from server.memory.app import stack_knowledge

            try:
                knowledge = await stack_knowledge.collect(
                    input.org, input.namespace, input.agent, seed, int(input.knowledge_top_k), plan
                )
            except Exception as e:  # noqa: BLE001 — a failed knowledge section must not fail the stack
                log.warning("memory.stack: knowledge fan-out failed (non-fatal): %s", e)
        return CheckedStack(fill, knowledge, plan.marked)


async def handle_memory_stack(input: MemoryStackInput) -> MemoryStackOutput:
    """Compose a token-budgeted profile, pinned, recent, procedure and knowledge bundle."""
    from server.knowledge.app import source_access

    input = input.model_copy(deep=True)
    plan = (
        await source_access.prepare(input.org, [input.namespace])
        if input.include_knowledge and input.knowledge_top_k > 0
        else ReadPlan(input.org)
    )
    from server.knowledge.app import source_publication

    checked = await run_checked_stack(input, plan)
    output = await checked.current()
    for proof in checked.publication_proofs():
        source_publication.retain(proof)
    return output
