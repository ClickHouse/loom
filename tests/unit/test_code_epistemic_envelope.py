"""A graph answer says whether it is exact or a lower bound and
why; an empty upstream walk that is a lower bound is `unknown`, never low;
code.impact pages its radius per hop and reports the true per-hop counts."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from server.code.app.symbol import dispatch, gate, graph, impact
from server.code.domain.index import epistemic as ep
from server.settings import settings

# ---- pure module ---------------------------------------------------------------


def test_exact_needs_every_condition() -> None:
    kind, causes = ep.classify(
        unresolved=0,
        ambiguous=0,
        seed_paths=["src/A.cpp"],
        scip_paths={"src/A.cpp"},
    )
    assert (kind, causes) == ("exact", [])


@pytest.mark.parametrize(
    "kw, expected",
    [
        ({"unresolved": 3}, [ep.CAUSE_UNRESOLVED]),
        ({"ambiguous": 1}, [ep.CAUSE_AMBIGUOUS]),
        ({"dispatch_missing": True}, [ep.CAUSE_DISPATCH]),
        ({"scip_paths": set()}, [ep.CAUSE_TREESITTER]),
        ({"scip_paths": set(), "unindexed_paths": {"src/A.cpp"}}, [ep.CAUSE_EXTERNAL]),
    ],
)
def test_each_gap_is_a_named_cause(kw: dict[str, Any], expected: list[str]) -> None:
    base: dict[str, Any] = {
        "unresolved": 0,
        "ambiguous": 0,
        "seed_paths": ["src/A.cpp"],
        "scip_paths": {"src/A.cpp"},
    }
    kind, causes = ep.classify(**{**base, **kw})
    assert kind == "lower_bound" and causes == expected


def test_causes_keep_enum_order() -> None:
    _, causes = ep.classify(
        unresolved=1,
        ambiguous=1,
        dispatch_missing=True,
        seed_paths=["src/A.cpp", "src/B.cpp"],
        scip_paths={"src/A.cpp"},
    )
    assert causes == [
        ep.CAUSE_UNRESOLVED,
        ep.CAUSE_AMBIGUOUS,
        ep.CAUSE_DISPATCH,
        ep.CAUSE_TREESITTER,
    ]
    assert all(c in ep.CAUSES for c in causes)


def test_envelope_classifies() -> None:
    env = ep.envelope(
        unresolved=2,
        ambiguous=0,
        seed_paths=["src/A.cpp"],
        scip_paths={"src/A.cpp"},
    )
    assert env.epistemic == "lower_bound" and env.causes == [ep.CAUSE_UNRESOLVED]


def test_scip_coverage_from_rows_only_proves() -> None:
    rows = [{"path": "src/A.cpp", "tier": "scip"}, {"path": "src/B.cpp", "tier": "treesitter"}]
    assert ep.scip_coverage_from_rows(rows) == {"src/A.cpp"}


# ---- handlers over a fake graph -----------------------------------------------------


def _sym(name: str, sid: str, path: str, tier: str = "scip") -> dict[str, Any]:
    return {
        "symbol_id": sid,
        "qualified_name": name,
        "name": name.split("::")[-1],
        "kind": "function",
        "path": path,
        "start_line": 10,
        "end_line": 20,
        "tier": tier,
        "signature": "()",
        "doc_comment": "",
    }


def _edge(frm: str, to: str, fq: str, tq: str, path: str = "src/X.cpp") -> dict[str, Any]:
    return {
        "from_symbol_id": frm,
        "to_symbol_id": to,
        "from_qualified_name": fq,
        "to_qualified_name": tq,
        "path": path,
        "line": 5,
        "tier": "scip",
        "edge_kind": "calls",
        "confidence": 1.0,
    }


class _Repo:
    """Seed `DB::A::insert` in a compiler-indexed file with two hop-1 callers,
    one of which has three callers of its own (hop 2)."""

    def __init__(
        self,
        *,
        misses: dict[str, dict[str, int]] | None = None,
        scip_paths: set[str] | None = None,
        seed_tier: str = "scip",
    ) -> None:
        self.seed = _sym("DB::A::insert", "sA", "src/A.cpp", tier=seed_tier)
        self.callers = {
            "sA": [
                _edge("c1", "sA", "DB::C1::run", "DB::A::insert"),
                _edge("c2", "sA", "DB::C2::run", "DB::A::insert", "tests/gtest_a.cpp"),
            ],
            "c1": [_edge(f"d{i}", "c1", f"DB::D{i}::go", "DB::C1::run") for i in range(3)],
        }
        self.misses = misses or {}
        self.scip_paths = {"src/A.cpp"} if scip_paths is None else scip_paths
        self.coverage_asked: list[list[str]] = []
        self.edge_limits: list[int] = []

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        return [self.seed] if name == self.seed["qualified_name"] else []

    async def symbols_by_ids(self, org, ns, ids):
        return [self.seed] if self.seed["symbol_id"] in ids else []

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
        self.edge_limits.append(limit)
        if direction != "callers":
            return []
        return [e for sid in ids for e in self.callers.get(sid, [])]

    async def call_misses_for_names(self, org, ns, names):
        return {n: self.misses[n] for n in names if n in self.misses}

    async def scip_covered_paths(self, org, ns, paths):
        self.coverage_asked.append(list(paths))
        return {p for p in paths if p in self.scip_paths}

    async def scip_stale_paths(self, org, ns, paths):
        return {p for p in paths if p in getattr(self, "stale_paths", set())}

    indexed = {"src/A.cpp", "src/Constants.h"}  # in code_files; Constants.h has no symbols

    async def live_paths_present(self, org, ns, paths):
        return {p for p in paths if p in self.indexed}

    async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
        return [self.seed] if self.seed["path"] in paths else []

    async def test_map_for(self, org, ns, paths, *, limit=30):
        return []

    async def pr_history_for(self, org, ns, *, paths, limit):
        return []

    async def reviewers_by_pr(self, org, ns, pr_numbers):
        return {}

    async def top_reviewers(self, org, ns, *, days=180, limit=6):
        return []


@pytest.fixture
def intel(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")
    for h in (graph, impact, gate):
        monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)

    async def no_dispatch(*a, **k):
        return []

    async def has_overrides(*a, **k):
        return True  # compiler tier carries relationships: no base = the answer

    monkeypatch.setattr(graph, "_dispatch_seeds", no_dispatch)
    monkeypatch.setattr(impact, "_dispatch_seeds", no_dispatch)
    monkeypatch.setattr(dispatch, "_namespace_has_override_edges", has_overrides)
    return graph


async def _impact(h, repo, **fields):
    from server.code.api.models_symbol import CodeImpactInput

    with patch.object(h, "CodeRepo", return_value=repo):
        return await h.handle_code_impact(CodeImpactInput(org="o", namespace="n", **fields))


async def test_impact_symbol_shape_is_exact(intel) -> None:
    repo = _Repo()
    out = await _impact(impact, repo, name="DB::A::insert")
    assert out.epistemic == "exact" and out.causes == []
    assert out.by_hop_counts == {1: 2, 2: 3}
    # the seed's file leads the files its `lessons` are read for
    assert out._paths == ["src/A.cpp"]
    assert out.truncated is False
    # coverage was proven from the seed row itself: no coverage read
    assert repo.coverage_asked == []


async def test_impact_declined_sites_make_a_lower_bound(intel) -> None:
    repo = _Repo(misses={"insert": {"unresolved": 4, "ambiguous": 0}})
    out = await _impact(impact, repo, name="DB::A::insert")
    assert out.epistemic == "lower_bound" and out.causes == ["unresolved_calls"]
    assert out.unresolved_calls == 4


async def test_impact_treesitter_only_seed_reads_coverage_once(intel) -> None:
    repo = _Repo(scip_paths=set(), seed_tier="treesitter")
    out = await _impact(impact, repo, name="DB::A::insert")
    assert repo.coverage_asked == [["src/A.cpp"]]
    assert out.epistemic == "lower_bound" and out.causes == ["treesitter_only_files"]


async def test_impact_empty_treesitter_walk_is_a_lower_bound(intel) -> None:
    repo = _Repo(scip_paths=set(), seed_tier="treesitter")
    repo.callers = {}
    out = await _impact(impact, repo, name="DB::A::insert")
    assert out.impacted_symbols == [] and out.epistemic == "lower_bound"


async def test_impact_pages_per_hop_and_reports_true_counts(intel) -> None:
    repo = _Repo()
    out = await _impact(impact, repo, name="DB::A::insert", per_hop_limit=1)
    names = [(s["hop"], s["qualified_name"]) for s in out.impacted_symbols]
    assert names == [(1, "DB::C1::run"), (2, "DB::D0::go")], (
        "one row from EACH hop, not the first N"
    )
    assert out.by_hop_counts == {1: 2, 2: 3} and out.truncated is True
    page2 = await _impact(impact, repo, name="DB::A::insert", per_hop_limit=1, offset=1)
    assert [(s["hop"], s["qualified_name"]) for s in page2.impacted_symbols] == [
        (1, "DB::C2::run"),
        (2, "DB::D1::go"),
    ]
    assert page2.truncated is True


async def test_impact_summary_only_keeps_counts_and_drops_rows(intel) -> None:
    repo = _Repo()
    out = await _impact(impact, repo, name="DB::A::insert", summary_only=True)
    assert out.impacted_symbols == [] and out.by_hop_counts == {1: 2, 2: 3}
    assert out.blast_radius_score > 0


async def test_impact_read_limit_rises_with_per_hop_limit(intel) -> None:
    repo = _Repo()
    await _impact(impact, repo, name="DB::A::insert", per_hop_limit=500)
    assert max(repo.edge_limits) == 500
    repo2 = _Repo()
    await _impact(impact, repo2, name="DB::A::insert")
    assert max(repo2.edge_limits) == 300


async def test_impact_classifies_and_carries_no_risk_band(intel) -> None:
    # the envelope is on the wire, `risk` / `risk_note` are not
    out = await _impact(impact, _Repo(), name="DB::A::insert")
    assert out.epistemic == "exact"
    body = out.model_dump()
    assert "risk" not in body and "risk_note" not in body


async def test_impact_coverage_read_failure_cannot_claim_exact(intel) -> None:
    repo = _Repo(seed_tier="treesitter")

    async def boom(org, ns, paths):
        raise RuntimeError("down")

    repo.scip_covered_paths = boom  # type: ignore[method-assign]
    out = await _impact(impact, repo, name="DB::A::insert")
    assert out.epistemic == "lower_bound" and "treesitter_only_files" in out.causes


async def test_impact_dispatch_missing_without_override_edges(intel, monkeypatch) -> None:
    async def no_overrides(*a, **k):
        return False

    monkeypatch.setattr(dispatch, "_namespace_has_override_edges", no_overrides)
    out = await _impact(impact, _Repo(), name="DB::A::insert")
    assert out.causes == ["dispatch_seeds_missing"] and out.epistemic == "lower_bound"


DIFF = """\
diff --git a/src/A.cpp b/src/A.cpp
--- a/src/A.cpp
+++ b/src/A.cpp
@@ -12,3 +12,3 @@ void DB::A::insert()
 x
diff --git a/src/Unindexed.cpp b/src/Unindexed.cpp
--- a/src/Unindexed.cpp
+++ b/src/Unindexed.cpp
@@ -1,3 +1,3 @@
 y
diff --git a/src/Constants.h b/src/Constants.h
--- a/src/Constants.h
+++ b/src/Constants.h
@@ -1,1 +1,1 @@
 c
diff --git a/tests/queries/0_stateless/04000_new.sh b/tests/queries/0_stateless/04000_new.sh
--- /dev/null
+++ b/tests/queries/0_stateless/04000_new.sh
@@ -0,0 +1,2 @@
+t
diff --git a/docs/readme.md b/docs/readme.md
--- a/docs/readme.md
+++ b/docs/readme.md
@@ -1,1 +1,1 @@
 z
"""


async def test_impact_diff_shape_sizes_floor_and_names_external_files(intel) -> None:
    repo = _Repo(misses={"insert": {"unresolved": 0, "ambiguous": 2}})
    out = await _impact(impact, repo, diff=DIFF)
    assert out.ambiguous_calls == 2, "the diff shape sizes its floor too"
    # Unindexed.cpp is a graphed language the index does not hold: external.
    # Constants.h is indexed but defines nothing; the .sh test is a leaf; the
    # .md is not code. None of those three is a cause.
    assert out.causes == ["ambiguous_calls", "external_calls"]
    assert repo.coverage_asked == [], "coverage is proven from the seed row; nothing to read"


async def test_callers_carries_envelope_and_hop_counts(intel) -> None:
    from server.code.api.models_symbol import CodeCallersInput

    repo = _Repo()
    with patch.object(intel, "CodeRepo", return_value=repo):
        out = await intel.handle_code_callers(
            CodeCallersInput(
                org="o", namespace="n", name="DB::A::insert", depth=2, include_ambiguous=False
            )
        )
    assert out.epistemic == "exact" and out.by_hop_counts == {1: 2, 2: 3}


async def test_a_scip_edge_from_a_file_changed_since_the_compiler_run_is_no_proof(intel) -> None:
    # a compiler-tier edge from an older tree is not served as `exact`
    from server.code.api.models_symbol import CodeCallersInput

    repo = _Repo()
    repo.stale_paths = {"src/X.cpp"}
    with patch.object(intel, "CodeRepo", return_value=repo):
        out = await intel.handle_code_callers(
            CodeCallersInput(org="o", namespace="n", name="DB::A::insert", include_ambiguous=False)
        )
    assert out.epistemic == "lower_bound" and out.causes == ["scip_stale_files"]
    assert "src/X.cpp" in out.note and "stale" in out.note


async def test_a_failed_staleness_read_never_claims_exact(intel) -> None:
    from server.code.api.models_symbol import CodeCallersInput

    repo = _Repo()

    async def boom(org, ns, paths):
        raise RuntimeError("clickhouse down")

    repo.scip_stale_paths = boom  # type: ignore[method-assign]
    with patch.object(intel, "CodeRepo", return_value=repo):
        out = await intel.handle_code_callers(
            CodeCallersInput(org="o", namespace="n", name="DB::A::insert", include_ambiguous=False)
        )
    assert out.epistemic == "lower_bound" and "scip_stale_files" in out.causes


def test_causes_render_in_enum_order() -> None:
    from server.code.domain.index import epistemic as ep

    got = ep.envelope(
        unresolved=1,
        ambiguous=0,
        seed_paths=["a"],
        scip_paths={"a"},
        scip_stale=True,
        truncated=True,
    )
    assert got.epistemic == "lower_bound"
    assert got.causes == ["unresolved_calls", "scip_stale_files", "truncated"]
    assert got.causes == [c for c in ep.CAUSES if c in got.causes]


async def test_callees_carry_the_envelope(intel) -> None:
    from server.code.api.models_symbol import CodeCallersInput

    with patch.object(intel, "CodeRepo", return_value=_Repo()):
        out = await intel.handle_code_callees(
            CodeCallersInput(org="o", namespace="n", name="DB::A::insert", include_ambiguous=False)
        )
    assert out.epistemic == "exact"


async def test_test_gate_carries_envelope(intel) -> None:
    from server.code.api.models_testing import CodeTestGateInput

    repo = _Repo(misses={"insert": {"unresolved": 1, "ambiguous": 0}})
    with patch.object(gate, "CodeRepo", return_value=repo):
        out = await gate.handle_code_test_gate(
            CodeTestGateInput(org="o", namespace="n", files=["src/A.cpp"])
        )
    assert out.epistemic == "lower_bound" and out.causes == ["unresolved_calls"]
    assert out.by_hop_counts == {1: 2, 2: 3}


def test_input_bounds() -> None:
    from pydantic import ValidationError

    from server.code.api.models_symbol import CodeImpactInput

    with pytest.raises(ValidationError):
        CodeImpactInput(name="x", per_hop_limit=501)
    with pytest.raises(ValidationError):
        CodeImpactInput(name="x", offset=-1)


async def test_stale_paths_are_every_asked_path_the_read_does_not_prove_fresh() -> None:
    # a file the fast tier deleted (memory_search.py after its package split) or holds
    # no row for is as stale as one it rewrote: the read returns only the fresh paths
    from server.code.store.index import CodeRepo

    class _Ch:
        def __init__(self) -> None:
            self.params: dict[str, Any] = {}

        async def query_all(self, sql, params=None):
            self.params = dict(params or {})
            assert "tombstoned_at IS NULL" in sql and "created_at <=" in sql
            return [{"path": "src/fresh.py"}]

    ch = _Ch()
    stale = await CodeRepo(ch).scip_stale_paths(  # type: ignore[arg-type]
        "o", "n", ["src/fresh.py", "src/deleted.py", "src/fresh.py", "src/newer.cpp"]
    )
    assert stale == {"src/deleted.py", "src/newer.cpp"}
    assert ch.params["paths"] == ["src/fresh.py", "src/deleted.py", "src/newer.cpp"]
    assert await CodeRepo(ch).scip_stale_paths("o", "n", []) == set()  # type: ignore[arg-type]


async def test_callees_count_the_seeds_own_declined_sites(intel) -> None:
    """A call inside the seed the resolver declined is a callee the walk cannot list: the
    answer is a lower bound with the count, not `exact`."""
    from server.code.api.models_symbol import CodeCallersInput

    class _WithOutgoingMiss(_Repo):
        async def live_call_misses_for_paths(self, org, ns, paths):
            return {
                "src/A.cpp": [
                    {"line": 5, "disposition": "unresolved", "from_symbol_id": "sA"},
                    {"line": 6, "disposition": "ambiguous", "from_symbol_id": "sA"},
                    {"line": 500, "disposition": "unresolved", "from_symbol_id": "other"},
                ]
            }

    with patch.object(intel, "CodeRepo", return_value=_WithOutgoingMiss()):
        out = await intel.handle_code_callees(
            CodeCallersInput(org="o", namespace="n", name="DB::A::insert", include_ambiguous=False)
        )
    assert (out.unresolved_calls, out.ambiguous_calls) == (1, 1)
    assert out.epistemic == "lower_bound"
    assert out.causes[:2] == ["unresolved_calls", "ambiguous_calls"]
