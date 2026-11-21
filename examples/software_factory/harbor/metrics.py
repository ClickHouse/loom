"""Aggregate recorded provider tokens without turning missing usage into zero."""

from __future__ import annotations

TOKEN_FIELDS = {
    "model_uncached_input_tokens": ("input_tokens", "inputTokens"),
    "model_output_tokens": ("output_tokens", "outputTokens"),
    "model_cache_read_tokens": ("cache_read_input_tokens", "cacheReadInputTokens"),
    "model_cache_write_tokens": ("cache_creation_input_tokens", "cacheWriteInputTokens"),
}


def known_total(records, key):
    values = [record.get(key) for record in records]
    return sum(values) if values and None not in values else None


def token_totals(result):
    usage = result.get("provider_usage")
    requests = result.get("provider_logical_requests")
    complete = (
        type(requests) is int
        and requests >= 0
        and isinstance(usage, list)
        and len(usage) == requests
        and result.get("provider_failed_requests") == 0
    )
    totals = {}
    for target, aliases in TOKEN_FIELDS.items():
        values = []
        for row in usage if complete else []:
            present = [row[name] for name in aliases if name in row]
            if not present or any(type(v) is not int or v < 0 or v != present[0] for v in present):
                break
            values.append(present[0])
        totals[target] = sum(values) if complete and len(values) == requests else None
    incoming = [
        totals[key]
        for key in (
            "model_uncached_input_tokens",
            "model_cache_read_tokens",
            "model_cache_write_tokens",
        )
    ]
    totals["model_input_tokens"] = sum(incoming) if None not in incoming else None
    combined = [totals["model_input_tokens"], totals["model_output_tokens"]]
    totals["model_total_tokens"] = sum(combined) if None not in combined else None
    return totals
