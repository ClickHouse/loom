"""Pure model-setting policy for single-provider onboarding."""

from collections.abc import Mapping, Sequence
from typing import Any


def committed_configuration(rows: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """A reset is a committed revision, not permission to resurrect older saves."""
    return next((row for row in rows if row["state"] in ("active", "environment")), None)


def pending_configuration(rows: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """A failed rebuild still owns the transition until explicitly recovered."""
    return next(
        (row for row in rows if row["state"] in ("queued", "rebuilding", "validating", "failed")),
        None,
    )


def committed_descendant(rows: Sequence[dict[str, Any]], latest: str, ancestor: str) -> bool:
    """A subsequent key-only save may follow a rebuild before a replica refreshes."""
    by_id = {
        str(row["config_id"]): row
        for row in rows
        if row["state"] in ("active", "environment") and "config_id" in row
    }
    seen: set[str] = set()
    while latest in by_id and latest not in seen:
        if latest == ancestor:
            return True
        seen.add(latest)
        latest = str(by_id[latest].get("parent_id", ""))
    return False


def llm_setup_settings(
    provider: str,
    model: str,
    overrides: Mapping[str, str],
    verification_models: Sequence[str] | None,
) -> dict[str, str]:
    """Resolve an explicit setup without changing production env defaults.

    Clear old per-job overrides so switching providers cannot keep using a prior
    provider unintentionally. Empty optional synthesis tiers remain disabled.
    Verification stays enabled on the selected main model unless explicitly changed.
    The caller supplies validated field names from LLMSetup.
    """
    values = dict.fromkeys(
        (
            "llm_fallback_model",
            "memory_ask_synth_model",
            "memory_ask_synth_model_cheap",
            "memory_ask_mid_model",
            "profile_consolidation_model",
            "reflection_distill_model",
            "session_brief_llm_model",
            "code_ask_llm_model",
            "code_search_hyde_model",
            "code_search_concept_expand_model",
            "code_bisect_llm_rerank_model",
            "code_summary_model",
            "tracker_dupcheck_rerank_model",
            "tracker_dupcheck_judge_model",
            "retrieval_judge_model",
        ),
        "",
    )
    values.update(overrides)
    values.update(
        llm_provider=provider,
        llm_model=model,
        memory_extract_verify_models=",".join(
            [model] if verification_models is None else verification_models
        ),
    )
    return values
