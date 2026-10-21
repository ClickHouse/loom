"""Probation for content nobody endorsed.

A row whose `source_class` is `external` (a tool result, a fetched page, a
document the caller declared as such) may be retrieved and shown as a hit,
but it does not get to *speak* through memory.ask until a second party
stands behind it: a human or process attestation (`verified`). The write an
attacker controls is exactly the one an agent copied from its environment, and
a salient retrieved row can steer the answer. Keeping
it out of the synthesis pool until verified is the cheapest brake that
does not touch the stored vectors.
"""

from __future__ import annotations

from typing import Any


def is_probationary(hit: Any) -> bool:
    return str(getattr(hit, "source_class", "") or "") == "external" and not bool(
        getattr(hit, "verified", False)
    )


def exclude_unverified_external[H](hits: list[H]) -> tuple[list[H], int]:
    """Hits allowed into synthesis, and how many were held back."""
    kept = [h for h in hits if not is_probationary(h)]
    return kept, len(hits) - len(kept)


# Ranking term: how much a row's provenance moves its score
# when memory_search_trust_weight > 0. Asserted / observed content and an
# attestation lift; imported content sits slightly below; declared-external
# content sinks one unit. Everything else (extracted, derived, unknown) is 0.
TRUST_CLASS_SQL = (
    "multiIf(e.source_class = 'external', -1.0, "
    "e.source_class = 'imported', -0.25, "
    "e.source_class IN ('asserted', 'observed'), 0.5, 0.0)"
)


def trust_boost_sql(weight: float, params: dict[str, Any]) -> str:
    """SQL term for the rank expression; '0.0' when the lever is off so every
    search mode stays a strict no-op at the default."""
    if not weight or weight <= 0:
        return "0.0"
    params["trust_w"] = float(weight)
    return f"{{trust_w:Float32}} * ({TRUST_CLASS_SQL} + toFloat32(e.verified))"
