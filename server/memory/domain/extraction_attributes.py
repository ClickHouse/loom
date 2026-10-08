"""A stated age is a fact of its own.

"As a 32-year-old Digital Marketing Specialist …", "I'm 32 and officially
in my early thirties", "I'm 32 years old, and I'm considering going back to
school": the age is the aside and the extractor keeps the request. About
half of stated ages reach no fact of their turn, and multi-session and
temporal questions need them ("how many years older is my grandma", "how old
was I when I moved"); a missed statement can even leave a stale age stored.

The recovery is deterministic: a first-person age statement in a user
turn whose number appears in no fact citing that turn yields one row,
"User is 32 years old (as of <session day>).", under `user_age`, cited to
the turn. One row per turn; a later turn's age is the newer statement and
takes the same key, so the same-key supersession orders them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_AGE_RE = re.compile(
    r"\b(?:I(?:'m| am) (?:a |an |now )?(?P<a>\d{2})"
    r"(?:-year-old| years old|(?= (?:and|,|\.|but|so)))"
    r"|as a (?P<b>\d{2})-year-old"
    r"|I turned (?P<c>\d{2})\b"
    r"|(?P<d>\d{2})[- ]year[- ]old (?:male|female|man|woman|guy|girl)"
    r"|(?:think|consider|feel|say) (?:that )?(?P<e>\d{2}) is "
    r"(?:considered |still |too )?(?:young|old))",
    re.IGNORECASE,
)


def stated_age(text: str) -> str | None:
    """The user's own age a turn states in the first person, or None."""
    for m in _AGE_RE.finditer(text or ""):
        age = next(g for g in m.groups() if g)
        if 13 <= int(age) <= 99:
            return age
    return None


def recover_stated_age(
    facts: Sequence[Any],
    messages: Sequence[Mapping[str, Any]] | None,
    said_on_for: Any,
    make: Any,
) -> list[Any]:
    """`facts` plus one `user_age` row per user turn whose stated age appears
    in no fact citing that turn. `said_on_for(index)` gives the turn's day
    ("" when unknown); `make(key, value, index)` builds the row."""
    if not messages:
        return list(facts)
    out = list(facts)
    for idx, m in enumerate(messages):
        if str(m.get("role") or "user").lower() not in ("user", "human", ""):
            continue
        age = stated_age(str(m.get("content") or ""))
        if age is None:
            continue
        turn_facts = [
            f for f in facts if idx in (getattr(f, "source_message_indices", None) or [])
        ] or list(facts)
        if any(re.search(rf"\b{age}\b", str(getattr(f, "value", "") or "")) for f in turn_facts):
            continue
        if any(getattr(f, "memory_key", "") == "user_age" for f in out):
            continue
        day = str(said_on_for(idx) or "")
        note = f" (as of {day})" if day else ""
        out.append(make("user_age", f"User is {age} years old{note}.", idx))
    return out
