"""The conditions that guard a call site.

For every `calls` edge the fast tier records, the dominating conditions
inside the caller function, read off the syntax tree and nothing else:

  enclosing   the condition text of each `if` / `else` / `switch` case /
              ternary / `for`-`while` header / preprocessor conditional the
              call sits inside, innermost GUARD_MAX_LEVELS levels, listed
              outermost first;
  early exit  `if (cond) return|throw|continue|break` statements textually
              before the call in the blocks that enclose it (the same
              function body), nearest EARLY_EXIT_MAX, in source order.

`guard_kinds` names what each text is; the text is always the condition as
written (whitespace collapsed, GUARD_MAX_CHARS), so the else branch of
`if (x)` carries `x` under kind `else` and identical conditions compress to
one string. Polarity lives in the kind, not the text: the question the op
answers is "does a guard on this path NAME the identifier", which is
polarity-free.

This is the analyst's read made mechanical — intraprocedural and syntactic,
a lower bound in every direction: a check hoisted into a helper, a
precondition asserted by the caller's caller, a guard that names an alias
of the identifier, a `try`/`catch`, a `[[likely]]` — none are seen. Most conditionals in a
function are not security checks, so the op reports the guard that names the
identifier and leaves sufficiency to the reader. Lambdas are function boundaries: a guard
outside the lambda decides whether it is created, not whether it runs.

A call under no condition yields no record — the table is sparse, and the
op distinguishes "unguarded" from "not indexed" with a namespace probe.

Storage (`loom.code_caller_guards`): one record per CALLER function per file,
not per call site. Per-site rows would repeat a function's early exits in every
one of its call sites and write the callee id once per site although the edge
row already holds it (28% of code_edges on a 300-file subset). A caller record
lists every distinct (kind, condition) once (`guards` / `guard_kinds`,
parallel) and, per guarded call site, its line (`sites`), a 32-bit tag of its
callee (`site_callees`, `callee_tag`) and the indexes of the guards that
dominate it in the order they apply (`site_guards`) — so the read still judges
every call site of a (caller, callee) pair on its own guards (`site_guards_of`).

CH-free and pure: `extract_call_guards(pf, edges)` takes the parse and the
edges the same pass derived and returns records the ingest turns into rows.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from typing import Any

from server.code.domain.extract.edges_treesitter import Edge, _call_name
from server.code.domain.extract.treesitter import ParsedFile, node_text

GUARD_MAX_CHARS = 200
GUARD_MAX_LEVELS = 3
EARLY_EXIT_MAX = 8
# distinct (kind, condition) pairs one caller record lists — `site_guards`
# indexes are UInt8; a function with more distinct conditions than this
# keeps the first 255 in site order and its later sites reference only those
CALLER_GUARDS_MAX = 255

_WS_RE = re.compile(r"\s+")

# Per language: the call node, the nodes that end the intraprocedural walk,
# the block node whose direct children are the statements an early exit can
# be among, and the statement kinds that end a block early.
_LANG: dict[str, dict[str, Any]] = {
    "cpp": {
        "call": "call_expression",
        "boundary": {"function_definition", "lambda_expression"},
        "block": {"compound_statement"},
        "exit": {
            "return_statement": "early_return",
            "throw_statement": "early_throw",
            "continue_statement": "early_continue",
            "break_statement": "early_break",
            "goto_statement": "early_return",
        },
    },
    "python": {
        "call": "call",
        "boundary": {"function_definition", "lambda"},
        "block": {"block"},
        "exit": {
            "return_statement": "early_return",
            "raise_statement": "early_throw",
            "continue_statement": "early_continue",
            "break_statement": "early_break",
        },
    },
}


@dataclass(frozen=True)
class CallerGuards:
    """The guard record of one caller function in one file:
    `guards`/`guard_kinds` list each distinct (kind, condition) once;
    `sites[i]` / `site_callees[i]` / `site_guards[i]` describe the i-th
    guarded call site — its line, `callee_tag` of its callee, and the
    indexes into `guards` that dominate it, early exits first in source
    order, then the enclosing conditions outermost first."""

    from_symbol_id: str
    path: str
    guards: tuple[str, ...]
    guard_kinds: tuple[str, ...]
    sites: tuple[int, ...]
    site_callees: tuple[int, ...]
    site_guards: tuple[tuple[int, ...], ...]


def callee_tag(symbol_id: str) -> int:
    """A 32-bit tag of a callee's symbol id — the first eight hex digits of
    the id (symbol ids are 32-hex sha256 prefixes), crc32 of anything else.
    It tells the call sites of one caller apart by callee (two sites on one
    line, `x ? g(x) : h(x)`); the full id is on the edge row the walk
    already holds, so the table does not repeat it per site."""
    head = symbol_id[:8]
    if len(head) == 8 and all(c in "0123456789abcdefABCDEF" for c in head):
        return int(head, 16)
    return zlib.crc32(symbol_id.encode("utf-8", "replace")) & 0xFFFFFFFF


def site_guards_of(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, int, str, int], list[dict[str, str]]]:
    """Read side: caller rows -> {(from_symbol_id, callee_tag, path, line):
    [{kind, text}, ...]} — the per-site guard list the op judges, in the
    order the extractor derived it. An index the row does not hold (a
    truncated caller record) is skipped, never an error."""
    out: dict[tuple[str, int, str, int], list[dict[str, str]]] = {}
    for r in rows:
        texts = [str(g) for g in (r.get("guards") or [])]
        kinds = [str(k) for k in (r.get("guard_kinds") or [])]
        sites = list(r.get("sites") or [])
        callees = list(r.get("site_callees") or [])
        per_site = list(r.get("site_guards") or [])
        for line, tag, idxs in zip(sites, callees, per_site, strict=False):
            out[(str(r["from_symbol_id"]), int(tag), str(r["path"]), int(line))] = [
                {"kind": kinds[i], "text": texts[i]}
                for i in idxs
                if 0 <= int(i) < len(texts) and int(i) < len(kinds)
            ]
    return out


def normalize_guard(text: str) -> str:
    """One line, single spaces, outer parentheses of a condition clause
    dropped, GUARD_MAX_CHARS."""
    t = _WS_RE.sub(" ", text).strip()
    if t.startswith("(") and t.endswith(")") and _balanced_outer(t):
        t = t[1:-1].strip()
    return t[:GUARD_MAX_CHARS]


def _balanced_outer(t: str) -> bool:
    """True when the first '(' closes at the last char (so stripping the
    pair keeps `(a) && (b)` intact)."""
    depth = 0
    for i, ch in enumerate(t):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i == len(t) - 1
    return False


def guard_names(guard: str, mentions: str) -> bool:
    """Does the guard text name `mentions`? An identifier matches on word
    boundaries (`ok` does not match `is_ok`); anything with punctuation
    (`p->isReadOnly()`, `Foo::bar`) matches as a whitespace-normalised
    substring. Case-sensitive: these are program identifiers."""
    m = _WS_RE.sub(" ", mentions).strip()
    if not m:
        return False
    g = _WS_RE.sub(" ", guard)
    if re.fullmatch(r"\w+", m):
        return re.search(rf"(?<!\w){re.escape(m)}(?!\w)", g) is not None
    return m in g


# --------------------------------------------------------------------------- the walk


def _field_of(parent: Any, child: Any) -> str:
    """The field name `child` occupies on `parent` ('' when unnamed)."""
    for i, c in enumerate(parent.children):
        if c.id == child.id:
            return parent.field_name_for_child(i) or ""
    return ""


def _condition_text(node: Any, source: bytes) -> str:
    cond = node.child_by_field_name("condition")
    return normalize_guard(node_text(cond, source)) if cond is not None else ""


def _preproc_directive(node: Any, source: bytes) -> str:
    """`#ifdef NAME` / `#ifndef NAME` / `#if COND` — the directive line as
    written, which carries its own polarity."""
    first = node_text(node, source).split("\n", 1)[0]
    return normalize_guard(first)


def _enclosing_guard(anc: Any, child: Any, lang: str, source: bytes) -> tuple[str, str] | None:
    """(text, kind) when `anc` conditions the execution of `child`, else None."""
    t = anc.type
    field = _field_of(anc, child)
    if t == "if_statement":
        if field == "consequence":
            return _condition_text(anc, source), "if"
        if field == "alternative":
            return _condition_text(anc, source), "else"
        return None
    if t == "elif_clause":  # python: its own condition guards its consequence
        return (_condition_text(anc, source), "if") if field == "consequence" else None
    if t == "switch_statement":
        return (_condition_text(anc, source), "switch") if field == "body" else None
    if t == "case_statement":
        value = anc.child_by_field_name("value")
        if value is not None and value.id == child.id:
            return None
        guard = normalize_guard(node_text(value, source)) if value is not None else "default"
        return guard, "case"
    if t == "conditional_expression":
        return _ternary_guard(anc, child, lang, source)
    if t in {"for_statement", "while_statement"}:
        if field == "body" and anc.child_by_field_name("condition") is not None:
            return _condition_text(anc, source), "loop"
        return None
    if t in {"preproc_if", "preproc_ifdef", "preproc_elif"}:
        if field == "alternative":
            return _preproc_directive(anc, source), "preproc_else"
        if field in {"condition", "name"}:
            return None
        return _preproc_directive(anc, source), "preproc"
    return None


def _ternary_guard(anc: Any, child: Any, lang: str, source: bytes) -> tuple[str, str] | None:
    if lang == "python":  # no fields: [consequence, condition, alternative]
        named = anc.named_children
        if len(named) != 3:
            return None
        cond = normalize_guard(node_text(named[1], source))
        if child.id == named[0].id:
            return cond, "ternary"
        if child.id == named[2].id:
            return cond, "ternary_else"
        return None
    field = _field_of(anc, child)
    if field == "consequence":
        return _condition_text(anc, source), "ternary"
    if field == "alternative":
        return _condition_text(anc, source), "ternary_else"
    return None


def _early_exit_kind(stmt: Any, cfg: dict[str, Any]) -> str:
    """The kind when `stmt` is `if (cond) <exit>` with no else, else ''."""
    if stmt.type != "if_statement" or stmt.child_by_field_name("alternative") is not None:
        return ""
    body = stmt.child_by_field_name("consequence")
    if body is None:
        return ""
    if body.type in cfg["block"]:
        named = body.named_children
        body = named[-1] if named else None
        if body is None:
            return ""
    return str(cfg["exit"].get(body.type, ""))


def guards_for_call(call: Any, lang: str, source: bytes) -> tuple[list[str], list[str]]:
    """(guards, kinds) for one call node — early exits first in source
    order, then the enclosing conditions outermost first. Empty when the
    call runs unconditionally within its function."""
    cfg = _LANG.get(lang)
    if cfg is None:
        return [], []
    # the path from the call up to the function boundary: (ancestor, child on the path)
    path: list[tuple[Any, Any]] = []
    child, anc = call, call.parent
    while anc is not None and anc.type not in cfg["boundary"]:
        path.append((anc, child))
        child, anc = anc, anc.parent
    if anc is not None:
        path.append((anc, child))  # the function itself, for its body block

    enclosing: list[tuple[str, str]] = []
    for a, c in path:  # innermost first
        g = _enclosing_guard(a, c, lang, source)
        if g is not None and g[0] and len(enclosing) < GUARD_MAX_LEVELS:
            enclosing.append(g)
    enclosing.reverse()

    # blocks come innermost first; source order is the outer block's exits,
    # then the inner block's — each block's own list already in source order
    per_block: list[list[tuple[str, str]]] = []
    for a, c in path:
        if a.type not in cfg["block"]:
            continue
        exits: list[tuple[str, str]] = []
        for stmt in a.named_children:
            if stmt.start_byte >= c.start_byte:
                break
            kind = _early_exit_kind(stmt, cfg)
            if kind:
                exits.append((_condition_text(stmt, source), kind))
        per_block.append(exits)
    early = [e for block in reversed(per_block) for e in block if e[0]][-EARLY_EXIT_MAX:]
    out = early + enclosing
    return [g for g, _k in out], [k for _g, k in out]


def extract_call_guards(pf: ParsedFile, edges: list[Edge]) -> list[CallerGuards]:
    """Caller records for the `calls` edges of one parsed file, one per
    caller symbol, sites ordered by (line, callee tag). A call site matches
    its edge by (line, bare callee name); a site under no condition is not
    listed, and a caller with no listed site has no record."""
    cfg = _LANG.get(pf.lang)
    if cfg is None or pf.tree is None:
        return []
    source = pf.source
    by_site: dict[tuple[int, str], Any] = {}

    def walk(node: Any) -> None:
        for child in node.named_children:
            if child.type == cfg["call"]:
                name = _call_name(child, source)
                bare = name.split("::")[-1].split(".")[-1]
                by_site.setdefault((child.start_point[0] + 1, bare), child)
            walk(child)

    walk(pf.tree.root_node)
    # (caller, path) -> {(line, callee tag): (guards, kinds)}, one site per edge key
    per_caller: dict[tuple[str, str], dict[tuple[int, int], tuple[list[str], list[str]]]] = {}
    seen: set[tuple[str, str, str, int]] = set()
    for e in edges:
        if e.edge_kind != "calls":
            continue
        key = (e.from_symbol_id, e.to_symbol_id, e.path, e.line)
        if key in seen:
            continue
        seen.add(key)
        call = by_site.get((e.line, e.to_qualified_name.split("::")[-1]))
        if call is None:
            continue
        guards, kinds = guards_for_call(call, pf.lang, source)
        if guards:
            per_caller.setdefault((e.from_symbol_id, e.path), {}).setdefault(
                (e.line, callee_tag(e.to_symbol_id)), (guards, kinds)
            )
    return [
        _caller_record(from_id, path, sites)
        for (from_id, path), sites in sorted(per_caller.items())
    ]


def _caller_record(
    from_id: str, path: str, sites: dict[tuple[int, int], tuple[list[str], list[str]]]
) -> CallerGuards:
    """Fold a caller's sites into one record: each distinct (kind, text) is
    listed once, in the order sites first use it; a site references its
    guards by index."""
    order: dict[tuple[str, str], int] = {}
    texts: list[str] = []
    kinds: list[str] = []
    lines: list[int] = []
    tags: list[int] = []
    refs: list[tuple[int, ...]] = []
    for (line, tag), (guards, gkinds) in sorted(sites.items()):
        idxs: list[int] = []
        for text, kind in zip(guards, gkinds, strict=True):
            pos = order.get((kind, text))
            if pos is None:
                if len(texts) >= CALLER_GUARDS_MAX:
                    continue
                pos = len(texts)
                order[(kind, text)] = pos
                texts.append(text)
                kinds.append(kind)
            idxs.append(pos)
        lines.append(line)
        tags.append(tag)
        refs.append(tuple(idxs))
    return CallerGuards(
        from_id, path, tuple(texts), tuple(kinds), tuple(lines), tuple(tags), tuple(refs)
    )
