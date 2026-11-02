"""Retrieval legs beyond the primary memory.search: decomposition into
sub-questions, the recency rerank, time-cue ordering, the recommendation
user rows, pending-extraction turns and an ordering question's window turns."""

from __future__ import annotations

import logging
import re
from typing import Any

from server.memory.app.ask.question_shapes import (
    looks_multihop,
)
from server.memory.domain.search_types import hit_row_key
from server.settings.budgets import llm_max_tokens

logger = logging.getLogger(__name__)


def _with(search_input: Any, **changes: Any) -> Any:
    """A copy of the retrieval input with fields changed.

    The handler passes a pydantic `MemorySearchInput`, which
    `dataclasses.replace` rejects (`replace() should be called on dataclass
    instances`), and the widening steps below would catch that fail-open
    and silently skip. Pydantic copies through `model_copy`; dataclasses through
    `replace`; anything else is returned unchanged with the fields set."""
    if hasattr(search_input, "model_copy"):
        return search_input.model_copy(update=changes)
    from dataclasses import is_dataclass, replace

    if is_dataclass(search_input) and not isinstance(search_input, type):
        return replace(search_input, **changes)
    for k, v in changes.items():
        setattr(search_input, k, v)
    return search_input


# Below this spread the calendar cannot separate the rows (an incremental
# ingest lands a whole history within minutes) and arrival order is the signal.
_RECENCY_SPREAD_FLOOR_S = 86400.0


def _recency_rerank(results: list[Any], w: float) -> list[Any]:
    """Re-rank retrieved memories by (similarity + w * recency), so the freshest
    relevant fact reaches the synthesis window even when stale cross-session
    copies rank alongside it on cosine.

    recency is calendar-based — 1/(1 + age_days/14) from valid_at, falling back
    to created_at — when the retrieved set spans more than a day. When it does
    not (every fact of a memorised history arrives within
    minutes, and a correction learned an hour after the original is the same
    case), recency is the ROW'S RANK BY ARRIVAL: newest 1.0, oldest 0.0, so
    the later fact still outranks the earlier one it contradicts. Undated
    rows get recency 0 in both regimes."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)

    def _when(m: Any) -> datetime | None:
        raw = getattr(m, "valid_at", None) or getattr(m, "created_at", None)
        if not raw:
            return None
        try:
            d = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw)[:19])
            return d if d.tzinfo is not None else d.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return None

    stamps = {id(m): _when(m) for m in results}
    dated = [d for d in stamps.values() if d is not None]
    spread = (max(dated) - min(dated)).total_seconds() if len(dated) >= 2 else 0.0

    recency: dict[int, float] = {}
    if dated and spread < _RECENCY_SPREAD_FLOOR_S:
        order = sorted(((k, d) for k, d in stamps.items() if d is not None), key=lambda kd: kd[1])
        denom = max(1, len(order) - 1)
        # Rows stamped the same instant share the mean of their ranks, so a tie
        # never turns on object addresses; oldest 0.0 … newest 1.0.
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and order[j + 1][1] == order[i][1]:
                j += 1
            for k, _d in order[i : j + 1]:
                recency[k] = (i + j) / 2 / denom
            i = j + 1
    else:
        for k, d in stamps.items():
            if d is None:
                continue
            age = max(0.0, (now - d).total_seconds() / 86400.0)
            recency[k] = 1.0 / (1.0 + age / 14.0)

    def _sim(m: Any) -> float:
        s = getattr(m, "similarity", None)
        if s is not None:
            return float(s)
        dist = getattr(m, "distance", None)
        return 1.0 - float(dist) if dist is not None else 0.0

    return sorted(results, key=lambda m: -(_sim(m) + w * recency.get(id(m), 0.0)))


async def _pending_evidence_stage(
    search_input: Any, question: str, ranked: list[Any]
) -> tuple[list[Any], int]:
    """The pending-evidence stage of memory.ask: when the
    scope has extraction batches in flight, the turns they hold join the pool
    as raw rows ahead of the ranked hits, so the answer knows what was just
    said. LLM-free, bounded, fail-open. Returns the widened pool and how many
    rows it added (0 when nothing was pending)."""
    from server.settings import settings as _settings

    pending_evidence_n = 0
    try:
        from server.memory.app.pending_disclosure import pending_extraction as _pend_n
        from server.memory.app.pending_evidence import pending_evidence_for as _pend_hits

        _uid = str(getattr(search_input, "user_id", "") or "")
        _uid_or_none = _uid if _uid and _uid != "-" else None
        if (
            await _pend_n(
                org=str(getattr(search_input, "org", "") or ""),
                namespace=str(getattr(search_input, "namespace", "") or ""),
                agent=str(getattr(search_input, "agent", "") or "") or None,
                user_id=_uid_or_none,
            )
            > 0
        ):
            _pending = await _pend_hits(
                question,
                org=str(getattr(search_input, "org", "") or ""),
                namespace=str(getattr(search_input, "namespace", "") or ""),
                agent=str(getattr(search_input, "agent", "") or "") or None,
                user_id=_uid_or_none,
                max_turns=int(_settings.memory_ask_pending_evidence_turns),
                max_batches=int(_settings.memory_ask_pending_evidence_batches),
            )
            if _pending:
                ranked = list(_pending) + list(ranked)
                pending_evidence_n = len(_pending)
    except Exception as e:  # noqa: BLE001 — pending evidence never fails an answer
        logger.debug("memory.ask pending evidence skipped: %s", e)
    return ranked, pending_evidence_n


_DECOMPOSE_SYSTEM = (
    "You split a question about a person's stored memories into the smallest set of "
    "self-contained sub-questions whose answers, taken together, answer it. Each "
    "sub-question must be answerable on its own from a memory store (no pronouns "
    "referring to another sub-question; repeat the names). Two to four lines, one "
    "sub-question per line, no numbering, no commentary. If the question is a single "
    "lookup that needs no intermediate fact, reply with exactly: NONE"
)


def parse_decomposition(text: str, question: str, *, max_parts: int = 4) -> list[str]:
    """Lines of the model's reply → sub-questions: trimmed, de-numbered,
    deduplicated, never the question itself, at most `max_parts`. 'NONE' or a
    single line → []."""
    out: list[str] = []
    seen: set[str] = set()
    norm_q = re.sub(r"\W+", " ", (question or "").lower()).strip()
    for raw in (text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", raw).strip()
        if not line or line.upper() == "NONE":
            continue
        norm = re.sub(r"\W+", " ", line.lower()).strip()
        if not norm or norm == norm_q or norm in seen:
            continue
        seen.add(norm)
        out.append(line if line.endswith("?") else line + "?")
        if len(out) >= max_parts:
            break
    return out if len(out) >= 2 else []


async def decompose_question(llm: Any, question: str, *, max_parts: int = 4) -> list[str]:
    """One cheap-tier call; fail-open to [] on any error or a NONE reply."""
    try:
        from server.observability.genai import llm_stage

        with llm_stage("ask_decompose"):
            res = await llm.complete(
                _DECOMPOSE_SYSTEM,
                f"Question: {question}",
                max_tokens=llm_max_tokens("ask_decompose"),
            )
        text = str(getattr(res, "text", "") or "")
        return parse_decomposition(text, question, max_parts=max_parts)
    except Exception as e:  # noqa: BLE001 — additive
        logger.warning("memory.ask decompose failed (fail-open): %s", e)
        return []


async def decomposed_retrieval(
    search_input: Any,
    question: str,
    ranked: list[Any],
    *,
    llm: Any,
    mode: str,
    max_parts: int = 4,
    part_top_k: int = 8,
    stats: dict[str, int] | None = None,
) -> tuple[list[str], list[Any]]:
    """Retrieve each sub-question on its own and union the
    hits into `ranked` (whole-question hits first, then per part in order,
    deduplicated by memory_key). Returns (sub_questions, merged). A part
    whose retrieval raises is skipped (fail-open) and counted in
    `stats["failed"]` — the count rides the response and the
    ledger, so a lever that stops working is a number, not a log line."""
    from server.memory.app.search import handle_memory_search

    if mode == "auto" and not looks_multihop(question):
        return [], ranked
    parts = await decompose_question(llm, question, max_parts=max_parts)
    if not parts:
        return [], ranked
    merged = list(ranked)
    ns = str(search_input.namespace)
    have = {hit_row_key(h, ns) for h in merged}
    for part in parts:
        try:
            sub = await handle_memory_search(
                _with(search_input, query=part, top_k=max(1, int(part_top_k)))
            )
        except Exception as e:  # noqa: BLE001 — additive
            logger.warning("memory.ask decompose: part retrieval failed (fail-open): %s", e)
            if stats is not None:
                stats["failed"] = int(stats.get("failed", 0)) + 1
            continue
        for h in sub.results or []:
            row = hit_row_key(h, ns)
            if row.memory_key and row not in have:
                have.add(row)
                merged.append(h)
    return parts, merged


_TIME_CUE_MAX_DAYS = 45
_TIME_CUE_GRACE_DAYS = 2


def _time_cue_first(
    ranked: list[Any], window: int, question: str, observation_date: Any
) -> list[Any]:
    """A question that names a time ("a week ago",
    "the past weekend", "two months ago") is about the rows dated there. The
    cosine ranks by words, so the row from another time that shares them
    outranks the asked one — in a temporal chat-history store the cousin's
    wedding (dated in the cue's week) sat at rank 11 behind a niece's
    graduation twelve days off, the road-bike check (the weekend) at 20
    behind a Wednesday flat fix, the History Museum lecture (two months ago)
    at 26 behind a visit five months back. When `resolve_time_window` bounds
    the question to at most 45 days, the pool's rows dated inside it (two
    days' grace) move to the front in their own order; nothing is dropped.
    An unbounded / wide window leaves the order as ranked."""
    if not ranked:
        return ranked
    from datetime import date as _date

    from server.memory.domain.question_window import resolve_time_window, window_gap_days

    try:
        obs = str(observation_date)[:10] if observation_date else None
        tw = resolve_time_window(question, obs)
    except Exception:  # noqa: BLE001 — a parser miss leaves the order alone
        return ranked
    if tw is None or not tw.bounded or tw.days > _TIME_CUE_MAX_DAYS or tw.kind == "habitual":
        return ranked
    near: list[Any] = []
    rest: list[Any] = []
    for m in ranked:
        raw = str(getattr(m, "valid_at", None) or "")[:10]
        key = str(getattr(m, "memory_key", "") or "")
        try:
            d = _date.fromisoformat(raw) if raw else None
        except ValueError:
            d = None
        is_row = not key.startswith(("chunk:", "summary:", "turn:"))
        if d is not None and is_row and window_gap_days(d, tw) <= _TIME_CUE_GRACE_DAYS:
            near.append(m)
        else:
            rest.append(m)
    if not near or near == ranked[: len(near)]:
        return ranked
    logger.info(
        "memory.ask: time cue %r → %s..%s, %d row(s) first", tw.phrase, tw.start, tw.end, len(near)
    )
    return near + rest


async def _order_window_turns(
    search_input: Any, question: str, settings_obj: Any
) -> tuple[str, str]:
    """An ORDERING question with a time cue of up to
    three months ("the concerts I attended in the past two months", "the
    sports events I watched in January") reads the user's own turns from that
    window with the first synthesis — the member the extractor left as an
    aside (the NBA game, the Brooklyn festival) is otherwise absent from a
    list the reader gives confidently, so no second reading is triggered.
    Ordering questions only: the count version measured negative on
    multi-session. Returns (rendered turns, note) or ("", "")."""
    from server.memory.app import ask_when_evidence as _awe
    from server.memory.domain.synth_policy import _ORDER_SET_RE

    if not _ORDER_SET_RE.search(question or ""):
        return "", ""
    bounds = _awe.cue_window_bounds(
        question,
        getattr(search_input, "observation_date", None),
        max_days=int(settings_obj.memory_ask_order_window_max_days or 95),
    )
    if not bounds:
        return "", ""
    try:
        turns = await _awe.window_turns(
            org=getattr(search_input, "org", "") or "",
            namespace=getattr(search_input, "namespace", "") or "",
            agent=getattr(search_input, "agent", "") or "*",
            user_id=getattr(search_input, "user_id", None),
            question=question,
            bounds=bounds,
            top_k=int(settings_obj.memory_ask_cue_window_turns_top_k or 24),
        )
    except Exception:  # noqa: BLE001 — additive, never blocks the answer
        return "", ""
    if not turns:
        return "", ""
    logger.info(
        "memory.ask: ordering question read %d window turn(s) in %s..%s", len(turns), *bounds
    )
    return _awe.render_turns(turns), _awe.ORDER_TURNS_NOTE
