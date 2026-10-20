"""The long-session scenario the conflict resolver is tested and measured on: one
user, twelve sessions a week apart, about sixty facts, and the ground truth for
each of them. Pure data; no I/O.

Read by `tests/integration/test_conflict_resolver_long_session_ch.py` (a scripted
judge over placed vectors) and `bench/conflict_resolver_long_session.py` (a real
server, its embedder and its judge).

- Six attributes (`ATTRIBUTES`) change once or twice, each change under a new
  key and in new words. The last value is current (`CURRENT`); every earlier
  one should end superseded behind the next (`OLDER`).
- Four facts are restated, four elaborated, four sit near a different fact, one
  pinned fact is contradicted later, one fact is deleted and brought back with
  Undo, and eighteen stand alone. All of them should stay live and unflagged
  (`KEPT`).
- `JUDGE` holds the right verdict for every pair a cluster should hold, except
  for the scripted judge's deliberate mistakes: two wrong answers at low
  confidence (`LOW_CONFIDENCE`) and one answer cut off mid-way (`MALFORMED`).
- `Fact.at` places each text in the test's vector space, where topic t owns four
  axes. Successive values of an attribute sit at cosine 0.75, between the
  resolver's 0.70 floor and the 0.85 conflict threshold, so the write only marks
  the row for the resolver. Pairs at 0.87 to 0.92 are also flagged `conflicted`
  by the write. The bench ignores `at`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

Part = tuple[str, int, float]  # (topic, axis within the topic, weight)


@dataclass(frozen=True)
class Fact:
    """One write: its session, key and words, its direction, and for an attribute's
    value the object of the (user, relation, object) fact it states."""

    session: int
    key: str
    value: str
    at: tuple[Part, ...]
    obj: str = ""
    pinned: bool = False


@dataclass(frozen=True)
class Attribute:
    """A single-valued attribute: the relation its values state, a question it
    answers, the topic that question points at, and its keys oldest first."""

    relation: str
    query: str
    topic: str
    keys: tuple[str, ...]


def chain(topic: str, step: int) -> tuple[Part, ...]:
    """The `step`-th value of an attribute: each 0.75 from every other."""
    return (
        ((topic, 0, 1.0),),
        ((topic, 0, 0.75), (topic, 1, 0.661)),
        ((topic, 0, 0.75), (topic, 1, 0.30), (topic, 2, 0.589)),
    )[step]


def alone(topic: str) -> tuple[Part, ...]:
    return ((topic, 0, 1.0),)


def near(topic: str, cos: float, axis: int = 1) -> tuple[Part, ...]:
    """A text at cosine `cos` from `alone(topic)`."""
    return ((topic, 0, cos), (topic, axis, math.sqrt(1 - cos * cos)))


F = Fact
FACTS: list[Fact] = [
    # The six attributes.
    F(1, "home_city", "The user lives in Paris", chain("home", 0), obj="paris"),
    F(5, "relocation", "The user moved to Lisbon for a new job", chain("home", 1), obj="lisbon"),
    F(
        10,
        "rome_lease",
        "The user signed a lease in Rome and lives there now",
        chain("home", 2),
        obj="rome",
    ),
    F(2, "employer", "The user works at Acme as a data engineer", chain("job", 0), obj="acme"),
    F(7, "new_role", "The user started a new role at Globex", chain("job", 1), obj="globex"),
    F(2, "manager", "The user reports to Ana", chain("mgr", 0), obj="ana"),
    F(6, "manager_change", "Ana left and the user now reports to Ben", chain("mgr", 1), obj="ben"),
    F(11, "reorg", "After the reorg the user reports to Chen", chain("mgr", 2), obj="chen"),
    F(
        3,
        "standup",
        "The user's daily standup is at nine thirty",
        chain("standup", 0),
        obj="nine thirty",
    ),
    F(
        8,
        "standup_moved",
        "The user's standup moved to quarter past ten for the US team",
        chain("standup", 1),
        obj="quarter past ten",
    ),
    F(4, "ci", "The user's team runs CI on GitLab", chain("ci", 0), obj="gitlab"),
    # Flagged by the write as well (0.88): the judge still retires the older one.
    F(
        9,
        "ci_migration",
        "The user's team moved CI over to Buildkite",
        near("ci", 0.88),
        obj="buildkite",
    ),
    F(1, "diet", "The user is vegetarian", chain("diet", 0), obj="vegetarian"),
    F(6, "diet_vegan", "The user went fully vegan in March", chain("diet", 1), obj="vegan"),
    F(
        12,
        "diet_fish",
        "The user eats fish again and is pescatarian now",
        chain("diet", 2),
        obj="pescatarian",
    ),
    # Restated: two flagged by the write, two only marked.
    F(2, "editor", "The user's favourite editor is Neovim", alone("editor")),
    F(8, "editor_again", "The user does all their editing in Neovim", near("editor", 0.90)),
    F(4, "cat", "The user's cat is called Miso", alone("cat")),
    F(10, "cat_again", "Miso is the name of the user's cat", near("cat", 0.92)),
    F(5, "language", "The user speaks Portuguese", alone("lang")),
    F(11, "language_again", "Portuguese is a language the user speaks", near("lang", 0.72)),
    F(6, "laptop", "The user's laptop is a ThinkPad", alone("laptop")),
    F(12, "laptop_again", "The user works on a ThinkPad laptop", near("laptop", 0.75)),
    # Elaborated: the newer keeps the older's fact and adds detail.
    F(1, "marathon", "The user is training for a marathon", alone("run")),
    F(
        7,
        "marathon_plan",
        "The user is training for the Porto marathon in April, running 50 km a week",
        near("run", 0.80),
    ),
    F(2, "rust", "The user is learning Rust", alone("rust")),
    F(
        8,
        "rust_plan",
        "The user is learning Rust through the async book and Advent of Code",
        near("rust", 0.76),
    ),
    F(3, "guitar", "The user plays guitar", alone("guitar")),
    F(
        9,
        "guitar_band",
        "The user plays guitar, mostly fingerstyle, in a weekend band",
        near("guitar", 0.88),
    ),
    F(5, "daughter", "The user's daughter is Lena", alone("kid")),
    F(
        11,
        "daughter_school",
        "The user's daughter Lena is seven and starts school in the fall",
        near("kid", 0.78),
    ),
    # Near a different fact; two of them near an attribute's first value.
    F(3, "sister_city", "The user's sister lives in Berlin", near("home", 0.72, axis=3)),
    F(4, "skip_level", "The user's skip-level manager is Priya", near("mgr", 0.73, axis=3)),
    F(4, "car", "The user drives a blue Golf", alone("car")),
    F(10, "partner_car", "The user's partner drives a red Polo", near("car", 0.87)),
    F(5, "dentist", "The user's dentist is Dr. Okafor", alone("doc")),
    F(11, "gp", "The user's GP is Dr. Mensah", near("doc", 0.72)),
    # Pinned, and contradicted later: a pinned row is never retired.
    F(3, "code_freeze", "Code freeze starts every Friday at noon", alone("freeze"), pinned=True),
    F(9, "freeze_change", "Code freeze now starts on Thursday evening", near("freeze", 0.75)),
    # Deleted in session 4, brought back with Undo in session 6, elaborated in 9.
    F(3, "badge", "The user's badge number is 4471", alone("badge")),
    F(9, "badge_lab", "Badge 4471 also opens the third-floor lab", near("badge", 0.75)),
    # Unrelated, each in its own direction: no peer, never judged.
    F(1, "seat", "The user prefers window seats", alone("solo1")),
    F(1, "allergy", "The user is allergic to penicillin", alone("solo2")),
    F(2, "dog", "The user has a dog named Rex", alone("solo3")),
    F(2, "coffee", "The user takes coffee black", alone("solo4")),
    F(3, "timezone", "The user's calendar is set to UTC", alone("solo5")),
    F(4, "keyboard", "The user types on a split keyboard", alone("solo6")),
    F(4, "podcast", "The user listens to a history podcast on commutes", alone("solo7")),
    F(5, "chess", "The user plays chess online on weekends", alone("solo8")),
    F(6, "plants", "The user keeps a fiddle-leaf fig on the desk", alone("solo9")),
    F(6, "birthday", "The user's birthday is in November", alone("solo10")),
    F(7, "shell", "The user's shell is zsh", alone("solo11")),
    F(7, "bike", "The user cycles to work when it is dry", alone("solo12")),
    F(8, "tea", "The user drinks green tea in the afternoon", alone("solo13")),
    F(9, "book", "The user is reading a novel by Le Guin", alone("solo14")),
    F(10, "theme", "The user uses a dark terminal theme", alone("solo15")),
    F(10, "holiday", "The user plans a trip to Japan next spring", alone("solo16")),
    F(11, "music", "The user works to ambient music", alone("solo17")),
    F(12, "notes", "The user keeps notes in plain Markdown files", alone("solo18")),
]
SESSIONS = 12
SOLO = {f.key for f in FACTS if f.at[0][0].startswith("solo")}

# The key deleted in a session, and the session whose Undo brings it back (after
# each session's writes).
DELETE_IN, RESTORE_IN = {4: "badge"}, {6: "badge"}

# The relations are not single-valued, so no write closes a fact on its own.
ATTRIBUTES: dict[str, Attribute] = {
    "home city": Attribute(
        "home_city",
        "Which city is the user's home?",
        "home",
        ("home_city", "relocation", "rome_lease"),
    ),
    "employer": Attribute(
        "works_for", "Which company does the user work for?", "job", ("employer", "new_role")
    ),
    "manager": Attribute(
        "reports_to", "Who is the user's manager?", "mgr", ("manager", "manager_change", "reorg")
    ),
    "standup time": Attribute(
        "standup_time", "When is the user's standup?", "standup", ("standup", "standup_moved")
    ),
    "ci provider": Attribute(
        "ci_provider", "What does the user's team run CI on?", "ci", ("ci", "ci_migration")
    ),
    "diet": Attribute(
        "diet", "What does the user eat?", "diet", ("diet", "diet_vegan", "diet_fish")
    ),
}

# The judge's answer per pair: (verdict, confidence, same_entity, same_attribute).
Verdict = tuple[str, float, bool, bool]
JUDGE: dict[frozenset[str], Verdict] = {
    **{
        frozenset(pair): ("update", 0.98, True, True)
        for a in ATTRIBUTES.values()
        for pair in zip(a.keys, a.keys[1:], strict=False)
    },
    frozenset({"editor", "editor_again"}): ("restatement", 0.97, True, True),
    frozenset({"cat", "cat_again"}): ("restatement", 0.98, True, True),
    frozenset({"laptop", "laptop_again"}): ("restatement", 0.95, True, True),
    frozenset({"language", "language_again"}): ("restatement", 0.97, True, True),
    frozenset({"marathon", "marathon_plan"}): ("elaboration", 0.96, True, True),
    frozenset({"rust", "rust_plan"}): ("elaboration", 0.95, True, False),
    frozenset({"guitar", "guitar_band"}): ("elaboration", 0.96, True, True),
    frozenset({"badge", "badge_lab"}): ("elaboration", 0.96, True, False),
    frozenset({"sister_city", "home_city"}): ("unrelated", 0.97, False, False),
    frozenset({"skip_level", "manager"}): ("related", 0.95, False, False),
    frozenset({"car", "partner_car"}): ("unrelated", 0.97, False, True),
    frozenset({"code_freeze", "freeze_change"}): ("update", 0.99, True, True),
    # The scripted judge's wrong answers, below the confidence floor.
    frozenset({"daughter", "daughter_school"}): ("contradiction", 0.85, True, True),
    frozenset({"dentist", "gp"}): ("update", 0.80, True, True),
}
# The rows whose judgement the scripted judge cuts off mid-answer.
MALFORMED = {"language_again"}
LOW_CONFIDENCE = {"daughter", "daughter_school", "dentist", "gp"}
# The compatible pairs the write flags by cosine: the resolver clears them.
FLAGGED_AT_WRITE = {
    frozenset({"editor", "editor_again"}),
    frozenset({"cat", "cat_again"}),
    frozenset({"guitar", "guitar_band"}),
    frozenset({"car", "partner_car"}),
}

CURRENT = {a.keys[-1] for a in ATTRIBUTES.values()}
OLDER = {k for a in ATTRIBUTES.values() for k in a.keys[:-1]}
KEPT = {f.key for f in FACTS} - CURRENT - OLDER
BY_KEY = {f.key: f for f in FACTS}


def relation_of(key: str) -> str:
    """The relation an attribute's value states; '' for any other fact."""
    return next((a.relation for a in ATTRIBUTES.values() if key in a.keys), "")


def dialogue(session: int) -> list[dict[str, str]]:
    """The session as a short chat: the user says each of its facts, the assistant
    acknowledges. The pinned fact is left out (it is written with a pin)."""
    turns: list[dict[str, str]] = []
    for f in FACTS:
        if f.session == session and not f.pinned:
            turns.append({"role": "user", "content": f"Something to remember: {f.value}."})
            turns.append({"role": "assistant", "content": "Noted."})
    return turns
