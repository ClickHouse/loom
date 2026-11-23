"""Measure and separate consecutive cold and warm Claude Code trials."""

from pathlib import Path

from .runtime import read

CACHE_5M_SECONDS = 300
CACHE_1H_SECONDS = 3600


def verified_start(expected, measured):
    reads = measured.get("first_cache_read_tokens")
    writes = measured.get("first_cache_write_tokens")
    if type(reads) is not int or type(writes) is not int:
        return False
    if expected == "cold":
        return reads == 0 and writes > 0
    return expected == "warm" and reads > 0 and writes >= 0


def pair_usage(schedule, observed):
    """Retain first-response counters for each consecutive provider cache pair."""
    if not schedule or schedule[0].get("cache_start") != "cold":
        return []
    pairs = []
    for cold, warm in zip(schedule[::2], schedule[1::2], strict=True):
        first = observed.get(cold["order"], {})
        second = observed.get(warm["order"], {})
        cold_reads = first.get("first_cache_read_tokens")
        cold_writes = first.get("first_cache_write_tokens")
        warm_reads = second.get("first_cache_read_tokens")
        warm_writes = second.get("first_cache_write_tokens")
        pairs.append(
            {
                "arm": cold["arm"],
                "cold_order": cold["order"],
                "warm_order": warm["order"],
                "cold_first_read_tokens": cold_reads,
                "cold_first_write_tokens": cold_writes,
                "warm_first_read_tokens": warm_reads,
                "warm_first_write_tokens": warm_writes,
                "first_prefix_token_count_match": warm_reads == cold_writes and warm_writes == 0
                if verified_start("cold", first) and verified_start("warm", second)
                else None,
            }
        )
    return pairs


def quiet_period(trial: Path):
    """Use the longest reported lease, or the provider maximum if omitted."""
    path = trial / "agent/trajectory.json"
    if not path.is_file():
        return CACHE_1H_SECONDS
    usages = [
        step["metrics"].get("extra") or {}
        for step in read(path)["steps"]
        if step.get("metrics") and not step.get("is_copied_context")
    ]
    if not usages or any(
        (u.get("cache_creation_input_tokens", 0) and not u.get("cache_creation"))
        or (u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0)
        for u in usages
    ):
        return CACHE_1H_SECONDS
    return CACHE_5M_SECONDS
