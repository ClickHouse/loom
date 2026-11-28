"""code.test_gate — the pre-PR obligation list: pure assembly rules and the
handler over a fake graph (no ClickHouse)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from server.code.domain.index import test_gate as tg
from server.core.errors import ValidationError
from server.settings import settings

# ---- pure helpers --------------------------------------------------------------


@pytest.mark.parametrize(
    "path, expected",
    [
        ("tests/queries/0_stateless/02345_foo.sql", True),
        ("tests/integration/test_merge/test.py", True),
        ("src/Storages/MergeTree/tests/gtest_parts.cpp", True),
        ("server/memory/app/test_foo.py", True),
        ("pkg/store_test.go", True),
        ("web/app.spec.ts", True),
        ("src/Storages/MergeTree/MergeTreeData.cpp", False),
        ("server/code/app/symbol/symbol.py", False),
        ("docs/testing.md", False),
        ("", False),
    ],
)
def test_is_test_path(path, expected) -> None:
    assert tg.is_test_path(path) is expected


@pytest.mark.parametrize(
    "path, run, unknown",
    [
        (
            "tests/queries/0_stateless/02345_foo_bar.sql",
            "tests/clickhouse-test 02345_foo_bar",
            False,
        ),
        ("tests/queries/0_stateless/02345_foo.sh", "tests/clickhouse-test 02345_foo", False),
        (
            "tests/integration/test_merge_tree/test.py",
            "pytest tests/integration/test_merge_tree",
            False,
        ),
        ("tests/integration/test_x/configs/a.xml", "pytest tests/integration/test_x", False),
        ("tests/unit/test_memory_ask.py", "pytest tests/unit/test_memory_ask.py", False),
        ("pkg/store/store_test.go", "go test ./pkg/store/", False),
        ("web/app.spec.ts", "npx vitest run web/app.spec.ts", False),
        ("src/Storages/MergeTree/tests/gtest_parts.cpp", "", True),
        ("tests/fixtures/data.json", "", True),
    ],
)
def test_run_recipes(path, run, unknown) -> None:
    assert tg.run_command_for(path) == (run, unknown)


def test_run_overrides_win_and_bad_json_is_ignored() -> None:
    ov = '[{"pattern": "gtest_[^/]+\\\\.cpp$", "run": "unit_tests_dbms --gtest_filter={name}*"}]'
    assert tg.run_command_for("src/x/tests/gtest_parts.cpp", ov) == (
        "unit_tests_dbms --gtest_filter=gtest_parts*",
        False,
    )
    assert tg.run_command_for("tests/unit/test_a.py", "not json") == (
        "pytest tests/unit/test_a.py",
        False,
    )
    assert tg.run_command_for("tests/unit/test_a.py", '[{"pattern": "("}]') == (
        "pytest tests/unit/test_a.py",
        False,
    )


def test_stem_partners_follow_the_worker_rule() -> None:
    srcs = ["src/Storages/MergeTree/MergeTreeData.cpp", "src/IO/io.cpp", "server/foo.py"]
    assert tg.stem_partners("src/Storages/MergeTree/tests/gtest_merge_tree_data.cpp", srcs) == []
    assert tg.stem_partners("src/Storages/tests/gtest_mergetreedata.cpp", srcs) == [srcs[0]]
    assert tg.stem_partners("tests/unit/test_foo.py", srcs) == ["server/foo.py"]
    # short stems never match: "io" is inside everything
    assert tg.stem_partners("tests/unit/test_io.py", srcs) == []


def _touched(sid: str, path: str, name: str = "f") -> dict[str, Any]:
    return {"symbol_id": sid, "qualified_name": name, "path": path, "start_line": 1}


def _node(name: str, path: str, hop: int, roots: list[str], **extra: Any) -> dict[str, Any]:
    return {"qualified_name": name, "path": path, "hop": hop, "roots": roots, **extra}


def test_build_gate_orders_by_evidence_and_dedupes() -> None:
    inp = tg.GateInputs(
        changed_paths=["src/alpha.py", "tests/unit/test_zeta.py", "src/b.py"],
        touched=[_touched("s1", "src/alpha.py", "alpha"), _touched("s2", "src/b.py", "b")],
        impacted={
            "t1": _node("test_b_hop", "tests/unit/test_hop.py", 2, ["s2"]),
            "t2": _node("test_m", "tests/unit/test_mapped.py", 1, ["s1"]),
            "c1": _node("caller", "src/c.py", 1, ["s1"]),
        },
        test_map=[
            {"test_path": "tests/unit/test_mapped.py", "src_path": "src/b.py", "confidence": 0.4},
            {
                "test_path": "tests/unit/test_alpha.py",
                "src_path": "src/alpha.py",
                "confidence": 0.7,
            },
            {"test_path": "tests/unit/test_zeta.py", "src_path": "src/alpha.py", "confidence": 0.9},
        ],
        neighbors=[
            {"path": "tests/unit/test_near.py", "callees": 3},
            {"path": "tests/unit/test_hop.py", "callees": 9},
        ],
        measured={"s1": {"executed": True, "commit": "c1"}},
        coverage=[{"test_path": "tests/unit/test_ran.py", "roots": ["s1"]}],
    )
    g = tg.build_gate(inp)
    # Five rows are about the change, so no file neighbour is listed.
    assert [(r["test_path"], r["why"]) for r in g.tests] == [
        ("tests/unit/test_zeta.py", "changed_test"),
        ("tests/unit/test_ran.py", "coverage"),
        ("tests/unit/test_mapped.py", "static_call"),
        ("tests/unit/test_hop.py", "static_call"),
        ("tests/unit/test_alpha.py", "stem_partner"),
    ]
    # A test file keeps its first (strongest) evidence: the changed test is also in the
    # map, and a test a call chain links names its definition, whatever the map says of it.
    assert g.tests[0]["evidence"] == "in the diff"
    assert g.tests[1]["links_to"] == ["alpha"]
    assert g.tests[2]["links_to"] == ["alpha"] and "src_path" not in g.tests[2]
    hop = g.tests[3]
    assert hop["hop"] == 2 and hop["evidence"] == "test_b_hop reaches the change in 2 hop(s)"
    assert hop["links_to"] == ["b"] and hop["callers"] == 1 and "confidence" not in hop
    assert hop["run"] == "pytest tests/unit/test_hop.py" and hop["run_unknown"] is False
    assert g.tests[4]["src_path"] == "src/alpha.py" and "confidence" not in g.tests[4]
    assert g.tests_total == 5 and g.untested == [] and g.unlinked == []
    assert g.obligations is True


def test_a_map_row_follows_the_call_chain_rows_and_its_stem_partner() -> None:
    g = tg.build_gate(
        tg.GateInputs(
            changed_paths=["src/alpha.py"],
            touched=[_touched("s1", "src/alpha.py", "alpha")],
            impacted={"t": _node("test_x", "tests/unit/test_chain.py", 1, ["s1"])},
            test_map=[
                {
                    "test_path": "tests/unit/test_low.py",
                    "src_path": "src/alpha.py",
                    "confidence": 0.2,
                },
                {
                    "test_path": "tests/unit/test_high.py",
                    "src_path": "src/alpha.py",
                    "confidence": 0.9,
                },
                {
                    "test_path": "tests/unit/test_alpha.py",
                    "src_path": "src/alpha.py",
                    "confidence": 0.1,
                },
            ],
        )
    )
    assert [(r["test_path"], r["why"]) for r in g.tests] == [
        ("tests/unit/test_chain.py", "static_call"),
        ("tests/unit/test_alpha.py", "stem_partner"),
        ("tests/unit/test_high.py", "test_map"),
        ("tests/unit/test_low.py", "test_map"),
    ]
    assert g.tests[2]["confidence"] == 0.9


def test_file_neighbours_fill_a_short_list_and_take_no_rows_place() -> None:
    """Thirty neighbour files beside one test-map row: the map row is listed, and the
    neighbours fill the list to four, whatever the page limit."""
    neighbors = [{"path": f"tests/test_n{i:02}.py", "callees": 30 - i} for i in range(30)]
    mapped = {"test_path": "tests/test_history.py", "src_path": "src/a.py", "confidence": 0.3}

    def gate(**kw: Any) -> tg.Gate:
        return tg.build_gate(
            tg.GateInputs(
                changed_paths=["src/a.py"],
                touched=[_touched("s1", "src/a.py", "a")],
                impacted=kw.get("impacted", {}),
                test_map=kw.get("test_map", []),
                neighbors=neighbors,
                limit=30,
            )
        )

    g = gate(test_map=[mapped])
    assert [(r["test_path"], r["why"]) for r in g.tests] == [
        ("tests/test_history.py", "test_map"),
        ("tests/test_n00.py", "file_neighbor"),
        ("tests/test_n01.py", "file_neighbor"),
        ("tests/test_n02.py", "file_neighbor"),
    ]
    assert g.tests_total == 4 and g.tests[1]["callees"] == 30
    # With nothing else known the four neighbours that call the most of the file are the lead.
    assert [r["test_path"] for r in gate().tests] == [f"tests/test_n0{i}.py" for i in range(4)]
    # Four tests a call chain links leave room for none.
    linked = {f"t{i}": _node(f"test_{i}", f"tests/test_linked_{i}.py", 1, ["s1"]) for i in range(4)}
    assert {r["why"] for r in gate(impacted=linked).tests} == {"static_call"}


def test_static_rows_put_the_file_with_more_links_and_more_tests_first() -> None:
    """Within a hop, the file whose tests link to more of the touched definitions is first,
    then the one with more tests doing so: a module's own test file before a bystander."""
    inp = tg.GateInputs(
        changed_paths=["src/a.py"],
        touched=[_touched("s1", "src/a.py", "m::A::__init__"), _touched("s2", "src/a.py", "m::f")],
        impacted={
            "a1": _node("tests.test_a::test_one", "tests/test_a.py", 1, ["s1"], via="constructor"),
            "a2": _node("tests.test_a::test_two", "tests/test_a.py", 1, ["s1"]),
            "b1": _node("tests.test_b::test_b", "tests/test_b.py", 1, ["s1", "s2"]),
            "0": _node("tests.aaa::test_one_call", "tests/aaa.py", 1, ["s1"]),
            "far": _node("tests.test_far::test_far", "tests/test_far.py", 2, ["s1", "s2"]),
        },
        test_map=[],
    )
    g = tg.build_gate(inp)
    assert [r["test_path"] for r in g.tests] == [
        "tests/test_b.py",  # links to both touched definitions
        "tests/test_a.py",  # two tests link to one
        "tests/aaa.py",  # one test links to one
        "tests/test_far.py",  # a hop further, whatever it links to
    ]
    by_path = {r["test_path"]: r for r in g.tests}
    assert by_path["tests/test_b.py"]["links_to"] == ["m::A::__init__", "m::f"]
    assert by_path["tests/test_a.py"]["callers"] == 2
    assert by_path["tests/test_a.py"]["via"] == "constructor"
    assert "via" not in by_path["tests/test_b.py"]


def test_untested_needs_a_measurement_and_unlinked_needs_no_link() -> None:
    inp = tg.GateInputs(
        changed_paths=["src/a.py", "src/b.py", "src/c.py", "src/d.py", "src/e.py"],
        touched=[
            _touched("s1", "src/a.py", "a"),
            _touched("s2", "src/b.py", "b"),
            _touched("s3", "src/c.py", "c"),
            _touched("s4", "src/d.py", "d"),
            _touched("s5", "src/e.py", "e"),
        ],
        impacted={
            # a test reaches s3 through the graph in two hops, and s5 in one
            "t": _node("test_c", "tests/x/test_other.py", 2, ["s3", "s5"]),
        },
        test_map=[
            {"test_path": "tests/unit/test_a.py", "src_path": "src/a.py", "confidence": 0.7},
        ],
        neighbors=[{"path": "tests/unit/test_b_neighbor.py", "callees": 4}],
        measured={
            "s4": {"executed": True, "commit": "abc"},
            "s5": {"executed": False, "commit": "abc"},
        },
    )
    g = tg.build_gate(inp)
    # s1 has only a test-map row and s2 only a file neighbour: both are about the file.
    # s3 is linked by a call chain. s4 was measured executed. s5 was measured unexecuted,
    # whatever the graph says.
    assert [u["qualified_name"] for u in g.unlinked] == ["a", "b"]
    assert g.untested == [
        {"qualified_name": "e", "path": "src/e.py", "start_line": 1, "commit": "abc"}
    ]
    assert (g.unlinked_total, g.untested_total, g.unmeasured) == (2, 1, 3)
    assert {r["why"] for r in g.tests} == {"static_call", "file_neighbor", "test_map"}


def test_unlinked_alone_is_no_obligation() -> None:
    g = tg.build_gate(
        tg.GateInputs(
            changed_paths=["src/a.py"],
            touched=[_touched("s1", "src/a.py", "a")],
            impacted={},
            test_map=[],
        )
    )
    assert [u["qualified_name"] for u in g.unlinked] == ["a"]
    assert g.tests == [] and g.untested == [] and g.obligations is False


def test_gate_caps_and_reports_totals() -> None:
    inp = tg.GateInputs(
        changed_paths=["src/a.py"],
        touched=[_touched(f"s{i}", f"src/f{i}.py") for i in range(5)],
        impacted={},
        test_map=[
            {"test_path": f"tests/unit/test_{i}.py", "src_path": "src/a.py", "confidence": 0.5}
            for i in range(4)
        ],
        measured={
            "s0": {"executed": False, "commit": "c"},
            "s1": {"executed": False, "commit": "c"},
        },
        limit=1,
    )
    g = tg.build_gate(inp)
    assert len(g.tests) == 1 and g.tests_total == 4
    assert len(g.untested) == 1 and g.untested_total == 2
    assert len(g.unlinked) == 1 and g.unlinked_total == 3


def test_no_change_no_obligations() -> None:
    g = tg.build_gate(tg.GateInputs(changed_paths=[], touched=[], impacted={}, test_map=[]))
    assert g.obligations is False and g.tests == [] and g.untested == [] and g.unlinked == []


# ---- handler over a fake repo ----------------------------------------------------


def _sym(
    sid: str, name: str, path: str, start: int, end: int, kind: str = "function"
) -> dict[str, Any]:
    return {
        "symbol_id": sid,
        "qualified_name": name,
        "name": name.split("::")[-1],
        "kind": kind,
        "path": path,
        "start_line": start,
        "end_line": end,
        "tier": "scip",
        "signature": "",
        "doc_comment": "",
    }


def _edge(frm: str, to: str, fq: str, path: str, kind: str = "calls") -> dict[str, Any]:
    return {
        "from_symbol_id": frm,
        "to_symbol_id": to,
        "from_qualified_name": fq,
        "to_qualified_name": "",
        "path": path,
        "line": 5,
        "tier": "scip",
        "edge_kind": kind,
        "confidence": 1.0,
    }


class _Repo:
    def __init__(self) -> None:
        self.symbols = [
            _sym("sA", "DB::A::f", "src/A.cpp", 10, 20),
            _sym("sB", "DB::A::g", "src/A.cpp", 40, 60),  # not in the hunks
        ]
        self.callers = {
            "sA": [_edge("sX", "sA", "DB::X::x", "src/X.cpp")],
            "sX": [_edge("tT", "sX", "TEST_F(Parts, x)", "src/tests/gtest_parts.cpp")],
        }
        self.neighbors: list[dict[str, Any]] = []
        self.neighbor_ids: list[str] = []
        self.neighbor_definitions: dict[str, str] = {}
        self.coverage_files: dict[str, dict[str, Any]] = {}
        self.coverage_rows: list[dict[str, Any]] = []
        self.kinds: tuple[str, ...] = ()

    async def functions_for_paths(self, org, ns, paths, *, kinds=(), with_revision=False, **_kw):
        self.kinds = self.kinds or tuple(kinds)
        rows = [s for s in self.symbols if s["path"] in paths and s["kind"] in kinds]
        return [{**s, "extent_commit": "c" * 40} for s in rows] if with_revision else rows

    async def edges_for_symbols(
        self, org, ns, ids, *, direction, edge_kinds=None, limit=200, min_confidence=0.0
    ):
        assert direction == "callers"
        return [e for sid in ids for e in self.callers.get(sid, [])]

    async def test_files_calling(self, org, ns, definitions, *, limit):
        self.neighbor_ids = list(definitions)
        self.neighbor_definitions = dict(definitions)
        return self.neighbors[:limit]

    async def test_map_for(self, org, ns, paths, *, limit=30):
        return [
            {
                "test_path": "tests/queries/0_stateless/01234_a.sql",
                "src_path": "src/A.cpp",
                "confidence": 0.8,
            }
        ]

    async def coverage_files_for(self, org, ns, paths):
        return {p: f for p, f in self.coverage_files.items() if p in paths}

    async def coverage_rows_for(self, org, ns, paths, *, limit=2000):
        return [r for r in self.coverage_rows if r["src_path"] in paths]

    async def get_state(self, org, ns, key):
        return "c" * 40

    async def file_rows_for_paths(self, org, ns, paths):
        return {p: {"indexed_commit": "c" * 40} for p in paths}


@pytest.fixture
def intel(monkeypatch: pytest.MonkeyPatch):
    import server.code.app.symbol.gate as h

    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
    return h


_DIFF = """diff --git a/src/A.cpp b/src/A.cpp
--- a/src/A.cpp
+++ b/src/A.cpp
@@ -12,3 +12,4 @@ void DB::A::f()
+  x();
"""


async def _gate(intel, repo: _Repo, **kw: Any):
    from server.code.api.models_testing import CodeTestGateInput

    with patch.object(intel, "CodeRepo", return_value=repo):
        return await intel.handle_code_test_gate(CodeTestGateInput(org="o", namespace="n", **kw))


async def test_handler_diff_shape_walks_roots_to_tests(intel) -> None:
    repo = _Repo()
    repo.neighbors = [{"path": "src/tests/gtest_g.cpp", "callees": 1}]
    out = await _gate(intel, repo, diff=_DIFF, depth=2)
    assert out.changed_files == ["src/A.cpp"]
    # the added line sits in f (lines 10-20), not in g (40-60)
    assert [t["qualified_name"] for t in out.touched_symbols] == ["DB::A::f"]
    assert "symbol_id" not in out.touched_symbols[0]
    assert repo.kinds == ("function", "method", "fixture", "class")
    # the neighbours are asked for the file's other definitions only
    assert repo.neighbor_ids == ["sB"]
    assert [t["why"] for t in out.tests] == ["static_call", "test_map", "file_neighbor"]
    assert out.tests[0]["test_path"] == "src/tests/gtest_parts.cpp"
    assert out.tests[0]["hop"] == 2 and out.tests[0]["run_unknown"] is True
    assert out.tests[0]["links_to"] == ["DB::A::f"]
    assert out.tests[1]["run"] == "tests/clickhouse-test 01234_a"
    # a call chain links a test to f; nothing was measured
    assert out.untested == [] and out.unlinked == [] and out.obligations is True
    assert out.tests_total == 3 and out.counts_floor is True
    assert out.epistemic == "lower_bound" and "no_coverage" in out.causes


async def test_handler_files_shape_lists_unlinked_functions(intel) -> None:
    repo = _Repo()
    repo.symbols.append(_sym("cA", "DB::A", "src/A.cpp", 5, 70, kind="class"))
    repo.callers = {}  # no graph at all

    async def no_tests(org, ns, paths, *, limit=30):
        return []

    repo.test_map_for = no_tests  # type: ignore[method-assign]
    out = await _gate(intel, repo, files=["src/A.cpp"], depth=1)
    # a file given without hunks touches every function in it, and no class
    assert [t["qualified_name"] for t in out.touched_symbols] == ["DB::A::f", "DB::A::g"]
    assert out.tests == [] and out.untested == []
    assert [u["qualified_name"] for u in out.unlinked] == ["DB::A::f", "DB::A::g"]
    # the index cannot say, so nothing is asked for
    assert out.obligations is False and out.unlinked_total == 2


async def test_handler_requires_a_change(intel) -> None:
    from server.code.api.models_testing import CodeTestGateInput

    with patch.object(intel, "CodeRepo", return_value=_Repo()):
        with pytest.raises(ValidationError):
            await intel.handle_code_test_gate(CodeTestGateInput(org="o", namespace="n"))


async def test_a_test_only_the_parser_saw_credits_the_collapsed_definition(intel) -> None:
    """A definition both tiers hold arrives as its scip row carrying both ids (`tier_ids`);
    a test that calls it only through the parser-tier id still covers it."""
    repo = _Repo()
    repo.symbols = [{**_sym("sA", "DB::A::f", "src/A.cpp", 10, 20), "tier_ids": ["sA", "pA"]}]
    repo.callers = {"pA": [_edge("tT", "pA", "TEST_F(Parts, f)", "src/tests/gtest_parts.cpp")]}

    async def no_tests(org, ns, paths, *, limit=30):
        return []

    repo.test_map_for = no_tests  # type: ignore[method-assign]
    out = await _gate(intel, repo, diff=_DIFF, depth=1)
    assert [t["test_path"] for t in out.tests] == ["src/tests/gtest_parts.cpp"]
    assert out.unlinked == []


def _py_repo() -> _Repo:
    repo = _Repo()
    repo.symbols = [
        _sym("cW", "pkg.w::Widget", "pkg/w.py", 10, 40, kind="class"),
        _sym("mI", "pkg.w::Widget::__init__", "pkg/w.py", 14, 20),
        _sym("mR", "pkg.w::Widget::render", "pkg/w.py", 22, 40),
        _sym("fH", "pkg.w::helper", "pkg/w.py", 44, 50),
    ]
    repo.callers = {
        "cW": [_edge("t1", "cW", "tests.test_w::test_builds", "tests/test_w.py")],
        "fH": [_edge("t2", "fH", "tests.test_w::test_helper", "tests/test_w.py")],
    }

    async def no_tests(org, ns, paths, *, limit=30):
        return []

    repo.test_map_for = no_tests  # type: ignore[method-assign]
    return repo


def _one_line(path: str, line: int) -> str:
    return f"--- a/{path}\n+++ b/{path}\n@@ -{line} +{line} @@\n-old\n+new\n"


async def test_a_test_that_builds_the_object_links_to_its_constructor(intel) -> None:
    out = await _gate(intel, _py_repo(), diff=_one_line("pkg/w.py", 16))
    assert [t["qualified_name"] for t in out.touched_symbols] == ["pkg.w::Widget::__init__"]
    assert out.unlinked == []
    row = out.tests[0]
    assert (row["why"], row["via"], row["hop"]) == ("static_call", "constructor", 1)
    assert row["links_to"] == ["pkg.w::Widget::__init__"]
    assert row["evidence"] == "tests.test_w::test_builds reaches the change in 1 hop(s)"


async def test_a_nested_function_is_reached_through_the_function_it_is_defined_in(intel) -> None:
    """A closure has no caller of its own in a call graph: the tests that call the function
    it is defined in are its tests, and that function is not touched."""
    repo = _py_repo()
    repo.symbols.append(_sym("fN", "pkg.w::helper::inner", "pkg/w.py", 46, 48))
    out = await _gate(intel, repo, diff=_one_line("pkg/w.py", 47))
    assert [t["qualified_name"] for t in out.touched_symbols] == ["pkg.w::helper::inner"]
    row = out.tests[0]
    assert (row["why"], row["via"], row["hop"]) == ("static_call", "enclosing", 1)
    assert row["links_to"] == ["pkg.w::helper::inner"] and out.unlinked == []


async def test_a_line_of_a_class_body_touches_the_class_and_not_its_methods(intel) -> None:
    out = await _gate(intel, _py_repo(), diff=_one_line("pkg/w.py", 12))
    assert [t["qualified_name"] for t in out.touched_symbols] == ["pkg.w::Widget"]
    assert out.tests[0]["links_to"] == ["pkg.w::Widget"] and "via" not in out.tests[0]
    assert out.unlinked == []


async def test_a_method_edit_touches_the_method_only_and_a_neighbour_does_not_link_it(
    intel,
) -> None:
    repo = _py_repo()
    repo.neighbors = [{"path": "tests/test_w.py", "callees": 2}]
    out = await _gate(intel, repo, diff=_one_line("pkg/w.py", 30))
    assert [t["qualified_name"] for t in out.touched_symbols] == ["pkg.w::Widget::render"]
    assert [(t["test_path"], t["why"]) for t in out.tests] == [("tests/test_w.py", "file_neighbor")]
    # the file's test is named, and the method is still one the index links no test to
    assert [u["qualified_name"] for u in out.unlinked] == ["pkg.w::Widget::render"]
    assert out.obligations is True and out.untested == []


async def test_a_module_level_line_touches_nothing_and_says_so(intel) -> None:
    repo = _py_repo()
    repo.neighbors = [{"path": "tests/test_w.py", "callees": 3}]
    out = await _gate(intel, repo, diff=_one_line("pkg/w.py", 42))
    assert out.touched_symbols == [] and out.unlinked == []
    assert "unmapped_changes" in out.causes and out.epistemic == "lower_bound"
    # the file's tests are still the nearest lead
    assert [t["why"] for t in out.tests] == ["file_neighbor"]
    assert sorted(repo.neighbor_ids) == ["cW", "fH", "mI", "mR"]


async def test_a_coverage_run_decides_untested_and_makes_the_answer_measured(intel) -> None:
    repo = _py_repo()
    repo.coverage_files = {
        "pkg/w.py": {"commit_sha": "c" * 40, "lines": "[[14, 20], [44, 50]]", "tests": 2}
    }
    repo.coverage_rows = [
        {
            "src_path": "pkg/w.py",
            "test_path": "tests/test_w.py",
            "confidence": 0.9,
            "evidence": '{"commit": "%s", "lines": [[44, 50]]}' % ("c" * 40),
        }
    ]
    diff = _one_line("pkg/w.py", 30) + "@@ -46 +46 @@\n-old\n+new\n"
    out = await _gate(intel, repo, diff=diff)
    assert [t["qualified_name"] for t in out.touched_symbols] == [
        "pkg.w::Widget::render",
        "pkg.w::helper",
    ]
    # render (22-40) was measured and no test executed it; helper was executed
    assert out.untested == [
        {
            "qualified_name": "pkg.w::Widget::render",
            "path": "pkg/w.py",
            "start_line": 22,
            "commit": "c" * 40,
        }
    ]
    assert out.unlinked == [] and out.obligations is True
    assert (out.epistemic, out.causes) == ("measured", [])
    assert out.tests[0]["why"] == "coverage" and out.tests[0]["links_to"] == ["pkg.w::helper"]


async def test_a_one_line_compiler_row_is_measured_on_the_parser_rows_body(intel) -> None:
    """The compiler tier may hold a definition as its declaration line only. Whether that
    line ran says nothing about the body: the run is read against the parser row's extent,
    and a definition with no extent wider than a line is not measured."""
    repo = _py_repo()
    repo.symbols = [
        _sym("fH", "pkg.w::helper", "pkg/w.py", 44, 44),
        {**_sym("pH", "pkg.w::helper", "pkg/w.py", 44, 50), "tier": "treesitter"},
        _sym("fS", "pkg.w::stub", "pkg/w.py", 60, 60),
    ]
    # the body of helper ran (46-48), its declaration line did not; line 60 ran
    repo.coverage_files = {
        "pkg/w.py": {"commit_sha": "c" * 40, "lines": "[[46, 48], [60, 60]]", "tests": 1}
    }
    out = await _gate(intel, repo, diff=_one_line("pkg/w.py", 47))
    assert [t["qualified_name"] for t in out.touched_symbols] == ["pkg.w::helper"]
    assert out.untested == [] and out.epistemic == "measured"

    out = await _gate(intel, repo, diff=_one_line("pkg/w.py", 60))
    assert [t["qualified_name"] for t in out.touched_symbols] == ["pkg.w::stub"]
    assert out.untested == [] and out.epistemic == "lower_bound"
    assert "no_coverage" in out.causes


async def test_measured_needs_every_changed_source_file_to_touch_a_definition(intel) -> None:
    """A coverage run settled every definition of one file; the other changed file holds
    no definition the index knows, so the change was not measured as a whole."""
    repo = _py_repo()
    repo.coverage_files = {"pkg/w.py": {"commit_sha": "c" * 40, "lines": "[[1, 99]]", "tests": 2}}
    out = await _gate(intel, repo, files=["pkg/w.py"])
    assert out.epistemic == "measured" and out.untested == []
    out = await _gate(intel, repo, files=["pkg/w.py", "pkg/script.py"])
    # the index holds nothing of the second file, which is the graph's own cause too
    assert out.epistemic == "lower_bound"
    assert out.causes == ["external_calls", "unmapped_changes"]


async def test_a_neighbour_counts_a_definition_both_tiers_hold_once(intel) -> None:
    repo = _py_repo()
    repo.symbols[3] = {**repo.symbols[3], "tier_ids": ["fH", "pH"]}
    await _gate(intel, repo, diff=_one_line("pkg/w.py", 30))
    # every id of a definition stands for that definition, in path and line order
    assert repo.neighbor_definitions == {"cW": "cW", "mI": "mI", "fH": "fH", "pH": "fH"}


async def test_a_coverage_run_of_another_revision_measures_nothing(intel) -> None:
    repo = _py_repo()
    repo.coverage_files = {"pkg/w.py": {"commit_sha": "d" * 40, "lines": "[]", "tests": 2}}
    out = await _gate(intel, repo, diff=_one_line("pkg/w.py", 30))
    assert out.untested == []
    assert [u["qualified_name"] for u in out.unlinked] == ["pkg.w::Widget::render"]
    assert out.epistemic == "lower_bound" and "no_coverage" in out.causes
