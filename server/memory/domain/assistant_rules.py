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


# Words of a user turn that ask for a standing behaviour ("always answer me in French").
_ASKS = {"always", "never", "don't", "dont"}  # not a setting: a word list
# Words that open a question asking for a behaviour ("could you always ...?").
_POLITE = {"can", "could", "would", "will"}  # not a setting: a word list
# Words that name the user as the one a question asks something done to.
_ME = {"me", "us"}  # not a setting: a word list
# Words that ask for a behaviour before "you to" ("I want you to always ...").
_WANTS = {"want", "need"}  # not a setting: a word list
# Words that make a request or a rule ask for a behaviour not to happen.
_NEGATIONS = {"never", "don't", "dont", "doesn't", "not", "won't"}  # not a setting: a word list
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
    """The words of `sentence` from the ask word that opens an imperative, or none.
    A clause after a comma or semicolon opens one only with "always" ("From now on,
    always ..."): one that opens with never or don't usually leaves out its subject
    ("I called twice, never got my refund"). A question asks for a behaviour only
    when it opens with can, could, would or will you and asks for something done to
    the user ("Could you always answer me in French?"); "Do you always escalate
    refunds?" and "Will you always escalate refunds?" ask about a policy."""
    words = _words(sentence)
    if not sentence.endswith("?"):
        return _imperative_words(sentence, words)
    if not (words[:1] and words[0] in _POLITE and words[1:2] == ["you"]):
        return []
    requested = _imperative_words(sentence, words)
    return requested if _ME & set(requested) else []


def _imperative_words(sentence: str, words: list[str]) -> list[str]:
    """The words of `sentence` from the ask word that opens an imperative, or none."""
    for i, word in enumerate(words):
        if word in _ASKS and _opens_imperative(words, i):
            return words[i:]
    for match in re.finditer(r"[,;]\s", sentence):
        clause = _words(sentence[match.end() :])
        if clause[:1] == ["always"]:
            return clause
    return []


def _negative(words: list[str]) -> bool:
    """Whether `words` ask for a behaviour not to happen ("never escalate ...").
    "only" and "anything but" ask for the one behaviour they name ("Don't answer me
    in anything but French"), so they read as positive."""
    if "only" in words or "anything but" in " ".join(words):
        return False
    return bool(_NEGATIONS & set(words))


def user_requested(fact: dict[str, Any], messages: list[dict[str, Any]], cited: list[int]) -> bool:
    """Whether the user turn before the earliest cited assistant turn in `messages`
    (the session's earlier turns, then the request's) asks for the behaviour the
    rule states (an agreed request is the user's preference): a sentence whose
    requested words share at least two of the rule's words, and at least half of
    them, and that asks for it to happen or not as the rule says. A request for
    the opposite ("Never escalate my refund" before "I always escalate any refund")
    is not agreed to, so its rule is filed. It reads English requests only; in
    other languages only the extraction prompt keeps an agreed request from being
    filed."""
    asked = _user_turn_before(messages, min(cited))
    rule_words = _words(str(fact.get("value") or ""))
    rule = _stems(rule_words)
    for sentence in re.findall(r"[^.!?\n]+[.!?]*", asked):
        requested = _requested_words(sentence.strip())
        shared = rule & _stems(requested[1:])
        if len(shared) < 2 or 2 * len(shared) < len(rule):
            continue
        if _negative(requested) == _negative(rule_words):
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
