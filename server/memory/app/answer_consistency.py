"""memory.ask checks its answer against the memories it cites.

A holistic quality judge misses contradictions: a turn can score 10/10 on
quality while an NLI model puts its consistency with the dialogue at 0.57, so
entailment rate, not a quality score, is what this checks. Loom's
cascade verifier (memory.domain.synth_policy.cheap_answer_failed) escalates a
cheap answer that is empty, uncited or hedged; it cannot see an answer that
is fluent, cited and wrong about what the citation says.

This module is that check. Given the answer and the memories it cited, one
LLM call at temperature zero labels the answer ENTAILED (every claim is
supported by a cited memory), NEUTRAL (claims the memories neither support
nor contradict) or CONTRADICTED (a claim conflicts with a cited memory), with
probabilities for entailment and contradiction. The consistency score is
``(1 + P_entail - P_contradict) / 2`` — 1.0 is entailed, 0.5
neutral, 0.0 contradicted.

Design notes
- Pure parsing and scoring here; ``check`` is the only I/O and it fails OPEN:
  any error yields an empty label and the ask proceeds unchanged. A
  consistency check must never turn an answer into an error.
- The judge sees only the CITED memories (the answer's own evidence), not
  the whole grounding window: the question is "does the answer say what its
  sources say", which is what a reader can verify.
- The prompt asks for the two probabilities and the offending span so a
  local NLI cross-encoder (e.g. DeBERTa-v3 MNLI) can replace
  the LLM behind the same ``Verdict`` without touching the caller.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from server.settings.budgets import llm_max_tokens

log = logging.getLogger(__name__)

ENTAILED = "entailed"
NEUTRAL = "neutral"
CONTRADICTED = "contradicted"
_LABELS = (ENTAILED, NEUTRAL, CONTRADICTED)

# Per-memory excerpt cap in the judge prompt; the synthesizer saw at most
# _EXCERPT_CHAR_LIMIT per memory too, so the judge sees what the answer saw.
_JUDGE_EXCERPT_CHARS = 1200
_JUDGE_MAX_MEMORIES = 12

_SYSTEM = (
    "You audit an assistant's answer against the memories it cited. Judge ONLY "
    "whether the answer is consistent with those memories — not whether it is "
    "complete, well written or true in the world.\n"
    "Labels:\n"
    "- entailed: every factual claim in the answer is stated by, or follows "
    "directly from, a cited memory.\n"
    "- neutral: the answer makes claims the memories neither state nor "
    "contradict (extra detail, general knowledge, hedges).\n"
    "- contradicted: at least one claim in the answer conflicts with what a "
    "cited memory says (a different value, date, status, owner, count or "
    "polarity).\n"
    "Extra detail that does not conflict is NOT a contradiction. A memory "
    "that is newer supersedes an older one on the same attribute; following "
    "the newer value is consistent.\n"
    'Respond with ONE JSON object and nothing else: {"label": "entailed" | '
    '"neutral" | "contradicted", "p_entail": 0.0-1.0, "p_contradict": '
    '0.0-1.0, "contradicted_key": "<memory_key or empty>", "span": "<the '
    'offending phrase from the answer, or empty>"}.'
)


@dataclass(frozen=True)
class Verdict:
    label: str = ""  # "" when the check did not run or could not be parsed
    p_entail: float = 0.0
    p_contradict: float = 0.0
    contradicted_key: str = ""
    span: str = ""

    @property
    def ran(self) -> bool:
        return self.label in _LABELS

    @property
    def score(self) -> float:
        """Consistency score: (1 + P_ent - P_con) / 2."""
        if not self.ran:
            return 0.0
        return round(max(0.0, min(1.0, (1.0 + self.p_entail - self.p_contradict) / 2.0)), 3)

    @property
    def contradicted(self) -> bool:
        return self.label == CONTRADICTED


_JSON_RE = re.compile(r"\{.*\}", re.S)


def _clamp(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f:  # NaN
        return 0.0
    return max(0.0, min(1.0, f))


def parse_verdict(text: str) -> Verdict:
    """Tolerant parse: the first {...} block in the text; prose around it is
    ignored. An unknown or missing label yields the empty Verdict (check did
    not run). Probabilities are clamped to [0, 1]; when the model gives a
    label but no probabilities, the label alone sets them (1/0, 0/0, 0/1) so
    the score is still meaningful."""
    if not text:
        return Verdict()
    m = _JSON_RE.search(text)
    if not m:
        return Verdict()
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return Verdict()
    if not isinstance(obj, dict):
        return Verdict()
    label = str(obj.get("label") or "").strip().lower()
    if label not in _LABELS:
        return Verdict()
    has_probs = "p_entail" in obj or "p_contradict" in obj
    if has_probs:
        pe, pc = _clamp(obj.get("p_entail")), _clamp(obj.get("p_contradict"))
    else:
        pe = 1.0 if label == ENTAILED else 0.0
        pc = 1.0 if label == CONTRADICTED else 0.0
    return Verdict(
        label=label,
        p_entail=pe,
        p_contradict=pc,
        contradicted_key=str(obj.get("contradicted_key") or "")[:200],
        span=str(obj.get("span") or "")[:300],
    )


def build_user_prompt(question: str, answer: str, cited: list[Any]) -> str:
    parts = [f"Question: {question}", f"Answer under audit:\n{answer}", "Cited memories:"]
    for m in cited[:_JUDGE_MAX_MEMORIES]:
        key = str(getattr(m, "memory_key", "") or "")
        excerpt = str(getattr(m, "content_excerpt", "") or "")[:_JUDGE_EXCERPT_CHARS]
        date = str(getattr(m, "valid_at", "") or "")[:10]
        stamp = f" [{date}]" if date else ""
        parts.append(f"[{key}]{stamp} {excerpt}")
    return "\n\n".join(parts)


async def check(llm: Any, question: str, answer: str, cited: list[Any]) -> Verdict:
    """One judge call; fails open to the empty Verdict on any error."""
    if llm is None or not (answer or "").strip() or not cited:
        return Verdict()
    from server.observability.genai import llm_stage

    try:
        with llm_stage("ask_consistency"):
            res = await llm.complete(
                _SYSTEM,
                build_user_prompt(question, answer, cited),
                max_tokens=llm_max_tokens("ask_consistency"),
                response_format={"type": "json_object"},
            )
        return parse_verdict(str(getattr(res, "text", "") or ""))
    except Exception as e:  # noqa: BLE001 — a check never blocks an answer
        log.warning("answer consistency check failed (open): %s", e)
        return Verdict()


# ---------------------------------------------------------------------------
# The question's premise against pinned / verified memories.
# ---------------------------------------------------------------------------
# Holding a correct answer against a wrong premise needs the established facts
# checked before the question is adopted. The trust primitives are memory.pin
# and memory.verify (a person attests, bound to the content hash); without this
# check, a question that presupposed the opposite of a pinned fact would be
# answered inside its own premise.


@dataclass(frozen=True)
class TrustedRow:
    memory_key: str
    pinned: bool = False
    verified_by: str = ""

    @property
    def label(self) -> str:
        if self.verified_by and self.pinned:
            return "pinned, human-verified"
        if self.verified_by:
            return "human-verified"
        return "pinned"


@dataclass(frozen=True)
class PremiseVerdict:
    conflict: bool = False
    memory_key: str = ""
    premise: str = ""
    correction: str = ""
    ran: bool = False


_PREMISE_SYSTEM = (
    "You check whether a QUESTION assumes something that a TRUSTED memory "
    "contradicts. Trusted memories were pinned or verified by a person; treat "
    "them as correct. Look only at what the question asserts or presupposes "
    '("since X", "now that X", "X, so ...", a stated value, date, owner, '
    "status or count) — not at what it asks.\n"
    "A conflict exists ONLY when a presupposition is incompatible with a "
    "trusted memory. A question that merely asks about the topic, or asks "
    "whether something is true, is not a conflict.\n"
    'Respond with ONE JSON object and nothing else: {"conflict": true | false, '
    '"memory_key": "<key of the trusted memory contradicted, or empty>", '
    '"premise": "<the assumption in the question, or empty>", "correction": '
    '"<what the trusted memory says instead, one clause, or empty>"}.'
)


def parse_premise(text: str) -> PremiseVerdict:
    if not text:
        return PremiseVerdict()
    m = _JSON_RE.search(text)
    if not m:
        return PremiseVerdict()
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return PremiseVerdict()
    if not isinstance(obj, dict) or "conflict" not in obj:
        return PremiseVerdict()
    conflict = obj.get("conflict") is True
    key = str(obj.get("memory_key") or "")[:200]
    if conflict and not key:
        # A conflict must point at a trusted memory; without one it is opinion.
        return PremiseVerdict(ran=True)
    return PremiseVerdict(
        conflict=conflict,
        memory_key=key if conflict else "",
        premise=str(obj.get("premise") or "")[:300] if conflict else "",
        correction=str(obj.get("correction") or "")[:300] if conflict else "",
        ran=True,
    )


def trusted_rows(rows: list[dict[str, Any]], pool_keys: list[str]) -> list[TrustedRow]:
    """loom.memory rows (memory_key, pinned, verified_by, attested) folded to
    the pool's pinned-or-verified keys, pool order. A row is verified only
    when `attested` (its attestation binds its current content); an actor
    over changed content is not."""
    by_key: dict[str, TrustedRow] = {}
    for r in rows:
        k = str(r.get("memory_key") or "")
        pinned = bool(int(r.get("pinned") or 0))
        verified_by = str(r.get("verified_by") or "") if int(r.get("attested") or 0) else ""
        if k and (pinned or verified_by):
            by_key[k] = TrustedRow(k, pinned, verified_by)
    return [by_key[k] for k in pool_keys if k in by_key]


def build_premise_prompt(question: str, trusted: list[Any], rows: list[TrustedRow]) -> str:
    label = {t.memory_key: t.label for t in rows}
    parts = [f"Question: {question}", "Trusted memories:"]
    for m in trusted[:_JUDGE_MAX_MEMORIES]:
        key = str(getattr(m, "memory_key", "") or "")
        excerpt = str(getattr(m, "content_excerpt", "") or "")[:_JUDGE_EXCERPT_CHARS]
        parts.append(f"[{key}] ({label.get(key, 'trusted')}) {excerpt}")
    return "\n\n".join(parts)


_PREMISE_MAX_TRUSTED = 3


async def premise_check(
    llm: Any, question: str, trusted: list[Any], rows: list[TrustedRow]
) -> PremiseVerdict:
    """Judge the question against the trusted memories ONE AT A TIME, in pool
    (relevance) order, at most `_PREMISE_MAX_TRUSTED`; the first conflict
    wins. Fails open.

    On sixteen leading questions over a pinned-dense namespace, shown one trusted memory
    the cheap judge found the presupposition 12/16 times and named the right memory
    every time; shown three it found 3/16, shown eleven 1/16. Several trusted memories
    in one prompt dilute the comparison, so the prompt carries one.
    """
    if llm is None or not trusted or not rows:
        return PremiseVerdict()
    from server.observability.genai import llm_stage

    by_key = {str(getattr(m, "memory_key", "") or ""): m for m in trusted}
    ran = False
    for row in rows[:_PREMISE_MAX_TRUSTED]:
        m = by_key.get(row.memory_key)
        if m is None:
            continue
        try:
            with llm_stage("ask_premise"):
                res = await llm.complete(
                    _PREMISE_SYSTEM,
                    build_premise_prompt(question, [m], [row]),
                    max_tokens=llm_max_tokens("ask_premise"),
                    response_format={"type": "json_object"},
                )
            v = parse_premise(str(getattr(res, "text", "") or ""))
        except Exception as e:  # noqa: BLE001 — a premise check never blocks an answer
            log.warning("premise check failed (open): %s", e)
            return PremiseVerdict(ran=ran)
        ran = ran or v.ran
        if v.conflict and v.memory_key == row.memory_key:
            return v
    return PremiseVerdict(ran=ran)


def premise_note(v: PremiseVerdict, rows: list[TrustedRow]) -> str:
    """System-prompt addendum: hold the trusted fact, correct the premise."""
    if not v.conflict:
        return ""
    label = next((t.label for t in rows if t.memory_key == v.memory_key), "trusted")
    premise = f" It presupposes: {v.premise}." if v.premise else ""
    correction = f" That memory records: {v.correction}." if v.correction else ""
    return (
        f" The question assumes something that memory [{v.memory_key}] ({label}) "
        f"contradicts.{premise}{correction} Do NOT adopt the question's premise. "
        "Answer from the trusted memory, say in one clause that the premise is "
        "contradicted and by which memory, and only then address the rest."
    )
