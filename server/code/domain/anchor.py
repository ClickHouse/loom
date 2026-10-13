"""Code anchors: a memory's claim about code, resolved to the definition it names and
followed as the code changes.

Identity is the tree-sitter (path, qualified_name): every indexed language has it right
after ingest, and the compiler tier hashes signatures differently, so a `symbol_id` is no
identity across tiers. Overloads share that identity and are one target. A claim is
linked only when it resolves to one target; a homonym is skipped, never linked.
Definitions are what tree-sitter finds in the file at a commit: the index only proposes
where to look.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import NamedTuple

from server.code.domain.extract.lang_go import go_dir_scope
from server.code.domain.extract.lang_rust import module_scope_rust

_TICKS = re.compile(r"`([^`\n]{3,160})`")
_IDENT = re.compile(r"~?[A-Za-z_]\w*(?:(?:::|\.)~?[A-Za-z_]\w*)*")
_QUALIFIED = re.compile(r"\b[A-Za-z_]\w*(?:::~?[A-Za-z_]\w*)+")
_DOTTED_CALL = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+(?=\()")
_EMPTY_CALL = re.compile(r"\b[A-Za-z_]\w{3,}(?=\(\))")
_CAMEL = re.compile(r"\b(?:[a-z]+[A-Z]|[A-Z][a-z0-9]+[A-Z])[A-Za-z0-9]*\b")
_SNAKE = re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]{2,}\b")
_MIN_BARE = 4


@dataclass(frozen=True)
class Definition:
    path: str
    qualified_name: str
    name: str
    symbol_id: str = ""
    start_line: int = 0
    end_line: int = 0
    # The fingerprint of the definition's text, all overloads together.
    body_hash: str = ""
    # Its tokens with the declared name and its bare calls and reads blanked
    # (`declaration.Declarations.shape`): unchanged by a rename alone; '' where no grammar
    # parses it or the name is also used another way inside, and then never matched as one.
    shape: str = ""
    # The declared name with its bare calls, and with its bare calls and reads, blanked: a
    # rename matched by these alone may have changed what a name means, so it is
    # relinked as changed.
    loose_shapes: tuple[str, ...] = ()
    # the index's kind: function, class, struct, …
    kind: str = ""
    # what the judge reads of it: every declaration its fingerprint covers, each under the
    # conditions that select it (`Declarations.family_text`)
    shown: str = ""


@dataclass(frozen=True)
class Link:
    """One anchor: a symbol (or, with `qualified_name` '', the file) a memory is about.
    `body_hash` is the definition's fingerprint when the memory was written,
    `checked_hash` at the last check (a new edit to changed code moves it)."""

    path: str
    qualified_name: str = ""
    body_hash: str = ""
    state: str = "fresh"
    changed_since: str = ""
    start_line: int = 0
    end_line: int = 0
    checked_hash: str = ""
    # its renamed class carried it: what surrounds it changed, so it reads changed until
    # a revalidation, whatever its own text
    carried: bool = False
    # where the memory was written against (its path and name then): a link elsewhere
    # reads moved. A new link is anchored where it is.
    anchored_path: str = ""
    anchored_name: str = ""
    # the file its qualified name was last read in, when that file has since moved on
    # without it (code not found): where its old text and module are
    checked_path: str = ""

    def __post_init__(self) -> None:
        if not self.anchored_path:
            object.__setattr__(self, "anchored_path", self.path)
            object.__setattr__(self, "anchored_name", self.qualified_name)

    def moved_from_anchor(self, path: str, qualified_name: str) -> bool:
        """`path`/`qualified_name` is not where the memory says the code is."""
        return (path, qualified_name) != (self.anchored_path, self.anchored_name)

    @property
    def read_path(self) -> str:
        """The file its qualified name was last read in."""
        return self.checked_path or self.path

    def matched_baseline(self) -> bool:
        """Its last check found the text the memory was written against (whatever state
        it reads now: an unresolved or missing link keeps both hashes)."""
        return not self.carried and self.body_hash == self.checked_hash


def bare_name(name: str) -> str:
    return re.split(r"::|\.", name)[-1]


class Mention(NamedTuple):
    name: str
    # a bare camelCase or snake_case word of the prose (`GitHub`, `max_threads`): it is
    # code only where the memory's files define it
    in_prose: bool


_RECEIVER = re.compile(r"^(?:self|cls|this)\.")


def mentions(text: str, *, cap: int = 16) -> list[Mention]:
    """Code-shaped names in `text`, each once, in order: backticked identifiers, `A::b`,
    `a.b(` and `f()` calls, camelCase / PascalCase and snake_case. Plain words never
    qualify, and a part of a qualified name is not a mention of its own."""
    text = text or ""
    found: list[tuple[int, int, str, bool]] = []
    for m in _TICKS.finditer(text):
        span = m.group(1).strip().split("(", 1)[0]
        if _IDENT.fullmatch(span):
            found.append((m.start(), m.end(), span, False))
    for rx, in_prose in (
        (_QUALIFIED, False),
        (_DOTTED_CALL, False),
        (_EMPTY_CALL, False),
        (_CAMEL, True),
        (_SNAKE, True),
    ):
        found.extend((m.start(), m.end(), m.group(0), in_prose) for m in rx.finditer(text))
    # the spans inside another: by start, the longer first, each against the furthest
    # end reached before it
    inside: set[tuple[int, int]] = set()
    reach = -1
    for start, end in sorted({(s, e) for s, e, _, _ in found}, key=lambda x: (x[0], -x[1])):
        if end <= reach:
            inside.add((start, end))
        reach = max(reach, end)
    out: dict[str, bool] = {}
    for start, end, spelled, in_prose in found:
        name = _RECEIVER.sub("", spelled)  # `self.read_rows()` names `read_rows`
        explicit = "::" in name or "." in name  # `Reader.get`: resolution decides
        long = len(bare_name(name).lstrip("~")) >= _MIN_BARE
        if (start, end) not in inside and (explicit or long):
            out[name] = out.get(name, True) and in_prose  # backticked anywhere: not prose
    return [Mention(name, in_prose) for name, in_prose in list(out.items())[:cap]]


def names_symbol(mention: str, qualified_name: str) -> bool:
    """`mention` is the qualified name or a `::`- / `.`-separated suffix of it."""
    m, q = mention.replace("::", "."), qualified_name.replace("::", ".")
    return q == m or q.endswith("." + m)


def path_matches(path: str, ref: str) -> bool:
    """`ref` (a memory's files entry: repo-relative or a bare filename) names `path`."""
    return path == ref or path.endswith("/" + ref) or ref.endswith("/" + path)


def resolve(
    mention: str, defs: Sequence[Definition], files: Sequence[str], *, in_prose: bool = False
) -> Definition | None:
    """The one target `mention` names: the only match inside the memory's `files`, else
    (unless it is a word of the prose) the only match anywhere. None when nothing or more
    than one target matches."""
    targets: dict[tuple[str, str], Definition] = {}
    for d in sorted(defs, key=lambda d: d.start_line):
        if names_symbol(mention, d.qualified_name):
            targets.setdefault((d.path, d.qualified_name), d)
    if files:
        inside = [d for d in targets.values() if any(path_matches(d.path, f) for f in files)]
        if inside:
            return inside[0] if len(inside) == 1 else None
    if in_prose:
        return None
    return next(iter(targets.values())) if len(targets) == 1 else None


# C and C++ names never carry the path: a namespace named like its file is still a namespace
_PATHLESS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".ipp", ".inl"}


_ORDINAL = re.compile(r"(?<=.)#\d+$")


def unnumbered(name: str) -> str:
    """`name` without the index's ordinal: `load_rows#2` is its second `load_rows`."""
    return _ORDINAL.sub("", name)


def _ext(path: str) -> str:
    file = path.rsplit("/", 1)[-1]
    return file[file.rfind(".") :].lower() if "." in file else ""


def _pathless(path: str) -> bool:
    return _ext(path) in _PATHLESS


def local_name(qualified_name: str, path: str) -> str:
    """`qualified_name` without the part its path makes, dot-separated: the module; for
    Go the package (its directory and name: a file has one package clause, and a move
    may rename it); for Rust the crate and module path; whole for C and C++."""
    file, ext = path.rsplit("/", 1)[-1], _ext(path)
    if ext in _PATHLESS:
        return qualified_name.replace("::", ".")
    if ext == ".go":
        scope = go_dir_scope(path.rsplit("/", 1)[0] if "/" in path else "") + "."
        if qualified_name.startswith(scope):
            rest = qualified_name[len(scope) :]
            return (rest.partition("::")[2] or rest).replace("::", ".")
    if ext == ".rs":
        scope = module_scope_rust(path) + "::"
        if qualified_name.startswith(scope):
            return qualified_name[len(scope) :].replace("::", ".")
    q = qualified_name.replace("::", ".")
    stem = path[: path.rfind(".")] if "." in file else path
    directory = path.rsplit("/", 1)[0] if "/" in path else ""
    for prefix in (stem, directory):
        p = prefix.replace("/", ".")
        if p and q.startswith(p + "."):
            return q[len(p) + 1 :]
    return q


def fingerprint(texts: Sequence[str]) -> str:
    """One hash over a definition's texts (its overloads, in order), verbatim but for line
    endings: indentation and literals carry meaning, so any edit changes it."""
    joined = "\0".join(t.replace("\r\n", "\n") for t in texts)
    return hashlib.sha256(joined.encode()).hexdigest() if joined.strip("\0") else ""


def locate(
    link: Link,
    path_now: str,
    here: Sequence[Definition],
    *,
    renamed: Definition | None = None,
    elsewhere: Sequence[Definition] = (),
) -> Definition | None:
    """Where a symbol link's definition is now: the same qualified name at `path_now`;
    after a file move, the one definition there with the same name inside its module;
    `renamed` (found there by its shape); or `elsewhere` (a move git did not pair) the one
    same qualified name, else the one same name inside its module with the same text.
    Nothing else counts: a namesake in another scope is not it, nor a C or C++ one in
    another file with other text (their names carry no path)."""
    same = [d for d in here if d.qualified_name == link.qualified_name]
    if same:
        return same[0]
    # its module renamed: the file moved, or a Go package clause was renamed in place
    local = local_name(link.qualified_name, link.read_path)
    moved = {d.qualified_name: d for d in here if local_name(d.qualified_name, path_now) == local}
    if len(moved) == 1:
        return next(iter(moved.values()))
    if renamed is not None:
        return renamed
    paths = {d.path: d for d in elsewhere if d.qualified_name == link.qualified_name}
    if len(paths) == 1 and not _pathless(link.read_path):
        return next(iter(paths.values()))
    # moved, unchanged, into another module: the one with its name and its text
    texts = {link.body_hash, link.checked_hash} - {""}
    unchanged = {
        (d.path, d.qualified_name): d
        for d in elsewhere
        if local_name(d.qualified_name, d.path) == local and d.body_hash in texts
    }
    return next(iter(unchanged.values())) if len(unchanged) == 1 else None


def recheck(
    link: Link,
    *,
    path_now: str,
    here: Sequence[Definition] = (),
    renamed: Definition | None = None,
    renamed_exactly: bool = True,
    elsewhere: Sequence[Definition] = (),
    uncertain: bool = False,
    carried: bool = False,
    head: str,
) -> Link:
    """The link after the code moved to `head`.

    `path_now` is the link's file at `head` ('' when deleted) and `here` its definitions;
    `renamed` and `elsewhere` are candidates, as `locate` takes them. A rename that is
    not exact, and a method `carried` by its renamed class, read `changed`. Code that is
    not found reads `unresolved` when `uncertain` (a new definition may be it), else
    `missing`; either way the link keeps its name, and follows its file while the file
    is there, so a revert finds it again."""
    if not link.qualified_name:
        if not path_now:
            return _in_state(link, "missing", head)
        state = "moved" if link.moved_from_anchor(path_now, "") else "fresh"
        return replace(_in_state(link, state, head), path=path_now)
    d = locate(link, path_now, here, renamed=renamed, elsewhere=elsewhere)
    if d is None:
        lost = _in_state(link, "unresolved" if uncertain else "missing", head)
        path = path_now or link.path
        return replace(
            lost, path=path, checked_path="" if path == link.read_path else link.read_path
        )
    if d is renamed and carried:
        link = replace(link, carried=True)
    elif d is renamed and renamed_exactly and link.matched_baseline():
        # only the name changed since a check that matched the baseline: it is the new one
        link = replace(link, body_hash=d.body_hash)
    changed = link.carried or bool(link.body_hash and d.body_hash and d.body_hash != link.body_hash)
    moved = link.moved_from_anchor(d.path, d.qualified_name)
    state = "changed" if changed else "moved" if moved else "fresh"
    return replace(
        _in_state(link, state, head),
        path=d.path,
        checked_path="",
        qualified_name=d.qualified_name,
        body_hash=link.body_hash or d.body_hash,
        checked_hash=d.body_hash,
        start_line=d.start_line,
        end_line=d.end_line,
    )


def _in_state(link: Link, state: str, head: str) -> Link:
    """`link` in `state`; `changed_since` is the commit it entered that state at."""
    if state in ("fresh", "moved"):
        return replace(link, state=state, changed_since="")
    since = link.changed_since if link.state == state else head
    return replace(link, state=state, changed_since=since)
