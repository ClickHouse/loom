"""The specifics a turn stated come back when the fact dropped them.

Two more shapes of the dropped-phrase loss the other extraction recoveries
handle — the model was given the words and left them out:

- An AMOUNT: "I bought my bike helmet for $120" became "User
  has been using a bike helmet … got from the local bike shop"; "how much have I spent
  on bike expenses" then summed two of three purchases ($65 instead of $185). When the
  cited sentence that best matches a value carries a currency amount and the value
  carries no amount at all, the amount is appended: "(for $120)".
- A START or FINISH: "I started listening to 'The Power' by Naomi
  Alderman" in a turn about another book yielded rows about that other book and none
  about the start ("the start date is not recorded"). A first-person
  start/finish/completion clause with a named object that no fact of the turn covers
  becomes its own fact, dated by the turn like any event row ("User started listening to
  'The Power' by Naomi Alderman.").
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_TOKEN_RE = re.compile(r"[a-z]{3,}")
_STOP = frozenset(
    "the and for with that this from his her their its was were been have has had got get about "
    "just really very some they them she him you your our who what when which into onto also "
    "recently today yesterday finally started finished began completed listening reading".split()
)
_EVENT_RE = re.compile(
    r"\bI (?:just |finally |recently |also )?"
    r"(?P<verb>started|began|finished|completed|wrapped up|kicked off|attended|visited|"
    r"went to|participated in|took part in|volunteered at|fixed|repaired|serviced|replaced|"
    r"installed)\s+"
    r"(?P<obj>(?:reading|listening to|watching|playing|taking|learning|writing|building|"
    r"training for|working on|attending|a |an |my |the |'|\")[^.!?;]{4,120})",
    re.IGNORECASE,
)
_TIME_TAIL_RE = re.compile(
    r"\s+(?:today|yesterday|this (?:morning|week|weekend)|last (?:week|night|weekend))\b",
    re.IGNORECASE,
)
_PHASE_WORDS = {
    "started": ("start", "began", "begin"),
    "began": ("start", "began", "begin"),
    "finished": ("finish", "complet", "wrapped", "done"),
    "completed": ("finish", "complet", "wrapped", "done"),
    "wrapped up": ("finish", "complet", "wrapped", "done"),
    "kicked off": ("start", "began", "begin", "kick"),
    "attended": ("attend", "went", "visit", "was at", "saw"),
    "visited": ("visit", "went", "attend", "trip to", "was at"),
    "went to": ("went", "visit", "attend", "trip to", "was at"),
    "participated in": ("particip", "took part", "joined", "ran", "competed", "attend"),
    "took part in": ("particip", "took part", "joined", "ran", "competed", "attend"),
    "volunteered at": ("volunteer", "helped"),
    "fixed": ("fix", "repair", "servic", "replac"),
    "repaired": ("fix", "repair", "servic", "replac"),
    "serviced": ("servic", "maintenance", "tune", "fix", "repair"),
    "replaced": ("replac", "new", "swap"),
    "installed": ("install", "set up", "put in"),
}
_CONJ_CUT_RE = re.compile(
    r"\s+(?:and|but|so|which|because|since|although|while)\s+(?:I|I'm|I've|I'd|it|it's|we|my)\b",
    re.IGNORECASE,
)
_VERB_3P = {
    "started": "started",
    "began": "began",
    "finished": "finished",
    "completed": "completed",
    "wrapped up": "wrapped up",
    "kicked off": "kicked off",
}


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOP}


_COST_RE = re.compile(
    r"\bI (?:just |recently |also |finally )?(?P<verb>bought|purchased|paid|spent|got|picked up|"
    r"ordered|booked|rented|donated|tipped|splurged on)\s+(?P<obj>[^.!?;]{3,110}?)"
    r"(?P<amt>(?:\$|€|£)\s?\d[\d,]*(?:\.\d{1,2})?(?:\s?[kK])?)\b",
    re.IGNORECASE,
)
_COST_3P = {
    "bought": "bought",
    "purchased": "purchased",
    "paid": "paid",
    "spent": "spent",
    "got": "got",
    "picked up": "picked up",
    "ordered": "ordered",
    "booked": "booked",
    "rented": "rented",
    "donated": "donated",
    "tipped": "tipped",
    "splurged on": "splurged on",
}


def stated_costs(text: str) -> list[tuple[str, str, str, str]]:
    """(sentence, verb, object, amount) for each first-person purchase or
    payment that names its amount ("I bought my bike helmet for
    $120", "I paid $40 for a parking ticket", "I spent around $70 on
    groceries"). Questions are skipped."""
    out: list[tuple[str, str, str, str]] = []
    for sent in _SENTENCE_SPLIT_RE.split(text or ""):
        if "?" in sent:
            continue
        for m in _COST_RE.finditer(sent):
            obj = m.group("obj").strip().rstrip(",")
            # The object must name a thing: a pronoun ("it from Petco") or a
            # bare "online for" carries nothing the amount can attach to.
            if re.match(r"(?i)(?:it|them|her|him|one|this|that|these|those)\b", obj):
                continue
            if len(_tokens(obj) - {"for", "from", "online", "around", "about"}) < 1:
                continue
            out.append((sent, m.group("verb").lower(), obj, m.group("amt").replace(" ", "")))
    return out


def stated_events(text: str) -> list[tuple[str, str, str]]:
    """(sentence, verb, object) for each first-person start/finish clause."""
    out: list[tuple[str, str, str]] = []
    for sent in _SENTENCE_SPLIT_RE.split(text or ""):
        if "?" in sent and not _EVENT_RE.search(sent):
            continue
        for m in _EVENT_RE.finditer(sent):
            obj = m.group("obj").strip().rstrip(",")
            # Cut at a trailing time phrase the anchor pass resolves on its own.
            obj = _TIME_TAIL_RE.split(obj, maxsplit=1)[0]
            obj = _CONJ_CUT_RE.split(obj, maxsplit=1)[0].strip().rstrip(",")
            named = bool(re.search(r"[\"'\u201c\u2018]|\b[A-Z][a-z]{2,}", obj))
            activity = bool(
                re.match(
                    r"(?i)(?:reading|listening|watching|playing|taking|learning|"
                    r"writing|building|training|working|attending)\b",
                    obj,
                )
            )
            if len(_tokens(obj)) >= 2 and (named or activity):
                out.append((sent, m.group("verb").lower(), obj))
    return out


def recover_dropped_specifics(
    facts: Sequence[Any],
    messages: Sequence[Mapping[str, Any]] | None,
    *,
    make_event: Any,
    make_cost: Any,
    min_share: float = 0.5,
    max_new: int = 3,
) -> list[Any]:
    """`facts` plus one fact per uncovered start/finish clause via
    `make_event(key, value, index)` and one per uncovered stated cost
    via `make_cost(key, value, index)` — uncovered meaning no fact citing
    the turn carries the amount."""
    if not messages:
        return list(facts)
    out = list(facts)
    added = 0
    for idx, m in enumerate(messages):
        if not _is_user(m):
            continue
        text = str(m.get("content") or "")
        turn_facts = [f for f in facts if idx in _cited(f)] or list(facts)
        for _sent, verb, obj in stated_events(text):
            ot = _tokens(obj)
            if not ot or any(_covers(f, ot, verb, min_share) for f in turn_facts):
                continue
            key = f"user_{_VERB_3P.get(verb, verb).replace(' ', '_')}_{'_'.join(sorted(ot))[:40]}"
            if any(getattr(f, "memory_key", "") == key for f in out):
                continue
            out.append(make_event(key, f"User {_VERB_3P.get(verb, verb)} {obj}.", idx))
            added += 1
            if added >= max_new:
                return out
        for _sent, verb, obj, amt in stated_costs(text):
            digits = re.sub(r"[^\d.]", "", amt)
            if any(
                digits in re.sub(r"[^\d.]", "", str(getattr(f, "value", "") or ""))
                for f in turn_facts
            ):
                continue
            v3 = _COST_3P.get(verb, verb)
            key = f"user_{v3.replace(' ', '_')}_{'_'.join(sorted(_tokens(obj)))[:40]}"
            if any(getattr(f, "memory_key", "") == key for f in out):
                continue
            out.append(make_cost(key, f"User {v3} {obj.rstrip()} {amt}.", idx))
            added += 1
            if added >= max_new:
                return out
    return out


def _cited(f: Any) -> list[int]:
    return list(getattr(f, "source_message_indices", None) or [])


def _covers(f: Any, obj_tokens: set[str], verb: str, min_share: float) -> bool:
    """A fact covers a start/finish clause when it names the object AND the
    phase: "listening to The Power" under "User just finished listening to
    'The Power'" is covered for 'finished', not for 'started'."""
    value = str(getattr(f, "value", "") or "").lower()
    if len(obj_tokens & _tokens(value)) / len(obj_tokens) < min_share:
        return False
    phase = _PHASE_WORDS.get(verb, (verb[:5],))
    return any(p in value for p in phase)


def _is_user(m: Mapping[str, Any]) -> bool:
    return str(m.get("role") or "user").lower() in ("user", "human", "")
