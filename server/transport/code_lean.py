"""An MCP code answer carries what the agent acts on.

`lean()` already drops empty values and the retrieval diagnostics. A code
result still carries, per hit, the bookkeeping of how it was found: 32-hex
symbol ids next to the qualified names they identify, the seed of a callers
walk repeated on every edge, `tier` / `edge_kind` / `confidence` at their
default on every row, the module prefix on every outline entry, and a full
`as_of` block (commit, branch, index time, lag, flags) on every answer —
about 46% of a `code.callers` answer's edge bytes (~2,900 tokens average).

What stays, per tool, is what an agent needs to act: path, line / span,
symbol and signature, the body or answer, the `next` step, paging, and
every correctness signal — `epistemic`, `causes`, `abstain`,
`retrieval_risk`, a stale or lagging index, a truncated or floor count.
`trace_run_id` stays: the plugin's capture hook labels outcomes with it.
HTTP and SDK shapes are unchanged; `explain=true` skips all trimming.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from io import StringIO
from typing import Any, NamedTuple

from pydantic import ValidationError

from server.code.api.models_common import _safe_path
from server.code.api.models_read import (
    CODE_READ_MAX_RANGES,
    CODE_READ_RANGE_TEXT_MAX_CHARS,
    CodeReadRangeInput,
)
from server.code.api.models_symbol import EDIT_CONTEXT_MAX_NAMES, CodeEditContextInput
from server.code.app.symbol.resolve import _parse_file_symbol, _parse_location
from server.code.domain.extract.py_notebooks import is_notebook_path
from server.code.domain.scope import current_code_scope
from server.code.domain.symbol_documentation import description_repeats_body
from server.settings import settings

# Accounting counters and ranking internals no agent acts on.
_DROP_KEYS: frozenset[str] = frozenset(
    {
        "took_ms",
        "tokens_returned",
        "bodies_returned",
        "bodies_elided",
        "anchors",
        "neighbour_tokens",
        "paths_indexed",
        "context_tokens",
        "tokens_used",
        "chunks_matched",
        "route",
        "constrained",
        "top_similarity",
        "cache_similarity",
    }
)
# Per-hit ranking internals, by tool: the order already carries the rank.
# `via` on a callers edge (registration / override) is NOT this — it stays.
_HIT_DROP: dict[str, frozenset[str]] = {
    "code.search": frozenset({"score", "lexical_rank", "vector_rank", "via", "unit_id"}),
    "code.ask": frozenset({"unit_id"}),
    "code.symbol": frozenset({"score", "unit_id"}),
    "code.edit_context": frozenset({"score", "unit_id"}),
}
# Flags that only speak when true — false is the default reading.
_TRUE_ONLY: frozenset[str] = frozenset(
    {
        "elided",
        "has_more",
        "truncated",
        "uses_truncated",
        "anchors_capped",
        "not_covered",
        "coverage_floor",
        "ambiguous",
        "is_revert",
        "via_override",
        "counts_floor",
    }
)
# Counts that only speak when non-zero.
_NONZERO_ONLY: frozenset[str] = frozenset({"unresolved_calls", "ambiguous_calls", "files_total"})
# Lists whose items share a `tier`: hoisted to one top-level value.
_HIT_LISTS: tuple[str, ...] = ("results", "definitions", "entries", "edges", "matches", "citations")


def _compact_as_of(a: Any) -> Any:
    """`{branch, commit[:9], status}` for a fresh index; every stale / lag /
    fetch field kept when the index is not fresh."""
    if not isinstance(a, dict):
        return a
    fresh = a.get("status") == "fresh" and not a.get("stale") and not a.get("lag_commits")
    out = {k: a[k] for k in ("branch", "status") if a.get(k)}
    if a.get("commit"):
        out["commit"] = str(a["commit"])[:9]
    if not fresh:
        for k, v in a.items():
            if k not in out and k != "commit" and v not in (None, "", 0, False):
                out[k] = v
    return out


def _scrub(row: dict[str, Any], extra: frozenset[str] = frozenset()) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in row.items():
        if k in _DROP_KEYS or k in extra:
            continue
        if k in _TRUE_ONLY and not v:
            continue
        if k in _NONZERO_ONLY and not v:
            continue
        if k == "as_of":
            v = _compact_as_of(v)
        out[k] = v
    return out


# Per-hit fields whose most common value becomes a list-wide default: a row
# names its own value only where it differs (`defaults` says the rest).
_DEFAULTED: tuple[str, ...] = ("tier", "kind")


def _hoist_defaults(out: dict[str, Any]) -> None:
    for key in _HIT_LISTS:
        rows = out.get(key)
        if not isinstance(rows, list) or len(rows) < 3:
            continue
        defaults: dict[str, Any] = {}
        for field in _DEFAULTED:
            vals = [r.get(field) for r in rows if isinstance(r, dict) and field in r]
            if len(vals) < 3:
                continue
            top = max(set(vals), key=vals.count)
            if vals.count(top) * 2 > len(rows):
                defaults[field] = top
        if not defaults:
            continue
        out[key] = [
            {k: v for k, v in r.items() if not (k in defaults and v == defaults[k])}
            if isinstance(r, dict)
            else r
            for r in rows
        ]
        out.setdefault("defaults", {}).update(defaults)


def _lean_callers(out: dict[str, Any]) -> None:
    """The seed named once; ids only where a caller's name is ambiguous;
    `calls` / confidence 1.0 are the defaults; zero candidate sites say
    nothing. `edges[].path` stays (the plugin reads it)."""
    edges = [e for e in out.get("edges") or [] if isinstance(e, dict)]
    if not edges:
        return
    seeds = {e.get("to_symbol") for e in edges}
    one_seed = len(seeds) == 1 and None not in seeds
    if one_seed and "seed" not in out:
        out["seed"] = seeds.pop()
    names: dict[str, set[str]] = {}
    for e in edges:
        names.setdefault(str(e.get("from_symbol")), set()).add(str(e.get("from_symbol_id")))
    lean_edges = []
    for e in edges:
        row = {k: v for k, v in e.items() if not (one_seed and k == "to_symbol")}
        if len(names.get(str(e.get("from_symbol")), ())) < 2:
            row.pop("from_symbol_id", None)
        row.pop("to_symbol_id", None)
        if row.get("edge_kind") == "calls":
            row.pop("edge_kind")
        if isinstance(row.get("confidence"), (int, float)) and row["confidence"] >= 0.999:
            row.pop("confidence")
        lean_edges.append(row)
    out["edges"] = lean_edges
    if out.get("seed_symbols") == [out.get("seed")]:
        out.pop("seed_symbols")
    cs = out.get("candidate_sites")
    if isinstance(cs, dict) and not cs.get("total"):
        out.pop("candidate_sites")
    if not out.get("truncated"):
        out.pop("by_hop_counts", None)  # the edges are the whole count


def _module_of_path(path: str) -> str:
    stem = path.rsplit(".", 1)[0] if "." in path.rsplit("/", 1)[-1] else path
    return stem.replace("/", ".")


def _shared_scope(names: list[str]) -> str:
    """The longest `::`-segment prefix every name extends (the file's module
    in Python, its namespace + class in C++); "" when there is none."""
    parts = [n.split("::") for n in names]
    common: list[str] = []
    for i in range(min(len(p) for p in parts) - 1 if parts else 0):
        seg = {p[i] for p in parts}
        if len(seg) != 1:
            break
        common.append(seg.pop())
    return "::".join(common)


def _lean_outline(out: dict[str, Any]) -> None:
    """The shared scope said once (`module`), each entry named within it."""
    entries = [e for e in out.get("entries") or [] if isinstance(e, dict)]
    qn = [str(e.get("qualified_name") or "") for e in entries]
    if not entries or not all("::" in q for q in qn):
        return
    mod = _shared_scope(qn)
    if not mod:
        return
    out["module"] = mod
    cut = len(mod) + 2
    out["entries"] = [
        {
            ("name" if k == "qualified_name" else k): (v[cut:] if k == "qualified_name" else v)
            for k, v in e.items()
        }
        for e in entries
    ]
    anon = f"$anonymous_namespace_{out.get('path', '')}::"
    for e in out["entries"]:
        if isinstance(e.get("name"), str) and anon in e["name"]:
            e["name"] = e["name"].replace(anon, "(anonymous namespace)::")  # code.symbol takes it
    if out.get("indexed") is True:
        out.pop("indexed")  # `indexed: false` is the one that says "read the file"


def _lean_grep(out: dict[str, Any]) -> None:
    """Keep grep locations and source, omitting metadata the body already supplies."""
    for m in out.get("matches") or []:
        if not isinstance(m, dict):
            continue
        scope, path = str(m.get("scope") or ""), str(m.get("path") or "")
        mod = _module_of_path(path)
        if mod and scope.startswith(mod + "::"):
            m["scope"] = scope[len(mod) + 2 :]
    _share_grep_match_text(out)
    definitions = [row for row in out.get("definitions") or [] if isinstance(row, dict)]
    for definition in definitions:
        _lean_grep_definition(definition)
    _share_grep_bodies(definitions)
    related = out.get("related")
    if isinstance(related, dict):
        out["related"] = lean_code("code.grep", related)


def _share_grep_match_text(out: dict[str, Any]) -> None:
    """Use the excerpt for exact match text at the same path and line."""
    matches, contexts = out.get("matches"), out.get("contexts")
    if not (isinstance(matches, list) and matches and isinstance(contexts, list) and contexts):
        return
    source: dict[tuple[str, int], str | None] = {}
    for context in contexts:
        if not isinstance(context, dict):
            continue
        path, start, end, text = (
            context.get(k) for k in ("path", "start_line", "end_line", "text")
        )
        if not (
            isinstance(path, str)
            and path
            and isinstance(start, int)
            and isinstance(end, int)
            and 0 < start <= end
            and isinstance(text, str)
        ):
            continue
        lines = list(StringIO(text, newline="\n"))
        if len(lines) != end - start + 1:
            continue
        for line_number, source_line in enumerate(lines, start):
            key = (path, line_number)
            if source.setdefault(key, source_line) != source_line:
                source[key] = None

    shared = []
    for match in matches:
        if isinstance(match, dict):
            path, line, text = (match.get(k) for k in ("path", "line", "text"))
            if isinstance(path, str) and isinstance(line, int) and isinstance(text, str) and text:
                raw = source.get((path, line))
                if raw is not None and raw.strip() == text:
                    match = {key: value for key, value in match.items() if key != "text"}
        shared.append(match)
    compact = {"matches": shared, "match_text_in_context": True}
    if shared != matches and _structured_chars(compact) < _structured_chars({"matches": matches}):
        out.update(compact)


_PYTHON_DECLARATION = re.compile(r"^(?:async\s+)?def\s+([^\W\d]\w*)\s*[\[(]")


def _python_declaration_at_hit(hit: dict[str, Any], path: str) -> bool:
    definition = _PYTHON_DECLARATION.match(str(hit.get("text") or "").lstrip())
    return bool(
        path.endswith((".py", ".pyi"))
        and definition
        and definition[1] == str(hit["scope"]).rsplit("::", 1)[-1]
    )


def _grep_edit_selector(hit: dict[str, Any], span: CodeReadRangeInput) -> str:
    if hit["line"] == span.start_line:
        name = f"{span.path}:{span.start_line}"
        if _python_declaration_at_hit(hit, span.path) and _parse_location(name) == (
            span.path,
            span.start_line,
        ):
            return name
        return ""
    scope = str(hit["scope"])
    module, _, member = scope.partition("::")
    # A dotted module name cannot be mistaken for a class suffix in this file.
    if not (
        span.path.endswith((".py", ".pyi"))
        and "." in module
        and all(part.isidentifier() for part in member.split("::"))
        and not str(hit.get("text") or "").lstrip().startswith("@")
    ):
        return ""
    name = f"{span.path}:{scope}"
    return name if _parse_file_symbol(name) == (span.path, scope) else ""


class _GrepFollowup(NamedTuple):
    span: CodeReadRangeInput
    name: str
    declaration_hit: bool


def _grep_edit_names(items: list[_GrepFollowup]) -> list[str]:
    if not 0 < len(items) <= EDIT_CONTEXT_MAX_NAMES:
        return []
    names = [item.name for item in items]
    if len(set(names)) != len(names):
        return []
    try:
        return CodeEditContextInput(names=names).names
    except ValidationError:
        return []


def _grep_read_range(span: CodeReadRangeInput) -> str | dict[str, Any]:
    last = span.end_line if span.end_line is not None else span.start_line
    location = f"{span.path}:{span.start_line}-{last}"
    # The object form can hold a path whose suffix would exceed the string field's limit.
    return location if len(location) <= CODE_READ_RANGE_TEXT_MAX_CHARS else span.model_dump()


def _grep_continuation(
    out: dict[str, Any],
    arguments: dict[str, Any],
    followups: frozenset[str],
) -> None:
    """Give excerpt callers the source spans covering their matches."""
    if (
        arguments.get("include_body") is not False
        or "loom_code_read" not in followups
        or out.get("definitions")
    ):
        return
    evidence = [out]
    related = out.get("related")
    if isinstance(related, dict) and related.get("matches") and not related.get("definitions"):
        evidence.append(related)
    contexts = [window for item in evidence for window in item.get("contexts") or []]
    selections: dict[tuple[str, int, int], _GrepFollowup] = {}
    hits: list[dict[str, Any]] = []
    # Exact hits come first, with declaration matches first within each group.
    for item in evidence:
        matches = [hit for hit in item.get("matches") or [] if isinstance(hit, dict)]
        hits.extend(sorted(matches, key=lambda hit: hit.get("line") != hit.get("scope_start_line")))
    for hit in hits:
        path, line, start, end = (
            hit.get(key) for key in ("path", "line", "scope_start_line", "scope_end_line")
        )
        if not (path and start and line and end and hit.get("scope") and start <= line <= end):
            continue
        if any(
            window["path"] == path and window["start_line"] <= start <= end <= window["end_line"]
            for window in contexts
        ):
            continue
        try:
            span = CodeReadRangeInput(path=path, start_line=start, end_line=end)
        except ValidationError:
            continue
        if not _safe_path(span.path) or "\0" in span.path or is_notebook_path(span.path):
            continue
        key = span.path, span.start_line, end
        if key not in selections:
            selections[key] = _GrepFollowup(span, _grep_edit_selector(hit, span), line == start)
        if len(selections) == CODE_READ_MAX_RANGES:
            break
    if not selections:
        return
    scope = (arguments.get("org"), arguments.get("namespace"))
    # A cell checks the organization before applying the connection's scope.
    options = (
        {}
        if not settings.cell and scope == current_code_scope()
        else {key: arguments[key] for key in ("org", "namespace") if arguments.get(key)}
    )
    options.update(
        {
            key: arguments[key]
            for key in ("include_lessons", "lesson_namespaces")
            if key in arguments
        }
    )
    selected = list(selections.values())
    names: list[str] = []
    has_edit_context = "loom_code_edit_context" in followups
    exact_read = bool(contexts) or len(evidence) > 1 or (has_edit_context and len(selected) == 1)
    if has_edit_context:
        names = _grep_edit_names(selected)
        if not names:
            declarations = [item for item in selected if item.declaration_hit]
            names = _grep_edit_names(declarations)
            if not exact_read:
                selected = declarations
    if names:
        out["edit_context_next"] = {
            "tool": "loom_code_edit_context",
            "arguments": {"names": names, **options},
        }
    elif not exact_read:
        return
    params = {"ranges": [_grep_read_range(item.span) for item in selected], **options}
    out["next"] = "For the full source, call loom_code_read with " + json.dumps(
        params, separators=(",", ":")
    )


class _GrepBody(NamedTuple):
    row: dict[str, Any]
    start: int
    end: int
    lines: list[str]


def _grep_body(definition: dict[str, Any]) -> _GrepBody | None:
    body = definition.get("body")
    start, end = definition.get("start_line"), definition.get("end_line")
    if not (
        isinstance(body, str)
        and body
        and isinstance(start, int)
        and isinstance(end, int)
        and 0 < start <= end
        and not definition.get("body_elided")
    ):
        return None
    lines = list(StringIO(body, newline="\n"))
    return _GrepBody(definition, start, end, lines) if len(lines) == end - start + 1 else None


def _share_grep_bodies(definitions: list[dict[str, Any]]) -> None:
    """Reference an already returned body only when its covered lines are identical."""
    if len(definitions) < 2:
        return
    ids = Counter(str(row.get("symbol_id") or "") for row in definitions)
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in definitions:
        if (path := row.get("path")) and row.get("body"):
            by_path[str(path)].append(row)
    for rows in by_path.values():
        if len(rows) < 2:
            continue
        bodies = [body for row in rows if (body := _grep_body(row))]
        keep: list[_GrepBody] = []
        for child in sorted(bodies, key=lambda b: b.start - b.end):
            parent_id = _containing_grep_body(child, keep)
            if parent_id:
                child.row.pop("body")
                child.row["body_in_symbol_id"] = parent_id
            elif (symbol_id := child.row.get("symbol_id")) and ids[str(symbol_id)] == 1:
                keep.append(child)


def _containing_grep_body(child: _GrepBody, candidates: list[_GrepBody]) -> str:
    for parent in candidates:
        if (parent.start, parent.end) == (child.start, child.end):
            continue
        first, last = child.start - parent.start, child.end - parent.start + 1
        parent_id = str(parent.row["symbol_id"])
        if not (0 <= first < last <= len(parent.lines) and child.lines == parent.lines[first:last]):
            continue
        if _grep_reference_is_smaller(child.row["body"], parent_id):
            return parent_id
    return ""


def _grep_reference_is_smaller(body: str, symbol_id: str) -> bool:
    reference = {"body_in_symbol_id": symbol_id}
    source = {"body": body}
    if len(json.dumps(reference)) >= len(json.dumps(source)):
        return False
    reference_json = json.dumps(reference, ensure_ascii=False)
    source_json = json.dumps(source, ensure_ascii=False)
    # Clients can measure structured results in UTF-8 bytes or JavaScript string length.
    return all(
        len(reference_json.encode(encoding)) < len(source_json.encode(encoding))
        for encoding in ("utf-8", "utf-16-le")
    )


def _lean_grep_definition(definition: dict[str, Any]) -> None:
    body = definition.get("body")
    if not isinstance(body, str) or not body or definition.get("body_elided"):
        return
    symbol = definition.get("symbol")
    qualified_name = definition.get("qualified_name")
    if (
        isinstance(symbol, str)
        and symbol
        and isinstance(qualified_name, str)
        and qualified_name.endswith(f"::{symbol}")
    ):
        definition.pop("symbol")
    signature = definition.get("signature")
    if isinstance(signature, str) and signature:
        source = body.lstrip()
        start = source.find(signature)
        first_break = source.find("\n")
        if start >= 0 and (first_break < 0 or start < first_break):
            definition.pop("signature")
    if definition.get("body_elided") is False:
        definition.pop("body_elided")


def _lean_enclosing(out: dict[str, Any]) -> None:
    for r in out.get("results") or []:
        if not isinstance(r, dict):
            continue
        qn, sym = str(r.get("qualified_name") or ""), str(r.get("symbol") or "")
        if sym and qn.endswith(sym):
            r.pop("symbol")
        r.pop("symbol_id", None)
        for k in ("start_line", "end_line"):
            if r.get(k) == 0:
                r.pop(k)  # an unresolved line has no span


def _lean_prs(rows: Any) -> None:
    for d in rows or []:
        for p in (d.get("recent_prs") or []) if isinstance(d, dict) else []:
            if isinstance(p, dict) and isinstance(p.get("merged_at"), str):
                p["merged_at"] = p["merged_at"][:10]


# Library namespaces whose functions a hit's callee list repeats on every
# unit (`std::chrono::system_clock::now`, `boost::shared_ptr::operator*`).
_LIBRARY_CALLEES: tuple[str, ...] = ("std::", "boost::", "builtins::", "__gnu_cxx::")


def _lean_search(out: dict[str, Any]) -> None:
    """A hit's description without its `Answers:` questions (they are the
    retrieval text, not news to the agent who asked), and only on the first
    `_DESCRIBED_HITS` hits; its callees as the repository's own function names,
    the library ones dropped."""
    for rank, r in enumerate(out.get("results") or []):
        if not isinstance(r, dict):
            continue
        if rank >= _DESCRIBED_HITS:
            r.pop("description", None)
        desc = r.get("description")
        if isinstance(desc, str) and " Answers:" in desc:
            r["description"] = desc.split(" Answers:", 1)[0]
        callees = r.get("callees")
        if isinstance(callees, list):
            names = [
                str(c.get("name") if isinstance(c, dict) else c)
                for c in callees
                if not str(c.get("name") if isinstance(c, dict) else c).startswith(_LIBRARY_CALLEES)
            ]
            if names:
                r["callees"] = names
            else:
                r.pop("callees")


_NO_DOC = "No documentation available."
# Search hits past this rank keep where they are, not what they do.
_DESCRIBED_HITS = 3


def _lean_symbol(out: dict[str, Any]) -> None:
    """Keep requested source and leave out an outline the body already supplies.
    Asked for a class, the class answers: definitions of
    its own members that matched the bare name (its constructor) keep their
    path and span, and `next` fetches their bodies. An edit batch names each
    target separately and keeps requested member bodies."""
    defs = [d for d in out.get("definitions") or [] if isinstance(d, dict)]
    for d in defs:
        if d.get("body") and d.get("outline"):
            d.pop("outline")
    if (
        not out.get("edit_targets")
        and defs
        and defs[0].get("granularity") == "skeleton"
        and defs[0].get("body")
    ):
        owner = str(defs[0].get("symbol") or "")
        for d in defs[1:]:
            sym = str(d.get("symbol") or "")
            if owner and sym.startswith(owner + "::") and d.get("body"):
                d.pop("body")
                d["elided"] = True
                d["next"] = f"code.symbol name={sym}"
    for d in defs:
        description = d.get("description")
        body = d.get("body")
        if description == _NO_DOC or (
            isinstance(description, str)
            and isinstance(body, str)
            and not (d.get("elided") or d.get("granularity") == "skeleton")
            and description_repeats_body(description, body, scip=d.get("tier") == "scip")
        ):
            d.pop("description")


def _edit_test_sources(
    definitions: list[Any],
) -> dict[tuple[str, str, int, int, str], list[tuple[int, int]]]:
    groups: dict[tuple[str, str, int, int, str], list[tuple[int, int]]] = defaultdict(list)
    for i, definition in enumerate(definitions):
        if not isinstance(definition, dict) or not isinstance(definition.get("examples"), list):
            continue
        for j, example in enumerate(definition["examples"]):
            if not isinstance(example, dict) or not isinstance(example.get("context"), dict):
                continue
            context = example["context"]
            path, commit, body = example.get("path"), context.get("commit"), context.get("text")
            start, end = context.get("start_line"), context.get("end_line")
            if (
                isinstance(path, str)
                and path
                and isinstance(commit, str)
                and commit
                and isinstance(body, str)
                and body
                and isinstance(start, int)
                and isinstance(end, int)
            ):
                groups[path, commit, start, end, body].append((i, j))
    return groups


def _structured_chars(value: dict[str, Any]) -> int:
    # Session capture counts the UTF-16 units in compact JSON.
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return len(rendered.encode("utf-16-le", errors="surrogatepass")) // 2


def _shared_excerpt(
    path: str, body: str, definitions: list[Any], locations: list[tuple[int, int]]
) -> dict[str, Any]:
    # Capture must read the source and its identifiers in the same piece.
    callers = dict.fromkeys(
        caller
        for i, j in locations
        if isinstance((caller := definitions[i]["examples"][j].get("caller")), str) and caller
    )
    owners = dict.fromkeys(
        (str(definition.get("symbol") or ""), str(definition.get("path") or ""))
        for i, _ in locations
        if (definition := definitions[i])
    )
    return {
        "path": path,
        "callers": list(callers),
        "definitions": [{"symbol": symbol, "path": owner_path} for symbol, owner_path in owners],
        "text": body,
    }


def _share_edit_test_source(out: dict[str, Any]) -> dict[str, Any]:
    """Keep shared text in a standalone result row for clients that split top-level lists."""
    if "test_sources" in out:
        return out
    definitions = out["definitions"]
    sources: list[dict[str, Any]] = []
    references: dict[tuple[int, int], str] = {}
    for (path, _, _, _, body), locations in _edit_test_sources(definitions).items():
        if len(locations) < 2:
            continue
        pointer = f"#/test_sources/0/excerpts/{len(sources)}/text"
        text_size = len(json.dumps({"text": body}))
        ref_size = len(json.dumps({"text_ref": pointer}))
        excerpt = _shared_excerpt(path, body, definitions, locations)
        if len(locations) * text_size > len(json.dumps(excerpt)) + len(locations) * ref_size:
            sources.append(excerpt)
            references.update(dict.fromkeys(locations, pointer))
    if not sources:
        return out
    shared = {"test_sources": [{"excerpts": sources}]}
    original_rows = {i for i, _ in references}
    # A client that retained any old source row can retain the whole source row here.
    if _structured_chars(shared) > min(
        _structured_chars({"definitions": [definitions[i]]}) for i in original_rows
    ):
        return out
    updated = [
        {**d, "examples": list(d["examples"])} if i in original_rows else d
        for i, d in enumerate(definitions)
    ]
    for (i, j), pointer in references.items():
        example = updated[i]["examples"][j]
        context = {key: value for key, value in example["context"].items() if key != "text"}
        updated[i]["examples"][j] = {**example, "context": {**context, "text_ref": pointer}}
    packed = {**shared, **out, "definitions": updated}
    return packed if len(json.dumps(packed)) < len(json.dumps(out)) else out


_PER_OP = {
    "code.search": _lean_search,
    "code.symbol": _lean_symbol,
    "code.edit_context": _lean_symbol,
    "code.callers": _lean_callers,
    "code.callees": _lean_callers,
    "code.outline": _lean_outline,
    "code.grep": _lean_grep,
    "code.enclosing": _lean_enclosing,
}


# The tools this shape was reviewed for; every other code.* op (pr_check,
# bisect_candidates, impact, …) keeps lean() only.
LEAN_CODE_OPS: frozenset[str] = frozenset(
    {
        "code.search",
        "code.symbol",
        "code.edit_context",
        "code.callers",
        "code.callees",
        "code.outline",
        "code.grep",
        "code.enclosing",
        "code.ask",
        "code.context_pack",
    }
)


def lean_code(
    op_name: str,
    payload: Any,
    arguments: dict[str, Any] | None = None,
    *,
    followups: frozenset[str] = frozenset(),
) -> Any:
    """The MCP shape of a code.* result (see module docstring)."""
    if not isinstance(payload, dict) or op_name not in LEAN_CODE_OPS:
        return payload
    out = _scrub(payload)
    extra = _HIT_DROP.get(op_name, frozenset())
    for key in _HIT_LISTS:
        if isinstance(out.get(key), list):
            out[key] = [_scrub(r, extra) if isinstance(r, dict) else r for r in out[key]]
    for d in out.get("definitions") or []:
        if isinstance(d, dict) and isinstance(d.get("recent_prs"), list):
            d["recent_prs"] = [_scrub(p) if isinstance(p, dict) else p for p in d["recent_prs"]]
    if isinstance(out.get("definitions"), list) and len(out["definitions"]) > 1:
        # several definitions: the ids are how the agent picks one
        for src, d in zip(payload.get("definitions") or [], out["definitions"], strict=False):
            if isinstance(src, dict) and isinstance(d, dict) and src.get("unit_id"):
                d["symbol_id"] = src["unit_id"]
    _lean_prs(out.get("definitions"))
    if op_name == "code.grep" and arguments is not None and followups:
        evidence = out
        if not out.get("matches") and isinstance(related := out.get("related"), dict):
            evidence = dict(related)
            out["related"] = evidence
        _grep_continuation(evidence, arguments, followups)
    fn = _PER_OP.get(op_name)
    if fn is not None:
        fn(out)
    if op_name == "code.edit_context" and out.get("edit_targets") and out.get("definitions"):
        out = _share_edit_test_source(out)
    _hoist_defaults(out)
    if out.get("candidates") == []:
        out.pop("candidates")
    return out
