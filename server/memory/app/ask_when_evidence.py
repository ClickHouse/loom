"""A when-question that the memories "do not record" is answered from the
dated raw turns.

On temporal questions over multi-session conversations with the ask reader, 40 % of
the misses were the synthesizer saying the memories do not record an event that the
conversation states in plain text: extraction turned "I started a yoga
class last Friday" into an attribute ("Deborah does yoga") or into nothing,
and which events survive changes from one ingest to the next (2 fixed, 4
lost on a re-ingest of the same two conversations). The raw turns are
stored for every ingested session and always hold the event; with evidence
dating they also hold the day it was said.

So: when the question asks WHEN (or how long) and the first answer says the memories do
not record it, recall the turns that carry the event phrase, render them dated, and
synthesize once more with the turns as source evidence. The turn's date is the day it
was said; a relative phrase inside it resolves against that day. The same second reading
runs for any question; a when-question keeps its own note.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

_WHEN_RE = re.compile(
    r"^\s*(when|what (day|date|month|year|time)|which (day|month|year)|how long( ago)?|"
    r"how many (days|weeks|months|years)|in (which|what) (month|year))\b",
    re.IGNORECASE,
)
_NOT_RECORDED_RE = re.compile(
    r"\b(memories|memory|records?) (do|does)(n't| not) (explicitly |specifically )?"
    r"(record|mention|state|contain|show|give|specify|say|include|have|list|note|tie|attach|"
    r"provide|pin|name|report)|"
    r"\bno memor(y|ies) (record|mention|of)|\bnot recorded\b|\bnothing (is )?recorded\b|"
    r"\bdo(es)?n't have (a |any )?(memory|record)|"
    # "the exact increase isn't specified", "no cashback amount is recorded",
    # "no specific date is stated", "the difference can't be calculated"
    r"\b(is|are|was|were)n't (explicitly |specifically )?"
    r"(specified|recorded|stated|given|mentioned|tied|attached|available|noted)\b|"
    r"\b(is|are|was|were) not (explicitly |specifically )?"
    r"(specified|recorded|stated|given|mentioned|tied|attached|noted)\b|"
    r"\bno (exact |specific |explicit |precise |actual )?[a-z][a-z ]{0,40}?"
    r"(is|was|are|were) (recorded|stated|given|mentioned|specified|attached|tied)\b|"
    r"\bcan(no|')t be (computed|calculated|determined|given|derived)\b|"
    r"\bno [a-z]+(?: [a-z]+){0,3} (stated|recorded|given|specified|mentioned)\b|"
    r"\bnot (an )?explicit(ly)?\b",
    re.IGNORECASE,
)
_WH_LEAD_RE = re.compile(
    r"^\s*(when|what (day|date|month|year|time)|which (day|month|year)|how long( ago)?|"
    r"how many (days|weeks|months|years)|in (which|what) (month|year))\s+"
    r"(did|does|do|is|was|were|has|have|had|will|would|passed|between)?\s*",
    re.IGNORECASE,
)
_STOP = frozenset(
    "the a an of to in on at for with and or her his their she he they it its is was were be been "
    "did do does have has had get got go went start started take took make made first last".split()
)


def is_when_question(question: str) -> bool:
    return bool(_WHEN_RE.search(question or ""))


def looks_not_recorded(answer: str) -> bool:
    return bool(_NOT_RECORDED_RE.search(answer or ""))


def event_phrase(question: str) -> str:
    """The question minus its interrogative head: 'When did Deborah start the
    yoga class?' -> 'Deborah start the yoga class'."""
    q = _WH_LEAD_RE.sub("", question or "", count=1)
    return q.strip().rstrip("?.!").strip()


def phrase_tokens(question: str, limit: int = 6) -> list[str]:
    """Content words of the event phrase, for a match='any' recall."""
    words = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", event_phrase(question))
    out: list[str] = []
    for w in words:
        lw = w.lower()
        if lw in _STOP or lw in out:
            continue
        out.append(lw)
    return out[:limit]


@dataclass(frozen=True)
class DatedTurn:
    day: str  # YYYY-MM-DD or "" when the turn carries no date
    role: str
    content: str


def render_turns(turns: list[DatedTurn], *, max_chars: int = 320) -> str:
    lines = []
    for t in turns:
        body = " ".join((t.content or "").split())
        if len(body) > max_chars:
            body = body[: max_chars - 1] + "…"
        stamp = t.day or "date unknown"
        lines.append(f"[turn @ {stamp}] {t.role or 'user'}: {body}")
    return "\n".join(lines)


WHEN_EVIDENCE_NOTE = (
    " The raw conversation turns appended after the memories are dated with the "
    "day each was SAID. For a when-question, a turn that states the event is the "
    "evidence: resolve any relative phrase in it ('yesterday', 'last Friday', "
    "'two weeks ago', 'last year') against that turn's date and answer with the "
    "resolved date, saying which turn's date you used; when the turn gives no "
    "phrase, the event is 'as of' the turn's date. Do not say the memories do not "
    "record an event that a turn states."
)


NOT_RECORDED_NOTE = (
    " The raw conversation turns appended after the memories are the conversation "
    "itself, dated with the day each was SAID. The first reading said the memories do "
    "not record what the question asks; a turn that states it — an amount, a name, a "
    "date, a count, a starting value — is the evidence: answer from that turn and cite "
    "it as [turn @ date]. When one turn gives a rate or a total and another the base "
    "or the current value, compute the asked figure from them and show the arithmetic. "
    "Say the memories do not record it only when no turn states it either."
)


_ADVICE_REQUEST_RE = re.compile(
    r"(?:\bany (?:tips|advice|suggestions?|recommendations?|ideas|thoughts)\b"
    r"|\bcan you (?:suggest|recommend|help me (?:choose|decide|plan|pick|figure))\b"
    r"|\b(?:could|would) you (?:suggest|recommend)\b"
    r"|\bwhat do you think\b|\bdo you think\b|\bshould i\b"
    r"|\bhow (?:do|can|should|would) i\b|\bhelp me (?:find|choose|decide|plan|pick)\b"
    r"|\bwhat (?:should|could|would) (?:i|we)\b|\brecommend(?:ations?)? for\b)",
    re.IGNORECASE,
)


def is_advice_request(question: str) -> bool:
    """A request for suggestions or an opinion ("any tips for my phone battery?",
    "can you suggest a hotel for Miami?", "what do you think?"), as opposed to
    a question about a recorded fact. Pure."""
    return bool(_ADVICE_REQUEST_RE.search(question or ""))


ADVICE_NOTE = (
    " ADVICE REQUEST: the question asks for suggestions or an opinion, not for a "
    "recorded fact, so the request itself need not appear in the memories and its "
    "absence is not the answer. Answer the request directly with concrete "
    "suggestions, built on what the memories record about the user's situation, "
    "purchases, plans and preferences — name and cite those rows so the advice is "
    "visibly theirs (the power bank they bought, the storage trouble they described, "
    "the hot-tub balcony they chose last time). Do not open with, and do not "
    "include, a sentence about what the memories do not record. Raw conversation "
    "turns appended after the memories, if any, are dated context to draw on."
)


async def window_turns(
    *,
    org: str,
    namespace: str,
    agent: str,
    user_id: str | None,
    question: str,
    bounds: tuple[str, str],
    top_k: int = 24,
) -> list[DatedTurn]:
    """The user's turns dated inside `bounds`, ranked by the question's words
    (match any) and read whole — the window recall on its own, for a first
    reading that wants the window's asides."""
    from datetime import datetime as _dt

    tokens = phrase_tokens(question)
    kwargs: dict[str, Any] = {
        "org": org,
        "namespace": namespace,
        "agent": agent or "*",
        "max_chars": 1500,
        "query": " ".join(tokens) if tokens else "",
        "match": "any",
        "role": "user",
        "order": "relevance" if tokens else "recent",
        "since": _dt.fromisoformat(bounds[0]),
        "until": _dt.fromisoformat(bounds[1] + "T23:59:59"),
        "top_k": max(1, int(top_k)),
    }
    if user_id:
        kwargs["user_id"] = user_id
    return await _recall_turns(kwargs)


ORDER_TURNS_NOTE = (
    " The raw conversation turns appended after the memories are the user's own turns "
    "from the time the question names, dated with the day each was SAID. An event a turn "
    "states that no memory lists is still a member of the set the question asks to "
    "order: include it, dated by its turn, cited as [turn @ date], in its place in the "
    "order. A plan, suggestion or itinerary the assistant wrote is not an event the user "
    "took part in."
)


async def _recall_turns(kwargs: dict[str, Any]) -> list[DatedTurn]:
    """One memory.recall call rendered as dated turns; fail-open to none."""
    try:
        from server.memory.api.models_temporal import MemoryRecallInput
        from server.memory.app.recall import handle_memory_recall

        out = await handle_memory_recall(MemoryRecallInput(**kwargs))
    except Exception as e:  # noqa: BLE001 — the first answer stands when recall fails
        log.warning("ask when-evidence recall failed (open): %s", e)
        return []
    turns: list[DatedTurn] = []
    for t in getattr(out, "turns", None) or []:
        content = str(getattr(t, "content", "") or "").strip()
        if not content:
            continue
        observed = str(getattr(t, "observed_at", "") or "")
        turns.append(
            DatedTurn(day=observed[:10], role=str(getattr(t, "role", "") or ""), content=content)
        )
    return turns


_CUE_WINDOW_MAX_DAYS = 45


_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        )
    )
}
_IN_MONTH_RE = re.compile(
    r"\b(?:in|during|for|of) (january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\b",
    re.IGNORECASE,
)
_PAST_N_RE = re.compile(
    r"\b(?:in|over|during|within|for) the (?:past|last|previous) "
    r"(a|an|one|two|three|four|five|six|\d{1,2})?\s*(week|month)s?\b",
    re.IGNORECASE,
)
_SMALL = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def period_bounds(question: str, obs: Any) -> tuple[Any, Any] | None:
    """The period a question names when the temporal-window resolver has no
    answer: "in January" (that month of the observation year, or the year
    before when it lies ahead), "the past two months" / "last three weeks"
    (that span ending on the observation day). None otherwise."""
    from datetime import date as _date
    from datetime import timedelta

    q = question or ""
    m = _IN_MONTH_RE.search(q)
    if m:
        mo = _MONTHS[m.group(1).lower()]
        year = obs.year if mo <= obs.month else obs.year - 1
        start = _date(year, mo, 1)
        end = _date(year + (mo == 12), (mo % 12) + 1, 1) - timedelta(days=1)
        return start, end
    m = _PAST_N_RE.search(q)
    if m:
        tok = (m.group(1) or "one").lower()
        n = _SMALL.get(tok) or (int(tok) if tok.isdigit() else 1)
        days = n * (7 if m.group(2).lower() == "week" else 30)
        return obs - timedelta(days=days), obs
    return None


def cue_window_bounds(
    question: str, observation_date: Any, *, max_days: int = _CUE_WINDOW_MAX_DAYS
) -> tuple[str, str] | None:
    """(since, until) ISO days for the question's time cue when it bounds the
    question to at most 45 days (the temporal-window resolver), widened by a day
    before and two after so a session that reports the event the next day is
    inside; None when the question names no such window."""
    if not observation_date:
        return None
    from datetime import date as _date
    from datetime import timedelta

    from server.memory.domain.question_window import resolve_time_window

    try:
        tw = resolve_time_window(question, str(observation_date)[:10])
        _date.fromisoformat(str(observation_date)[:10])
    except Exception:  # noqa: BLE001 — a parser miss or a bad date means no window
        return None
    if tw is None or not tw.bounded or tw.kind == "habitual":
        obs = _date.fromisoformat(str(observation_date)[:10])
        pb = period_bounds(question, obs)
        if pb is None or (pb[1] - pb[0]).days + 1 > max_days:
            return None
        return (pb[0] - timedelta(days=1)).isoformat(), (pb[1] + timedelta(days=2)).isoformat()
    if tw.days > max_days:
        return None
    s = _date.fromisoformat(tw.start) - timedelta(days=1)
    e = _date.fromisoformat(tw.end) + timedelta(days=2)
    return s.isoformat(), e.isoformat()


async def fetch_dated_turns(
    *,
    org: str,
    namespace: str,
    agent: str,
    user_id: str | None,
    question: str,
    top_k: int,
    observation_date: Any = None,
) -> list[DatedTurn]:
    """Recall the raw turns carrying the event phrase's words (match any,
    ranked by how many they carry). When the question names a short time window, the
    user's turns dated inside that window join the list whatever words they carry — the
    aside the extractor dropped ("I baked a chocolate cake for my friend") shares no token
    with "cooking something for my friend a couple of days ago". Fail-open: any error
    means no turns."""
    from server.settings import settings as _s

    base: dict[str, Any] = {
        "org": org,
        "namespace": namespace,
        "agent": agent or "*",
        "max_chars": 600,
    }
    if user_id:
        base["user_id"] = user_id
    turns: list[DatedTurn] = []
    tokens = phrase_tokens(question)
    if tokens:
        turns = await _recall_turns(
            {
                **base,
                "query": " ".join(tokens),
                "match": "any",
                "order": "relevance",
                "top_k": max(1, int(top_k)),
            }
        )
    bounds = cue_window_bounds(question, observation_date)
    if bounds:
        from datetime import datetime as _dt

        extra = await _recall_turns(
            {
                **base,
                "max_chars": 1500,
                # Inside the window, the question's own words rank the
                # turns (match any): a four-day window on this corpus
                # holds ~200 user turns, and "recent" picks 24 at random.
                "query": " ".join(tokens) if tokens else "",
                "match": "any",
                "role": "user",
                "order": "relevance" if tokens else "recent",
                "since": _dt.fromisoformat(bounds[0]),
                "until": _dt.fromisoformat(bounds[1] + "T23:59:59"),
                "top_k": int(_s.memory_ask_cue_window_turns_top_k),
            }
        )
        seen = {t.content for t in turns}
        added = [t for t in extra if t.content not in seen]
        if added:
            log.info("ask cue-window turns: %d user turn(s) in %s..%s", len(added), *bounds)
            turns = turns + added
    return turns
