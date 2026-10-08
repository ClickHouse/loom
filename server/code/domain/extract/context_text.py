"""Contextual-embedding text construction.

A chunk is never embedded raw: it is prefixed with where it lives (path,
module), what encloses it (qualified scope + signature), and — when the
LLM description pass has run — a one-line natural-language summary. The
prefix is what lets "how is quota enforcement applied?" land on a chunk
whose body never says the word "enforcement".

An optional callee outline can join a function chunk's head:
`calls: a, b, c` (the unit's own call sites, stoplisted, at most
MAX_CALLEES) and `Called by: X::f, Y::g` (at most MAX_CALLERS caller
qualified names from code_edges). Both lines are empty by default; a
backfill's `--context-callees` turns them on for its run. Against the plain
text on a twin embedding label they made no measurable retrieval difference.

The exact embedded string is persisted in code_chunks.context_text so a
retrieval miss can always be reproduced offline.
"""

from __future__ import annotations

from collections.abc import Iterable

# Bumped when the head's shape changes. The extractor stamp already hashes
# this module's code, so a change here re-heals every file of a namespace
# in paced batches — but a heal keeps a content-addressed row and never
# re-embeds it, so a template change reaches an existing row only through a
# re-embed under a new label.
CONTEXT_TEMPLATE_VERSION = 2

MAX_CALLEES = 24
MAX_CALLERS = 8

# Callee names that say nothing about what a unit does: logging macros,
# the standard library, assertions, allocation helpers and the container
# verbs edges_treesitter already treats as noise. A name is dropped when it
# is in the set, starts with one of the prefixes, or is qualified into std.
CALLEE_STOPLIST: frozenset[str] = frozenset(
    {
        "assert",
        "chassert",
        "static_assert",
        "sizeof",
        "alignof",
        "typeid",
        "decltype",
        "static_cast",
        "dynamic_cast",
        "reinterpret_cast",
        "const_cast",
        "typeid_cast",
        "assert_cast",
        "checkAndGetColumn",
        "Exception",
        "NetException",
        "ErrnoException",
        "move",
        "forward",
        "get",
        "size",
        "begin",
        "end",
        "cbegin",
        "cend",
        "push_back",
        "emplace_back",
        "emplace",
        "empty",
        "data",
        "reset",
        "release",
        "first",
        "second",
        "c_str",
        "str",
        "count",
        "find",
        "insert",
        "erase",
        "clear",
        "at",
        "back",
        "front",
        "make_shared",
        "make_unique",
        "make_pair",
        "make_tuple",
        "swap",
        "min",
        "max",
        "abs",
        "static_pointer_cast",
        "dynamic_pointer_cast",
        "throwIf",
        "unlikely",
        "likely",
        "UNREACHABLE",
        "fmt",
        "format",
        "toString",
        "operator",
        "as",
        "tryGetAs",
        # python / shell builtins the edge walker also skips
        "print",
        "len",
        "int",
        "float",
        "bool",
        "list",
        "dict",
        "set",
        "tuple",
        "range",
        "enumerate",
        "zip",
        "isinstance",
        "super",
        "getattr",
        "setattr",
        "hasattr",
        "join",
        "split",
        "strip",
        "append",
        "items",
        "keys",
        "values",
        "open",
        "repr",
        "type",
        "sorted",
        "sum",
        "any",
        "all",
        "next",
        "iter",
        "id",
        "hash",
        "echo",
        "printf",
        "cd",
        "rm",
        "cp",
        "mv",
        "cat",
        "grep",
        "sed",
        "awk",
        "export",
        "local",
        "return",
        "exit",
        "sleep",
        "mkdir",
        "touch",
        "test",
        "true",
        "false",
        "read",
        "shift",
        "trap",
        "wait",
        "kill",
    }
)
_STOP_PREFIXES: tuple[str, ...] = ("LOG_", "std::", "__", "boost::", "fmt::", "DB::ErrorCodes::")


def module_of(path: str) -> str:
    """Coarse module label: first two path segments (src/Interpreters)."""
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) > 1 else path


def filter_callees(
    names: Iterable[str],
    *,
    stoplist: frozenset[str] = frozenset(),
    limit: int = MAX_CALLEES,
) -> list[str]:
    """The callee names worth a place in the outline: first-seen order,
    deduplicated, the built-in stoplist and prefixes applied, `stoplist`
    (a df cap the caller computed over its corpus) applied on the bare
    name, capped at `limit`. A leading `DB::` is dropped — every callee in
    this repository lives there."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = str(raw or "").strip()
        if "<" in name:  # a template call as written: `as<ASTFunction>` is the cast `as`
            name = name.split("<", 1)[0].rstrip()
        if name.startswith("DB::"):
            name = name[4:]
        if not name or len(name) < 2 or name in seen:
            continue
        bare = name.split("::")[-1]
        if (
            name in CALLEE_STOPLIST
            or bare in CALLEE_STOPLIST
            or bare in stoplist
            or name in stoplist
            or name.startswith(_STOP_PREFIXES)
        ):
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= limit:
            break
    return out


def build_context_text(
    *,
    path: str,
    scope: str,
    signature: str = "",
    description: str = "",
    content: str,
    calls: Iterable[str] = (),
    callers: Iterable[str] = (),
) -> str:
    head_lines = [f"{path} · {module_of(path)}"]
    if scope:
        head_lines.append(f"{scope}{signature}" if signature else scope)
    if description:
        head_lines.append(description.strip())
    calls_list = [c for c in calls if c][:MAX_CALLEES]
    if calls_list:
        head_lines.append("calls: " + ", ".join(calls_list))
    callers_list = [c for c in callers if c][:MAX_CALLERS]
    if callers_list:
        head_lines.append("Called by: " + ", ".join(callers_list))
    return "\n".join(head_lines) + "\n---\n" + content


def with_description(context_text: str, *, scope: str, description: str) -> str:
    """`context_text` with `description` set as its description line, every
    other head line (signature, calls, callers) and the content kept: what
    `build_context_text` would have produced with that description."""
    head, sep, content = context_text.partition("\n---\n")
    if not sep:
        return context_text
    lines = head.split("\n")
    at = 2 if scope and len(lines) > 1 else 1
    rest = lines[at:]
    if rest and not rest[0].startswith(("calls: ", "Called by: ")):
        rest = rest[1:]  # the description line already there
    return "\n".join([*lines[:at], description.strip(), *rest]) + sep + content
