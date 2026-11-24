"""Isolate native Claude Code prompt caches between successive Harbor attempts."""

import asyncio
import json
import math

DEFAULT_CACHE_TTL_S = 300
LONG_CACHE_TTL_S = 3600


def first_prompt_verified(record):
    reads = record.get("first_cache_read_tokens")
    writes = record.get("first_cache_write_tokens")
    return (
        record.get("solver_token_source") == "anthropic-reported"
        and type(reads) is int
        and reads == 0
        and type(writes) is int
        and writes > 0
    )


def prompt_caching_disabled(record):
    return record.get("solver_token_source") == "anthropic-reported" and all(
        type(value) is int and value == 0
        for value in (
            record.get("first_cache_read_tokens"),
            record.get("first_cache_write_tokens"),
            record.get("model_cache_read_tokens"),
            record.get("model_cache_write_tokens"),
        )
    )


def study_cache_verified(protocol, entries):
    policy = protocol.get("prompt_cache_isolation")
    if not policy or len(entries) != len(protocol["schedule"]):
        return None
    verify = prompt_caching_disabled if policy == "disabled" else first_prompt_verified
    return all(verify(entry["result"]) for entry in entries)


def observed_cache_ttl_s(record):
    writes = record.get("model_cache_write_tokens")
    if record.get("model_cache_write_1h_tokens", 0) or writes != record.get(
        "model_cache_write_5m_tokens"
    ):
        return LONG_CACHE_TTL_S
    return DEFAULT_CACHE_TTL_S


async def wait_for_expiry(remaining_s, next_order):
    if remaining_s <= 0:
        return
    seconds = math.ceil(remaining_s)
    print(
        json.dumps({"phase": "prompt-cache-expiry", "next_order": next_order, "wait_s": seconds}),
        flush=True,
    )
    await asyncio.sleep(seconds)
