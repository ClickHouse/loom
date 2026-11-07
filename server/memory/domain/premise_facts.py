"""Facts a user's question presumes ("since I moved to Denver, ..."), lifted
from the messages as premises."""

from __future__ import annotations

import re
from typing import Any

from server.memory.domain.forget_requests import (
    _SENTENCE_SPLIT_RE,
    _content_tokens,
)

# A premise the assistant states about the user is read from
# the assistant's own sentence. "Since you enjoy podcasts about world
# history, you might like…" — the extractor prompt says this is the user's fact
# (Example 37) but the model rarely writes it; the sentence says it in plain
# words.
_PREMISE_RE = re.compile(
    r"^(?:(?:oh|well|sure|great|of course|absolutely|certainly|got it)[,!]?\s+)?"
    r"(?:(?:since|given that|because|knowing that|considering that|seeing that)\s+"
    r"you(?P<be>'re|’re|\s+are)?|(?P<who>as someone who))\s+(?P<clause>[^,;:.!?]{3,140}?)"
    r"(?=\s*[,;:.!?]|\s+(?:you|i|here|it|there|we|let|this|that|so)\b)",
    re.IGNORECASE,
)
_PREMISE_ADVERBS = frozenset(
    "already still usually often regularly currently typically mostly generally always also "
    "really clearly obviously now".split()
)
_PREMISE_TRAIT_VERBS = frozenset(
    "enjoy like love prefer dislike hate avoid value prize collect keep have maintain practice "
    "follow wear use drink eat read listen watch play speak attend travel live work own run swim "
    "bake cook garden study teach volunteer struggle suffer tend monitor manage take rely favor "
    "favour appreciate cherish seek pursue grow raise care celebrate observe support".split()
)
_PREMISE_BE_OK_RE = re.compile(
    r"^(?:a|an|allergic|vegetarian|vegan|lactose|gluten|diabetic|pregnant|retired|married|"
    r"single|new to|fond of|into|based in|from|from a|originally|left-handed|colou?r-blind|"
    r"monitoring|training|learning|studying|managing|recovering|raising|caring|living|working as|"
    r"someone who|the kind of|a fan of|partial to|sensitive to|prone to|keen on|passionate about|"
    r"interested in|used to|accustomed to)\b",
    re.IGNORECASE,
)
_PREMISE_EXCLUDE_RE = re.compile(
    r"\b(?:want|wants|wanted|need the|need a|need this|looking for|asked|mentioned|said|after "
    r"something|trying to|working on|writing|drafting|preparing|translating|planning to|"
    r"about to|going to|here|this email|this message|this note|this post|the email|the "
    r"message|the note|the post|the text|the draft)\b",
    re.IGNORECASE,
)
_PREMISE_SLUG_STOP = frozenset(
    "a an the and or of to for in on at by with about from your you already still usually often "
    "regularly currently typically mostly generally always also really now very quite some any "
    "both every each their them they".split()
)


def _third_person(verb: str) -> str:
    v = verb.lower()
    irregular = {"have": "has", "do": "does", "go": "goes", "be": "is"}
    if v in irregular:
        return irregular[v]
    if v.endswith("y") and len(v) > 2 and v[-2] not in "aeiou":
        return v[:-1] + "ies"
    if v.endswith(("s", "x", "z", "ch", "sh")):
        return v + "es"
    return v + "s"


def _base_verb(word: str) -> str:
    """ "enjoys" → "enjoy", "studies" → "study", "watches" → "watch", "has" → "have"."""
    w = word.lower()
    if w in {"has": 1, "does": 1, "goes": 1, "is": 1}:
        return {"has": "have", "does": "do", "goes": "go", "is": "be"}[w]
    if w in _PREMISE_TRAIT_VERBS:
        return w
    if w.endswith("ies") and w[:-3] + "y" in _PREMISE_TRAIT_VERBS:
        return w[:-3] + "y"
    if w.endswith("es") and w[:-2] in _PREMISE_TRAIT_VERBS:
        return w[:-2]
    if w.endswith("s") and w[:-1] in _PREMISE_TRAIT_VERBS:
        return w[:-1]
    return w


def _premise_value(be: str | None, clause: str, *, third: bool = False) -> tuple[str, str] | None:
    """("User enjoys podcasts about world history", "enjoy") from the clause
    after "since you"; None when the clause is not a durable trait. `third`
    marks an "as someone who <verb>s …" clause, already in the third person."""
    words = clause.split()
    if not words or _PREMISE_EXCLUDE_RE.search(clause):
        return None
    if third:
        i = 0
        while i < len(words) - 1 and words[i].lower() in _PREMISE_ADVERBS:
            i += 1
        verb = _base_verb(words[i])
        if verb not in _PREMISE_TRAIT_VERBS and verb != "be":
            return None
        value = "User " + clause
        value = re.sub(r"\byour\b", "their", value, flags=re.IGNORECASE)
        return value.rstrip(" .") + ".", verb
    if be:
        if not _PREMISE_BE_OK_RE.match(clause):
            return None
        rest = clause
        verb = "be"
        value = "User is " + rest
    else:
        i = 0
        while i < len(words) - 1 and words[i].lower() in _PREMISE_ADVERBS:
            i += 1
        verb = words[i].lower().strip("'’")
        if verb not in _PREMISE_TRAIT_VERBS:
            return None
        head = " ".join(words[:i])
        rest = " ".join(words[i + 1 :])
        value = (
            "User "
            + (head + " " if head else "")
            + _third_person(verb)
            + (" " + rest if rest else "")
        )
    value = re.sub(r"\byourself\b", "themselves", value, flags=re.IGNORECASE)
    value = re.sub(r"\byours\b", "theirs", value, flags=re.IGNORECASE)
    value = re.sub(r"\byour\b", "their", value, flags=re.IGNORECASE)
    value = re.sub(r"\byou\b", "they", value, flags=re.IGNORECASE)
    return value.rstrip(" .") + ".", verb


def _premise_key(value: str) -> str:
    toks = [
        w
        for w in re.findall(r"[a-z0-9]+", value.lower())
        if w not in _PREMISE_SLUG_STOP and w not in {"user", "is", "has", "have"}
    ]
    return "user_" + "_".join(toks[:5]) if toks else ""


# Each premise is compared with every one before it.
MAX_PREMISES = 50  # not a setting: more than a window's assistant turns state as premises

_PREMISE_PREFERENCE_VERBS = frozenset(
    "enjoy like love prefer dislike hate avoid value prize collect practice follow favor favour "
    "appreciate cherish seek pursue".split()
)


_PREMISE_PREPOSITIONS = frozenset("about for in at on with of to from during while when".split())


def _premise_attribute(value: str, verb: str) -> str:
    """The head noun of what the preference is about — the word before the
    first preposition of the object, else its last word: "podcasts about
    world history" → podcast, "fresh fruit juices" → juice, "swimming for
    exercise in the summer" → swimming."""
    obj = value.lower().split(_third_person(verb), 1)[-1]
    words = [
        w
        for w in re.findall(r"[a-z][a-z-]*", obj)
        if w in _PREMISE_PREPOSITIONS or w not in _PREMISE_SLUG_STOP
    ]
    if not words:
        return ""
    head = ""
    for i, w in enumerate(words):
        if w in _PREMISE_PREPOSITIONS and i > 0:
            head = words[i - 1]
            break
    if not head:
        nouns = [w for w in words if w not in _PREMISE_PREPOSITIONS]
        if not nouns:
            return ""
        head = nouns[-1]
    head = head.strip("-")
    if head.endswith("s") and len(head) > 4 and not head.endswith("ss"):
        head = head[:-1]
    return head


def premise_facts_from_messages(
    messages: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    existing: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The user's facts an assistant turn states as its premise, as
    extraction-shaped facts keyed `user_…`, sourced to the
    assistant's turn, tagged `premise` (and `preference` + its attribute
    when the verb is one), for the write step to add to the batch. A premise
    the extractor already wrote (half its content words present in a batch
    fact) or the store already holds is not written again; a premise about
    the task ("since you want the email shorter") is not a fact."""
    out: list[dict[str, Any]] = []
    # Only the user's own facts count as "already written": the assistant's
    # recommendation carries the premise's words ("audio series about world
    # history") and must not suppress the user's fact.
    have: list[set[str]] = [
        _content_tokens(str(f.get("value") or ""))
        for f in list(facts or []) + list(existing or [])
        if str(f.get("key") or f.get("memory_key") or "").startswith("user_")
        or re.match(r"^\s*\"?User\b", str(f.get("value") or ""))
    ]
    seen: set[str] = set()
    for idx, m in enumerate(messages or []):
        if str(m.get("role") or "").lower() != "assistant":
            continue
        text = str(m.get("content") or "")[:1200]
        for sent in _SENTENCE_SPLIT_RE.split(text):
            mt = _PREMISE_RE.match(sent.strip().strip("\"'“”‘’*"))
            if not mt:
                continue
            made = _premise_value(
                mt.group("be"), mt.group("clause").strip(), third=bool(mt.group("who"))
            )
            if not made:
                continue
            value, verb = made
            want = _content_tokens(value)
            if len(want) < 2:
                continue
            if any(len(want & h) / len(want) >= 0.5 and len(want & h) >= 2 for h in have):
                continue
            key = _premise_key(value)
            if not key or key in seen:
                continue
            seen.add(key)
            have.append(want)
            tags = ["premise"]
            if verb in _PREMISE_PREFERENCE_VERBS:
                attr = _premise_attribute(value, verb)
                tags = ["preference", *(["preference:" + attr] if attr else []), "premise"]
            out.append(
                {
                    "key": key,
                    "value": value,
                    "tags": tags,
                    "about": "user",
                    "source_message_indices": [idx],
                }
            )
            if len(out) == MAX_PREMISES:
                return out
    return out
