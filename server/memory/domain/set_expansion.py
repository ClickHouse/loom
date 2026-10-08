"""Set-question instance expansion for memory.search.

A question that asks for a SET — "What martial arts has John done?",
"What desserts has Maria made?", "Which cities have both Jean and John
visited?", "How many tournaments has Nate won?" — names a category, while
the rows that answer it name instances: "John is doing kickboxing", "John is
off to do some taekwondo". One embedding of the category question sits at a
middling distance from every instance row, and the instances lose the top-k
cut to rows that merely mention the entity. On multi-hop conversation questions at K=10, half
the misses had the gold rows in memory but outside the top 10, most of this
shape.

`is_set_question` is the trigger memory.ask reads; the instance phrases
memory.search runs as extra vector legs through the sub-query fusion are
the deterministic collective-noun ones. An LLM expansion of the category into
invented instances measured no gain, so there is none.
"""

from __future__ import annotations

import re

# ---- trigger ---------------------------------------------------------------

_WH = r"(?:what|which)"
# "what books", "which cities", "what martial arts", "what kinds of food"
_PLURAL_HEAD_RE = re.compile(
    rf"^\s*{_WH}\s+(?:(?:kinds?|types?|sorts?)\s+of\s+)?(?:[a-z\-]+\s+){{0,2}}[a-z\-]+(?:s|es|ies)\b",
    re.IGNORECASE,
)
# "what ... has/have <name> done/read/seen/made/visited/bought/tried/played"
_HAS_DONE_RE = re.compile(
    rf"^\s*{_WH}\b.*\b(?:has|have|had|did|does|do)\b.*\b"
    r"(?:done|read|seen|watched|made|cooked|baked|visited|been to|bought|purchased|tried|"
    r"played|attended|joined|written|written|taken|adopted|recommended|received|given|"
    r"participated|competed|won|learned|studied|collected|owned|met|helped|hosted|planned|"
    r"gone|travell?ed|practi[cs]ed)\b",
    re.IGNORECASE,
)
_HOW_MANY_RE = re.compile(r"^\s*how\s+many\b", re.IGNORECASE)
# "the total number of siblings", "number of books I read"
_NUMBER_OF_RE = re.compile(r"\b(?:total\s+)?number\s+of\s+[a-z]", re.IGNORECASE)
# Collective nouns whose instances are stored under their own words: a row
# never says "sibling", it says "sister" or "brother".
_COLLECTIVES: dict[str, tuple[str, ...]] = {
    "siblings": ("sister", "brother", "sibling"),
    "sibling": ("sister", "brother"),
    "parents": ("mother", "father", "mom", "dad", "parent"),
    "children": ("son", "daughter", "child", "kid"),
    "kids": ("son", "daughter", "child", "kid"),
    "grandparents": ("grandmother", "grandfather", "grandma", "grandpa"),
    "grandchildren": ("grandson", "granddaughter", "grandchild"),
    "pets": ("dog", "cat", "pet", "puppy", "kitten", "bird", "fish", "hamster", "rabbit"),
    "in-laws": ("mother-in-law", "father-in-law", "sister-in-law", "brother-in-law"),
    "cousins": ("cousin",),
    "aunts and uncles": ("aunt", "uncle"),
}
_BOTH_RE = re.compile(
    r"\bboth\s+[A-Z][a-z]+\s+and\s+[A-Z][a-z]+\b|\b[A-Z][a-z]+\s+and\s+[A-Z][a-z]+\s+both\b"
)
_IN_COMMON_RE = re.compile(r"\b(?:in common|share|both)\b", re.IGNORECASE)
# Singular-answer shapes that the plural regex must not catch.
_SINGLE_RE = re.compile(
    r"^\s*(?:what|which)\s+(?:is|was|are|were)\b"
    r"|^\s*(?:when|where|why|how (?:long|old|much|far|often))\b"
    r"|^\s*who\s+(?:is|was|did|has|does)\b",
    re.IGNORECASE,
)
# "Who have written notes to Maria?", "Who are Nate's friends?": a plural who.
_WHO_PLURAL_RE = re.compile(r"^\s*who\s+(?:have|are|were)\b", re.IGNORECASE)
_NAME_RE = re.compile(r"\b([A-Z][a-z]{2,})\b")
_NOT_NAMES = {
    "What",
    "Which",
    "When",
    "Where",
    "Who",
    "Why",
    "How",
    "Did",
    "Does",
    "Has",
    "Have",
    "The",
    "Are",
    "Was",
    "Were",
    "And",
    "Both",
    "Many",
    "Kind",
    "Kinds",
    "Type",
    "Types",
}


def is_set_question(question: str) -> bool:
    """Deterministic: does the question ask for a set of instances (or their count)?"""

    q = (question or "").strip()
    if not q:
        return False
    if _HOW_MANY_RE.search(q) or _NUMBER_OF_RE.search(q):
        return True
    if _BOTH_RE.search(q) or (_IN_COMMON_RE.search(q) and len(question_entities(q)) >= 2):
        return True
    if _WHO_PLURAL_RE.search(q):
        return True
    if _SINGLE_RE.search(q):
        return False
    if _PLURAL_HEAD_RE.search(q):
        return True
    return bool(_HAS_DONE_RE.search(q))


def question_entities(question: str) -> list[str]:
    """Capitalised names in the question, in order, deduplicated."""

    out: list[str] = []
    for m in _NAME_RE.finditer(question or ""):
        w = m.group(1)
        if w in _NOT_NAMES or w in out:
            continue
        out.append(w)
    return out[:4]


# ---- collective nouns ------------------------------------


def collective_terms(question: str) -> list[str]:
    """The instance words for every collective noun the question names
    ("siblings" -> sister, brother, sibling). Deterministic."""
    q = (question or "").lower()
    out: list[str] = []
    for coll, parts in _COLLECTIVES.items():
        if re.search(rf"\b{re.escape(coll)}\b", q):
            for t in parts:
                if t not in out:
                    out.append(t)
    return out


def collective_phrases(question: str) -> list[str]:
    """Instance-row phrases for the question's collective nouns, shaped like
    the rows the extractor writes ("User has a sister")."""
    return [
        f"has a {t}"
        for t in collective_terms(question)
        if t not in ("sibling", "parent", "child", "kid", "pet", "cousin")
    ][:8]
