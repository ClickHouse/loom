"""Learnings queue candidates from rules an assistant states about itself.

Pure: the candidate's id, the assistant turns a rule cites and the bounded
quotes one supporter's conversation gives, the rationale a reviewer reads, and
which agent's reads a promoted rule serves (serves_agent, hits_to_ask).
"""

from __future__ import annotations

import uuid
from typing import Any

from server.knowledge.domain.documents import MAX_SEARCH_TOP_K
from server.memory.domain.sensitivity import reads_high

SOURCE_KIND = "assistant_turn"

# A supporter row keeps this much of the assistant quote as said and of the user turn
# before it.
_SAID_CHARS = 400  # not a setting: a bound on the assistant quote a supporter row keeps for review
_USER_CHARS = 200  # not a setting: a privacy bound on a user's words kept for review


def serves_agent(tags: list[str], agent: str) -> bool:
    """Whether a knowledge document reaches `agent`'s reads: a bot's promoted rule,
    tagged `agent:<bot>`, serves only that bot and a namespace-wide read (`*`),
    which spans every agent as memory reads do; every other document, a rule for
    every agent included, serves every agent."""
    if f"source-kind:{SOURCE_KIND}" not in tags:
        return True
    bots = [tag.removeprefix("agent:") for tag in tags if tag.startswith("agent:")]
    return not bots or agent == "*" or agent in bots


def hits_to_ask(top_k: int) -> int:
    """How many knowledge hits a read asks for again when hits were dropped from its
    first `top_k` (another bot's rule, a noise section), so that `top_k` remain, within
    knowledge.search's cap."""
    return min(top_k * 4, MAX_SEARCH_TOP_K)  # not a setting: headroom for dropped hits


def _folded(text: str) -> str:
    return " ".join(text.lower().split())


def normalized_value(value: str) -> str:
    """The rule as its candidate is keyed: case and spacing folded, a trailing
    period dropped."""
    return _folded(value).rstrip(".")


def candidate_id(org: str, namespace: str, agent: str, memory_key: str, value: str) -> str:
    """One candidate per (agent, key, rule text). The same rule restated in another
    session lands on the same candidate; a different rule under the same key is a
    sibling candidate, never a supporter of the first wording."""
    name = "\x1f".join((SOURCE_KIND, org, namespace, agent, memory_key, normalized_value(value)))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def _bounded(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def cited_assistant_turns(fact: dict[str, Any], messages: list[dict[str, Any]]) -> list[int]:
    """The assistant turns among the rule's `source_message_indices`, earliest
    first. A rule that cites none is not something the assistant said."""
    return sorted(
        {
            i
            for i in fact.get("source_message_indices") or []
            if 0 <= i < len(messages) and messages[i].get("role") == "assistant"
        }
    )


def cited_texts(
    fact: dict[str, Any], messages: list[dict[str, Any]], cited: list[int]
) -> tuple[str, str]:
    """What one supporter's conversation shows a reviewer, whole: the user turn
    before the earliest of the `cited` assistant turns, and the rule as said. `said`
    quotes only an assistant turn or the rule itself: the evidence span when a
    cited assistant turn contains it, else that turn, else the rule. Checked
    whole, then cut by `bounded_quotes`."""
    turns = [str(messages[i].get("content") or "") for i in cited]
    evidence = str(fact.get("evidence") or "")
    if evidence and any(_folded(evidence) in _folded(turn) for turn in turns):
        said = evidence
    else:
        said = turns[0]
    return _user_turn_before(messages, min(cited)), said or str(fact.get("value") or "")


def _user_turn_before(messages: list[dict[str, Any]], index: int) -> str:
    return next(
        (
            str(m.get("content") or "")
            for m in reversed(messages[:index])
            if m.get("role") == "user"
        ),
        "",
    )


def bounded_quotes(asked: str, said: str) -> tuple[str, str]:
    """The quotes a supporter row stores, each bounded. A quote that reads as high
    is left blank, so no secret lands in the queue."""
    # Read whole: a high shape the cut would split no longer reads as high.
    return (
        "" if reads_high(asked) else _bounded(asked, _USER_CHARS),
        "" if reads_high(said) else _bounded(said, _SAID_CHARS),
    )


def rationale(asked: str, said: str) -> str:
    """A supporter's quotes as the reviewer reads them, one per line."""
    lines = [f'User: "{asked}"'] if asked else []
    if said:
        lines.append(f'Assistant: "{said}"')
    return "\n".join(lines)
