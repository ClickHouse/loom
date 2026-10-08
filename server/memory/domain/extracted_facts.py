"""Pure shaping of extracted facts before the bulk write: per-segment
extractions merged into one, one fact per key, source-role tags,
collision-free keys, a foresight window's end."""

from __future__ import annotations

from typing import Any

from server.core.datetimes import parse_iso_dt
from server.memory.domain.forget_requests import (
    is_forget_request,
)


def foresight_end(raw: Any, *, start: Any) -> Any:
    """The end of a foresight window from the extractor's
    `{"until": date|null, "duration_days": int|null}`: `until` when given,
    else `start + duration_days` when both are known. None for anything
    else — a malformed object, a non-positive duration, or a duration with
    no start — so a bad field never types a row as foresight."""
    if not isinstance(raw, dict):
        return None
    until = parse_iso_dt(raw.get("until"))
    if until is not None:
        return until
    days = raw.get("duration_days")
    try:
        n = int(days) if days is not None else 0
    except (TypeError, ValueError):
        return None
    if n <= 0 or start is None:
        return None
    from datetime import timedelta

    return start + timedelta(days=n)


def _source_role_tags(
    messages: list[dict[str, Any]],
    fact: dict[str, Any],
) -> list[str]:
    """Return role ownership tags from extractor source_message_indices."""

    raw_indices = fact.get("source_message_indices") or []
    if not isinstance(raw_indices, list):
        return []
    roles: list[str] = []
    seen: set[str] = set()
    for idx_raw in raw_indices:
        try:
            idx = int(idx_raw)
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(messages):
            continue
        role = str(messages[idx].get("role") or "").strip().lower()
        if not role:
            continue
        role = "".join(ch for ch in role if ch.isalnum() or ch in {"_", "-"})
        if not role or role in seen:
            continue
        seen.add(role)
        roles.append(role)
    if not roles:
        # An extracted fact the model did not tie to a turn is
        # still labelled, so "stated by the user" and "provenance unknown" are
        # distinguishable in the store (66% of preference rows were untagged).
        return ["source_role:unattributed"]
    if len(roles) == 1:
        return [f"source_role:{roles[0]}"]
    return ["source_role:mixed", *[f"source_role:{role}" for role in roles]]


# Memory keys are clamped to 64 chars of [a-z0-9_] by the extractor
# (_sanitize_key), so two distinct facts can collapse onto one key.
_KEY_MAX_LEN = 64


def keep_latest_per_key(
    facts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Pure: one fact per key, the last stated wins. Returns
    the kept facts in their original order and the keys that were dropped,
    one entry per dropped fact. A request to forget is not a statement of
    the attribute: it neither supersedes nor is superseded —
    "I prefer coffee" followed by the forget entry the extractor keyed the
    same way must not lose the coffee."""
    last_index: dict[str, int] = {}
    for i, fact in enumerate(facts):
        key = str(fact.get("key", "")).strip()
        if key and not is_forget_request(fact):
            last_index[key] = i
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for i, fact in enumerate(facts):
        key = str(fact.get("key", "")).strip()
        if key and not is_forget_request(fact) and last_index.get(key) != i:
            dropped.append(key)
            continue
        kept.append(fact)
    return kept, dropped


def _disambiguate_key(key: str, taken: set[str]) -> str:
    """Smallest `<key>_N` (N >= 2) not in `taken`, kept within the key
    length cap so the result is as predictable as the sanitiser's output.
    Deterministic for a given input order — the same transcript yields the
    same keys on re-ingest."""
    n = 2
    while True:
        suffix = f"_{n}"
        candidate = key[: _KEY_MAX_LEN - len(suffix)].rstrip("_") + suffix
        if candidate not in taken:
            return candidate
        n += 1


def _merge_extractions(parts: list[tuple[int, Any]]) -> Any:
    """Combine per-segment extractions into one: facts concatenated in
    segment order with source_message_indices shifted back to whole-
    conversation indices and a `segment:<k>` tag, token counts and drop
    counters summed. Provider/model come from the first part."""
    from dataclasses import replace

    if len(parts) == 1:
        return parts[0][1]
    parts = sorted(parts, key=lambda p: p[0])
    facts: list[Any] = []
    tin = tout = tcached = dparse = djudge = 0
    for k, (offset, ex) in enumerate(parts):
        for f in ex.facts:
            idxs = [int(i) + offset for i in (f.source_message_indices or [])]
            tags = [*list(f.tags or []), f"segment:{k}"]
            facts.append(replace(f, source_message_indices=idxs, tags=tags))
        tin += int(ex.llm_result.tokens_in)
        tout += int(ex.llm_result.tokens_out)
        tcached += int(getattr(ex.llm_result, "cached_input_tokens", 0) or 0)
        dparse += int(getattr(ex, "dropped_parse", 0) or 0)
        djudge += int(getattr(ex, "dropped_judge_wrong", 0) or 0)
    first = parts[0][1]
    llm = replace(first.llm_result, tokens_in=tin, tokens_out=tout, cached_input_tokens=tcached)
    return replace(
        first, facts=facts, llm_result=llm, dropped_parse=dparse, dropped_judge_wrong=djudge
    )


def keep_latest_relation_keyed(
    facts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Pure: `keep_latest_per_key` over the keys every holder of which took
    its key from a single-valued relation (`key_from_relation`); other keys
    pass through untouched, so a generic key two distinct facts share keeps
    both (the writer disambiguates them)."""
    holders: dict[str, list[bool]] = {}
    for fact in facts:
        key = str(fact.get("key", "")).strip()
        if key and not is_forget_request(fact):
            holders.setdefault(key, []).append(bool(fact.get("key_from_relation")))
    relation_keys = {k for k, flags in holders.items() if len(flags) > 1 and all(flags)}
    if not relation_keys:
        return facts, []
    scoped = [f for f in facts if str(f.get("key", "")).strip() in relation_keys]
    kept, dropped = keep_latest_per_key(scoped)
    gone = {id(f) for f in scoped} - {id(f) for f in kept}
    return [f for f in facts if id(f) not in gone], dropped
