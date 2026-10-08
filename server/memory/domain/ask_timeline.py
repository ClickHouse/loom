"""A computed timeline for memory.ask: dates and deltas the model does not
have to do in its head.

Temporal arithmetic is a leading source of wrong answers ("how many days
ago", a yes/no about a dated visit): the synthesizer is asked to subtract dates
from a list of memories ordered by time (`evidence_order` sorts them), and
without this module nothing computes the differences.

This module renders, deterministically and with no LLM call, one line per
dated memory in the synthesis window: the date the memory is about, its
distance to the question date ("3 days before"), and the gap to the previous
dated memory. The synthesizer reads the arithmetic instead of performing it.
Undated memories are listed last as undated so the block never hides a row.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

_MAX_ROWS = 24


def memory_date(m: Any) -> date | None:
    """The date a memory is ABOUT: temporal_anchor first (the event date the
    extractor pinned), else valid_at, else created_at. None when undated."""

    for attr in ("temporal_anchor", "valid_at", "created_at"):
        v = getattr(m, attr, None)
        if not v:
            continue
        txt = str(v).replace("T", " ")[:10]
        try:
            return date.fromisoformat(txt)
        except ValueError:
            continue
    return None


def question_date(as_of: Any = None, observation_date: str = "") -> date:
    """The "now" the deltas are measured against: the as-of anchor, else the
    caller's observation_date, else today (UTC)."""

    if as_of is not None:
        try:
            return date.fromisoformat(str(as_of).replace("T", " ")[:10])
        except ValueError:
            pass
    if observation_date:
        try:
            return date.fromisoformat(str(observation_date)[:10])
        except ValueError:
            pass
    return datetime.now(UTC).date()


def _human_delta(days: int) -> str:
    """'3 days before', 'same day', '2 months after' — coarse units so the
    model is not tempted to over-read a day count that came from a
    date-only anchor."""

    if days == 0:
        return "same day as the question date"
    side = "before" if days > 0 else "after"
    n = abs(days)
    if n < 14:
        unit = f"{n} day" + ("s" if n != 1 else "")
    elif n < 60:
        w = round(n / 7)
        unit = f"{w} week" + ("s" if w != 1 else "") + f" ({n} days)"
    elif n < 730:
        mo = round(n / 30.44)
        unit = f"{mo} month" + ("s" if mo != 1 else "") + f" ({n} days)"
    else:
        y = round(n / 365.25, 1)
        unit = f"{y} years ({n} days)"
    return f"{unit} {side} the question date"


def timeline_block(
    memories: list[Any],
    *,
    now: date,
    max_rows: int = _MAX_ROWS,
) -> tuple[str, int]:
    """Render the timeline. Returns ``(block, dated_rows)``; an empty block
    when no memory in the window carries a date."""

    dated: list[tuple[date, Any]] = []
    undated: list[Any] = []
    for m in memories:
        d = memory_date(m)
        (dated if d is not None else undated).append((d, m) if d is not None else m)
    if not dated:
        return "", 0
    dated.sort(key=lambda p: p[0])
    lines = [f"Question date: {now.isoformat()}."]
    prev: date | None = None
    for d, m in dated[:max_rows]:
        delta = (now - d).days
        key = str(getattr(m, "memory_key", "") or "")
        conf = getattr(m, "temporal_confidence", None)
        pin = ""
        if isinstance(conf, (int, float)) and 0 < float(conf) < 0.7:
            pin = " (date pinned loosely by its source)"
        gap = ""
        if prev is not None:
            g = (d - prev).days
            gap = (
                f"; {g} day{'s' if g != 1 else ''} after the previous row"
                if g
                else "; same day as the previous row"
            )
        lines.append(f"- {d.isoformat()} [{key}]: {_human_delta(delta)}{gap}{pin}")
        prev = d
    if len(dated) > max_rows:
        lines.append(f"- … {len(dated) - max_rows} more dated memories not listed")
    if undated:
        keys = ", ".join(f"[{getattr(m, 'memory_key', '')}]" for m in undated[:8])
        more = f" and {len(undated) - 8} more" if len(undated) > 8 else ""
        lines.append(f"- undated: {keys}{more}")
    return "\n".join(lines), len(dated)


__all__ = ["memory_date", "question_date", "timeline_block"]
