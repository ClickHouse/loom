"""A bisect without a stack frame is bounded by the report's
rarest words — IDF over the issue corpus weighs and selects the patterns,
the bot-written blocks of a body are cut before deriving them, a master-only
reproduction with a clean release draws a window, the frameless scan is
bounded by the rarest patterns in the window's diffs, the component's
directories and the fix's files, and `rescan` runs the scan although the
record names a culprit."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.code.api.models_history import CodeBisectCandidatesInput, CodeBisectCandidatesOutput
from server.code.app import bisect as h
from server.tracker.app import binding
from server.tracker.domain import bisect as bi
from server.tracker.domain.errors import TrackerUnavailable

ORG, NS, QA = "clickhouse", "code-clickhouse", "tracker-clickhouse"
SRC = "src/Storages/MergeTree/MergeTreeData.cpp"

BODY = """Wrong results with `ORDER BY sqrt(c0)`: `KeyCondition` drops rows.
cc @MorganLee please look
<!-- ch-version-info:start -->
### Version info
- Resolved by: #106507
<!-- ch-version-info:end -->
_ClickGap · Finding: `h_issue_106262`_
<!-- clickgap-triage v1
introduced_by_pr: 89982
introduced_at_commit: edae50c1670d131c06cf036ac174690cc48191af
tagged_owners:
  - login: averymorganlee
    kind: introducer_author
-->
KeyCondition checkInHyperrectangle index_granularity KeyCondition allow_suspicious_indices
edae50c1670d131c06cf036ac174690cc48191af"""

STATS = {
    "n": 22251,
    "df": {
        "keycondition": 200,
        "index": 5000,
        "granularity": 3000,
        "allow": 4000,
        "suspicious": 800,
        "indices": 1500,
    },
}


# ---- pure helpers -------------------------------------------------------------


def test_report_text_cuts_what_a_bot_wrote_into_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bi.settings, "code_bot_authors", "ClickGap")
    text = bi.report_text(BODY)
    for leaked in (
        "introduced_by_pr",
        "introducer_author",
        "Resolved by",
        "h_issue_106262",
        "averymorganlee",
        "ClickGap",
    ):
        assert leaked not in text
    assert "KeyCondition" in text and "sqrt(c0)" in text


def test_derive_patterns_drops_handles_shas_ids_and_ranks_by_frequency_times_idf() -> None:
    plain = bi.derive_patterns(BODY)
    assert "MorganLee" not in plain and "introducer_author" not in plain
    assert not any(len(p) == 40 for p in plain) and "h_issue_106262" not in plain
    assert plain[0] == "KeyCondition"  # most frequent first
    weighted = bi.derive_patterns(BODY, stats=STATS)
    # frequency × IDF: KeyCondition (2 × 4.7) still leads; the unseen
    # checkInHyperrectangle (1 × 8.9) beats the common index_granularity (1 × 1.7)
    assert weighted.index("checkInHyperrectangle") < weighted.index("index_granularity")
    assert weighted.index("KeyCondition") == 0


def test_pattern_weights_are_the_mean_idf_of_the_vocabulary_tokens() -> None:
    w = bi.pattern_weights(["KeyCondition", "index_granularity", "checkInHyperrectangle"], STATS)
    assert w["checkInHyperrectangle"] > w["KeyCondition"] > w["index_granularity"] > 0
    # the snake_case pattern is three upstream tokens: their mean
    assert w["index_granularity"] == pytest.approx(
        (bi.math.log(22252 / 5001) + bi.math.log(22252 / 3001)) / 2, abs=1e-3
    )
    assert bi.pattern_weights(["x_y"], None) == {"x_y": 1.0}
    assert bi.pattern_weights(["x_y"], {"n": 0, "df": {}}) == {"x_y": 1.0}


def test_scope_patterns_are_the_rarest_k() -> None:
    pats = [
        "KeyCondition",
        "index_granularity",
        "checkInHyperrectangle",
        "allow_suspicious_indices",
    ]
    w = bi.pattern_weights(pats, STATS)
    assert bi.scope_patterns(pats, w, 2) == ["checkInHyperrectangle", "KeyCondition"]
    assert bi.scope_patterns(pats, w, 0) == []


def test_master_only_reproduction_with_a_clean_release_draws_a_window() -> None:
    assert bi.versions_between("26.5", "", master=True) == ["26.6", "26.7", "26.8"]
    assert bi.versions_between("26.5", "") == []  # no master sighting: no range
    assert bi.versions_between("", "", master=True) == []
    assert bi.master_reproduces(["master", "26.6"]) and not bi.master_reproduces(["26.6"])
    assert bi.window_name("26.5", "", ["26.6"]) == "master_after_clean"
    assert bi.window_name("26.3", "26.6", ["26.4"]) == "clean_to_bad_plus_slack"
    assert bi.window_name("", "26.6", ["26.4"]) == "lookback_from_bad"
    assert bi.window_name("26.5", "", []) == ""


def test_rank_weighs_an_added_rare_pattern_above_two_common_ones() -> None:
    rows = [
        {
            "pr_number": 1,
            "title": "Fix a",
            "added_patterns": ["checkInHyperrectangle"],
            "matched_patterns": ["checkInHyperrectangle"],
            "merged_at": datetime(2026, 3, 1),
        },
        {
            "pr_number": 2,
            "title": "Fix b",
            "added_patterns": ["index_granularity", "allow_suspicious_indices"],
            "matched_patterns": ["index_granularity", "allow_suspicious_indices"],
            "merged_at": datetime(2026, 3, 2),
        },
    ]
    pats = ["checkInHyperrectangle", "index_granularity", "allow_suspicious_indices"]
    plain = bi.rank_candidates(rows, frames=[], paths=[], limit=5, patterns=pats)
    assert [c.pr_number for c in plain] == [2, 1]  # Two of three beats one
    w = bi.pattern_weights(pats, STATS)
    weighted = bi.rank_candidates(
        rows, frames=[], paths=[], limit=5, patterns=pats, pattern_weights=w
    )
    assert [c.pr_number for c in weighted] == [1, 2]  # The rare one weighs more
    assert weighted[0].score == pytest.approx(2.5 * w["checkInHyperrectangle"] / sum(w.values()))


def test_a_widened_scan_is_a_scope_not_a_missing_input() -> None:
    v, causes = bi.verdict_of(
        candidates=[], versions=["26.6"], paths=[], introduced_commit="", qa_ok=True, widened=True
    )
    assert (v, causes) == (bi.NO_CANDIDATES, [])
    v, causes = bi.verdict_of(
        candidates=[], versions=["26.6"], paths=[], introduced_commit="", qa_ok=True
    )
    assert (v, causes) == (bi.UNKNOWN, ["no_resolved_paths"])


# ---- handler -------------------------------------------------------------------


class _Repo:
    async def symbols_by_name(self, org, ns, name, *, limit=50):
        return [{"path": SRC}] if "loadDataParts" in name else []

    async def pr_records_by_number(self, org, ns, numbers):
        return {}


class _Source:
    def __init__(
        self,
        issue: dict[str, Any],
        *,
        edges=None,
        prs=None,
        dirs=None,
        changes=None,
        stats=None,
        text: str = BODY,
    ) -> None:
        self.issue, self.edges, self.prs = issue, edges or [], prs or []
        self.dirs, self.changes, self.stats, self.text = dirs or [], changes or {}, stats, text
        self.scan: dict[str, Any] | None = None
        self.calls: list[str] = []

    def repos(self):
        return ["ClickHouse/ClickHouse"]

    async def items_by_number(self, org, ns, nums):
        self.calls.append("items")
        return {n: self.issue for n in nums if n == self.issue["number"]}

    async def edges_from(self, org, ns, refs, rels=None, *, budget_ms=None):
        self.calls.append("edges")
        self.rels = rels
        return list(self.edges)

    async def issue_text(self, org, ns, number, *, chars=6000):
        return self.text

    async def token_stats(self, org, ns):
        self.calls.append("vocab")
        if self.stats == "down":
            raise TrackerUnavailable("vocab timeout")
        return dict(self.stats or {})

    async def component_dirs(self, org, ns, component, *, limit=8, min_lift=1.5):
        self.calls.append(f"dirs:{component}:{limit}:{min_lift}")
        return [{"dir": d, "prs": 10, "lift": 3.0} for d in self.dirs]

    async def pr_change(self, org, ns, number):
        self.calls.append(f"change:{number}")
        return self.changes.get(number)

    async def merged_prs_touching(
        self,
        org,
        ns,
        paths,
        versions,
        *,
        symbols=None,
        patterns=None,
        dirs=None,
        scope_patterns=None,
        pattern_weights=None,
        limit=20,
        before=None,
        after=None,
    ):
        self.scan = {
            "paths": list(paths),
            "versions": list(versions),
            "dirs": list(dirs or []),
            "scope_patterns": list(scope_patterns or []),
            "pattern_weights": dict(pattern_weights or {}),
            "patterns": list(patterns or []),
        }
        return list(self.prs)


def _issue(**kw: Any) -> dict[str, Any]:
    base = {
        "repo": "ClickHouse/ClickHouse",
        "number": 106262,
        "is_pr": 0,
        "title": "Wrong results",
        "primary_component": "SkipIndex",
        "reproduces_on": ["master", "26.6", "26.5"],
        "clean_on": ["26.3"],
        "affected_versions": [],
        "introduced_commit": "",
        "labels": [],
    }
    return {**base, **kw}


def _prs() -> list[dict[str, Any]]:
    return [
        {
            "pr_number": 700,
            "title": "Fix KeyCondition",
            "author": "a",
            "matched_paths": [],
            "added_patterns": ["checkInHyperrectangle"],
            "matched_patterns": ["checkInHyperrectangle"],
            "merged_at": datetime(2026, 4, 1),
            "minor_version": "26.5",
            "has_test": 1,
            "url": "u/700",
        },
        {
            "pr_number": 701,
            "title": "Tune granularity",
            "author": "b",
            "matched_paths": [],
            "added_patterns": ["index_granularity"],
            "matched_patterns": ["index_granularity"],
            "merged_at": datetime(2026, 4, 2),
            "minor_version": "26.5",
            "has_test": 0,
            "url": "u/701",
        },
    ]


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(h, "CodeRepo", lambda: _Repo())
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)

    async def _authorized(org, code_ns, *, hides_answer=True):
        return QA

    monkeypatch.setattr(binding, "authorized_tracker_namespace", _authorized)

    async def _no_candidate(org, tracker_ns, number, *, repo=None):
        return None

    monkeypatch.setattr(h.lk, "fix_candidate", _no_candidate)
    holder: dict[str, Any] = {}
    import server.tracker.store.source as qs

    monkeypatch.setattr(qs, "TrackerSource", lambda org, ns: holder["src"])

    def use(src: _Source) -> _Source:
        holder["src"] = src
        return src

    return use


def _inp(**kw: Any) -> CodeBisectCandidatesInput:
    return CodeBisectCandidatesInput(
        **{"org": ORG, "namespace": NS, "issue_number": 106262, "marks": False, "limit": 5, **kw}
    )


@pytest.mark.asyncio
async def test_frameless_issue_is_bounded_by_patterns_component_and_ranked_by_idf(wired) -> None:
    src = wired(_Source(_issue(), prs=_prs(), dirs=["src/Storages/MergeTree/"], stats=STATS))
    out = await h.handle_code_bisect_candidates(_inp())
    assert out.verdict == "candidates" and out.causes == []
    assert out.widened_by == ["component", "patterns"] and out.scope == "directory"
    assert out.component == "SkipIndex" and out.component_dirs == ["src/Storages/MergeTree/"]
    assert (
        src.scan is not None and src.scan["paths"] == [] and src.scan["dirs"] == out.component_dirs
    )
    # the rarest patterns bound the scan, IDF-weighted, the bot's words never among them
    assert src.scan["scope_patterns"] and src.scan["scope_patterns"][0] == "checkInHyperrectangle"
    assert "introducer_author" not in out.patterns and "MorganLee" not in out.patterns
    assert out.pattern_weights["checkInHyperrectangle"] > out.pattern_weights["index_granularity"]
    assert src.scan["pattern_weights"] == out.pattern_weights
    # the rare pattern's PR ranks first although both added one pattern
    assert [c["pr_number"] for c in out.candidates] == [700, 701]
    assert out.window == "clean_to_bad_plus_slack" and out.versions_searched == [
        "26.4",
        "26.5",
        "26.6",
    ]
    assert "dirs:SkipIndex:8:1.5" in src.calls and src.rels == ["caused_by", "fixes"]
    # the payload names the widening; a framed call would not carry these keys
    dumped = out.model_dump()
    assert dumped["widened_by"] == ["component", "patterns"] and "pattern_weights" in dumped


@pytest.mark.asyncio
async def test_master_only_issue_draws_the_window_after_the_clean_release(wired) -> None:
    src = wired(_Source(_issue(reproduces_on=["master"], clean_on=["26.5", "26.4"]), prs=_prs()))
    out = await h.handle_code_bisect_candidates(_inp(wideners=["patterns"]))
    assert out.window == "master_after_clean" and out.versions_searched == ["26.6", "26.7", "26.8"]
    assert out.bad_version == "" and out.good_version == "26.5"
    assert out.widened_by == ["patterns"] and out.scope == "patterns"
    assert src.scan is not None and src.scan["dirs"] == [] and src.scan["scope_patterns"]
    assert out.verdict == "candidates"


@pytest.mark.asyncio
async def test_the_records_fix_seeds_the_paths_and_a_backport_never_does(wired) -> None:
    edges = [
        {
            "rel_type": "fixes",
            "direction": "in",
            "dst_kind": "github_pr",
            "dst_ref": "ClickHouse/ClickHouse#800",
        },
        {
            "rel_type": "fixes",
            "direction": "in",
            "dst_kind": "github_pr",
            "dst_ref": "ClickHouse/ClickHouse#801",
        },
    ]
    changes = {
        800: {
            "pr_number": 800,
            "title": "Backport #801 to 26.5: Fix",
            "is_merged": 1,
            "is_backport_pr": 1,
            "is_cherrypick_pr": 0,
            "files": [SRC],
            "url": "u/800",
            "minor_version": "",
        },
        801: {
            "pr_number": 801,
            "title": "Fix KeyCondition",
            "is_merged": 1,
            "is_backport_pr": 0,
            "is_cherrypick_pr": 0,
            "url": "u/801",
            "minor_version": "26.7",
            "files": [SRC, "tests/queries/0_stateless/1.sql", "src/Core/Settings.cpp"],
        },
    }
    src = wired(_Source(_issue(primary_component=""), edges=edges, prs=_prs(), changes=changes))
    out = await h.handle_code_bisect_candidates(_inp())
    assert out.fix_candidate is not None and out.fix_candidate["pr_number"] == 801
    assert out.fix_candidate["source"] == "record" and "files" not in out.fix_candidate
    assert out.fix_candidate["paths_used"] == [SRC, "src/Core/Settings.cpp"]
    assert out.paths == [SRC] and out.hub_paths_dropped == ["src/Core/Settings.cpp"]
    assert out.widened_by == ["fix_candidate", "patterns"]
    assert src.calls.count("change:800") == 1 and src.calls.count("change:801") == 1
    assert src.scan is not None and src.scan["paths"] == [SRC]
    # the frames' directory rule still widens: patterns exist
    assert src.scan["dirs"] == ["src/Storages/MergeTree/"] and out.scope == "directory"


@pytest.mark.asyncio
async def test_no_wideners_answers_unknown_with_no_resolved_paths(wired) -> None:
    src = wired(_Source(_issue(), prs=_prs(), dirs=["src/Storages/MergeTree/"], stats=STATS))
    out = await h.handle_code_bisect_candidates(_inp(wideners=[]))
    assert out.verdict == "unknown" and out.causes == ["no_resolved_paths"]
    assert out.widened_by == [] and src.scan is None
    assert not any(c.startswith("dirs:") for c in src.calls)
    dumped = out.model_dump()
    assert (
        "widened_by" not in dumped and "component_dirs" not in dumped and "component" not in dumped
    )
    # the patterns are still weighed (the vocabulary was read) — disclosed
    assert "pattern_weights" in dumped


@pytest.mark.asyncio
async def test_rescan_runs_the_scan_although_the_record_names_a_culprit(wired) -> None:
    edges = [
        {
            "rel_type": "caused_by",
            "direction": "out",
            "dst_kind": "github_pr",
            "dst_ref": "ClickHouse/ClickHouse#650",
        }
    ]
    src = wired(
        _Source(
            _issue(introduced_commit="abc123"),
            edges=edges,
            prs=_prs(),
            dirs=["src/Storages/MergeTree/"],
        )
    )
    out = await h.handle_code_bisect_candidates(_inp())
    assert out.verdict == "already_bisected" and src.scan is None and out.candidates == []
    src = wired(
        _Source(
            _issue(introduced_commit="abc123"),
            edges=edges,
            prs=_prs(),
            dirs=["src/Storages/MergeTree/"],
            stats=STATS,
        )
    )
    out = await h.handle_code_bisect_candidates(_inp(rescan=True))
    assert out.verdict == "candidates" and src.scan is not None
    assert out.introduced_commit == "abc123" and out.named_by_graph == []
    assert [c["pr_number"] for c in out.candidates] == [700, 701]
    assert out.issue.get("introduced_commit") == "abc123"


@pytest.mark.asyncio
async def test_a_vocabulary_outage_keeps_the_count_ranking(wired) -> None:
    src = wired(_Source(_issue(primary_component=""), prs=_prs(), stats="down"))
    out = await h.handle_code_bisect_candidates(_inp(wideners=["patterns"]))
    assert out.verdict == "candidates" and out.pattern_weights == {}
    assert src.scan is not None and src.scan["pattern_weights"] == {}
    assert "pattern_weights" not in out.model_dump()
    assert out.tracker_unavailable is False


@pytest.mark.asyncio
async def test_a_framed_call_keeps_the_framed_payload_keys(wired) -> None:
    src = wired(
        _Source(
            _issue(),
            prs=[{**_prs()[0], "matched_paths": [SRC], "matched_symbols": ["loadDataParts"]}],
            dirs=["src/Storages/MergeTree/"],
        )
    )
    out = await h.handle_code_bisect_candidates(
        _inp(
            issue_number=0,
            symbols=["DB::MergeTreeData::loadDataParts(bool)"],
            bad_version="26.6",
            good_version="26.3",
        )
    )
    dumped = out.model_dump()
    for key in ("widened_by", "component", "component_dirs", "pattern_weights", "fix_candidate"):
        assert key not in dumped
    assert out.scope == "directory" and src.scan is not None and src.scan["scope_patterns"] == []
    assert not any(c.startswith("dirs:") for c in src.calls)


def test_input_validates_the_wideners_and_defaults_to_all_three() -> None:
    inp = CodeBisectCandidatesInput(issue_number=1)
    assert inp.wideners == ["patterns", "component", "fix_candidate"] and inp.rescan is False
    assert CodeBisectCandidatesInput(
        issue_number=1, wideners=["patterns", "patterns"]
    ).wideners == ["patterns"]
    with pytest.raises(ValueError, match="wideners"):
        CodeBisectCandidatesInput(issue_number=1, wideners=["frames"])
    assert CodeBisectCandidatesOutput().model_dump().get("scope") == "file"


# ---- template sections, repro names, the report's time -------------------------

TEMPLATE = """### Reproduction
```
CREATE TABLE t_minmax_float (c0 Int32) ENGINE = MergeTree ORDER BY sqrt(c0)
  SETTINGS allow_suspicious_indices = 1, index_granularity = 4;
INSERT INTO repro_topk SELECT number FROM numbers(100);
SELECT count() FROM demo_ctrl WHERE checkInHyperrectangle
```
### Bisect
- Bisect outcome: `CONVERGED`

### Affects
- Reproduces on: master

The reporter adds KeyCondition here.
<!-- clickgap-enrichment -->
### Triage metadata
- Suggested assignees (from introducing PR #99736): @introducer
introducer_reviewer
<!-- /clickgap-enrichment -->
Trailing __set_16854109006789820482_7702020288925574204 text SerializationDateTime"""


def test_repro_names_template_sections_and_hashes_never_become_patterns() -> None:
    assert bi.repro_names(TEMPLATE) >= {"t_minmax_float", "repro_topk", "demo_ctrl"}
    pats = bi.derive_patterns(TEMPLATE)
    for junk in (
        "t_minmax_float",
        "repro_topk",
        "demo_ctrl",
        "CONVERGED",
        "introducer_reviewer",
        "Suggested",
        "__set_16854109006789820482_7702020288925574204",
        "introducer",
    ):
        assert junk not in pats
    # the reporter's words survive the cuts, on either side of the bot's blocks
    for kept in (
        "KeyCondition",
        "checkInHyperrectangle",
        "allow_suspicious_indices",
        "index_granularity",
        "SerializationDateTime",
    ):
        assert kept in pats
    # a template section ends at the blank line before prose, not at the end of the body
    assert "getExtremes" in bi.report_text("### Bisect\n- outcome: CONVERGED\n\nProse getExtremes.")
    # a named pair ends at the first closer of its own name
    blocks = "<!-- a --> x <!-- b --> y <!-- /a --> keep <!-- b --> z <!-- /b -->"
    assert bi.report_text(blocks).split() == ["keep"]


def test_a_pr_merged_after_the_report_is_not_a_candidate() -> None:
    cands = bi.rank_candidates(
        [
            {
                "pr_number": 1,
                "title": "Culprit",
                "merged_at": datetime(2026, 3, 1),
                "added_patterns": ["x"],
            },
            {
                "pr_number": 2,
                "title": "Fix",
                "merged_at": datetime(2026, 5, 1),
                "added_patterns": ["x"],
            },
            {"pr_number": 3, "title": "No date", "merged_at": None, "added_patterns": ["x"]},
        ],
        frames=[],
        paths=[],
        limit=5,
        patterns=["x"],
    )
    kept, dropped = bi.split_after_report(cands, datetime(2026, 4, 1))
    assert [c.pr_number for c in kept] == [1, 3] and [c.pr_number for c in dropped] == [2]
    assert bi.split_after_report(cands, None) == (cands, [])


@pytest.mark.asyncio
async def test_handler_drops_post_report_prs_and_runs_no_diff_on_a_frameless_pool(
    wired, monkeypatch
) -> None:
    prs = [
        *_prs(),
        {
            "pr_number": 702,
            "title": "Fix KeyCondition for real",
            "author": "c",
            "matched_paths": [],
            "added_patterns": ["checkInHyperrectangle", "index_granularity"],
            "matched_patterns": ["checkInHyperrectangle", "index_granularity"],
            "merged_at": datetime(2026, 6, 1),
            "minor_version": "26.6",
            "has_test": 1,
            "url": "u/702",
        },
    ]
    src = wired(
        _Source(_issue(opened_at=datetime(2026, 5, 1), primary_component=""), prs=prs, stats=STATS)
    )
    calls: list[Any] = []

    async def _diff(git_dir, sha, paths, frames):
        calls.append((sha, paths))
        return "", None, True

    monkeypatch.setattr(h.cb, "diff_kind", _diff)
    out = await h.handle_code_bisect_candidates(_inp(marks=True, wideners=["patterns"]))
    assert [c["pr_number"] for c in out.candidates] == [700, 701]
    assert out.dropped_after_report == 1 and out.model_dump()["dropped_after_report"] == 1
    assert calls == [] and out.marks["status"] == "ok" and out.marks["git_calls"] == 0
    assert src.scan is not None
    # with no opening time nothing is dropped and the key is absent
    src = wired(_Source(_issue(primary_component=""), prs=prs, stats=STATS))
    out = await h.handle_code_bisect_candidates(_inp(wideners=["patterns"]))
    assert [c["pr_number"] for c in out.candidates] == [702, 700, 701]
    assert "dropped_after_report" not in out.model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize("held", ["switched_off", "unread"])
async def test_a_held_embed_answers_the_refusal_not_a_ranking_without_vectors(
    wired, monkeypatch, held
) -> None:
    from server.llm import customer

    error = (
        customer.ModelCallsDisabled("model calls are switched off for org 'clickhouse'")
        if held == "switched_off"
        else customer.SwitchUnread("the model-call switch could not be read")
    )

    class _Refusing:
        model_name, dim = "m", 8

        async def embed(self, text: str) -> list[float]:
            raise error

    class _Down(_Source):
        async def window_prs(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            raise RuntimeError("window read down")  # fails as the embed is held

    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: _Refusing())
    wired(_Down(_issue(), prs=_prs(), dirs=["src/Storages/MergeTree/"], stats=STATS))
    with pytest.raises(type(error)):  # 403 / 503 as memory.search answers, not a degraded rank
        await h.handle_code_bisect_candidates(_inp())


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [True, False])
async def test_the_wide_window_waits_for_its_embed_when_the_read_fails_first(
    monkeypatch, held
) -> None:
    import asyncio

    from server.code.app import bisect_fusion
    from server.llm import customer

    finished: list[bool] = []

    async def slow_embed(text: str) -> list[float]:
        await asyncio.sleep(0.01)  # ends after the read has failed
        finished.append(True)
        if held:
            raise customer.SwitchUnread("the model-call switch could not be read")
        return [0.5]

    class _Down:
        async def window_prs(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            raise RuntimeError("window read down")

    monkeypatch.setattr(bisect_fusion, "_embed", slow_embed)
    call = bisect_fusion.wide_extras(
        _Down(),
        "o",
        "t",
        good="v1",
        bad="v2",
        master=False,
        versions=[],
        report_at=datetime(2026, 1, 1),
        in_window=set(),
        text="report",
        paths=[],
    )
    if held:
        with pytest.raises(customer.SwitchUnread):  # the request answers the hold
            await call
    else:
        assert await call == ([], {})  # the first window stands
    assert finished == [True]  # the embed ran to its end, none left behind
