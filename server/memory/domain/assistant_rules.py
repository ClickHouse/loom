"""Learnings queue candidates from rules an assistant states about itself.

Pure: the candidate's id, the quotes one supporter's conversation gives, the
rationale a reviewer reads from them, and which agent's reads a promoted rule
serves (serves_agent, hits_to_ask).
"""

from __future__ import annotations

import re
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
    """How many knowledge hits a read asks for again when serves_agent dropped
    another bot's rule from its first `top_k`, so that `top_k` remain, within
    knowledge.search's cap."""
    return min(top_k * 4, MAX_SEARCH_TOP_K)  # not a setting: headroom for other bots' rules


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
    """The assistant turns among the rule's `source_message_indices`. A rule that
    cites none is not something the assistant said."""
    return [
        i
        for i in fact.get("source_message_indices") or []
        if 0 <= i < len(messages) and messages[i].get("role") == "assistant"
    ]


def cited_texts(
    fact: dict[str, Any], messages: list[dict[str, Any]], cited: list[int]
) -> tuple[str, str]:
    """What one supporter's conversation shows a reviewer, whole: the user turn
    before the first of the `cited` assistant turns, and the rule as said. `said`
    quotes only an assistant turn or the rule itself: the evidence span when a
    cited assistant turn contains it, else that turn, else the rule. Checked
    whole, then cut by `bounded_quotes`."""
    turns = [str(messages[i].get("content") or "") for i in cited]
    evidence = str(fact.get("evidence") or "")
    if evidence and any(_folded(evidence) in _folded(turn) for turn in turns):
        said = evidence
    else:
        said = turns[0]
    return _user_turn_before(messages, cited[0]), said or str(fact.get("value") or "")


def _user_turn_before(messages: list[dict[str, Any]], index: int) -> str:
    return next(
        (
            str(m.get("content") or "")
            for m in reversed(messages[:index])
            if m.get("role") == "user"
        ),
        "",
    )


# Words of a user turn that ask for a standing behaviour ("always answer me in French").
_ASKS = {"always", "never", "don't", "dont"}  # not a setting: a word list
# Words that open a question asking for a behaviour ("could you always ...?").
_POLITE = {"can", "could", "would", "will"}  # not a setting: a word list
# Words that ask for a behaviour before "you to" ("I want you to always ...").
_WANTS = {"want", "need"}  # not a setting: a word list
# Words a rule and a request share without naming the same behaviour.
_COMMON = set(
    "a an the to in on of for and or with me my i you your user user's assistant always"
    " never will please".split()
)  # not a setting: a word list


def _words(text: str) -> list[str]:
    # Mobile keyboards type the typographic apostrophe (U+2019) in "don't".
    text = text.lower().replace("\u2019", "'")
    return re.findall(r"[a-z0-9'$]+", re.sub(r"\bdo not\b", "don't", text))


def _stems(words: list[str]) -> set[str]:
    return {word.removesuffix("s") for word in words if word not in _COMMON}


def _opens_imperative(words: list[str], i: int) -> bool:
    """Whether the ask word at `words[i]` opens an imperative: first in the words,
    after "please" ("please always ..."), after "can/could/would/will you" ("could
    you always ...") or after "want/need you to" ("I want you to always ..."). After
    any other word ("I never ...", "You never told me ...") it states a fact."""
    if i == 0 or words[i - 1] == "please":
        return True
    if words[i - 1] == "you":
        return i >= 2 and words[i - 2] in _POLITE
    return words[i - 1] == "to" and i >= 3 and words[i - 2] == "you" and words[i - 3] in _WANTS


def _requested_words(sentence: str) -> list[str]:
    """The words after the ask word that opens an imperative in `sentence`, or none.
    A clause after a comma or semicolon opens one only with "always" ("From now on,
    always ..."): one that opens with never or don't usually leaves out its subject
    ("I called twice, never got my refund"). A question asks for a behaviour only
    when it opens with can, could, would or will you ("Could you always answer me in
    French?"); "Do you always escalate refunds?" asks about a policy."""
    words = _words(sentence)
    if sentence.endswith("?") and not (words[:1] and words[0] in _POLITE and words[1:2] == ["you"]):
        return []
    for i, word in enumerate(words):
        if word in _ASKS and _opens_imperative(words, i):
            return words[i + 1 :]
    for match in re.finditer(r"[,;]\s", sentence):
        clause = _words(sentence[match.end() :])
        if clause[:1] == ["always"]:
            return clause[1:]
    return []


def user_requested(fact: dict[str, Any], messages: list[dict[str, Any]], cited: list[int]) -> bool:
    """Whether the user turn before the first cited assistant turn asks for the
    behaviour the rule states (an agreed request is the user's preference): a
    sentence whose requested words share at least two of the rule's words, and at
    least half of them."""
    asked = _user_turn_before(messages, cited[0])
    rule = _stems(_words(str(fact.get("value") or "")))
    for sentence in re.findall(r"[^.!?\n]+[.!?]*", asked):
        shared = rule & _stems(_requested_words(sentence.strip()))
        if len(shared) >= 2 and 2 * len(shared) >= len(rule):
            return True
    return False


def bounded_quotes(asked: str, said: str) -> tuple[str, str]:
    """The quotes a supporter row stores, each bounded. A quote that reads as a
    credential is left blank, so no secret lands in the queue."""
    # Read whole: a credential the cut would split no longer reads as one.
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
