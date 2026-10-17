"""The conflict resolver's judgement: the prompt, the answer it parses and what each
verdict does. Pure; the reads, the LLM call and the writes are
server/memory/app/conflict_resolver.py.

One call per marked row carries the row and its nearest live peers, each under a
short id (`p1`..`p8`): a model miscopies a 36-character id often enough to matter.
Each peer gets a verdict and a confidence, and the fold acts on precision first:
nothing changes below `clear_at`, and only an update at `retire_at` or above
retires a row."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

UPDATE = "update"
CONTRADICTION = "contradiction"
ELABORATION = "elaboration"
RESTATEMENT = "restatement"
RELATED = "related"
UNRELATED = "unrelated"
VERDICTS = (UPDATE, CONTRADICTION, ELABORATION, RESTATEMENT, RELATED, UNRELATED)

# What the resolver does to one (row, peer) pair.
RETIRE = "retire"
FLAG = "flag"
CLEAR = "clear"
NOTHING = "nothing"

# A value is cut to this many characters in the prompt.
MAX_VALUE_CHARS = 700

SYSTEM_PROMPT = (
    "You check whether a NEW memory note makes an EXISTING note stale or contradicts it. "
    "A similarity search put them together; most such pairs are NOT conflicts. For EACH "
    "existing note decide, in this order:\n\n"
    "1. Entity: what specific thing is each note about? Name it by its identifier: a "
    "person, a PR number, an issue, a finding's title, a test, a file, a function, a "
    'setting, a service, or a tracked metric such as "<area> BUG findings tally". Two '
    "notes about different items are different entities even when they share a template, "
    "vocabulary, a subsystem or a code path: different PR numbers, different SQL constructs "
    "or functions, different files, different tests, different headers or findings with "
    "different titles are DIFFERENT entities.\n"
    "2. Attribute: which property of that entity does each note state (location, owner, "
    "status, version, count, rate, date, decision, verdict...)?\n"
    "3. Verdict:\n"
    '- "update": same entity AND same attribute, and NEW gives a different, newer value, so '
    "EXISTING is no longer current. Includes: moved, changed, replaced, reversed, upgraded, "
    "rescheduled; a status change (open -> fixed, closed, rejected, false positive); a later "
    "verdict on the same finding or hypothesis; a rolling summary or tally of the same "
    'area/metric with new numbers (e.g. "X BUG: 10 findings, 6 accepted" -> "X BUG: 11 '
    'findings, 5 accepted"), even if the example lists differ.\n'
    '- "contradiction": same entity AND same attribute, values incompatible, and nothing '
    "says which is newer.\n"
    '- "elaboration": same entity; NEW keeps everything EXISTING says and adds detail. '
    "EXISTING could be dropped without losing a fact.\n"
    '- "restatement": same information in other words.\n'
    '- "related": same entity or topic, but a different fact, step or property; both stay '
    'true. A bug report or test gap about a PR is "related" to that PR\'s summary unless it '
    "says the summary's claim is false.\n"
    '- "unrelated": different entities.\n'
    'Never answer "update" or "contradiction" unless same_entity and same_attribute are both '
    'true. Two notes about different items are "related" or "unrelated", never '
    '"contradiction".\n'
    "confidence: your probability (0 to 1) that the verdict is correct.\n"
    "Return JSON matching the schema, one entry per existing note id, reason first (one "
    "short sentence naming both entities)."
)

RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "judgement",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["peers"],
            "properties": {
                "peers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "peer_id",
                            "reason",
                            "same_entity",
                            "same_attribute",
                            "verdict",
                            "confidence",
                        ],
                        "properties": {
                            "peer_id": {"type": "string"},
                            "reason": {"type": "string"},
                            "same_entity": {"type": "boolean"},
                            "same_attribute": {"type": "boolean"},
                            "verdict": {"type": "string", "enum": list(VERDICTS)},
                            "confidence": {"type": "number"},
                        },
                    },
                }
            },
        },
    },
}


@dataclass(frozen=True)
class Note:
    """One memory as the prompt shows it."""

    memory_id: str
    memory_key: str
    value: str
    similarity: float = 1.0


@dataclass(frozen=True)
class PeerVerdict:
    memory_id: str
    verdict: str
    confidence: float
    same_entity: bool
    same_attribute: bool
    reason: str = ""


def max_output_tokens(n_peers: int) -> int:
    """The output cap for one call: room for one short entry per peer."""
    return 120 + 90 * n_peers


def short_id(i: int) -> str:
    return f"p{i + 1}"


def user_prompt(new: Note, peers: list[Note]) -> str:
    """The new row, then each peer under its short id with its key and cosine."""
    parts = [
        f"NEW note (key {new.memory_key}):\n{new.value[:MAX_VALUE_CHARS]}",
        "EXISTING notes:",
    ]
    for i, p in enumerate(peers):
        parts.append(
            f"[{short_id(i)}] (key {p.memory_key}, cosine {p.similarity:.2f})\n"
            f"{p.value[:MAX_VALUE_CHARS]}"
        )
    return "\n\n".join(parts)


_JSON_RE = re.compile(r"\{.*\}", re.S)


def _peer_of(raw_id: str, peers: list[Note]) -> Note | None:
    """The peer an answer's `peer_id` names: `p2`, `[p2]`, the memory key,
    `key <memory_key>` or the memory id."""
    pid = raw_id.strip().strip("[]").strip()
    if pid.lower().startswith("key "):
        pid = pid[4:].strip()
    m = re.fullmatch(r"[pP](\d+)", pid)
    if m and 1 <= int(m.group(1)) <= len(peers):
        return peers[int(m.group(1)) - 1]
    for p in peers:
        if pid in (p.memory_id, p.memory_key):
            return p
    return None


def parse_verdicts(text: str, peers: list[Note]) -> dict[str, PeerVerdict]:
    """{peer memory_id: its verdict} for every well-formed entry naming a peer.
    Malformed output yields {}: an entry with an unknown verdict, a confidence
    that is not a number in [0, 1], or a peer id that names no peer is dropped.
    A peer named twice keeps its first entry."""
    m = _JSON_RE.search(text or "")
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    entries = obj.get("peers") if isinstance(obj, dict) else None
    if not isinstance(entries, list):
        return {}
    out: dict[str, PeerVerdict] = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        peer = _peer_of(str(e.get("peer_id") or ""), peers)
        verdict = str(e.get("verdict") or "").strip().lower()
        confidence = e.get("confidence")
        if peer is None or verdict not in VERDICTS or peer.memory_id in out:
            continue
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            continue
        if not 0.0 <= float(confidence) <= 1.0:
            continue
        out[peer.memory_id] = PeerVerdict(
            memory_id=peer.memory_id,
            verdict=verdict,
            confidence=float(confidence),
            same_entity=e.get("same_entity") is True,
            same_attribute=e.get("same_attribute") is True,
            reason=str(e.get("reason") or "")[:300],
        )
    return out


def action_for(v: PeerVerdict, *, policy: str, retire_at: float, clear_at: float) -> str:
    """What the resolver does for one judged pair.

    An update or contradiction about a different entity or attribute is not one:
    it reads as `related`. Below `clear_at` nothing changes. An update retires the
    older row only under `newest_wins` and at `retire_at` or above; any other
    update, and a contradiction, goes to the review queue. Every compatible
    verdict clears."""
    verdict = v.verdict
    if verdict in (UPDATE, CONTRADICTION) and not (v.same_entity and v.same_attribute):
        verdict = RELATED
    if v.confidence < clear_at:
        return NOTHING
    if verdict == UPDATE:
        return RETIRE if policy == "newest_wins" and v.confidence >= retire_at else FLAG
    if verdict == CONTRADICTION:
        return FLAG
    return CLEAR
