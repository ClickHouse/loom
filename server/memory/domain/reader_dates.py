"""Read-time grounding of a relative phrase the stored text still carries.

The extractor writes the resolved date into the value ("attended a gala
yesterday (on 2023-11-16)") because a reader shown "[2023-11-16] … yesterday"
subtracts the day again. Rows whose extractor call did not honour that rule
still carry the bare phrase — on multi-session conversations 19–21 % of the fact
rows a temporal question retrieves do, and most sampled wrong-date answers were
exactly one period off in the shifting direction. The anchor
is already on the row (`temporal_anchor`, else `valid_at`, both set by the
same resolver at ingest); this appends it to the excerpt at read time with
the same deterministic function, so every reader — memory.ask, the
render="reader" block, an integrator's own prompt — sees the date the
phrase resolved to next to the phrase.

Only phrases the ingest resolver itself recognises are grounded (its
`valid_at` is then the resolved date); vague ones ("a while ago") and
future ones ("next Saturday", "tomorrow") are left alone, because for those
the row's date may be when the sentence was said. Pure; no I/O.
"""

from __future__ import annotations

import re
from typing import Any

from server.memory.domain.extraction_temporal import (
    _ISO_IN_TEXT_RE,
    ground_relative_phrase,
    resolve_relative_date,
)

# Verbatim and dense planes carry the SESSION date and unresolved speech;
# grounding them would pin "yesterday" to the day it was said.
_SKIP_SOURCES = frozenset(
    {"chunk", "summary", "pending", "timeline", "entity_summary", "digest", "knowledge"}
)
_SKIP_KEY_PREFIXES = ("chunk:", "summary:", "pending:", "entity_summary:", "digest:", "community:")
_FUTURE_RE = re.compile(
    r"\b(?:tomorrow|next (?:week|weekend|month|year|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)|the week after next|in two weeks|upcoming)\b",
    re.IGNORECASE,
)


def ground_excerpt(text: str, anchor_iso: str) -> str:
    """`text` with "(on YYYY-MM-DD)" / "(around …)" appended when it carries a
    past, closed-class relative phrase the resolver recognises and no ISO
    date of its own; unchanged otherwise."""
    if not text or not anchor_iso:
        return text
    day = str(anchor_iso).replace("T", " ")[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        return text
    if _ISO_IN_TEXT_RE.search(text) or _FUTURE_RE.search(text):
        return text
    # Recognised by the ingest resolver ⇒ the row's anchor is the resolved
    # date, not the utterance date. (The anchor passed here only tests
    # recognition; the appended date is the row's own.)
    if resolve_relative_date(text, day) is None:
        return text
    return ground_relative_phrase(text, day)


def _anchor_of(h: Any) -> str:
    for attr in ("temporal_anchor", "valid_at"):
        v = h.get(attr) if isinstance(h, dict) else getattr(h, attr, None)
        if v:
            return str(v)
    return ""


def ground_hits(hits: list[Any]) -> int:
    """Rewrite `content_excerpt` in place for eligible hits; returns how many
    changed. Works on attribute hits (pydantic / dataclass) and dicts."""
    changed = 0
    for h in hits:
        get = h.get if isinstance(h, dict) else (lambda k, _h=h: getattr(_h, k, None))
        source = str(get("source") or "")
        key = str(get("memory_key") or "")
        if source in _SKIP_SOURCES or key.startswith(_SKIP_KEY_PREFIXES):
            continue
        text = str(get("content_excerpt") or "")
        new = ground_excerpt(text, _anchor_of(h))
        if new != text:
            if isinstance(h, dict):
                h["content_excerpt"] = new
            else:
                h.content_excerpt = new
            changed += 1
    return changed


__all__ = ["ground_excerpt", "ground_hits"]
