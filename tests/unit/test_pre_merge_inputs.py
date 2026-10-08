"""Pre-merge inputs: the mirror carries the heads of open
PRs (bounded, refetched when upstream moves, never every PR), the items
ledger carries head / base / files (filled from the mirror first, carried
forward across upserts), and a PR's post-image is a transient overlay on
the index — added functions are seeds, the PR's own call sites are edges,
the answer says `overlay: true` — while a request without `pr_number` is
byte for byte what it was."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from server.code.app.index import gitrepo
from server.code.app.index import pr_overlay as po
from server.settings import settings

ORG, NS = "o", "n"
HEAD = "a" * 40
BASE = "b" * 40


# ---- gitrepo: the PR-head refspecs -------------------------------------------


def test_pull_head_refs_parse_and_select() -> None:
    text = (
        f"{HEAD}\trefs/pull/12/head\n{'c' * 40}\trefs/pull/12/merge\n{'d' * 40}\trefs/pull/7/head\n"
    )
    remote = gitrepo.parse_ls_remote_pull_heads(text)
    assert remote == {12: HEAD, 7: "d" * 40}
    assert gitrepo.pull_head_ref(12) == "refs/pull/12/head"
    # 12 is behind (local tip differs), 7 is current, 99 is not upstream (closed), 0 is noise
    local = {12: "e" * 40, 7: "d" * 40}
    assert gitrepo.pull_heads_to_fetch([12, 7, 99, 0, 12], remote, local, limit=10) == [12]
    # the batch bound keeps the caller's (newest-first) order
    remote = {n: f"{n:040d}" for n in range(1, 6)}
    assert gitrepo.pull_heads_to_fetch([5, 4, 3, 2, 1], remote, {}, limit=2) == [5, 4]


async def test_fetch_pull_heads_sends_one_stdin_fetch_without_prune(monkeypatch) -> None:
    calls: list[tuple[tuple[str, ...], bytes | None]] = []

    async def fake_git(git_dir, *args, timeout_s=0.0, token="", stdin=None):
        calls.append((args, stdin))
        return ""

    monkeypatch.setattr(gitrepo, "_git", fake_git)
    assert await gitrepo.fetch_pull_heads("/g", "https://r", [3, 3, 9, 0]) == 2
    ((args, stdin),) = calls
    assert args[:3] == ("fetch", "--stdin", "https://r") and "--prune" not in args
    assert stdin == b"+refs/pull/3/head:refs/pull/3/head\n+refs/pull/9/head:refs/pull/9/head\n"
    assert await gitrepo.fetch_pull_heads("/g", "https://r", []) == 0


# ---- the overlay's pure pieces --------------------------------------------------


@dataclass(frozen=True)
class _Sym:
    qualified_name: str
    name: str
    kind: str
    signature: str
    path: str
    start_line: int
    end_line: int
    has_body: bool = True


DIFF = """diff --git a/src/A.cpp b/src/A.cpp
--- a/src/A.cpp
+++ b/src/A.cpp
@@ -10,0 +11,3 @@ void DB::A::old()
+int DB::A::fresh() { return old(); }
+
+
@@ -30 +33 @@
+  x();
diff --git a/src/Gone.cpp b/src/Gone.cpp
--- a/src/Gone.cpp
+++ /dev/null
@@ -1,3 +0,0 @@
-a
-b
-c
"""


def test_new_side_hunks_and_cut() -> None:
    hunks = po.new_side_hunks(DIFF)
    assert hunks == {"src/A.cpp": [(11, 13), (33, 33)]}  # a deleted file has no post-image
    cut, truncated = po.cut_diff(DIFF, 120)
    assert truncated and cut.startswith("diff --git a/src/A.cpp") and "src/Gone.cpp" not in cut
    assert po.cut_diff(DIFF, 10_000) == (DIFF, False)
    assert po.parsable_paths(["src/A.cpp", "src/Gone.cpp", "docs/x.md"], ("src/",), hunks) == [
        "src/A.cpp"
    ]


def test_classify_symbols_added_modified_unchanged() -> None:
    syms = [
        _Sym("DB::A::fresh", "fresh", "function", "()", "src/A.cpp", 11, 11),
        _Sym("DB::A::old", "old", "function", "()", "src/A.cpp", 5, 9),
        _Sym("DB::A::touched", "touched", "function", "()", "src/A.cpp", 30, 40),
        _Sym("DB::A", "A", "class", "", "src/A.cpp", 1, 60),
        _Sym("DB::A::decl", "decl", "function", "()", "src/A.cpp", 2, 2, has_body=False),
    ]
    indexed = {"DB::A::old", "DB::A::touched"}
    out = po.classify_symbols(syms, indexed, [(11, 13), (33, 33)])
    assert [(s.qualified_name, s.status) for s in out] == [
        ("DB::A::fresh", "added"),
        ("DB::A::old", "unchanged"),
        ("DB::A::touched", "modified"),
    ]
    from server.code.domain.extract.treesitter import symbol_id

    assert out[0].symbol_id == symbol_id("DB::A::fresh", "()")  # the id the ingest would write
    assert out[0].row()["tier"] == "overlay" and out[0].row()["overlay_status"] == "added"


def test_merge_name_table_keeps_the_indexs_id_for_a_known_name() -> None:
    master = {"old": [("id-old", "DB::A::old", "function")]}
    syms = po.classify_symbols(
        [
            _Sym("DB::A::old", "old", "function", "()", "src/A.cpp", 5, 9),
            _Sym("DB::A::fresh", "fresh", "function", "()", "src/A.cpp", 11, 11),
        ],
        {"DB::A::old"},
        [],
    )
    table = po.merge_name_table(master, syms)
    assert table["old"] == [("id-old", "DB::A::old", "function")]  # not a second candidate
    assert [c[1] for c in table["fresh"]] == ["DB::A::fresh"]
    assert master == {"old": [("id-old", "DB::A::old", "function")]}  # the input is not mutated


def test_merge_edge_rows_counts_a_site_once_index_tier_first() -> None:
    master = [{"from_qualified_name": "f", "to_qualified_name": "g", "path": "p", "tier": "scip"}]
    overlay = [
        {"from_qualified_name": "f", "to_qualified_name": "g", "path": "p", "tier": "overlay"},
        {"from_qualified_name": "h", "to_qualified_name": "g", "path": "p", "tier": "overlay"},
    ]
    out = po.merge_edge_rows(master, overlay)
    assert [r["tier"] for r in out] == ["scip", "overlay"] and out[1]["from_qualified_name"] == "h"


def _overlay(**kw: Any) -> po.PrOverlay:
    ov = po.PrOverlay(pr_number=77, head_sha=HEAD, base_sha=BASE, **kw)
    return ov


def test_overlay_lookup_and_disclosure() -> None:
    fresh = po.OverlaySymbol(
        "sid-f", "DB::A::fresh", "fresh", "function", "()", "src/A.cpp", 11, 11, "added"
    )
    ov = _overlay(
        symbols=[fresh],
        edges=[
            po.OverlayEdge(
                "calls", "sid-c", "sid-f", "DB::A::caller", "DB::A::fresh", "src/A.cpp", 20, 1.0
            ),
        ],
    )
    assert ov.ok and ov.added == [fresh]
    assert ov.symbols_named("fresh") == [fresh] and ov.symbols_named("A::fresh") == [fresh]
    assert ov.symbols_named("DB::A::fresh") == [fresh] and ov.symbols_named("nope") == []
    rows = ov.edge_rows(["sid-f"], direction="callers")
    assert len(rows) == 1 and rows[0]["tier"] == "overlay" and rows[0]["from_symbol_id"] == "sid-c"
    assert ov.edge_rows(["sid-f"], direction="callees") == []
    assert ov.disclosure() == {
        "overlay": True,
        "overlay_head_sha": HEAD,
        "overlay_added": 1,
        "overlay_note": "",
    }
    off = po.PrOverlay(pr_number=77, reason=po.HEAD_NOT_IN_MIRROR)
    assert not off.ok and off.disclosure() == {
        "overlay": False,
        "overlay_head_sha": "",
        "overlay_added": 0,
        "overlay_note": "head_not_in_mirror",
    }


async def test_load_overlay_head_missing_or_no_merge_base(monkeypatch) -> None:
    async def no_head(git_dir, n):
        return ""

    monkeypatch.setattr(gitrepo, "pull_head_sha", no_head)
    ov = await po.load_pr_overlay(None, ORG, NS, git_dir="/g", pr_number=1, branch="master")
    assert ov.reason == po.HEAD_NOT_IN_MIRROR and not ov.ok

    async def head(git_dir, n):
        return HEAD

    async def no_base(git_dir, a, b):
        return ""

    monkeypatch.setattr(gitrepo, "pull_head_sha", head)
    monkeypatch.setattr(gitrepo, "merge_base", no_base)
    po.reset_cache_for_tests()
    ov = await po.load_pr_overlay(None, ORG, NS, git_dir="/g", pr_number=1, branch="master")
    assert ov.reason == po.NO_MERGE_BASE and ov.head_sha == HEAD and not ov.ok
    assert (
        await po.load_pr_overlay(None, ORG, NS, git_dir="", pr_number=1, branch="m")
    ).reason == po.NO_REPO


# ---- the handlers: impact and callers with the overlay -------------------------


def _sym(
    name: str, sid: str, path: str = "src/A.cpp", start: int = 10, end: int = 40
) -> dict[str, Any]:
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


def _edge(
    frm: str, to: str, fq: str, tq: str, *, path: str = "src/X.cpp", line: int = 5
) -> dict[str, Any]:
    return {
        "from_symbol_id": frm,
        "to_symbol_id": to,
        "from_qualified_name": fq,
        "to_qualified_name": tq,
        "path": path,
        "line": line,
        "tier": "scip",
        "edge_kind": "calls",
        "confidence": 1.0,
    }


class _Repo:
    def __init__(self) -> None:
        self.symbols = {"DB::A::old": _sym("DB::A::old", "sOld")}
        self.callers = {"sOld": [_edge("sX", "sOld", "DB::X::x", "DB::A::old")]}
        self.functions = [_sym("DB::A::old", "sOld")]

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        s = self.symbols.get(name)
        return [s] if s else []

    async def symbols_by_ids(self, org, ns, ids):
        return [s for s in self.symbols.values() if s["symbol_id"] in ids]

    async def functions_for_paths(self, org, ns, paths, *, limit=400, **kw):
        return [r for r in self.functions if r["path"] in paths]

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
        if direction != "callers":
            return []
        return [e for sid in ids for e in self.callers.get(sid, [])]

    async def call_misses_for_names(self, org, ns, names):
        return {}

    async def live_call_misses_for_names(self, org, ns, names, *, limit=2000):
        return []

    async def live_call_misses_for_path(self, org, ns, path):
        return []

    async def scip_covered_paths(self, org, ns, paths):
        return set(paths)

    async def scip_stale_paths(self, org, ns, paths):
        return {p for p in paths if p in getattr(self, "stale_paths", set())}

    async def live_paths_present(self, org, ns, paths):
        return set(paths)

    async def test_map_for(self, org, ns, paths):
        return []

    async def pr_history_for(self, org, ns, *, paths, limit):
        return []

    async def reviewers_by_pr(self, org, ns, numbers):
        return {}

    async def top_reviewers(self, org, ns, *, days, limit):
        return []


FRESH = po.OverlaySymbol(
    "sFresh", "DB::A::fresh", "fresh", "function", "()", "src/A.cpp", 50, 60, "added"
)
OLD_OV = po.OverlaySymbol(
    "sOld", "DB::A::old", "old", "function", "()", "src/A.cpp", 10, 40, "modified"
)
OVERLAY = po.PrOverlay(
    pr_number=77,
    head_sha=HEAD,
    base_sha=BASE,
    changed_paths=["src/A.cpp"],
    diff="diff --git a/src/A.cpp b/src/A.cpp\n--- a/src/A.cpp\n+++ b/src/A.cpp\n@@ -20,0 +21,2 @@ void DB::A::old()\n+  fresh();\n+\n",
    hunks={"src/A.cpp": [(21, 22)]},
    symbols=[OLD_OV, FRESH],
    edges=[
        # the PR's own call site: old() now calls fresh()
        po.OverlayEdge(
            "calls", "sOld", "sFresh", "DB::A::old", "DB::A::fresh", "src/A.cpp", 21, 1.0
        ),
        # a site the index already carries: counted once, the index's tier winning
        po.OverlayEdge("calls", "sX", "sOld", "DB::X::x", "DB::A::old", "src/X.cpp", 5, 1.0),
    ],
)


@pytest.fixture
def intel(monkeypatch: pytest.MonkeyPatch):
    from server.code.app.symbol import graph, impact

    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", ORG)
    monkeypatch.setattr(settings, "code_namespace", NS)

    async def no_dispatch(*a, **k):
        return []

    for h in (graph, impact):
        monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
        monkeypatch.setattr(h, "_dispatch_seeds", no_dispatch)
    return impact


def _patch_overlay(intel, overlay: po.PrOverlay):
    async def fake(repo, org, ns, pr_number):
        assert pr_number == 77
        return overlay

    return patch.object(intel, "_pr_overlay", fake)


async def test_impact_with_pr_number_seeds_added_functions_and_counts_pr_call_sites(intel) -> None:
    from server.code.api.models_symbol import CodeImpactInput

    repo = _Repo()
    with patch.object(intel, "CodeRepo", return_value=repo), _patch_overlay(intel, OVERLAY):
        out = await intel.handle_code_impact(CodeImpactInput(org=ORG, namespace=NS, pr_number=77))
    data = out.model_dump(mode="json")
    # the PR's range is the diff: old() is touched by hunk, fresh() is an added seed
    touched = {t["qualified_name"]: t for t in data["touched_symbols"]}
    assert touched["DB::A::old"]["via"] == "header"  # git's hunk header names it
    assert touched["DB::A::fresh"] == {
        "qualified_name": "DB::A::fresh",
        "path": "src/A.cpp",
        "start_line": 50,
        "tier": "overlay",
        "via": "added",
    }
    # hop 1: X::x from the index (once), and the PR's own site old() -> fresh()
    names = sorted((r["qualified_name"], r["tier"], r["hop"]) for r in data["impacted_symbols"])
    assert names == [("DB::X::x", "scip", 1)]  # old is a seed; a seed calling a seed is no new node
    assert data["by_hop_counts"] == {"1": 1}
    assert (
        data["overlay"] is True and data["overlay_head_sha"] == HEAD and data["overlay_added"] == 1
    )
    assert "overlay_note" in data and data["overlay_note"] == ""
    assert data["verdict"] == "" and data["epistemic"] in ("exact", "lower_bound")


async def test_impact_added_function_alone_is_a_seed_the_index_lacks(intel) -> None:
    """A brand-new file: no index row, no `not_covered` abstain — the
    overlay's rows make the path a graphed path and its function a seed."""
    from server.code.api.models_symbol import CodeImpactInput

    new = po.OverlaySymbol(
        "sNew", "DB::New::run", "run", "function", "()", "src/New.cpp", 1, 9, "added"
    )
    ov = po.PrOverlay(
        pr_number=77,
        head_sha=HEAD,
        base_sha=BASE,
        changed_paths=["src/New.cpp"],
        diff="diff --git a/src/New.cpp b/src/New.cpp\n--- /dev/null\n+++ b/src/New.cpp\n@@ -0,0 +1,9 @@\n+x\n",
        hunks={"src/New.cpp": [(1, 9)]},
        symbols=[new],
        edges=[
            po.OverlayEdge(
                "calls", "sOld", "sNew", "DB::A::old", "DB::New::run", "src/A.cpp", 30, 1.0
            )
        ],
    )
    repo = _Repo()
    with patch.object(intel, "CodeRepo", return_value=repo), _patch_overlay(intel, ov):
        out = await intel.handle_code_impact(CodeImpactInput(org=ORG, namespace=NS, pr_number=77))
    assert [t["via"] for t in out.touched_symbols] == ["added"]
    assert (
        out.verdict == ""
    )  # not `not_covered`: the overlay's row makes src/New.cpp a graphed path
    # hop 1 is the PR's own call site (overlay); hop 2 continues on the index's edges
    assert [(r["qualified_name"], r["tier"], r["hop"]) for r in out.impacted_symbols] == [
        ("DB::A::old", "overlay", 1),
        ("DB::X::x", "scip", 2),
    ]
    assert out.overlay and out.overlay_added == 1


async def test_impact_without_pr_number_is_byte_for_byte_what_it_was(intel) -> None:
    from server.code.api.models_symbol import CodeImpactInput

    repo = _Repo()
    with patch.object(intel, "CodeRepo", return_value=repo):
        out = await intel.handle_code_impact(
            CodeImpactInput(org=ORG, namespace=NS, files=["src/A.cpp"])
        )
    data = out.model_dump(mode="json")
    assert not any(k.startswith("overlay") for k in data)
    assert [t["qualified_name"] for t in data["touched_symbols"]] == ["DB::A::old"]


async def test_impact_pr_head_not_in_mirror_discloses_and_answers_from_the_index(intel) -> None:
    from server.code.api.models_symbol import CodeImpactInput

    repo = _Repo()
    off = po.PrOverlay(pr_number=77, reason=po.HEAD_NOT_IN_MIRROR)
    with patch.object(intel, "CodeRepo", return_value=repo), _patch_overlay(intel, off):
        out = await intel.handle_code_impact(
            CodeImpactInput(org=ORG, namespace=NS, pr_number=77, files=["src/A.cpp"])
        )
    data = out.model_dump(mode="json")
    assert data["overlay"] is False and data["overlay_note"] == "head_not_in_mirror"
    assert [t["qualified_name"] for t in data["touched_symbols"]] == ["DB::A::old"]


async def test_callers_of_an_added_function_resolve_through_the_overlay(intel) -> None:
    from server.code.api.models_symbol import CodeCallersInput
    from server.code.app.symbol import graph

    repo = _Repo()
    with patch.object(graph, "CodeRepo", return_value=repo), _patch_overlay(graph, OVERLAY):
        out = await graph.handle_code_callers(
            CodeCallersInput(
                org=ORG, namespace=NS, name="DB::A::fresh", pr_number=77, include_ambiguous=False
            )
        )
    data = out.model_dump(mode="json")
    assert data["seed_symbols"] == ["DB::A::fresh"]
    assert [(e["from_symbol"], e["tier"], e["line"]) for e in data["edges"]] == [
        ("DB::A::old", "overlay", 21)
    ]
    assert data["overlay"] is True and data["overlay_head_sha"] == HEAD
    # the same name without the PR is not found — the index has no row
    with patch.object(graph, "CodeRepo", return_value=repo):
        from server.core.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await graph.handle_code_callers(
                CodeCallersInput(
                    org=ORG, namespace=NS, name="DB::A::fresh", include_ambiguous=False
                )
            )


async def test_callers_without_pr_number_is_byte_for_byte_what_it_was(intel) -> None:
    from server.code.api.models_symbol import CodeCallersInput
    from server.code.app.symbol import graph

    repo = _Repo()
    with patch.object(graph, "CodeRepo", return_value=repo):
        out = await graph.handle_code_callers(
            CodeCallersInput(org=ORG, namespace=NS, name="DB::A::old", include_ambiguous=False)
        )
    data = out.model_dump(mode="json")
    assert not any(k.startswith("overlay") for k in data)
    assert set(data) == {
        "edges",
        "seed_symbols",
        "ambiguous",
        "candidates",
        "dispatch_seeds",
        "note",
        "truncated",
        "counts_floor",
        "unresolved_calls",
        "ambiguous_calls",
        "epistemic",
        "causes",
        "by_hop_counts",
        "trace_run_id",
    }


# ---- the review brief's open path ------------------------------------------------


async def test_review_brief_open_requires_pr_number_and_keys_by_head(monkeypatch) -> None:
    import server.code.app.review_brief as h
    from server.code.api.models_review import CodeReviewBriefInput
    from server.core.errors import ValidationError

    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", ORG)
    monkeypatch.setattr(settings, "code_namespace", NS)
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
    with pytest.raises(ValidationError):
        await h.handle_code_review_brief(CodeReviewBriefInput(org=ORG, namespace=NS, open=True))

    reads: list[tuple[int, str]] = []

    class _BriefRepo:
        async def review_brief(self, org, ns, *, pr_number, merge_sha):
            reads.append((pr_number, merge_sha))
            return None

    async def head(org, ns, n):
        return HEAD, ""

    payload = {
        "v": 1,
        "pr_number": 77,
        "merge_sha": "",
        "title": "t",
        "built_at": "x",
        "indexed_commit": "",
        "build_ms": 3,
        "decisions": {},
        "overlay": True,
        "head_sha": HEAD,
        "base_sha": BASE,
        "sections": {
            "symbols": {
                "status": "served",
                "verdict": "touched",
                "epistemic": "lower_bound",
                "causes": [],
                "rows": [{"qualified_name": "f", "via": "added"}],
                "rows_total": 1,
                "tokens": 10,
                "truncated": False,
            }
        },
    }

    async def build(repo, org, ns, *, pr_number, head_sha, store):
        assert head_sha == HEAD and store is False  # `eval-*` consumers never fill the cache
        return payload

    monkeypatch.setattr(h, "_open_head", head)
    monkeypatch.setattr(h, "_build_open", build)
    with patch.object(h, "CodeRepo", return_value=_BriefRepo()):
        out = await h.handle_code_review_brief(
            CodeReviewBriefInput(
                org=ORG, namespace=NS, pr_number=77, open=True, tier="trivial", consumer="eval-test"
            )
        )
    assert reads == [(77, HEAD)]  # the point read is keyed by the head, not a merge commit
    data = out.model_dump(mode="json")
    assert data["source"] == "built" and data["merge_sha"] == ""
    assert (
        data["overlay"] is True and data["overlay_head_sha"] == HEAD and data["overlay_added"] == 1
    )

    # a head the mirror lacks: missing, with the reason
    async def no_head(org, ns, n):
        return "", po.HEAD_NOT_IN_MIRROR

    monkeypatch.setattr(h, "_open_head", no_head)
    with patch.object(h, "CodeRepo", return_value=_BriefRepo()):
        out = await h.handle_code_review_brief(
            CodeReviewBriefInput(org=ORG, namespace=NS, pr_number=78, open=True)
        )
    data = out.model_dump(mode="json")
    assert data["source"] == "missing" and data["overlay"] is False
    assert data["overlay_note"] == "head_not_in_mirror"


async def test_review_brief_merged_read_is_byte_for_byte_what_it_was(monkeypatch) -> None:
    import server.code.app.review_brief as h
    from server.code.api.models_review import CodeReviewBriefInput

    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", ORG)
    monkeypatch.setattr(settings, "code_namespace", NS)
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)

    class _BriefRepo:
        async def review_brief(self, org, ns, *, pr_number, merge_sha):
            return None

    async def miss(repo, org, ns, *, pr_number, merge_sha, store):
        return None, "missing"

    monkeypatch.setattr(h, "_build_on_miss", miss)
    with patch.object(h, "CodeRepo", return_value=_BriefRepo()):
        out = await h.handle_code_review_brief(
            CodeReviewBriefInput(org=ORG, namespace=NS, pr_number=5)
        )
    data = out.model_dump(mode="json")
    assert not any(k.startswith("overlay") for k in data)


def test_touched_wire_keeps_every_added_seed_behind_the_index_rows() -> None:
    import server.code.app.symbol.impact as h

    rows = [
        {"qualified_name": f"f{i}", "path": "p", "start_line": i, "tier": "scip", "via": "hunk"}
        for i in range(250)
    ]
    rows += [
        {"qualified_name": f"a{i}", "path": "q", "start_line": i, "tier": "overlay", "via": "added"}
        for i in range(3)
    ]
    wire = h._touched_wire(rows)
    assert len(wire) == 203 and [r["qualified_name"] for r in wire[-3:]] == ["a0", "a1", "a2"]
    assert h._touched_wire(rows[:250]) == wire[:200]  # a merged input: the first 200, as before


def test_overlay_call_puts_the_pr_on_the_impact_read_only() -> None:
    from server.code.app.index import review_brief_build

    seen: list[tuple[str, dict[str, Any]]] = []

    async def inner(op, body):
        seen.append((op, body))
        return {}

    import asyncio

    call = review_brief_build.overlay_call(77, inner)
    asyncio.run(call("code.impact", {"files": ["a"]}))
    asyncio.run(call("code.symbol_history", {"name": "f"}))
    assert seen == [
        ("code.impact", {"files": ["a"], "pr_number": 77}),
        ("code.symbol_history", {"name": "f"}),
    ]


def test_symbols_section_carries_via_added_only_for_overlay_seeds() -> None:
    import asyncio

    from server.code.app.index import review_brief as rb
    from server.code.app.index import review_brief_build

    inp = rb.BriefInputs(
        org=ORG, ns=NS, pr_number=1, merge_sha="", title="", changed_paths=[], diff=""
    )
    impact = {
        "touched_symbols": [
            {"qualified_name": "a", "path": "p", "start_line": 1, "tier": "scip", "via": "hunk"},
            {
                "qualified_name": "b",
                "path": "p",
                "start_line": 2,
                "tier": "overlay",
                "via": "added",
            },
        ],
        "verdict": "touched",
        "epistemic": "lower_bound",
        "causes": [],
        "by_hop_counts": {},
    }
    sec = asyncio.run(
        review_brief_build._symbols_section(impact, impact["touched_symbols"], inp, None)
    )
    rows = {r["qualified_name"]: r for r in sec["rows"]}
    assert "via" not in rows["a"] and rows["b"]["via"] == "added"


# ---- the workers -----------------------------------------------------------------


async def test_ingest_fetches_only_open_pr_heads_the_mirror_lacks(monkeypatch) -> None:
    from server.code.app.index import pass_budget as ingest_budget
    from server.code.app.index import pass_fetch as ingest_fetch

    monkeypatch.setattr(settings, "code_git_fetch_pr_heads", True)
    monkeypatch.setattr(settings, "code_git_pr_head_batch", 2)
    ingest_fetch._PR_HEADS_FETCHED_AT.clear()

    class _R:
        state: list[Any] = []

        async def open_pr_numbers(self, org, ns, *, limit):
            return [5, 4, 3, 2]

        async def set_state(self, org, ns, key, value, detail=""):
            self.state.append((key, value, detail))

    sent: list[list[int]] = []

    async def remote(git_dir, remote, *, token=""):
        return {5: "5" * 40, 4: "4" * 40, 3: "3" * 40, 2: "2" * 40}

    async def local(git_dir):
        return {5: "5" * 40}  # 5 is current; 4, 3, 2 are behind; the batch takes two

    async def fetch(git_dir, remote, numbers, *, token=""):
        sent.append(list(numbers))
        return len(numbers)

    monkeypatch.setattr(gitrepo, "remote_pull_heads", remote)
    monkeypatch.setattr(gitrepo, "local_pull_heads", local)
    monkeypatch.setattr(gitrepo, "fetch_pull_heads", fetch)

    @dataclass
    class _Cfg:
        org: str = ORG
        namespace: str = NS
        git_remote: str = "https://r"
        github_token: str = ""
        label: str = "o/n"
        git_fetch_pr_heads: bool = True

    res = ingest_budget.CodeIngestResult()
    assert await ingest_fetch.fetch_pr_heads(_Cfg(), "/g", _R(), res) == 2
    assert sent == [[4, 3]] and res.errors == []
    assert _R.state[-1][0] == "pr_heads_fetch" and "behind=3" in _R.state[-1][2]
    # once per mirror per pass: the second entry sharing the mirror skips
    assert await ingest_fetch.fetch_pr_heads(_Cfg(namespace="n2"), "/g", _R(), res) == 0
    # the per-entry switch
    ingest_fetch._PR_HEADS_FETCHED_AT.clear()
    assert await ingest_fetch.fetch_pr_heads(_Cfg(git_fetch_pr_heads=False), "/g", _R(), res) == 0


async def test_pr_history_fills_heads_from_the_mirror_and_carries_them_forward(monkeypatch) -> None:
    from server.code.workers import pr_history as w

    monkeypatch.setattr(settings, "code_gh_items_enabled", True)
    monkeypatch.setattr(settings, "code_gh_items_head_fill_batch", 10)
    monkeypatch.setattr(settings, "code_gh_items_head_github_batch", 0)

    rows = [
        {
            "number": 7,
            "state": "open",
            "title": "seven",
            "labels": [],
            "opened_at": None,
            "closed_at": None,
            "merged_at": None,
            "updated_at": None,
            "head_sha": "",
            "base_sha": "",
            "files": [],
        },
        {
            "number": 8,
            "state": "open",
            "title": "eight",
            "labels": [],
            "opened_at": None,
            "closed_at": None,
            "merged_at": None,
            "updated_at": None,
            "head_sha": HEAD,
            "base_sha": BASE,
            "files": ["src/B.cpp"],
        },  # filled from this head already: skipped
        {
            "number": 9,
            "state": "open",
            "title": "nine",
            "labels": [],
            "opened_at": None,
            "closed_at": None,
            "merged_at": None,
            "updated_at": None,
            "head_sha": "",
            "base_sha": "",
            "files": [],
        },  # the mirror has no head for 9; no GitHub budget
    ]
    written: list[list[dict[str, Any]]] = []

    class _R:
        async def open_prs_for_head_fill(self, org, ns, *, limit):
            return rows

        async def insert_rows(self, table, new_rows):
            written.append(new_rows)

        async def pr_heads_by_number(self, org, ns, numbers):
            return {8: {"head_sha": HEAD, "base_sha": BASE, "files": ["src/B.cpp"]}}

    async def local(git_dir):
        return {7: "7" * 40, 8: HEAD}

    async def mb(git_dir, a, b):
        assert b == "refs/heads/master"
        return BASE

    async def names(git_dir, base, head, *, limit=2000):
        return ["src/A.cpp", "tests/t.sql"]

    monkeypatch.setattr(gitrepo, "local_pull_heads", local)
    monkeypatch.setattr(gitrepo, "merge_base", mb)
    monkeypatch.setattr(gitrepo, "diff_name_only", names)
    monkeypatch.setattr(w, "_isdir", lambda p: True)

    @dataclass
    class _Cfg:
        org: str = ORG
        namespace: str = NS
        git_dir: str = "/g"
        git_branch: str = "master"
        github_token: str = ""
        repo: str = "o/r"
        label: str = "o/n"

    res = w.CodePrHistoryResult()
    await w._fill_pr_heads(_Cfg(), _R(), res)
    assert res.heads_filled == 1 and len(written) == 1
    (row,) = written[0]
    assert (row["number"], row["head_sha"], row["base_sha"], row["files"]) == (
        7,
        "7" * 40,
        BASE,
        ["src/A.cpp", "tests/t.sql"],
    )
    assert row["is_pr"] is True and row["title"] == "seven" and row["state"] == "open"
    # the items sync carries a fill forward instead of blanking it
    carried = await w._carried_heads(_R(), ORG, NS, [7, 8])
    assert w._head_cols(carried.get(8)) == {
        "head_sha": HEAD,
        "base_sha": BASE,
        "files": ["src/B.cpp"],
    }
    assert w._head_cols(carried.get(7)) == {}


def test_settings_and_repo_entry_defaults() -> None:
    from server.code.domain.index.repos import RepoConfig

    assert settings.code_git_fetch_pr_heads is True and settings.code_git_pr_head_batch == 200
    assert RepoConfig.__dataclass_fields__["git_fetch_pr_heads"].default is True
