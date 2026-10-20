"""code.backport_gap(exact: true): the fix's own lines on
every branch. The diff is parsed into hunk groups by (path, declaration),
tests and deleted files never count; the line ratios make `gap` exact and
the fixed state without a record `not_affected` exact, a pure addition never
reads present, the token bag is disclosed and decides nothing; the record's
verdicts and every `unknown` stand; the deadline is `symbol_only(deadline)`;
and with the flag unset the payload is byte for byte the non-exact one."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

import server.code.app.backport as h
import server.tracker.app.binding as binding
from server.code.api.models_history import CodeBackportGapInput
from server.settings import settings
from server.tracker.domain import backport as bp
from server.tracker.domain import patch_presence as pp
from server.tracker.domain.errors import TrackerUnavailable

ORG, NS, QA = "clickhouse", "code-clickhouse", "tracker-clickhouse"
BRANCHES = {f"code-clickhouse-26-{i}": f"26.{i}" for i in range(1, 9)}
SRC = "src/DataTypes/IDataType.cpp"
SYM = "IDataType::getSubcolumnInfo"

PRE = """std::unique_ptr<IDataType::SubcolumnInfo> IDataType::getSubcolumnInfo(const String & name) const
{
    for (size_t i = 0; i < subpath.size(); ++i)
    {
        size_t prefix_len = i + 1;
        if (!subpath[i].visited && ISerialization::hasSubcolumnForPath(subpath, prefix_len))
        {
            auto name = ISerialization::getSubcolumnNameForStream(subpath, prefix_len, false, initial_array_level);
            /// Create data from path only if it's requested subcolumn.
            if (name == requested_name)
                return makeSubcolumnInfo(subpath, prefix_len);
        }
    }
    return nullptr;
}"""
POST = PRE.replace(
    "getSubcolumnNameForStream(subpath, prefix_len, false, initial_array_level);",
    "getSubcolumnNameForStream(subpath, prefix_len, initial_array_level);\n"
    "            auto cache_key = SubstreamsCacheKey::fromPath(subpath, prefix_len, requested_name);\n"
    "            if (auto cached = substreams_cache.lookupElement(cache_key))\n"
    "                return cached->subcolumn_info;",
).replace(
    "if (name == requested_name)",
    "if (name == requested_name && !substreams_cache.contains(cache_key))",
)
# the same fix, refactored on the branch: the lookup and the guard are one helper
REFACTORED = PRE.replace(
    "getSubcolumnNameForStream(subpath, prefix_len, false, initial_array_level);",
    "getSubcolumnNameForStream(subpath, prefix_len, initial_array_level);\n"
    "            const auto cache_key = SubstreamsCacheKey::fromPath(subpath, prefix_len, requested_name);\n"
    "            if (auto * cached = lookupCached(substreams_cache, cache_key))\n"
    "                return cached->subcolumn_info;",
).replace(
    "if (name == requested_name)",
    "if (name == requested_name && !cachedAlready(substreams_cache, cache_key))",
)
# a branch whose function shares neither image
REWRITTEN = """std::unique_ptr<IDataType::SubcolumnInfo> IDataType::getSubcolumnInfo(const String & name) const
{
    return resolveSubcolumnInfoByName(name, subpath, initial_array_level);
}"""

DIFF_SRC = f"""a/{SRC} b/{SRC}
index 2a12605cce4b..138be2f294e8 100644
--- a/{SRC}
+++ b/{SRC}
@@ -180,7 +180,10 @@ std::unique_ptr<IDataType::SubcolumnInfo> IDataType::getSubcolumnInfo(
             size_t prefix_len = i + 1;
             if (!subpath[i].visited && ISerialization::hasSubcolumnForPath(subpath, prefix_len))
             {{
-                auto name = ISerialization::getSubcolumnNameForStream(subpath, prefix_len, false, initial_array_level);
+                auto name = ISerialization::getSubcolumnNameForStream(subpath, prefix_len, initial_array_level);
+                auto cache_key = SubstreamsCacheKey::fromPath(subpath, prefix_len, requested_name);
+                if (auto cached = substreams_cache.lookupElement(cache_key))
+                    return cached->subcolumn_info;
                 /// Create data from path only if it's requested subcolumn.
-                if (name == requested_name)
+                if (name == requested_name && !substreams_cache.contains(cache_key))
                     return makeSubcolumnInfo(subpath, prefix_len);
"""
DIFF_TEST = """a/tests/queries/0_stateless/04927_x.sql b/tests/queries/0_stateless/04927_x.sql
new file mode 100644
--- /dev/null
+++ b/tests/queries/0_stateless/04927_x.sql
@@ -0,0 +1,2 @@
+SELECT 1 SETTINGS enable_substreams_cache = 1;
+SELECT 2;
"""
DIFF_DELETED = """a/src/Old.cpp b/src/Old.cpp
deleted file mode 100644
--- a/src/Old.cpp
+++ /dev/null
@@ -1,3 +0,0 @@
-void old_function_body_here(int x)
-{
-}
"""
DIFF_RENAMED = """a/src/Common/A.h b/src/Common/Renamed.h
similarity index 90%
rename from src/Common/A.h
rename to src/Common/Renamed.h
--- a/src/Common/A.h
+++ b/src/Common/Renamed.h
@@ -10,3 +10,4 @@ class RenamedHolder
     int field_one_value;
+    int field_two_value_added_here;
     int field_three_value;
"""
SECTIONS = [DIFF_SRC, DIFF_TEST, DIFF_DELETED, DIFF_RENAMED]


def _chunk(path: str, scope: str, content: str, **kw: Any) -> dict[str, Any]:
    return {
        "path": path,
        "scope": scope,
        "kind": "function",
        "start_line": 1,
        "end_line": 20,
        "content": content,
        **kw,
    }


# ---- the hunks -----------------------------------------------------------------------


def test_sections_become_hunk_groups_without_tests_or_deleted_files() -> None:
    groups = pp.parse_sections(SECTIONS)
    assert [(g.path, g.symbol, g.hunks) for g in groups] == [
        (SRC, SYM, 1),
        ("src/Common/Renamed.h", "RenamedHolder", 1),
    ]
    g = groups[0]
    assert len(g.added) == 5 and len(g.removed) == 2 and len(g.context) == 5
    assert g.added[0] == (
        "auto name = ISerialization::getSubcolumnNameForStream("
        "subpath, prefix_len, initial_array_level);"
    )
    added, removed = g.informative()
    assert len(added) == 5 and len(removed) == 2
    assert pp.paths_of(groups) == [SRC, "src/Common/Renamed.h"]
    assert pp.symbols_of(groups) == [SYM, "RenamedHolder"]
    assert pp.parse_sections([]) == [] and pp.parse_sections([""]) == []


def test_normalisation_and_informative_lines() -> None:
    assert pp.normalise("   auto   x =\t1;  ") == "auto x = 1;"
    g = pp.HunkGroup(
        path=SRC,
        symbol=SYM,
        added=["}", "else", "return;", "auto value = compute(x);"],
        removed=["auto value = compute(x);", "int y = 0;"],
    )
    added, removed = g.informative()
    # trivial lines are not evidence; a line moved verbatim counts on neither side
    assert added == [] and removed == ["int y = 0;"]


def test_needles_are_long_whitespace_free_runs_from_both_images() -> None:
    groups = pp.parse_sections(SECTIONS)
    ns = pp.needles(groups)
    assert ns and all(" " not in n and len(n) >= pp.MIN_NEEDLE_CHARS for n in ns)
    assert any("getSubcolumnNameForStream(subpath," in n for n in ns)  # both images
    assert any("SubstreamsCacheKey::fromPath" in n for n in ns)  # added
    assert len(ns) <= 2 * pp.NEEDLE_LIMIT and len(set(ns)) == len(ns)
    assert pp.skip_pattern().startswith("^a/(?:docs|utils|ci)/|")


def test_token_bag_is_a_multiset_containment() -> None:
    a = pp.tokens('auto x = foo(bar, "s t", 42); foo(x);')
    assert a["foo"] == 2 and a['"s t"'] == 1 and a["42"] == 1
    assert pp.containment(pp.tokens("foo foo bar"), pp.tokens("foo bar baz")) == pytest.approx(
        2 / 3
    )
    assert pp.containment(pp.tokens(""), pp.tokens("x")) == 0.0


# ---- the decision ---------------------------------------------------------------------


def _groups() -> list[pp.HunkGroup]:
    return [g for g in pp.parse_sections([DIFF_SRC])]


def test_post_image_on_the_branch_is_present_exact() -> None:
    d = pp.decide(_groups(), [_chunk(SRC, "DB::" + SYM, POST)])
    assert (d.mode, d.verdict) == ("exact", "present")
    assert d.added_present == d.added_total == 5 and d.removed_gone == d.removed_total == 2
    assert d.functions_matched == 1 and d.chunks_read == 1 and d.hunks == 1
    block = d.as_dict()
    assert block["mode"] == "exact" and "new_tokens" not in block and "reason" not in block


def test_pre_image_on_the_branch_is_absent_exact() -> None:
    d = pp.decide(_groups(), [_chunk(SRC, "DB::" + SYM, PRE)])
    assert (d.mode, d.verdict) == ("exact", "absent")
    assert d.added_present == 0 and d.removed_gone == 0
    # the lines decided before the bag was consulted: no token counts on the block
    assert d.new_tokens == 0 and "new_tokens" not in d.as_dict()


def test_pure_addition_never_reads_present_by_lines() -> None:
    # 13 record-less "covered" claims in the replay came from ≤ 2 generic added lines
    g = pp.HunkGroup(
        path=SRC,
        symbol=SYM,
        hunks=1,
        added=["if (node_acls.empty())", "return true_result_value;", "auto value = compute(x);"],
    )
    body = "void f()\n{\n    if (node_acls.empty())\n    return true_result_value;\n    auto value = compute(x);\n}"
    d = pp.decide([g], [_chunk(SRC, SYM, body)])
    assert (d.mode, d.verdict, d.reason) == (
        "symbol_only",
        "",
        "undecided",
    ) and d.added_present == 3
    # … while a pure addition that is ABSENT is a gap: nothing of the fix arrived
    d2 = pp.decide([g], [_chunk(SRC, SYM, "void f()\n{\n    other_call(y);\n}")])
    assert (d2.mode, d2.verdict) == ("exact", "absent")


def test_refactored_backport_is_disclosed_by_the_bag_but_undecided() -> None:
    d = pp.decide(_groups(), [_chunk(SRC, "DB::" + SYM, REFACTORED)])
    assert (d.mode, d.verdict, d.reason) == ("symbol_only", "", "undecided")
    assert d.added_present == 2 and d.removed_gone == 2  # two lines survived the refactor verbatim
    assert d.new_tokens >= pp.MIN_NEW_TOKENS and d.new_token_overlap >= pp.TOKEN_OVERLAP_MIN
    assert d.similar is True and d.as_dict()["similar"] is True
    assert d.as_dict()["new_token_overlap"] == round(d.new_token_overlap, 3)
    d2 = pp.decide(_groups(), [_chunk(SRC, "DB::" + SYM, REWRITTEN)])
    assert (d2.mode, d2.verdict, d2.reason) == ("symbol_only", "", "undecided")
    assert d2.similar is False and d2.new_token_overlap < pp.TOKEN_OVERLAP_MIN


def test_file_level_lines_decide_without_a_matched_function() -> None:
    # the chunk was fetched by a needle, not by scope: lines still decide
    d = pp.decide(_groups(), [_chunk(SRC, "", POST, kind="other")])
    assert (d.mode, d.verdict, d.functions_matched) == ("exact", "present", 0)
    d2 = pp.decide(_groups(), [_chunk(SRC, "", PRE, kind="other")])
    assert (d2.mode, d2.verdict) == ("exact", "absent")
    # a hunk that runs past its function: the lines are tested against every
    # chunk of the path, so the new function's chunk counts
    d3 = pp.decide(
        _groups(), [_chunk(SRC, "DB::" + SYM, PRE), _chunk(SRC, "DB::IDataType::other", POST)]
    )
    assert (
        (d3.mode, d3.verdict) == ("symbol_only", "")
        and d3.added_present == 5
        and d3.removed_gone == 0
    )


def test_signature_drops_context_and_keywords() -> None:
    g = pp.parse_sections([DIFF_SRC])[0]
    assert g.context and g.context[0] == "size_t prefix_len = i + 1;"
    ctx = pp.Counter()
    for ln in g.context:
        ctx.update(pp.tokens(ln))
    added = pp.Counter()
    for ln in g.added:
        added.update(pp.tokens(ln))
    sig = pp._signature(added, ctx)
    assert (
        "subpath" not in sig and "prefix_len" not in sig and "auto" not in sig and "if" not in sig
    )
    assert sig["cache_key"] == 3 and sig["SubstreamsCacheKey"] == 1
    # a flipped flag is often the fix: true / false / nullptr are kept
    assert pp._signature(pp.tokens("return false;"), pp.Counter()) == pp.Counter({"false": 1})


def test_no_hunks_no_chunks_and_pure_deletions() -> None:
    assert pp.decide([], [_chunk(SRC, SYM, PRE)]).reason == "no_hunks"
    d = pp.decide(_groups(), [])
    assert (d.mode, d.reason, d.added_total) == ("symbol_only", "no_chunks", 5)
    deletion = pp.HunkGroup(
        path=SRC,
        symbol=SYM,
        hunks=1,
        removed=["auto name = compute_the_name(subpath);", "checkTheName(name, subpath);"],
    )
    gone = pp.decide([deletion], [_chunk(SRC, SYM, "void f()\n{\n    return;\n}")])
    assert (gone.mode, gone.verdict) == ("exact", "present")
    still = pp.decide(
        [deletion],
        [
            _chunk(
                SRC,
                SYM,
                "void f()\n{\n    auto name = compute_the_name(subpath);\n"
                "    checkTheName(name, subpath);\n}",
            )
        ],
    )
    assert (still.mode, still.verdict) == ("exact", "absent")
    # one informative line is not an exact claim either way
    one = pp.HunkGroup(
        path=SRC, symbol=SYM, hunks=1, added=["auto name = compute_the_name(subpath);"]
    )
    d1 = pp.decide(
        [one], [_chunk(SRC, SYM, "void f()\n{\n    auto name = compute_the_name(subpath);\n}")]
    )
    assert (d1.mode, d1.verdict, d1.reason) == ("symbol_only", "", "too_few_lines")
    only_comments = pp.HunkGroup(
        path=SRC, symbol=SYM, hunks=1, added=["/// a comment line that is long enough"]
    )
    assert pp.decide([only_comments], [_chunk(SRC, SYM, "x")]).reason == "no_informative_lines"


def test_pre_image_intact_is_a_gap_whatever_matched_by_chance() -> None:
    # 12 removed lines all still there; 6 of 12 added lines happen to match
    removed = [f"auto value_{i} = compute_{i}(input_{i});" for i in range(12)]
    added = [f"auto value_{i} = compute_{i}(input_{i}, extra);" for i in range(6)] + [
        f"auto shared_{i} = common_helper(input_{i});" for i in range(6)
    ]
    g = pp.HunkGroup(path=SRC, symbol=SYM, hunks=1, added=added, removed=removed)
    body = "void f()\n{\n" + "\n".join(removed + added[6:]) + "\n}"
    d = pp.decide([g], [_chunk(SRC, SYM, body)])
    assert (
        (d.mode, d.verdict) == ("exact", "absent") and d.added_present == 6 and d.removed_gone == 0
    )
    # comments and test paths never count
    assert pp.is_test_path("src/Storages/MergeTree/tests/gtest_key_condition.cpp")
    assert pp.is_test_path("tests/queries/0_stateless/1.sql") and not pp.is_test_path(
        "src/Core/tests_helper.cpp"
    )
    assert not pp.is_informative("/// A lazy proxy would hide the type from the checks")
    assert not pp.is_informative("# a python comment that is long") and pp.is_informative(
        "#include <Core/Names.h>"
    )


def test_refine_touches_only_what_presence_floored() -> None:
    pick = {"pr_number": 7, "merge_sha": "e" * 40}

    def row(verdict: str, basis: str) -> bp.BranchVerdict:
        return bp.BranchVerdict(
            namespace="code-clickhouse-26-4",
            branch="26.4",
            verdict=verdict,
            epistemic="lower_bound" if verdict in ("gap", "not_affected") else "exact",
            basis=basis,
            cherry_pick=dict(pick) if verdict == "gap" else {},
        )

    present = pp.decide(_groups(), [_chunk(SRC, "DB::" + SYM, POST)])
    absent = pp.decide(_groups(), [_chunk(SRC, "DB::" + SYM, PRE)])
    undecided = pp.decide(_groups(), [_chunk(SRC, "DB::" + SYM, REFACTORED)])

    v = row("gap", "symbol")
    pp.refine(v, present, pick)
    assert (v.verdict, v.epistemic, v.basis, v.cherry_pick) == ("not_affected", "exact", "hunk", {})
    assert v.presence["patch"]["mode"] == "exact" and "already on 26.4" in v.evidence[-1]
    v = row("not_affected", "symbol")
    pp.refine(v, absent, pick)
    assert (v.verdict, v.epistemic, v.basis, v.cherry_pick) == ("gap", "exact", "hunk", pick)
    v = row("gap", "symbol")
    pp.refine(v, undecided, pick)
    assert (v.verdict, v.epistemic, v.basis, v.cherry_pick) == (
        "gap",
        "lower_bound",
        "symbol",
        pick,
    )
    assert v.presence["patch"]["reason"] == "undecided" and v.presence["patch"]["similar"] is True
    for verdict, basis in (("covered", "backport_pr"), ("pending", "backport_pr"), ("unknown", "")):
        v = row(verdict, basis)
        pp.refine(v, absent, pick)
        assert (v.verdict, v.basis) == (verdict, basis) and v.presence["patch"]["mode"] == "exact"


# ---- the handler ----------------------------------------------------------------------


class _Cfg:
    def __init__(self, ns: str, branch: str, repo: str = "ClickHouse/ClickHouse") -> None:
        self.namespace, self.git_branch, self.repo, self.org = ns, branch, repo, ORG


class _Repo:
    def __init__(self, chunks: dict[str, list[dict[str, Any]]], *, slow: str = "") -> None:
        self.chunks, self.slow = chunks, slow
        self.calls: list[tuple[str, str]] = []
        self.patch_args: list[tuple[list[str], list[str], list[str]]] = []

    async def pr_facts(self, org, ns, pr_number):
        return {
            "pr_number": pr_number,
            "title": "Fix substreams cache key collision",
            "files": [SRC],
            "labels": ["pr-must-backport"],
            "merge_commit_sha": b"e" * 40,
            "merged_at": datetime(2026, 9, 15, 23, 11),
        }

    async def backport_prs_titled(self, org, ns, upstream, *, limit=8):
        return []

    async def symbol_names_present(self, org, ns, names):
        self.calls.append(("names", ns))
        return {n: [n] for n in names}

    async def live_paths_present(self, org, ns, paths):
        return set(paths)

    async def chunks_for_patch(self, org, ns, paths, symbols, needles, *, limit=400):
        self.calls.append(("patch", ns))
        self.patch_args.append((list(paths), list(symbols), list(needles)))
        if ns == self.slow:
            await asyncio.sleep(0.3)
        return list(self.chunks.get(ns, []))


class _Source:
    def __init__(
        self, *, sections: list[str], backports: list | None = None, down: bool = False
    ) -> None:
        self.sections, self.backports, self.down = sections, backports or [], down
        self.calls: list[str] = []

    def repos(self):
        return ["ClickHouse/ClickHouse"]

    async def pr_change(self, org, ns, number):
        self.calls.append("pr_change")
        if self.down:
            raise TrackerUnavailable("breaker OPEN")
        return {
            "pr_number": number,
            "title": "Fix substreams cache key collision",
            "url": "u",
            "author": "a",
            "base_branch": "master",
            "minor_version": "26.9",
            "is_merged": 1,
            "is_backport_pr": 0,
            "is_cherrypick_pr": 0,
            "is_reverted": 0,
            "upstream_pr_number": 0,
            "backport_required": 1,
            "changelog_category": "bug_fix",
            "src_components": [],
            "files": [SRC],
            "merged_at": datetime(2026, 9, 15, 23, 11),
            "hunk_contexts": [
                "std::unique_ptr<IDataType::SubcolumnInfo> IDataType::getSubcolumnInfo("
            ],
        }

    async def backport_prs_of(self, org, ns, number):
        self.calls.append("backport_prs_of")
        return list(self.backports)

    async def pr_diff_sections(self, org, ns, number):
        self.calls.append("pr_diff_sections")
        return list(self.sections)


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Any:
    import server.code.app.search_fanout as fanout
    import server.code.domain.index.repos as repos
    import server.tracker.store.source as qs

    monkeypatch.setattr(
        repos,
        "configured_repos",
        lambda: [_Cfg(NS, "master"), *(_Cfg(n, v) for n, v in BRANCHES.items())],
    )
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)

    async def _authorized(org, code_ns, *, hides_answer=True):
        return QA

    monkeypatch.setattr(binding, "authorized_tracker_namespace", _authorized)

    async def _as_of(repo, org, ns):
        return {"commit": f"{ns}-head", "status": "fresh"}

    monkeypatch.setattr(h, "index_as_of", _as_of)
    authz: list[list[str]] = []

    async def _authorize(org, namespaces, *, op="code.search"):
        authz.append(list(namespaces))

    monkeypatch.setattr(fanout, "authorize", _authorize)
    holder: dict[str, Any] = {"authz": authz}

    def _use(source: _Source, repo: _Repo) -> dict[str, Any]:
        holder["src"], holder["repo"] = source, repo
        return holder

    monkeypatch.setattr(qs, "TrackerSource", lambda org, ns: holder["src"])
    monkeypatch.setattr(h, "CodeRepo", lambda: holder["repo"])
    return _use


def _bp(n: int, branch: str, state: str) -> dict[str, Any]:
    return {
        "pr_number": n,
        "title": f"Backport #115670 to {branch}: Fix",
        "url": f"u/{n}",
        "base_branch": branch,
        "minor_version": branch,
        "state": state,
        "merged_at": datetime(2026, 9, 16),
        "source": "qa",
    }


CHUNKS = {
    "code-clickhouse-26-8": [_chunk(SRC, "DB::" + SYM, POST)],  # a backport the record misses
    "code-clickhouse-26-7": [_chunk(SRC, "DB::" + SYM, REFACTORED)],  # refactored backport
    "code-clickhouse-26-6": [_chunk(SRC, "DB::" + SYM, PRE)],  # the bug, exactly
    "code-clickhouse-26-5": [_chunk(SRC, "DB::" + SYM, REWRITTEN)],  # neither image
    "code-clickhouse-26-4": [_chunk(SRC, "DB::" + SYM, POST)],  # covered by the record anyway
    "code-clickhouse-26-3": [],  # nothing read
}


@pytest.mark.asyncio
async def test_unset_flag_answers_without_the_patch_stage(wired) -> None:
    src, repo = _Source(sections=[DIFF_SRC]), _Repo(CHUNKS)
    holder = wired(src, repo)
    out = await h.handle_code_backport_gap(
        CodeBackportGapInput(org=ORG, namespace=NS, pr_number=115670)
    )
    assert "pr_diff_sections" not in src.calls and not any(c[0] == "patch" for c in repo.calls)
    assert "patch" not in out.fix and set(out.stage_ms) == {"fix", "presence"}
    assert all("patch" not in b["presence"] for b in out.branches)
    assert all(b["basis"] not in ("hunk", "token_bag") for b in out.branches)
    assert out.verdict == "gap" and len(holder["authz"]) == 1
    assert set(CodeBackportGapInput.model_fields) >= {
        "exact",
        "pr_number",
        "issue_number",
        "branches",
        "max_symbols",
    }
    assert CodeBackportGapInput(org=ORG, namespace=NS).exact is False


@pytest.mark.asyncio
async def test_exact_reads_the_diff_once_and_the_chunks_once_per_branch(wired) -> None:
    src, repo = _Source(sections=SECTIONS, backports=[_bp(120302, "26.4", "merged")]), _Repo(CHUNKS)
    holder = wired(src, repo)
    out = await h.handle_code_backport_gap(
        CodeBackportGapInput(org=ORG, namespace=NS, pr_number=115670, exact=True)
    )
    assert src.calls.count("pr_diff_sections") == 1
    assert sorted(ns for k, ns in repo.calls if k == "patch") == sorted(BRANCHES)
    assert len(holder["authz"]) == 1  # the symbol leg authorized; the patch leg did not repeat it
    paths, symbols, needles = repo.patch_args[0]
    assert paths == [SRC, "src/Common/Renamed.h"] and symbols == [SYM, "RenamedHolder"] and needles
    assert out.fix["patch"] == {
        "files": 2,
        "sections": 4,
        "hunks": 2,
        "added": 6,
        "removed": 2,
        "symbols": [SYM, "RenamedHolder"],
    }
    assert set(out.stage_ms) == {"fix", "presence", "patch"}
    by = {b["branch"]: b for b in out.branches}
    assert (by["26.8"]["verdict"], by["26.8"]["epistemic"], by["26.8"]["basis"]) == (
        "not_affected",
        "exact",
        "hunk",
    )
    assert by["26.8"]["cherry_pick"] == {} and by["26.8"]["presence"]["patch"]["mode"] == "exact"
    # refactored: the lines do not decide, the bag is disclosed, the record's floor stands
    assert (by["26.7"]["verdict"], by["26.7"]["epistemic"], by["26.7"]["basis"]) == (
        "gap",
        "lower_bound",
        "symbol",
    )
    assert (
        by["26.7"]["presence"]["patch"]["similar"] is True
        and by["26.7"]["presence"]["patch"]["reason"] == "undecided"
    )
    assert (by["26.6"]["verdict"], by["26.6"]["epistemic"], by["26.6"]["basis"]) == (
        "gap",
        "exact",
        "hunk",
    )
    assert by["26.6"]["cherry_pick"] == {"pr_number": 115670, "merge_sha": "e" * 40}
    assert (by["26.5"]["verdict"], by["26.5"]["epistemic"], by["26.5"]["basis"]) == (
        "gap",
        "lower_bound",
        "symbol",
    )
    assert (
        by["26.5"]["presence"]["patch"]["reason"] == "undecided"
        and by["26.5"]["presence"]["patch"]["similar"] is False
    )
    # the record decided 26.4; the patch block is disclosed, the verdict untouched
    assert (by["26.4"]["verdict"], by["26.4"]["basis"]) == ("covered", "backport_pr")
    assert by["26.4"]["presence"]["patch"]["mode"] == "exact"
    assert by["26.3"]["presence"]["patch"] == {
        "mode": "symbol_only",
        "added_total": 6,
        "added_present": 0,
        "removed_total": 2,
        "removed_gone": 2,
        "hunks": 2,
        "functions_matched": 0,
        "chunks_read": 0,
        "reason": "no_chunks",
    }
    assert by["26.3"]["verdict"] == "gap" and by["26.3"]["epistemic"] == "lower_bound"
    assert out.summary["gap"] == ["26.7", "26.6", "26.5", "26.3", "26.2", "26.1"]
    assert (
        out.summary["not_affected"] == ["26.8"]
        and out.summary["covered"] == ["26.4"]
        and out.verdict == "gap"
    )


@pytest.mark.asyncio
async def test_exact_deadline_and_error_are_symbol_only_never_a_verdict(wired, monkeypatch) -> None:
    class _Broken(_Repo):
        async def chunks_for_patch(self, org, ns, paths, symbols, needles, *, limit=400):
            if ns == "code-clickhouse-26-2":
                raise RuntimeError("boom")
            return await super().chunks_for_patch(org, ns, paths, symbols, needles, limit=limit)

    monkeypatch.setattr(settings, "code_backport_patch_budget_ms", 50)
    repo = _Broken(CHUNKS, slow="code-clickhouse-26-8")
    wired(_Source(sections=[DIFF_SRC]), repo)
    out = await h.handle_code_backport_gap(
        CodeBackportGapInput(
            org=ORG,
            namespace=NS,
            pr_number=115670,
            exact=True,
            branches=["26.8", "26.6", "26.2"],
        )
    )
    by = {b["branch"]: b for b in out.branches}
    assert by["26.8"]["verdict"] == "gap" and by["26.8"]["epistemic"] == "lower_bound"
    assert by["26.8"]["presence"]["patch"]["reason"] == "deadline"
    assert by["26.2"]["presence"]["patch"]["reason"] == "error:RuntimeError"
    assert (by["26.6"]["verdict"], by["26.6"]["epistemic"]) == ("gap", "exact")


@pytest.mark.asyncio
async def test_exact_without_hunks_or_record_reads_no_chunks(wired) -> None:
    src, repo = _Source(sections=[DIFF_TEST]), _Repo(CHUNKS)
    wired(src, repo)
    out = await h.handle_code_backport_gap(
        CodeBackportGapInput(org=ORG, namespace=NS, pr_number=115670, exact=True)
    )
    assert not any(c[0] == "patch" for c in repo.calls)
    assert out.fix["patch"]["hunks"] == 0 and all(
        "patch" not in b["presence"] for b in out.branches
    )
    src2, repo2 = _Source(sections=[DIFF_SRC], down=True), _Repo(CHUNKS)
    wired(src2, repo2)
    out2 = await h.handle_code_backport_gap(
        CodeBackportGapInput(org=ORG, namespace=NS, pr_number=115670, exact=True)
    )
    # a QA miss is never evidence: no diff, no patch stage, the record's verdicts stand
    assert "tracker_unavailable" in out2.causes and not any(c[0] == "patch" for c in repo2.calls)
    assert out2.fix["patch"]["sections"] == 0


@pytest.mark.asyncio
async def test_sdk_sends_exact_only_when_set() -> None:
    from sdk.python.loom.code import CodeClient

    class _Transport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((path, body))
            return {}

    class _Agent:
        def __init__(self) -> None:
            self.transport = _Transport()

    ag = _Agent()
    c = CodeClient(ag)  # type: ignore[arg-type]
    await c.backport_gap(115670)
    await c.backport_gap(115670, exact=True, branches=["26.4"])
    assert ag.transport.calls[0][1] == {"max_symbols": 8, "pr_number": 115670}
    assert ag.transport.calls[1][1] == {
        "max_symbols": 8,
        "pr_number": 115670,
        "branches": ["26.4"],
        "exact": True,
    }


def test_a_read_at_its_cap_is_no_verdict() -> None:
    """The branch's chunk read stops at CHUNK_READ_LIMIT chunks (and reads the
    first PATH_READ_LIMIT paths): lines in chunks it left out look gone or
    absent, so a read at the cap decides nothing."""
    import inspect

    from server.code.store.index.files import FilesMixin

    sig = inspect.signature(FilesMixin.chunks_for_patch)
    assert sig.parameters["limit"].default == pp.CHUNK_READ_LIMIT
    filler = [_chunk(SRC, f"other_{i}", "void g() {}") for i in range(pp.CHUNK_READ_LIMIT - 1)]
    full = pp.decide(_groups(), [_chunk(SRC, "DB::" + SYM, POST), *filler])
    assert (full.mode, full.verdict, full.reason) == ("symbol_only", "", "read_truncated")
    assert full.added_present == full.added_total  # the tally is still disclosed
    under = pp.decide(_groups(), [_chunk(SRC, "DB::" + SYM, POST), *filler[:-1]])
    assert (under.mode, under.verdict) == ("exact", "present")
    many = [
        pp.HunkGroup(path=f"src/f{i}.cpp", symbol=SYM, hunks=1, removed=["int value_one = 1;"])
        for i in range(pp.PATH_READ_LIMIT + 1)
    ]
    d = pp.decide(many, [_chunk("src/f0.cpp", SYM, "void f() {}")])
    assert (d.verdict, d.reason) == ("", "read_truncated")
