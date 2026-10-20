"""A last look at a coding capture before it is stored.

The rules in `durable_facts` catch the shapes a session's junk takes by
construction: a status line, a tool's answer restated, a housekeeping step.
What is left needs judgment — a fact that restates the session's own
activity in words no rule knows, a paraphrase of another fact in the same
capture, an earlier state a later fact replaces, a garbled attribute. One
batched call per capture, per verifier model, answers three things for the
whole list: keep or drop each fact, which facts state the same claim (and
which member is the most complete), and which earlier fact a later one
replaces.

A fact is dropped as non-durable only when EVERY verifier model says drop:
models disagree on the boundary (a second judge disagreed with the labelled
eval on 73 of its 77 stored-junk rows), and a lost real fact costs more than
a stored junk one. Same-claim groups and replacements come from the primary
(first) model: its folds were checked equal on the eval. Every failure —
an error, a timeout, an unparseable answer — keeps every fact.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from server.memory.domain.extraction_prompts import (
    DURABILITY_VERIFIER_PROMPT_VERSION as VERIFIER_PROMPT_VERSION,
)
from server.memory.domain.extraction_prompts import (
    DURABILITY_VERIFIER_SYSTEM_PROMPT as VERIFIER_SYSTEM_PROMPT,
)
from server.settings import settings

log = logging.getLogger(__name__)
__all__ = [
    "VERIFIER_PROMPT_VERSION",
    "VERIFIER_SYSTEM_PROMPT",
    "decide",
    "parse_verdicts",
    "verify",
]


@dataclass
class Verdicts:
    """One model's answer over one slice, by fact position in the slice."""

    drop: dict[int, str] = field(default_factory=dict)
    folds: dict[int, int] = field(default_factory=dict)  # member -> kept rep
    ok: bool = False
    tokens_in: int = 0
    tokens_out: int = 0


def _fact_value(fact: dict[str, Any]) -> str:
    v = fact.get("value")
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)


# What the verifier is told about the capture. Values alone make a looked-up fact ("The
# fix is #4512, merged into 2.3.1") indistinguishable from a durable one; the
# goal and what the session's lookups returned are what mark it as a restatement.
CONTEXT_GOAL_CHARS = 300
CONTEXT_LOOKUP_CHARS = 240
CONTEXT_LOOKUPS_MAX = 6


@dataclass
class CaptureContext:
    """What the session was for, and what its lookups returned."""

    goal: str = ""
    lookups: list[str] = field(default_factory=list)


def capture_context(
    messages: list[dict[str, Any]] | None, session_values: list[str] | None
) -> CaptureContext:
    """The first user message as the goal, and up to CONTEXT_LOOKUPS_MAX
    tool results, each clipped — the verifier's view of the session."""
    goal = ""
    for m in messages or []:
        if str(m.get("role") or "") == "user":
            goal = " ".join(str(m.get("content") or "").split())[:CONTEXT_GOAL_CHARS]
            break
    lookups = [
        " ".join(str(v).split())[:CONTEXT_LOOKUP_CHARS]
        for v in (session_values or [])
        if str(v).strip()
    ][:CONTEXT_LOOKUPS_MAX]
    return CaptureContext(goal=goal, lookups=lookups)


def _role(fact: dict[str, Any]) -> str:
    role = str(fact.get("source_role") or "")
    if not role:
        for t in fact.get("tags") or []:
            if str(t).startswith("source_role:"):
                role = str(t).split(":", 1)[1]
                break
    return f"[{role}] " if role else ""


def _key(fact: dict[str, Any]) -> str:
    key = str(fact.get("key") or fact.get("memory_key") or "")
    return f"({key}) " if key else ""


def _user_prompt(facts: list[dict[str, Any]], context: CaptureContext | None = None) -> str:
    lines = "\n".join(
        f"{i}. {_role(f)}{_key(f)}{' '.join(_fact_value(f).split())}" for i, f in enumerate(facts)
    )
    head = ""
    if context is not None and (context.goal or context.lookups):
        parts = []
        if context.goal:
            parts.append(f"Session goal (the user's first message): {context.goal}")
        if context.lookups:
            shown = "\n".join(f"- {v}" for v in context.lookups)
            parts.append(f"What the session's tools and lookups returned (clipped):\n{shown}")
        head = "\n\n".join(parts) + "\n\n"
    return f"{head}Facts extracted from one coding session:\n{lines}\n\nReturn the JSON object now."


def _int(x: Any, n: int) -> int | None:
    try:
        i = int(x)
    except (TypeError, ValueError):
        return None
    return i if 0 <= i < n else None


def parse_verdicts(text: str, n: int) -> Verdicts:
    """Read a verifier answer for `n` facts. Anything unreadable is an empty,
    not-ok answer — the caller keeps every fact."""
    out = Verdicts()
    a, b = (text or "").find("{"), (text or "").rfind("}")
    if a < 0 or b <= a:
        return out
    try:
        payload = json.loads(text[a : b + 1])
    except (json.JSONDecodeError, ValueError):
        return out
    if not isinstance(payload, dict) or not isinstance(payload.get("verdicts"), list):
        return out
    for v in payload["verdicts"]:
        if not isinstance(v, dict) or v.get("keep") is not False:
            continue
        i = _int(v.get("i"), n)
        if i is not None:
            out.drop[i] = str(v.get("why") or "")[:120]
    for g in payload.get("same_claim") or []:
        if not isinstance(g, dict):
            continue
        members = {m for m in (_int(x, n) for x in g.get("members") or []) if m is not None}
        best = _int(g.get("best"), n)
        if best is None or best not in members or len(members) < 2:
            continue
        for m in members - {best}:
            out.folds.setdefault(m, best)
    for p in payload.get("superseded") or []:
        if not isinstance(p, dict):
            continue
        old, new = _int(p.get("old"), n), _int(p.get("new"), n)
        if old is not None and new is not None and old != new:
            out.folds.setdefault(old, new)
    out.ok = True
    return out


def _resolve_rep(i: int, folds: dict[int, int]) -> int | None:
    """Follow member -> rep links to the fact that stays; None on a cycle."""
    seen = {i}
    while i in folds:
        i = folds[i]
        if i in seen:
            return None
        seen.add(i)
    return i


def decide(n: int, answers: list[Verdicts]) -> dict[int, tuple[str, int | None]]:
    """Which of `n` facts to drop: {position: (reason, rep_or_None)}.

    Non-durable when every model answered and every model says drop; folded
    into the primary model's representative when that fact itself stays."""
    if not answers or not answers[0].ok:
        return {}
    out: dict[int, tuple[str, int | None]] = {}
    if all(a.ok for a in answers):
        for i in range(n):
            if all(i in a.drop for a in answers):
                out[i] = ("non_durable: " + answers[0].drop[i], None)
    primary = answers[0]
    for m in primary.folds:
        if m in out:
            continue
        rep = _resolve_rep(m, primary.folds)
        if rep is not None and rep != m and rep not in out:
            out[m] = ("same_claim", rep)
    return out


async def _ask(
    client: Any,
    facts: list[dict[str, Any]],
    timeout_s: float,
    context: CaptureContext | None = None,
) -> Verdicts:
    try:
        result = await asyncio.wait_for(
            client.complete(
                system=VERIFIER_SYSTEM_PROMPT,
                user=_user_prompt(facts, context),
                max_tokens=90 * len(facts) + 400,
                response_format={"type": "json_object"},
            ),
            timeout=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 — fail open: keep every fact
        log.warning("durability verifier call failed (%s); keeping all facts", type(exc).__name__)
        return Verdicts()
    v = parse_verdicts(getattr(result, "text", "") or "", len(facts))
    v.tokens_in = int(getattr(result, "tokens_in", 0) or 0)
    v.tokens_out = int(getattr(result, "tokens_out", 0) or 0)
    return v


@dataclass
class VerifyResult:
    kept: list[dict[str, Any]]
    dropped: list[dict[str, Any]]
    tokens_in: int = 0
    tokens_out: int = 0
    answered: bool = False


async def verify(
    facts: list[dict[str, Any]],
    clients: list[Any],
    *,
    timeout_s: float = 45.0,
    context: CaptureContext | None = None,
) -> VerifyResult:
    """Judge `facts` with every client in `clients` (the first is primary).
    A dropped fact carries `_verify` (the reason) and, when folded,
    `_duplicate_of` (the key of the fact that carries its claim)."""
    if len(facts) < 1 or not clients:
        return VerifyResult(kept=list(facts), dropped=[])
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    t_in = t_out = 0
    answered = False
    for start in range(0, len(facts), settings.memory_durability_verifier_max_facts_per_call):
        chunk = facts[start : start + settings.memory_durability_verifier_max_facts_per_call]
        answers = list(await asyncio.gather(*(_ask(c, chunk, timeout_s, context) for c in clients)))
        t_in += sum(a.tokens_in for a in answers)
        t_out += sum(a.tokens_out for a in answers)
        answered = answered or answers[0].ok
        drops = decide(len(chunk), answers)
        for i, f in enumerate(chunk):
            if i not in drops:
                kept.append(f)
                continue
            reason, rep = drops[i]
            g = dict(f, _verify=reason)
            if rep is not None:
                rf = chunk[rep]
                g["_duplicate_of"] = str(rf.get("key") or rf.get("memory_key") or "")
            dropped.append(g)
    return VerifyResult(
        kept=kept, dropped=dropped, tokens_in=t_in, tokens_out=t_out, answered=answered
    )
