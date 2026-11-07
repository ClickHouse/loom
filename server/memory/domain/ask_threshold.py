"""Program-of-thoughts for count questions: the model lists, the server
compares.

A count question is enumerate-then-count and the server recounts the verdict lines
(`ask_recount`). What the model still gets wrong is the COMPARISON: "six weeks or
longer" against "six weeks" is a boundary it reads inclusively one run and strictly the
next, and a two-part count where one part fits an option and the other does not ends in
"closest" guesses. So the language work is separated from the arithmetic: the model
produces the structured intermediate — the ITEMS lines with one value each — and a
program does the comparing.

This module parses the threshold from each question part ("eight days or
longer" → ≥ 8 days; "more than 600" → > 600; "a week or less" → ≤ 7 days),
parses the value on each ITEMS line ("a week and a day", "about four hundred
people", "seven weeks", "26"), counts under the stated reading and under the
strict reading, and picks the option those counts hit — the asker's options
are evidence of the asker's reading, so the inclusive count is tried first
and the strict count only when the inclusive one fits no option. It rewrites
the leading letter and the ANSWER numbers, says what it computed, and
touches nothing else. Any part it cannot parse leaves the answer alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from server.memory.domain.question_literals import question_literals

_WORDS = {
    "zero": 0,
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
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_SCALES = {"hundred": 100, "thousand": 1000, "k": 1000}
_UNIT_DAYS = {
    "day": 1,
    "days": 1,
    "week": 7,
    "weeks": 7,
    "month": 30,
    "months": 30,
    "year": 365,
    "years": 365,
}
_NUM_RE = re.compile(
    r"(?:~|about|around|roughly|approximately|nearly|almost|just|whole|over|under)?(?<!\s)\s*"
    r"\b(?P<num>\d+(?:[.,]\d+)?|" + "|".join(sorted(_WORDS, key=len, reverse=True)) + r")\b"
    r"(?:[\s-]+(?P<tens>"
    + "|".join(k for k in _WORDS if _WORDS[k] < 10 and k not in ("a", "an"))
    + r")\b)?"
    r"(?:\s+(?P<scale>hundred|thousand)\b|(?P<k>k)\b)?"
    r"(?:\s+(?:whole|full|entire|long|solid))?"
    r"(?:\s+(?P<unit>days?|weeks?|months?|years?)\b)?",
    re.I,
)
_THRESH_RE = re.compile(
    r"(?P<qty>(?:\d+(?:[.,]\d+)?|" + "|".join(sorted(_WORDS, key=len, reverse=True)) + r")"
    r"(?:[\s-]+(?:"
    + "|".join(k for k in _WORDS if 0 < _WORDS[k] < 10 and k not in ("a", "an"))
    + r")\b)?"
    r"(?:\s+(?:hundred|thousand))?"
    r"(?:\s+(?:days?|weeks?|months?|years?|people|persons|attendees|participants|years? old))?)"
    r"\s+(?P<ge>or (?:longer|more|older|above|greater|larger|higher|bigger|later)\b"
    r"|and (?:up|above|over)\b)?"
    r"(?P<le>or (?:less|fewer|shorter|younger|below|smaller|lower|earlier)\b"
    r"|and (?:under|below)\b)?",
    re.I,
)
_PRE_RE = re.compile(
    r"\b(?P<op>at least|no fewer than|no less than|a minimum of|more than|over|longer than|"
    r"older than|above|greater than|larger than|at most|no more than|up to|a maximum of|"
    r"less than|under|"
    r"shorter than|younger than|below|fewer than)\s+(?P<qty>(?:\d+(?:[.,]\d+)?|"
    + "|".join(sorted(_WORDS, key=len, reverse=True))
    + r")"
    r"(?:[\s-]+(?:"
    + "|".join(k for k in _WORDS if 0 < _WORDS[k] < 10 and k not in ("a", "an"))
    + r")\b)?"
    r"(?:\s+(?:hundred|thousand))?(?:\s+(?:days?|weeks?|months?|years?))?)",
    re.I,
)
_PRE_OPS = {
    "at least": ">=",
    "no fewer than": ">=",
    "no less than": ">=",
    "a minimum of": ">=",
    "more than": ">",
    "over": ">",
    "longer than": ">",
    "older than": ">",
    "above": ">",
    "greater than": ">",
    "larger than": ">",
    "at most": "<=",
    "no more than": "<=",
    "up to": "<=",
    "a maximum of": "<=",
    "less than": "<",
    "under": "<",
    "shorter than": "<",
    "younger than": "<",
    "below": "<",
    "fewer than": "<",
}
_PART_SPLIT_RE = re.compile(
    # blanks read from the first of a run; right after one (where a `;` split
    # stopped) the mark comes at once
    r"(?:(?<!\s)\s*|(?<=\s))(?:\?|\.)\s*(?:Additionally|Also|In addition|And)[,:]?\s+|;\s+",
    re.I,
)
_ANSWER_RE = re.compile(r"(?im)^\s*ANSWER\s*:\s*(.*)$")
_ITEMS_HEAD_RE = re.compile(r"(?im)^\s*ITEMS\b[^\n]*$")
_VERDICT_RE = re.compile(r"—\s*(qualifies|fails|unclear)\b", re.I)
_LEAD_LETTER_RE = re.compile(r"^\s*\(?([A-Ha-h])[\)\.:]\s*")
_OPTION_LINE_RE = re.compile(r"(?m)^\s*\(?([A-Ha-h])[\)\.:]\s+(.+?)\s*$")
_INT_RE = re.compile(r"\d+")
_SUPERSEDED_RE = re.compile(
    r"superseded|no longer|moved|latest|now (?:in|at|based)|updated|formerly|previously|was in",
    re.I,
)
_STATEMENT_SEMANTICS_RE = re.compile(
    r"every stated instance|each statement|each stated|every statement|each time it was mentioned|"
    r"each mention|count (?:the )?statements",
    re.I,
)


def wants_statement_count(question: str) -> bool:
    """The asker said to count statements, not entities."""
    return bool(_STATEMENT_SEMANTICS_RE.search(question or ""))


@dataclass(frozen=True)
class Threshold:
    op: str  # ">=", ">", "<=", "<"
    value: float
    temporal: bool  # value in days

    def strict(self) -> Threshold:
        return Threshold({">=": ">", "<=": "<"}.get(self.op, self.op), self.value, self.temporal)

    def holds(self, v: float) -> bool:
        return {
            ">=": v >= self.value,
            ">": v > self.value,
            "<=": v <= self.value,
            "<": v < self.value,
        }[self.op]


def _word_num(tok: str) -> float | None:
    t = tok.lower().replace(",", "")
    if t in _WORDS:
        return float(_WORDS[t])
    try:
        return float(t)
    except ValueError:
        return None


def parse_quantity(text: str, *, temporal: bool | None = None) -> float | None:
    """The quantity a value phrase states, summed over joined pieces ("a week
    and a day" → 8 days). Durations come back in days; counts as plain
    numbers. None when nothing numeric is found."""
    total = 0.0
    found = False
    any_unit = False
    for m in _NUM_RE.finditer(text or ""):
        n = _word_num(m.group("num"))
        if n is None:
            continue
        if m.group("num").lower() in ("a", "an") and not m.group("unit") and not m.group("scale"):
            continue  # "a crowd of …" is an article, not a one
        if m.group("tens"):
            t = _word_num(m.group("tens"))
            if t is not None and n >= 20:
                n += t
        scale = (m.group("scale") or m.group("k") or "").lower()
        if scale:
            n *= _SCALES[scale]
        unit = (m.group("unit") or "").lower()
        if unit:
            n *= _UNIT_DAYS[unit]
            any_unit = True
        elif temporal:
            # A bare number in a duration context ("lasts 7") is read as days.
            pass
        total += n
        found = True
    if not found:
        return None
    if temporal is True and not any_unit and total > 0 and re.search(r"\bweek", text or "", re.I):
        return total * 7
    return total


_CAPACITY_RE = re.compile(
    r"\b(?P<verb>can (?:fit|hold|accommodate|host|seat|welcome|take)|capacity (?:of|for)|"
    r"room for|(?:fits?|holds?|accommodates?|seats?) )\s*(?P<upto>up to )?"
    r"(?:a crowd of |a team of |about |around |roughly )?"
    r"(?P<qty>(?:\d+(?:[.,]\d+)?|" + "|".join(sorted(_WORDS, key=len, reverse=True)) + r")"
    r"(?:[\s-]+(?:"
    + "|".join(k for k in _WORDS if 0 < _WORDS[k] < 10 and k not in ("a", "an"))
    + r")\b)?"
    r"(?:\s+(?:hundred|thousand))?)",
    re.I,
)


def parse_threshold(part: str) -> Threshold | None:
    """The comparison a question part states, or None (a plain "how many in
    Chicago" has no threshold). Capacity phrasing ("can fit a crowd of two
    hundred", "room for 500") reads as at-least: a venue that fits two
    hundred fits a crowd of two hundred."""
    text = part or ""
    cm = _CAPACITY_RE.search(text)
    if cm:
        v = parse_quantity(cm.group("qty"))
        if v is not None:
            return Threshold("<=" if cm.group("upto") else ">=", v, False)
    m = _PRE_RE.search(text)
    if m:
        op = _PRE_OPS[m.group("op").lower()]
        qty = m.group("qty")
        temporal = bool(re.search(r"days?|weeks?|months?|years?", qty, re.I))
        v = parse_quantity(qty, temporal=temporal)
        return Threshold(op, v, temporal) if v is not None else None
    for m in _THRESH_RE.finditer(text):
        if not (m.group("ge") or m.group("le")):
            continue
        qty = m.group("qty")
        temporal = bool(re.search(r"days?|weeks?|months?|years?", qty, re.I))
        v = parse_quantity(re.sub(r"\byears? old\b", "", qty, flags=re.I), temporal=temporal)
        if v is None:
            continue
        return Threshold(">=" if m.group("ge") else "<=", v, temporal)
    return None


def split_parts(question: str) -> list[str]:
    stem = (question or "").split("\n")[0] if "\n" in (question or "") else (question or "")
    # Drop the "Current date:" line and the option block when the whole query was passed.
    stem = re.sub(r"(?im)^\s*Current date:.*$", "", question or "").strip()
    stem = stem.split("\n\n")[0]
    parts = [p.strip() for p in _PART_SPLIT_RE.split(stem) if p.strip()]
    return parts or [stem]


@dataclass
class ItemsPart:
    lines: list[tuple[str, str, str]] = field(default_factory=list)  # (name, value_text, verdict)


def parse_items(text: str) -> list[ItemsPart]:
    """ITEMS blocks in order; a two-part answer has two blocks (each under
    its own ITEMS heading), a one-part answer one."""
    body = text or ""
    ans = _ANSWER_RE.search(body)
    if ans:
        body = body[: ans.start()] + body[ans.end() :]
    heads = list(_ITEMS_HEAD_RE.finditer(body))
    if not heads:
        return []
    parts: list[ItemsPart] = []
    for i, h in enumerate(heads):
        seg = body[h.end() : heads[i + 1].start() if i + 1 < len(heads) else len(body)]
        part = ItemsPart()
        for raw in seg.splitlines():
            line = raw.strip().lstrip("-•* ").strip()
            if not line or line.upper().startswith("UNKNOWN"):
                continue
            v = _VERDICT_RE.search(line)
            if not v:
                continue
            head = line[: v.start()]
            pieces = [p.strip() for p in head.split("—") if p.strip()]
            if not pieces:
                continue
            name = pieces[0]
            value = " ".join(pieces[1:]) if len(pieces) > 1 else ""
            part.lines.append((name, value, v.group(1).lower()))
        parts.append(part)
    return parts


def option_numbers(question: str) -> dict[str, list[int]]:
    """{letter: [n1, n2, …]} from the option block; empty when fewer than two options."""
    out: dict[str, list[int]] = {}
    for m in _OPTION_LINE_RE.finditer(question or ""):
        nums = [int(x) for x in _INT_RE.findall(m.group(2))]
        if nums:
            out[m.group(1).upper()] = nums
    return out if len(out) >= 2 else {}


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def dedupe_lines(part: ItemsPart) -> ItemsPart:
    """One line per entity: the same name spelt with different spacing,
    punctuation or case (a fact row and the raw turn behind it) is one item
    under entity semantics. The first line wins."""
    seen: set[str] = set()
    out = ItemsPart()
    for name, value, verdict in part.lines:
        key = _norm_name(name)
        if key in seen:
            continue
        seen.add(key)
        out.lines.append((name, value, verdict))
    return out


def _count_literal(part: ItemsPart, literal: str) -> tuple[int, int]:
    """(lines whose value or name carries `literal` and is not superseded, lines)."""
    lit = literal.lower()
    hit = 0
    for name, value, verdict in part.lines:
        carries = lit in value.lower() or (not value and lit in name.lower())
        superseded = bool(_SUPERSEDED_RE.search(value))
        # A qualifying line counts; a line the reader failed for its CATEGORY
        # counts too when its value still carries the literal (a crew "based
        # in Boston" is in Boston); a superseded value never counts.
        if verdict == "qualifies" and not (superseded and not carries):
            hit += 1
        elif carries and not superseded:
            hit += 1
    return hit, len(part.lines)


def _count(part: ItemsPart, th: Threshold) -> tuple[int, int]:
    """(count under th, lines that parsed)."""
    hit = parsed = 0
    for _name, value, _verdict in part.lines:
        v = parse_quantity(value, temporal=th.temporal)
        if v is None:
            continue
        parsed += 1
        if th.holds(v):
            hit += 1
    return hit, parsed


@dataclass(frozen=True)
class Verified:
    text: str
    changed: bool = False
    inclusive: tuple[int, ...] = ()
    strict: tuple[int, ...] = ()
    letter: str = ""
    reading: str = ""


def verify_counts(text: str, question: str) -> Verified:
    """Recompute the thresholded counts from the ITEMS values and pick the
    option they hit; untouched when there is no threshold, no parseable
    items, no option block, or when the model's letter already matches."""
    options = option_numbers(question)
    ans_m = _ANSWER_RE.search(text or "")
    if not options or not ans_m:
        return Verified(text)

    parts_q = split_parts(question)
    thresholds = [parse_threshold(p) for p in parts_q]
    literals = [
        (question_literals(p, max_literals=1) or [""])[0] if th is None else ""
        for p, th in zip(parts_q, thresholds, strict=False)
    ]
    if not any(thresholds) and not any(literals):
        return Verified(text)
    items = parse_items(text)
    if len(items) != len(parts_q):
        return Verified(text)
    if not wants_statement_count(question):
        items = [dedupe_lines(p) for p in items]
    ans_line = ans_m.group(1)
    lead = _LEAD_LETTER_RE.match(ans_line)
    model_letter = lead.group(1).upper() if lead else ""
    model_nums = [int(x) for x in _INT_RE.findall(ans_line)]

    def counts(strict: bool) -> list[int] | None:
        out: list[int] = []
        for i, th in enumerate(thresholds):
            if th is None:
                if literals[i]:
                    hit, n = _count_literal(items[i], literals[i])
                    if n == 0:
                        return None
                    out.append(hit)
                    continue
                # Neither threshold nor literal: trust the model's number in position i.
                if i < len(model_nums):
                    out.append(model_nums[i])
                else:
                    return None
                continue
            hit, parsed = _count(items[i], th.strict() if strict else th)
            if parsed == 0:
                return None
            out.append(hit)
        return out

    incl = counts(False)
    if incl is None:
        return Verified(text)
    stri = counts(True)

    def match(nums: list[int]) -> str:
        for letter, on in options.items():
            if on[: len(nums)] == nums:
                return letter
        return ""

    letter, reading = match(incl), "inclusive"
    if not letter and stri is not None:
        letter, reading = match(stri), "strict"
    if not letter or letter == model_letter:
        return Verified(
            text, inclusive=tuple(incl), strict=tuple(stri or ()), letter=letter or model_letter
        )
    chosen = incl if reading == "inclusive" else (stri or incl)
    body = ans_line[lead.end() :].strip() if lead else ans_line.strip()
    body = re.sub(
        r"^\d+(?:\s*\(\d+[^)]*\))?(?:\s+\w+)?(?:\s*;\s*\d+(?:\s+\w+)?)*\s*[—:-]?\s*", "", body
    )
    note = (
        f"(computed from the listed items: {', '.join(str(c) for c in chosen)}"
        + (f" under the {reading} reading of the threshold" if any(thresholds) else "")
        + (
            f"; {', '.join(str(c) for c in stri)} strictly"
            if reading == "inclusive" and stri and stri != incl
            else ""
        )
        + ")"
    )
    new_ans = f"ANSWER: {letter}) {'; '.join(str(c) for c in chosen)} — {body} {note}".rstrip()
    new_text = text[: ans_m.start()] + new_ans + text[ans_m.end() :]
    return Verified(
        new_text,
        changed=True,
        inclusive=tuple(incl),
        strict=tuple(stri or ()),
        letter=letter,
        reading=reading,
    )


_CATEGORY_REASON_RE = re.compile(
    r"not an? (?:event|venue)|(?:a |an )?(?:company|organi[sz]ation|team|crew|brand|business|"
    r"group|club|firm|publisher|team/crew)\b",
    re.I,
)


def verify_literal_counts(text: str, question: str) -> Verified:
    """A literal count ("how many events are in Boston") is decided by the
    value on the ITEMS line, not by the reader's category judgement: a line
    whose value carries the literal counts unless its verdict says the value
    was superseded. Example: "Team Connect — Boston, MA — fails: it's a
    team/crew, not an event" under gold that counts it; the same shape read
    five Houston events as companies. Single-part literal questions only;
    untouched when the count already matches the model's letter."""
    options = option_numbers(question)
    ans_m = _ANSWER_RE.search(text or "")
    if not options or not ans_m:
        return Verified(text)
    parts_q = split_parts(question)
    if len(parts_q) != 1 or parse_threshold(parts_q[0]) is not None:
        return Verified(text)
    lits = question_literals(parts_q[0], max_literals=1)
    if not lits:
        return Verified(text)
    lit = lits[0].lower()
    items = parse_items(text)
    if len(items) != 1 or not items[0].lines:
        return Verified(text)
    n = 0
    flipped = 0
    for name, value, verdict in items[0].lines:
        carries = lit in value.lower() or (not value and lit in name.lower())
        if not carries:
            continue
        if verdict == "qualifies":
            n += 1
        elif (
            verdict != "qualifies"
            and not _SUPERSEDED_RE.search(value)
            and _CATEGORY_REASON_RE.search(_line_reason(text, name))
        ):
            n += 1
            flipped += 1
    if flipped == 0:
        return Verified(text)
    ans_line = ans_m.group(1)
    lead = _LEAD_LETTER_RE.match(ans_line)
    model_letter = lead.group(1).upper() if lead else ""
    letter = ""
    for opt_letter, nums in options.items():
        if nums[:1] == [n]:
            letter = opt_letter
            break
    if not letter or letter == model_letter:
        return Verified(text, inclusive=(n,), letter=letter or model_letter)
    body = ans_line[lead.end() :].strip() if lead else ans_line.strip()
    body = re.sub(r"^\d+(?:\s+\w+)?\s*[—:-]?\s*", "", body)
    note = (
        f"(computed: {n} listed items carry '{lits[0]}'; {flipped} had been set aside for "
        f"their category, which the question does not ask about)"
    )
    new_ans = f"ANSWER: {letter}) {n} — {body} {note}".rstrip()
    new_text = text[: ans_m.start()] + new_ans + text[ans_m.end() :]
    return Verified(new_text, changed=True, inclusive=(n,), letter=letter, reading="literal")


def verify_statement_count(text: str, question: str, statements: int) -> Verified:
    """When the asker counts statements and the question names a literal, the
    number of user turns carrying that literal IS the answer — computed, not
    read. Picks the option equal to it; untouched when the asker did not ask
    for statements, the question has a threshold or two parts, no turns were
    counted, or the model's letter already matches."""
    if statements <= 0 or not wants_statement_count(question):
        return Verified(text)
    options = option_numbers(question)
    ans_m = _ANSWER_RE.search(text or "")
    if not options or not ans_m:
        return Verified(text)
    parts_q = split_parts(question)
    if len(parts_q) != 1 or parse_threshold(parts_q[0]) is not None:
        return Verified(text)

    lits = question_literals(parts_q[0], max_literals=1)
    if not lits:
        return Verified(text)
    ans_line = ans_m.group(1)
    lead = _LEAD_LETTER_RE.match(ans_line)
    model_letter = lead.group(1).upper() if lead else ""
    letter = next((k for k, v in options.items() if v[:1] == [statements]), "")
    if not letter or letter == model_letter:
        return Verified(text, inclusive=(statements,), letter=letter or model_letter)
    body = ans_line[lead.end() :].strip() if lead else ans_line.strip()
    body = re.sub(r"^\d+(?:\s+\w+)?\s*[—:-]?\s*", "", body)
    note = f"(computed: {statements} stated instances name '{lits[0]}')"
    new_ans = f"ANSWER: {letter}) {statements} — {body} {note}".rstrip()
    return Verified(
        text[: ans_m.start()] + new_ans + text[ans_m.end() :],
        changed=True,
        inclusive=(statements,),
        letter=letter,
        reading="statements",
    )


def _line_reason(text: str, name: str) -> str:
    """The failure reason written after the verdict on the ITEMS line for `name`."""
    for raw in (text or "").splitlines():
        if raw.strip().lstrip("-•* ").startswith(name):
            m = _VERDICT_RE.search(raw)
            return raw[m.end() :] if m else ""
    return ""


__all__ = [
    "Threshold",
    "Verified",
    "option_numbers",
    "parse_items",
    "parse_quantity",
    "parse_threshold",
    "split_parts",
    "verify_counts",
    "dedupe_lines",
    "verify_literal_counts",
    "verify_statement_count",
    "wants_statement_count",
]
