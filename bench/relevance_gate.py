"""LLM relevance judge the chat-history QA bench runs on the reader side
(`--relevance-gate`).

ONE call labels the synthesis window's rows against the question and drops the
ones that do not bear on it. memory.ask does not run it; the server keeps only
the LLM-free cut, `server.memory.domain.relevance_gate.similarity_floor_cut`.

Contract:
  * fail OPEN: any error, unparseable reply, or empty verdict keeps the full
    window; the gate can only remove rows, never invent them;
  * a floor: never hand the reader fewer than `min_keep` rows (the
    highest-ranked dropped rows are restored), so a judge that says "nothing"
    still leaves the reader the evidence to abstain FROM;
  * conflicts survive: an older or superseded statement about the asked
    entity is RELEVANT (the answer must be able to say what changed), so the
    prompt tells the judge to keep it. Only rows about a different subject go.
"""

from __future__ import annotations

import json
import re
from typing import Any

from server.memory.domain.relevance_gate import GateVerdict

# Excerpt chars per row shown to the judge. Shorter than the synthesizer's
# limit: the judge decides "about this or not", it does not need the tail.
_JUDGE_EXCERPT_CHARS = 600

GATE_SYSTEM = (
    "You are a relevance filter between a memory store and an answering "
    "model. You are given a QUESTION and a numbered list of retrieved "
    "MEMORIES. Decide which memories bear on the question.\n"
    "A memory is RELEVANT when it states, dates, quantifies, or qualifies "
    "something the question asks about, or names the entity/event asked "
    "about — INCLUDING an older, superseded, or conflicting statement about "
    "that same entity (the answer must be able to say what changed). Keep a "
    "memory that gives the date or order of the asked event.\n"
    "A memory is IRRELEVANT when it is about a different person, event, "
    "object, or period, even if it shares words or a topic with the "
    "question.\n"
    "When unsure, KEEP.\n"
    'Reply with JSON only: {"relevant": [<memory numbers to keep>]}.'
)

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)
_INT_RE = re.compile(r"\d+")


def build_gate_user(question: str, excerpts: list[str]) -> str:
    """The user turn: question + 1-based numbered memories."""
    rows = "\n\n".join(
        f"{i + 1}. {(e or '').strip()[:_JUDGE_EXCERPT_CHARS]}" for i, e in enumerate(excerpts)
    )
    return f"QUESTION: {question.strip()}\n\nMEMORIES:\n{rows}"


def parse_relevant(text: str, n: int) -> set[int] | None:
    """1-based indices the judge kept, as 0-based positions within [0, n).
    Tolerant: a JSON object anywhere in the reply, a bare list, or a run of
    numbers all parse. None = unparseable (the caller fails open). An empty
    set is a real verdict ("none relevant") and is returned as such."""
    raw = (text or "").strip()
    if not raw:
        return None
    vals: list[Any] | None = None
    m = _JSON_OBJ_RE.search(raw)
    if m:
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            v = obj.get("relevant")
            if isinstance(v, list):
                vals = v
            else:
                # A well-formed object that does not answer the question is
                # not a verdict; do not mine its other numbers.
                return None
    if vals is None:
        try:
            obj = json.loads(raw)
            if isinstance(obj, list):
                vals = obj
        except ValueError:
            vals = None
    if vals is None:
        # Last resort: an explicit negative ("none of them") is a real empty
        # verdict; otherwise a run of numbers ("1, 3, 7") is the kept set. A
        # reply with neither is unparseable and the caller fails open.
        if re.search(r"\b(none|nothing|no memories)\b", raw, re.IGNORECASE):
            return set()
        if not _INT_RE.search(raw):
            return None
        vals = [int(x) for x in _INT_RE.findall(raw)]
    out: set[int] = set()
    for v in vals:
        try:
            i = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= i <= n:
            out.add(i - 1)
    return out


def apply_verdict(memories: list[Any], relevant: set[int] | None, *, min_keep: int) -> GateVerdict:
    """Pure half of the gate: fold a parsed verdict onto the ranked window,
    honouring the floor. Rank order is preserved on both sides so the
    synthesizer still sees the retriever's ordering."""
    n = len(memories)
    if relevant is None:
        return GateVerdict(kept=list(memories), mode="error", judged=n)
    keep_idx = {i for i in relevant if 0 <= i < n}
    if len(keep_idx) >= n:
        return GateVerdict(kept=list(memories), mode="kept_all", judged=n)
    mode = "filtered"
    if len(keep_idx) < min_keep:
        # Restore the highest-ranked dropped rows until the floor is met.
        for i in range(n):
            if len(keep_idx) >= min_keep:
                break
            keep_idx.add(i)
        mode = "floor"
        if len(keep_idx) >= n:
            return GateVerdict(kept=list(memories), mode="kept_all", judged=n)
    kept = [m for i, m in enumerate(memories) if i in keep_idx]
    dropped = [m for i, m in enumerate(memories) if i not in keep_idx]
    return GateVerdict(kept=kept, dropped=dropped, mode=mode, judged=n)
