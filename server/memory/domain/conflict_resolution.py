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
from datetime import UTC, datetime
from typing import Any

import numpy as np

from server.memory.domain.memory_types import DEFINITION

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

# The policies under which a confident update retires the older row; `manual_review`
# only flags it, and `append_only` is never judged.
RETIRING_POLICIES = ("newest_wins", "auto_reinforce")

# A value is cut to this many characters in the prompt.
MAX_VALUE_CHARS = 700  # not a setting: the thresholds were calibrated on this prompt

# Named in each memory.conflict_resolver audit row, so a verdict can be traced to the
# prompt that produced it. Change it with SYSTEM_PROMPT or `user_prompt`.
PROMPT_VERSION = "v3"

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
    '- "update": same entity AND same attribute, NEW gives a different value and is the '
    "later statement, so EXISTING is no longer current. Includes: moved, changed, "
    "replaced, reversed, upgraded, rescheduled; a status change (open -> fixed, closed, "
    "rejected, false positive); a later verdict on the same finding or hypothesis, even "
    "when EXISTING holds more detail; a rolling summary or tally of the same "
    'area/metric with new numbers (e.g. "X BUG: 10 findings, 6 accepted" -> "X BUG: 11 '
    'findings, 5 accepted"), even if the example lists differ.\n'
    '- "contradiction": same entity AND same attribute, values incompatible, and either '
    "nothing says which is newer or EXISTING is the later statement.\n"
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
    "Each note shows when it was written and whether it comes from NEW's session. A value "
    "tied to one session's work (a task id, a deadline, a build, a slot, a test run, the PR "
    "under work) stated in another session is about that session's task: a different "
    "entity, unless the text names the same item.\n"
    'A note that does not name its area or item ("findings are accepted 60% of the time") '
    "is not the same entity as a note about a named area, nor as another such note whose "
    "focus or examples differ.\n"
    "A PR summary and a finding that PR fixes are different entities, unless the PR note "
    "names the finding and states its new status.\n"
    'A later version of a list of advice or rules is an "update" only if it keeps every '
    'item of EXISTING; if it drops items, EXISTING stays current ("related").\n'
    "A tip, substitution, addition or estimate about an item (use X instead of Y in a "
    'recipe, a price for a shopping list) is "related" to that item, not an update: the '
    "item itself stays true.\n"
    "Two items of one kind (two rides, two trips, two phrases, two routines) are different "
    'entities. Successive events (moves in a game, steps in a story) are "related", never '
    'an "update": only a new value of a state that replaces the old one is.\n'
    'A completed action is later than a plan or need to do it: "needs to do X" never '
    'supersedes "did X". When the times do not order two notes, judge the order from the '
    'text; if the text does not show it either, answer "contradiction".\n'
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
    """One memory as the prompt shows it. `written` is when it was stated (a day when its
    writer dated the fact: `dated`), `session` how its session relates to the judged
    row's (for a peer)."""

    memory_id: str
    memory_key: str
    value: str
    similarity: float = 1.0
    written: str = ""
    session: str = ""
    dated: bool = False


def same_fact_day(a: Note, b: Note) -> bool:
    """Both writers dated the fact and the prompt shows one day for both: two facts from
    one conversation day have no order, so an update between them goes to review."""
    return a.dated and b.dated and bool(a.written) and a.written == b.written


def written_at(holds_ns: int | None, valid_at: Any = None) -> str:
    """The time a note is shown with: when its statement holds (`holds_ns`, wall-clock ns,
    the order a retirement checks), as a date when the writer gave the fact's date."""
    if not holds_ns:
        return ""
    t = datetime.fromtimestamp(holds_ns / 1e9, UTC)
    return t.strftime("%Y-%m-%d" if valid_at else "%Y-%m-%d %H:%M")


def session_relation(row_agent: str, row_session: str, agent: str, session: str) -> str:
    """How a peer's session relates to the judged row's."""
    if (agent, session) == (row_agent, row_session):
        return "same session"
    return "other session, same agent" if agent == row_agent else "other agent"


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
    written = f", written {new.written}" if new.written else ""
    parts = [
        f"NEW note (key {new.memory_key}{written}):\n{new.value[:MAX_VALUE_CHARS]}",
        "EXISTING notes:",
    ]
    for i, p in enumerate(peers):
        meta = "".join(f", {x}" for x in (p.written and f"written {p.written}", p.session) if x)
        parts.append(
            f"[{short_id(i)}] (key {p.memory_key}, cosine {p.similarity:.2f}{meta})\n"
            f"{p.value[:MAX_VALUE_CHARS]}"
        )
    return "\n\n".join(parts)


def _peer_of(raw_id: str, peers: list[Note]) -> Note | None:
    """The peer an answer's `peer_id` names: `p2`, `[p2]`, the memory id, or the
    memory key (`key <memory_key>`) when no other peer has that key: two sessions'
    rows can share one."""
    pid = raw_id.strip().strip("[]").strip()
    by_key = pid.lower().startswith("key ")
    if by_key:
        pid = pid[4:].strip()  # an explicit key is only ever a key, never a short id
    m = re.fullmatch(r"[pP](\d+)", pid)
    if not by_key and m and 1 <= int(m.group(1)) <= len(peers):
        return peers[int(m.group(1)) - 1]
    for p in peers:
        if not by_key and pid == p.memory_id:
            return p
    keyed = [p for p in peers if pid == p.memory_key]
    return keyed[0] if len(keyed) == 1 else None


def parse_verdicts(text: str, peers: list[Note]) -> dict[str, PeerVerdict]:
    """{peer memory_id: its verdict} for every well-formed entry naming a peer.
    Malformed output yields {}: an entry with an unknown verdict, a confidence
    that is not a number in [0, 1], a `same_entity` or `same_attribute` that is not
    a boolean, or a peer id that names no peer is dropped, so its peer is left
    unresolved and never acted on. A peer named twice keeps its first well-formed entry,
    unless any two of its entries disagree on the verdict: then it has none. Checked
    here, not trusted to the schema: not every provider enforces it."""
    # The first JSON object in the text, whatever follows it (a greedy match from the
    # first brace to the last would take a trailing note's braces too).
    text = text or ""
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[text.index("{") :])
    except ValueError:
        return {}
    entries = obj.get("peers") if isinstance(obj, dict) else None
    if not isinstance(entries, list):
        return {}
    out: dict[str, PeerVerdict] = {}
    named: dict[str, str] = {}  # each peer's first verdict, its entry well-formed or not
    torn: set[str] = set()  # peers two entries gave different verdicts
    for e in entries:
        if not isinstance(e, dict):
            continue
        peer = _peer_of(str(e.get("peer_id") or ""), peers)
        raw_verdict, confidence = e.get("verdict"), e.get("confidence")
        verdict = raw_verdict.strip().lower() if isinstance(raw_verdict, str) else ""
        if peer is None or verdict not in VERDICTS:
            continue
        if named.setdefault(peer.memory_id, verdict) != verdict:
            torn.add(peer.memory_id)
        if peer.memory_id in out:
            continue
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            continue
        # A missing boolean is no answer; reading it as False would hide a real conflict.
        if not (
            isinstance(e.get("same_entity"), bool) and isinstance(e.get("same_attribute"), bool)
        ):
            continue
        if not 0.0 <= float(confidence) <= 1.0:
            continue
        out[peer.memory_id] = PeerVerdict(
            memory_id=peer.memory_id,
            verdict=verdict,
            confidence=float(confidence),
            same_entity=e["same_entity"],
            same_attribute=e["same_attribute"],
            reason=str(e.get("reason") or "")[:300],
        )
    return {mid: v for mid, v in out.items() if mid not in torn}


def action_for(
    v: PeerVerdict,
    *,
    policy: str,
    retire_at: float,
    clear_at: float,
    flag_at: float,
    cross_session: bool = False,
    same_written: bool = False,
) -> str:
    """What the resolver does for one judged pair.

    A verdict of update or contradiction that names a different entity or attribute is
    incoherent: nothing changes. Below `clear_at` nothing changes, nor does an update or
    a contradiction below `flag_at`. An update at `retire_at` or above retires the older
    row under `newest_wins` and `auto_reinforce`; any other update or contradiction goes
    to review. `cross_session` (another session of the anonymous user) changes nothing
    for an update or contradiction; `same_written` (both facts dated the same day) sends
    an update to review. A compatible verdict clears."""
    verdict = v.verdict
    if verdict in (UPDATE, CONTRADICTION) and not (v.same_entity and v.same_attribute):
        return NOTHING
    if v.confidence < clear_at:
        return NOTHING
    if cross_session and verdict in (UPDATE, CONTRADICTION):
        return NOTHING
    if verdict in (UPDATE, CONTRADICTION) and v.confidence < flag_at:
        return NOTHING
    if verdict == UPDATE:
        retires = policy in RETIRING_POLICIES and v.confidence >= retire_at and not same_written
        return RETIRE if retires else FLAG
    if verdict == CONTRADICTION:
        return FLAG
    return CLEAR


def later_close_siblings(vectors: list[list[float]], types: list[str], floor: float) -> set[int]:
    """The indexes of a batch's rows that have an EARLIER row of the same type at
    cosine >= `floor`. The verdicts compare each row with the store only, so an
    update inside one batch is marked here; the later row is the newer statement
    (the batch keeps reading order). Rows without a vector, and definitions, never pair."""
    groups: dict[tuple[str, int], list[int]] = {}
    for i, (vec, kind) in enumerate(zip(vectors, types, strict=True)):
        if vec and kind != DEFINITION:
            groups.setdefault((kind, len(vec)), []).append(i)
    out: set[int] = set()
    for idx in groups.values():
        if len(idx) < 2:
            continue
        m = np.asarray([vectors[i] for i in idx], dtype=np.float32)
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        unit = m / np.where(norms == 0, 1.0, norms)
        # close[a, b]: rows a and b of the group are near; only an earlier a counts.
        close = np.triu(unit @ unit.T >= floor, k=1)
        out.update(idx[b] for b in np.flatnonzero(close.any(axis=0)))
    return out
