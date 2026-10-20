"""Learnings queue candidates from rules an assistant states about itself.

Pure: the candidate's id, the supporter entry a conversation adds, and the
rationale that quotes the assistant turn.
"""

from __future__ import annotations

import uuid
from typing import Any

SOURCE_KIND = "assistant_turn"

# The rationale quotes this much of the assistant turn.
_QUOTE_CHARS = 400


def candidate_id(org: str, namespace: str, agent: str, memory_key: str) -> str:
    """One candidate per (org, namespace, agent, key): the same rule restated in
    another session lands on the same row and adds a supporter."""
    name = "\x1f".join((SOURCE_KIND, org, namespace, agent, memory_key))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def supporter(
    org: str, namespace: str, agent: str, user_id: str, session_id: str, memory_key: str
) -> str:
    """The conversation backing a candidate, in the queue's six-part supporter form."""
    return "/".join((org, namespace, agent, user_id, session_id, memory_key))


def rationale(fact: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    """Why the candidate exists: the first assistant turn the fact cites, quoted,
    else the extractor's evidence span, else the rule itself."""
    quote = ""
    for idx in fact.get("source_message_indices") or []:
        if 0 <= idx < len(messages) and str(messages[idx].get("role") or "") == "assistant":
            quote = str(messages[idx].get("content") or "")
            break
    quote = quote or str(fact.get("evidence") or "") or str(fact.get("value") or "")
    quote = " ".join(quote.split())
    if len(quote) > _QUOTE_CHARS:
        quote = quote[: _QUOTE_CHARS - 1] + "…"
    return f'Stated in an assistant turn: "{quote}"'


def add_supporter(
    row: dict[str, Any], entry: str, user_id: str, confidence: float
) -> dict[str, Any] | None:
    """`row`'s columns with one more supporter, or None when the candidate was
    already decided or this conversation already backs it. A reviewer reads the
    count; it never promotes the candidate by itself."""
    supporters = list(row.get("supporters") or [])
    if row.get("status") != "pending" or entry in supporters:
        return None
    count = int(row.get("supporter_count") or len(supporters))
    users = list(row.get("supporter_users") or [])
    return {
        "supporters": [*supporters, entry],
        "supporter_count": count + 1,
        "supporter_users": users if user_id in users else [*users, user_id],
        "confidence_avg": (float(row.get("confidence_avg") or 0.0) * count + confidence)
        / (count + 1),
    }
