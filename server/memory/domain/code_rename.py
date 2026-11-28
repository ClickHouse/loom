"""The edits that bring a memory's text up to a rename its link followed, or why none
is offered.

A link that followed its definition to another name or file leaves the memory's text
naming the old one. Whether the name can be swapped in the text depends on how it is
spelled and on the sentence around it, not on the link: the same rename is unambiguous
as a full dotted name, ambiguous as a bare word another definition also bears, and
wrong in any spelling when the sentence is about the name, the place or a past state.
`plan` offers an edit only for a name written as code (`anchor.code_spans`) or a whole
path, and withholds the whole memory for the first of `REASONS` that applies. Nothing
here writes: the link pass applies the edits of a memory nobody holds
(`server.memory.app.code_renames.revise`), and a principal applies any other's with
memory.update and `if_match_sha256`.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from server.code.domain.anchor import (
    Definition,
    Span,
    bare_name,
    code_shaped,
    code_spans,
    local_name,
    mentions,
    names_symbol,
    path_matches,
    resolve,
)
from server.code.domain.python_names import import_forms
from server.core.hashing import served_content_sha256

# Why a memory gets no edit, in the order `plan` reports them: the first that applies.
REASONS = (
    "not_text",  # its value is not one string
    "link_not_settled",  # a link is pending, changed, lost, or not on its baseline
    "incomplete",  # the caps cut its links: not all of its code is known
    "overloaded",  # an old or new name is one of several definitions under its name
    "quoted_or_literal",  # an old name or path sits in a fenced block, a URL, a quoted
    # string or a longer backticked span
    "prose_occurrence",  # an old name is written as a word of the prose
    "old_name_remains",  # an old name or path stays where no edit reaches it
    "names_several",  # one written name is the old name of more than one link
    "receiver_form",  # `self.name`, and the memory's files do not name its file
    "old_name_still_defined",  # the name as written names a definition at the head
    "new_name_present",  # the text already holds the new name or path
    "dated_claim",  # the memory, or the sentence, is about a point in time
    "about_naming",  # the sentence is about the name or the place itself
    "named_in_key_or_tags",  # the memory_key or a tag holds an old name or path
    "named_in_files",  # `files` names the old path, which memory.update keeps
    "would_unlink",  # the revised memory would not link to all the code it links to now
    "no_code_token",  # the text names no old name or path: nothing to edit
)
# the reason of a memory whose text holds as it is
UNNAMED = REASONS[-1]
# the reasons the links, the labels and `files` give whatever the text says: they
# withhold only a text that names something old
_UNWRITTEN = frozenset({"link_not_settled", "incomplete", "named_in_key_or_tags", "named_in_files"})

# what makes a sentence a statement about a point in time: a commit, a version tag, a
# year, a month, a pull request or issue. `May` is the month only capitalised after
# another word: elsewhere it is the verb.
_DATED = re.compile(
    r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b"
    r"|\bv\d+(?:\.\d+)*\b|\b\d+\.\d+\.\d+\b"
    r"|\b(?:19|20)\d\d\b"
    r"|\b(?:january|february|march|april|june|july|august|september|october|november"
    r"|december)\b|(?<=\w\s)(?-i:May)\b"
    r"|#\d+\b|\b(?:pr|mr|gh)[- ]?\d+\b|\b(?:issue|pull request)s? \d+",
    re.IGNORECASE,
)
# what makes a sentence a statement about the name or the place (English only). Not
# `call` or `calls`: far more often the code is called than named.
_NAMING = re.compile(
    r"\b(?:nam(?:e|es|ed|ing)|renam\w+|called|alias\w*|formerly|used to|replaced"
    r"|moved from|deprecated|legacy|before|previously)\b",
    re.IGNORECASE,
)
# a sentence ends after `.`, `!` or `?` and a space (not after a one-letter abbreviation:
# `e.g.`, `i.e.`), at a blank line (`\r\n` ends a line too) and before a list item; a
# wrapped line goes on
_SENTENCE_END = re.compile(
    r"(?<!\W[A-Za-z]\.)(?<=[.!?])\s+|\n[ \t\r]*\n\s*|\n(?=[ \t]*(?:[-*+]|\d+[.)])[ \t])"
)
_JOIN = re.compile(r"::|\.")
# the ordinal the index gives the second and later definitions of one name in a scope
_ORDINAL = re.compile(r"(?<=[^:.])#\d+(?=$|::|\.)")
# the columns that date a memory: its validity ended, or it is about an event
_DATES = ("valid_to", "invalid_at", "temporal_anchor")


class Edit(NamedTuple):
    """Replace `text[start:end]`, which reads `old`, with `new`. The positions count
    Unicode code points, as a Python string index does: a caller that indexes UTF-16
    units or bytes converts them, and checks the slice against `old`."""

    start: int
    end: int
    old: str
    new: str


@dataclass(frozen=True)
class Plan:
    """The edits of one memory's text, all or none, or the reason it gets none."""

    edits: tuple[Edit, ...] = ()
    reason: str = ""
    # the hash memory.get serves for the text the edits are of: memory.update's
    # `if_match_sha256`
    sha256: str = ""


class _Move(NamedTuple):
    """A link that sits elsewhere than its memory's text says: from, to."""

    was_path: str
    was_name: str
    path: str
    name: str


def written_as(link: Mapping[str, Any]) -> tuple[str, str]:
    """(path, qualified name) the memory's text was written against: the oldest
    identity the link keeps, else its anchor. A definition link that replaced a file
    link was never that file: only an identity of the link's own kind counts."""
    of_symbol = bool(link.get("qualified_name"))
    former = [f for f in link.get("former") or [] if bool(f["name"]) == of_symbol]
    if former:
        return str(former[0]["path"]), str(former[0]["name"])
    return str(link.get("anchored_path") or ""), str(link.get("anchored_name") or "")


def revised(text: str, edits: Sequence[Edit]) -> str:
    """`text` with `edits` applied."""
    out, at = [], 0
    for e in sorted(edits):
        out += [text[at : e.start], e.new]
        at = e.end
    return "".join(out) + text[at:]


def plan(
    text: str | None,
    memory: Mapping[str, Any],
    links: Sequence[Mapping[str, Any]],
    by_name: Mapping[str, Sequence[Definition]],
    *,
    gone: Collection[str] = (),
    rules: str,
) -> Plan:
    """The edits that make `text` (a memory's value; None when it is not a string) name
    its code where the code is now, or the reason none is offered.

    `memory` is its head (`files`, `memory_key`, `tags`, the columns that date it) and
    `links` the links made from it as it is now. `by_name` holds the index's definitions
    at the head under every bare name the text mentions and every new name; `gone` the
    old paths the head no longer has; `rules` the fingerprint rules in force. Only a link
    in state `moved` on its baseline is revised, and only when every link is settled:
    applying an edit makes the link pass link the memory again from its new text."""
    if text is None:
        return Plan(reason="not_text")
    why: set[str] = set()
    if not all(_settled(a, rules) for a in links):
        why.add("link_not_settled")
    if not all(a["complete"] for a in links):
        why.add("incomplete")
    files = [str(f) for f in memory.get("files") or []]
    spans, quoted = code_spans(text)
    edits: dict[tuple[int, int], Edit] = {}
    moves = [m for a in links if a["state"] == "moved" and (m := _move(a))]
    for m in moves:
        alive = m.was_path not in gone
        why |= _in_labels(memory, m, alive)
        if m.name:
            why |= _renamed(text, m, moves, spans, quoted, by_name, files, edits)
        if m.was_path != m.path:
            why |= _repathed(text, m, quoted, files, alive, edits)
    why |= _old_modules(text, moves, list(edits), spans, quoted, by_name)
    if edits:
        why |= _about(text, memory, edits.values())
        why |= _unlinked(revised(text, list(edits.values())), links, by_name, files)
    elif why <= _UNWRITTEN:
        return Plan(reason=UNNAMED)
    for reason in REASONS:
        if reason in why:
            return Plan(reason=reason)
    return Plan(edits=tuple(sorted(edits.values())), sha256=served_content_sha256(text))


def _settled(link: Mapping[str, Any], rules: str) -> bool:
    """The link is at its code, on the text its memory was written against, hashed
    under the rules in force."""
    return (
        link["state"] in ("fresh", "moved")
        and not link["carried"]
        and link["body_hash"] == link["checked_hash"]
        and link["fingerprint_version"] == rules
    )


def _move(link: Mapping[str, Any]) -> _Move | None:
    """Where `link` was written and where it is; None for one back where it was."""
    was, now = written_as(link), (str(link["path"]), str(link["qualified_name"]))
    return _Move(*was, *now) if was[0] and was != now else None


def _forms(path: str, qualified_name: str) -> tuple[str, ...]:
    """The dotted names a text may spell the definition by, the qualified name first."""
    dotted = qualified_name.replace("::", ".")
    return tuple(dict.fromkeys((dotted, *import_forms(path, qualified_name.split("::")))))


def _respelled(written: str, m: _Move) -> str:
    """`written`, a spelling of the old name, as the same spelling of the new one, joined
    as `written` joins it. A name written inside its module (`get`, `Reader.get`) keeps
    as many segments. One written with its module names the whole new name inside the
    new module, under as many module segments, and under the whole module when it wrote
    the whole old one: a full dotted name stays one, also where no part of it is the
    module (C and C++)."""
    dotted = written.replace("::", ".")
    old, new = _forms(m.was_path, m.was_name), _forms(m.path, m.name)
    at = next((i for i, f in enumerate(old) if f == dotted or f.endswith("." + dotted)), 0)
    was_local = local_name(m.was_name, m.was_path).split(".")
    local = local_name(m.name, m.path).split(".")
    joins = _JOIN.findall(written)
    over = len(joins) + 1 - len(was_local)  # the module segments it wrote
    if dotted == old[0]:
        parts = new[0].split(".")
    elif over <= 0:
        parts = local[-(len(joins) + 1) :]
    else:
        module = new[min(at, len(new) - 1)].split(".")[: -len(local)]
        whole = over >= len(old[at].split(".")) - len(was_local)
        parts = (module if whole else module[-over:]) + local
    # its own joins, from the right; a longer name repeats its first
    joins = joins[:1] * (len(parts) - 1 - len(joins)) + joins
    joins = joins[len(joins) - (len(parts) - 1) :]
    return parts[0] + "".join(j + p for j, p in zip(joins, parts[1:], strict=True))


def _renamed(
    text: str,
    m: _Move,
    moves: Sequence[_Move],
    spans: Sequence[Span],
    quoted: Sequence[tuple[int, int]],
    by_name: Mapping[str, Sequence[Definition]],
    files: Sequence[str],
    edits: dict[tuple[int, int], Edit],
) -> set[str]:
    """Add to `edits` the names in `text` that spell the old definition of `m` and not
    the new one, each as the same spelling of the new one; the reasons they withhold."""
    was, now = _plain(m.was_name), _plain(m.name)
    old, new = bare_name(was), bare_name(now)
    mine = [
        s
        for s in spans
        if names_symbol(s.name, was, m.was_path) and not names_symbol(s.name, now, m.path)
    ]
    if (was, now) != (m.was_name, m.name):
        # one of several under its name: withheld where the text writes the old one
        return {"overloaded"} if mine or (old != new and _word(old).search(text)) else set()
    why: set[str] = set()
    for s in mine:
        to = _respelled(s.name, m)
        if s.receiver and not any(path_matches(p, f) for f in files for p in (m.was_path, m.path)):
            why.add("receiver_form")
        if _names_another(s.name, m, by_name):
            why.add("old_name_still_defined")
        at = resolve(to, by_name.get(new, ()), files)
        if at is None or (at.path, at.qualified_name) != (m.path, m.name):
            why.add("would_unlink")
        if edits.setdefault((s.start, s.end), Edit(s.start, s.end, s.name, to)).new != to:
            why.add("names_several")
    # a move keeps the bare name valid: only the spellings it replaces may be left over
    olds = [old] if old != new else sorted({s.name for s in mine})
    news = [new] if old != new else sorted({_respelled(s.name, m) for s in mine})
    # a name that ends in an old spelling holds as it is when it names another
    # definition at the head, and is edited by the link whose old definition it names
    others = [
        s
        for s in spans
        if _names_another(s.name, m, by_name)
        or any(x != m and x.name and names_symbol(s.name, x.was_name, x.was_path) for x in moves)
    ]
    why |= _left_over(text, olds, [*mine, *others], spans, quoted)
    if mine and any(_word(t).search(text) for t in news):
        why.add("new_name_present")
    return why


def _names_another(name: str, m: _Move, by_name: Mapping[str, Sequence[Definition]]) -> bool:
    """`name` as written names a definition at the head other than the one `m` is at."""
    return any(
        names_symbol(name, _plain(d.qualified_name), d.path)
        for d in by_name.get(bare_name(name), ())
        if (d.path, d.qualified_name) != (m.path, m.name)
    )


def _plain(qualified_name: str) -> str:
    """`qualified_name` without the index's ordinals: `Reader#2::get` is a `Reader::get`."""
    return _ORDINAL.sub("", qualified_name)


def _word(spelling: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(spelling)}(?!\w)")


def _inside(regions: Sequence[tuple[int, int]], start: int, end: int) -> tuple[int, int] | None:
    """The first of `regions` that holds start..end."""
    return next(((a, b) for a, b in regions if a <= start and end <= b), None)


def _left_over(
    text: str,
    olds: Sequence[str],
    covered: Sequence[Span],
    spans: Sequence[Span],
    quoted: Sequence[tuple[int, int]],
) -> set[str]:
    """Why an old spelling withholds the memory, unless it ends one of the `covered`
    names: those an edit replaces and those that name other code."""
    why: set[str] = set()
    for spelling in olds:
        for o in _word(spelling).finditer(text):
            if any(s.start <= o.start() and o.end() == s.end for s in covered):
                continue
            if _inside(quoted, *o.span()):
                why.add("quoted_or_literal")
            elif any(s.start <= o.start() and o.end() <= s.end for s in spans):
                # `Reader.get` still holds the old `Reader`; `cfg.read_rows()` names nothing
                why.add("old_name_remains")
            else:
                why.add("prose_occurrence" if code_shaped(spelling) else "old_name_remains")
    return why


def _modules(path: str, qualified_name: str) -> set[str]:
    """The dotted spellings of the module a definition is in: whole, and from each of
    its segments on."""
    local = "." + local_name(qualified_name, path)
    out: set[str] = set()
    for form in _forms(path, qualified_name):
        parts = form.removesuffix(local).split(".") if form.endswith(local) else []
        out.update(".".join(parts[i:]) for i in range(len(parts)))
    return out


def _old_modules(
    text: str,
    moves: Sequence[_Move],
    edited: Sequence[tuple[int, int]],
    spans: Sequence[Span],
    quoted: Sequence[tuple[int, int]],
    by_name: Mapping[str, Sequence[Definition]],
) -> set[str]:
    """Why a module a definition of `moves` left withholds the memory: `text` writes it,
    over more than one segment, outside the `edited` spans (an import, a sentence about
    the place), so the revised text would name both places. One segment is as often a
    word of the prose, and a name written as code that names a definition at the head
    (another one still in that module) holds as it is."""
    why: set[str] = set()
    held = [
        (s.start, s.end)
        for s in spans
        if any(
            names_symbol(s.name, _plain(d.qualified_name), d.path)
            for d in by_name.get(bare_name(s.name), ())
        )
    ]
    for m in moves:
        left = _modules(m.was_path, m.was_name) - _modules(m.path, m.name) if m.name else set()
        for module in (x for x in left if "." in x):
            spelled = re.escape(module).replace(r"\.", r"(?:\.|::)")
            for o in re.finditer(rf"(?<!\w){spelled}(?!\w)", text):
                if not _inside(edited, *o.span()) and not _inside(held, *o.span()):
                    literal = _inside(quoted, *o.span())
                    why.add("quoted_or_literal" if literal else "old_name_remains")
    return why


def _repathed(
    text: str,
    m: _Move,
    quoted: Sequence[tuple[int, int]],
    files: Sequence[str],
    alive: bool,
    edits: dict[tuple[int, int], Edit],
) -> set[str]:
    """Add to `edits` the whole old path of `m` wherever `text` writes it, when the head
    no longer has that file (`alive` False); the reasons its path withholds. A path in
    a longer one, before a line number or in a test id, and the old file's bare name,
    are left over; so is every one while the old file is still there, where the text
    may be about the file."""
    why: set[str] = set()
    whole = re.compile(rf"(?<![\w./-]){re.escape(m.was_path)}(?![\w/-]|\.\w|:\d|::)")
    mine: list[tuple[int, int]] = []
    for o in () if alive else whole.finditer(text):
        within = _inside(quoted, *o.span())
        # a backticked path is quoted as a whole: it is the token
        ticked = within == (o.start() - 1, o.end() + 1) and text[o.start() - 1] == "`"
        if within is None or ticked:
            mine.append(o.span())
            edits[o.span()] = Edit(*o.span(), m.was_path, m.path)
    was_file, file = m.was_path.rsplit("/", 1)[-1], m.path.rsplit("/", 1)[-1]
    left = [re.compile(re.escape(m.was_path))]
    if was_file not in (file, m.was_path):
        left.append(re.compile(rf"(?<![\w./-]){re.escape(was_file)}(?![\w-])"))
    for rx in left:
        for o in rx.finditer(text):
            if _inside(mine, *o.span()):
                continue
            literal = not alive and _inside(quoted, *o.span())
            why.add("quoted_or_literal" if literal else "old_name_remains")
    # the new path inside a longer one (`test/src/util.py`, `src/util.pyc`) is another file
    present = rf"(?<![\w.-])(?<![\w-]/){re.escape(m.path)}(?![\w-]|\.\w)"
    if mine and re.search(present, text):
        why.add("new_name_present")
    if not alive and any(
        path_matches(m.was_path, f) and not path_matches(m.path, f) for f in files
    ):
        why.add("named_in_files")
    return why


def _in_labels(memory: Mapping[str, Any], m: _Move, alive: bool) -> set[str]:
    """`named_in_key_or_tags` when the memory's key or a tag (stored lowercased) holds
    the old bare name of `m`, or its old path once the head no longer has that file
    (`alive` False): an edit of the text leaves them."""
    labels = [str(memory.get("memory_key") or ""), *(str(t) for t in memory.get("tags") or [])]
    old, new = bare_name(m.was_name), bare_name(m.name)
    held = []
    if old and old != new:
        held.append(re.compile(rf"(?<![A-Za-z0-9_]){re.escape(old)}(?![A-Za-z0-9_])", re.I))
    if m.was_path != m.path and not alive:
        held.append(re.compile(re.escape(m.was_path), re.I))
    return {"named_in_key_or_tags"} if any(rx.search(t) for rx in held for t in labels) else set()


def _sentence(text: str, at: int) -> str:
    """The sentence of `text` that holds position `at`."""
    start = max((b.end() for b in _SENTENCE_END.finditer(text, 0, at)), default=0)
    end = _SENTENCE_END.search(text, at)
    return text[start : end.start() if end else len(text)]


def _about(text: str, memory: Mapping[str, Any], edits: Collection[Edit]) -> set[str]:
    """The reasons the sentences `edits` change, or the memory's own dates, withhold
    them: a claim about a point in time, or about the name itself, is right as it is."""
    why: set[str] = set()
    if any(memory.get(column) for column in _DATES):
        why.add("dated_claim")
    # without the names themselves: a path under `v2/` dates nothing
    rest = revised(text, [e._replace(new=" " * (e.end - e.start)) for e in edits])
    for sentence in {_sentence(rest, e.start) for e in edits}:
        if _DATED.search(sentence):
            why.add("dated_claim")
        if _NAMING.search(sentence):
            why.add("about_naming")
    return why


def _unlinked(
    text: str,
    links: Sequence[Mapping[str, Any]],
    by_name: Mapping[str, Sequence[Definition]],
    files: Sequence[str],
) -> set[str]:
    """`would_unlink` when `text` (the revised one) would not be linked to every
    definition and file `links` are at: each name resolved as the link pass resolves it
    (`anchor.resolve`), each file link named by `files`."""
    named = set()
    for mention in mentions(text, cap=len(text)):
        d = resolve(
            mention.name, by_name.get(bare_name(mention.name), ()), files, in_prose=mention.in_prose
        )
        if d is not None:
            named.add((d.path, d.qualified_name))
    for a in links:
        if a["qualified_name"]:
            kept = (a["path"], a["qualified_name"]) in named
        else:
            kept = any(path_matches(a["path"], f) for f in files)
        if not kept:
            return {"would_unlink"}
    return set()
