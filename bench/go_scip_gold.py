"""The Go fast tier against scip-go's gold.

Two subcommands over one checkout:

    python -m bench.go_scip_gold derive  <repo dir> <fast.json>
    python -m bench.go_scip_gold compare <fast.json> <gold.json>

`derive` parses every `.go` file with the worktree's code, builds the module
index from every `go.mod`, the repository name table, and the lane context
the ingest would hand each file (package aliases, method signatures, struct
embeds, container elements, package values),
and dumps symbols, edges and misses. It is the fast tier with the
repository's rows replaced by the same checkout's symbols: what the index
holds once the whole repository is ingested.

`compare` scores it against a scip-go delta produced by
`scripts/scip_harness/scip_to_delta.py` (keys of the form
`<dotted dir>.<clause>::Type::Method`): call edges by (path, line) — exact
target, same bare name on another type (a WRONG binding), missed, and edges
at lines the gold has none; `inherits` recall and precision; function
symbols by (path, line, name).

Run from the repository root with the worktree on `PYTHONPATH`; no live
ClickHouse, no tokens.
"""

from __future__ import annotations

import json
import os
import posixpath
import sys
import time
from collections import Counter, defaultdict
from typing import Any

SKIP_DIRS: frozenset[str] = frozenset({".git", "vendor", "testdata", "node_modules"})


def read_checkout(root: str) -> dict[str, bytes]:
    """Every `.go` file and `go.mod` under `root`, by repository-relative path."""
    files: dict[str, bytes] = {}
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in names:
            if n.endswith(".go") or n == "go.mod":
                full = os.path.join(dirpath, n)
                with open(full, "rb") as fh:
                    files[posixpath.relpath(full, root)] = fh.read()
    return files


def derive(files: dict[str, bytes]) -> dict[str, Any]:
    """Symbols, edges and misses of the fast tier over a whole checkout."""
    from server.code.app.index.ingest_parse import parse_file
    from server.code.domain.extract.edges_treesitter import callee_targets, extract_edges_and_misses
    from server.code.domain.extract.go_implementors import (
        go_clauses_of,
        struct_embeds,
        type_alias_target,
        type_element,
        value_type,
    )
    from server.code.domain.extract.lang_go import go_module_index, go_package_context
    from server.code.domain.extract.name_scope import gate_name_table

    t0 = time.time()
    parsed = {
        p: pf
        for p, src in files.items()
        if p.endswith(".go")
        for pf in [parse_file(p, src)]
        if pf is not None
    }
    index = go_module_index({p: files[p].decode() for p in files if p.endswith("go.mod")})
    table: dict[str, list[tuple[str, str, str]]] = {}
    returns_by_qn: dict[str, str] = {}
    for pf in parsed.values():
        for s in pf.symbols:
            table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
            if s.return_type:
                returns_by_qn[s.qualified_name] = s.return_type
    gated = gate_name_table(table, "go")
    every = [s for pf in parsed.values() for s in pf.symbols]
    sigs_by_id = {s.id: s.signature for s in every}
    embeds = {
        s.qualified_name: struct_embeds(s.signature)
        for s in every
        if s.kind == "struct" and struct_embeds(s.signature)
    }
    elems = {
        s.name: type_element(s.signature)
        for s in every
        if s.kind == "type" and type_element(s.signature)
    }
    aliases = {
        s.qualified_name: type_alias_target(s.signature)
        for s in every
        if s.kind == "type" and type_alias_target(s.signature)
    }
    values_by_scope: dict[str, dict[str, str]] = {}
    for s in every:
        if s.kind in ("const", "var") and value_type(s.signature):
            scope = s.qualified_name.partition("::")[0]
            values_by_scope.setdefault(scope, {})[s.name] = value_type(s.signature)
    edges: list[dict[str, Any]] = []
    misses: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    for pf in parsed.values():
        own = next((s.qualified_name.partition("::")[0] for s in pf.symbols), "")
        packages = go_package_context(pf, index)
        ctx = {
            "go_packages": packages,
            "go_clauses": go_clauses_of(packages, gated),
            "go_method_sigs": sigs_by_id,
            "go_struct_embeds": embeds,
            "go_type_elems": elems,
            "go_type_aliases": aliases,
            "go_value_types": values_by_scope.get(own, {}),
        }
        targets = callee_targets(pf, gated, ctx)
        returns = {c: returns_by_qn[q] for c, q in targets.items() if q in returns_by_qn}
        es, ms = extract_edges_and_misses(pf, gated, None, returns or None, ctx)
        edges.extend(
            {
                "kind": e.edge_kind,
                "path": e.path,
                "line": e.line,
                "from": e.from_qualified_name,
                "to": e.to_qualified_name,
                "conf": e.confidence,
            }
            for e in es
        )
        misses.extend(
            {
                "path": m.path,
                "line": m.line,
                "callee": m.callee_name,
                "disp": m.disposition,
            }
            for m in ms
        )
        symbols.extend(
            {
                "path": s.path,
                "qn": s.qualified_name,
                "kind": s.kind,
                "line": s.start_line,
                "name": s.name,
                "ret": s.return_type,
                "body": s.has_body,
            }
            for s in pf.symbols
        )
    return {
        "files": len(parsed),
        "symbols": symbols,
        "edges": edges,
        "misses": misses,
        "seconds": round(time.time() - t0, 1),
    }


def gold_key(qn: str) -> str | None:
    """A gold qualified name as a comparison key, None outside the repository:
    the converter spells names like the fast tier
    (`<dotted dir>.<clause>::Type::Method`); a scope with a `/` or without a
    `.` is a library's (`strconv`, `github.com/x/y`)."""
    scope, sep, _tail = qn.partition("::")
    if not sep or "/" in scope or "." not in scope:
        return None
    return fast_key(qn)


def fast_key(qn: str) -> str:
    """`<dotted dir>::Type::Method` — the package-clause segment dropped, so a
    `_test` package folds onto its directory the way the converter folds it."""
    scope, _sep, tail = qn.partition("::")
    d = scope.rsplit(".", 1)[0] if "." in scope else scope
    return f"{d}::{tail}"


def compare(fast: dict[str, Any], gold: dict[str, Any]) -> dict[str, Any]:
    """Score the fast tier's derivation against a scip-go delta."""
    gold_calls: dict[tuple[str, int], set[str]] = defaultdict(set)
    for f in gold["files"]:
        for e in f.get("edges") or []:
            if e.get("edge_kind") != "calls":
                continue
            k = gold_key(e["to_qualified_name"])
            if k is not None:
                gold_calls[(f["path"], int(e["line"]))].add(k)
    ours: dict[tuple[str, int], set[str]] = defaultdict(set)
    ours_conf: dict[tuple[str, int, str], float] = {}
    for e in fast["edges"]:
        if e["kind"] != "calls":
            continue
        k = fast_key(e["to"])
        ours[(e["path"], int(e["line"]))].add(k)
        ours_conf[(e["path"], int(e["line"]), k)] = e["conf"]
    hit = wrong = missed = 0
    by_conf_hit: Counter[float] = Counter()
    by_conf_wrong: Counter[float] = Counter()
    for site, targets in gold_calls.items():
        mine = ours.get(site, set())
        if not mine:
            missed += len(targets)
            continue
        mine_bare = {m.rsplit("::", 1)[-1]: m for m in mine}
        for t in targets:
            if t in mine:
                hit += 1
                by_conf_hit[ours_conf[(site[0], site[1], t)]] += 1
            elif t.rsplit("::", 1)[-1] in mine_bare:
                wrong += 1
                by_conf_wrong[ours_conf[(site[0], site[1], mine_bare[t.rsplit("::", 1)[-1]])]] += 1
            else:
                missed += 1
    extra = sum(len(mine) for site, mine in ours.items() if site not in gold_calls)
    n_gold = sum(len(v) for v in gold_calls.values())

    gold_inh: set[tuple[str, str]] = set()
    for f in gold["files"]:
        for e in f.get("edges") or []:
            if e.get("edge_kind") == "inherits":
                a, b = gold_key(e["from_qualified_name"]), gold_key(e["to_qualified_name"])
                if a and b:
                    gold_inh.add((a, b))
    our_inh = {
        (fast_key(e["from"]), fast_key(e["to"])) for e in fast["edges"] if e["kind"] == "inherits"
    }
    both = gold_inh & our_inh

    g_sym = {
        (f["path"], s["line_start"], s["name"])
        for f in gold["files"]
        for s in (f.get("symbols") or [])
        if s["kind"] == "function"
    }
    o_sym = {(s["path"], s["line"], s["name"]) for s in fast["symbols"] if s["kind"] == "function"}
    return {
        "calls": {
            "gold": n_gold,
            "exact": hit,
            "exact_rate": round(hit / max(n_gold, 1), 3),
            "wrong_type": wrong,
            "missed": missed,
            "extra_lines": extra,
            "exact_by_confidence": dict(by_conf_hit),
            "wrong_by_confidence": dict(by_conf_wrong),
        },
        "inherits": {
            "gold": len(gold_inh),
            "ours": len(our_inh),
            "both": len(both),
            "recall": round(len(both) / max(len(gold_inh), 1), 3),
            "precision": round(len(both) / max(len(our_inh), 1), 3),
            "ours_only": sorted(our_inh - gold_inh)[:8],
            "gold_only": sorted(gold_inh - our_inh)[:8],
        },
        "functions": {
            "gold": len(g_sym),
            "ours": len(o_sym),
            "both": len(g_sym & o_sym),
            "ours_only": len(o_sym - g_sym),
            "gold_only": len(g_sym - o_sym),
            "gold_only_sample": sorted(g_sym - o_sym)[:5],
        },
    }


def report(scores: dict[str, Any]) -> str:
    c, i, f = scores["calls"], scores["inherits"], scores["functions"]
    return "\n".join(
        [
            f"calls: gold(in-repo) {c['gold']} | exact {c['exact']} ({c['exact_rate']}) | "
            f"same name, other type {c['wrong_type']} | missed {c['missed']} | "
            f"ours at lines gold has none {c['extra_lines']}",
            f"  exact by confidence {c['exact_by_confidence']} | "
            f"wrong-type by confidence {c['wrong_by_confidence']}",
            f"inherits: gold {i['gold']} | ours {i['ours']} | both {i['both']} | "
            f"recall {i['recall']} | precision {i['precision']}",
            f"  ours-only sample {i['ours_only']}",
            f"  gold-only sample {i['gold_only']}",
            f"functions: gold {f['gold']} | ours {f['ours']} | both {f['both']} | "
            f"ours-only {f['ours_only']} | gold-only {f['gold_only']}",
            f"  gold-only sample {f['gold_only_sample']}",
        ]
    )


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[1] == "derive":
        out = derive(read_checkout(argv[2]))
        with open(argv[3], "w") as fh:
            json.dump(out, fh)
        kinds = Counter(s["kind"] for s in out["symbols"])
        edge_kinds = Counter(e["kind"] for e in out["edges"])
        print(
            f"files {out['files']} symbols {len(out['symbols'])} {dict(kinds)} "
            f"edges {dict(edge_kinds)} misses {len(out['misses'])} in {out['seconds']}s"
        )
        return 0
    if len(argv) == 4 and argv[1] == "compare":
        with open(argv[2]) as fa, open(argv[3]) as fb:
            print(report(compare(json.load(fa), json.load(fb))))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
