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

from typing import Any

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
    """A match's scope without the module form of its own path."""
    for m in out.get("matches") or []:
        if not isinstance(m, dict):
            continue
        scope, path = str(m.get("scope") or ""), str(m.get("path") or "")
        mod = _module_of_path(path)
        if mod and scope.startswith(mod + "::"):
            m["scope"] = scope[len(mod) + 2 :]


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


# A definition body over this many characters (about 1,500 tokens) is left to a
# ranged Read: an agent reading the unit reads it from its checkout either way,
# and a body it did not need is paid for on every later turn.
_BODY_MAX_CHARS = 6000
_NO_DOC = "No documentation available."
# Search hits past this rank keep where they are, not what they do.
_DESCRIBED_HITS = 3


def _lean_symbol(out: dict[str, Any]) -> None:
    """A definition that carries its body leaves out its outline (the body
    shows the members); a body over `_BODY_MAX_CHARS` becomes a ranged Read.
    Asked for a class, the class answers: definitions of
    its own members that matched the bare name (its constructor) keep their
    path and span, not their bodies; `next` fetches one."""
    defs = [d for d in out.get("definitions") or [] if isinstance(d, dict)]
    for d in defs:
        if d.get("body") and d.get("outline"):
            d.pop("outline")
        if d.get("description") == _NO_DOC:
            d.pop("description")
        body = d.get("body")
        if isinstance(body, str) and len(body) > _BODY_MAX_CHARS and d.get("start_line"):
            # a long body is read once, from the file, in the range the agent picks
            d.pop("body")
            d["elided"] = True
            span = int(d.get("end_line") or d["start_line"]) - int(d["start_line"]) + 1
            d["next"] = f"Read {d.get('path')} offset={d['start_line']} limit={span}"
    if not defs or defs[0].get("granularity") != "skeleton" or not defs[0].get("body"):
        return
    owner = str(defs[0].get("symbol") or "")
    for d in defs[1:]:
        sym = str(d.get("symbol") or "")
        if owner and sym.startswith(owner + "::") and d.get("body"):
            d.pop("body")
            d["elided"] = True
            d["next"] = f"code.symbol name={sym}"


_PER_OP = {
    "code.search": _lean_search,
    "code.symbol": _lean_symbol,
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
        "code.callers",
        "code.callees",
        "code.outline",
        "code.grep",
        "code.enclosing",
        "code.ask",
        "code.context_pack",
    }
)


def lean_code(op_name: str, payload: Any) -> Any:
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
    fn = _PER_OP.get(op_name)
    if fn is not None:
        fn(out)
    _hoist_defaults(out)
    if out.get("candidates") == []:
        out.pop("candidates")
    return out
