"""Virtual dispatch and member folding for the graph walks: the base declarations
and overrides a method seed stands for, whether the namespace's compiler tier carries
`overrides` edges (process cache), and a class seed folded to its members.
"""

from __future__ import annotations

import re
from typing import Any

from server.code.app.candidates import overrides_of
from server.code.app.ttl_cache import TtlCache
from server.code.store.index import CodeRepo


async def _widen_dispatch(
    repo: CodeRepo,
    org: str,
    ns: str,
    *,
    seed_ids: list[str],
    dispatch_rows: list[dict[str, Any]],
    dispatch: list[dict[str, str]],
    note: str,
) -> tuple[dict[str, str], list[str], list[dict[str, str]], str]:
    """`include_ambiguous`: the compiler tier's overrides of the seeds (and of the base
    declarations dispatch already added) join the seeds as `overridden_by`; every
    dispatch seed maps to the `via_override` its edges will carry. Returns (via_for,
    seed_ids, dispatch, note)."""

    via_for = {str(r["symbol_id"]): str(r["qualified_name"]) for r in dispatch_rows}
    overrides, capped = await overrides_of(repo, org, ns, seed_ids)
    for r in overrides:
        via_for[str(r["symbol_id"])] = str(r["qualified_name"])
    if overrides:
        dispatch = [
            *dispatch,
            *({"symbol": r["qualified_name"], "via": r["via"]} for r in overrides),
        ]
        seed_ids = list(dict.fromkeys([*seed_ids, *(str(r["symbol_id"]) for r in overrides)]))
        widen = (
            f"callers of {len(overrides)} override(s) of the seed are included, tagged via_override"
            + (f" (first {len(overrides)} of more; the rest are not walked)" if capped else "")
        )
        note = f"{note}; {widen}" if note else widen
    return via_for, seed_ids, dispatch, note


def _class_scoped(seeds: list[dict[str, Any]]) -> bool:
    return any(
        "::" in str(r.get("qualified_name") or "") and r.get("kind") in (None, "method", "function")
        for r in seeds
    )


async def _dispatch_missing(
    repo: CodeRepo,
    org: str,
    ns: str,
    seeds: list[dict[str, Any]],
    dispatch_rows: list[dict[str, Any]],
    *,
    attempted: bool,
) -> bool:
    """A method seed whose base declaration could not be resolved, in a
    namespace whose compiler tier carries no `overrides` relationships:
    the seed MAY be an override whose call sites bind elsewhere. Where the
    compiler tier has relationships, "no overrides edge" is the answer."""
    if dispatch_rows or not _class_scoped(seeds):
        return False
    if not attempted:
        return True
    return not await _namespace_has_override_edges(repo, org, ns)


_re_interface = re.compile(r"^I[A-Z]\w*$")


def _arity(sig: str) -> int:
    """Parameter count from a signature; 0 for `()` or no parameter list."""
    if "(" not in sig:
        return 0
    inner = sig.split("(", 1)[1].rsplit(")", 1)[0].strip()
    return 0 if not inner else inner.count(",") + 1


_OVR_CACHE: TtlCache[tuple[str, str], bool] = TtlCache(3600.0)
# How long a namespace probe's fail-closed answer after a read error is kept.
PROBE_ERROR_TTL_S = 30.0


_PARTIAL_RELATIONSHIP_SUFFIXES = (".go", ".ts", ".tsx", ".js", ".jsx", ".py", ".java", ".kt", ".rs")


def _partial_relationships(path: str) -> bool:
    """A seed whose compiler tier records override / implementation relationships only
    partially (scip-go: standard-library interfaces; scip-typescript: transitive bases;
    the Python and Rust producers: none) keeps the fast-tier hierarchy walk."""
    return path.endswith(_PARTIAL_RELATIONSHIP_SUFFIXES)


async def _namespace_has_override_edges(repo: CodeRepo, org: str, ns: str) -> bool:
    """Cached (1h) per namespace: does the compiler tier carry live `overrides`
    edges at all? Fail-closed to False so a query error keeps the heuristics; that
    answer is kept only `PROBE_ERROR_TTL_S`, so one transient error does not hold the
    namespace on the slow heuristics for the hour."""
    hit = _OVR_CACHE.get((org, ns))
    if hit is not None:
        return hit
    ttl: float | None = None
    try:
        has = await repo.has_live_scip_override_edges(org, ns)
    except Exception:  # noqa: BLE001 — fail closed to False: the heuristics stay on
        has, ttl = False, PROBE_ERROR_TTL_S
    _OVR_CACHE.put((org, ns), has, ttl_s=ttl)
    return has


# Seed kinds whose direct members' callers count as the seed's callers (a class is used
# through its methods). Distinct from the large-container outline set
# (`outline._OUTLINE_CONTAINER_KINDS`).
_MEMBER_FOLD_KINDS = frozenset({"class", "interface", "struct", "namespace", "module", "enum"})


async def _fold_members(
    repo: CodeRepo,
    org: str,
    ns: str,
    seeds: list[dict[str, Any]],
    seed_ids: list[str],
    note: str,
) -> tuple[dict[str, str], list[str], str]:
    """Join the members of every container seed to the walk. Returns (owner qualified
    name per member id, widened seed ids, note)."""
    member_rows = await _member_seeds(repo, org, ns, seeds)
    if not member_rows:
        return {}, seed_ids, note
    owners = {str(r["qualified_name"]): str(r["qualified_name"]) for r in seeds}
    via_member = {
        str(r["symbol_id"]): str(r["qualified_name"]).rsplit("::", 1)[0] for r in member_rows
    }
    via_member = {k: owners.get(v, v) for k, v in via_member.items()}
    fold_note = (
        f"seed is a container; callers of {len(member_rows)} member(s) are included, "
        "tagged via: member"
    )
    return (
        via_member,
        list(dict.fromkeys([*seed_ids, *via_member])),
        f"{note}; {fold_note}" if note else fold_note,
    )


async def _member_seeds(
    repo: CodeRepo, org: str, ns: str, seeds: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The direct members of every container seed: a class is used through its methods,
    so their callers are its callers. Empty for a function seed, whose payload is
    unchanged."""

    containers = [
        str(r["qualified_name"]) for r in seeds if str(r.get("kind", "")) in _MEMBER_FOLD_KINDS
    ]
    if not containers:
        return []
    return await repo.members_of(org, ns, list(dict.fromkeys(containers)))


async def _dispatch_seeds(
    repo: CodeRepo, org: str, ns: str, seeds: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Base declarations an override's call sites actually bind to.

    The index stores no `virtual`/`override` keywords (tree-sitter keeps the
    parameter list, scip a hash), so this is structural: for a `Class::method`
    seed, (1) walk `inherits` edges from Class up to 3 hops and take every
    `Base::method`; (2) when the hierarchy edge is missing (tree-sitter skips
    some `class X final : public Y` forms) take same-name, same-arity methods
    declared on a class that something else inherits from. Each row carries
    `via` (inherits | virtual_by_name). Free functions and seeds without a
    class scope return []."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    exact_for: set[str] = set()  # qualified names already resolved via an overrides edge
    for s in seeds:
        q = str(s.get("qualified_name") or "")
        if "::" not in q or s.get("kind") not in (None, "method", "function"):
            continue
        if q in exact_for:
            continue  # the tree-sitter row of a symbol the compiler row already resolved
        cls, method = q.rsplit("::", 1)
        # 0. exact: `overrides` edges from the compiler index (scip-clang
        #    relationships). When present they are authoritative and the
        #    heuristics below are skipped for this seed.
        try:
            ov = await repo.edges_for_symbols(
                org,
                ns,
                [str(s["symbol_id"])],
                direction="callees",
                edge_kinds=["overrides"],
                limit=10,
            )
        except Exception:  # noqa: BLE001
            ov = []
        exact = False
        for e in ov:
            sid = str(e["to_symbol_id"])
            if sid not in seen and str(e["to_qualified_name"]) != q:
                seen.add(sid)
                out.append(
                    {
                        "symbol_id": sid,
                        "qualified_name": str(e["to_qualified_name"]),
                        "path": str(e.get("path") or ""),
                        "via": "overrides",
                    }
                )
                exact = True
        if exact:
            exact_for.add(q)
            continue
        # Once the compiler tier carries `overrides` edges for this namespace, "no
        # overrides edge" is the answer, not a gap: skip the hierarchy walk and the by-name
        # scan (measured at 82% of callers latency, empty for 29 of 40 replayed symbols).
        # Heuristics remain for namespaces whose producer emits no relationships.
        # scip-go's relationships name the standard library's interfaces (fmt.Stringer,
        # sql.Scanner) and few of the repository's own, so for the languages whose compiler
        # tier is known to be partial the bounded hierarchy walk (rung 1: the seed's type →
        # `inherits` → `Base::method`, three keyed reads) still runs; only the by-name scan
        # (rung 2) stays behind the gate.
        has_overrides = await _namespace_has_override_edges(repo, org, ns)
        if has_overrides and not _partial_relationships(str(s.get("path") or "")):
            continue
        # scip stores a hash as the signature; take the arity from any seed row
        # with a real parameter list, else leave it unknown (-1 = don't filter).
        real_sigs = [
            str(x.get("signature") or "")
            for x in seeds
            if str(x.get("qualified_name")) == q
            and not str(x.get("signature") or "").startswith("(scip:")
        ]
        arity = _arity(real_sigs[0]) if real_sigs else -1
        # 1. hierarchy walk
        bases: list[str] = []
        try:
            cls_rows = await repo.symbols_by_name(org, ns, cls, limit=5)
            frontier = [
                str(r["symbol_id"]) for r in cls_rows if r.get("kind") in ("class", "struct")
            ]
            for _ in range(3):
                if not frontier:
                    break
                edges = await repo.edges_for_symbols(
                    org,
                    ns,
                    frontier,
                    direction="callees",
                    edge_kinds=["inherits"],
                    limit=50,
                )
                frontier = []
                for e in edges:
                    b = str(e["to_qualified_name"])
                    if b and b not in bases:
                        bases.append(b)
                        frontier.append(str(e["to_symbol_id"]))
        except Exception:  # noqa: BLE001 — hierarchy is best-effort
            bases = []
        found_here = False
        for b in bases:
            for r in await repo.symbols_by_name(org, ns, f"{b}::{method}", limit=3):
                sid = str(r["symbol_id"])
                if sid not in seen and str(r["qualified_name"]) != q:
                    seen.add(sid)
                    out.append({**r, "via": "inherits"})
                    found_here = True
        if found_here or has_overrides:
            continue
        # 2. same name + arity, declared on a class something inherits from
        checked_cls: dict[str, bool] = {}
        for r in await repo.methods_named(org, ns, method, limit=300):
            rq = str(r["qualified_name"])
            if rq == q or "::" not in rq:
                continue
            if arity >= 0 and _arity(str(r.get("signature") or "")) != arity:
                continue
            rcls = rq.rsplit("::", 1)[0]
            if rcls == cls:
                continue
            if rcls not in checked_cls:
                if len(checked_cls) >= 15:
                    break
                # Interface naming (IStorage, IColumn, IProcessor …) is the
                # codebase's own contract for "this is the base you dispatch
                # through"; it covers the families whose `class X final :
                # public IBase` clause the hierarchy extractor misses.
                is_base = bool(_re_interface.match(rcls.rsplit("::", 1)[-1]))
                try:
                    crow = await repo.symbols_by_name(org, ns, rcls, limit=3)
                    cids = [
                        str(x["symbol_id"]) for x in crow if x.get("kind") in ("class", "struct")
                    ]
                    if cids and not is_base:
                        inbound = await repo.edges_for_symbols(
                            org,
                            ns,
                            cids,
                            direction="callers",
                            edge_kinds=["inherits"],
                            limit=1,
                        )
                        is_base = bool(inbound)
                except Exception:  # noqa: BLE001
                    is_base = False
                checked_cls[rcls] = is_base
            if not checked_cls[rcls]:
                continue
            sid = str(r["symbol_id"])
            if sid not in seen:
                seen.add(sid)
                out.append({**r, "via": "virtual_by_name"})
    return out[:6]
