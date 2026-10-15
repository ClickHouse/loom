"""Pure, ClickHouse-free temporal-visibility oracles shared by the
bi-temporal benches (`fama_forgetting.py`, `fact_consolidation.py`,
`mutation_sequence.py`).

These mirror — byte-for-byte in intent — the as-of WHERE clauses the
server already ships, so a bench can decide *locally* whether a row
SHOULD have surfaced at a given event-time before it ever asserts what
Loom actually returned. Keeping the oracle here (not inline in each
harness) means the boundary semantics are unit-testable without a live
CH, and every harness agrees on what "visible as-of T" means.

The triple / facts plane (`facts.list`, `loom.entity_triples`) is a true
interval test against `(valid_from, valid_to)`:
    (valid_from IS NULL OR valid_from <= valid_as_of)
    AND (valid_to IS NULL OR valid_to >= valid_as_of)
— see server/memory/app/facts.py (handle_facts_list). BOTH bounds are
inclusive, so at exactly valid_as_of == valid_to the closed interval is still
considered visible (the closing fact's valid_from == the closed fact's
valid_to). Forgetting must therefore be asserted *strictly after* the close
instant, which `forgetting_violation` enforces by construction.

The memory plane answers point-in-time reads from its bi-temporal journal
(`valid_as_of` / `known_as_of`, docs/architecture/bitemporal-memory.md).
"""

from __future__ import annotations

from datetime import datetime


def _as_dt(value: datetime | str | None) -> datetime | None:
    """Coerce an ISO-8601 string (or datetime, or None) to a naive UTC
    datetime, matching how the server normalizes its DateTime params.
    Loom stores naive-UTC; a trailing 'Z' or offset is stripped to
    naive so comparisons never mix aware/naive (which would raise)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    text = str(value).strip()
    if not text:
        return None
    if "T" in text or " " in text:
        text = text.replace("Z", "+00:00").replace(" ", "T", 1)
        dt = datetime.fromisoformat(text)
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    # Date-only: anchor to start-of-day, matching the server's
    # date-only legacy normalization for valid_from.
    return datetime.fromisoformat(text)


def triple_visible_as_of(
    valid_from: datetime | str | None,
    valid_to: datetime | str | None,
    as_of: datetime | str,
) -> bool:
    """The triple/facts-plane interval oracle.

    Returns True iff a triple with the given (valid_from, valid_to)
    interval is visible at `as_of`, replicating the inclusive-both-ends
    predicate in handle_facts_list / handle_memory_record_query. NULL
    bounds mean open (always-true on that side)."""
    vf = _as_dt(valid_from)
    vt = _as_dt(valid_to)
    at = _as_dt(as_of)
    assert at is not None, "as_of must be a concrete instant"
    if vf is not None and vf > at:
        return False
    if vt is not None and vt < at:
        return False
    return True


def forgetting_violation(
    *,
    valid_from: datetime | str | None,
    valid_to: datetime | str | None,
    as_of: datetime | str,
    surfaced: bool,
) -> bool:
    """FAMA core: a *violation* is surfacing a memory whose validity
    interval does NOT contain `as_of` — i.e. retrieval relied on an
    invalidated fact. Returns True when the harness saw the row but the
    oracle says the interval is closed before `as_of`.

    The symmetric case (oracle says visible but the row was missing) is
    a recall miss, not a forgetting violation; callers score that
    separately so the FAMA metric stays a clean *staleness* signal."""
    expected = triple_visible_as_of(valid_from, valid_to, as_of)
    return surfaced and not expected


def step_field_ok(
    *,
    expected_value: object,
    expected_state: str,
    observed_value: object,
    observed_state: str,
) -> bool:
    """Decide whether one field matches its mutation-step expectation.

    A 'value' state must match the value (with light numeric/string
    coercion so 32 == 32.0 == "32"); any non-'value' state (unknown /
    rejected / absent — i.e. a *cleared* field) is matched on the state
    label alone, since cleared fields carry no comparable value."""
    if expected_state != observed_state:
        return False
    if expected_state != "value":
        return True
    return _values_equal(expected_value, observed_value)


def _values_equal(a: object, b: object) -> bool:
    if a is None or b is None:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    # Numeric coercion: 32, 32.0, "32" should all compare equal because
    # the typed-record store splits values across value_number /
    # value_string and re-stringifies through record_json.
    try:
        return float(a) == float(b)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()
