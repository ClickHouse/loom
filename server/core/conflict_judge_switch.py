"""Whether the conflict judge runs.

`LOOM_MEMORY_CONFLICT_JUDGE` when set; otherwise whether this process can reach a
real LLM for the judge's model. Decided on each use, so a key saved from the
dashboard after boot turns the judge on.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

REAL_LLM_PROVIDERS: frozenset[str] = frozenset({"openai", "anthropic"})


def _judge_provider() -> str:
    """The provider the judge's calls go to (`conflicts.judge_peers`' model)."""
    from server.core.llm_provider import provider_for_model
    from server.settings import settings

    cheap = settings.memory_ask_synth_model_cheap.strip()
    return provider_for_model(cheap, settings.llm_provider) if cheap else settings.llm_provider


def _has_key(provider: str) -> bool:
    from server.core.provider_keys import provider_key

    return bool(provider_key(provider))


def _forwards_to_main() -> bool:
    """A read-replica completes LLM calls through the main Loom (whose key it
    cannot see)."""
    from server.settings import settings

    return bool(settings.read_replica) and bool(str(settings.read_replica_main_url).strip())


def conflict_judge_on() -> bool:
    """The switch every reader of the judge goes through."""
    from server.settings import settings

    if settings.memory_conflict_judge is not None:
        return bool(settings.memory_conflict_judge)
    provider = _judge_provider()
    return provider in REAL_LLM_PROVIDERS and (_has_key(provider) or _forwards_to_main())


def conflict_judge_reason() -> str:
    """Why the switch is where it is, for the boot log and `loom doctor`."""
    from server.core.provider_keys import KEY_NAMES
    from server.settings import settings

    if settings.memory_conflict_judge is not None:
        return "LOOM_MEMORY_CONFLICT_JUDGE is set"
    provider = _judge_provider()
    if provider not in REAL_LLM_PROVIDERS:
        return f"the judge's provider is {provider}"
    if _has_key(provider):
        return f"the judge's provider is {provider}, with its key"
    if _forwards_to_main():
        return f"the judge's provider is {provider}, called through the main Loom"
    return f"the judge's provider is {provider} but {KEY_NAMES[provider][0]} is not set"


def log_conflict_judge_resolution() -> None:
    """One boot line when LOOM_MEMORY_CONFLICT_JUDGE is unset: whether the judge
    runs, why, and how to turn it off."""
    from server.settings import settings

    if settings.memory_conflict_judge is not None:
        return
    log.info(
        "LOOM_MEMORY_CONFLICT_JUDGE unset — the conflict judge is %s (%s); "
        "set it to false to keep it off",
        "on" if conflict_judge_on() else "off",
        conflict_judge_reason(),
    )
