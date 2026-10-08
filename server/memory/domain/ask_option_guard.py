"""The answer named an option it called unoffered — set the letter to it.

A multiple-choice reader (a small model) sometimes writes "D) Surprise — the nearest cue
is Happiness, which is not offered" under an option list that reads "B) Happiness". The
evidence was read right, the option list was not: the letter went to a fallback the
prompt reserves for the case where the emotion is genuinely missing. A prompt rule
cannot make a model re-read a list it has already misread; a deterministic check can.

`reconcile_unoffered_claim` parses the option block from the question, looks
for the claim shape ("<Word> … not offered / not an option / not among the
options / not listed", "no <Word> option") on the answer's first line, and
when the word IS an option's text, rewrites the leading letter to that
option and says so. Nothing else changes; an answer without the claim shape
is returned untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_OPTION_LINE_RE = re.compile(r"(?m)^\s*\(?([A-Ha-h])[\)\.:]\s+(.+?)\s*$")
_LEADING_LETTER_RE = re.compile(r"^\s*\(?([A-Ha-h])[\)\.:]\s*")
# Between the named word and the claim, no other capitalised word may occur:
# the capture is the NEAREST candidate ("Surprise — nearest cue is Happiness
# (not offered)" names Happiness, not Surprise).
_GAP = r"(?:(?![A-Z][a-z]+)[^.;\n]){0,50}?"
_UNOFFERED_RES = (
    re.compile(
        r"\b([A-Z][a-z]+)\b" + _GAP + r"\b(?:is |are |was )?not (?:offered|an option|"
        r"among the options|listed|available)",
    ),
    re.compile(r"\bno ([A-Z][a-z]+) option\b"),
    re.compile(r"\b([A-Z][a-z]+)\b" + _GAP + r"\((?:not offered|not an option|not listed)\)"),
)


def question_stem(question: str) -> str:
    """The line that asks the question. A caller's query may carry an answer
    scaffold around it — "Current date: 2024-10-05" above, the option block and a "reply
    with the letter" instruction below — and a verifier that reads the first line reads
    the date: the comparison verifier would never fire and the subject gate would take
    "Current" for a proper noun."""
    from server.memory.domain.query_scaffold import split_answer_scaffold

    stem, _ = split_answer_scaffold(question or "")
    for line in (stem or question or "").split("\n"):
        if line.strip():
            return line.strip()
    return (question or "").strip()


def parse_options(question: str) -> dict[str, str]:
    """{letter: text} for every option line in the question; {} when fewer
    than two lines parse (a bare letter in prose is not an option list)."""
    out: dict[str, str] = {}
    for m in _OPTION_LINE_RE.finditer(question or ""):
        out[m.group(1).upper()] = m.group(2).strip().rstrip(".")
    return out if len(out) >= 2 else {}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


@dataclass(frozen=True)
class Reconciled:
    text: str
    corrected: bool = False
    from_letter: str = ""
    to_letter: str = ""
    named: str = ""


def reconcile_unoffered_claim(answer: str, question: str) -> Reconciled:
    """Rewrite the leading letter when the first line claims an option is
    unoffered while the option block offers it."""
    options = parse_options(question)
    if not options or not answer:
        return Reconciled(answer)
    first, sep, rest = answer.partition("\n")
    lead = _LEADING_LETTER_RE.match(first)
    if not lead:
        return Reconciled(answer)
    chosen = lead.group(1).upper()
    by_text = {_norm(v): k for k, v in options.items()}
    for rx in _UNOFFERED_RES:
        for m in rx.finditer(first):
            word = m.group(1)
            target = by_text.get(_norm(word))
            if target and target != chosen:
                body = first[lead.end() :].strip()
                # Drop a mis-named leading option ("Surprise — …") so the
                # line reads as the option it now carries.
                lead_opts = "|".join(re.escape(v) for v in options.values() if v)
                body = re.sub(r"^(?:" + lead_opts + r")\s*[—:\-]\s*", "", body)
                new_first = (
                    f"{target}) {options[target]} — {body} "
                    f"(letter set to {target}: the answer named {options[target]}, "
                    f"which the option list offers)"
                )
                return Reconciled(
                    new_first + (sep + rest if sep else ""),
                    corrected=True,
                    from_letter=chosen,
                    to_letter=target,
                    named=options[target],
                )
    return Reconciled(answer)


_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*(am|pm|a\.m\.|p\.m\.)?\b", re.I)
# "2 PM" without minutes; the lookbehind keeps "2:00 PM"'s minutes from reading as "00 PM".
_TIME_LOOSE_RE = re.compile(r"(?<![\d:])\b(\d{1,2})\s*(am|pm)\b", re.I)
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FFFF\u2600-\u27BF]")
_WORD_RE = re.compile(r"[a-z0-9]{4,}")
_STOP_WORDS = {
    "that",
    "this",
    "with",
    "from",
    "have",
    "been",
    "their",
    "they",
    "them",
    "which",
    "what",
    "when",
    "where",
    "there",
    "here",
    "into",
    "about",
    "also",
    "than",
    "then",
    "your",
    "will",
    "would",
    "could",
    "should",
    "very",
    "just",
    "like",
    "more",
    "most",
    "some",
    "such",
    "only",
    "over",
    "after",
    "before",
    "user",
    "memories",
    "memory",
    "option",
    "options",
    "event",
    "events",
    "people",
    "person",
    "does",
    "were",
    "being",
    "make",
    "makes",
    "made",
    "much",
}


def _times_in(text: str) -> set[str]:
    out: set[str] = set()
    for m in _TIME_RE.finditer(text or ""):
        h, mi, ap = int(m.group(1)), m.group(2), (m.group(3) or "").lower().replace(".", "")
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        out.add(f"{h:02d}:{mi}")
    for m in _TIME_LOOSE_RE.finditer(text or ""):
        h, ap = int(m.group(1)), m.group(2).lower()
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        out.add(f"{h:02d}:00")
    return out


_WEEKDAY_RE = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)


_MONTH_NUM = {
    m: i + 1
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    )
}
_MONTH_DAY_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b"
    r"|\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    re.I,
)


def _month_days_in(text: str) -> set[tuple[int, int]]:
    out: set[tuple[int, int]] = set()
    for m in _MONTH_DAY_RE.finditer(text or ""):
        if m.group(1):
            out.add((_MONTH_NUM[m.group(1).lower()], int(m.group(2))))
        else:
            out.add((_MONTH_NUM[m.group(4).lower()], int(m.group(3))))
    return out


def _weekdays_in(text: str) -> set[str]:
    return {m.group(1).lower() for m in _WEEKDAY_RE.finditer(text or "")}


def _loose_times_in(text: str) -> set[str]:
    """ "2 PM" → "14:00" (no minutes written)."""
    out: set[str] = set()
    for m in _TIME_LOOSE_RE.finditer(text or ""):
        h, ap = int(m.group(1)), m.group(2).lower()
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        out.add(f"{h:02d}:00")
    return out


_RELATIVE_PHRASE_RE = re.compile(
    r"^\s*((?:this|next|nextnext|last) week|the week after next|two weeks from now"
    r"|tomorrow|today)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(.+)$",
    re.I,
)


_WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_QUESTION_DATE_RE = re.compile(
    r"(?im)^\s*(?:current date|question date|today|date)\s*:\s*(\d{4}-\d{2}-\d{2})"
)


def question_date_of(question: str) -> str:
    """The ISO date a "Current date: YYYY-MM-DD" scaffold line stamps, "" otherwise."""
    m = _QUESTION_DATE_RE.search(question or "")
    return m.group(1) if m else ""


def resolve_relative_day(phrase: str, weekday: str, question_date: str) -> str:
    """ "next week sunday" from 2024-10-05 → "2024-10-13": weeks run Monday to
    Sunday, 'this week' is the week holding the question date, 'next week'
    the one after, 'nextnext week' / 'the week after next' / 'two weeks from
    now' two after. "" when the phrase or the date is not understood."""
    import datetime as _dt

    try:
        base = _dt.date.fromisoformat(question_date)
    except Exception:  # noqa: BLE001 — no date, no resolution
        return ""
    p = " ".join((phrase or "").lower().split())
    weeks = {
        "this week": 0,
        "next week": 1,
        "nextnext week": 2,
        "the week after next": 2,
        "two weeks from now": 2,
        "last week": -1,
    }
    if p not in weeks or weekday.lower() not in _WEEKDAY_INDEX:
        return ""
    monday = base - _dt.timedelta(days=base.weekday())
    target = monday + _dt.timedelta(weeks=weeks[p], days=_WEEKDAY_INDEX[weekday.lower()])
    return target.isoformat()


def _relative_parts(option: str) -> tuple[str, str, str] | None:
    """("next week", "sunday", "14:00") for "next week Sunday 2:00 PM"; None
    when the option is not shaped like that."""
    m = _RELATIVE_PHRASE_RE.match(option or "")
    if not m:
        return None
    times = _times_in(m.group(3)) | _loose_times_in(m.group(3))
    if len(times) != 1:
        return None
    return m.group(1).lower(), m.group(2).lower(), next(iter(times))


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _EMOJI_RE.sub("", (text or "")).lower()).strip(" .")


def pick_option_from_answer(
    answer_head: str, options: dict[str, str], question_date: str = ""
) -> tuple[str, str]:
    """The option letter an answer NAMES without saying it, and how it was
    found ('datetime', 'parts', 'text', 'overlap'); ('', '') when no single
    option is named. Reads the answer's head (its first line), never the
    runner-up clauses after it."""
    head = answer_head or ""
    if not options:
        return "", ""
    # 1) date-time options: the option whose date, time and (when the answer
    # names one) weekday all appear in the answer. The answer may also name
    # the anchor it resolved FROM ("from 2024-10-09, next Friday is
    # 2024-10-11 at 14:00"), so dates are matched as a set and the weekday
    # breaks the tie between options a day apart.
    dated = {
        k: (set(_DATE_RE.findall(v)), _times_in(v), _weekdays_in(v)) for k, v in options.items()
    }
    if all(d and t for d, t, _w in dated.values()):
        a_dates, a_times, a_days = set(_DATE_RE.findall(head)), _times_in(head), _weekdays_in(head)
        if not a_dates:
            # "Oct 15, 9:00 AM" names the day without the year: match the
            # options' dates on month and day.
            md = _month_days_in(head)
            a_dates = {
                d for dd, _t, _w in dated.values() for d in dd if (int(d[5:7]), int(d[8:10])) in md
            }
        if a_dates and a_times:
            hits = [
                k
                for k, (d, t, w) in dated.items()
                if d & a_dates and t & a_times and (not a_days or not w or w & a_days)
            ]
            if len(hits) == 1:
                return hits[0], "datetime"
    low = _clean(head)
    # 1b) relative-day options ("next week Sunday 2:00 PM"): the option whose
    # relative phrase, weekday and time all appear in the answer — the time
    # matched by value ("at 2 PM" is "2:00 PM"), the phrase as words.
    rel = {k: _relative_parts(v) for k, v in options.items()}
    if all(r is not None for r in rel.values()):
        a_times = _times_in(head) | _loose_times_in(head)
        a_days = _weekdays_in(head)
        hits = [
            k
            for k, r in rel.items()
            if r is not None and r[0] in low and r[1] in a_days and r[2] in a_times
        ]
        if len(hits) == 1:
            return hits[0], "relative"
        # The answer may state the DATE the option's phrase resolves to
        # ("ServiceFest is on 2024-10-13 at 2 PM" for "next week Sunday 2:00
        # PM" from 2024-10-05): resolve each option against the question's
        # date and match on date + time (month-day when the year is absent).
        if question_date:
            a_dates = set(_DATE_RE.findall(head))
            a_md = _month_days_in(head)
            dated_hits = []
            for k, r in rel.items():
                if r is None:
                    continue
                iso = resolve_relative_day(r[0], r[1], question_date)
                if not iso or r[2] not in a_times:
                    continue
                if iso in a_dates or (not a_dates and (int(iso[5:7]), int(iso[8:10])) in a_md):
                    dated_hits.append(k)
            if len(dated_hits) == 1:
                return dated_hits[0], "relative-date"
    # 2) multi-part options ("Owen Brooks; Ocean Bounty"): every part named.
    parts_hits: dict[str, list[int]] = {}
    for k, v in options.items():
        parts = [_clean(p) for p in v.split(";") if p.strip()]
        if len(parts) >= 2 and all(p and p in low for p in parts):
            parts_hits[k] = [low.find(p) for p in parts]
    if len(parts_hits) == 1:
        return next(iter(parts_hits)), "parts"
    if len(parts_hits) > 1:
        # A comparison names both sides ("Owen is older than Jasper"): the
        # option whose every part is named FIRST in its clause is the one
        # the answer chose; when the order is mixed, no pick.
        ranked = sorted(parts_hits.items(), key=lambda kv: sum(kv[1]))
        best, second = ranked[0], ranked[1]
        if all(b < o for b, o in zip(best[1], second[1], strict=False)):
            return best[0], "parts"
    # 3) short single options named verbatim, exactly one of them.
    text_hits = [
        k
        for k, v in options.items()
        if len(v) <= 60
        and _clean(v)
        and re.search(r"(?<![a-z0-9])" + re.escape(_clean(v)) + r"(?![a-z0-9])", low)
    ]
    if len(text_hits) == 1 and len({_clean(options[k]) for k in text_hits}) == 1:
        return text_hits[0], "text"
    # 4) long options (quoted reviews, descriptions): content-word overlap.
    if any(len(v) > 60 for v in options.values()):
        a_words = {w for w in _WORD_RE.findall(low) if w not in _STOP_WORDS}
        scored = sorted(
            (
                (
                    len(
                        a_words & ({w for w in _WORD_RE.findall(_clean(v)) if w not in _STOP_WORDS})
                    ),
                    k,
                )
                for k, v in options.items()
            ),
            reverse=True,
        )
        if len(scored) >= 2 and scored[0][0] >= 3 and scored[0][0] - scored[1][0] >= 2:
            return scored[0][1], "overlap"
    return "", ""


_PLACES_LINE_RE = re.compile(r"(?im)^\s*PLACES?\s*:\s*(.+)$")
_PLACE_PAIR_RE = re.compile(r"\b([A-Ha-h])\s*[=:→-]\s*([A-Z][A-Za-z.]+(?:[ '][A-Z][A-Za-z.]+)*)")
_IN_CITY_RE = re.compile(
    r"\b(?:in|at|held in|located in|based in|happening in|is in|takes place in)\s+"
    r"([A-Z][A-Za-z.]+(?:[ '][A-Z][A-Za-z.]+)*)(?:,\s*[A-Z]{2}\b)?"
)


_ENTITY_BEFORE_IN_RE = re.compile(
    r"\b([A-Z][\w&'.-]*(?:\s+[A-Z&][\w&'.-]*){0,4})(?:,?\s+(?:which|that))?"
    r"\s+(?:is|was|will be|takes place|is held|is happening|is based|is located)"
    r"\s+(?:in|at)\s+[A-Z]"
)


def _key_prefix_of(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


def stored_city_for(entity: str, memories: list[Any]) -> str:
    """The city the pool's rows state for `entity`: rows whose key starts
    with the entity's key spelling (with or without `user_`) and whose text
    puts it somewhere ("Success Gala is happening in San Diego, CA"). ""
    when no such row, or when they disagree."""
    key = _key_prefix_of(entity)
    if len(key) < 3:
        return ""
    cities: set[str] = set()
    for m in memories or []:
        mk = str(getattr(m, "memory_key", "") or "").lower()
        if mk.startswith("chunk:") or mk.startswith("summary:"):
            continue
        bare = mk[5:] if mk.startswith("user_") else mk
        if not (bare.startswith(key) or mk.startswith(key)):
            continue
        text = str(getattr(m, "content_excerpt", "") or "")
        if entity.lower() not in text.lower():
            continue
        for cm in _IN_CITY_RE.finditer(text):
            cities.add(_clean(cm.group(1)))
    return next(iter(cities)) if len(cities) == 1 else ""


def verify_entity_location(answer: str, question: str, memories: list[Any]) -> Reconciled:
    """The city an answer gives an entity must be the city the pool's rows
    give it. Otherwise, reliably: "Success Gala … is in Denver, CO"
    with the row "Success Gala is happening in San Diego, CA" in the pool —
    the reader took a neighbouring event's city. The entity is the
    capitalised span before "is in / is happening in"; when the pool names
    one city for it and the answer names another, the answer's city is
    replaced and the PLACES map (if present) then re-picks the option."""
    if not answer or not memories:
        return Reconciled(answer)
    first, sep, rest = answer.partition("\n")
    em = _ENTITY_BEFORE_IN_RE.search(first)
    cm = _IN_CITY_RE.search(first)
    if not em or not cm:
        return Reconciled(answer)
    entity = em.group(1).strip()
    if len(entity.split()) == 1 and entity.lower() in ("the", "it", "this", "that"):
        return Reconciled(answer)
    stated = _clean(cm.group(1))
    stored = stored_city_for(entity, memories)
    if not stored or stored == stated or stored in stated or stated in stored:
        return Reconciled(answer)
    fixed_first = first[: cm.start(1)] + stored.title() + first[cm.end(1) :]
    # drop a trailing ", ST" that belonged to the wrong city
    fixed_first = re.sub(re.escape(stored.title()) + r",\s*[A-Z]{2}\b", stored.title(), fixed_first)
    return Reconciled(
        fixed_first + (sep + rest if sep else ""),
        corrected=True,
        from_letter="",
        to_letter="",
        named=f"{entity}: {stated} → {stored}",
    )


def reconcile_place_options(answer: str, question: str) -> Reconciled:
    """Options that DESCRIBE places ('Known for its theme parks…'): the reader
    writes a PLACES line naming each option's city, then the memory's city; the option
    whose city is the memory's city is the answer — the mapping is the model's, the
    choice is the program's. Without it the reader named Orlando and chose the beaches
    option, Portland and chose 'culture, music, cuisine'. The PLACES line is removed
    from the answer either way."""
    options = parse_options(question)
    if not options or not answer:
        return Reconciled(answer)
    m = _PLACES_LINE_RE.search(answer)
    if not m:
        return Reconciled(answer)
    mapping = {k.upper(): _clean(v) for k, v in _PLACE_PAIR_RE.findall(m.group(1))}
    body = (answer[: m.start()] + answer[m.end() :]).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    if len(mapping) < 2:
        return Reconciled(body)
    # the memory's city: the first "in <City>" the body states that is not an
    # option's description
    stated = ""
    for cm in _IN_CITY_RE.finditer(body):
        cand = _clean(cm.group(1))
        if any(cand == city or cand in city or city in cand for city in mapping.values()):
            stated = cand
            break
    if not stated:
        return Reconciled(body)
    hits = [
        k
        for k, city in mapping.items()
        if k in options and (city == stated or stated in city or city in stated)
    ]
    if len(hits) != 1:
        return Reconciled(body)
    letter = hits[0]
    first, sep, rest = body.partition("\n")
    lead = _LEADING_LETTER_RE.match(first)
    chosen = lead.group(1).upper() if lead else ""
    if chosen == letter:
        return Reconciled(body, named=stated)
    head = first[lead.end() :].strip() if lead else first.strip()
    new_first = f"{letter}) {options[letter]} — {stated.title()} per the memories; {head}"
    return Reconciled(
        new_first + (sep + rest if sep else ""),
        corrected=True,
        from_letter=chosen,
        to_letter=letter,
        named=stated,
    )


def verify_relative_date_option(answer: str, question: str) -> Reconciled:
    """Relative-day options ("next week Sunday 2:00 PM") with an answer that
    STATES the date ("ServiceFest is on October 13, 2024 at 2 PM") but
    chose the letter by its own week arithmetic: from the question's date the
    stated date resolves to exactly one option, and that option is the
    answer. Example: an answer read "C) this week Sunday 2:00 PM" with "October
    13, 2024 at 2 PM" in the same line, from a 2024-10-05 question date —
    next week's Sunday. Never runs without a question date, a stated date and
    exactly one matching option."""
    options = parse_options(question)
    qd = question_date_of(question)
    if not options or not answer or not qd:
        return Reconciled(answer)
    rel = {k: _relative_parts(v) for k, v in options.items()}
    if not rel or not all(r is not None for r in rel.values()):
        return Reconciled(answer)
    first, sep, rest = answer.partition("\n")
    lead = _LEADING_LETTER_RE.match(first)
    if not lead:
        return Reconciled(answer)
    chosen = lead.group(1).upper()
    body = first
    a_dates = set(_DATE_RE.findall(body))
    a_md = _month_days_in(body)
    a_times = _times_in(body) | _loose_times_in(body)
    if not (a_dates or a_md) or not a_times:
        return Reconciled(answer)
    hits = []
    for k, r in rel.items():
        if r is None:
            continue
        iso = resolve_relative_day(r[0], r[1], qd)
        if not iso or r[2] not in a_times:
            continue
        if iso in a_dates or (not a_dates and (int(iso[5:7]), int(iso[8:10])) in a_md):
            hits.append(k)
    if len(hits) != 1 or hits[0] == chosen:
        return Reconciled(answer)
    letter = hits[0]
    head = first[lead.end() :].strip()
    new_first = (
        f"{letter}) {options[letter]} — the stated date resolves to this option from "
        f"{qd} (weeks Monday to Sunday). Reader: {head}"
    )
    return Reconciled(
        new_first + (sep + rest if sep else ""),
        corrected=True,
        from_letter=chosen,
        to_letter=letter,
        named=options[letter],
    )


def pick_relative_option_from_text(text: str, question: str) -> str:
    """For relative-day options, the ONE option whose date (from the
    question's date) and time the whole answer states — "FitFest2024 is on
    October 11th, 2024 at 7 PM" → "next week Friday 7:00 PM"; "" when none
    or several. Used when the answer carries no letter and its head names
    nothing: the reader listed candidates and their times and concluded
    that none matched, while one did."""
    options = parse_options(question)
    qd = question_date_of(question)
    if not options or not qd:
        return ""
    rel = {k: _relative_parts(v) for k, v in options.items()}
    if not rel or not all(r is not None for r in rel.values()):
        return ""
    body = text or ""
    a_dates = set(_DATE_RE.findall(body))
    a_md = _month_days_in(body)
    a_times = _times_in(body) | _loose_times_in(body)
    hits = []
    for k, r in rel.items():
        if r is None:
            continue
        iso = resolve_relative_day(r[0], r[1], qd)
        if not iso or r[2] not in a_times:
            continue
        if iso in a_dates or (int(iso[5:7]), int(iso[8:10])) in a_md:
            hits.append(k)
    return hits[0] if len(hits) == 1 else ""


def reconcile_missing_letter(answer: str, question: str) -> Reconciled:
    """An answer that names an option without leading with its letter gets
    the letter: a multiple-choice contract asked for the letter, the reader
    wrote the answer instead (a resolved date, both halves of a comparison,
    a paraphrase of a long option). Deterministic; untouched when a letter
    already leads or no single option is named."""
    options = parse_options(question)
    if not options or not answer:
        return Reconciled(answer)
    first, sep, rest = answer.partition("\n")
    if _LEADING_LETTER_RE.match(first):
        return Reconciled(answer)
    letter, how = pick_option_from_answer(first, options, question_date=question_date_of(question))
    if not letter:
        # No letter and nothing named in the head: a relative-day option the
        # WHOLE answer's stated dates resolve to.
        fallback = pick_relative_option_from_text(answer, question)
        if fallback:
            return Reconciled(
                f"{fallback}) {options[fallback]} — the stated date resolves to this option. "
                f"Reader: {answer.strip()}",
                corrected=True,
                from_letter="",
                to_letter=fallback,
                named=options[fallback],
            )
        return Reconciled(answer)
    new_first = (
        f"{letter}) {first.strip()} (letter set to {letter}: the answer names that option; {how})"
    )
    return Reconciled(
        new_first + (sep + rest if sep else ""),
        corrected=True,
        to_letter=letter,
        named=options[letter],
    )


__all__ = [
    "verify_entity_location",
    "verify_relative_date_option",
    "reconcile_place_options",
    "question_stem",
    "Reconciled",
    "parse_options",
    "pick_option_from_answer",
    "reconcile_missing_letter",
    "reconcile_unoffered_claim",
]


_NEED_MORE_Q_RE = re.compile(
    r"\bhow (?:many|much)\b[^?]{0,60}\b(?:do|will|would|did) i (?:still )?need\b|"
    r"\bhow (?:many|much) more\b",
    re.IGNORECASE,
)
_NEED_MORE_A_RE = re.compile(
    r"\bneed(?:s|ed)?\s+(?:about |around |roughly |approximately )?"
    r"((?:\$|€|£)?\d[\d,]*(?:\.\d+)?)\s+more\b",
    re.IGNORECASE,
)
_LEADING_FIGURE_RE = re.compile(
    r"^\s*\**\s*(?:about |around |roughly |approximately )?(?:\$|€|£)?\d"
)


def lead_with_needed_figure(answer: str, question: str) -> Reconciled:
    """ "How many points do I need to earn…" answered "You need a total of 300
    … you have 200, meaning you need 100 more": the derived figure is right
    and buried, and a free-form judge reads the first number. When the
    question asks how many/much more is needed and the answer states "need
    N more", the answer leads with N. Pure; the text otherwise stands."""
    if not answer or not _NEED_MORE_Q_RE.search(question or ""):
        return Reconciled(answer)
    if _LEADING_FIGURE_RE.match(answer):
        return Reconciled(answer)
    m = _NEED_MORE_A_RE.search(answer)
    if not m:
        return Reconciled(answer)
    figure = m.group(1)
    return Reconciled(f"{figure} more — {answer.strip()}", corrected=True, named=figure)
