"""An answer is scored on what it cites, not only on what it says.

The citations carry a signal the answer text does not: whether the cited
evidence matches the gold evidence, and whether it sits in the question's
time window. `memory.ask` already cites (`memory_keys`); this module turns
the citations into two numbers computed without a model call:

- `temporal_consistency`: mean of the window score R_s over
  the cited memories' dates when the question resolved to a bounded window;
  1.0 means every cited row sits inside the window, negative means the
  answer leans on rows outside it. None when there is no window or no dated
  citation.
- `citation_concentration`: cited memories per cited session, normalised
  (1.0 when every citation comes from one session, → 0 as they scatter).

and one list, `cited_sessions`, so a bench can compute a Jaccard score
against gold evidence and a caller can open the sessions the answer used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from server.memory.domain.question_window import TimeWindow, logistic_window_score, window_gap_days


@dataclass
class Grounding:
    cited_sessions: list[str] = field(default_factory=list)
    cited: int = 0
    temporal_consistency: float | None = None
    citation_concentration: float | None = None


def _attr(h: Any, name: str, default: Any = "") -> Any:
    if isinstance(h, dict):
        return h.get(name, default)
    return getattr(h, name, default)


def _row_date(h: Any) -> date | None:
    for attr in ("temporal_anchor", "valid_at"):
        raw = str(_attr(h, attr, "") or "")
        if raw and not raw.startswith("1970"):
            try:
                return date.fromisoformat(raw[:10])
            except ValueError:
                continue
    return None


def grounding_of(
    cited_keys: list[str],
    memories: list[Any],
    *,
    window: TimeWindow | None = None,
    grace_days: int = 7,
    scale_days: int = 3,
) -> Grounding:
    by_key = {str(_attr(m, "memory_key", "")): m for m in memories}
    g = Grounding()
    sessions: list[str] = []
    scores: list[float] = []
    for k in cited_keys:
        m = by_key.get(str(k))
        if m is None:
            continue
        g.cited += 1
        sid = str(_attr(m, "session_id", "") or "")
        if sid:
            sessions.append(sid)
        d = _row_date(m)
        if d is not None and window is not None and window.bounded:
            scores.append(
                logistic_window_score(
                    window_gap_days(d, window), grace_days=grace_days, scale_days=scale_days
                )
            )
    g.cited_sessions = sorted(set(sessions))
    if scores:
        g.temporal_consistency = round(sum(scores) / len(scores), 4)
    if sessions:
        g.citation_concentration = round(1.0 / len(set(sessions)), 4)
    return g
