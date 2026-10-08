"""Per-request knobs of memory.search: the request's field when set, else the
deployment default."""

from __future__ import annotations

from typing import Any

from server.memory.api.models_search import MemorySearchInput


def _mention_boost_weight(input: MemorySearchInput) -> float:
    """The request's pool-consensus mention boost; unset or 0 = off."""
    return float(input.mention_boost or 0.0)


def _ground_relative_excerpts_enabled(input: MemorySearchInput) -> bool:
    """On unless the request's `ground_relative_dates` turns it off."""
    v = input.ground_relative_dates
    return True if v is None else bool(v)


def _reinforcement_weight(input: MemorySearchInput) -> float:
    """Per-request override or the server default for the reinforcement boost."""
    v = input.reinforcement_weight
    if v is not None:
        return float(v)
    from server.settings import settings as _s

    return float(_s.memory_search_reinforcement_weight)


def _recency_pool_weight(input: MemorySearchInput) -> float:
    """Per-request override or the server default for the recency pool re-rank; the namespace's
    retrieval_defaults land on the input before this runs, like every other defaulted knob."""
    v = input.recency_pool_weight
    if v is not None:
        return float(v)
    from server.settings import settings as _s

    return float(_s.memory_search_recency_pool_weight)


def _reinforcement_half_life(input: MemorySearchInput) -> float:
    v = input.reinforcement_half_life_days
    if v is not None:
        return float(v)
    from server.settings import settings as _s

    return float(_s.memory_search_reinforcement_half_life_days)


def _reinforcement_half_life_use_scale(input: MemorySearchInput) -> float:
    """How much the half-life grows with reinforcement; unset or 0 = fixed."""
    return float(input.reinforcement_half_life_use_scale or 0.0)


def _sensitivity_penalty(input: object) -> float:
    """Deployment default (`memory_search_sensitivity_demote`), zero when the
    query asks for a way to reach someone — then the labelled row IS the
    answer. Per-request `sensitivity_penalty` wins."""
    from server.memory.domain.sensitivity import contact_intent
    from server.settings import settings

    v = getattr(input, "sensitivity_penalty", None)
    if v is not None:
        return float(v)
    if contact_intent(str(getattr(input, "query", "") or "")):
        return 0.0
    return float(settings.memory_search_sensitivity_demote)


def _resolve_prior_cap(input: object) -> float:
    """Per-request prior_cap, falling back to the deployment default.
    None (unset) -> settings.memory_prior_cap; explicit value wins."""
    from server.settings import settings

    v = getattr(input, "prior_cap", None)
    return float(settings.memory_prior_cap) if v is None else float(v)


def _source_class_boost_eligible() -> list[str] | None:
    """LOOM_MEMORY_SEARCH_SOURCE_CLASS_BOOST_ELIGIBLE as a list, None when
    unset (every row eligible — the neutral default)."""
    from server.settings import settings as _s

    raw = str(_s.memory_search_source_class_boost_eligible)
    vals = [v.strip() for v in raw.split(",") if v.strip()]
    return vals or None


def _noise_floor(input: Any, calibration: Any) -> float:
    """The similarity floor for this read: the request's
    `min_similarity` (which the namespace's retrieval_defaults land on the
    request, like every defaulted knob), else the calibrated floor."""
    v = getattr(input, "min_similarity", None)
    if v is not None:
        return float(v)
    from server.memory.app.embedder_calibration import effective_noise_floor
    from server.settings import settings as _s

    return float(effective_noise_floor(_s.memory_search_min_similarity, calibration))


def _floor_mode(input: Any) -> str:
    """The request's floor_mode, else the deployment default."""
    from server.settings import settings as _s

    mode = getattr(input, "floor_mode", None) or _s.memory_search_floor_mode
    return mode if mode in ("readmit", "ids_only", "drop") else "readmit"
