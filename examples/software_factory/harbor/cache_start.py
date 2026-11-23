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
