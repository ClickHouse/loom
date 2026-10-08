"""A test is a unit that reaches the symbol. The coverage
claim reads the `calls` edges from test units, the test functions named for
the subject and the sibling test file by convention — on any repository,
not only the ClickHouse trees — and a reaching unit is the positive control
where the ClickHouse probe finds nothing."""

from __future__ import annotations

import pytest

import server.code.app.coverage as h
from server.code.api.models_testing import CodeCoverageClaimInput
from server.code.domain.index import coverage_claim as cc
from server.code.domain.index import test_conventions as tcv

# The scalar code repository these tests model (none is configured by default).
pytestmark = pytest.mark.usefixtures("scalar_code_repo")

ORG, NS = "clickhouse", "code-go-repo"


# ---- conventions ---------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "store/store_test.go",
        "pkg/a/b_test.go",
        "tests/unit/test_x.py",
        "server/foo_test.py",
        "tests/conftest.py",
        "test/helpers.py",
        "src/Grid.test.tsx",
        "src/__tests__/Grid.tsx",
        "src/api.spec.ts",
        "cypress/e2e/login.cy.ts",
        "src/test/java/com/x/FooTest.java",
        "core/FooTests.java",
        "core/TestFoo.java",
        "core/FooIT.java",
        "app/src/test/kotlin/FooTest.kt",
        "tests/integration.rs",
        "src/lib_test.rs",
        "src/Storages/tests/gtest_foo.cpp",
        "src/a/foo_test.cc",
        "src/a/foo_unittest.cc",
        "spec/models/user_spec.rb",
        "test/test_user.rb",
        "tests/cli.bats",
        "tests/queries/0_stateless/02994_x.sql",
        "tests/integration/test_quota/test.py",
    ],
)
def test_is_test_unit_path_positive(path: str) -> None:
    assert tcv.is_test_unit_path(path), path


@pytest.mark.parametrize(
    "path",
    [
        "store/store.go",
        "store/Testing.go",
        "store/testdata/store_test.go",
        "api/v1/foo.pb.go",
        "tests/fixtures/sample_test.go",
        "src/Grid.tsx",
        "src/Core/Settings.cpp",
        "tests/queries/0_stateless/02994_x.reference",
        "src/main/java/com/x/Foo.java",
        "src/lib.rs",
        "README.md",
        "",
    ],
)
def test_is_test_unit_path_negative(path: str) -> None:
    assert not tcv.is_test_unit_path(path), path


def test_test_unit_names_by_language() -> None:
    names = tcv.test_unit_names("Get", "Store")
    assert names[:3] == ["TestGet", "TestStore_Get", "TestStoreGet"]
    assert "BenchmarkGet" in names and "ExampleStore_Get" in names and "FuzzGet" in names
    assert "test_get" in names and "test_store_get" in names
    assert len(names) <= 12
    py = tcv.test_unit_names("parseQuotaKey")
    assert (
        "TestParseQuotaKey" in py and "test_parse_quota_key" in py and "shouldParseQuotaKey" in py
    )
    assert tcv.test_unit_names("") == []
    assert tcv.snake("HTTPServerV2") == "http_server_v2"


def test_sibling_test_paths_by_language() -> None:
    assert tcv.sibling_test_paths("store/store.go") == ["store/store_test.go"]
    py = tcv.sibling_test_paths("src/pkg/mod.py")
    assert "src/pkg/test_mod.py" in py and "tests/pkg/test_mod.py" in py
    ts = tcv.sibling_test_paths("src/components/Grid.tsx")
    assert "src/components/Grid.test.tsx" in ts and "src/components/__tests__/Grid.test.ts" in ts
    assert len(ts) <= 8
    assert tcv.sibling_test_paths("svc/src/main/java/com/x/Foo.java") == [
        "svc/src/test/java/com/x/FooTest.java"
    ]
    assert "tests/parser.rs" in tcv.sibling_test_paths("src/parser.rs")
    assert "src/Access/tests/gtest_QuotaCache.cpp" in tcv.sibling_test_paths(
        "src/Access/QuotaCache.cpp"
    )
    assert tcv.sibling_test_paths("") == []


# ---- the legs over a stub repository ------------------------------------------

STORE = "store/store.go"
STORE_T = "store/store_test.go"
CACHE = "store/cache.go"


class _Repo:
    """A Go tree: `Store.Get` is called by `TestStore_Get` (a test unit) and
    by `Cache.Load` (not a test), which `TestCache_Load` calls."""

    def __init__(
        self,
        *,
        edges: bool = True,
        names: bool = True,
        sibling: bool = True,
        extra_test_callers: int = 0,
    ) -> None:
        self.edges, self.names, self.sibling = edges, names, sibling
        self.extra = extra_test_callers
        self.calls: list[tuple] = []

    async def grep_files(
        self, org, ns, pattern, *, regex, case_sensitive, path_prefix, limit, scope=None
    ):
        self.calls.append(("grep", pattern, path_prefix))
        return [], 0  # no ClickHouse trees: the control probe finds nothing

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        if name == "store.store::Store::Get":
            return [
                {
                    "symbol_id": "get",
                    "qualified_name": name,
                    "kind": "function",
                    "path": STORE,
                    "name": "Get",
                }
            ]
        return []

    async def coverage_rows_for(self, org, ns, paths, *, limit=2000):
        return []

    async def coverage_files_for(self, org, ns, paths):
        return {}

    async def test_map_detail(self, org, ns, paths, *, limit=60):
        return []

    async def edges_for_symbols(
        self,
        org,
        ns,
        ids,
        *,
        direction,
        edge_kinds=None,
        limit=200,
        min_confidence=0.0,
        per_match=None,
    ):
        self.calls.append(("edges", tuple(ids), direction))
        if not self.edges:
            return []
        if ids == ["get"]:
            rows = [
                {
                    "from_symbol_id": "t-get",
                    "to_symbol_id": "get",
                    "path": STORE_T,
                    "line": 12,
                    "from_qualified_name": "store.store::TestStore_Get",
                    "confidence": 1.0,
                    "tier": "treesitter",
                },
                {
                    "from_symbol_id": "load",
                    "to_symbol_id": "get",
                    "path": CACHE,
                    "line": 40,
                    "from_qualified_name": "store.store::Cache::Load",
                    "confidence": 0.9,
                    "tier": "treesitter",
                },
            ]
            for i in range(self.extra):
                rows.append(
                    {
                        "from_symbol_id": f"t{i}",
                        "to_symbol_id": "get",
                        "path": STORE_T,
                        "line": 100 + i,
                        "from_qualified_name": f"store.store::TestGet{i}",
                        "confidence": 1.0,
                        "tier": "treesitter",
                    }
                )
            return rows
        if ids == ["load"]:
            return [
                {
                    "from_symbol_id": "t-load",
                    "to_symbol_id": "load",
                    "path": "store/cache_test.go",
                    "line": 7,
                    "from_qualified_name": "store.store::TestCache_Load",
                    "confidence": 1.0,
                    "tier": "treesitter",
                }
            ]
        return []

    async def name_candidates(self, org, ns, names):
        self.calls.append(("names", tuple(names)))
        if self.names and "TestStore_Get" in names:
            return {
                "TestStore_Get": [("t-get", "store.store::TestStore_Get", "function")],
                "TestGet": [("data-get", "store.store::TestGet", "var")],
            }
        return {}

    async def symbols_by_ids(self, org, ns, ids):
        self.calls.append(("by_ids", tuple(ids)))
        return [
            {
                "symbol_id": "t-get",
                "qualified_name": "store.store::TestStore_Get",
                "kind": "function",
                "path": STORE_T,
                "name": "TestStore_Get",
            }
            for i in ids
            if i == "t-get"
        ]

    async def live_paths_present(self, org, ns, paths):
        self.calls.append(("present", tuple(sorted(paths))))
        return {STORE_T} if self.sibling and STORE_T in paths else set()


@pytest.fixture
def plane(monkeypatch):
    h.reset_control_cache()
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "resolve_code_identity", lambda o, n: (ORG, NS))
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)

    def use(repo):
        monkeypatch.setattr(h, "CodeRepo", lambda: repo)
        return repo

    return use


async def _claim(symbol: str = "store.store::Store::Get", **kw):
    return await h.handle_code_coverage_claim(
        CodeCoverageClaimInput(org=ORG, namespace=NS, symbol=symbol, **kw)
    )


@pytest.mark.asyncio
async def test_a_test_unit_calling_the_symbol_covers_it(plane) -> None:
    repo = plane(_Repo())
    out = await _claim()
    assert out.verdict == cc.COVERED_BY
    assert out.control == {"symbol": "arrayJoin", "ok": True, "via": "unit_tests"}
    row = out.tests[0]
    assert row["test_path"] == STORE_T and row["tier"] == cc.EXACT_SCOPED
    # all three legs agree on the one file; the edge's evidence leads
    assert set(row["methods"]) == {h.METHOD_CALLS_EDGE, h.METHOD_TEST_NAME, h.METHOD_SIBLING}
    assert row["evidence"].startswith("store.store::TestStore_Get calls it at line 12")
    assert row["confidence"] == 1.0
    # depth two ran (one test caller < 3) and found the test of the intermediate
    hop2 = next(r for r in out.tests if r["test_path"] == "store/cache_test.go")
    assert hop2["methods"] == [h.METHOD_CALLS_EDGE_2] and hop2["tier"] == cc.SHORT_NAME_GREP
    assert "through store.store::Cache::Load" in hop2["evidence"]
    assert out.epistemic == "exact" and out.strongest_tier == cc.EXACT_SCOPED
    assert out.tests_total == 2
    # the ClickHouse control grep still ran, in every tree
    assert [c for c in repo.calls if c[0] == "grep" and c[1] == "arrayJoin"]


@pytest.mark.asyncio
async def test_depth_two_is_skipped_when_the_first_hop_has_enough(plane) -> None:
    repo = plane(_Repo(extra_test_callers=3))
    out = await _claim()
    assert out.verdict == cc.COVERED_BY
    assert not any(c == ("edges", ("load",), "callers") for c in repo.calls)
    assert all(r["test_path"] != "store/cache_test.go" for r in out.tests)


@pytest.mark.asyncio
async def test_a_test_named_for_the_symbol_is_exact_evidence_without_edges(plane) -> None:
    repo = plane(_Repo(edges=False, sibling=False))
    out = await _claim()
    assert out.verdict == cc.COVERED_BY and out.control["via"] == "unit_tests"
    [row] = out.tests
    assert row["methods"] == [h.METHOD_TEST_NAME] and row["tier"] == cc.EXACT_SCOPED
    assert row["evidence"] == "defines TestStore_Get" and row["confidence"] == 0.9
    # the name read asked the conventional names, function rows only
    names = next(c for c in repo.calls if c[0] == "names")[1]
    assert names[:3] == ("TestGet", "TestStore_Get", "TestStoreGet")
    assert ("by_ids", ("t-get",)) in repo.calls


@pytest.mark.asyncio
async def test_a_sibling_file_alone_is_weak_evidence_and_no_control(plane) -> None:
    plane(_Repo(edges=False, names=False))
    out = await _claim()
    # the sibling leg is path_convention: no control passes on it alone, so
    # the verdict stays unknown (a claim needs a passing positive control)
    assert out.verdict == cc.UNKNOWN
    assert out.control == {"symbol": "arrayJoin", "ok": False}
    [row] = out.tests
    assert row["methods"] == [h.METHOD_SIBLING] and row["tier"] == cc.PATH_CONVENTION
    assert row["evidence"] == f"sits beside {STORE}"


@pytest.mark.asyncio
async def test_a_path_subject_takes_only_the_sibling_leg(plane) -> None:
    repo = plane(_Repo())
    out = await h.handle_code_coverage_claim(
        CodeCoverageClaimInput(org=ORG, namespace=NS, path=STORE)
    )
    assert [r["test_path"] for r in out.tests] == [STORE_T]
    assert out.tests[0]["methods"] == [h.METHOD_SIBLING]
    assert not any(c[0] in ("edges", "names") for c in repo.calls)


def test_merge_keeps_the_strongest_tier_and_claim_tests_order() -> None:
    base = [
        {
            "test_path": "a_test.go",
            "methods": ["grep"],
            "tier": cc.SHORT_NAME_GREP,
            "confidence": 0.8,
            "evidence": "names Get at line 3",
        }
    ]
    extra = [
        {
            "test_path": "a_test.go",
            "methods": [h.METHOD_CALLS_EDGE],
            "tier": cc.EXACT_SCOPED,
            "confidence": 1.0,
            "evidence": "x calls it at line 3",
        },
        {
            "test_path": "b_test.go",
            "methods": [h.METHOD_SIBLING],
            "tier": cc.PATH_CONVENTION,
            "confidence": 0.6,
            "evidence": "sits beside a.go",
        },
    ]
    merged = h.merge_test_rows(base, extra)
    assert [r["test_path"] for r in merged] == ["a_test.go", "b_test.go"]
    assert merged[0]["methods"] == ["grep", h.METHOD_CALLS_EDGE]
    assert merged[0]["tier"] == cc.EXACT_SCOPED and merged[0]["confidence"] == 1.0
    assert merged[0]["evidence"] == "x calls it at line 3"  # the strongest tier's evidence leads
    assert cc.epistemic_of([{"method": h.METHOD_CALLS_EDGE}]) == "exact"
    assert cc.epistemic_of([{"method": h.METHOD_SIBLING}]) == "lower_bound"
