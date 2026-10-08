"""What every synthesis call of one ask reads besides the window: the answer
length and the premise check."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from server.memory.app.ask.identity import memory_identity_where
from server.memory.app.ask.pool import _AskRun
from server.memory.app.ask.question_shapes import is_count_intent
from server.memory.app.ask.result import _by_keys, _keys_of

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _AskReader:
    """What every synthesis call of one ask reads besides the window: the
    notes, blocks and switches its prompt is built from, fixed once the
    window has settled. `first_kwargs` is the full set the first synthesis
    (and its not-recorded / deep-first re-runs) take; the strong escalation
    and the deep pass pass their own subsets."""

    question: str
    answer_chars: int
    hedge_ident: str
    as_of: Any
    stage: str
    recent_block: str
    presupposition: bool
    record_note: bool
    inference_note: bool
    temporal_derive: bool
    chain_note: bool
    knowledge_block: str
    intent: Any
    omitted: list[Any]
    timeline: str
    premise_system: str
    world_knowledge: bool
    scratchpad: str
    # The ordering question's window turns; set by _prepare_synthesis.
    order_turns_text: str = ""
    order_turns_note: str = ""

    def first_kwargs(self) -> dict[str, Any]:
        return dict(
            raw_evidence=self.order_turns_text,
            hedge_ident=self.hedge_ident,
            as_of=self.as_of,
            stage=self.stage,
            recent_block=self.recent_block,
            presupposition=self.presupposition,
            record_note=self.record_note,
            inference_note=self.inference_note,
            temporal_derive=self.temporal_derive,
            chain_note=self.chain_note,
            knowledge_block=self.knowledge_block,
            intent=self.intent,
            omitted=self.omitted,
            timeline=self.timeline,
            premise_system=f"{self.premise_system}{self.order_turns_note}",
            world_knowledge=self.world_knowledge,
            scratchpad=self.scratchpad,
        )


def _answer_chars(
    question: str,
    intent: Any,
    *,
    max_answer_chars: int,
    default_answer_chars: int | None,
    settings: Any,
) -> int:
    """The answer-length cap the synthesizer writes to."""
    _settings = settings
    # Default answer length. The caller's max_answer_chars wins;
    # when it is 0 (unbounded) the deployment default
    # applies, except to breadth questions ("which / all / list", whose answer
    # is the set) and structured count/preference/affect answers (their ITEMS
    # lines are the working, the cap governs only the ANSWER line and the fold
    # already protects it). Latency is the target: the synthesis call is 88%
    # of memory.ask's 11 s median and the median winning answer is about
    # 1,000 characters.
    answer_chars = int(max_answer_chars or 0)
    if answer_chars <= 0:
        # The request may pin the deployment default for this call (0 = off).
        _default_chars = (
            int(default_answer_chars)
            if default_answer_chars is not None
            else int(_settings.memory_ask_default_answer_chars)
        )
        if _default_chars > 0:
            from server.memory.domain.synth_policy import is_breadth_query as _is_breadth

            if not _is_breadth(question) and not is_count_intent(intent):
                answer_chars = _default_chars
    return answer_chars


async def _check_premise(run: _AskRun, *, llm: Any, llm_cheap: Any) -> tuple[Any, str]:
    """The premise check against the pinned or human-verified
    rows in the window. Returns (the premise verdict, its system note)."""
    search_input, _settings = run.search_input, run.settings
    premise = _ac_premise_default()
    premise_rows: list[Any] = []
    if bool(_settings.memory_ask_premise_check):
        premise_rows = await _trusted_in_pool(search_input, run.memories)
        if premise_rows:
            from server.memory.app import answer_consistency as _acp

            premise = await _acp.premise_check(
                llm_cheap if llm_cheap is not None else llm,
                run.query_text,
                _by_keys(run.memories, [t.memory_key for t in premise_rows]),
                premise_rows,
            )
    premise_system = ""
    if premise.conflict:
        from server.memory.app.answer_consistency import premise_note as _pn

        premise_system = _pn(premise, premise_rows)
    return premise, premise_system


def _ac_premise_default() -> Any:
    from server.memory.app.answer_consistency import PremiseVerdict

    return PremiseVerdict()


async def _trusted_in_pool(search_input: Any, memories: list[Any]) -> list[Any]:
    """Pinned or human-verified rows among the pool's keys.
    One FINAL read scoped to the pool; fails open to none."""
    from server.memory.app.answer_consistency import trusted_rows
    from server.memory.store.sql import attested_sql

    keys = _keys_of(memories)
    identity = memory_identity_where(search_input)
    if not keys or identity is None:
        return []
    where, params = identity
    try:
        from server.core.ch import get_client

        ch = get_client()
        # Verified = an attestation that binds the row's current content; an
        # actor over edited (or unhashed) content is not trusted.
        rows = await ch.query_all(
            f"""
            SELECT memory_key, pinned, verified_by, {attested_sql()} AS attested
            FROM loom.memory FINAL
            WHERE {" AND ".join(where)}
              AND memory_key IN {{keys:Array(String)}}
              AND status = 'active'
              AND (pinned = 1 OR {attested_sql()})
            """,
            params={**params, "keys": keys},
        )
    except Exception as e:  # noqa: BLE001 — a trust read never blocks an answer
        logger.warning("memory.ask trusted-row read failed (open): %s", e)
        return []
    return trusted_rows([dict(r) for r in rows], keys)
