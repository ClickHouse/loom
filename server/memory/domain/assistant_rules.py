"""Learnings queue candidates from rules an assistant states about itself.

Pure: the candidate's id and the rationale that quotes the assistant turn.
"""

from __future__ import annotations

import uuid
from typing import Any

SOURCE_KIND = "assistant_turn"

# The rationale quotes this much of the assistant turn.
_QUOTE_CHARS = 400


def normalized_value(value: str) -> str:
    """The rule as its candidate is keyed: case and spacing folded."""
    return " ".join(value.lower().split()).rstrip(".")


def candidate_id(org: str, namespace: str, agent: str, memory_key: str, value: str) -> str:
    """One candidate per (agent, key, rule text). The same rule restated in another
    session lands on the same candidate; a different rule under the same key is a
    sibling candidate, never a supporter of the first wording."""
    name = "\x1f".join((SOURCE_KIND, org, namespace, agent, memory_key, normalized_value(value)))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def rationale(fact: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    """Why the candidate exists: the span the extractor cited as evidence, else the
    first assistant turn the fact cites, else the rule itself."""
    quote = str(fact.get("evidence") or "")
    if not quote:
        for idx in fact.get("source_message_indices") or []:
            if 0 <= idx < len(messages) and messages[idx].get("role") == "assistant":
                quote = str(messages[idx].get("content") or "")
                break
    quote = " ".join((quote or str(fact.get("value") or "")).split())
    if len(quote) > _QUOTE_CHARS:
        quote = quote[: _QUOTE_CHARS - 1] + "…"
    return f'Stated in an assistant turn: "{quote}"'
