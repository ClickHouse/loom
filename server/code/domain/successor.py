"""The definition a linked one became when its name is gone: renamed alone (its shape is
the same), renamed with its body edited, or carried by a renamed class.

A rename with an edit cannot always be told from a removal plus an unrelated addition, so
a successor is named only on corroborated evidence: one new definition in the same scope
(or the scope's own successor) close to the old text, and in its place among the
definitions that stayed. When new definitions there may be it but none certainly is, the
answer is uncertain, never a removal.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from server.code.domain.anchor import Definition, bare_name, local_name, unnumbered

# how close the old and new text, names blanked, must be for an edited rename
_CLOSE = 0.6
# the share of a class's members a renamed class must keep to be its successor, and how
# many at least: one shared name is the method itself, no evidence about the class
_KEPT_MEMBERS = 0.5
_MIN_SHARED = 2
_TOKEN = re.compile(r"\w+|[^\w\s]")
# below this many tokens two definitions look alike by their shape alone: the new name
# must be kin to the old (`_kin`: `fetch_rows` -> `load_rows`, not -> `warm_up`)
_SMALL = 30
# a name's words; an operator's symbol is one (`operator+` is not `operator-`)
_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+|[^\w\s]+")
# the work one comparison may take: token by token up to this many tokens a side, line by
# line up to this many lines; past that the two are not compared and the answer is unsure
_MAX_TOKENS = 600
_MAX_LINES = 400


# the kinds that make a scope a class (a namespace or module is never renamed into one)
_CLASS_KINDS = {"class", "struct", "interface", "trait", "record", "enum"}


@dataclass(frozen=True)
class Successor:
    definition: Definition | None
    # the shape matched: only the name changed
    exact: bool = False
    # new definitions in its scope may be it; none certainly is
    uncertain: bool = False
    # carried by its renamed class: what surrounds it changed, so it reads changed
    carried: bool = False


@dataclass(frozen=True)
class Side:
    """One file at one commit: its definitions and its lines."""

    path: str
    defs: Sequence[Definition]
    lines: Sequence[str]

    def text(self, d: Definition) -> str:
        return "\n".join(self.lines[max(0, d.start_line - 1) : d.end_line])


def scope_of(d: Definition) -> str:
    """The class or namespace a definition is declared in, without its module."""
    return local_name(d.qualified_name, d.path).rpartition(".")[0]


def successor(was: Definition, before: Side, now: Side) -> Successor:
    """What `was` (in `before`) is in `now`, where its qualified name is gone."""
    # new since the check: by the name inside its module, so a file move renames nothing
    seen = {_unnumbered(d) for d in before.defs}
    fresh = []
    for d in now.defs:  # the first definition of each new name
        if (name := _unnumbered(d)) not in seen:
            seen.add(name)
            fresh.append(d)
    scope = scope_of(was)
    target, scope_unsure = scope, False
    if scope and scope not in {scope_of(d) for d in now.defs} | _names(now):
        target, scope_unsure = _renamed_scope(scope, before, now)
    # the other definitions gone from its scope: one new definition is the successor of
    # one of them at most, and a rename alone of another is that one's
    rivals = [
        d
        for d in _removed(before, now)
        if _unnumbered(d) != _unnumbered(was) and scope_of(d) == scope
    ]
    in_scope = [d for d in fresh if scope_of(d) == target]
    # a method keeps what surrounds it only when the class changed nothing but its name
    carried = target != scope and not _renamed_alone(scope, target, before, now)
    if target != scope:
        namesake, rivals, in_scope = _across_renamed_class(was, rivals, in_scope)
        if namesake is not None:
            return Successor(namesake, exact=not carried, carried=carried)
    taken = {r.shape for r in rivals if r.shape and r.shape != was.shape}
    pool = [d for d in in_scope if d.shape not in taken]
    for found, exact in (
        ([d for d in pool if was.shape and d.shape == was.shape], True),
        ([d for d in pool if _loose(was, d)], False),
    ):
        if (
            len(found) == 1
            and _related(was, found[0], before)
            and not any(_loose(r, found[0]) for r in rivals)
        ):
            return Successor(found[0], exact=exact, carried=carried)
        if found:
            return Successor(None, uncertain=True)
    scores = [(d, close_to(was, d, before, now)) for d in pool]
    close = [d for d, ok in scores if ok]
    unknown = any(ok is None for _, ok in scores)
    if (
        len(close) == 1
        and not unknown
        and not any(_maybe_close(r, close[0], before, now) for r in rivals)
    ):
        if len(pool) == 1 or _in_place(was, close[0], before, now):
            return Successor(close[0], carried=target != scope)
    if pool or scope_unsure:
        return Successor(None, uncertain=True)
    # a rival's rename alone, that is still close (or too long to tell) to this one too:
    # it may be either
    contested = any(
        _loose(was, d) or _maybe_close(was, d, before, now) for d in in_scope if d.shape in taken
    )
    return Successor(None, uncertain=contested)


def _renamed_alone(scope: str, target: str, before: Side, now: Side) -> bool:
    """Whether class `scope` became `target` with nothing but its name changed: the
    same rename shape."""

    def shape(side: Side, name: str) -> str:
        found = [d.shape for d in side.defs if local_name(d.qualified_name, d.path) == name]
        return found[0] if len(found) == 1 else ""

    old = shape(before, scope)
    return bool(old) and old == shape(now, target)


def _across_renamed_class(
    was: Definition, rivals: list[Definition], members: list[Definition]
) -> tuple[Definition | None, list[Definition], list[Definition]]:
    """`was`'s namesake in the renamed class, if any; and the rivals and candidates with
    the members carried there under their own names taken out of both."""
    by_name = {bare_name(d.qualified_name): d for d in members}
    namesake = by_name.get(bare_name(was.qualified_name))
    carried = by_name.keys() & {bare_name(r.qualified_name) for r in rivals}
    rivals = [r for r in rivals if bare_name(r.qualified_name) not in carried]
    members = [d for d in members if bare_name(d.qualified_name) not in carried]
    return namesake, rivals, members


def _removed(before: Side, now: Side) -> list[Definition]:
    """The definitions of `before` whose name is gone from `now`."""
    left = {_unnumbered(d) for d in now.defs}
    return [d for d in before.defs if _unnumbered(d) not in left]


def close_to(was: Definition, d: Definition, before: Side, now: Side) -> bool | None:
    """`d` is close enough to `was`'s old text to be it edited, under a related name if
    `was` is small; None when the two are too long to compare: maybe."""
    score = _similar(before.text(was), was, now.text(d), d)
    if score is None:
        return None
    return score >= _CLOSE and _related(was, d, before)


def _maybe_close(was: Definition, d: Definition, before: Side, now: Side) -> bool:
    """`d` is close to `was`, or too long to tell."""
    return close_to(was, d, before, now) is not False


def _related(was: Definition, d: Definition, before: Side) -> bool:
    """Two small definitions look alike by their shape alone: `d` may be `was` only under
    a related name (`_kin`). A larger one is judged by its text."""
    return len(_TOKEN.findall(before.text(was))) >= _SMALL or _kin(was, d)


def _unnumbered(d: Definition) -> str:
    """`d`'s name inside its module, the index's ordinal dropped (`load_rows#2` is one
    candidate with `load_rows`)."""
    return unnumbered(local_name(d.qualified_name, d.path))


def _names(side: Side) -> set[str]:
    return {local_name(d.qualified_name, d.path) for d in side.defs}


def _layout(side: Side) -> tuple[dict[str, set[str]], set[str]]:
    """`side`'s scopes -> the bare names of their members (not Rust's `impl` block, which
    every struct has), and the scopes that are classes: declared with a class kind,
    implemented (`impl Reader`), or with a member named after them (a constructor
    defined out of line)."""
    members: dict[str, set[str]] = defaultdict(set)
    classes = set()
    for d in side.defs:
        scope, name = scope_of(d), bare_name(d.qualified_name)
        if d.kind == "impl":
            classes.add(scope)
        else:
            members[scope].add(name)
        if d.kind in _CLASS_KINDS:
            classes.add(local_name(d.qualified_name, d.path))
        if name == scope.rpartition(".")[2]:
            classes.add(scope)
    return members, classes


def _renamed_scope(scope: str, before: Side, now: Side) -> tuple[str, bool]:
    """(the scope `scope` became in `now`, whether that is uncertain): the one new
    class keeping most of its members; `scope` itself when none does. A scope this file
    does not show to be a class (its methods defined out of line, its class in a header)
    may have been renamed too, but is never followed: unsure."""
    was, was_classes = _layout(before)
    members, classes = _layout(now)
    old = was[scope]
    news = (classes if scope in was_classes else set(members)) - {scope_of(d) for d in before.defs}
    kept = []
    for s in news & set(members):
        shared = len(old & members[s])
        if shared >= _MIN_SHARED and shared / len(old | members[s]) >= _KEPT_MEMBERS:
            kept.append(s)
    if len(kept) == 1 and scope in was_classes:
        return kept[0], False
    return scope, bool(kept)


def _loose(was: Definition, d: Definition) -> bool:
    return any(x and x == y for x, y in zip(d.loose_shapes, was.loose_shapes, strict=False))


def _tokens(text: str, name: str) -> list[str]:
    return ["_" if t == name else t for t in _TOKEN.findall(text)]


def _similar(a: str, was: Definition, b: str, d: Definition) -> float | None:
    """How close two definitions' texts are, names blanked (0..1); None when they are too
    long to compare within the bound."""
    left, right = _tokens(a, bare_name(was.name)), _tokens(b, bare_name(d.name))
    if max(len(left), len(right)) > _MAX_TOKENS:
        left = [" ".join(_tokens(x, bare_name(was.name))) for x in a.splitlines() if x.strip()]
        right = [" ".join(_tokens(x, bare_name(d.name))) for x in b.splitlines() if x.strip()]
        if max(len(left), len(right)) > _MAX_LINES:
            # too long to compare, unless their sizes alone rule it out
            sizes = 2 * min(len(left), len(right)) / (len(left) + len(right))
            return None if sizes >= _CLOSE else 0.0
    m = SequenceMatcher(None, left, right, autojunk=False)
    # the cheap upper bounds first: most candidates are nowhere near
    if m.real_quick_ratio() < _CLOSE or m.quick_ratio() < _CLOSE:
        return 0.0
    return m.ratio()


def _kin(was: Definition, d: Definition) -> bool:
    """The new name grows or shrinks the old one (`fetch` -> `fetch_all`), bumps its
    `v` version (`parse_v1` -> `parse_v2`), or keeps a word of it past the words both begin
    with (`fetch_rows` -> `load_rows`, `total_rows` -> `rows_total`, not -> `fetch_users`)."""
    a, b = _words(was.name), _words(d.name)
    if not a or not b:
        return False
    short, long = sorted((a, b), key=len)
    if long[: len(short)] == short and not all(w.isdigit() for w in long[len(short) :]):
        return True  # grown by a word, not by a width alone (`sha` -> `sha256` is a sibling)
    if _unversioned(a) == _unversioned(b):
        return True
    lead = 0  # the words both names begin with say nothing: `test_get_` is not a kinship
    while lead < min(len(a), len(b)) and a[lead] == b[lead]:
        lead += 1
    # a shared number is a width or a version, not a kinship (`sha256` -> `blake256`)
    return any(len(w) >= 3 and not w.isdigit() for w in set(a[lead:]) & set(b[lead:]))


def _words(name: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(bare_name(name))]


def _unversioned(words: list[str]) -> list[str]:
    """`words` without a trailing version, `v` and a number (a bare number is a width or
    a sibling: `toInt32`, `sha256`)."""
    if len(words) > 2 and words[-2] == "v" and words[-1].isdigit():
        return words[:-2]
    return words


def _in_place(was: Definition, d: Definition, before: Side, now: Side) -> bool:
    """`d` sits where `was` did among the definitions that stayed: one of its
    neighbours there is the same."""
    stayed = _names(before) & _names(now)
    a = _neighbours(local_name(was.qualified_name, was.path), before, stayed)
    b = _neighbours(local_name(d.qualified_name, d.path), now, stayed)
    return any(x is not None and x == y for x, y in zip(a, b, strict=True)) or a == b


def _neighbours(name: str, side: Side, stayed: set[str]) -> tuple[str | None, str | None]:
    order = [
        n
        for n in dict.fromkeys(
            local_name(d.qualified_name, d.path)
            for d in sorted(side.defs, key=lambda d: d.start_line)
        )
        if n in stayed or n == name
    ]
    i = order.index(name)
    return (order[i - 1] if i else None), (order[i + 1] if i + 1 < len(order) else None)
