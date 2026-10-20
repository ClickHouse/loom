"""Learnings queue candidates from rules an assistant states about itself.

Pure: the candidate's id and the rationale that quotes the conversation.
"""

from __future__ import annotations

import uuid
from typing import Any

SOURCE_KIND = "assistant_turn"

# The rationale quotes this much of the assistant's rule and of the user turn before it.
_RULE_CHARS = 400
_USER_CHARS = 200


def normalized_value(value: str) -> str:
    """The rule as its candidate is keyed: case and spacing folded."""
    return " ".join(value.lower().split()).rstrip(".")


def candidate_id(org: str, namespace: str, agent: str, memory_key: str, value: str) -> str:
    """One candidate per (agent, key, rule text). The same rule restated in another
    session lands on the same candidate; a different rule under the same key is a
    sibling candidate, never a supporter of the first wording."""
    name = "\x1f".join((SOURCE_KIND, org, namespace, agent, memory_key, normalized_value(value)))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def _quote(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def rationale(fact: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    """What the reviewer reads: the user turn before the rule, so the prompt that
    drew it is in view, then the rule as said (the span the extractor cited as
    evidence, else the cited assistant turn, else the rule itself)."""
    cited = [
        i
        for i in fact.get("source_message_indices") or []
        if 0 <= i < len(messages) and messages[i].get("role") == "assistant"
    ]
    at = cited[0] if cited else len(messages)
    said = str(fact.get("evidence") or "")
    if not said and cited:
        said = str(messages[at].get("content") or "")
    lines = []
    asked = next(
        (str(m.get("content") or "") for m in reversed(messages[:at]) if m.get("role") == "user"),
        "",
    )
    if asked:
        lines.append(f'User: "{_quote(asked, _USER_CHARS)}"')
    lines.append(f'Assistant: "{_quote(said or str(fact.get("value") or ""), _RULE_CHARS)}"')
    return "\n".join(lines)
