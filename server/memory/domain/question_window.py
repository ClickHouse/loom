"""A question predicts a time window, not an anchor day.

The cheapest half of temporal reasoning over a long history is deciding *when*
the question is about and using that to shape the candidate pool: predict the
question's target window, drop sessions outside it, and let a soft logistic
penalty on the day-gap to the window (a 7-day grace, then decay) do the rest.
The soft form is what generalizes; a hard cut helps simple lookups and breaks
multi-step reasoning.

Loom already resolves relative phrases to *point* dates
(`query_intent.resolve_relative_dates`) and boosts rows within ±7 days of
the first one. This module produces the window instead: a start and an end,
each possibly open, by these rules:

- an explicit date is the full day; a month-year is the month; a bare year
  is the year;
- "yesterday" / "today" / "last <weekday>" are one day;
- "last week" is the seven days ending the day before the observation
  date; "last month" / "last year" are the previous calendar month / year;
- "N days|weeks ago" is that day; "N months|years ago" is that month / year;
- "between A and B" / "from A to B" is [A, B]; "before A" is open-start to
  A; "after / since A" is A to the observation date;
- a past-tense question with no marker is open-start to the observation
  date; a future-tense one is the observation date to open-end; a habitual
  one ("usually", "every week") is open on both sides.

memory.search reports the window in `retrieval_work` so a reader or a bench
can see the classification; its ranking keeps the point anchor (the window
boost measured no gain). Deterministic, no model call.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

from server.memory.domain.query_intent import (
    _AGO_RE,
    _ISO_DATE_RE,
    _LAST_PERIOD_RE,
    _LAST_WEEKDAY_RE,
    _MDY_RE,
    _MY_RE,
    _WEEKDAYS,
    _WORD_NUMBERS,
    _month_num,
    _parse_obs,
)

_YEAR_RE = re.compile(r"\b(?:in|during|of|since|before|after|until)\s+((?:19|20)\d{2})\b")
_BETWEEN_RE = re.compile(r"\bbetween\s+(.+?)\s+and\s+(.+?)(?:[,?.]|$)", re.IGNORECASE)
_FROM_TO_RE = re.compile(
    r"\bfrom\s+(.+?)\s+(?:to|until|till|through)\s+(.+?)(?:[,?.]|$)", re.IGNORECASE
)
_BEFORE_RE = re.compile(r"\b(?:before|prior to|until|up to)\s+(.+?)(?:[,?.]|$)", re.IGNORECASE)
_AFTER_RE = re.compile(r"\b(?:after|since|starting|from)\s+(.+?)(?:[,?.]|$)", re.IGNORECASE)
_PAST_TENSE_RE = re.compile(
    r"\b(did|was|were|had|went|has been|have been|used to)\b", re.IGNORECASE
)
_FUTURE_RE = re.compile(
    r"\b(will|going to|plan(?:s|ning)? to|next (?:week|month|year)|tomorrow|upcoming)\b",
    re.IGNORECASE,
)
_HABITUAL_RE = re.compile(
    r"\b(usually|typically|often|always|every (?:day|week|month|year|morning|weekend)"
    r"|habit|routine|regularly)\b",
    re.IGNORECASE,
)
_RELATIVE_DAY_RES = (
    re.compile(r"\bday before yesterday\b"),
    re.compile(r"\byesterday\b|\blast night\b"),
    re.compile(r"\btoday\b|\btonight\b"),
)


@dataclass(frozen=True)
class TimeWindow:
    start: str = ""  # ISO date, "" = open
    end: str = ""  # ISO date, "" = open
    kind: str = ""  # day | month | year | span | open_past | open_future | habitual
    phrase: str = ""  # the text that decided it

    @property
    def bounded(self) -> bool:
        return bool(self.start and self.end)

    @property
    def days(self) -> int:
        if not self.bounded:
            return 0
        return (date.fromisoformat(self.end) - date.fromisoformat(self.start)).days + 1


def _month_span(y: int, m: int) -> tuple[date, date]:
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def _resolve_point_or_span(text: str, obs: date | None) -> tuple[date, date, str] | None:
    """One date expression → (start, end, kind). Understands the resolver's
    forms plus month-year and bare-year spans."""
    t = text.strip().lower().strip(" ,.?!'\"")
    m = _ISO_DATE_RE.search(t)
    if m:
        try:
            d = date(int(m[1]), int(m[2]), int(m[3]))
            return d, d, "day"
        except ValueError:
            return None
    m = _MDY_RE.search(t)
    if m and _month_num(m[1]):
        mo = _month_num(m[1]) or 1
        if m[3]:
            try:
                d = date(int(m[3]), mo, int(m[2]))
                return d, d, "day"
            except ValueError:
                return None
        if obs is not None:
            try:
                d = date(obs.year, mo, int(m[2]))
            except ValueError:
                return None
            if d > obs:
                d = date(obs.year - 1, mo, int(m[2]))
            return d, d, "day"
        return None
    m = _MY_RE.search(t)
    if m and _month_num(m[1]):
        s, e = _month_span(int(m[2]), _month_num(m[1]) or 1)
        return s, e, "month"
    m = re.search(r"\b((?:19|20)\d{2})\b", t)
    if m and not _AGO_RE.search(t):
        y = int(m[1])
        return date(y, 1, 1), date(y, 12, 31), "year"
    if obs is None:
        return None
    if re.search(r"\b(today|tonight)\b", t):
        return obs, obs, "day"
    if re.search(r"\bday before yesterday\b", t):
        d = obs - timedelta(days=2)
        return d, d, "day"
    if re.search(r"\byesterday\b|\blast night\b", t):
        d = obs - timedelta(days=1)
        return d, d, "day"
    m = _LAST_WEEKDAY_RE.search(t)
    if m:
        wd = _WEEKDAYS[m[1]]
        d = obs - timedelta(days=(obs.weekday() - wd - 1) % 7 + 1)
        return d, d, "day"
    m = _LAST_PERIOD_RE.search(t)
    if m:
        unit = m[1]
        if unit == "week":
            return obs - timedelta(days=7), obs - timedelta(days=1), "span"
        if unit == "weekend":
            sat = obs - timedelta(days=(obs.weekday() - 5 - 1) % 7 + 1)
            return sat, sat + timedelta(days=1), "span"
        if unit == "month":
            y, mo = (obs.year, obs.month - 1) if obs.month > 1 else (obs.year - 1, 12)
            s, e = _month_span(y, mo)
            return s, e, "month"
        if unit == "year":
            return date(obs.year - 1, 1, 1), date(obs.year - 1, 12, 31), "year"
        if unit == "night":
            d = obs - timedelta(days=1)
            return d, d, "day"
    m = _AGO_RE.search(t)
    if m:
        n_raw = m[1]
        n = int(n_raw) if n_raw.isdigit() else _WORD_NUMBERS.get(n_raw, 0)
        if 0 < n <= 5000:
            unit = m[2]
            if unit == "day":
                d = obs - timedelta(days=n)
                return d, d, "day"
            if unit == "week":
                d = obs - timedelta(days=7 * n)
                return d, d, "day"
            if unit == "month":
                y, mo = obs.year, obs.month - n
                while mo <= 0:
                    mo += 12
                    y -= 1
                s, e = _month_span(y, mo)
                return s, e, "month"
            if unit == "year":
                y = obs.year - n
                return date(y, 1, 1), date(y, 12, 31), "year"
    return None


def resolve_time_window(query: str, observation_date: str | None = None) -> TimeWindow | None:
    """The window the question is about, or None when the text carries no
    temporal signal at all (no date, no tense cue)."""
    q = (query or "").strip()
    if not q:
        return None
    obs = _parse_obs(observation_date)
    ql = q.lower()

    # Two-ended forms first: "between A and B", "from A to B".
    for rx in (_BETWEEN_RE, _FROM_TO_RE):
        m = rx.search(ql)
        if m:
            a = _resolve_point_or_span(m[1], obs)
            b = _resolve_point_or_span(m[2], obs)
            if a and b:
                s, e = min(a[0], b[0]), max(a[1], b[1])
                return TimeWindow(s.isoformat(), e.isoformat(), "span", m[0].strip())
    # One-ended forms: "before A" (open start), "after / since A" (to obs).
    m = _BEFORE_RE.search(ql)
    if m:
        a = _resolve_point_or_span(m[1], obs)
        if a:
            return TimeWindow("", a[0].isoformat(), "span", m[0].strip())
    m = _AFTER_RE.search(ql)
    if m:
        a = _resolve_point_or_span(m[1], obs)
        if a:
            end = obs.isoformat() if obs else ""
            return TimeWindow(a[1].isoformat(), end, "span", m[0].strip())
    # A single expression anywhere in the question.
    for rx in (_ISO_DATE_RE, _MDY_RE, _MY_RE, _LAST_WEEKDAY_RE, _LAST_PERIOD_RE, _AGO_RE):
        m = rx.search(ql)
        if m:
            a = _resolve_point_or_span(m[0], obs)
            if a:
                return TimeWindow(a[0].isoformat(), a[1].isoformat(), a[2], m[0])
    for rx in _RELATIVE_DAY_RES:
        m = rx.search(ql)
        if m:
            a = _resolve_point_or_span(m[0], obs)
            if a:
                return TimeWindow(a[0].isoformat(), a[1].isoformat(), a[2], m[0])
    m = _YEAR_RE.search(ql)
    if m:
        y = int(m[1])
        return TimeWindow(date(y, 1, 1).isoformat(), date(y, 12, 31).isoformat(), "year", m[0])
    # Tense-only classification (open windows).
    obs_iso = obs.isoformat() if obs else ""
    mh = _HABITUAL_RE.search(ql)
    if mh:
        return TimeWindow("", "", "habitual", mh[0])
    mf = _FUTURE_RE.search(ql)
    if mf:
        return TimeWindow(obs_iso, "", "open_future", mf[0])
    mp = _PAST_TENSE_RE.search(ql)
    if mp:
        return TimeWindow("", obs_iso, "open_past", mp[0])
    return None


def window_gap_days(row_date: date, window: TimeWindow) -> int:
    """Days from a row's date to the nearest edge of a bounded window; 0 inside."""
    s, e = date.fromisoformat(window.start), date.fromisoformat(window.end)
    if row_date < s:
        return (s - row_date).days
    if row_date > e:
        return (row_date - e).days
    return 0


def logistic_window_score(gap_days: int, *, grace_days: int = 7, scale_days: int = 3) -> float:
    """The chronological-proximity shape, R_s = c/(1+exp(x)) − d with
    c=1.5, d=0.5, x=(gap−grace)/scale: about 0.87 inside the window (gap 0),
    0.25 at the grace edge, −0.36 one week past it, −0.5 far away. A scale of
    three days (not one) makes the decay a week wide, which is the
    session spacing of the dialogue corpora loom is measured on."""
    import math

    x = (gap_days - grace_days) / max(1, scale_days)
    x = max(-60.0, min(60.0, x))
    return 1.5 / (1.0 + math.exp(x)) - 0.5
