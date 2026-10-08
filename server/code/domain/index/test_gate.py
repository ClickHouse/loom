"""Pure helpers for code.test_gate: the pre-PR obligation list.

A change carries two obligations before "done": the tests that reach it
must run, and the parts of its blast radius that NO test reaches must be
named (so the author writes one, or says why not). This module turns the
raw evidence — the diff's own files, the test map, the reverse-edge walk —
into rows ordered by the STRENGTH of their evidence, each saying why it is
there and how to run it, and into the untested complement.

Why evidence order and not confidence order: a test file that is itself in the diff is
an obligation on its own evidence (you edited it, run it); a stem partner (`foo.py` →
`test_foo.py`) is the repository's own naming convention; a test-map row is mined
history; a graph hop is a call chain. Naming the tests before the agent declares done is
what cuts agent-caused regressions.

Run commands: a row carries a runnable `run` when the harness is derivable
from the path, else `run_unknown=True` — never a guessed suite command.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

# Evidence classes, strongest first. The integer is the sort key and rides
# the wire as `why` text; keep the order stable — the plugin renders it.
CHANGED_TEST = "changed_test"
STEM_PARTNER = "stem_partner"
TEST_MAP = "test_map"
GRAPH_HOP = "graph_hop"
_ORDER = {CHANGED_TEST: 0, STEM_PARTNER: 1, TEST_MAP: 2, GRAPH_HOP: 3}

_TEST_DIR_RE = re.compile(r"(^|/)(tests?|__tests__|spec|specs)(/|$)")
_TEST_FILE_RE = re.compile(
    r"(^|/)(test_[^/]+\.(py|sh|sql|rb|go)|[^/]+_test\.(py|go|rs|cc|cpp|c|ts|js|tsx|jsx)"
    r"|[^/]+\.(test|spec)\.(ts|js|tsx|jsx|mjs|cjs)|[^/]*Tests?\.(java|kt|cs|swift|php)"
    r"|gtest_[^/]+\.(cpp|cc)|[^/]+-test\.(cpp|cc))$"  # `-test.cpp`
)


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
    # symbols the change touches: {symbol_id, qualified_name, path}
    touched: list[dict[str, Any]]
    # reverse-edge reach: {symbol_id: {qualified_name, path, hop, roots}} where
    # roots is the set of touched symbol_ids this node is reachable from
    impacted: dict[str, dict[str, Any]]
    # code_test_map rows for the change's source paths
    test_map: list[dict[str, Any]]
    run_overrides_json: str = ""
    limit: int = 30


@dataclass
class Gate:
    tests: list[dict[str, Any]] = field(default_factory=list)
    untested: list[dict[str, Any]] = field(default_factory=list)
    tests_total: int = 0
    untested_total: int = 0

    @property
    def obligations(self) -> bool:
        return bool(self.tests_total or self.untested_total)


def build_gate(inp: GateInputs) -> Gate:
    src_changed = [p for p in inp.changed_paths if not is_test_path(p)]
    rows: dict[str, dict[str, Any]] = {}  # test_path -> row (first = strongest)

    def add(test_path: str, why: str, **extra: Any) -> None:
        if not test_path or test_path in rows:
            return
        run, unknown = run_command_for(test_path, inp.run_overrides_json)
        rows[test_path] = {
            "test_path": test_path,
            "why": why,
            "run": run,
            "run_unknown": unknown,
            **extra,
        }

    # 1. a changed test file is an obligation on its own evidence
    for p in inp.changed_paths:
        if is_test_path(p):
            add(p, CHANGED_TEST, evidence="in the diff")
    # 2. stem partners of changed source files, by the repo's own naming
    for t in inp.test_map:
        tp = str(t.get("test_path") or "")
        partners = stem_partners(tp, src_changed)
        if partners:
            add(tp, STEM_PARTNER, evidence=f"stem of {partners[0]}", src_path=partners[0])
    # 3. the mined test map, confidence-ordered
    for t in sorted(inp.test_map, key=lambda r: -float(r.get("confidence") or 0.0)):
        add(
            str(t.get("test_path") or ""),
            TEST_MAP,
            evidence=f"test map, confidence {float(t.get('confidence') or 0.0):.2f}",
            src_path=str(t.get("src_path") or ""),
            confidence=round(float(t.get("confidence") or 0.0), 3),
        )
    # 4. tests reached through the call graph, nearest hop first
    graph_tested_roots: set[str] = set()
    test_nodes = sorted(
        (v for v in inp.impacted.values() if is_test_path(str(v.get("path") or ""))),
        key=lambda v: (int(v.get("hop") or 0), str(v.get("path") or "")),
    )
    for v in test_nodes:
        graph_tested_roots |= set(v.get("roots") or ())
        qn, hop = v.get("qualified_name", ""), v.get("hop", "?")
        add(
            str(v["path"]),
            GRAPH_HOP,
            evidence=f"{qn} reaches the change in {hop} hop(s)",
            hop=int(v.get("hop") or 0),
        )

    ordered = sorted(
        rows.values(),
        key=lambda r: (
            _ORDER[r["why"]],
            -float(r.get("confidence") or 0.0),
            int(r.get("hop") or 0),
            r["test_path"],
        ),
    )

    # Untested complement: touched source symbols no test reaches by any
    # evidence class — not via the test map (its path has no row), not via
    # the graph (no test node has it as a root).
    mapped_src = {str(t.get("src_path") or "") for t in inp.test_map}
    stem_covered = {
        str(r.get("src_path")) for r in ordered if r["why"] == STEM_PARTNER and r.get("src_path")
    }
    untested: list[dict[str, Any]] = []
    for s in inp.touched:
        path = str(s.get("path") or "")
        if is_test_path(path):
            continue
        sid = str(s.get("symbol_id") or "")
        if path in mapped_src or path in stem_covered or sid in graph_tested_roots:
            continue
        untested.append(
            {
                "qualified_name": str(s.get("qualified_name") or ""),
                "path": path,
                "start_line": int(s.get("start_line") or 0),
            }
        )
    untested.sort(key=lambda r: (r["path"], r["start_line"]))

    lim = max(1, int(inp.limit or 30))
    return Gate(
        tests=ordered[:lim],
        untested=untested[:lim],
        tests_total=len(ordered),
        untested_total=len(untested),
    )
