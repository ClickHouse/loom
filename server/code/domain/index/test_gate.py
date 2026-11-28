"""Pure helpers for code.test_gate: the pre-PR obligation list.

A change carries two obligations before "done": the tests linked to it must run, and the
parts of it a coverage run measured unexecuted must be named (so the author writes a test,
or says why not). This module turns the raw evidence into test rows ordered by the
STRENGTH of their evidence, each saying why it is there and how to run it, and sorts the
touched definitions into `untested` (measured unexecuted) and `unlinked` (no coverage run
and no static link).

Why evidence order: a test file that is itself in the diff is an obligation on its own
evidence (you edited it, run it); a coverage run measured its tests executing the
definition; a static call chain names a test that reaches the definition; a stem partner
(`foo.py` → `test_foo.py`) is the repository's own naming convention about the file; a
test-map row is mined history about the file; a file neighbour is a test that calls other
definitions of the edited file and says nothing about this one, so neighbours only fill a
short list. A test file is listed once, under its strongest kind, so a kind about the
definition comes before a kind about the file: the row then names the test and the
definition it links to.

A static graph cannot prove that no test runs a definition: tests reach code through
fixtures, string dispatch and public entry points the graph holds no edge for. So only a
coverage run says `untested`, and a file-level row never clears `unlinked`.

Run commands: a row carries a runnable `run` when the harness is derivable
from the path, else `run_unknown=True` — never a guessed suite command.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

# Evidence kinds, strongest first. The integer is the sort key and the name rides the wire
# as `why`; keep the order stable — the plugin renders it.
CHANGED_TEST = "changed_test"
COVERAGE = "coverage"
STATIC_CALL = "static_call"
STEM_PARTNER = "stem_partner"
TEST_MAP = "test_map"
FILE_NEIGHBOR = "file_neighbor"
_ORDER = {
    CHANGED_TEST: 0,
    COVERAGE: 1,
    STATIC_CALL: 2,
    STEM_PARTNER: 3,
    TEST_MAP: 4,
    FILE_NEIGHBOR: 5,
}
# File neighbours are added only while the list is shorter than this: they say nothing
# about the touched definitions, so they are a lead when little else is known and never
# take a row's place.
NEIGHBOR_FILL = 4
# Definitions a row names in `links_to`.
_LINKS_SHOWN = 10

_TEST_DIR_RE = re.compile(r"(^|/)(tests?|__tests__|spec|specs)(/|$)")
_TEST_FILE_RE = re.compile(
    r"(^|/)(test_[^/]+\.(py|sh|sql|rb|go)|[^/]+_test\.(py|go|rs|cc|cpp|c|ts|js|tsx|jsx)"
    r"|[^/]+\.(test|spec)\.(ts|js|tsx|jsx|mjs|cjs)|[^/]*Tests?\.(java|kt|cs|swift|php)"
    r"|gtest_[^/]+\.(cpp|cc)|[^/]+-test\.(cpp|cc))$"  # `-test.cpp`
)
TEST_PATH_PATTERN = f"(?:{_TEST_DIR_RE.pattern}|{_TEST_FILE_RE.pattern})"


def is_test_path(path: str) -> bool:
    """A path the repository's conventions mark as a test: a tests/ or spec/
    directory, or a test-named file. Fixture and data files under tests/
    count too — a changed fixture is still something to re-run against."""
    p = (path or "").strip().lstrip("./")
    if not p:
        return False
    return bool(_TEST_DIR_RE.search(p) or _TEST_FILE_RE.search(p))


def _stem(path: str) -> str:
    name = PurePosixPath(path).stem.lower()
    # 02345_foo_bar.sql -> foo_bar; test_foo -> foo; foo_test -> foo
    name = re.sub(r"^\d+_", "", name)
    name = re.sub(r"^test_?", "", name)
    name = re.sub(r"_?tests?$", "", name)
    name = re.sub(r"\.(test|spec)$", "", name)
    return name


def stem_partners(test_path: str, src_paths: list[str]) -> list[str]:
    """Source files among `src_paths` whose stem the test's stem contains,
    the same rule the test-map worker uses (`src/**/tests/` name-stem match).
    Stems under 3 characters never match: 'io' is inside everything."""
    t = _stem(test_path)
    out = []
    for s in src_paths:
        ss = _stem(s)
        if len(ss) >= 3 and ss in t:
            out.append(s)
    return out


# ---- run recipes -----------------------------------------------------------
# (regex over the repo-relative path, template). Templates may use {path},
# {dir}, {stem}, {name} (file name without extension), {parent} (name of the
# containing directory) and {g1}.. for the pattern's capture groups.
# Built-ins cover pytest, ClickHouse's stateless and
# integration suites, Go and Rust; LOOM_CODE_TEST_GATE_RUN_TEMPLATES (a JSON
# list of {"pattern": ..., "run": ...}) is consulted FIRST so a deployment
# can name its own harness. A path no recipe matches is run_unknown.
_BUILTIN_RECIPES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"^tests/queries/(0_stateless|1_stateful)/\d+_[^/]+\.(sql|sh|py|j2)$"),
        "tests/clickhouse-test {name}",
    ),
    (re.compile(r"^tests/integration/(test_[^/]+)/"), "pytest tests/integration/{g1}"),
    (re.compile(r"(^|/)test_[^/]+\.py$"), "pytest {path}"),
    (re.compile(r"(^|/)[^/]+_test\.py$"), "pytest {path}"),
    (re.compile(r"(^|/)[^/]+_test\.go$"), "go test ./{dir}/"),
    (re.compile(r"(^|/)[^/]+\.(test|spec)\.(ts|js|tsx|jsx|mjs)$"), "npx vitest run {path}"),
    (re.compile(r"(^|/)[^/]+_test\.rs$"), "cargo test --test {stem}"),
]


def _compile_overrides(raw: str) -> list[tuple[re.Pattern[str], str]]:
    if not (raw or "").strip():
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    out: list[tuple[re.Pattern[str], str]] = []
    for it in items if isinstance(items, list) else []:
        try:
            out.append((re.compile(str(it["pattern"])), str(it["run"])))
        except (KeyError, TypeError, re.error):
            continue
    return out


def run_command_for(test_path: str, overrides_json: str = "") -> tuple[str, bool]:
    """(run, run_unknown). `run` is '' when no recipe matches; then
    run_unknown is True and the row says so instead of guessing."""
    p = (test_path or "").strip().lstrip("./")
    pp = PurePosixPath(p)
    ctx = {
        "path": p,
        "dir": str(pp.parent) if str(pp.parent) != "." else ".",
        "stem": pp.stem,
        "name": pp.name.rsplit(".", 1)[0],
        "parent": pp.parent.name,
    }
    for rx, tpl in [*_compile_overrides(overrides_json), *_BUILTIN_RECIPES]:
        m = rx.search(p)
        if m:
            groups = {f"g{i}": g or "" for i, g in enumerate(m.groups(), start=1)}
            try:
                return tpl.format(**ctx, **groups), False
            except (KeyError, IndexError):
                continue
    return "", True


# ---- assembly ----------------------------------------------------------------


@dataclass
class GateInputs:
    changed_paths: list[str]
    # definitions the change touches: {symbol_id, qualified_name, path, start_line}
    touched: list[dict[str, Any]]
    # reverse-edge reach: {symbol_id: {qualified_name, path, hop, roots, [via]}} where
    # roots is the touched symbol_ids this node reaches
    impacted: dict[str, dict[str, Any]]
    # code_test_map rows for the change's source paths
    test_map: list[dict[str, Any]]
    # {path, callees}: test files calling other definitions of the edited files, most
    # first; `NEIGHBOR_FILL` of them are enough
    neighbors: list[dict[str, Any]] = field(default_factory=list)
    # touched symbol_id -> {executed, commit}: what a verified coverage run measured
    measured: dict[str, dict[str, Any]] = field(default_factory=dict)
    # {test_path, roots}: tests a verified coverage run measured executing touched symbols
    coverage: list[dict[str, Any]] = field(default_factory=list)
    run_overrides_json: str = ""
    limit: int = 30


@dataclass
class Gate:
    tests: list[dict[str, Any]] = field(default_factory=list)
    untested: list[dict[str, Any]] = field(default_factory=list)
    unlinked: list[dict[str, Any]] = field(default_factory=list)
    tests_total: int = 0
    untested_total: int = 0
    unlinked_total: int = 0
    # touched source definitions no verified coverage run measured
    unmeasured: int = 0

    @property
    def obligations(self) -> bool:
        return bool(self.tests_total or self.untested_total)


def _links_to(roots: set[str], names: dict[str, str]) -> list[str]:
    return sorted(names[r] for r in roots if r in names)[:_LINKS_SHOWN]


def _static_files(inp: GateInputs) -> tuple[list[dict[str, Any]], set[str]]:
    """(per test file a call chain reaches the change from: its nearest test node, the
    touched symbol ids its tests link to and how many of its tests do; the touched symbol
    ids any test links to)."""
    files: dict[str, dict[str, Any]] = {}
    linked: set[str] = set()
    # Nearest hop first; within a hop the walk's own order stands (the surest edge first),
    # so the test a file's row names is the one the index is surest of.
    nodes = sorted(
        (v for v in inp.impacted.values() if is_test_path(str(v.get("path") or ""))),
        key=lambda v: int(v.get("hop") or 0),
    )
    for v in nodes:
        roots = set(v.get("roots") or ())
        linked |= roots
        f = files.setdefault(str(v["path"]), {"node": v, "roots": set(), "callers": 0})
        f["roots"] |= roots
        f["callers"] += 1
    return [{"path": p, **f} for p, f in files.items()], linked


def _test_rows(inp: GateInputs, names: dict[str, str]) -> tuple[list[dict[str, Any]], set[str]]:
    """(one row per test file under its strongest evidence, the touched symbol ids a
    static call chain links a test to)."""
    rows: dict[str, dict[str, Any]] = {}
    rank: dict[str, tuple[int, int]] = {}  # test_path -> (touched definitions linked, tests)

    def add(test_path: str, why: str, **extra: Any) -> bool:
        if not test_path or test_path in rows:
            return False
        run, unknown = run_command_for(test_path, inp.run_overrides_json)
        rows[test_path] = {
            "test_path": test_path,
            "why": why,
            "run": run,
            "run_unknown": unknown,
            **extra,
        }
        return True

    # 1. a changed test file is an obligation on its own evidence
    for p in inp.changed_paths:
        if is_test_path(p):
            add(p, CHANGED_TEST, evidence="in the diff")
    # 2. tests a coverage run measured executing a touched definition
    for c in inp.coverage:
        tp, roots = str(c.get("test_path") or ""), set(c.get("roots") or ())
        links = _links_to(roots, names)
        evidence = f"a coverage run measured it executing {', '.join(links[:3])}"
        if add(tp, COVERAGE, evidence=evidence, links_to=links):
            rank[tp] = (len(roots), 0)
    # 3. tests a call chain reaches a touched definition from
    static, linked = _static_files(inp)
    for f in static:
        v, hop = f["node"], int(f["node"].get("hop") or 0)
        added = add(
            f["path"],
            STATIC_CALL,
            evidence=f"{v.get('qualified_name', '')} reaches the change in {hop} hop(s)",
            hop=hop,
            callers=int(f["callers"]),
            links_to=_links_to(f["roots"], names),
            **({"via": str(v["via"])} if v.get("via") else {}),
        )
        if added:
            rank[f["path"]] = (len(f["roots"]), int(f["callers"]))
    # 4. stem partners of changed source files, by the repository's own naming
    src_changed = [p for p in inp.changed_paths if not is_test_path(p)]
    for t in inp.test_map:
        tp = str(t.get("test_path") or "")
        partners = stem_partners(tp, src_changed)
        if partners:
            add(tp, STEM_PARTNER, evidence=f"stem of {partners[0]}", src_path=partners[0])
    # 5. the mined test map, most confident first
    for t in sorted(inp.test_map, key=lambda r: -float(r.get("confidence") or 0.0)):
        add(
            str(t.get("test_path") or ""),
            TEST_MAP,
            evidence=f"test map, confidence {float(t.get('confidence') or 0.0):.2f}",
            src_path=str(t.get("src_path") or ""),
            confidence=round(float(t.get("confidence") or 0.0), 3),
        )
    # 6. tests that call other definitions of the edited files, while the list is short:
    # about the file, not about any touched definition
    for n in inp.neighbors:
        if len(rows) >= NEIGHBOR_FILL:
            break
        callees = int(n.get("callees") or 0)
        add(
            str(n.get("path") or ""),
            FILE_NEIGHBOR,
            evidence=f"calls {callees} other definition(s) of the edited files",
            callees=callees,
        )
    # Within a kind: the nearest hop, then the file that links to more of the touched
    # definitions, then the one with more tests doing so: a module's own test file calls it
    # from many tests, a bystander from one.
    ordered = sorted(
        rows.values(),
        key=lambda r: (
            _ORDER[r["why"]],
            int(r.get("hop") or 0),
            -rank.get(r["test_path"], (0, 0))[0],
            -rank.get(r["test_path"], (0, 0))[1],
            -int(r.get("callees") or 0),
            -float(r.get("confidence") or 0.0),
            r["test_path"],
        ),
    )
    return ordered, linked


def build_gate(inp: GateInputs) -> Gate:
    names = {str(s.get("symbol_id") or ""): str(s.get("qualified_name") or "") for s in inp.touched}
    ordered, linked = _test_rows(inp, names)

    # A touched source definition is `untested` only when a coverage run measured it
    # unexecuted, and `unlinked` when no run measured it and no call chain links a test to
    # it. File-level rows (a stem partner, the test map, a neighbour) say nothing about one
    # definition.
    untested: list[dict[str, Any]] = []
    unlinked: list[dict[str, Any]] = []
    unmeasured = 0
    for s in inp.touched:
        path = str(s.get("path") or "")
        if is_test_path(path):
            continue
        sid = str(s.get("symbol_id") or "")
        row = {
            "qualified_name": str(s.get("qualified_name") or ""),
            "path": path,
            "start_line": int(s.get("start_line") or 0),
        }
        run = inp.measured.get(sid)
        if run is None:
            unmeasured += 1
            if sid not in linked:
                unlinked.append(row)
        elif not run.get("executed"):
            untested.append({**row, "commit": str(run.get("commit") or "")})
    untested.sort(key=lambda r: (r["path"], r["start_line"]))
    unlinked.sort(key=lambda r: (r["path"], r["start_line"]))

    lim = max(1, int(inp.limit or 30))
    return Gate(
        tests=ordered[:lim],
        untested=untested[:lim],
        unlinked=unlinked[:lim],
        tests_total=len(ordered),
        untested_total=len(untested),
        unlinked_total=len(unlinked),
        unmeasured=unmeasured,
    )
