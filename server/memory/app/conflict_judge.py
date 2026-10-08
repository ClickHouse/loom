"""A cosine hit is a question; the judge answers it.

Loom flags a write `conflicted` when it lands within cosine 0.85 of an
existing row with a different canonical form. Judged one by one, 48 such
pairs split into 27 about a different subject (two hash-join PRs), 10 the
same fact restated with more detail and 11 contradictions: three of four
flags were not conflicts, yet each would reach the review queue and the
`conflicted_penalty` demotion in search. A naive reading also takes "adopted
another dog named Scout" as a contradiction of "adopted a dog named Buddy".
When the cosine check would say "conflict", one cheap call labels each peer:

- CONTRADICTION: incompatible values for the same attribute → the
  `conflict` verdict.
- ELABORATION: the new row carries EVERYTHING the old row says plus more,
  so the old row could be dropped without losing a fact → the verdict
  `refines`: the new row is inserted active, the peer is superseded
  (status, `superseded_by`, `invalid_at`; never deleted) and a `refines`
  edge records why: an update, in bitemporal form.
- RESTATEMENT: same information in other words → `merge` to the existing
  row, as the canonical-form match already does.
- RELATED: the same subject or topic but a different fact, plan or step;
  the old row is still needed on its own → `clear`; both rows stay.
- UNRELATED: a different attribute or subject → `clear`; nothing is
  flagged.

Contradiction wins over elaboration, which wins over restatement, which wins
over related and unrelated, so a mixed set of peers is never quietly cleared.
Parsing is tolerant and everything fails open to the cosine verdict.

Why `related` exists: without it, the next step of the same story
("planning a trip to San Francisco, considering United" followed by
"planning to visit Alcatraz during the trip") reads as an elaboration and
retires the earlier row. With the label, such pairs come back `related`,
while a real elaboration (27 bird species → 28 after a Northern Flicker)
stays `elaboration` and contradictions stay `contradiction`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

CONTRADICTION = "contradiction"
ELABORATION = "elaboration"
RESTATEMENT = "restatement"
RELATED = "related"
UNRELATED = "unrelated"
_LABELS = (CONTRADICTION, ELABORATION, RESTATEMENT, RELATED, UNRELATED)

_MAX_VALUE_CHARS = 700

_SYSTEM = (
    "You compare a NEW memory against EXISTING memories that a similarity search "
    "put near it. For each existing memory, output exactly one label:\n"
    "- contradiction: the two assert incompatible values for the same attribute "
    "of the same subject (a different status, owner, date, count, place, or a "
    "negation). Being more recent is not a contradiction by itself; a different "
    "value is.\n"
    "- elaboration: the new memory contains EVERYTHING the existing one says and "
    "adds detail or a more specific version, so the existing memory could be "
    "dropped without losing any information. If any fact in the existing memory "
    "is missing from the new one, it is not an elaboration.\n"
    "- restatement: the same information in different words, no new detail.\n"
    "- related: the same topic or subject, but a different fact, event, plan or "
    "step; the existing memory is still needed on its own (a later step of the "
    "same trip, a second interest, feedback about the same product).\n"
    "- unrelated: a different attribute, a different subject, or a different "
    "item that merely shares vocabulary (two different pull requests, two "
    "different tests, two different people).\n"
    'Respond with ONE JSON object and nothing else: {"labels": {"<id>": '
    '"contradiction" | "elaboration" | "restatement" | "related" | "unrelated", '
    "...}} with exactly one entry per existing memory id."
)


@dataclass(frozen=True)
class Judgement:
    """Per-peer labels plus the verdict they fold to.

    `verdict` is one of clear | merge | refines | conflict; `ran` is False when
    the judge did not run or could not be parsed (caller keeps the cosine
    verdict). `keep` lists the peers that keep the verdict's meaning: the
    contradicted peers for `conflict`, the elaborated peers for `refines`,
    the restated peer for `merge`."""

    verdict: str = "conflict"
    labels: dict[str, str] = field(default_factory=dict)
    keep: list[str] = field(default_factory=list)
    ran: bool = False


_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_labels(text: str, peer_ids: list[str]) -> dict[str, str]:
    """{peer_id: label} for the ids the model labelled with a known label.
    Prose around the JSON is ignored; unknown ids and labels are dropped."""
    if not text or not peer_ids:
        return {}
    m = _JSON_RE.search(text)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    raw = obj.get("labels") if isinstance(obj, dict) else None
    if not isinstance(raw, dict):
        return {}
    allowed = set(peer_ids)
    out: dict[str, str] = {}
    for k, v in raw.items():
        pid = str(k)
        lab = str(v or "").strip().lower()
        if pid in allowed and lab in _LABELS:
            out[pid] = lab
    return out


def fold(labels: dict[str, str], peer_ids: list[str]) -> Judgement:
    """Fold per-peer labels into one verdict. Peers the judge did not label
    count as contradiction — the safe reading of an unlabelled cosine hit."""
    if not peer_ids:
        return Judgement(verdict="clear", labels=dict(labels), ran=bool(labels))
    full = {pid: labels.get(pid, CONTRADICTION) for pid in peer_ids}
    contradicted = [p for p in peer_ids if full[p] == CONTRADICTION]
    if contradicted:
        return Judgement("conflict", full, contradicted, ran=True)
    elaborated = [p for p in peer_ids if full[p] == ELABORATION]
    if elaborated:
        return Judgement("refines", full, elaborated, ran=True)
    restated = [p for p in peer_ids if full[p] == RESTATEMENT]
    if restated:
        return Judgement("merge", full, restated[:1], ran=True)
    # related and unrelated both keep every row: nothing to retire, merge or flag.
    return Judgement("clear", full, [], ran=True)


def build_user_prompt(new_value: str, peers: list[Any]) -> str:
    parts = [f"NEW memory:\n{str(new_value)[:_MAX_VALUE_CHARS]}", "EXISTING memories:"]
    for p in peers:
        pid = str(getattr(p, "memory_id", "") or "")
        val = str(getattr(p, "value", "") or getattr(p, "canonical", "") or "")[:_MAX_VALUE_CHARS]
        parts.append(f"[{pid}] {val}")
    return "\n\n".join(parts)


async def judge(llm: Any, new_value: str, peers: list[Any]) -> Judgement:
    """One call over the given peers (the caller caps them). Fails open:
    `ran=False` and the cosine verdict `conflict` on any error."""
    if llm is None or not peers or not (new_value or "").strip():
        return Judgement()
    peer_ids = [str(getattr(p, "memory_id", "") or "") for p in peers]
    from server.observability.genai import llm_stage

    try:
        with llm_stage("conflict_judge"):
            res = await llm.complete(
                _SYSTEM,
                build_user_prompt(new_value, peers),
                max_tokens=40 + 24 * len(peers),
                response_format={"type": "json_object"},
            )
        labels = parse_labels(str(getattr(res, "text", "") or ""), peer_ids)
    except Exception as e:  # noqa: BLE001 — a judge never blocks a write
        log.warning("conflict judge failed (open, cosine verdict kept): %s", e)
        return Judgement()
    if not labels:
        return Judgement()
    return fold(labels, peer_ids)
