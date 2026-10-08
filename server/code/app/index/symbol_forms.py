"""Symbol-form tolerance on the read side.

A caller spells a symbol the way its compiler or its stack trace does; the table spells
it the way the parser did. Two forms would otherwise be a hard RESOURCE_NOT_FOUND:

  DB::(anonymous namespace)::AggregateFunctionLargestTriangleThreeBuckets::createResultType
      the parser tier never writes `(anonymous namespace)` — the table has
      DB::AggregateFunctionLargestTriangleThreeBuckets::createResultType
  DB::QueryPlanOptimizations::parseTableStatsHint
      the caller added a namespace the definition does not carry — the
      table has DB::parseTableStatsHint (defined in `namespace DB`)

Both are over-qualifications: every segment the table carries is in the
written name, in order. The rules here bind such a name when the exact and
`::`-suffix lookups (`CodeRepo.symbols_by_name`) return nothing:

  1. strip `(anonymous namespace)::` segments and look the rest up exactly
     (`resolved_by: anonymous_namespace`);
  2. among the definitions of the bare name, keep those whose scope segments
     are an order-preserving subsequence of the written scope; a written
     segment the match drops must not name a class the index knows (a
     member of a nested class the table lacks is not a member of its outer
     class); the longest match wins, and only when exactly one qualified
     name holds it (`resolved_by: tail`).

A bare name never enters rule 2 — the ambiguity guard of code.callers
(`candidates` above `code_callers_max_distinct_seeds`) and code.symbol's fuzzy
list keep their territory. No new confidence: the rows come back as the
table holds them; `resolved_by` on the payload discloses the form that bound.

  3. a name no symbol carries that a registration statement
     does — a SQL-level function name (`sumMap`, `indexHint`), or
     `sqlName::method` — binds to the implementing class's methods through
     the registration (`resolved_by: registration`; `sql_names.py`). It runs
     after rules 1 and 2, for bare names too: a bare SQL name is not an
     over-qualification but a different vocabulary, and the class it names
     is one class — the ambiguity guard is skipped for these rows.
"""

from __future__ import annotations

from typing import Any

ANON = "(anonymous namespace)"
# the compiler tier's spelling: `$anonymous_namespace_src/Common/Foo.cpp`
ANON_SCIP_PREFIX = "$anonymous_namespace"
RESOLVED_ANON = "anonymous_namespace"
RESOLVED_TAIL = "tail"
RESOLVED_REGISTRATION = "registration"
# Bare-name definitions read for the tail rule; a hub name past this many
# rows is not bound (the rule needs the whole candidate set to call a match unique).
TAIL_READ_LIMIT = 400


def is_anonymous(segment: str) -> bool:
    """Either spelling of an anonymous namespace: the parser's / clang's
    `(anonymous namespace)` and the compiler tier's `$anonymous_namespace_<path>`."""
    return segment == ANON or segment.startswith(ANON_SCIP_PREFIX)


def segments(name: str) -> list[str]:
    """`::`-segments of a qualified name, a trailing parameter list dropped,
    a template argument list kept with its segment (`Foo<T>::bar`)."""
    marked = name.replace(ANON, "\0")  # its own parentheses are not a parameter list
    head = marked.split("(", 1)[0].strip()
    parts = [p.strip() for p in head.split("::") if p.strip()]
    return [ANON if p == "\0" else p for p in parts]


def _named(name: str) -> list[str]:
    return [p for p in segments(name) if not is_anonymous(p)]


def strip_anonymous(name: str) -> str:
    """`DB::(anonymous namespace)::Foo::bar` -> `DB::Foo::bar`; "" when the
    name had no such segment (so callers can tell the form did not change).
    The result may be bare (`(anonymous namespace)::f` -> `f`): it is then
    looked up as a bare name is, the ambiguity guard included."""
    parts = segments(name)
    kept = _named(name)
    if len(kept) == len(parts):
        return ""
    return "::".join(kept)


def natural_name(name: str) -> str:
    """The spelling a caller writes: anonymous-namespace segments dropped
    (`DB::$anonymous_namespace_src/IO/Foo.cpp::f` -> `DB::f`), which binds
    back to the same definition; the name unchanged when it has none."""
    return strip_anonymous(name) or name


def is_qualified(name: str) -> bool:
    return len(_named(name)) >= 2


def _subsequence_match(scope: list[str], written: list[str]) -> list[str] | None:
    """The written segments a stored scope does NOT use when `scope` is an
    order-preserving subsequence of `written`; None when it is not."""
    i = 0
    dropped: list[str] = []
    for w in written:
        if i < len(scope) and scope[i] == w:
            i += 1
        else:
            dropped.append(w)
    return dropped if i == len(scope) else None


def tail_bind(
    written: str,
    rows: list[dict[str, Any]],
    *,
    class_names: set[str],
) -> list[dict[str, Any]]:
    """Rule 2 over `rows` — the definitions of the written name's bare
    segment. `class_names`: the written scope segments the index holds a
    class/struct for (a dropped one declines). Returns the rows of the one
    qualified name that binds, [] otherwise."""
    parts = _named(written)
    if len(parts) < 2:
        return []
    bare, wscope = parts[-1], parts[:-1]
    best: dict[str, list[dict[str, Any]]] = {}
    best_len = 0
    for r in rows:
        qn = str(r.get("qualified_name") or "")
        qparts = segments(qn)
        if not qparts or qparts[-1] != bare:
            continue
        scope = _named(qn)[:-1]
        if not scope:
            continue  # a bare definition never binds by tail
        if scope == wscope and not strip_anonymous(qn):
            continue  # the exact form, which the exact lookup already found
        # (an equal scope under an anonymous namespace is a match: `DB::f` names the
        # compiler tier's `DB::$anonymous_namespace_<path>::f`, which no exact lookup finds)
        dropped = _subsequence_match(scope, wscope)
        if dropped is None or any(d in class_names for d in dropped):
            continue
        if len(scope) > best_len:
            best_len, best = len(scope), {}
        if len(scope) == best_len:
            best.setdefault(qn, []).append(r)
    if len(best) != 1:
        return []
    return next(iter(best.values()))


def dropped_segments(written: str, rows: list[dict[str, Any]]) -> set[str]:
    """Every written scope segment some candidate's subsequence match would
    drop — the names whose class-ness the caller must check before `tail_bind`."""
    parts = _named(written)
    if len(parts) < 2:
        return set()
    bare, wscope = parts[-1], parts[:-1]
    out: set[str] = set()
    for r in rows:
        qn = str(r.get("qualified_name") or "")
        qparts = segments(qn)
        if not qparts or qparts[-1] != bare:
            continue
        scope = _named(qn)[:-1]
        dropped = _subsequence_match(scope, wscope) if scope else None
        if dropped:
            out.update(dropped)
    return out


async def resolve_tolerant(
    repo: Any,
    org: str,
    ns: str,
    name: str,
) -> tuple[list[dict[str, Any]], str]:
    """The two rules over the repository, after an exact miss: (rows,
    resolved_by). ([], "") when neither binds. Rule 1 runs for any name with
    an anonymous segment (the rest may be bare and is looked up as such);
    rule 2 only for a qualified name."""
    stripped = strip_anonymous(name)
    if stripped:
        rows = await repo.symbols_by_name(org, ns, stripped)
        if rows:
            return rows, RESOLVED_ANON
    if is_qualified(name):
        bound = await _tail(repo, org, ns, name)
        if bound:
            return bound, RESOLVED_TAIL
    return await _registration(repo, org, ns, name)


async def _tail(repo: Any, org: str, ns: str, name: str) -> list[dict[str, Any]]:
    bare = _named(name)[-1]
    rows = await repo.symbols_by_name(org, ns, bare, limit=TAIL_READ_LIMIT)
    if not rows or len(rows) >= TAIL_READ_LIMIT:
        return []
    classes: set[str] = set()
    for seg in sorted(dropped_segments(name, rows)):
        hits = await repo.symbols_by_name(org, ns, seg, limit=5)
        if any(str(h.get("kind") or "") in {"class", "struct"} for h in hits):
            classes.add(seg)
    return tail_bind(name, rows, class_names=classes)


async def _registration(
    repo: Any,
    org: str,
    ns: str,
    name: str,
) -> tuple[list[dict[str, Any]], str]:
    """Rule 3: a SQL-level name through its registration."""
    from server.code.app.index import sql_names

    if not sql_names.is_sql_name_candidate(name):
        return [], ""
    rows = await sql_names.Resolver(repo, org, ns).resolve(name)
    return rows, (RESOLVED_REGISTRATION if rows else "")


def mark(rows: list[dict[str, Any]], resolved_by: str) -> list[dict[str, Any]]:
    """The rows with `resolved_by` on each, so a handler that only sees the
    seed rows can put the disclosure on its payload."""
    if not resolved_by:
        return rows
    return [{**r, "resolved_by": resolved_by} for r in rows]


def resolved_by_of(rows: list[dict[str, Any]]) -> str:
    for r in rows:
        v = r.get("resolved_by")
        if v:
            return str(v)
    return ""
