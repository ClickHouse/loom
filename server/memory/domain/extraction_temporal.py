"""Deterministic relative-date resolution (temporal-anchor backstop).

resolve_relative_date(text, observation_date) -> "YYYY-MM-DD" | None
Resolves the FIRST closed-class relative qualifier in `text`. Closed-
class only: open-ended phrases ("recently", "a while back") return
None — the backstop never guesses. The contract is shared with
query-side intent parsing, so keep it stable.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta

from server.core.datetimes import parse_iso_dt

_WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_NUMBER_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "a couple": 2,
    "a couple of": 2,
    "couple": 2,
    "couple of": 2,
    "a few": 3,
    "few": 3,
}
_WEEKDAY_ALT = "|".join(_WEEKDAY_INDEX)
# "next week on Sunday" / "the week after next on Wednesday" / "Sunday next week".
_WEEK_WEEKDAY_RE = re.compile(
    rf"\b(?P<which>this|next|the week after next|in two weeks)(?: week)?,?\s+(?:on\s+)?"
    rf"(?P<wd>{_WEEKDAY_ALT})\b"
    rf"|\b(?P<wd2>{_WEEKDAY_ALT})\s+(?:of\s+)?(?P<which2>this|next|the week after next)\s+week\b",
    re.I,
)
_WEEKENDS_AGO_RE = re.compile(
    r"\b(?:about |around |roughly |nearly |almost |over |some )?"
    r"(\d{1,3}|a couple of|a couple|couple of|couple|a few|few"
    r"|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|an|a)"
    r"[ -]weekends?\s+ago\b"
)
_AGO_RE = re.compile(
    r"\b(?:about |around |roughly |nearly |almost |over |some )?"
    r"(\d{1,3}|a couple of|a couple|couple of|couple|a few|few"
    r"|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|an|a)"
    r"[ -](day|week|month|year)s?\s+ago\b"
)

# A bare month name with no day and no explicit 4-digit year ("in
# January", "back in March") resolves to the FIRST of the most recent
# matching month ON OR BEFORE observation_date — exact calendar
# arithmetic, never a year off. Longest-first alternation lets
# "september"/"sept" win over "sep". Deliberately NOT the query-side
# coarse "last month" = obs−30d resolver, which would drift a year here.
_MONTH_NUM: dict[str, int] = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTH_NUM, key=len, reverse=True))
# A month name preceded by a temporal preposition ("in January", "back
# in March", "during October"), NOT followed by a day number or a
# 4-digit year (those are explicit dates the prompt already resolves and
# must be left alone) and NOT preceded by one (a bare year like "2024
# January" likewise carries its own year).
_NAMED_MONTH_RE = re.compile(
    rf"\b(?:in|back in|during|since|last|early|late|mid)[\s-]+({_MONTH_ALT})\b"
    rf"(?!\s*\.?\s*\d)"
)


def _shift_months(dt: datetime, months: int) -> datetime:
    """Calendar-month arithmetic with end-of-month day clamping."""
    total = dt.year * 12 + (dt.month - 1) + months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _previous_weekday(dt: datetime, weekday: int) -> datetime:
    """Most recent `weekday` strictly before dt."""
    delta = (dt.weekday() - weekday) % 7
    return dt - timedelta(days=delta or 7)


def _next_weekday(dt: datetime, weekday: int) -> datetime:
    """Next `weekday` strictly after dt."""
    delta = (weekday - dt.weekday()) % 7
    return dt + timedelta(days=delta or 7)


def _recent_month_first(dt: datetime, month: int) -> datetime:
    """First-of-month for the most recent occurrence of `month` ON OR
    BEFORE dt. obs in May, month=January → this year's January; obs in
    May, month=September → last year's September."""
    year = dt.year if month <= dt.month else dt.year - 1
    return dt.replace(year=year, month=month, day=1)


def resolve_relative_date(text: str, observation_date: str) -> str | None:
    """Deterministically resolve the first closed-class relative date
    qualifier in `text` against `observation_date`. Returns "YYYY-MM-DD"
    or None when no closed-class qualifier is present (or the
    observation date itself is unparseable)."""
    obs = parse_iso_dt(observation_date)
    if obs is None or not text:
        return None
    t = str(text).lower()

    def _iso(dt: datetime) -> str:
        return f"{dt:%Y-%m-%d}"

    # Order matters: more specific phrases shadow their substrings
    # ("day before yesterday" before "yesterday", "last weekend" before
    # the bare weekday forms).
    if re.search(r"\b(?:the )?day before yesterday\b", t):
        return _iso(obs - timedelta(days=2))
    if re.search(r"\byesterday\b", t) or re.search(r"\blast night\b", t):
        return _iso(obs - timedelta(days=1))
    if re.search(r"\b(?:earlier )?today\b|\btonight\b|\bthis (?:morning|afternoon|evening)\b", t):
        return _iso(obs)
    if re.search(r"\btomorrow\b", t):
        return _iso(obs + timedelta(days=1))
    if re.search(r"\bthe weekend before last\b", t):
        return _iso(_previous_weekday(obs, _WEEKDAY_INDEX["saturday"]) - timedelta(days=7))
    if re.search(r"\bthe week before last\b", t):
        return _iso(obs - timedelta(days=14))
    # "two weekends ago", "a couple of weekends ago": N Saturdays back.
    m = _WEEKENDS_AGO_RE.search(t)
    if m:
        qty_raw = m.group(1)
        qty = int(qty_raw) if qty_raw.isdigit() else _NUMBER_WORDS.get(qty_raw, 0)
        if qty > 0:
            sat = _previous_weekday(obs, _WEEKDAY_INDEX["saturday"])
            return _iso(sat - timedelta(days=7 * (qty - 1)))
    if re.search(r"\b(?:last|this past) weekend\b", t):
        return _iso(_previous_weekday(obs, _WEEKDAY_INDEX["saturday"]))
    # "this weekend" / "over the weekend" / "on the weekend" in a past-tense
    # report: the most recent Saturday ON OR BEFORE the observation date.
    if re.search(r"\b(?:this|over the|on the|at the) weekend\b", t):
        if obs.weekday() == 5:
            return _iso(obs)
        if obs.weekday() == 6:
            return _iso(obs - timedelta(days=1))
        return _iso(_previous_weekday(obs, _WEEKDAY_INDEX["saturday"]))
    m = re.search(rf"\b(?:last|this past) ({_WEEKDAY_ALT})\b", t)
    if m:
        return _iso(_previous_weekday(obs, _WEEKDAY_INDEX[m.group(1)]))
    m = re.search(rf"\bnext ({_WEEKDAY_ALT})\b", t)
    if m:
        return _iso(_next_weekday(obs, _WEEKDAY_INDEX[m.group(1)]))
    # A weekday inside a named week: "next week on Sunday", "Sunday next
    # week", "the week after next on Wednesday", "this week on Friday".
    # Weeks run Monday–Sunday; the named week's Monday plus the weekday.
    m = _WEEK_WEEKDAY_RE.search(t)
    if m:
        which = re.sub(r"\s+", " ", (m.group("which") or m.group("which2") or "").lower()).strip()
        wd = (m.group("wd") or m.group("wd2") or "").lower()
        offset = {"this": 0, "next": 7, "the week after next": 14, "in two weeks": 14}.get(which)
        if wd in _WEEKDAY_INDEX and offset is not None:
            monday = obs - timedelta(days=obs.weekday())
            return _iso(monday + timedelta(days=offset + _WEEKDAY_INDEX[wd]))
    if re.search(r"\bthe week after next\b|\bin two weeks\b", t):
        return _iso(obs + timedelta(days=14))
    m = _AGO_RE.search(t)
    if m:
        qty_raw, unit = m.group(1), m.group(2)
        qty = int(qty_raw) if qty_raw.isdigit() else _NUMBER_WORDS.get(qty_raw, 0)
        if qty > 0:
            if unit == "day":
                return _iso(obs - timedelta(days=qty))
            if unit == "week":
                return _iso(obs - timedelta(weeks=qty))
            if unit == "month":
                return _iso(_shift_months(obs, -qty))
            return _iso(_shift_months(obs, -12 * qty))
    if re.search(r"\blast week\b", t):
        return _iso(obs - timedelta(days=7))
    if re.search(r"\blast month\b", t):
        return _iso(_shift_months(obs, -1))
    if re.search(r"\blast year\b", t):
        return _iso(_shift_months(obs, -12))
    if re.search(r"\bnext week\b", t):
        return _iso(obs + timedelta(days=7))
    if re.search(r"\bnext month\b", t):
        return _iso(_shift_months(obs, 1))
    if re.search(r"\bnext year\b", t):
        return _iso(_shift_months(obs, 12))
    # Lowest priority: a bare named month ("in January", "back in
    # March"). Month precision (first of month), most recent matching
    # month on or before observation_date. Checked last so the more
    # specific closed-class forms above always win.
    m = _NAMED_MONTH_RE.search(t)
    if m:
        month = _MONTH_NUM.get(m.group(1))
        if month is not None:
            return _iso(_recent_month_first(obs, month))
    return None


# ============================================================ Grounding the stored text
# ============================================================ The extractor is asked to
# write the resolved date INTO the value and keep the phrase ("on 2024-03-16 (last
# Saturday)"), and small models do not do it reliably: "attended a fancy gala in Boston
# yesterday" stored with the correct anchor 2023-11-16 reads back as 11-15 — the reader
# re-applies "yesterday" to the anchor. Most wrong-date answers had retrieved such a
# memory. The anchor is already computed; this makes the text say it, deterministically,
# after the fact.

_POINT_PHRASE_RE = re.compile(
    r"\b(?:(?:the )?day before yesterday|yesterday|last night|earlier today|today|tonight|"
    r"this (?:morning|afternoon|evening)|tomorrow|"
    r"(?:last|this past|next) (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten) days? ago)\b",
    re.IGNORECASE,
)
_SPAN_PHRASE_RE = re.compile(
    r"\b(?:(?:last|this past|next) (?:week|weekend|month|year)|"
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten) "
    r"(?:weeks?|weekends?|months?|years?) ago)\b",
    re.IGNORECASE,
)
# Vague offsets: real events, but the phrase carries no precision the
# anchor can honestly claim. "A few years ago" stored as 2020-01-23 made the
# reader answer "a few years before 2020".
_VAGUE_PHRASE_RE = re.compile(
    r"\b(?:(?:a few|several|a couple of|couple of|some|many) "
    r"(?:days|weeks|months|years) (?:ago|back)|"
    r"(?:days|weeks|months|years) ago|recently|a while (?:ago|back)|some time ago|"
    r"not (?:too )?long ago|the other day)\b",
    re.IGNORECASE,
)
_ISO_IN_TEXT_RE = re.compile(r"\b20\d\d-\d\d(?:-\d\d)?\b")
# The vague offsets worth keeping as a note when the value lost them — the ones that
# place an event weeks or more before the day it was said. "Recently" and "the other
# day" say little more than the session day does, and on a sample they attached to
# present-state values ("has been cooking meals for her mom") as often as to the event
# they described.
_VAGUE_OFFSET_RE = re.compile(
    r"\b(?:(?:a few|several|a couple of|couple of|some|many) "
    r"(?:weeks|months|years) (?:ago|back)|a while (?:ago|back)|some time ago|"
    r"not (?:too )?long ago)\b",
    re.IGNORECASE,
)
_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_MONTH_DAY_RE = re.compile(
    rf"\b{_MONTHS}\.? \d{{1,2}}(?:st|nd|rd|th)?\b|\b\d{{1,2}}(?:st|nd|rd|th)? (?:of )?{_MONTHS}\b",
    re.IGNORECASE,
)
# The value must tell of a past event for the offset to belong to it: on
# the same sample "is scheduled to meet Sarah soon" and "has been
# experimenting with flavors lately" sat next to "a few weeks ago" in
# their sentence and would have carried it.
_PAST_EVENT_RE = re.compile(
    r"\b(?:previously|earlier|once|used to|went|met|had|did|was|were|got|bought|replaced|"
    r"took|visited|attended|made|moved|started|finished|completed|received|volunteered|"
    r"travell?ed|saw|ran|played|read|wrote|built|joined|left|lost|won|sold|learned|tried|"
    r"noticed|celebrated|adopted|hosted|threw|cooked|baked|planted|painted|fixed|installed|"
    r"signed|graduated|retired|quit|broke|found|ordered|worked|lived|stayed|spent|passed|"
    r"caught|came|gave|told|asked|bought|paid|rented|hiked|climbed|swam|drove|flew|sang|"
    r"performed|presented|published|launched|opened|closed|ended|began|discovered|hurt|"
    r"injured|recovered|underwent|removed|switched|upgraded|cancelled|canceled)\b",
    re.IGNORECASE,
)


def named_week_weekday(text: str) -> int | None:
    """The weekday index a NAMED-WEEK phrase names ("next week on Sunday",
    "the week after next on Wednesday" → 6, 2); None for bare "last Friday"
    forms, whose disagreements are handled separately."""
    m = _WEEK_WEEKDAY_RE.search((text or "").lower())
    if not m:
        return None
    return _WEEKDAY_INDEX.get((m.group("wd") or m.group("wd2") or "").lower())


def relative_phrase_class(text: str) -> str:
    """'point' (day-precise), 'span' (week/month/year window), 'vague'
    (unquantified offset) or '' when the text carries no relative phrase.
    Point beats span beats vague when several are present."""
    t = text or ""
    if _POINT_PHRASE_RE.search(t):
        return "point"
    if _SPAN_PHRASE_RE.search(t):
        return "span"
    if _VAGUE_PHRASE_RE.search(t):
        return "vague"
    return ""


_SPAN_UNIT_RE = re.compile(
    r"\b(last|this past|next) (week|weekend|month|year)\b|"
    r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten) "
    r"(weeks?|weekends?|months?|years?) ago\b",
    re.IGNORECASE,
)
_DURATION_NUMBER_WORDS: dict[str, float] = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
    "a couple of": 2,
    "couple of": 2,
    "half a": 0.5,
}
_DURATION_RE = re.compile(
    r"\b(?:for|since)\s+(?:the (?:past|last)\s+)?"
    r"(?:about|almost|nearly|over|around|roughly|more than)?\s*"
    r"(\d+|a couple of|couple of|half a|an?|one|two|three|four|five|six|seven|eight|nine|ten"
    r"|eleven|twelve|fifteen|twenty)"
    r"\s+(years?|months?|weeks?|decades?)\b(?!\s+ago)",
    re.IGNORECASE,
)
_YEAR_IN_TEXT_RE = re.compile(r"\b(?:19|20)\d\d\b")


def duration_note(text: str, session_iso: str) -> str | None:
    """A duration said as of the session date, turned into the start it
    implies: "I've had them for 3 years now" said on 2022-01-21 → "since about 2019, as
    of 2022-01-21"; "for about four months" said 2023-12-06 → "since about 2023-08, as
    of 2023-12-06"; weeks → a day. Precision follows the unit. None without a duration
    phrase, when the text already names a year, when the session date is not a day, or
    for "for the next N years" (a plan, not a history)."""
    day = str(session_iso or "")[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day) or _YEAR_IN_TEXT_RE.search(text or ""):
        return None
    m = _DURATION_RE.search(text or "")
    if not m or re.search(r"\bnext\s+" + re.escape(m.group(1)), text or "", re.IGNORECASE):
        return None
    qty = _DURATION_NUMBER_WORDS.get(m.group(1).lower())
    if qty is None:
        try:
            qty = int(m.group(1))
        except ValueError:
            return None
    unit = m.group(2).lower().rstrip("s")
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return None
    if unit == "decade":
        qty, unit = qty * 10, "year"
    if unit == "year":
        if qty < 1:
            return f"since about {_month_start_shift(d, -6):%Y-%m}, as of {day}"
        return f"since about {d.year - int(qty)}, as of {day}"
    if unit == "month":
        return f"since about {_month_start_shift(d, -int(round(qty))):%Y-%m}, as of {day}"
    return f"since about {d - timedelta(days=int(round(qty * 7))):%Y-%m-%d}, as of {day}"


def _month_start_shift(d: date, months: int) -> date:
    """First day of the month `months` away from `d` (month precision)."""
    y, m0 = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m0 + 1, 1)


def span_note(text: str, session_iso: str) -> str | None:
    """How a span phrase reads against the SESSION date, in the shape the
    question's gold uses: "last week" said on 2023-06-09 is "the week before
    2023-06-09"; "two weeks ago" is "about two weeks before 2023-06-09"; "next month" is
    "the month after 2023-06-09". A note anchored on the RESOLVED day ("around
    2023-06-02") reads as one more relative step to a reader, which then answers "the
    week before 2023-06-02", a week early. None when the text carries no such phrase or
    the session date is not a day."""
    day = str(session_iso or "")[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        return None
    m = _SPAN_UNIT_RE.search(text or "")
    if not m:
        return None
    if m.group(1):
        unit = m.group(2).lower()
        side = "after" if m.group(1).lower() == "next" else "before"
        return f"the {unit} {side} {day}"
    qty, unit = m.group(3).lower(), m.group(4).lower().rstrip("s")
    plural = "" if qty in ("1", "one") else "s"
    return f"about {qty} {unit}{plural} before {day}"


def ground_relative_phrase(text: str, anchor_iso: str, observation_iso: str = "") -> str:
    """Append the resolved date to a value that still carries a relative
    phrase and no ISO date: point phrases get "(on YYYY-MM-DD)", spans
    "(the week before <session date>)" when the session date is known and
    "(around YYYY-MM-DD)" otherwise, vague offsets a precision matching the unit
    ("around 2020" for years, "around 2020-01" for months). Unchanged when
    there is no phrase, no anchor, or the text already names an ISO date.
    The closing quote/period of a JSON-ish value is preserved."""
    if not text or not anchor_iso:
        return text
    cls = relative_phrase_class(text)
    if not cls or _ISO_IN_TEXT_RE.search(text):
        return text
    day = str(anchor_iso)[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        return text
    if cls == "point":
        note = f"on {day}"
    elif cls == "span":
        note = span_note(text, observation_iso) or f"around {day}"
    else:
        low = text.lower()
        if "year" in low:
            note = f"around {day[:4]}"
        elif "month" in low or "while" in low or "some time" in low or "long ago" in low:
            note = f"around {day[:7]}"
        else:
            note = f"around {day}"
    return append_note(text, note)


def append_note(text: str, note: str) -> str:
    """`text` with " (note)" appended, keeping a closing quote and/or period
    of a JSON-ish value outside the note."""
    body = text.rstrip()
    tail = ""
    while body and body[-1] in "\"'.":
        tail = body[-1] + tail
        body = body[:-1]
    if tail:
        quote = "".join(ch for ch in tail if ch in "\"'")
        period = "." if "." in tail else ""
        return f"{body} ({note}){period}{quote}"
    return f"{body} ({note})"


# ============================================================ A phrase the extractor
# dropped ============================================================ "Was super
# nervous last week when I shared my book with my writers group" came out as "Joanna
# shared her book with her writers group and received great feedback." — no phrase, no
# date, and the prompt's default anchored it to the day it was said. Nothing above can
# act on a value that carries no phrase; the phrase is still in the cited turn. About 2%
# of fact rows are undated and phrase-less while the sentence they came from carries a
# past point/span phrase, and every temporal question about them is answered with the
# session date.

_PRESENT_OR_FUTURE_RE = re.compile(
    r"\b(?:today|tonight|tomorrow|earlier today|this (?:morning|afternoon|evening)|"
    r"next (?:week|weekend|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"this (?:week|weekend|month|year)|the week after next|in two weeks|upcoming)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_TOKEN_RE = re.compile(r"[a-z]{3,}")
_OVERLAP_STOP = frozenset(
    "the and for with that this from his her their its was were been have has had got get about "
    "just really very some they them she him you your our who what when which into onto".split()
)


def _overlap_tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _OVERLAP_STOP}


def past_relative_phrase(text: str) -> str | None:
    """The past point/span phrase `text` carries ("last week", "yesterday",
    "two weekends ago"), or None. Present and future forms ("today",
    "tomorrow", "next month", "this weekend") are not phrases to recover: for
    those the row's date may honestly be the day the sentence was said."""
    if not text or _PRESENT_OR_FUTURE_RE.search(text):
        return None
    for rx in (_POINT_PHRASE_RE, _SPAN_PHRASE_RE, _WEEKENDS_AGO_RE, _AGO_RE):
        m = rx.search(text)
        if m:
            return m.group(0)
    return None


def _best_cited_sentence(
    value: str, cited: str, min_shared: int, min_share: float, *, allow_iso: str = ""
) -> str | None:
    """The cited turn's sentence that best matches `value` by content-word
    overlap — at least `min_shared` words in common and at least `min_share`
    of the value's own — so a general statement ("John appreciates the
    beauty of nature") does not borrow the date of a sentence that merely
    mentions its subject. None for a value that already names an ISO date
    (other than `allow_iso`) or its own phrase, or when no sentence matches
    well enough."""
    if not value or not cited:
        return None
    if {d for d in _ISO_IN_TEXT_RE.findall(value)} - {allow_iso}:
        return None
    if relative_phrase_class(value) or past_relative_phrase(value):
        return None
    vt = _overlap_tokens(value)
    if not vt:
        return None
    best: tuple[int, str] | None = None
    for sent in _SENTENCE_SPLIT_RE.split(cited):
        shared = len(vt & _overlap_tokens(sent))
        if shared and (best is None or shared > best[0]):
            best = (shared, sent)
    if best is None or best[0] < min_shared or best[0] / len(vt) < min_share:
        return None
    return best[1]


def dropped_phrase_sentence(
    value: str, cited: str, *, min_shared: int = 3, min_share: float = 0.4
) -> tuple[str, str] | None:
    """(sentence, phrase) when the cited turn's sentence that best matches
    `value` carries a past relative phrase the value lost; None otherwise."""
    sent = _best_cited_sentence(value, cited, min_shared, min_share)
    if sent is None:
        return None
    phrase = past_relative_phrase(sent)
    return (sent, phrase) if phrase else None


def dropped_vague_phrase_sentence(
    value: str, cited: str, *, min_shared: int = 3, min_share: float = 0.4, said_on: str = ""
) -> tuple[str, str] | None:
    """(sentence, phrase) when the best-matching cited
    sentence carries a VAGUE past offset ("a few months ago", "a while back") that the
    value lost and no concrete one. The phrase cannot anchor a date — that is why
    `dropped_phrase_sentence` leaves it alone — but it is the only ordering evidence the
    row has: "met Tom at a charity event" from "a few months ago, I … met Tom" read as
    the session day and lost "who did I meet first" to a friend anchored a month
    earlier.

    A value that names the SESSION day (`said_on`) as the event's date is
    the same loss in a worse form — "replaced spark plugs … on 2023-03-15"
    from "a while back … after replacing the spark plugs" said on 03-15 —
    and is a candidate too; any other ISO date is not."""
    if _MONTH_DAY_RE.search(value or "") or not _PAST_EVENT_RE.search(value or ""):
        return None  # "on March 5th" already places the event; a state is not an event
    sent = _best_cited_sentence(value, cited, min_shared, min_share, allow_iso=said_on)
    if sent is None or _PRESENT_OR_FUTURE_RE.search(sent):
        return None
    m = _VAGUE_OFFSET_RE.search(sent)
    if m is None:
        return None
    # A concrete phrase in the same sentence is `dropped_phrase_sentence`'s; the
    # vague note is only for a sentence that has nothing better.
    if any(rx.search(sent) for rx in (_POINT_PHRASE_RE, _SPAN_PHRASE_RE, _WEEKENDS_AGO_RE)):
        return None
    return (sent, m.group(0))


_ON_DATE_NOTE_RE = re.compile(r"\s*\((?:on|around) (20\d\d-\d\d-\d\d)\)")
_SPAN_NOTE_RE = re.compile(
    r"\s*\((the (?:week|weekend|month|year) (?:before|after) 20\d\d-\d\d-\d\d"
    r"|about [\w ]+? before 20\d\d-\d\d-\d\d)\)"
)


def resolve_relative_phrase(text: str, anchor_iso: str, session_iso: str = "") -> str:
    """Rewrite a value's relative day phrase INTO the resolved date at read
    time: "made a plate in pottery class yesterday" with an event anchor of 2023-08-24
    reads "made a plate in pottery class on 2023-08-24". Point phrases become "on
    <date>", span phrases "around <date>"; a "(on <same date>)" note
    `ground_relative_phrase` appended is dropped so the date is stated once. Vague
    offsets are left alone (their anchor is honest only to the month or year). Unchanged
    when there is no anchor.

    Why at read time: the anchor already holds the resolved date, but a
    reader that sees the word "yesterday" next to it resolves it again —
    near-miss temporal answers come out EARLY by the phrase's offset — and
    stored rows keep the phrase (a write-time note only annotates it)."""
    if not text or not anchor_iso:
        return text
    day = str(anchor_iso)[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        return text
    out = text
    if _POINT_PHRASE_RE.search(out):
        out = _POINT_PHRASE_RE.sub(f"on {day}", out, count=1)
    elif _SPAN_PHRASE_RE.search(out):
        # A write-time note already says what the phrase means against the
        # session date: "last week (the week before 2022-01-21)" → "the week
        # before 2022-01-21". Without one, the caller's session date; without
        # that, "around <anchor>". A stored row's valid_at is the ANCHOR for
        # anchored facts, not the session date, so it must not stand in for
        # the session date here.
        m2 = _SPAN_NOTE_RE.search(out)
        rep_ = m2.group(1) if m2 else (span_note(out, session_iso) or f"around {day}")
        out = _SPAN_PHRASE_RE.sub(rep_, out, count=1)
        m2 = _SPAN_NOTE_RE.search(out)
        if m2 and m2.group(1) == rep_:
            out = out[: m2.start()] + out[m2.end() :]
    else:
        return text
    # "… on 2023-08-24 … (on 2023-08-24)" → drop the now-redundant note.
    m = _ON_DATE_NOTE_RE.search(out)
    if m and m.group(1) == day:
        out = out[: m.start()] + out[m.end() :]
    return out
