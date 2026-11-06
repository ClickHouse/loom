"""SQL-level names on the read side: a registered function name resolves to the class
that implements it, and a class knows the SQL names it registers.

Bug reports name what a query names — `sumMap`, `indexHint`, `arrayAUCPR`, `quantileGK`
— not the C++ symbol behind it, so `code.callers` and `code.impact` would answer `symbol
not found` for a SQL name. The registration is the bridge: every SQL-visible name is
introduced by a C++ statement the index holds as a chunk, in one of three shapes
(ClickHouse's source):

  static constexpr auto name = "indexHint";     in the class body (scope
                                                DB::FunctionIndexHint) or in
                                                a name struct handed to a
                                                template (DB::NameQuantileGK)
  factory.registerFunction("arrayAUCPR", …);    in the registrar function —
                                                whose `calls` edges already
                                                reach the class's methods

  return "sumMap";                              in a `getNameImpl` method of
                                                the class

The rule greps `src/` for the quoted literal (an index-served literal read),
keeps the chunks where the literal sits in one of those shapes, and binds:

  * a scope that is a class, or the class of a method: the class's methods
    defined in that file (`resolved_by: registration`);
  * a scope that is a registrar (`REGISTER_FUNCTION`, `registerAggregate…`):
    the targets of its registration edges, narrowed to the class whose name
    contains the SQL name when one does (AggregateFunctionSumMap ⊇ summap);
  * a name struct (no methods of its own): the registrar that names it,
    then the rule above.

The inverse — the SQL names a class registers — reads the class file's
chunks (both the `.h` and the `.cpp` of the same stem) for the same three
shapes and keeps the names that belong to the class: in its scope, or a
registered literal whose lower-case form is inside the class name (or the
reverse). Those names are what a test would write, so the coverage claim
greps them as `exact_scoped` evidence instead of the method's bare name.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from server.code.domain.extract.registration import is_registration_caller

RESOLVED_REGISTRATION = "registration"

# Registration helpers, kept here (not in `server/code/app/citations.py`) so the
# coverage claim, the symbol ops and code.verify_citations share one vocabulary; the
# handler imports from here, never the reverse. The class a registration of `sumMap`
# would carry by convention, and the aggregate combinators (`countIf`, `uniqExactIf`,
# `anyLast_respect_nulls`) that register the stem and the combinator apart, so the
# written name is in no table and no file. Longest first; stripped one at a time until a
# stem binds.
REGISTRATION_CLASS_PREFIXES = ("Function", "AggregateFunction", "TableFunction", "Name")
COMBINATOR_SUFFIXES = (
    "_respect_nulls",
    "_RespectNulls",
    "SimpleState",
    "MergeState",
    "OrDefault",
    "Resample",
    "Distinct",
    "ForEach",
    "OrNull",
    "ArgMin",
    "ArgMax",
    "State",
    "Merge",
    "Array",
    "Map",
    "If",
)


def registration_classes(name: str) -> list[str]:
    """`sumMap` -> the class names a registration of it would carry."""
    cap = name[:1].upper() + name[1:]
    return [prefix + cap for prefix in REGISTRATION_CLASS_PREFIXES]


def combinator_stems(name: str) -> list[str]:
    """`countIfOrNull` -> ["countIf", "count"]: the stems left by stripping
    one combinator at a time (at most two), each at least three characters."""
    out: list[str] = []
    cur = name
    for _ in range(2):
        nxt = next(
            (
                cur[: -len(sfx)]
                for sfx in COMBINATOR_SUFFIXES
                if cur.endswith(sfx) and len(cur) - len(sfx) >= 3
            ),
            "",
        )
        if not nxt:
            break
        out.append(nxt)
        cur = nxt
    return out


# A SQL-visible identifier as a finding writes it. `std`, `llvm`, `DB` and
# the like are namespaces a compiler spells, never a registered name.
SQL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{1,60}$")
_NOT_SQL_NAMESPACES = frozenset(
    {
        "std",
        "llvm",
        "boost",
        "absl",
        "fmt",
        "Poco",
        "DB",
        "detail",
        "Setting",
        "ErrorCodes",
        "pqxx",
        "arrow",
        "parquet",
        "orc",
        "avro",
        "google",
        "grpc",
        "Aws",
        "Azure",
        "fs",
    }
)
# Where a bare `name` constant is the SQL identifier (the crossref pass trusts
# the same three registries).
NAME_CONSTANT_PATHS: tuple[str, ...] = (
    "src/Functions/",
    "src/AggregateFunctions/",
    "src/TableFunctions/",
)
# Where a registered string literal is SQL-visible vocabulary a test would
# write: functions, aggregates, table functions, engines, formats, types. A
# `registerStep("Limit")` in the query plan is not.
REGISTRY_PATHS: tuple[str, ...] = (
    *NAME_CONSTANT_PATHS,
    "src/Storages/",
    "src/Formats/",
    "src/Processors/Formats/",
    "src/DataTypes/",
)
SRC_PREFIX = "src/"
GREP_LIMIT = 40
SEED_CAP = 40
# Methods the registry drives: when a class defines more than SEED_CAP
# functions, these come first.
_INTERFACE_FIRST = (
    "executeImpl",
    "create",
    "add",
    "merge",
    "getReturnTypeImpl",
    "getReturnType",
    "insertResultInto",
    "serialize",
    "deserialize",
    "getName",
    "getNameImpl",
)


def _shape_re(name: str) -> re.Pattern[str]:
    q = re.escape(name)
    return re.compile(
        r"(?:(?P<const>\bname\s*=\s*)|(?P<ret>\breturn\s+)|"
        r'(?P<reg>register\w*\s*(?:<[^>()]{0,200}>)?\s*\(\s*))"' + q + r'"'
    )


# The methods whose `return "…";` is the SQL name.
_NAME_METHODS = frozenset({"getName", "getNameImpl"})
# Class-name prefixes the registries add: `AggregateFunctionSumMap` is about `SumMap`.
_CLASS_PREFIXES = ("AggregateFunction", "TableFunction", "Function", "Storage", "DataType")


def class_core(cls: str) -> str:
    """`DB::AggregateFunctionSumMap` -> `summap`: the class's last segment,
    the registry prefix dropped, lower-cased — what a registered name shares."""
    tail = cls.rsplit("::", 1)[-1].split("<", 1)[0]
    for pre in _CLASS_PREFIXES:
        if tail.startswith(pre) and len(tail) > len(pre):
            tail = tail[len(pre) :]
            break
    return tail.lower()


def names_match(sql_name: str, cls: str) -> bool:
    """Does a registered literal belong to a class: the class core is inside
    the name (`sumMapFiltered` ⊇ `summap`) or the name inside the core."""
    low, core = sql_name.lower(), class_core(cls)
    return bool(core) and (core in low or low in core)


# The inverse direction: every quoted literal in one of the three shapes.
_ANY_CONST_RE = re.compile(r'\bname\s*=\s*"([A-Za-z_][A-Za-z0-9_]{2,60})"')
_ANY_RETURN_RE = re.compile(r'\breturn\s+"([A-Za-z_][A-Za-z0-9_]{2,60})"\s*;')
_ANY_REG_RE = re.compile(
    r'register\w*\s*(?:<[^>()]{0,200}>)?\s*\(\s*"([A-Za-z_][A-Za-z0-9_]{2,60})"'
)


def is_sql_name_candidate(name: str) -> bool:
    """Could `name` be a SQL-level identifier: a bare identifier, or `sqlName::method` —
    the shape `arrayFold::executeImpl` / `toFixedString::isInjective` take in bug
    reports. A leading compiler namespace declines."""
    parts = [p for p in name.split("(", 1)[0].split("::") if p]
    if not parts or len(parts) > 2:
        return False
    head = parts[0]
    if head in _NOT_SQL_NAMESPACES or not SQL_NAME_RE.match(head):
        return False
    return len(parts) == 1 or bool(SQL_NAME_RE.match(parts[1]))


def split_sql_form(name: str) -> tuple[str, str]:
    """(sql name, method) — method '' for a bare name."""
    parts = [p for p in name.split("(", 1)[0].split("::") if p]
    return parts[0], (parts[1] if len(parts) > 1 else "")


@dataclass(frozen=True)
class RegistrationHit:
    path: str
    scope: str
    line: int
    shape: str  # name_constant | return_literal | registration_call
    # the class a file-level definition names: `const char *
    # FunctionPolygonPerimeter<SphericalPoint>::name = "…";` (scope is '')
    stated_class: str = ""


# Read from each word's first letter only, so a long word costs one pass.
_STATED_CLASS_RE = re.compile(r"(?<!\w)[^\WA-Za-z_]*+([A-Za-z_]\w*+)\s*(?:<[^;]*>)?\s*::\s*$")


def _stated_class(content: str, m: re.Match[str]) -> str:
    """The bare class name written before `::name =` on the matched
    statement (`const char * FunctionPolygonPerimeter<SphericalPoint>::name =
    "…";` at file level), '' when the constant sits inside a class body."""
    if not m.group("const"):
        return ""
    line_start = content.rfind("\n", 0, m.start()) + 1
    head = content[line_start : m.start()]
    sm = _STATED_CLASS_RE.search(head)
    return sm.group(1) if sm else ""


def registration_hits(name: str, chunks: list[dict[str, Any]]) -> list[RegistrationHit]:
    """The chunks where `"name"` sits in one of the three shapes. A name
    constant or a return literal is trusted only under the registries where
    `name` is the SQL identifier; a registration call anywhere under src/."""
    pat = _shape_re(name)
    out: list[RegistrationHit] = []
    for c in chunks:
        path = str(c.get("path") or "")
        content = str(c.get("content") or "")
        for m in pat.finditer(content):
            scope = str(c.get("scope") or "")
            if m.group("reg"):
                shape = "registration_call"
            elif not path.startswith(NAME_CONSTANT_PATHS):
                continue
            elif m.group("const"):
                shape = "name_constant"
            elif scope.rsplit("::", 1)[-1] in _NAME_METHODS:
                shape = "return_literal"
            else:
                continue
            line = int(c.get("start_line") or 0) + content.count("\n", 0, m.start())
            stated = _stated_class(content, m) if shape == "name_constant" else ""
            out.append(
                RegistrationHit(path=path, scope=scope, line=line, shape=shape, stated_class=stated)
            )
            break
    return out


def class_of_scope(scope: str, rows: list[dict[str, Any]]) -> str:
    """The class a chunk scope belongs to: the scope itself when the file
    declares it as a class/struct, its parent when the scope is a function
    of that file, '' when neither (a free function, file level)."""
    if not scope:
        return ""
    if is_registration_caller(scope):
        return ""
    kinds = {str(r.get("qualified_name") or ""): str(r.get("kind") or "") for r in rows}
    k = kinds.get(scope, "")
    if k in {"class", "struct"}:
        return scope
    if k in {"function", "method"} and "::" in scope:
        parent = scope.rsplit("::", 1)[0]
        # a one-segment parent with no class row is a namespace (`DB`)
        if kinds.get(parent) in {"class", "struct"} or ("::" in parent and parent not in kinds):
            return parent
        return ""
    if k:
        return ""
    # the parser tier may hold the class without the scope row (a template):
    # a scope with a method-looking tail whose parent has functions in the file
    parent = scope.rsplit("::", 1)[0] if "::" in scope else ""
    if parent and any(qn.startswith(parent + "::") for qn in kinds):
        return parent
    return scope if any(qn.startswith(scope + "::") for qn in kinds) else ""


def _class_by_bare_name(bare: str, rows: list[dict[str, Any]]) -> str:
    """The qualified class among `rows` whose last segment is `bare`, else the
    scope its methods share (`DB::FunctionPolygonPerimeter::executeImpl`)."""
    for r in rows:
        qn = str(r.get("qualified_name") or "")
        if qn.rsplit("::", 1)[-1] == bare and str(r.get("kind") or "") in {"class", "struct"}:
            return qn
    for r in rows:
        qn = str(r.get("qualified_name") or "")
        marker = "::" + bare + "::"
        if marker in qn:
            return qn[: qn.index(marker) + len(marker) - 2]
    return ""


def methods_of(cls: str, rows: list[dict[str, Any]], *, method: str = "") -> list[dict[str, Any]]:
    """The function rows of `cls` (direct members), interface methods first,
    capped; `method` keeps one bare name only."""
    out = []
    for r in rows:
        qn = str(r.get("qualified_name") or "")
        if str(r.get("kind") or "function") not in {"function", "method"}:
            continue
        if not qn.startswith(cls + "::"):
            continue
        bare = qn[len(cls) + 2 :]
        if "::" in bare:
            continue
        if method and bare != method:
            continue
        out.append(r)

    def _order(r: dict[str, Any]) -> tuple[int, str]:
        qn = str(r["qualified_name"])
        bare = qn.rsplit("::", 1)[-1]
        rank = _INTERFACE_FIRST.index(bare) if bare in _INTERFACE_FIRST else len(_INTERFACE_FIRST)
        return rank, qn

    out.sort(key=_order)
    return out[:SEED_CAP]


def narrow_targets(name: str, edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Registration-edge targets of one registrar, narrowed to the class
    whose name contains the SQL name when any does (a registrar registers
    several classes: minMap, maxMap, sumMap share one). Every target when
    none does — the file is right, the class is not singled out."""

    def cls_of(e: dict[str, Any]) -> str:
        qn = str(e.get("to_qualified_name") or "")
        return qn.rsplit("::", 1)[0] if "::" in qn else ""

    matched = [e for e in edges if cls_of(e) and names_match(name, cls_of(e))]
    return matched or edges


def _uniq_ids(edges: list[dict[str, Any]]) -> list[str]:
    ids = (str(e.get("to_symbol_id") or "") for e in edges if e.get("to_symbol_id"))
    return list(dict.fromkeys(ids))


class Resolver:
    """The reads, behind one object so a handler test can stub the repo."""

    def __init__(self, repo: Any, org: str, ns: str) -> None:
        self.repo, self.org, self.ns = repo, org, ns

    async def _chunks_with_literal(
        self,
        literal: str,
        *,
        prefix: str,
        limit: int = GREP_LIMIT,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = await self.repo.grep_chunks(
            self.org,
            self.ns,
            literal,
            regex=False,
            case_sensitive=True,
            path_prefix=prefix,
            limit=limit,
        )
        return rows

    async def _functions_in(self, path: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = await self.repo.functions_for_paths(
            self.org,
            self.ns,
            [path],
            limit=400,
            kinds=("function", "method", "class", "struct"),
        )
        return rows

    async def _registrar_row(
        self,
        path: str,
        scope: str,
        line: int,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        for r in rows:
            if str(r.get("qualified_name") or "") != scope:
                continue
            s, e = int(r.get("start_line") or 0), int(r.get("end_line") or 0)
            if s <= line <= max(e, s):
                return r
        cands = [r for r in rows if str(r.get("qualified_name") or "") == scope]
        return cands[0] if cands else None

    async def _through_registrar(
        self,
        name: str,
        registrar: dict[str, Any],
        *,
        method: str,
    ) -> list[dict[str, Any]]:
        edges: list[dict[str, Any]] = await self.repo.edges_for_symbols(
            self.org,
            self.ns,
            [str(registrar["symbol_id"])],
            direction="callees",
            limit=400,
        )
        edges = narrow_targets(name, edges)
        ids = _uniq_ids(edges)[:200]
        if not ids:
            return []
        rows: list[dict[str, Any]] = await self.repo.symbols_by_ids(self.org, self.ns, ids)
        if method:
            rows = [
                r for r in rows if str(r.get("qualified_name") or "").rsplit("::", 1)[-1] == method
            ]
        return rows[:SEED_CAP]

    async def resolve(self, written: str) -> list[dict[str, Any]]:
        """The implementing class's methods for a SQL-level name (or
        `sqlName::method`); [] when no registration shape names it."""
        if not is_sql_name_candidate(written):
            return []
        name, method = split_sql_form(written)
        # the convention first: `FunctionSumMap` / `AggregateFunctionSumMap` as keyed
        # reads, the name's combinator stems after it; the class binds when it defines
        # its methods
        for candidate in [name, *combinator_stems(name)]:
            got = await self._by_convention(candidate, method=method)
            if got:
                return got
        chunks = await self._chunks_with_literal(f'"{name}"', prefix=SRC_PREFIX)
        hits = registration_hits(name, chunks)
        if not hits:
            return []
        # one file read per distinct path, the class routes before the registrars
        paths = list(dict.fromkeys(h.path for h in hits))[:6]
        fn_lists = await asyncio.gather(*(self._functions_in(p) for p in paths))
        fn_rows = dict(zip(paths, fn_lists, strict=True))
        hits.sort(key=lambda h: (h.shape == "registration_call", h.path, h.line))
        for h in hits:
            if h.path not in fn_rows:
                continue
            rows = fn_rows[h.path]
            if h.shape == "registration_call" or is_registration_caller(h.scope):
                reg = await self._registrar_row(h.path, h.scope, h.line, rows)
                if reg is None:
                    continue
                got = await self._through_registrar(name, reg, method=method)
                if got:
                    return got
                continue
            cls = class_of_scope(h.scope, rows)
            if h.shape == "return_literal" and "::" in h.scope:
                cls = h.scope.rsplit("::", 1)[0]  # `X::getName` names X whether or not X is rowed
            if not cls and h.stated_class:
                cls = _class_by_bare_name(h.stated_class, rows)
            if cls:
                got = methods_of(cls, rows, method=method)
                if len(got) >= 2:
                    return got
                # the parser rows a template class's file: the constructor alone under
                # the class prefix, while the registrar's edges reach the methods — take
                # that route too
                via_reg = await self._through_file_registrars(name, rows, method=method)
                merged = list({str(r.get("symbol_id")): r for r in [*got, *via_reg]}.values())
                if merged:
                    return merged[:SEED_CAP]
            # a name struct: the registrar that names it (`NameQuantileGK::name`)
            struct = h.scope.rsplit("::", 1)[-1]
            if struct and SQL_NAME_RE.match(struct):
                got = await self._through_name_struct(name, struct, method=method)
                if got:
                    return got
        return []

    async def _by_convention(self, name: str, *, method: str) -> list[dict[str, Any]]:
        """The registration class the naming convention predicts, when the
        index holds it as a class and rows at least two of its methods (a
        template class rowed by its constructor alone goes to the chunk
        route, whose registrar fallback reaches the rest)."""
        for cls in registration_classes(name):
            if cls.startswith("Name"):
                continue  # a name struct has no methods; the chunk route follows it
            rows: list[dict[str, Any]] = await self.repo.symbols_by_name(
                self.org,
                self.ns,
                cls,
                limit=5,
            )
            klass = next(
                (r for r in rows if str(r.get("kind") or "") in {"class", "struct"}),
                None,
            )
            if klass is None:
                continue
            path = str(klass.get("path") or "")
            if not path.startswith(SRC_PREFIX):
                continue
            file_rows = await self._functions_in(path)
            got = methods_of(str(klass["qualified_name"]), file_rows, method=method)
            if len(got) >= 2 or (method and got):
                return got
        return []

    async def _through_file_registrars(
        self,
        name: str,
        rows: list[dict[str, Any]],
        *,
        method: str,
    ) -> list[dict[str, Any]]:
        """The registrar functions among one file's rows, their registration
        edges narrowed to the class the SQL name belongs to."""
        regs = [r for r in rows if is_registration_caller(str(r.get("qualified_name") or ""))]
        for reg in regs[:3]:
            got = await self._through_registrar(name, reg, method=method)
            if got:
                return got
        return []

    async def _through_name_struct(
        self,
        name: str,
        struct: str,
        *,
        method: str,
    ) -> list[dict[str, Any]]:
        chunks = await self._chunks_with_literal(struct, prefix=SRC_PREFIX, limit=20)
        for c in chunks:
            scope = str(c.get("scope") or "")
            if not is_registration_caller(scope):
                continue
            path = str(c.get("path") or "")
            rows = await self._functions_in(path)
            reg = await self._registrar_row(path, scope, int(c.get("start_line") or 0), rows)
            if reg is None:
                continue
            got = await self._through_registrar(name, reg, method=method)
            if got:
                return got
        return []

    async def is_registered(self, name: str) -> bool:
        """Is `name` itself a SQL-level name — a literal in one of the three
        registration shapes under src/? `geohashesInBox` and `arrayJoin` are both a
        registered function and a C++ free function of the same spelling: the symbol
        lookup binds the function, and the grep of the bare name must still count as
        naming the subject."""
        if not SQL_NAME_RE.match(name):
            return False
        chunks = await self._chunks_with_literal(f'"{name}"', prefix=SRC_PREFIX, limit=20)
        return bool(registration_hits(name, chunks))

    async def sql_names_for(self, rows: list[dict[str, Any]], *, limit: int = 3) -> list[str]:
        """The SQL names the class of `rows` registers, from the class file's
        chunks (`.h` and `.cpp` of one stem): name constants and return
        literals in the class's scope, registered literals whose lower-case
        form is inside the class name or the reverse. [] for a free function
        or a class outside the registries with no registration call."""
        cls = ""
        path = ""
        for r in rows:
            qn = str(r.get("qualified_name") or "")
            kind = str(r.get("kind") or "function")
            path = str(r.get("path") or "")
            if kind in {"class", "struct"}:
                cls = qn
            else:
                cls = qn.rsplit("::", 1)[0] if "::" in qn else ""
            if cls:
                break
        # a free function's parent is a namespace (`DB`), never a class
        if not cls or not path or "::" not in cls:
            return []
        if not path.startswith(SRC_PREFIX):
            return []
        stem = path.rsplit(".", 1)[0] + "."
        consts, regs = await asyncio.gather(
            self._chunks_with_literal("name", prefix=stem, limit=60),
            self._chunks_with_literal("register", prefix=stem, limit=30),
        )
        return sql_names_from_chunks(cls, consts, regs)[:limit]


def sql_names_from_chunks(
    cls: str,
    consts: list[dict[str, Any]],
    regs: list[dict[str, Any]],
) -> list[str]:
    """Pure half of `sql_names_for`: the names that belong to `cls`."""
    out: list[str] = []
    for c in consts:
        scope = str(c.get("scope") or "")
        path = str(c.get("path") or "")
        if not (scope == cls or scope.startswith(cls + "::")):
            continue
        if not path.startswith(NAME_CONSTANT_PATHS):
            continue
        content = str(c.get("content") or "")
        out.extend(_ANY_CONST_RE.findall(content))
        if scope.rsplit("::", 1)[-1] in _NAME_METHODS:
            out.extend(_ANY_RETURN_RE.findall(content))
    for c in regs:
        if not is_registration_caller(str(c.get("scope") or "")):
            continue
        if not str(c.get("path") or "").startswith(REGISTRY_PATHS):
            continue
        for n in _ANY_REG_RE.findall(str(c.get("content") or "")):
            if names_match(n, cls):
                out.append(n)
    return list(dict.fromkeys(n for n in out if SQL_NAME_RE.match(n)))


__all__ = [
    "COMBINATOR_SUFFIXES",
    "NAME_CONSTANT_PATHS",
    "REGISTRATION_CLASS_PREFIXES",
    "REGISTRY_PATHS",
    "RESOLVED_REGISTRATION",
    "RegistrationHit",
    "Resolver",
    "SQL_NAME_RE",
    "combinator_stems",
    "registration_classes",
    "class_core",
    "class_of_scope",
    "is_sql_name_candidate",
    "methods_of",
    "names_match",
    "narrow_targets",
    "registration_hits",
    "split_sql_form",
    "sql_names_from_chunks",
]
