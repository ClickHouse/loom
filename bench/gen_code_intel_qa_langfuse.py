"""Generate the langfuse lane of the code-intel QA bench:
`bench/fixtures/code_intel_qa_langfuse.jsonl` — a TypeScript monorepo with
a Rust service, a Prisma schema and ClickHouse migrations, indexed as the
bench namespace `bench-code-langfuse`.

Every gold is derived from the repository's OWN grammar by grep over the
checkout, never from the plane's derivation (the modules under test):

  trpc_procedure_def   "<router>::<procedure>"      symbol_def: the procedure
                        gold = the `key: …Procedure` line inside
                        `export const <router> = createTRPCRouter({` (tRPC's
                        own shape)
  ts_function_def      "<name>"                     symbol_def: an exported
                        function whose name is declared exactly once in the
                        tree (`export function` / `export const x = (…) =>`)
  prisma_model_def     "<Model>"                    symbol_def: `model X {`
                        in schema.prisma, for names no TypeScript file
                        exports under the same name
  rust_fn_def          "<fn>"                       symbol_def: a free
                        `pub fn` declared once across the crates
  vitest_suite_def     "<slug>"                     symbol_def: a top-level
                        `describe("…")` whose slug names nothing else
  ch_table_readers     test_set + facets [{table_read, t}, {dialect, clickhouse}]
                        gold = TypeScript files AND ClickHouse migrations
                        whose text holds `FROM t` / `JOIN t` for a table a
                        canonical migration creates
  ch_table_writers     test_set + facets [{table_write, t}, {dialect, clickhouse}]
                        (the dialect facet: langfuse's Postgres half has
                        tables of the same names — `observations`, `traces`,
                        `scores` — and a Prisma migration's view over them is
                        not a ClickHouse reader)
                        gold = the same files with `INSERT INTO t` /
                        `ALTER TABLE t` / `DELETE FROM t` / `OPTIMIZE TABLE t`
  component_render_callers  "components rendering <Name>"   callers: exported React
                        components (declared once, rendered by no test); the
                        callers are the top-level units whose formatter span
                        holds a `<Name` render site in another `.tsx` file
                        (JSX render edges are `calls`)
  ts_callers           callers of "<name>"                  callers: the exported
                        functions (declared once, called by no test) whose
                        call sites grep finds; the caller is the top-level
                        unit whose prettier-formatted span holds the call
                        line (`export function f(` … `^}` / `export const f =`
                        … `^};`), or the tRPC procedure key above the line
                        inside a `createTRPCRouter({` block
  abstain_probe        a symbol and a table the tree does not have

Set questions keep 2–40 gold files. Run from the repository root with the
checkout the namespace is indexed at:

  python bench/gen_code_intel_qa_langfuse.py --checkout <langfuse clone> \\
      > bench/fixtures/code_intel_qa_langfuse.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

NAMESPACE = "bench-code-langfuse"
MIN_FILES = 2
MAX_FILES = 40
MAX_PER_LANE = 40
# The generator serves any TypeScript repository — the
# namespace, the source roots and the id prefix come from the command line
# (langfuse's are the defaults); a lane with no rows for a repository is
# simply absent from its fixture
TS_DIRS: tuple[str, ...] = ("web/src", "worker/src", "packages/shared/src", "ee/src")
ID_PREFIX = "lf"
# A CommonJS repository keeps its tests in a tree of their own
# (`test/**/*.test.js`); `--test-dirs` names it
TEST_DIRS: tuple[str, ...] = ()
SKIP_DIRS = ("node_modules", "generated", "__tests__", ".next", "dist")

_ROUTER_RE = re.compile(r"^export const (\w+Router) = createTRPCRouter\(\{", re.M)
_PROC_RE = re.compile(r"^  (\w+): (?:protected|public)\w*Procedure", re.M)
_EXPORT_FN_RE = re.compile(r"^export (?:async )?function\*? (\w+)\s*[(<]", re.M)
# In a CommonJS file every column-0 function is a module unit
_JS_FN_RE = re.compile(r"^(?:async )?function\*? (\w+)\s*\(", re.M)
_PROTOTYPE_METHOD_RE = re.compile(r"^(\w+)\.prototype\.(\w+)\s*=\s*(?:async\s+)?function\b", re.M)
_EXPORT_CONST_FN_RE = re.compile(
    r"^export const (\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*(?::[^=]*)?=>", re.M
)
_ANY_DECL_RE = re.compile(
    r"^(?:export )?(?:async )?(?:function\*? |const |let |class |type |interface |enum )(\w+)", re.M
)
# any declaration of a name anywhere in a file (nested too): a bare-name
# symbol question is only fair when nobody else in the tree declares it
_ANY_NAME_RE = re.compile(
    r"\b(?:function\*?|const|let|var|class|type|interface|enum|struct|trait|mod|namespace)\s+(\w+)\b"
)
_MODEL_RE = re.compile(r"^model (\w+) \{", re.M)
_RUST_FN_RE = re.compile(r"^pub fn (\w+)", re.M)
_RUST_ANY_FN_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))? )?fn (\w+)", re.M)
_DESCRIBE_RE = re.compile(r'^describe(?:\.\w+)*\(\s*["\'`]([^"\'`]+)["\'`]', re.M)
# node:test / tap — a column-0 `test('name', …)` is a test block
_TEST_BLOCK_RE = re.compile(r'^test(?:\.\w+)*\(\s*["\'`]([^"\'`]+)["\'`]', re.M)
_CREATE_TABLE_RE = re.compile(r"^CREATE TABLE (?:IF NOT EXISTS )?(\w+)", re.M | re.I)
_DESCRIBE_ANY_RE = re.compile(r"\b" + _DESCRIBE_RE.pattern.lstrip("^"))  # nested describes too
_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")
_NOT_METHODS = frozenset({"constructor", "if", "for", "while", "switch", "return"})
_CLASS_DECL_RE = re.compile(r"^export (?:default )?(?:abstract )?class (\w+)\b", re.M)
_CLASS_METHOD_RE = re.compile(
    r"^  (?:(?:public|protected|static|async|override)\s+)*(\w+)\s*(?:<[^>]*>)?\([^)]*\)?"
    r"[^{;\n]*\{",
    re.M,
)


def ls_files(root: str, prefixes: tuple[str, ...] = ()) -> list[str]:
    out: list[str] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            rel = os.path.relpath(os.path.join(dirpath, f), root)
            if not prefixes or rel.startswith(prefixes):
                out.append(rel)
    return sorted(out)


def read(root: str, rel: str) -> str:
    try:
        return open(os.path.join(root, rel), encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def module_scope(path: str) -> str:
    stem = path[: path.rfind(".")] if "." in path.rsplit("/", 1)[-1] else path
    return stem.replace("/", ".")


def rust_scope(path: str) -> str:
    parts = path.split("/")
    i = parts.index("src") if "src" in parts else 0
    crate = parts[i - 1] if i > 0 else "crate"
    mods = parts[i + 1 :]
    if mods and mods[-1] in ("mod.rs", "lib.rs", "main.rs"):
        mods = mods[:-1]
    elif mods and mods[-1].endswith(".rs"):
        mods = [*mods[:-1], mods[-1][:-3]]
    return "::".join([crate, *mods])


def _row(
    qid: str,
    qtype: str,
    query: str,
    gold: dict,
    lane: str,
    facets: list | None = None,
) -> dict:
    qid = ID_PREFIX + qid[2:] if qid.startswith("lf") else qid
    row = {
        "id": qid,
        "type": qtype,
        "query": query,
        "gold": gold,
        "lane": lane,
        "namespace": NAMESPACE,
    }
    if facets:
        row["facets"] = facets
    return row


def ts_sources(root: str) -> dict[str, str]:
    return {
        rel: read(root, rel)
        for rel in ls_files(root, TS_DIRS)
        if rel.endswith((".ts", ".tsx", ".js", ".mjs", ".cjs"))
        and not re.search(r"\.(test|servertest|clienttest|stories)\.[mc]?[jt]sx?$", rel)
        and not rel.startswith(TEST_DIRS)
    }


def trpc_procedures(root: str, sources: dict[str, str]) -> list[dict]:
    rows: list[dict] = []
    for rel, text in sources.items():
        if "/routers/" not in rel and not rel.endswith("Router.ts"):
            continue
        for m in _ROUTER_RE.finditer(text):
            router = m.group(1)
            body = text[m.end() : text.find("\n});", m.end())]
            for p in _PROC_RE.finditer(body):
                proc = p.group(1)
                rows.append(
                    _row(
                        f"lftrpc{len(rows):03d}",
                        "symbol_def",
                        f"{router}::{proc}",
                        {
                            "qualified_name": f"{module_scope(rel)}::{router}::{proc}",
                            "path_prefix": rel,
                        },
                        "trpc_procedure_def",
                    )
                )
    return rows


def unique_ts_functions(sources: dict[str, str]) -> list[dict]:
    decl_counts: Counter[str] = Counter()
    where: dict[str, str] = {}
    for rel, text in sources.items():
        for m in _ANY_DECL_RE.finditer(text):
            decl_counts[m.group(1)] += 1
        for m in [*_EXPORT_FN_RE.finditer(text), *_EXPORT_CONST_FN_RE.finditer(text)]:
            where.setdefault(m.group(1), rel)
        if rel.endswith((".js", ".cjs")):
            for m in _JS_FN_RE.finditer(text):
                where.setdefault(m.group(1), rel)
    rows: list[dict] = []
    for name in sorted(where):
        if decl_counts[name] != 1 or len(name) < 6:
            continue
        rel = where[name]
        rows.append(
            _row(
                f"lffn{len(rows):03d}",
                "symbol_def",
                name,
                {"qualified_name": f"{module_scope(rel)}::{name}", "path_prefix": rel},
                "ts_function_def",
            )
        )
    return rows[:: max(1, len(rows) // MAX_PER_LANE)][:MAX_PER_LANE]


def declared_names(root: str, sources: dict[str, str]) -> set[str]:
    """Every name any TypeScript, Rust or test file declares anywhere."""
    names: set[str] = set()
    for src in sources.values():
        names.update(_ANY_NAME_RE.findall(src))
    for rel in ls_files(
        root,
        ("ai-gateway/src", "packages/native/src", "web/src", "worker/src", "packages/shared/src"),
    ):
        if rel.endswith((".rs", ".test.ts", ".servertest.ts", ".clienttest.ts", ".clienttest.tsx")):
            names.update(_ANY_NAME_RE.findall(read(root, rel)))
    return names


def class_methods(sources: dict[str, str]) -> list[dict]:
    """symbol_def rows `Class::method` for public methods of exported classes
    whose class name is declared once (the client library
    shape — `ClickHouseClient::query`, `ResultSet::json`)."""
    counts: Counter[str] = Counter()
    for text in sources.values():
        counts.update(_CLASS_DECL_RE.findall(text))
    rows: list[dict] = []
    for rel, text in sorted(sources.items()):
        for m in _CLASS_DECL_RE.finditer(text):
            cls = m.group(1)
            if counts[cls] != 1:
                continue
            start = m.end()
            end = text.find("\n}\n", start)
            body = text[start : end if end > 0 else len(text)]
            seen: set[str] = set()
            for mm in _CLASS_METHOD_RE.finditer(body):
                name = mm.group(1)
                if name in seen or name in _NOT_METHODS:
                    continue
                seen.add(name)
                rows.append(
                    _row(
                        f"lfmethod{len(rows):03d}",
                        "symbol_def",
                        f"{cls}::{name}",
                        {
                            "qualified_name": f"{module_scope(rel)}::{cls}::{name}",
                            "path_prefix": rel,
                        },
                        "class_method_def",
                    )
                )
    return rows[:: max(1, len(rows) // MAX_PER_LANE)][:MAX_PER_LANE]


def prisma_models(root: str, sources: dict[str, str]) -> list[dict]:
    schema_rel = "packages/shared/prisma/schema.prisma"
    text = read(root, schema_rel)
    if not text:
        return []
    exported = declared_names(root, sources)
    rows: list[dict] = []
    for name in _MODEL_RE.findall(text):
        if name in exported:
            continue
        rows.append(
            _row(
                f"lfmodel{len(rows):03d}",
                "symbol_def",
                name,
                {
                    "qualified_name": f"{module_scope(schema_rel)}::{name}",
                    "path_prefix": schema_rel,
                },
                "prisma_model_def",
            )
        )
    return rows[:MAX_PER_LANE]


def rust_functions(root: str) -> list[dict]:
    counts: Counter[str] = Counter()
    where: dict[str, str] = {}
    for rel in ls_files(root, ("ai-gateway/src", "packages/native/src")):
        if not rel.endswith(".rs"):
            continue
        text = read(root, rel)
        for m in _RUST_ANY_FN_RE.finditer(text):
            counts[m.group(1)] += 1
        for m in _RUST_FN_RE.finditer(text):
            where.setdefault(m.group(1), rel)
    rows: list[dict] = []
    for name in sorted(where):
        if counts[name] != 1:
            continue
        rel = where[name]
        rows.append(
            _row(
                f"lfrs{len(rows):03d}",
                "symbol_def",
                name,
                {"qualified_name": f"{rust_scope(rel)}::{name}", "path_prefix": rel},
                "rust_fn_def",
            )
        )
    return rows[:MAX_PER_LANE]


_METHOD_CALL_NOISE = frozenset(
    {
        "then",
        "catch",
        "finally",
        "map",
        "filter",
        "forEach",
        "push",
        "get",
        "set",
        "has",
        "add",
        "delete",
        "toString",
        "on",
        "once",
        "emit",
        "end",
        "write",
        "close",
        "open",
        "start",
        "stop",
        "run",
        "send",
        "call",
        "apply",
        "bind",
        "next",
        "done",
        "init",
        "update",
        "create",
        "remove",
        "find",
        "join",
        "split",
        "slice",
        "keys",
        "values",
        "entries",
        "log",
        "error",
        "warn",
        "info",
        "test",
        "match",
        "exec",
        "query",
        "execute",
        "parse",
        "format",
        "build",
        "load",
        "save",
    }
)


def method_callers(root: str, sources: dict[str, str]) -> list[dict]:
    """callers rows for methods: a class or prototype
    method whose NAME is declared as a method exactly once in the tree and
    is no bare function anywhere, called as `.name(` from 1–20 units of
    non-test files. The gold cannot type receivers; uniqueness of the
    method name makes every `.name(` site a call to it, and the noise list
    keeps out the names a library object answers to."""
    tests = test_sources(root)
    declared: Counter[str] = Counter()
    owner: dict[str, tuple[str, str]] = {}  # method -> (Class::method qualified, file)
    fn_names: set[str] = set()
    for rel, text in sources.items():
        for m in _ANY_DECL_RE.finditer(text):
            fn_names.add(m.group(1))
        for m in _PROTOTYPE_METHOD_RE.finditer(text):
            declared[m.group(2)] += 1
            owner.setdefault(m.group(2), (f"{module_scope(rel)}::{m.group(1)}::{m.group(2)}", rel))
        for cm in _CLASS_DECL_RE.finditer(text):
            cls = cm.group(1)
            body = text[cm.end() : _class_end(text, cm.end())]
            for mm in _CLASS_METHOD_RE.finditer(body):
                name = mm.group(1)
                if name in _NOT_METHODS:
                    continue
                declared[name] += 1
                owner.setdefault(name, (f"{module_scope(rel)}::{cls}::{name}", rel))
    routers = {m.group(1) for text in sources.values() for m in _ROUTER_RE.finditer(text)}
    rows: list[dict] = []
    for name in sorted(owner):
        if declared[name] != 1 or name in fn_names or name in _METHOD_CALL_NOISE or len(name) < 5:
            continue
        pat = re.compile(rf"\.{re.escape(name)}\(")
        # without a named test tree the gold cannot name a nested `it` block,
        # so a method tests call is skipped (the ts_callers rule)
        if not TEST_DIRS and any(pat.search(t) for t in tests.values()):
            continue
        qualified, _rel = owner[name]
        callers: set[str] = set()
        scan = {**sources, **tests} if TEST_DIRS else sources  # a test block is a caller too
        for rel, text in scan.items():
            for m in pat.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                q = caller_of_line(text, rel, line, routers)
                if not q:
                    continue
                if q == qualified and not text[max(0, m.start() - 4) : m.start()].endswith("this"):
                    continue  # `this.raw.getHeader()` inside getHeader is not recursion
                callers.add(q)  # `this.getHeader()` inside it is; the plane records that edge
        if 1 <= len(callers) <= 20:
            symbol = qualified.split("::", 1)[1]
            gold = {"symbol": symbol, "expected_callers": sorted(callers)}
            rows.append(
                _row(
                    f"lfmcall{len(rows):03d}",
                    "callers",
                    f"callers of {symbol}",
                    gold,
                    "method_callers",
                )
            )
    return rows[:: max(1, len(rows) // MAX_PER_LANE)][:MAX_PER_LANE]


def _class_end(text: str, start: int) -> int:
    """Offset of the class body's closing `}` at column 0 after `start`."""
    m = re.compile(r"^\}", re.M).search(text, start)
    return m.start() if m else len(text)


def prototype_methods(sources: dict[str, str]) -> list[dict]:
    """symbol_def rows `Cls::method` for CommonJS prototype methods:
    `Reply.prototype.send = function` declared once in the tree."""
    counts: Counter[str] = Counter()
    where: dict[str, tuple[str, str]] = {}
    for rel, text in sources.items():
        if not rel.endswith((".js", ".cjs")):
            continue
        for m in _PROTOTYPE_METHOD_RE.finditer(text):
            key = f"{m.group(1)}::{m.group(2)}"
            counts[key] += 1
            where.setdefault(key, (rel, m.group(2)))
    rows: list[dict] = []
    for key in sorted(where):
        rel, method = where[key]
        if counts[key] != 1 or len(method) < 4:
            continue
        rows.append(
            _row(
                f"lfproto{len(rows):03d}",
                "symbol_def",
                key,
                {"qualified_name": f"{module_scope(rel)}::{key}", "path_prefix": rel},
                "prototype_method_def",
            )
        )
    return rows[:: max(1, len(rows) // MAX_PER_LANE)][:MAX_PER_LANE]


def vitest_suites(root: str, sources: dict[str, str]) -> list[dict]:
    exported = declared_names(root, sources)
    rows: list[dict] = []
    seen: Counter[str] = Counter()
    tests = list(test_sources(root))
    cands: list[tuple[str, str]] = []
    for rel in sorted(tests):
        text = read(root, rel)
        # every describe, nested ones too, counts toward uniqueness; only a
        # top-level one is a candidate (its symbol sits directly under the module)
        for m in _DESCRIBE_ANY_RE.finditer(text):
            seen[_SLUG_RE.sub("_", m.group(1)).strip("_")[:60]] += 1
        for m in _DESCRIBE_RE.finditer(text):
            slug = _SLUG_RE.sub("_", m.group(1)).strip("_")[:60]
            if slug and slug not in exported:
                cands.append((slug, rel))
    for slug, rel in cands:
        if seen[slug] != 1:
            continue
        rows.append(
            _row(
                f"lfsuite{len(rows):03d}",
                "symbol_def",
                slug,
                {"qualified_name": f"{module_scope(rel)}::{slug}", "path_prefix": rel},
                "vitest_suite_def",
            )
        )
    if not rows:
        # a tree without describe blocks: its top-level `test('…')` blocks are
        # the units (`test_<slug>`), unique by slug across the test tree
        blocks: Counter[str] = Counter()
        found: list[tuple[str, str]] = []
        for rel in sorted(tests):
            text = read(root, rel)
            for m in _TEST_BLOCK_RE.finditer(text):
                slug = "test_" + _SLUG_RE.sub("_", m.group(1)).strip("_")[:60]
                blocks[slug] += 1
                found.append((slug, rel))
        for slug, rel in found:
            if blocks[slug] == 1 and len(slug) > 12:
                gold = {"qualified_name": f"{module_scope(rel)}::{slug}", "path_prefix": rel}
                rows.append(
                    _row(f"lfsuite{len(rows):03d}", "symbol_def", slug, gold, "test_block_def")
                )
    return rows[:: max(1, len(rows) // MAX_PER_LANE)][:MAX_PER_LANE]


def ch_tables(root: str) -> list[str]:
    names: set[str] = set()
    for rel in ls_files(root, ("packages/shared/clickhouse/migrations/canonical",)):
        if rel.endswith(".up.sql"):
            names.update(_CREATE_TABLE_RE.findall(read(root, rel)))
    return sorted(names)


def script_sources(root: str) -> dict[str, str]:
    """The seeder and the other repository scripts write the tables too."""
    return {
        rel: read(root, rel)
        for rel in ls_files(root, ("packages/shared/scripts/", "scripts/"))
        if rel.endswith((".ts", ".mjs"))
    }


def sql_sources(root: str) -> dict[str, str]:
    """The ClickHouse migrations: a `FROM t` in a materialized view's
    definition is a reader of `t` as much as a TypeScript query is."""
    return {
        rel: read(root, rel)
        for rel in ls_files(root, ("packages/shared/clickhouse/",))
        if rel.endswith(".sql")
    }


def test_sources(root: str) -> dict[str, str]:
    """The test files too: a `servertest` that queries `events_core` is
    code that reads the table (the symbol lanes exclude tests, the table
    lanes must not)."""
    out: dict[str, str] = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".next", "dist", ".git")]
        for f in files:
            rel = os.path.relpath(os.path.join(dirpath, f), root)
            is_test = re.search(r"\.(test|servertest|clienttest)\.[mc]?[jt]sx?$", rel) or (
                rel.startswith(TEST_DIRS) and rel.endswith((".js", ".mjs", ".ts"))
            )
            if (rel.startswith(TS_DIRS) or rel.startswith(TEST_DIRS)) and is_test:
                out[rel] = read(root, rel)
    return out


def table_sets(sources: dict[str, str], tables: list[str]) -> list[dict]:
    rows: list[dict] = []
    for t in tables:
        readers = sorted(
            rel
            for rel, text in sources.items()
            if re.search(rf"\b(?:FROM|JOIN)\s+{re.escape(t)}\b", text, re.I)
        )
        writers = sorted(
            rel
            for rel, text in sources.items()
            if re.search(
                rf"\b(?:INSERT\s+INTO|ALTER\s+TABLE|DELETE\s+FROM|OPTIMIZE\s+TABLE|TRUNCATE(?:\s+TABLE)?)"
                rf"\s+{re.escape(t)}\b",
                text,
                re.I,
            )
        )
        if MIN_FILES <= len(readers) <= MAX_FILES:
            rows.append(
                _row(
                    f"lfread_{t}",
                    "test_set",
                    f"code that reads the ClickHouse table {t}",
                    {"paths": readers},
                    "ch_table_readers",
                    [{"kind": "table_read", "name": t}, {"kind": "dialect", "name": "clickhouse"}],
                )
            )
        if MIN_FILES <= len(writers) <= MAX_FILES:
            rows.append(
                _row(
                    f"lfwrite_{t}",
                    "test_set",
                    f"code that writes the ClickHouse table {t}",
                    {"paths": writers},
                    "ch_table_writers",
                    [{"kind": "table_write", "name": t}, {"kind": "dialect", "name": "clickhouse"}],
                )
            )
    return rows


_UNIT_START_RE = re.compile(
    r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?"
    r"(?:function\*?\s+(\w+)|const\s+(\w+)\s*(?::(?:[^=]|=>)*?)?=(?!>)|(?:abstract\s+)?class\s+(\w+)"
    r"|(\w+\.prototype\.\w+)\s*="
    r"|((?:test|it|describe|suite|context)(?:\.\w+)*)\(\s*([\"'`])((?:(?!\6)[^\\\n]|\\.)*)\6)",
    re.M,
)
_TEST_CALLEES = frozenset({"test", "it"})


def _unit_name(m: re.Match[str]) -> str:
    """The plane's name for the unit a start line opens: a declaration's
    name, `Cls::method` for a prototype assignment, `test_<slug>` for a
    top-level test block and `<slug>` for a describe block (the test tree's
    units are callers too)."""
    if m.group(4):
        return m.group(4).replace(".prototype.", "::")
    if m.group(5):
        # the parser reads the string with its escapes resolved
        title = m.group(7).replace("\\'", "'").replace('\\"', '"')
        slug = _SLUG_RE.sub("_", title).strip("_")[:60]
        callee = m.group(5).split(".")[0]
        return ("test_" + slug) if callee in _TEST_CALLEES else slug
    return m.group(1) or m.group(2) or m.group(3)


# a member of a top-level object literal whose value is a function
# (`  render: (args) => …`, `  render(args) {`, `  render: function`): the
# plane's unit for the render site is `Const::member`
_MEMBER_FN_RE = re.compile(
    r"^  (\w+)\s*(?::\s*(?:async\s*)?(?:\([^)\n]*\)|\w+)\s*(?::[^=\n]*)?=>"
    r"|\([^)\n]*\)\s*(?::[^{\n]*)?\{|:\s*(?:async\s+)?function\b)",
    re.M,
)
# a formatter-closed unit ends at column 0: `}`, `});`, `];`, `)` …
_UNIT_END_RE = re.compile(r"^[\]\}\)`]+(?:\s+(?:as|satisfies)\s+[^;\n]+)?;?\s*$", re.M)
# `const Story: Meta = {` or `const Story = meta.story({` — an object literal,
# bare or as a call's argument (the plane names its function members)
_OBJECT_HEAD_RE = re.compile(
    r"^(?:export )?(?:default )?const \w+(?:\s*:[^=\n]+)?\s*=\s*(?:[\w.]+\()?\{\s*$"
)
_NOT_MEMBERS = frozenset({"if", "for", "while", "switch", "return", "catch", "else", "try"})
_PROC_KEY_RE = re.compile(r"^  (\w+): (?:protected|public)\w*Procedure", re.M)
# a class body's methods sit at two spaces: `  async run(` / `  static of(` / `  private x(`
_METHOD_RE = re.compile(
    r"^  (?:(?:public|private|protected|static|async|readonly|override)\s+)*"
    r"(\w+)\s*(?:<[^>]*>)?\(",
    re.M,
)
# a Next.js route: `export default withMiddlewares({` … `  GET: createAuthedProjectAPIRoute({` …
# … `fn:`
_HTTP_KEY_RE = re.compile(r"^  (GET|POST|PUT|PATCH|DELETE): ", re.M)
_CLASS_START_RE = re.compile(r"^(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+", re.M)


def top_level_units(text: str) -> list[tuple[int, int, str]]:
    """(start line, end line, name) of every prettier-formatted top-level
    unit: a declaration at column 0 to the next `}` / `});` at column 0."""
    starts = [
        (text.count("\n", 0, m.start()) + 1, _unit_name(m), m.end())
        for m in _UNIT_START_RE.finditer(text)
    ]
    ends = [text.count("\n", 0, m.start()) + 1 for m in _UNIT_END_RE.finditer(text)]
    out: list[tuple[int, int, str]] = []
    lines = text.split("\n")
    for i, (start, name, _pos) in enumerate(starts):
        head = lines[start - 1].rstrip()
        # a unit never swallows the next column-0 declaration (an arrow whose
        # expression body ends indented — `): T =>\n  x;` — has no closer)
        bound = starts[i + 1][0] - 1 if i + 1 < len(starts) else len(lines)
        if head.endswith(";") or (
            head.startswith(("const ", "let ", "var ", "export const ")) and _balanced(head)
        ):
            end = start  # a one-line unit (`export const f = (): T => ({ … });`, semicolon or not)
        elif head.endswith("="):
            # the value starts on the next line (`const X: T =\n  forwardRef(Y);`):
            # the unit ends at the first line closing with `;`
            end = next(
                (i + 1 for i in range(start, len(lines)) if lines[i].rstrip().endswith(";")), start
            )
        else:
            end = next((e for e in ends if e >= start), start)
        out.append((start, min(end, bound) if bound >= start else end, name))
    return out


def _last_key_before(text: str, rx: re.Pattern[str], start: int, line: int) -> str:
    key = ""
    for m in rx.finditer(text):
        k_line = text.count("\n", 0, m.start()) + 1
        if start < k_line <= line:
            key = m.group(1)
    return key


_MODULE_LEVEL_CONST_RE = re.compile(r"^(?:const|let|var) \w+\s*=.*$")
# A nested test block inside a top-level test — `  await
# t.test('name', async t => {` at some indent, closed by `})` at the same indent
_NESTED_TEST_RE = re.compile(
    r"^([ \t]+)(?:await[ \t]+)?(?:\w+\.)?(?:test|it)(?:\.\w+)*\(\s*"
    r"([\"'`])((?:(?!\2)[^\\\n]|\\.)*)\2",
    re.M,
)


def _block_close(lines: list[str], open_line: int, end: int) -> int:
    """The line (1-indexed) on which the brackets opened from `open_line`
    return to balance — the end of a `t.test('…', async t => { … })` block
    whether its callback opens on the same line or the next."""
    depth = 0
    opened = False
    for i in range(open_line - 1, min(end, len(lines))):
        for ch in re.sub(r"(['\"`])(?:\\.|(?!\1).)*\1", "", lines[i]):  # strings out
            if ch in "([{":
                depth += 1
                opened = True
            elif ch in ")]}":
                depth -= 1
        if opened and depth <= 0:
            return i + 1
    return end


def _nested_test_units(text: str, start: int, end: int, line: int) -> list[str]:
    """The `test_<slug>` names of the nested test blocks (outermost first)
    whose bodies hold `line`, within the top-level unit spanning start..end
    (the plane names them `parent::test_<slug>`)."""
    lines = text.split("\n")
    out: list[str] = []
    for m in _NESTED_TEST_RE.finditer(text):
        open_line = text.count("\n", 0, m.start()) + 1
        if not (start < open_line <= line <= end):
            continue
        close = _block_close(lines, open_line, end)
        if open_line <= line <= close:
            title = m.group(3).replace("\\'", "'").replace('\\"', '"')
            out.append("test_" + _SLUG_RE.sub("_", title).strip("_")[:60])
    return out


def _balanced(line: str) -> bool:
    """Every bracket the line opens it closes: a complete one-line statement
    in a codebase without semicolons (`const x = f(y)`)."""
    depth = {"(": 0, "[": 0, "{": 0}
    close = {")": "(", "]": "[", "}": "{"}
    for ch in line:
        if ch in depth:
            depth[ch] += 1
        elif ch in close:
            depth[close[ch]] -= 1
            if depth[close[ch]] < 0:
                return False
    return not any(depth.values())


def caller_of_line(text: str, rel: str, line: int, routers: set[str]) -> str:
    """The qualified name of the unit holding `line`, as the plane names
    it: a router's procedure (`module::router::key`), a class's method
    (`module::Class::method`), a route's handler (`module::unit::GET::fn`),
    else the top-level unit."""
    lines = text.split("\n")
    for start, end, name in top_level_units(text):
        if not (start <= line <= end):
            continue
        if start == end and _MODULE_LEVEL_CONST_RE.match(lines[start - 1]):
            # `const onRequestHookRunner = hookRunnerGenerator(…)` at module
            # level: the call runs at import time inside no unit the plane
            # names (a one-line unexported const is not a symbol)
            return ""
        mod = module_scope(rel)
        if name in routers:
            key = _last_key_before(text, _PROC_KEY_RE, start, line)
            return f"{mod}::{name}::{key}" if key else f"{mod}::{name}"
        head = lines[start - 1]
        if _CLASS_START_RE.match(head):
            method = _last_key_before(text, _METHOD_RE, start, line)
            return f"{mod}::{name}::{method}" if method else f"{mod}::{name}"
        body = "\n".join(lines[start - 1 : end])
        if "withMiddlewares(" in body or "createAuthedProjectAPIRoute(" in body:
            http = _last_key_before(text, _HTTP_KEY_RE, start, line)
            unit = name if not head.startswith("export default") else "default"
            return f"{mod}::{unit}::{http}::fn" if http else f"{mod}::{unit}::fn"
        if _UNIT_START_RE.match(head) and re.match(r"^(?:test|it|describe)", head):
            nested = _nested_test_units(text, start, end, line)
            if nested:
                return "::".join((mod, name, *nested))
        if _OBJECT_HEAD_RE.match(head):  # a top-level object literal (a story, a meta)
            member = _last_key_before(text, _MEMBER_FN_RE, start, line)
            if member and member not in _NOT_MEMBERS and line > start:
                return f"{mod}::{name}::{member}"
        return f"{mod}::{name}"
    return ""


def ts_callers(root: str, sources: dict[str, str], defs: list[dict]) -> list[dict]:
    """callers rows for the ts_function_def lane's functions that no test
    calls: every non-test call site's enclosing top-level unit."""
    tests = test_sources(root)
    routers = {m.group(1) for text in sources.values() for m in _ROUTER_RE.finditer(text)}
    # In a repository with a test tree (`--test-dirs`) the
    # top-level `test('…')` blocks are units the plane names (`test_<slug>`),
    # so a test is a caller like any other; without one (nested vitest
    # `it` under `describe`) the gold cannot name the block and skips the
    # functions tests call
    scan = {**sources, **tests} if TEST_DIRS else sources
    rows: list[dict] = []
    for d in defs:
        name = d["query"]
        # `f(` and `obj.f(` alike: the name is declared once in the tree, so a
        # member call on a decorated instance (`fastify.getDefaultJsonParser(`)
        # is a call to it — the plane's repo-unique rung binds it the same way
        pat = re.compile(rf"(?<!\w){re.escape(name)}\(")
        if not TEST_DIRS and any(pat.search(t) for t in tests.values()):
            continue
        callers: set[str] = set()
        for rel, text in scan.items():
            for m in pat.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                q = caller_of_line(text, rel, line, routers)
                if q and q != d["gold"]["qualified_name"]:
                    callers.add(q)
        if 1 <= len(callers) <= 20:
            rows.append(
                _row(
                    f"lfcallers{len(rows):03d}",
                    "callers",
                    f"callers of {name}",
                    {"symbol": name, "expected_callers": sorted(callers)},
                    "ts_callers",
                )
            )
    return rows[:MAX_PER_LANE]


_TYPE_DECL_RE = re.compile(
    r"^(?:export (?:default )?)?(?:abstract )?(?:class|interface|enum|type) (\w+)\b", re.M
)
_CLASS_HEAD_RE = re.compile(r"^(?:export (?:default )?)?(?:abstract )?class (\w+)", re.M)
# runtime / host globals a class extends without importing them (mirrors the
# resolver's list): never a repository base, whatever the tree names so
_SCRIPT_GLOBALS = frozenset(
    {
        "Error",
        "TypeError",
        "RangeError",
        "SyntaxError",
        "ReferenceError",
        "Map",
        "Set",
        "WeakMap",
        "WeakSet",
        "Promise",
        "Array",
        "Object",
        "Function",
        "Date",
        "RegExp",
        "Event",
        "EventTarget",
        "HTMLElement",
        "Element",
        "Node",
        "CustomEvent",
        "Transform",
        "Readable",
        "Writable",
        "Duplex",
        "EventEmitter",
        "Stream",
        "Component",
        "PureComponent",
    }
)


def _class_heads(text: str) -> list[tuple[str, str]]:
    """(class name, heritage clause) per class declaration, the clause read
    with balanced angle brackets so a generic parameter list spanning lines
    (`class B<\n  T extends Record<string, string>,\n> extends A {`) is
    stepped over; '' when the class has no clause."""
    out: list[tuple[str, str]] = []
    for m in _CLASS_HEAD_RE.finditer(text):
        i, depth, clause_start = m.end(), 0, m.end()
        while i < len(text) and (depth or text[i] != "{"):
            c = text[i]
            if c == "<":
                depth += 1
            elif c == ">" and depth:
                depth -= 1
                # the class's own `<T, …>` (nothing but space before it) is
                # not part of the clause; a generic on a base stays inside it
                if depth == 0 and text[clause_start:i].lstrip().startswith("<"):
                    clause_start = i + 1
            i += 1
        clause = text[clause_start:i].strip() if i < len(text) else ""
        out.append((m.group(1), clause if clause.startswith(("extends", "implements")) else ""))
    return out


def _strip_generics(s: str) -> str:
    out, depth = [], 0
    for c in s:
        if c == "<":
            depth += 1
        elif c == ">" and depth:
            depth -= 1
        elif not depth:
            out.append(c)
    return "".join(out)


def _heritage_bases(clause: str) -> list[str]:
    """Bare base names of an `extends A implements B, C<T>` clause; a
    member-expression base (`React.Component`) and a runtime global
    (`Error`) are a library's and skipped."""
    out: list[str] = []
    for _kw, lst in re.findall(
        r"(extends|implements)\s+(.+?)(?=\s+(?:extends|implements)\s|$)",
        _strip_generics(clause).replace("\n", " ").strip(),
    ):
        for b in re.split(r",\s*", lst.strip()):
            b = b.strip()
            if b and "." not in b and b not in _SCRIPT_GLOBALS and re.fullmatch(r"\w+", b):
                out.append(b)
    return out


def _excluded_sources(root: str, sources: dict[str, str]) -> dict[str, str]:
    """Script files under the source roots that `ts_sources` left out."""
    out: dict[str, str] = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".next", "dist", ".git")]
        for f in files:
            rel = os.path.relpath(os.path.join(dirpath, f), root)
            if (
                rel.startswith(TS_DIRS)
                and rel.endswith((".ts", ".tsx", ".js", ".mjs", ".cjs"))
                and rel not in sources
            ):
                out[rel] = read(root, rel)
    return out


def implementors(root: str, sources: dict[str, str]) -> list[dict]:
    """callers rows over the type hierarchy: a class,
    interface or enum whose NAME is declared exactly once in the tree and
    that 1-30 classes of non-test files extend or implement. The expected
    set is those classes; the walk is restricted to `inherits` edges. A base
    a test file also derives from is skipped (the gold reads product code)."""
    tests = test_sources(root)
    declared: Counter[str] = Counter()
    for text in [*sources.values(), *tests.values()]:
        for m in _TYPE_DECL_RE.finditer(text):
            declared[m.group(1)] += 1
    derived_by: dict[str, set[str]] = {}
    in_tests: set[str] = set()
    for rel, text in sources.items():
        for name, clause in _class_heads(text):
            for base in _heritage_bases(clause):
                derived_by.setdefault(base, set()).add(f"{module_scope(rel)}::{name}")
    # a test file by name, or any file the source selection left out
    # (`__tests__/utils/x.ts`): a class derived there is a test double
    for _rel, text in [*tests.items(), *_excluded_sources(root, sources).items()]:
        for _name, clause in _class_heads(text):
            in_tests.update(_heritage_bases(clause))
    rows: list[dict] = []
    for base in sorted(derived_by):
        if declared[base] != 1 or base in in_tests or not 1 <= len(derived_by[base]) <= 30:
            continue
        row = _row(
            f"lfimpl{len(rows):03d}",
            "callers",
            f"classes that implement or extend {base}",
            {"symbol": base, "expected_callers": sorted(derived_by[base])},
            "implementors",
        )
        row["edge_kinds"] = ["inherits"]
        rows.append(row)
    return rows[:MAX_PER_LANE]


_COMPONENT_DECL_RE = re.compile(
    r"^export (?:default )?(?:function ([A-Z]\w+)\s*[(<]"
    r"|const ([A-Z]\w+)\s*(?::(?:[^=]|=>)*?)?=(?!>))",
    re.M,
)


def story_sources(root: str) -> dict[str, str]:
    """`*.stories.tsx`: not product code (the symbol lanes skip them) but
    real render sites — a component's story renders it, and the plane
    records that edge, so the render-callers gold must count it."""
    return {
        rel: read(root, rel)
        for rel in ls_files(root, TS_DIRS)
        if re.search(r"\.stories\.tsx?$", rel)
    }


def component_render_callers(root: str, sources: dict[str, str]) -> list[dict]:
    """callers rows for exported React components:
    the callers of `<Name …>` are the top-level units whose formatter span
    holds the render site, in non-test `.tsx` files (stories included)
    other than the component's own; components whose name is declared
    exactly once in the whole tree (a story's or a test's local `Cell`
    makes the bare-name question ambiguous), rendered by 1–20 units."""
    tests = test_sources(root)
    stories = story_sources(root)
    counts: Counter[str] = Counter()
    where: dict[str, str] = {}
    for rel, text in sources.items():
        if not rel.endswith(".tsx"):
            continue
        for m in _COMPONENT_DECL_RE.finditer(text):
            name = m.group(1) or m.group(2)
            counts[name] += 1
            where.setdefault(name, rel)
    everywhere: Counter[str] = Counter()
    for text in (*sources.values(), *tests.values(), *stories.values()):
        for m in _ANY_DECL_RE.finditer(text):
            everywhere[m.group(1)] += 1
    routers: set[str] = set()
    rows: list[dict] = []
    for name in sorted(where):
        if counts[name] != 1 or everywhere[name] != 1:
            continue
        pat = re.compile(rf"<{re.escape(name)}\b")
        # A story's `meta = { component: Name }` registers the
        # component — a value reference the plane records as a read
        reg = re.compile(rf"\bcomponent:\s*{re.escape(name)}\b")
        if any(pat.search(t) for t in tests.values()):
            continue
        callers: set[str] = set()
        for rel, text in (*sources.items(), *stories.items()):
            if not rel.endswith(".tsx"):
                continue
            pats = (pat, reg) if rel in stories else (pat,)
            for p_ in pats:
                for m in p_.finditer(text):
                    line = text.count("\n", 0, m.start()) + 1
                    # A site no unit holds belongs to the file
                    q = caller_of_line(text, rel, line, routers) or rel
                    # a render inside the component's own unit is not a caller;
                    # another unit of the same file is (the plane records it)
                    if rel == where[name] and (q == rel or q.endswith(f"::{name}")):
                        continue
                    callers.add(q)
        if 1 <= len(callers) <= 20:
            rows.append(
                _row(
                    f"lfrender{len(rows):03d}",
                    "callers",
                    f"components rendering {name}",
                    {"symbol": name, "expected_callers": sorted(callers)},
                    "component_render_callers",
                )
            )
    return rows[:: max(1, len(rows) // MAX_PER_LANE)][:MAX_PER_LANE]


_PROBE_NAMES = (
    "getObservationsByMoonPhase",
    "recomputeTidalLedger",
    "quarantineOrbitalCache",
    "harvestGlacialCursor",
    "rebalanceAirshipQuota",
)
_PROBE_TABLES = ("lunar_events", "tidal_ledger", "orbital_quarantine", "glacial_cursors")


def _absent(name: str, corpus: str) -> bool:
    """Every camelCase / snake_case token of the name is absent from the
    tree (case-insensitive): a probe the lexical leg can reach is not a
    probe (click-ui holds a `Moon` icon and a `Table` component)."""
    tokens = [t for t in re.split(r"[_\s]|(?<=[a-z])(?=[A-Z])", name) if len(t) > 2]
    return all(t.lower() not in corpus for t in tokens)


def abstain_probes(sources: dict[str, str] | None = None) -> list[dict]:
    corpus = "\n".join((sources or {}).values()).lower()
    fn = next((n for n in _PROBE_NAMES if _absent(n, corpus)), _PROBE_NAMES[0])
    tbl = next((n for n in _PROBE_TABLES if _absent(n, corpus)), _PROBE_TABLES[0])
    return [
        _row("lfabstain000", "abstain_probe", fn, {}, "abstain_probe"),
        _row(
            "lfabstain001",
            "abstain_probe",
            f"TypeScript that reads the ClickHouse table {tbl}",
            {},
            "abstain_probe",
            [{"kind": "table_read", "name": tbl}],
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    global NAMESPACE, TS_DIRS, ID_PREFIX, TEST_DIRS
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkout", required=True, help="a clone at the indexed commit")
    ap.add_argument("--namespace", default=NAMESPACE)
    ap.add_argument(
        "--src-dirs",
        default=",".join(TS_DIRS),
        help="comma-separated source roots (tests excluded by name)",
    )
    ap.add_argument("--id-prefix", default=ID_PREFIX)
    ap.add_argument("--test-dirs", default="", help="CommonJS test tree prefixes")
    args = ap.parse_args(argv)
    NAMESPACE, ID_PREFIX = args.namespace, args.id_prefix
    TEST_DIRS = tuple(p for p in args.test_dirs.split(",") if p)
    TS_DIRS = tuple(d.strip() for d in args.src_dirs.split(",") if d.strip())
    root = args.checkout
    sources = ts_sources(root)
    fn_defs = unique_ts_functions(sources)
    rows = [
        *trpc_procedures(root, sources)[:MAX_PER_LANE],
        *fn_defs,
        *ts_callers(root, sources, fn_defs),
        *class_methods(sources),
        *prototype_methods(sources),
        *method_callers(root, sources),
        *component_render_callers(root, sources),
        *implementors(root, sources),
        *prisma_models(root, sources),
        *rust_functions(root),
        *vitest_suites(root, sources),
        *table_sets(
            {**sources, **sql_sources(root), **test_sources(root), **script_sources(root)},
            ch_tables(root),
        ),
        *abstain_probes(sources),
    ]
    for r in rows:
        sys.stdout.write(json.dumps(r, ensure_ascii=False) + "\n")
    lanes = Counter(r["lane"] for r in rows)
    print(f"{len(rows)} rows: {dict(lanes)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
