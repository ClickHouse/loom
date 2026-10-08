"""How precisely a fact's date is pinned by its source.

``temporal_anchor`` says WHEN a fact is about; this grade says HOW SURE the
extractor was of that date. "On May 7 2026" and "a while back" both land
as one DateTime, so without a grade the date-match boost treats them alike.
The extractor grades the source phrasing against a fixed rubric (below); the
server coerces that grade, stores it beside the anchor on both memory tables,
and scales the anchor boost by it. Rows from paths that never grade carry
0 = unscored and keep the full boost, so scoring only ever LOWERS the weight
of a date the extractor itself called vague.
"""

from __future__ import annotations

from typing import Any

# The rubric the extraction prompt states. Grades are properties of the
# source phrasing, not of the model's guess; kept here so tests and the
# prompt agree on the ladder.
RUBRIC: tuple[tuple[str, float], ...] = (
    ("iso", 1.0),  # full date, or today/yesterday against a known observation_date
    ("year_month", 0.9),  # "in March 2023"
    ("year", 0.85),  # "in 2019"
    ("quarter", 0.75),  # "Q2 2024"
    ("season", 0.65),  # "last summer"
    ("relative", 0.5),  # "a few weeks ago", "recently"
    ("vague", 0.35),  # "a while back", "years ago"
)

# Stored value meaning "no grade": imports,
# memory.set, and extractor output that omitted the field.
UNSCORED = 0.0


def coerce(raw: Any, *, has_anchor: bool) -> float:
    """Clamp an extractor-supplied grade to [0, 1]; UNSCORED when absent,
    non-numeric, or when the fact has no anchor to be confident about."""
    if not has_anchor or raw is None or isinstance(raw, bool):
        return UNSCORED
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return UNSCORED
    if value != value:  # NaN
        return UNSCORED
    return round(min(1.0, max(0.0, value)), 4)


def anchor_weight(confidence: float) -> float:
    """Multiplier applied to the date-match boost: the grade itself, or 1.0
    for an unscored row (never penalise history for lacking a grade)."""
    return confidence if confidence > 0 else 1.0


def anchor_weight_sql(column: str = "e.temporal_confidence") -> str:
    """ClickHouse expression mirroring :func:`anchor_weight`."""
    return f"if({column} > 0, {column}, 1.0)"
