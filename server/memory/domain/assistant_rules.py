"""Learnings queue candidates from rules an assistant states about itself.

Pure: the candidate's id, the quotes one supporter's conversation gives, and
the rationale a reviewer reads from them.
"""

from __future__ import annotations

import uuid
from typing import Any

from server.memory.domain.sensitivity import reads_high

SOURCE_KIND = "assistant_turn"

# A supporter row quotes this much of the rule as said and of the user turn before it.
_RULE_CHARS = 400  # not a setting: a privacy bound on a user's words kept for review
_USER_CHARS = 200  # not a setting: a privacy bound on a user's words kept for review


def normalized_value(value: str) -> str:
    """The rule as its candidate is keyed: case and spacing folded, a trailing
    period dropped."""
    return " ".join(value.lower().split()).rstrip(".")


def candidate_id(org: str, namespace: str, agent: str, memory_key: str, value: str) -> str:
    """One candidate per (agent, key, rule text). The same rule restated in another
    session lands on the same candidate; a different rule under the same key is a
    sibling candidate, never a supporter of the first wording."""
    name = "\x1f".join((SOURCE_KIND, org, namespace, agent, memory_key, normalized_value(value)))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def _bounded(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def quotes(fact: dict[str, Any], messages: list[dict[str, Any]]) -> tuple[str, str]:
    """What one supporter's conversation shows a reviewer: the user turn before the
    rule, and the rule as said (the span the extractor cited as evidence, else the
    cited assistant turn, else the rule itself), each bounded. A quote that reads as
    a credential is left blank, so no secret lands in the queue."""
    cited = [
        i
        for i in fact.get("source_message_indices") or []
        if 0 <= i < len(messages) and messages[i].get("role") == "assistant"
    ]
    at = cited[0] if cited else len(messages)
    said = str(fact.get("evidence") or "")
    if not said and cited:
        said = str(messages[at].get("content") or "")
    asked = next(
        (str(m.get("content") or "") for m in reversed(messages[:at]) if m.get("role") == "user"),
        "",
    )
    said = said or str(fact.get("value") or "")
    # Read whole: a credential the cut would split no longer reads as one.
    return (
        "" if reads_high(asked) else _bounded(asked, _USER_CHARS),
        "" if reads_high(said) else _bounded(said, _RULE_CHARS),
    )


def rationale(asked: str, said: str) -> str:
    """A supporter's quotes as the reviewer reads them, one per line."""
    lines = [f'User: "{asked}"'] if asked else []
    if said:
        lines.append(f'Assistant: "{said}"')
    return "\n".join(lines)
