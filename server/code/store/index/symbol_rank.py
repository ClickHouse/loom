"""Pure ranking and collapsing of symbol and edge rows: two-tier (compiler
over parser) collapse, noise / generated / member demotion, outline
collapse, and the TypeScript return-type reader. No ClickHouse here, so
handlers and tests can feed these fixtures directly.
"""

from __future__ import annotations

import re
from typing import Any

_SIG_RETURN_RE = re.compile(r"\)\s*:\s*(?:Promise<)?\s*([A-Za-z_$][\w$]*)")


def return_type_from_signature(signature: str) -> str:
    """The named return type a TypeScript signature ends with — `(o: Opts):
    ClickHouseClient` → ClickHouseClient, `Promise<Reply>` → Reply; '' for
    primitives, unions and shapes the receiver typer cannot use."""
    m = _SIG_RETURN_RE.search(signature or "")
    if not m:
        return ""
    name = m.group(1)
    return (
        ""
        if name
        in {
            "void",
            "Promise",
            "string",
            "number",
            "boolean",
            "any",
            "unknown",
            "never",
            "null",
            "undefined",
            "object",
            "this",
        }
        else name
    )


def _bare_name(qualified: str) -> str:
    """The last segment of either tier's spelling: `DB::Foo::bar` → `bar`,
    `a.b.util::Recv.Name` → `Name`, `example.com/a::Recv::Name` → `Name`."""
    tail = qualified.rsplit("::", 1)[-1]
    return tail.rsplit(".", 1)[-1]


def prefer_scip_edges(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """When both tiers observed the same call, keep only the scip row.
    Keyed on the call site plus the BARE names of both ends, NOT symbol_ids
    or qualified names — the tiers hash signatures differently (parameter
    text vs scip overload hash) and, for Go and file-local
    C++, scope one definition differently (`a.b.util::Recv.Name` from the
    parser, `example.com/a/b::Recv::Name` from the compiler), so ids and
    qualified names for the same logical symbol diverge across tiers while
    the site (path, line) and the bare names do not."""

    def key(r: dict[str, Any]) -> tuple[str, str, str, int]:
        return (
            _bare_name(str(r["from_qualified_name"])),
            _bare_name(str(r["to_qualified_name"])),
            str(r["path"]),
            int(r["line"]),
        )

    scip_keys = {key(r) for r in rows if str(r["tier"]) == "scip"}
    return [r for r in rows if str(r["tier"]) == "scip" or key(r) not in scip_keys]


# Paths whose definitions rank BELOW source definitions of the same name:
# test / example / fuzzer / benchmark directories, CI tooling, vendored
# third-party code (`contrib/`), and test-named files. The same shape
# code/app/search/priors.py uses to demote fused chunks; kept as one RE2
# string here because it is also bound into the ClickHouse ORDER BY.
# A vitest / jest file (`x.test.ts`, `x.servertest.ts`,
# `x.clienttest.tsx`, `__tests__/`, `.stories.tsx`) and a TypeScript
# declaration file (`next-auth.d.ts` augments a library's `Session`; the
# repository's `Session` is the Prisma model) are noise paths too.
SYMBOL_NOISE_PATH_RE = (
    r"(^|/)(tests?|examples?|fuzzers|benchmarks|ci|contrib|third[_-]party|vendor|__tests__|__mocks__)(/|$)"
    r"|(^|/)(test_[^/]+|[^/]+_(test|fuzz))\.(h|hpp|cpp|cc|cxx|py|go|rs|ts|js)$"
    r"|\.(test|spec|servertest|clienttest|stories)\.(ts|tsx|js|jsx|mjs)$"
    r"|\.d\.ts$"
    r"|\.pyi$"  # A stub declares what its implementation defines
)


_SYMBOL_NOISE_PATH = re.compile(SYMBOL_NOISE_PATH_RE)


def is_noise_path(path: str) -> bool:
    return bool(_SYMBOL_NOISE_PATH.search(path or ""))


def demote_noise_paths(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable partition: source-path rows first, test/example/fuzzer rows
    after, each group in its incoming order. The SQL already orders this
    way; this makes the rule hold for callers that assemble rows elsewhere
    and keeps it unit-testable without ClickHouse."""
    src = [r for r in rows if not is_noise_path(str(r.get("path") or ""))]
    noise = [r for r in rows if is_noise_path(str(r.get("path") or ""))]
    return src + noise


# A generated file is declared, not searched, and its
# declarations are the last answer to a bare name — the hand-written
# definition of the same name (the `.proto` message, the source the
# generator read) ranks first. The generated flag is not stored per row, so
# the rule reads the path shapes the generators write: protoc (`.pb.go`,
# `_grpc.pb.go`, `.pb.gw.go`, `.pb.validate.go`, `_pb2.py`, `_pb2_grpc.py`,
# `.pb.ts`/`.pb.js`/`.pb.cc`/`.pb.h`, google-protobuf's `_pb.js`), connect-go (`.connect.go`),
# controller-gen (`zz_generated.*.go`), ent/sqlc/gqlgen and the
# `_gen.go` / `.gen.go` / `_generated.` / `.generated.` conventions, mockgen
# and mockery (`mock_*.go`, `*_mock.go`, `mocks/`), Dart's `.g.dart`, and
# the `generated/` directory.
SYMBOL_GENERATED_PATH_RE = (
    r"(\.pb|_grpc\.pb|\.pb\.gw|\.pb\.validate|\.connect)\.go$"
    r"|_pb2(_grpc)?\.pyi?$|\.pb\.(ts|js|d\.ts|cc|h)$|_pb(_service)?\.(js|ts|d\.ts)$"
    r"|(^|/)zz_generated[^/]*\.go$|(_gen|\.gen|_generated|\.generated)\.[a-z]+$"
    r"|\.generated\.|_generated/|(^|/)generated(/|$)"
    r"|(^|/)mocks?(/|$)|(^|/)mock_[^/]+\.go$|_mock\.go$|\.g\.dart$"
)


_SYMBOL_GENERATED_PATH = re.compile(SYMBOL_GENERATED_PATH_RE)


def is_generated_path(path: str) -> bool:
    return bool(_SYMBOL_GENERATED_PATH.search(path or ""))


def demote_generated_paths(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable partition: hand-written rows first, generated
    paths after, noise paths (already last) untouched in their order."""
    head = [r for r in rows if not is_noise_path(str(r.get("path") or ""))]
    noise = [r for r in rows if is_noise_path(str(r.get("path") or ""))]
    hand = [r for r in head if not is_generated_path(str(r.get("path") or ""))]
    gen = [r for r in head if is_generated_path(str(r.get("path") or ""))]
    return hand + gen + noise


def demote_member_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For a BARE query the rows are partitioned by
    how deep the declaration sits — the shallowest `::` depth first, deeper
    after, each group in its incoming order. A top-level declaration of the
    name (`pkg.scope::Name`, `DB::Field`) outranks a member of the same bare
    name (`pkg.scope::Type::Name`: a struct field, a method, an enum member),
    whatever `kind` the tier wrote — scip labels a Go method `class`, which a
    rule on `kind IN ('field', 'method')` never sees. Runs
    before the hand-written / generated / noise partitions, which are stable,
    so a hand-written member still outranks a generated top-level row."""
    if not rows:
        return rows
    depth = [str(r.get("qualified_name") or "").count("::") for r in rows]
    shallow = min(depth)
    top = [r for r, d in zip(rows, depth, strict=True) if d == shallow]
    deep = [r for r, d in zip(rows, depth, strict=True) if d != shallow]
    return top + deep


def collapse_outline_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Source-ordered outline rows, one per (qualified_name, start_line):
    the row with the widest extent wins the span, and `tier` is 'scip' when
    ANY tier-scip row named the declaration at that line. Pure so the
    handler tests can feed it two-tier fixtures."""
    best: dict[tuple[str, int], dict[str, Any]] = {}
    order: list[tuple[str, int]] = []
    for r in rows:
        key = (str(r.get("qualified_name") or ""), int(r.get("start_line") or 0))
        cur = best.get(key)
        if cur is None:
            best[key] = dict(r)
            order.append(key)
            continue
        if int(r.get("end_line") or 0) > int(cur.get("end_line") or 0):
            tiers = (str(cur.get("tier")), str(r.get("tier")))
            tier = "scip" if "scip" in tiers else str(r.get("tier") or "")
            cur.update({k: v for k, v in r.items() if k != "tier"})
            cur["tier"] = tier
        elif str(r.get("tier")) == "scip":
            cur["tier"] = "scip"
            if not cur.get("signature") and r.get("signature"):
                cur["signature"] = r["signature"]
    # A class/struct row with no extent is a forward declaration, a `using`
    # alias or a compiler-only sighting (types_for_paths excludes the same
    # rows): it declares nothing a reader can open. Functions keep their
    # one-line rows — a method declared in a header IS the declaration.
    out = [
        best[k]
        for k in order
        if str(best[k].get("kind") or "") not in ("class", "struct")
        or int(best[k].get("end_line") or 0) > int(best[k].get("start_line") or 0)
    ]
    out.sort(key=lambda r: (int(r.get("start_line") or 0), -int(r.get("end_line") or 0)))
    return out


def _prefer_scip(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse two-tier symbol rows: for each (qualified_name, symbol_id)
    keep the scip row when both tiers have one (compiler beats parser).

    Second pass: the two tiers can name ONE definition differently — scip
    scopes file-local symbols as `DB::$anonymous_namespace_<path>::f` where
    the parser says `DB::f` — so also collapse a parser row onto a compiler
    row when both sit in the same file, carry the same bare name and the
    compiler row's definition line falls inside the parser row's span. The
    parser span (end_line) is carried onto the surviving compiler row, whose
    own span is a single line. Without this, one real definition read as
    `ambiguous: true` to a bot that cannot supply further qualification."""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for r in rows:
        key = (str(r["qualified_name"]), str(r["symbol_id"]))
        if key not in best:
            best[key] = r
            order.append(key)
        elif str(r["tier"]) == "scip" and str(best[key]["tier"]) != "scip":
            # The compiler row of a class or of a function it saw only as a
            # declaration spans one line; the parser row carries the body
            # extent. Keep the compiler identity, keep the parser extent —
            # otherwise a grep hit inside a long class reports the class as
            # spanning only its own declaration line.
            _carry_extent(r, best[key])
            best[key] = r
        elif str(r["tier"]) != "scip" and str(best[key]["tier"]) == "scip":
            _carry_extent(best[key], r)
    out = [best[k] for k in order]
    scip_rows = [r for r in out if str(r.get("tier")) == "scip"]
    if not scip_rows or len(scip_rows) == len(out):
        return out
    drop: set[int] = set()
    for t in out:
        if str(t.get("tier")) == "scip":
            continue
        t_start, t_end = int(t.get("start_line") or 0), int(t.get("end_line") or 0)
        for s in scip_rows:
            if str(s.get("path")) != str(t.get("path")) or _bare(s) != _bare(t):
                continue
            s_start = int(s.get("start_line") or 0)
            if s_start == t_start or (t_start <= s_start <= t_end):
                if int(s.get("end_line") or 0) <= s_start < t_end:
                    s["end_line"] = t_end
                # The parser id survives on the compiler row: parser-tier
                # edges point at it, and a graph walk seeded by the compiler
                # id alone would lose the callers only the parser saw.
                # Readers that want one identity keep `symbol_id`.
                ids = s.setdefault("tier_ids", [str(s.get("symbol_id") or "")])
                if str(t.get("symbol_id") or "") not in ids:
                    ids.append(str(t.get("symbol_id") or ""))
                drop.add(id(t))
                break
    return [r for r in out if id(r) not in drop]


def _carry_extent(keep: dict[str, Any], other: dict[str, Any]) -> None:
    """Copy `other`'s span onto `keep` when keep's span is a single line and
    other's is a real extent starting at the same definition line."""
    k_start, k_end = int(keep.get("start_line") or 0), int(keep.get("end_line") or 0)
    o_start, o_end = int(other.get("start_line") or 0), int(other.get("end_line") or 0)
    if k_end <= k_start and o_end > o_start and (k_start == o_start or o_start <= k_start <= o_end):
        keep["start_line"], keep["end_line"] = o_start, o_end


def _bare(row: dict[str, Any]) -> str:
    return str(row.get("name") or str(row.get("qualified_name") or "").rsplit("::", 1)[-1])
