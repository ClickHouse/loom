"""Whose fact a sentence states: the user's own, or someone the user names."""

from __future__ import annotations

import re
from typing import Any

# Whose fact a sentence states, read from its subject. The extractor almost never
# emits the `about` field it is asked for; the subject of the value says it every
# time.
_OWN_RELATION = (
    r"(?:best\s+)?(?:friend|colleague|coworker|co-worker|boss|manager|mentor|neighbou?r|"
    r"roommate|flatmate|partner|wife|husband|spouse|fianc[ée]e?|girlfriend|boyfriend|ex|"
    r"mother|mom|mum|father|dad|parents?|sister|brother|sibling|son|daughter|child|children|"
    r"kids?|cousin|aunt|uncle|grandmother|grandma|grandfather|grandpa|niece|nephew|teacher|"
    r"student|client|patient|doctor|therapist|professor|classmate|teammate|coach|pastor|"
    r"priest|supervisor|landlord|tenant|employee|intern|nanny|babysitter)"
)
_OWN_HONORIFIC = (
    r"(?:Mr|Mrs|Ms|Dr|Fr|Father|Sister|Brother|Prof|Professor|Aunt|Uncle|Coach|Pastor|Rev)\.?\s+"
)
_OWN_NAME = r"(?:" + _OWN_HONORIFIC + r")?[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}"
# The relation list is case-insensitive; the name that may follow it is not,
# or "reminded them to" would read as a name.
_OWN_USERS_RELATION_RE = re.compile(
    r"^User's\s+(?:[a-z]+\s+)?(?i:" + _OWN_RELATION + r")\b(?:,?\s+" + _OWN_NAME + r")?\s+[a-z]"
)
_OWN_NAME_RE = re.compile(r"^(?P<name>" + _OWN_NAME + r")\s+(?P<verb>[a-z]+)(?:\s+(?P<next>\S+))?")
_OWN_COPULA = frozenset("is are was were".split())
_OWN_STATE_WORDS = frozenset(
    "interested curious worried concerned excited happy sad tired busy unable able keen eager "
    "reluctant afraid proud grateful hopeful anxious stressed overwhelmed fond allergic vegetarian "
    "vegan married single retired pregnant new based from in at on not currently now still also "
    "already planning going considering thinking looking hoping trying struggling "
    "recovering".split()
)
# Verbs a person is the subject of; a keyed capitalised subject with any other
# next word ("Morning light exposure sharpens…") is a noun phrase, not a person.
_OWN_PERSON_VERBS = frozenset(
    "is are was were has had have feels felt likes liked loves loved enjoys enjoyed prefers "
    "preferred dislikes disliked hates wants wanted thinks thought believes struggles struggled "
    "suffers works worked lives lived studies studied plays played attends attended keeps kept "
    "practices values seeks avoids tends uses drinks eats reads listens watches collects owns "
    "runs plans hopes hoped needs experiences experienced finds found considers celebrates "
    "celebrated observes observed follows speaks teaches coaches volunteers grew moved asked "
    "requested mentioned said told wrote shared expressed described noted recommended suggested "
    "learns learned recently often always usually never sometimes currently typically will would "
    "can cannot could should might may does did do goes went takes took makes made gets got gave "
    "gives received receives sent sends invited invites complained complains faced faces handles "
    "handled dealt deals manages managed misses missed remembers remembered fears feared worries "
    "worried visited visits traveled travelled travels started starts stopped stops quit joined "
    "joins left leaves returned returns decided decides chose chooses agreed agrees refused "
    "refuses explained explains advised advises reminded reminds replied responded reported "
    "confirmed admitted announced".split()
)
_USER_NAME_RE = re.compile(r"^User(?:'s name is|\s+is\s+(?:called|named))\s+([^,;]+)")
_OWN_HONORIFIC_WORDS = frozenset(
    "mr mrs ms dr fr father sister brother prof professor aunt uncle coach pastor rev".split()
)
# "User's teacher said the drawing looked great", "User's doctor reminded
# them…": an episode involving the user, not another person's state.
_OWN_NAMING_RE = re.compile(
    r"^(?:is|was|are|were)\s+(?:named|called)\b|^name\s+is\b|^names?\s+(?:is|are)\b", re.IGNORECASE
)
_OWN_INTERACTION_VERBS = frozenset(
    "said says told tells gave gives reminded reminds asked asks invited invites sent sends wrote "
    "writes called calls helped helps taught teaches showed shows brought brings took takes "
    "rubbed handed hands offered offers thanked thanks praised praises encouraged encourages "
    "suggested suggests recommended recommends advised advises".split()
)


def user_names_in(facts: list[dict[str, Any]], existing: list[dict[str, Any]] | None) -> set[str]:
    """Lower-cased first names the store or this batch gives the user
    ("User's name is Mateo", key `user_name`), so a sentence with the user's
    own name as subject is still the user's."""
    names: set[str] = set()
    for row in list(existing or []) + list(facts or []):
        value = str(row.get("value") or "").strip().strip('"')
        key = str(row.get("memory_key") or row.get("key") or "")
        m = _USER_NAME_RE.match(value)
        source = m.group(1) if m else (value if key == "user_name" else "")
        for tok in re.findall(r"[A-Z][a-z]+", source):
            low = tok.lower()
            if low != "user" and low not in _OWN_HONORIFIC_WORDS:
                names.add(low)
    return names


def ownership_of(fact: dict[str, Any], *, user_names: set[str] | None = None) -> str | None:
    """ "user", "other", "assistant", or None (a fact about the world) from
    the subject of the fact's value. "User's friend Mark
    feels…" and "Kenji has difficulty sleeping" are another person's;
    "User's name is Mateo" and "Mateo attends speech class" (when Mateo is
    the user's name) are the user's; "Gallstones are solid deposits" is
    nobody's. Precision over recall: a capitalised subject is a person
    only with an honorific, or when the key is named after it and the verb
    is not a definition ("X is a …")."""
    value = str(fact.get("value") or "").strip().strip('"').strip()
    key = str(fact.get("memory_key") or fact.get("key") or "").strip().lower()
    names = user_names or set()
    if re.match(r"^User's\s", value):
        m_rel = _OWN_USERS_RELATION_RE.match(value)
        if not m_rel:
            return "user"
        rest = value[m_rel.end() - 1 :]
        verb = rest.split()[0].lower().strip(",.;:") if rest.split() else ""
        if verb in _OWN_INTERACTION_VERBS:
            return None
        # "User's sister is named Hazel", "User's cousin is called Finn": the
        # relative's name is the user's relationship fact, not the relative's
        # state.
        if _OWN_NAMING_RE.match(rest):
            return "user"
        return "other"
    if re.match(r"^User\b", value):
        return "user"
    if re.match(r"^(?:The\s+)?[Aa]ssistant\b", value) or key.startswith("assistant_"):
        return "assistant"
    m = _OWN_NAME_RE.match(value)
    if not m:
        return None
    name = m.group("name")
    verb = m.group("verb")
    nxt = (m.group("next") or "").lower().strip(",.;:")
    tokens = [t.lower().rstrip(".") for t in name.split()]
    if tokens[-1] in names or " ".join(tokens) in names:
        return "user"
    if key.startswith("user_"):
        return "user"
    honorific = bool(re.match(r"^" + _OWN_HONORIFIC, name))
    slug = "_".join(t for t in tokens if t)
    keyed = key.startswith(slug + "_") or key == slug
    if not (honorific or keyed):
        return None
    if not honorific and verb not in _OWN_PERSON_VERBS:
        return None
    if verb in _OWN_COPULA and not (nxt.endswith("ing") or nxt in _OWN_STATE_WORDS):
        return None
    return "other"
