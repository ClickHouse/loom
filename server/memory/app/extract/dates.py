"""Deterministic date passes over extracted facts.

Turn timestamps as anchors, the relative-phrase backstop (an anchor the model
left out or set to the session day, a weekday a week off, a phrase dropped
from the value), the immediacy clamp, the text-date alignment, and writing
the resolved date into the value.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta

from server.core.datetimes import parse_iso_dt
from server.memory.app.extract.models import ExtractedFact
from server.memory.app.extract.turns import _message_stamp
from server.memory.domain.extraction_temporal import (
    _ISO_IN_TEXT_RE,
    append_note,
    duration_note,
    ground_relative_phrase,
    named_week_weekday,
    relative_phrase_class,
    resolve_relative_date,
)


def apply_message_timestamps(
    facts: list[ExtractedFact], messages: list[dict[str, object]]
) -> list[ExtractedFact]:
    """Deterministic per-turn anchoring: a fact whose cited
    source messages all carry timestamps, and which the extractor left
    unanchored or anchored only to the day, takes the latest cited
    message's timestamp as temporal_anchor and valid_at. Never overrides an
    anchor with a time of day or a different date the model chose."""
    if not facts or not messages:
        return facts
    stamps = [_message_stamp(m) for m in messages]
    if not any(stamps):
        return facts
    out: list[ExtractedFact] = []
    for f in facts:
        idxs = [i for i in (f.source_message_indices or []) if 0 <= i < len(stamps) and stamps[i]]
        if not idxs:
            out.append(f)
            continue
        stamp = max(stamps[i] for i in idxs)  # latest cited turn
        iso = stamp.replace(" ", "T") + ":00"
        anchor = f.temporal_anchor
        day_only = anchor is None or (anchor.endswith("T00:00:00") and anchor[:10] == stamp[:10])
        if not day_only:
            out.append(f)
            continue
        valid = f.valid_at
        valid_day_only = valid is None or (valid.endswith("T00:00:00") and valid[:10] == stamp[:10])
        out.append(replace(f, temporal_anchor=iso, valid_at=iso if valid_day_only else valid))
    return out


def _apply_temporal_anchor_backstop(
    facts: list[ExtractedFact],
    observation_date: str,
    messages: list[dict[str, object]] | None = None,
) -> list[ExtractedFact]:
    """Deterministic backstop for facts whose value carries a closed-class
    relative qualifier.

    Two cases:
      * the model left the fact unanchored → anchor it;
      * the model anchored it to the OBSERVATION DATE ITSELF while the value
        says "yesterday" / "last Friday" / "two weekends ago" → the prompt's
        default ("dated activities anchor to observation_date") won over the
        phrase. Measured on 1,996 conversation facts: 259 of 349 relative-phrase
        facts carried the session date as their anchor, and the resolver
        disagreed with every one. The resolved date replaces it.
    A third case: the value names "last <weekday>" and the
    model's own anchor is exactly one week off the resolver's day. The model
    reads "last Friday" as the Friday of the previous calendar week; the
    resolver and every gold answer read it as the most recent Friday.
    Measured over 1,918 stored facts: 39 kept a weekday phrase with a date
    in the text, 9 disagreed with the resolver, all 9 by exactly −7 days.
    The resolver's day replaces the anchor, a `valid_at` on the same day,
    and the ISO day the model wrote into the text, so the stored sentence
    and its anchor agree. Any other anchor the model set is the model's
    own resolution and is never touched; facts without a qualifier are
    never touched. Writing a missing date into the TEXT is the grounding pass
    (`_apply_relative_date_grounding`), which runs after this one.
    """
    # A relative phrase resolves against the turn that SAID it: a fact cited to a
    # timestamped message anchors to that day, not to the session's observation_date.
    stamps = [_message_stamp(m) for m in (messages or [])]
    out: list[ExtractedFact] = []
    changed = False
    for f in facts:
        idxs = [i for i in (f.source_message_indices or []) if 0 <= i < len(stamps) and stamps[i]]
        said_on = max(stamps[i] for i in idxs)[:10] if idxs else (observation_date or "")[:10]
        # The phrase lives in the turn; the model's value may have dropped the
        # weekday ("happening on 2024-10-08 at 2:00 PM next week"). When a
        # cited turn names a weekday, the turn decides; otherwise the value.
        cited = " ".join(
            str((messages or [])[i].get("content") or "")
            for i in (f.source_message_indices or [])
            if messages and 0 <= i < len(messages)
        )
        wd = named_week_weekday(cited) if cited else None
        if wd is not None:
            resolved = resolve_relative_date(cited, said_on)
        else:
            resolved = resolve_relative_date(f.value, said_on)
            wd = named_week_weekday(f.value)
        if not resolved and cited:
            # Fourth case: the model dropped the phrase from
            # the value ("shared her book with her writers group" for "last
            # week when I shared my book…"). The cited turn still has it: the
            # sentence that best matches the value decides, the value gains
            # the phrase and its date so the reader sees both.
            recovered = _recover_dropped(f, cited, said_on)
            if recovered is not None:
                out.append(recovered)
                changed = True
                continue
        if not resolved:
            out.append(f)
            continue
        anchor_day = (f.temporal_anchor or "")[:10]
        if f.temporal_anchor is None or (anchor_day == said_on and resolved != said_on):
            out.append(replace(f, temporal_anchor=resolved + "T00:00:00"))
            changed = True
            continue
        # Two ways the model's own resolution is arithmetic gone wrong: a
        # NAMED-WEEK phrase whose anchor is not the named weekday ("next
        # week on Sunday" said on Friday 2024-10-04, anchored to Tuesday
        # 2024-10-08), or "last <weekday>" landed exactly a week off. A
        # bare-weekday anchor off by any other amount stays the model's own. The
        # resolver's date replaces the anchor, the valid_at that copied it, and
        # the date in the value text.
        wrong_weekday = False
        if wd is not None and anchor_day and anchor_day != resolved:
            try:
                wrong_weekday = datetime.strptime(anchor_day, "%Y-%m-%d").weekday() != wd
            except ValueError:
                wrong_weekday = False
        if anchor_day != resolved and (
            wrong_weekday or _weekday_week_off(f.value, anchor_day, resolved)
        ):
            fixed = replace(
                f,
                temporal_anchor=resolved + "T00:00:00",
                valid_at=(
                    resolved + (f.valid_at or "")[10:]
                    if (f.valid_at or "")[:10] == anchor_day
                    else f.valid_at
                ),
                value=f.value.replace(anchor_day, resolved) if anchor_day else f.value,
            )
            out.append(fixed)
            changed = True
            continue
        out.append(f)
    return out if changed else facts


_LAST_WEEKDAY_RE = re.compile(
    r"\b(?:last|this past) (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)

_RECOVER_MAX_DAYS = 60


def _recover_dropped_phrase(f: ExtractedFact, cited: str, said_on: str) -> ExtractedFact | None:
    """A value with no date and no relative phrase whose cited
    sentence carries a past one takes that sentence's resolution — anchor,
    `valid_at` (when it was the session day or unset) and a note in the text
    naming the phrase and the day ("(last week, around 2022-08-15)"). Day-
    and week-scale phrases only: "five years ago" on "Priya uses the
    guitar to express herself" would date a present state to 2018. None
    when nothing applies."""
    from server.memory.domain.extraction_temporal import (
        append_note,
        dropped_phrase_sentence,
        relative_phrase_class,
    )

    hit = dropped_phrase_sentence(f.value or "", cited)
    if hit is None or not said_on:
        return None
    sentence, phrase = hit
    resolved = resolve_relative_date(sentence, said_on)
    if not resolved:
        return None
    said_dt, res_dt = parse_iso_dt(said_on), parse_iso_dt(resolved)
    if said_dt is None or res_dt is None or not 0 <= (said_dt - res_dt).days <= _RECOVER_MAX_DAYS:
        return None
    word = "on" if relative_phrase_class(phrase) == "point" else "around"
    valid_at = f.valid_at
    if not valid_at or (valid_at or "")[:10] == said_on:
        valid_at = resolved + "T00:00:00"
    return replace(
        f,
        temporal_anchor=resolved + "T00:00:00",
        valid_at=valid_at,
        value=append_note(f.value, f"{phrase.lower()}, {word} {resolved}"),
    )


def _recover_dropped(f: ExtractedFact, cited: str, said_on: str) -> ExtractedFact | None:
    """A phrase the extractor dropped: the concrete one first, else the vague
    one."""
    return _recover_dropped_phrase(f, cited, said_on) or _recover_dropped_vague_phrase(
        f, cited, said_on
    )


def _recover_dropped_vague_phrase(
    f: ExtractedFact, cited: str, said_on: str
) -> ExtractedFact | None:
    """A value with no date and no phrase whose cited sentence
    carries a vague past offset ("a few months ago") keeps the phrase as a
    note against the day it was said — "(a few months ago as of
    2023-05-28)". Anchor and `valid_at` are left alone: the phrase names no
    day, and a month-precise guess would be read as one. None when nothing
    applies."""
    from server.memory.domain.extraction_temporal import append_note, dropped_vague_phrase_sentence

    if not said_on:
        return None
    hit = dropped_vague_phrase_sentence(f.value or "", cited, said_on=said_on)
    if hit is None:
        return None
    _, phrase = hit
    value = f.value
    if said_on in value:
        # The session day written as the event's date: the sentence said the
        # event was a while back, so the day is the extractor's default, not
        # a fact. The note replaces it.
        value = re.sub(
            rf"\s*\b(?:on|around|in)\s+{re.escape(said_on)}\b|\b{re.escape(said_on)}\b", "", value
        )
        value = re.sub(r"\s{2,}", " ", value).replace(" ,", ",").replace(" .", ".")
    return replace(f, value=append_note(value, f"{phrase.lower()} as of {said_on}"))


def _weekday_week_off(value: str, anchor_day: str, resolved: str) -> bool:
    """True when the value names "last <weekday>" and the model's anchor sits
    exactly seven days before or after the resolver's day: the calendar-week
    reading of the phrase. Any other disagreement is left to the model."""
    if not _LAST_WEEKDAY_RE.search(value or ""):
        return False
    a, r = parse_iso_dt(anchor_day), parse_iso_dt(resolved)
    if a is None or r is None:
        return False
    return abs((a - r).days) == 7


_IMMEDIACY_RE = re.compile(
    r"\b(?:just (?:got|came|arrived) (?:back|home)|just returned|just (?:finished|completed|"
    r"wrapped up|attended|went to|visited|had|did|saw|watched|tried|bought|received|got)\b"
    r"|today|this (?:morning|afternoon|evening)|tonight|earlier today|yesterday|last night)\b",
    re.IGNORECASE,
)
_IMMEDIACY_MAX_DAYS = 3


def clamp_immediacy_anchors(
    facts: list[ExtractedFact], observation_date: str = ""
) -> list[ExtractedFact]:
    """A statement of immediacy ("just got back from
    Universal Studios", "yesterday", "this morning") happened within days of
    the turn that says it. When the model's anchor for such a fact is not
    pinned (temporal_confidence < 1) and lies more than three days before the
    observation date, the anchor is the observation date. Otherwise "just got back from
    … Universal Studios Hollywood" can be anchored 2023-06-24 at confidence 0 in a
    session dated 2023-07-15, while its siblings from the same turn sit on 07-15 at 1.0,
    and "how many weeks ago" reads 6 for 3. A pinned anchor and a fact without an anchor
    pass through; "yesterday" and "last night" resolve to the day before."""
    if not facts or not observation_date:
        return facts
    try:
        obs = datetime.fromisoformat(str(observation_date)[:10]).date()
    except ValueError:
        return facts
    out: list[ExtractedFact] = []
    changed = False
    for f in facts:
        m = _IMMEDIACY_RE.search(f.value or "")
        if not m or not f.temporal_anchor or f.temporal_confidence >= 1.0:
            out.append(f)
            continue
        try:
            anchor = datetime.fromisoformat(str(f.temporal_anchor)[:10]).date()
        except ValueError:
            out.append(f)
            continue
        phrase = m.group(0).lower()
        target = obs - timedelta(days=1) if phrase in ("yesterday", "last night") else obs
        if (obs - anchor).days > _IMMEDIACY_MAX_DAYS:
            out.append(replace(f, temporal_anchor=target.isoformat(), temporal_confidence=0.5))
            changed = True
        else:
            out.append(f)
    return out if changed else facts


_TEXT_DAY_NOTE_RE = re.compile(r"\((?:[^()]*?,\s*)?(?:on|around|as of|in)\s+(20\d\d-\d\d-\d\d)\)")
_TEXT_DAY_MAX_GAP = 2


def align_anchor_to_text_date(facts: list[ExtractedFact]) -> list[ExtractedFact]:
    """The day a fact's text names is the day the
    fact holds. The grounding passes write "(around 2023-03-01)" into the
    value from the phrase's resolution, but a restatement in a later session
    ("recently attended a music festival in Brooklyn", said 2023-04-15)
    keeps its own session day as `valid_at`, so the store carries a row whose text says
    March and whose date says April; a reader calls such an event "recorded
    inconsistently" and drops it from an ordering. When the value carries such a note
    and the row's anchor or valid_at lies more than two days from that day, both take
    the day the text names. A pinned anchor (confidence 1) is left."""
    if not facts:
        return facts
    out: list[ExtractedFact] = []
    changed = False
    for f in facts:
        m = _TEXT_DAY_NOTE_RE.search(f.value or "")
        if not m or f.temporal_confidence >= 1.0:
            out.append(f)
            continue
        day = m.group(1)
        try:
            text_dt = datetime.fromisoformat(day).date()
        except ValueError:
            out.append(f)
            continue
        current = (f.valid_at or f.temporal_anchor or "")[:10]
        try:
            cur_dt = datetime.fromisoformat(current).date() if current else None
        except ValueError:
            cur_dt = None
        if cur_dt is not None and abs((cur_dt - text_dt).days) <= _TEXT_DAY_MAX_GAP:
            out.append(f)
            continue
        out.append(replace(f, valid_at=f"{day}T00:00:00", temporal_anchor=f"{day}T00:00:00"))
        changed = True
    return out if changed else facts


def _apply_relative_date_grounding(
    facts: list[ExtractedFact], observation_date: str = ""
) -> list[ExtractedFact]:
    """Append the resolved anchor to values that still carry a relative
    phrase and no ISO date (memory.domain.extraction_temporal.ground_relative_phrase).
    A vague offset ("a few years ago") also caps temporal_confidence at 0.5:
    the anchor is an estimate, and the rubric's 1.0 means the source pinned
    the day. Facts without an anchor or without a phrase pass through. The
    immediacy clamp (`clamp_immediacy_anchors`) runs first so the phrase is grounded
    to the corrected anchor."""
    facts = clamp_immediacy_anchors(facts, observation_date or "")
    out: list[ExtractedFact] = []
    changed = False
    for f in facts:
        if not f.value:
            out.append(f)
            continue
        # A duration ("for 3 years now") needs no anchor: the start it
        # implies is arithmetic on the session date. Whatever
        # the model anchored — the start or the session — the text now says
        # both, so a reader never subtracts the duration a second time.
        dur = (
            duration_note(f.value, observation_date or "")
            if not _ISO_IN_TEXT_RE.search(f.value)
            else None
        )
        if dur:
            out.append(replace(f, value=append_note(f.value, dur)))
            changed = True
            continue
        if not f.temporal_anchor:
            out.append(f)
            continue
        cls = relative_phrase_class(f.value)
        if not cls:
            out.append(f)
            continue
        new_value = ground_relative_phrase(f.value, f.temporal_anchor, observation_date or "")
        conf = f.temporal_confidence
        if cls == "vague" and conf > 0.5:
            conf = 0.5
        if new_value != f.value or conf != f.temporal_confidence:
            out.append(replace(f, value=new_value, temporal_confidence=conf))
            changed = True
        else:
            out.append(f)
    return align_anchor_to_text_date(out if changed else facts)
