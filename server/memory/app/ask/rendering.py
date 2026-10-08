"""The window as the synthesizer reads it: evidence order, the per-row line
and the assembly of one call's system and user prompts."""

from __future__ import annotations

import logging
from typing import Any

from server.memory.app.ask.question_shapes import (
    _LATEST_CUE_RE,
    _TEMPORAL_CUE_RE,
    _TEMPORAL_OPS,
    _is_first_person_question,
)
from server.memory.app.ask.result import _keys_of

logger = logging.getLogger(__name__)


_EXCERPT_CHAR_LIMIT = 2000


def _recorded_date(m: Any) -> str:
    """The date a memory reflects, for the recency-aware synthesis prompt:
    valid_at (when the fact became true) falling back to created_at. Empty
    string when neither is set (the synth prompt tolerates a bare date)."""
    for attr in ("valid_at", "created_at"):
        v = getattr(m, attr, None)
        if v:
            txt = str(v).replace("T", " ")
            # A per-turn time is part of the evidence for
            # "around 08:10" questions; midnight means a date-only anchor.
            if len(txt) >= 16 and txt[11:16] not in ("00:00", ""):
                return txt[:16]
            return txt[:10]
    return "undated"


def evidence_order(memories: list[Any], intent: Any, question: str = "") -> tuple[list[Any], str]:
    """Order the grounding window the way the question reads it.

    The retrieved rows are linearised by intent — timestamp order for temporal
    questions — because rank order (similarity + recency) interleaves 2024 and
    2026 rows for a "when / how long / what changed" question. Returns (memories, mode)
    with mode in {"rank", "chronological", "reverse_chronological"}; rank
    order is kept for everything without a temporal cue."""
    if not memories:
        return list(memories), "rank"
    chrono = str(getattr(intent, "chronological_intent", "") or "") if intent else ""
    op = str(getattr(intent, "op", "") or "") if intent else ""
    temporal_cue = bool(intent and getattr(intent, "resolved_dates", ())) or op in _TEMPORAL_OPS
    if chrono == "desc" or op == "latest":
        mode = "reverse_chronological"
    elif chrono == "asc" or temporal_cue:
        mode = "chronological"
    elif question and _LATEST_CUE_RE.search(question) and not _TEMPORAL_CUE_RE.search(question):
        mode = "reverse_chronological"
    elif question and _TEMPORAL_CUE_RE.search(question):
        mode = "chronological"
    else:
        return list(memories), "rank"

    def _key(m: Any) -> str:
        d = _recorded_date(m)
        return d if d != "undated" else ("9999" if mode == "chronological" else "0000")

    ordered = sorted(memories, key=_key, reverse=(mode == "reverse_chronological"))
    return ordered, mode


def elision_note(omitted: list[Any], *, max_keys: int = 8) -> str:
    """One line standing in for retrieved memories that did not fit the
    window: how many, their date span, and their keys, so the model knows
    the record continues and an agent can fetch them by key. Collapsing
    rather than dropping keeps a timeline continuous."""
    if not omitted:
        return ""
    dates = sorted(d for d in (_recorded_date(m) for m in omitted) if d != "undated")
    span = f", dated {dates[0]} to {dates[-1]}" if dates else ""
    keys = _keys_of(omitted)
    shown = ", ".join(keys[:max_keys]) + (", …" if len(keys) > max_keys else "")
    return (
        f"[… {len(omitted)} more retrieved memories not shown{span}; "
        f"keys: {shown}. Fetch by key if the answer needs them.]"
    )


_STORED_MEMORIES_HEAD = "Stored memories:\n\n"
_SOURCE_TURNS_HEAD = (
    "Source turns behind the memories above (verbatim, dated, labelled with the "
    "memory they support):\n\n"
)
_SCRATCHPAD_HEAD = "Run notes (the agent's own scratchpad for this task, not a memory):\n"
_TIMELINE_HEAD = (
    "Timeline of the memories below (deltas already computed against the "
    "question date; use them, do not recompute):\n"
)


def _memory_text(m: Any) -> str:
    return (getattr(m, "content_excerpt", "") or "")[:_EXCERPT_CHAR_LIMIT]


def _memory_line(m: Any, *, nonce: str) -> str:
    """One window line: date, key, type, the fenced body."""
    from server.memory.domain.prompt_fence import fence

    body = fence(m.memory_key, _memory_text(m), nonce)
    # A fact about someone else is labelled so the reader
    # does not hand the user another person's preference.
    whose = " (about someone other than the user)" if getattr(m, "about_other", False) else ""
    # A row the ASSISTANT authored — a recommendation,
    # an itinerary it drafted, a price it quoted — is not something the user
    # did or said: unlabelled, an `assistant_dubai_trip_schedule` row is listed
    # as one of the user's trips, and the assistant's taxi quote overrides the
    # user's own "$60" estimate. The label rides the type parenthesis.
    if not whose and _is_assistant_row(m):
        whose = (
            " (the assistant's own statement — a suggestion, plan or quote, "
            "not something the user did or said)"
        )
    # A retired row reaches the window only when the caller
    # asked for it; it is labelled so the reader states it as history.
    from server.memory.app.served_superseded import superseded_label

    old = f" {superseded_label(m)}" if superseded_label(m) else ""
    return (
        f"[{_recorded_date(m)}] [{m.memory_key}] "
        f"({getattr(m, 'memory_type', '') or 'memory'}){whose}"
        f"{old} {body}"
    )


def _is_assistant_row(m: Any) -> bool:
    """Whether a memory row records the assistant's statement rather than the
    user's: the extractor keys assistant-stated facts `assistant_*` and tags
    the speaker; either signal counts. Chunks and summaries are not rows."""
    key = str(getattr(m, "memory_key", "") or "")
    if key.startswith(("chunk:", "summary:", "turn:")):
        return False
    if key.startswith("assistant_"):
        return True
    return str(getattr(m, "speaker", "") or "").strip().lower() == "assistant"


def _render_window(
    memories: list[Any], intent: Any, question: str
) -> tuple[list[Any], str, str, str]:
    """The window as the synthesizer reads it: `(ordered memories, excerpts,
    order_mode, nonce)`."""
    memories, order_mode = evidence_order(memories, intent, question)
    # On a first-person question the assistant's own
    # rows (its plans, quotes, suggestions) read after the user's — the label
    # alone left the reader listing an assistant-drafted itinerary as one of
    # the user's trips and pricing the user's journey off the assistant's
    # quote. Stable: order within each group
    # is unchanged.
    if _is_first_person_question(question):
        _users = [m for m in memories if not _is_assistant_row(m)]
        _asst = [m for m in memories if _is_assistant_row(m)]
        if _asst and _users:
            memories = _users + _asst
    from server.memory.domain.prompt_fence import new_nonce

    # The nonce is keyed on the prompt's own bodies, not drawn
    # per call, so an identical window renders an identical prompt (the
    # response cache keys on it) and a body still cannot forge its marker.
    nonce = new_nonce(question, *(f"{m.memory_key}\x00{_memory_text(m)}" for m in memories))
    excerpts = "\n\n".join(_memory_line(m, nonce=nonce) for m in memories)
    return memories, excerpts, order_mode, nonce


def _context_block_chars(
    *,
    recent_block: str = "",
    scratchpad: str = "",
    timeline: str = "",
    knowledge_block: str = "",
    graph_note: str = "",
    elision: str = "",
) -> dict[str, int]:
    """Characters each per-ask context block occupies in the prompt, heads
    included where the prompt renders one. Pure."""
    return {
        "recent": len(recent_block or ""),
        "scratchpad": len(_SCRATCHPAD_HEAD) + len(scratchpad) if scratchpad else 0,
        "timeline": len(_TIMELINE_HEAD) + len(timeline) if timeline else 0,
        "knowledge": len(knowledge_block or ""),
        "graph": len(graph_note or ""),
        "elision": len(elision or ""),
    }


def _assemble_prompt(
    *,
    system: str,
    excerpts: str,
    question: str,
    attribution_block: str,
    notes: list[str],
    raw_evidence: str,
    memories: list[Any],
    nonce: str,
    parts: dict[str, int],
) -> tuple[str, str]:
    """`(system, user)` for one synthesis call: the notes are appended to the
    system prompt and the question comes before the memories.

    Block accounting rides the assembly: the characters every block claims are
    folded into the ask's per-block token estimate (`memory.domain.prompt_blocks`).
    The window rows are counted per row by kind (window / chunks / code /
    chase) from the same line renderer, the context blocks from `parts`, the
    heads and separators are `other`. Accounting never fails the call."""
    static_system = system
    joined = "".join(notes)
    notes_chars = len(joined)
    system = f"{system}{joined}"
    user = f"Question: {question}\n{attribution_block}\n{_STORED_MEMORIES_HEAD}{excerpts}"
    try:
        from server.memory.domain import prompt_blocks as _pb

        tally = dict(parts)
        tally["system"] = len(static_system)
        tally["notes"] = notes_chars
        tally["question"] = len("Question: ") + len(question) + len(attribution_block)
        tally["raw_turns"] = len(_SOURCE_TURNS_HEAD) + len(raw_evidence) if raw_evidence else 0
        for kind, chars in _pb.row_chars(
            (m, _memory_line(m, nonce=nonce)) for m in memories
        ).items():
            tally[kind] = tally.get(kind, 0) + chars
        _pb.add(_pb.block_tokens(total_chars=len(system) + len(user), parts=tally))
    except Exception as e:  # noqa: BLE001 — accounting never fails the answer
        logger.debug("memory.ask prompt block accounting skipped: %s", e)
    return system, user
