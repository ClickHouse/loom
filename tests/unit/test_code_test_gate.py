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


def test_stem_partners_follow_the_worker_rule() -> None:
    srcs = ["src/Storages/MergeTree/MergeTreeData.cpp", "src/IO/io.cpp", "server/foo.py"]
    assert tg.stem_partners("src/Storages/MergeTree/tests/gtest_merge_tree_data.cpp", srcs) == []
    assert tg.stem_partners("src/Storages/tests/gtest_mergetreedata.cpp", srcs) == [srcs[0]]
    assert tg.stem_partners("tests/unit/test_foo.py", srcs) == ["server/foo.py"]
    # short stems never match: "io" is inside everything
    assert tg.stem_partners("tests/unit/test_io.py", srcs) == []


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


def _touched(sid: str, path: str, name: str = "f") -> dict[str, Any]:
    return {"symbol_id": sid, "qualified_name": name, "path": path, "start_line": 1}


def test_build_gate_orders_by_evidence_and_dedupes() -> None:
    inp = tg.GateInputs(
        changed_paths=["src/alpha.py", "tests/unit/test_zeta.py", "src/b.py"],
        touched=[_touched("s1", "src/alpha.py", "alpha"), _touched("s2", "src/b.py", "b")],
        impacted={
            "t1": {
                "qualified_name": "test_b_hop",
                "path": "tests/unit/test_hop.py",
                "hop": 2,
                "roots": ["s2"],
            },
            "c1": {"qualified_name": "caller", "path": "src/c.py", "hop": 1, "roots": ["s1"]},
        },
        test_map=[
            {"test_path": "tests/unit/test_hop.py", "src_path": "src/b.py", "confidence": 0.4},
            {
                "test_path": "tests/unit/test_alpha.py",
                "src_path": "src/alpha.py",
                "confidence": 0.7,
            },
            {"test_path": "tests/unit/test_zeta.py", "src_path": "src/alpha.py", "confidence": 0.9},
        ],
    )
    g = tg.build_gate(inp)
    assert [r["why"] for r in g.tests] == ["changed_test", "stem_partner", "test_map"]
    assert [r["test_path"] for r in g.tests] == [
        "tests/unit/test_zeta.py",
        "tests/unit/test_alpha.py",
        "tests/unit/test_hop.py",
    ]
    # The changed test keeps its first (strongest) evidence even though the
    # test map also names it; the graph-hop test was already listed via the map.
    assert g.tests[0]["evidence"] == "in the diff"
    assert g.tests[1]["src_path"] == "src/alpha.py"
    assert (
        g.tests[2]["run"] == "pytest tests/unit/test_hop.py" and g.tests[2]["run_unknown"] is False
    )
    assert g.tests_total == 3 and g.untested == [] and g.untested_total == 0
    assert g.obligations is True


def test_untested_complement_by_map_stem_and_graph() -> None:
    inp = tg.GateInputs(
        changed_paths=["src/a.py", "src/b.py", "src/c.py", "src/d.py"],
        touched=[
            _touched("s1", "src/a.py", "a"),
            _touched("s2", "src/b.py", "b"),
            _touched("s3", "src/c.py", "c"),
            _touched("s4", "src/d.py", "d"),
        ],
        impacted={
            # a test reaches s3 through the graph in two hops
            "t": {
                "qualified_name": "test_c",
                "path": "tests/x/test_other.py",
                "hop": 2,
                "roots": ["s3"],
            },
        },
        test_map=[
            {"test_path": "tests/unit/test_a.py", "src_path": "src/a.py", "confidence": 0.7},
            {"test_path": "tests/unit/test_bee.py", "src_path": "src/zzz.py", "confidence": 0.5},
        ],
    )
    g = tg.build_gate(inp)
    # s1 via the map, s3 via the graph; s2 and s4 reach no test at all.
    assert [u["path"] for u in g.untested] == ["src/b.py", "src/d.py"]
    assert g.untested_total == 2
    assert {r["why"] for r in g.tests} == {"test_map", "graph_hop"}
    hop = next(r for r in g.tests if r["why"] == "graph_hop")
    assert hop["hop"] == 2 and "test_c reaches the change in 2 hop(s)" == hop["evidence"]


def test_gate_caps_and_reports_totals() -> None:
    inp = tg.GateInputs(
        changed_paths=["src/a.py"],
        touched=[_touched(f"s{i}", f"src/f{i}.py") for i in range(5)],
        impacted={},
        test_map=[
            {"test_path": f"tests/unit/test_{i}.py", "src_path": "src/a.py", "confidence": 0.5}
            for i in range(4)
        ],
        limit=2,
    )
    g = tg.build_gate(inp)
    assert len(g.tests) == 2 and g.tests_total == 4
    assert len(g.untested) == 2 and g.untested_total == 5


def test_no_change_no_obligations() -> None:
    g = tg.build_gate(tg.GateInputs(changed_paths=[], touched=[], impacted={}, test_map=[]))
    assert g.obligations is False and g.tests == [] and g.untested == []


# ---- handler over a fake repo ----------------------------------------------------


def _sym(sid: str, name: str, path: str, start: int, end: int) -> dict[str, Any]:
    return {
        "symbol_id": sid,
        "qualified_name": name,
        "name": name.split("::")[-1],
        "kind": "function",
        "path": path,
        "start_line": start,
        "end_line": end,
        "tier": "scip",
        "signature": "",
        "doc_comment": "",
    }


def _edge(frm: str, to: str, fq: str, path: str) -> dict[str, Any]:
    return {
        "from_symbol_id": frm,
        "to_symbol_id": to,
        "from_qualified_name": fq,
        "to_qualified_name": "",
        "path": path,
        "line": 5,
        "tier": "scip",
        "edge_kind": "calls",
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

    async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
        return [s for s in self.symbols if s["path"] in paths]

    async def edges_for_symbols(
        self, org, ns, ids, *, direction, edge_kinds=None, limit=200, min_confidence=0.0
    ):
        assert direction == "callers"
        return [e for sid in ids for e in self.callers.get(sid, [])]

    async def test_map_for(self, org, ns, paths, *, limit=30):
        return [
            {
                "test_path": "tests/queries/0_stateless/01234_a.sql",
                "src_path": "src/A.cpp",
                "confidence": 0.8,
            }
        ]


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


async def test_handler_diff_shape_walks_roots_to_tests(intel) -> None:
    from server.code.api.models_testing import CodeTestGateInput

    with patch.object(intel, "CodeRepo", return_value=_Repo()):
        out = await intel.handle_code_test_gate(
            CodeTestGateInput(org="o", namespace="n", diff=_DIFF, depth=2)
        )
    assert out.changed_files == ["src/A.cpp"]
    # hunk overlap picks f (lines 10-20), not g (40-60)
    assert [t["qualified_name"] for t in out.touched_symbols] == ["DB::A::f"]
    assert "symbol_id" not in out.touched_symbols[0]
    whys = [t["why"] for t in out.tests]
    assert whys == ["test_map", "graph_hop"]
    assert out.tests[0]["run"] == "tests/clickhouse-test 01234_a"
    assert out.tests[1]["test_path"] == "src/tests/gtest_parts.cpp"
    assert out.tests[1]["hop"] == 2 and out.tests[1]["run_unknown"] is True
    # f is covered (map + graph) so nothing is untested; obligations still hold
    assert out.untested == [] and out.obligations is True
    assert out.tests_total == 2 and out.counts_floor is True


async def test_handler_files_shape_and_untested(intel) -> None:
    from server.code.api.models_testing import CodeTestGateInput

    repo = _Repo()
    repo.callers = {}  # no graph at all

    async def no_tests(org, ns, paths, *, limit=30):
        return []

    repo.test_map_for = no_tests  # type: ignore[method-assign]
    with patch.object(intel, "CodeRepo", return_value=repo):
        out = await intel.handle_code_test_gate(
            CodeTestGateInput(org="o", namespace="n", files=["src/A.cpp"], depth=1)
        )
    assert [t["qualified_name"] for t in out.touched_symbols] == ["DB::A::f", "DB::A::g"]
    assert out.tests == []
    assert [u["qualified_name"] for u in out.untested] == ["DB::A::f", "DB::A::g"]
    assert out.obligations is True and out.untested_total == 2


async def test_handler_requires_a_change(intel) -> None:
    from server.code.api.models_testing import CodeTestGateInput

    with patch.object(intel, "CodeRepo", return_value=_Repo()):
        with pytest.raises(ValidationError):
            await intel.handle_code_test_gate(CodeTestGateInput(org="o", namespace="n"))


async def test_a_test_only_the_parser_saw_credits_the_collapsed_definition(intel) -> None:
    """A definition both tiers hold arrives as its scip row carrying both ids (`tier_ids`);
    a test that calls it only through the parser-tier id still covers it."""
    from server.code.api.models_testing import CodeTestGateInput

    repo = _Repo()
    repo.symbols = [{**_sym("sA", "DB::A::f", "src/A.cpp", 10, 20), "tier_ids": ["sA", "pA"]}]
    repo.callers = {"pA": [_edge("tT", "pA", "TEST_F(Parts, f)", "src/tests/gtest_parts.cpp")]}

    async def no_tests(org, ns, paths, *, limit=30):
        return []

    repo.test_map_for = no_tests  # type: ignore[method-assign]
    with patch.object(intel, "CodeRepo", return_value=repo):
        out = await intel.handle_code_test_gate(
            CodeTestGateInput(org="o", namespace="n", diff=_DIFF, depth=1)
        )
    assert [t["test_path"] for t in out.tests] == ["src/tests/gtest_parts.cpp"]
    assert out.untested == []
