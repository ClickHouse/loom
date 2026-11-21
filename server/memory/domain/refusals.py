"""Fingerprints for refusing deleted values without storing their plaintext."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

from server.core.errors import ValidationError
from server.core.hashing import sha256_value

Fingerprint = tuple[str, int, str]
_WORDS = re.compile(r"\w+|[^\w\s]")
# not a setting: fail-closed CPU safety ceiling for complete consent verification.
_WORK_LIMIT = 250_000


def tokens(text: str) -> list[str]:
    return _WORDS.findall(unicodedata.normalize("NFKC", text).casefold())


def fingerprint(value: Any) -> list[Fingerprint]:
    """One canonical hash and, for a string, a normalized literal fingerprint."""
    out = [("value", 0, sha256_value(value))]
    if isinstance(value, str) and (words := tokens(value)):
        out.append(("literal", len(words), sha256_value(words)))
    return out


def inspect(value: Any) -> tuple[set[Fingerprint], list[list[str]]]:
    """Canonical subtrees and normalized string leaves, bounded by input work."""
    exact: set[Fingerprint] = set()
    text: list[list[str]] = []
    pending = [value]
    work = 0
    while pending:
        part = pending.pop()
        work += 1
        exact.add(("value", 0, sha256_value(part)))
        if isinstance(part, dict):
            pending.extend(part.values())
            pending.extend(part.keys())
        elif isinstance(part, list):
            pending.extend(part)
        elif isinstance(part, str):
            words = tokens(part)
            text.append(words)
            work += len(words)
        if work > _WORK_LIMIT:
            raise ValidationError("The value is too large to verify against refusal policy")
    return exact, text


def candidates(
    inspected: tuple[set[Fingerprint], list[list[str]]], sizes: Iterable[int]
) -> set[Fingerprint]:
    exact, texts = inspected
    result = set(exact)
    work = 0
    sizes = set(sizes)
    for words in texts:
        for size in sizes:
            work += max(0, len(words) - size + 1) * size
            if size <= 0 or work > _WORK_LIMIT:
                raise ValidationError("The value is too large to verify against refusal policy")
            result.update(
                ("literal", size, sha256_value(words[i : i + size]))
                for i in range(len(words) - size + 1)
            )
    return result


def matches(value: Any, patterns: Iterable[Fingerprint]) -> bool:
    """Check complete JSON subtrees and literal passages; refuse an incomplete scan."""
    patterns = set(patterns)
    return bool(
        patterns
        and patterns & candidates(inspect(value), [p[1] for p in patterns if p[0] == "literal"])
    )


EXTRACTION_CONTENT_FIELDS = frozenset(
    {
        "messages",
        "last_messages",
        "session_values",
        "extra_tags",
        "recalled_keys",
        "extraction_instructions",
        "record_schema",
        "pipeline_steps",
        "observation_date",
        "memory_key",
    }
)
EXTRACTION_PROTOCOL_FIELDS = frozenset(
    {
        "org",
        "namespace",
        "agent",
        "user_id",
        "session_id",
        "run_id",
        "max_tokens",
        "judge_extraction",
        "llm_cache",
        "detect_updates",
        "coverage_sweep",
        "agent_learnings",
        "defer",
        "valid_at",
    }
)


EXTRACTION_PROTOCOL_STEPS = frozenset(
    {
        "loom.fetch_namespace_categories",
        "loom.fetch_existing_memories",
        "loom.llm_extract",
        "loom.drop_recall_echoes",
        "loom.drop_session_echoes",
        "loom.drop_activity_narration",
        "loom.drop_non_durable",
        "loom.verify_durable",
        "loom.hash_dedup",
        "loom.bulk_write",
    }
)


def transcript_content(payload: Mapping[str, Any]) -> list[Any]:
    """Caller data, excluding the request's and nested models' protocol defaults."""
    result: list[Any] = []
    for field in ("messages", "last_messages"):
        for message in payload.get(field) or []:
            result.extend(message[k] for k in ("content", "name") if message.get(k))
            role = message.get("role")
            if role and role not in {
                "user",
                "assistant",
                "system",
                "developer",
                "tool",
                "function",
            }:
                result.append(role)
    for field in ("session_values", "extra_tags", "recalled_keys"):
        result.extend(payload.get(field) or [])
    for field in ("extraction_instructions", "observation_date", "memory_key"):
        if payload.get(field):
            result.append(payload[field])
    for name, spec in (payload.get("record_schema") or {}).items():
        result.append(name)
        if not isinstance(spec, str) or spec not in {"string", "number", "integer", "boolean"}:
            result.append(spec)
    for step in payload.get("pipeline_steps") or []:
        step_id = step.get("step_id")
        if "step_id" in step and not (
            isinstance(step_id, str) and step_id in EXTRACTION_PROTOCOL_STEPS
        ):
            result.append(step_id)
        if "config" in step:
            result.append(step["config"])
        arguments = {k: v for k, v in step.items() if k not in {"step_id", "config"}}
        if arguments:
            result.append(arguments)
    return result


def payload_fingerprints(payload: Mapping[str, Any]) -> set[Fingerprint]:
    result: set[Fingerprint] = set()
    for value in transcript_content(payload):
        exact, texts = inspect(value)
        result.update(exact)
        result.update(("literal", len(words), sha256_value(words)) for words in texts if words)
    return result
