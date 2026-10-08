"""Pure parse / text / fusion-math helpers for memory.search.

State-light, ClickHouse-client-free helpers: date-column normalization,
reciprocal-rank fusion math, record-plane tokenization + excerpt building,
and the deterministic predicate-class floor under verb-synonym expansion.
"""

from __future__ import annotations

import json

from server.memory.domain.search_text import _RECORD_QUERY_STOPWORDS
from server.memory.domain.search_types import SearchQuery

# Deterministic predicate-class floor under LLM verb-synonym expansion.
# Verbs in one class describe the same event from different angles, and a
# query routinely phrases one side while extraction stored the other
# ("total SPENT on X" vs a stored `acquire` record). Surface forms are
# canonicalized lazily via canonicalize_verb to match stored stems.
_PREDICATE_CLASS_SURFACES: tuple[frozenset[str], ...] = (
    # Spend / acquire outflow.
    frozenset({"spend", "buy", "purchase", "pay", "acquire", "get", "order", "shop"}),
    # Visitation / participation: hosting a gathering is participating in it.
    frozenset({"visit", "attend", "go", "stop_by", "consult", "host", "throw"}),
    # Money inflow — symmetric counterpart to the spend/acquire outflow class.
    frozenset(
        {
            "raise",
            "earn",
            "collect",
            "make",
            "fundraise",
            "donate",
            "save",
            "give",
            "contribute",
            "pledge",
        }
    ),
)
_predicate_classes_canonical: list[frozenset[str]] | None = None


def expand_predicate_class(verb_canonical: str) -> set[str]:
    """Canonicalized predicate class(es) containing `verb_canonical`.

    Empty set when the verb belongs to no class. Input must already be
    canonicalized (`canonicalize_verb`), matching how loom.derived_facts
    stores predicates.
    """
    global _predicate_classes_canonical
    if not verb_canonical:
        return set()
    if _predicate_classes_canonical is None:
        from server.memory.domain.search_text import canonicalize_verb as _cv

        _predicate_classes_canonical = [
            frozenset(c for c in (_cv(v) for v in cls) if c) for cls in _PREDICATE_CLASS_SURFACES
        ]
    out: set[str] = set()
    for cls in _predicate_classes_canonical:
        if verb_canonical in cls:
            out |= cls
    return out


def _iso_date(value: str) -> str:
    """Normalize a CH-stringified date column to a wire ISO string.

    `coalesce(col, toDateTime(0))` returns "1970-01-01 00:00:00" when the
    source column is NULL; surface that sentinel as empty string so callers
    can treat "" as undated.
    """
    if not value or value.startswith("1970-01-01"):
        return ""
    return value


def _record_query_tokens(q: SearchQuery) -> list[str]:
    """Return content-bearing tokens for schema-record recall.

    Record rows are exact/system-of-record projections, so the query side
    should preserve operational nouns ("work", "job") that are bad entities
    but useful schema clues. Keep this separate from entity extraction.
    """

    raw_parts = [q.query_text or ""]
    if q.preference_attribute:
        raw_parts.append(q.preference_attribute)
    if q.entity_filter:
        raw_parts.extend(q.entity_filter)
    if q.query_anchor_iso:
        raw_parts.append(q.query_anchor_iso)

    tokens: list[str] = []
    cur: list[str] = []
    for ch_ in " ".join(raw_parts).lower():
        if ch_.isalnum():
            cur.append(ch_)
        else:
            if len(cur) >= 2:
                tokens.append("".join(cur))
            cur = []
    if len(cur) >= 2:
        tokens.append("".join(cur))

    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token in _RECORD_QUERY_STOPWORDS:
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= 32:
            break
    return out


def _record_excerpt(
    *,
    schema_name: str,
    memory_key: str,
    record_json: str,
    field_text: str,
) -> str:
    """Build a compact, agent-readable excerpt for a structured record."""

    fields: list[str] = []
    try:
        obj = json.loads(record_json or "{}")
    except json.JSONDecodeError:
        obj = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            if value is None:
                continue
            text = str(value)
            if not text:
                continue
            fields.append(f"{key}={text}")
            if len(fields) >= 12:
                break
    if not fields and field_text:
        fields = [part.strip() for part in field_text.split(" | ") if part.strip()][:12]

    head = f"{schema_name} record"
    if memory_key:
        head = f"{head} for {memory_key}"
    if not fields:
        return head
    return f"{head}: " + "; ".join(fields)
