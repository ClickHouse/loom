"""Issue–PR link recovery: the pure half (the closing
pattern, an issue's query, the window, features → score and `why`, ranking
into table rows, the latest-pass filter), the tracker source statements (the
trunk predicate with `is_private = 0`, the merge window, the number token
admitted by the index, the closing pattern, the triage budget), the
repository on a stub client, the pass on stub source and repository, the
`tracker.links` handler, the two consumers taking a candidate only when the
record is silent — and their payloads unchanged when no candidate exists."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import pytest

import server.code.app.backport as hb
import server.code.app.bisect as hbi
import server.tracker.app.binding as binding
import server.tracker.store.source as src
import server.tracker.store.source.qa_intel as qi
from server.code.api.models_history import CodeBackportGapInput, CodeBisectCandidatesInput
from server.core.errors import ValidationError
from server.operations.base import Role
from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings
from server.tracker.api.models import TrackerLinksInput
from server.tracker.app import handlers as h
from server.tracker.app import links as lk
from server.tracker.app import source as tsrc
from server.tracker.domain import links as dlk
from server.tracker.domain import similar as sim
from server.tracker.domain.errors import TrackerUnavailable
from server.tracker.store import links as qr
from server.tracker.workers import links as wk
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json

ORG, NS, REPO = "clickhouse", "tracker-clickhouse", "ClickHouse/ClickHouse"
CODE_NS = "code-clickhouse"
T0 = datetime(2026, 8, 19, 20, 36, 14)
TRACE = """Crash in CHECK TABLE.

0. DB::Exception::Exception(int) @ 0x0000000000d49ed0
1. DB::MergeTreeData::checkPartAndFix(std::shared_ptr<DB::IMergeTreeDataPart>) @ 0x1
2. DB::MergeTreeData::checkPartImpl(std::shared_ptr<DB::IMergeTreeDataPart>) @ 0x2
3. DB::InterpreterCheckQuery::execute() ./src/Interpreters/InterpreterCheckQuery.cpp:118
"""


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    qi.reset_for_tests()
    src.reset_cache_for_tests()
    monkeypatch.setattr(settings, "tracker_org", ORG)
    monkeypatch.setattr(
        settings,
        "tracker_sources_json",
        tracker_sources_json(f'{{"{REPO}": "{NS}"}}', restricted=""),
    )
    monkeypatch.setattr(settings, "tracker_credentials_json", tracker_credentials_json())
    monkeypatch.setattr(settings, "tracker_live_serve", True)
    monkeypatch.setattr(settings, "tracker_links_min_score", 0.9)
    yield
    qi.reset_for_tests()
    src.reset_cache_for_tests()


def _stats() -> sim.TokenStats:
    return sim.TokenStats(
        n=30000,
        df={
            "projection": 400,
            "mutation": 900,
            "check": 5000,
            "table": 20000,
            "crash": 3000,
            "checkpartandfix": 3,
            "checkpartimpl": 4,
            "exception": 9000,
            "execute": 8000,
            "interpretercheckquery": 12,
        },
        avg_title_len=8.0,
        avg_body_len=900.0,
    )


# ---------------------------------------------------------------- pure half


def test_closes_pattern_is_githubs_linking_grammar() -> None:
    pat = re.compile(dlk.closes_pattern(115506, REPO))
    assert pat.search("Fixes #115506")
    assert pat.search("closes: https://github.com/ClickHouse/ClickHouse/issues/115506.")
    assert pat.search("Resolved 115506 and more")
    assert not pat.search("see #115506 for context")  # a bare mention is not a closing keyword
    assert not pat.search("Fixes #1155060")  # a longer number is another issue
    assert "github\\.com/" not in dlk.closes_pattern(1)  # no repository → no URL form


def test_issue_query_takes_terms_and_frames_and_the_window_clamps_to_now() -> None:
    q = lk.issue_query(
        115506,
        "CHECK TABLE crash after DROP PROJECTION",
        TRACE,
        _stats(),
        component="MergeTree",
        created_at=T0,
    )
    assert q.number == 115506 and q.component == "MergeTree" and q.created_at == T0
    # title tokens first among the rarest; the frames' method names and the path's basename
    assert "projection" in q.terms and "checkpartandfix" in q.terms and len(q.idf) == len(q.terms)
    # the frames' method names (`execute`, in 27% of PRs, is dropped as common) and the
    # path frame's basename
    assert q.symbols == ["checkPartAndFix", "IMergeTreeDataPart", "checkPartImpl"]
    assert q.files == ["InterpreterCheckQuery.cpp"] and q.frames_total == 4
    # a body with no numbered frame lines makes no frames
    assert lk.issue_query(1, "t", "DB::Foo::bar() in prose", _stats()).symbols == []
    since, until = lk.window(T0, window_days=90)
    assert since == T0 - timedelta(days=7) and until == T0 + timedelta(days=90)
    assert lk.window(T0, window_days=90, now=T0 + timedelta(days=3))[1] == T0 + timedelta(days=3)


def test_score_is_additive_and_why_names_each_term() -> None:
    w = lk.Weights(mention=1.0)
    f = lk.Features(
        bm25_norm=0.8,
        closes_mention=2,
        frames_symbols=2,
        frames_files=1,
        frames_total=4,
        component_match=1,
    )
    assert lk.score(f, w) == pytest.approx(0.8 + 1.0 + 0.05)  # frames are recorded, unscored
    assert lk.why(f) == ["closes", "lexical", "frames", "component"]
    named = lk.Features(bm25_norm=0.5, closes_mention=1, frames_symbols=1)
    assert lk.score(named, w) == pytest.approx(0.5 + 0.5)
    assert lk.why(named) == ["mentions", "lexical", "frames"]
    assert lk.why(lk.Features()) == []
    assert lk.score(lk.Features(bm25_norm=1.0), lk.Weights(mention=0.0)) == 1.0


def _rows() -> list[dict[str, Any]]:
    return [
        {
            "pr_number": 119348,
            "title": "Keep a part with a dropped projection",
            "url": "u/119348",
            "merged_at": T0 + timedelta(days=24),
            "minor_version": "26.9",
            "primary_component": "MergeTree",
            "src_components": [],
            "bm25_title": 6.0,
            "bm25_body": 4.43,
            "mention": 0,
            "file_hits": 1,
        },
        {
            "pr_number": 117296,
            "title": "Recompute MATERIALIZED columns",
            "url": "u/117296",
            "merged_at": T0 + timedelta(days=26),
            "minor_version": "26.9",
            "primary_component": "Other",
            "src_components": ["MergeTree"],
            "bm25_title": 3.0,
            "bm25_body": 4.39,
            "mention": 0,
            "file_hits": 0,
        },
        {
            "pr_number": 120122,
            "title": "Isolate the mutation test",
            "url": "",
            "merged_at": T0 + timedelta(days=27),
            "minor_version": "",
            "primary_component": "",
            "src_components": [],
            "bm25_title": 1.0,
            "bm25_body": 5.29,
            "mention": 2,
            "file_hits": 0,
        },
        {
            "pr_number": 120122,
            "title": "duplicate row",
            "merged_at": T0,
            "bm25_title": 9.0,
            "bm25_body": 9.0,
        },
    ]


def test_rank_orders_by_score_numbers_ranks_and_builds_full_rows() -> None:
    q = lk.issue_query(
        115506, "CHECK TABLE crash", TRACE, _stats(), component="MergeTree", created_at=T0
    )
    ranked = lk.rank(
        q,
        _rows(),
        weights=lk.Weights(mention=1.0),
        symbol_hits={119348: 2},
        per_issue=2,
    )
    assert [c["pr_number"] for c in ranked] == [120122, 119348] and [c["rank"] for c in ranked] == [
        1,
        2,
    ]
    top = ranked[0]["features"]
    # the closing mention outranks the best lexical match; bm25_norm is a share of the issue's best
    assert top.closes_mention == 2 and top.bm25_norm == pytest.approx(6.29 / 10.43)
    second = ranked[1]["features"]
    assert second.bm25_norm == 1.0 and second.frames_symbols == 2 and second.frames_files == 1
    assert (
        second.frames_total == 4 and second.component_match == 1 and second.window_hours == 24 * 24
    )
    assert ranked[1]["why"] == ["lexical", "frames", "component"]
    rows = lk.table_rows(ORG, NS, q, ranked, computed_at=T0 + timedelta(days=30), version=7)
    assert len(rows) == 2 and set(rows[0]) == set(qr.TRACKER_LINK_COLS)
    assert (
        rows[0]["issue_number"] == 115506
        and rows[0]["pr_number"] == 120122
        and rows[0]["rank"] == 1
    )
    assert rows[1]["terms"] == q.terms and rows[1]["pr_merged_at"] == T0 + timedelta(days=24)
    assert {r["computed_at"] for r in rows} == {T0 + timedelta(days=30)} and {
        r["version"] for r in rows
    } == {7}
    # the component boost alone: src_components counts as a match
    assert lk.features_of(q, _rows()[1], top_bm25=10.0).component_match == 1
    assert lk.rank(q, [], weights=lk.Weights()) == []


def test_latest_only_keeps_each_issues_newest_pass_and_as_item_is_the_wire_shape() -> None:
    old, new = T0, T0 + timedelta(days=1)
    rows = [
        {"issue_number": 1, "pr_number": 10, "computed_at": old},
        {"issue_number": 1, "pr_number": 11, "computed_at": new},
        {"issue_number": 2, "pr_number": 12, "computed_at": old},
    ]
    assert [(r["issue_number"], r["pr_number"]) for r in dlk.latest_only(rows)] == [
        (1, 11),
        (2, 12),
    ]
    row = qr.build_link_row(
        org=ORG,
        qa_namespace=NS,
        issue_number=1,
        pr_number=11,
        rank=1,
        score=1.7,
        bm25_norm=0.7,
        closes_mention=2,
        frames_symbols=1,
        frames_total=3,
        pr_title="Fix",
        pr_merged_at=new,
        issue_created_at=old,
        computed_at=new,
        terms=["a"],
    )
    item = lk.as_item(row)
    assert item["issue_number"] == 1 and item["pr_number"] == 11 and item["score"] == 1.7
    assert item["why"] == ["closes", "lexical", "frames"] and item["epistemic"] == "candidate"
    assert item["source"] == "candidate" and item["terms"] == ["a"]
    assert item["features"]["frames_total"] == 3 and item["pr_merged_at"] == new.isoformat()
    assert item["computed_at"] == new.isoformat() and item["issue_created_at"] == old.isoformat()
    with pytest.raises(ValueError):
        qr.build_link_row(org=ORG, nope=1)


# ------------------------------------------------------------ source statements


class _Upstream:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, dict[str, Any], float | None]] = []

    async def __call__(self, sql, params, *, credential, max_execution_s=None, query_settings=None):
        self.calls.append((sql, dict(params), max_execution_s))
        self.settings = query_settings
        return list(self.rows)


@pytest.mark.asyncio
async def test_candidate_statement_carries_the_trunk_predicate_window_number_token_and_budget(
    monkeypatch,
) -> None:
    up = _Upstream(
        [
            {
                "pr_number": 5,
                "title": "t",
                "url": "u",
                "merged_at": T0,
                "minor_version": "26.9",
                "primary_component": "c",
                "src_components": ["a"],
                "bm25_title": 1.5,
                "bm25_body": 0.5,
                "mention": 2,
                "file_hits": 1,
            }
        ]
    )
    monkeypatch.setattr(qi, "query_rows", up)
    s = src.TrackerSource(ORG, NS)
    since, until = T0 - timedelta(days=7), T0 + timedelta(days=90)
    rows = await s.link_pr_candidates(
        ORG,
        NS,
        issue_number=115506,
        terms=["projection", "checkpartandfix"],
        idf=[4.3, 9.2],
        avg_title_len=8.0,
        avg_body_len=900.0,
        since=since,
        until=until,
        files=["InterpreterCheckQuery.cpp"],
    )
    sql, params, budget = up.calls[-1]
    assert "FROM qa_intelligence.github_prs FINAL" in sql and "PREWHERE" not in sql
    assert (
        "is_private = 0 AND is_merged = 1 AND is_backport_pr = 0" in sql
        and "is_cherrypick_pr = 0" in sql
    )
    assert "merged_at >= {since:DateTime64(3)} AND merged_at < {until:DateTime64(3)}" in sql
    assert params["since"] == since and params["until"] == until
    # the index admits every spelling of the terms, and the issue's number as a body token
    assert "hasAnyTokens(body, {numtok:Array(String)})" in sql and params["numtok"] == ["115506"]
    assert params["variants"] == sorted(
        {
            "projection",
            "PROJECTION",
            "Projection",
            "checkpartandfix",
            "CHECKPARTANDFIX",
            "Checkpartandfix",
        }
    )
    assert params["toks"] == ["projection", "checkpartandfix"] and params["idf"] == [4.3, 9.2]
    assert params["closes_re"] == dlk.closes_pattern(115506, REPO)
    assert params["hash"] == "#115506" and params["url"] == "/issues/115506"
    assert (
        params["files"] == ["InterpreterCheckQuery.cpp"]
        and "endsWith(p, f), affected_file_paths" in sql
    )
    # split BM25F, no diff read, the triage budget (index-time)
    assert "AS bm25_title" in sql and "AS bm25_body" in sql and "diff" not in sql
    # mentions ahead of the text order (a number-token admission must survive the cut), and
    # the direct read from the text index is off (it loses rows)
    assert "ORDER BY mention DESC, bm25_title + bm25_body DESC" in sql and params["limit"] == 50
    assert up.settings == {"query_plan_direct_read_from_text_index": 0}
    assert budget == pytest.approx(settings.tracker_live_bisect_budget_ms / 1000.0)
    assert rows == [
        {
            "pr_number": 5,
            "title": "t",
            "url": "u",
            "merged_at": T0,
            "minor_version": "26.9",
            "primary_component": "c",
            "src_components": ["a"],
            "bm25_title": 1.5,
            "bm25_body": 0.5,
            "mention": 2,
            "file_hits": 1,
        }
    ]
    assert (
        await s.link_pr_candidates(
            ORG,
            NS,
            issue_number=0,
            terms=["x"],
            idf=[1.0],
            avg_title_len=1,
            avg_body_len=1,
            since=since,
            until=until,
        )
        == []
    )


@pytest.mark.asyncio
async def test_issue_page_and_pr_vocabulary_statements(monkeypatch) -> None:
    up = _Upstream(
        [
            {
                "number": 7,
                "source_id": f"{REPO}#7",
                "title": "t",
                "body": "b",
                "primary_component": "c",
                "created_at": T0,
                "state_reason": "",
                "closed_at": None,
            }
        ]
    )
    monkeypatch.setattr(qi, "query_rows", up)
    s = src.TrackerSource(ORG, NS)
    issues = await s.link_issues(ORG, NS, since=T0, before_number=100, limit=400)
    sql, params, budget = up.calls[-1]
    assert "FROM qa_intelligence.github_issues FINAL" in sql and "is_private = 0 AND" in sql
    assert "created_at >= {since:DateTime64(3)}" in sql and "number < {before:UInt32}" in sql
    assert " state," not in sql  # the issue table has no `state` column
    assert params == {"repos": [REPO], "since": T0, "limit": 400, "before": 100}
    assert "ORDER BY number DESC" in sql and budget == pytest.approx(6.0)
    assert issues == [
        {
            "number": 7,
            "source_id": f"{REPO}#7",
            "title": "t",
            "body": "b",
            "component": "c",
            "created_at": T0,
            "state": "open",
            "state_reason": "",
            "closed_at": None,
        }
    ]
    await s.link_issues(ORG, NS, since=T0)
    assert "number <" not in up.calls[-1][0] and "before" not in up.calls[-1][1]
    up.rows = []
    await s.pr_token_stats(ORG, NS)
    vocab = [c for c in up.calls if "HAVING df >= 3" in c[0]][-1]
    assert "FROM qa_intelligence.github_prs FINAL" in vocab[0]
    assert "is_private = 0 AND is_merged = 1 AND is_backport_pr = 0" in vocab[0]
    assert "tokens(lower(" in vocab[0] and vocab[2] == pytest.approx(6.0)


# ---------------------------------------------------------------- repository


class _CH:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(self, sql, params=None):
        self.calls.append(("all", sql, dict(params or {})))
        if "max(computed_at) AS latest" in sql:
            latest: dict[int, datetime] = {}
            for r in self.rows:
                n = r["issue_number"]
                if n in params["nums"] and (n not in latest or r["computed_at"] > latest[n]):
                    latest[n] = r["computed_at"]
            return [{"issue_number": n, "latest": t} for n, t in latest.items()]
        if "issue_number = {n:UInt32}" in sql:
            return [r for r in self.rows if r["issue_number"] == params["n"]]
        return [r for r in self.rows if r["pr_number"] == params["n"]]

    async def query_one(self, sql, params=None):
        self.calls.append(("one", sql, dict(params or {})))
        last = max((r["computed_at"] for r in self.rows), default=None)
        return {
            "issues": len({r["issue_number"] for r in self.rows}),
            "pairs": len(self.rows),
            "last_computed": last,
        }

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, list(rows)))


def _row(issue: int, pr: int, *, rank: int, score: float, at: datetime) -> dict[str, Any]:
    return qr.build_link_row(
        org=ORG,
        qa_namespace=NS,
        issue_number=issue,
        pr_number=pr,
        rank=rank,
        score=score,
        computed_at=at,
        version=1,
    )


def _closing_row(issue: int, pr: int, *, at: datetime) -> dict[str, Any]:
    row = _row(issue, pr, rank=1, score=2.5, at=at)
    row.update(closes_mention=2, bm25_title=2.0, bm25_body=1.0, bm25_norm=1.0)
    return row


@pytest.mark.asyncio
async def test_repository_reads_the_latest_pass_only_and_validates_inserts() -> None:
    old, new = T0, T0 + timedelta(days=1)
    ch = _CH(
        [
            _row(1, 10, rank=1, score=1.9, at=old),
            _row(1, 11, rank=2, score=1.1, at=old),  # last pass
            _row(1, 11, rank=1, score=1.8, at=new),
            _row(1, 12, rank=2, score=1.0, at=new),  # this pass
            _row(2, 10, rank=1, score=1.5, at=new),
        ]
    )
    repo = qr.TrackerLinksRepo(ch)  # type: ignore[arg-type]
    got = await repo.candidates_for_issue(ORG, NS, 1, limit=10)
    assert [(r["pr_number"], r["rank"]) for r in got] == [(11, 1), (12, 2)]
    sql = ch.calls[-1][1]
    assert "FROM loom.qa_link_candidates FINAL" in sql and "PREWHERE" not in sql
    assert (
        "org = {org:String} AND qa_namespace = {ns:String}" in sql
        and "issue_number = {n:UInt32}" in sql
    )
    # the PR direction drops the pair issue 1 → PR 10 (it left issue 1's latest list)
    got = await repo.candidates_for_pr(ORG, NS, 10, limit=10)
    assert [(r["issue_number"], r["pr_number"]) for r in got] == [(2, 10)]
    stats = await repo.namespace_stats(ORG, NS)
    assert stats == {"issues": 2, "pairs": 5, "last_computed": new}
    await repo.insert_candidates([_row(3, 30, rank=1, score=1.0, at=new)])
    assert ch.inserts[0][0] == "loom.qa_link_candidates" and set(ch.inserts[0][1][0]) == set(
        qr.TRACKER_LINK_COLS
    )
    with pytest.raises(ValueError):
        await repo.insert_candidates([{"org": ORG}])
    await repo.insert_candidates([])
    assert len(ch.inserts) == 1


# --------------------------------------------------------------------- the pass


class _Source:
    def __init__(
        self,
        issues: list[dict[str, Any]],
        cands: dict[int, list[dict[str, Any]]],
        *,
        down: set[int] = frozenset(),
        facts: dict[int, list[str]] | None = None,
    ) -> None:
        self.issues, self.cands, self.down, self.facts = issues, cands, set(down), facts or {}
        self.calls: list[tuple[str, Any]] = []

    def repos(self):
        return [REPO]

    async def pr_token_stats(self, org, ns):
        self.calls.append(("vocab", None))
        return {"n": 30000, "df": _stats().df, "avg_title_len": 8.0, "avg_body_len": 900.0}

    async def link_issues(self, org, ns, *, since, before_number=0, limit=400):
        self.calls.append(("issues", before_number))
        page = [i for i in self.issues if not before_number or i["number"] < before_number]
        return page[:limit]

    async def link_pr_candidates(
        self,
        org,
        ns,
        *,
        issue_number,
        terms,
        idf,
        avg_title_len,
        avg_body_len,
        since,
        until,
        files=None,
        limit=50,
    ):
        self.calls.append(
            ("cands", {"issue": issue_number, "since": since, "until": until, "files": files})
        )
        if issue_number in self.down:
            raise TrackerUnavailable("breaker OPEN")
        return list(self.cands.get(issue_number, []))

    async def prs_facts(self, org, ns, numbers, *, names=None):
        self.calls.append(("facts", {"numbers": list(numbers), "names": list(names or [])}))
        return {n: {"pr_number": n, "matched_symbols": self.facts.get(n, [])} for n in numbers}

    async def fix_prs_of_issue(self, org, ns, number):
        if number in self.down:
            raise TrackerUnavailable("breaker OPEN")
        return [{"pr_number": p} for p in getattr(self, "record", {}).get(number, [])]

    async def edges_from(self, org, ns, refs, rels=None, *, budget_ms=None):
        return list(getattr(self, "edges", []))


class _Repo:
    def __init__(self, stamps: dict[int, datetime] | None = None) -> None:
        self.stamps = stamps or {}
        self.rows: list[dict[str, Any]] = []
        self.by_issue: dict[int, list[dict[str, Any]]] = {}
        self.by_pr: dict[int, list[dict[str, Any]]] = {}
        self.stats: dict[str, Any] = {"issues": 0, "pairs": 0, "last_computed": None}

    async def latest_computed(self, org, ns, nums):
        return {n: t for n, t in self.stamps.items() if n in nums}

    async def insert_candidates(self, rows):
        self.rows.extend(rows)

    async def candidates_for_issue(self, org, ns, n, *, limit=20):
        return list(self.by_issue.get(n, []))[:limit]

    async def candidates_for_pr(self, org, ns, n, *, limit=20):
        return list(self.by_pr.get(n, []))[:limit]

    async def namespace_stats(self, org, ns):
        return dict(self.stats)


def _issue(n: int, body: str = "a projection crash", created: datetime = T0) -> dict[str, Any]:
    return {
        "number": n,
        "source_id": f"{REPO}#{n}",
        "title": "CHECK TABLE crash after DROP PROJECTION",
        "body": body,
        "component": "MergeTree",
        "created_at": created,
        "state": "open",
        "state_reason": "",
    }


@pytest.mark.asyncio
async def test_pass_scores_each_issue_reads_the_diff_only_for_frames_and_skips_final_ones(
    monkeypatch,
) -> None:
    now = T0 + timedelta(days=40)
    final_stamp = T0 - timedelta(days=200) + timedelta(days=100)
    issues = [_issue(4), _issue(3, TRACE), _issue(2), _issue(1, created=T0 - timedelta(days=200))]
    cands = {4: _rows()[:3], 3: _rows()[:2], 2: []}
    source = _Source(issues, cands, down={4}, facts={119348: ["checkPartAndFix", "checkPartImpl"]})
    repo = _Repo(stamps={1: final_stamp})
    result = wk.TrackerLinksResult()
    monkeypatch.setattr(settings, "tracker_links_window_days", 90)
    monkeypatch.setattr(settings, "tracker_links_issue_days", 365)
    monkeypatch.setattr(settings, "tracker_links_per_issue", 20)
    await wk._run(result, now=now, repo=repo, source_factory=lambda org, ns: source)
    assert result.namespaces == 1 and result.issues_seen == 4
    assert result.issues_scored == 1 and result.issues_empty == 1 and result.issues_unavailable == 1
    assert result.issues_final == 1 and result.errors == []
    # issue 3 carries frames: the candidate read, then ONE keyed diff read of its top rows
    facts = [c for c in source.calls if c[0] == "facts"]
    assert len(facts) == 1 and facts[0][1]["names"] == [
        "checkPartAndFix",
        "IMergeTreeDataPart",
        "checkPartImpl",
    ]
    assert facts[0][1]["numbers"] == [119348, 117296]
    cand_calls = [c[1] for c in source.calls if c[0] == "cands"]
    assert [c["issue"] for c in cand_calls] == [4, 3, 2]  # the final issue 1 was never read
    assert (
        cand_calls[1]["since"] == T0 - timedelta(days=7) and cand_calls[1]["until"] == now
    )  # clamped
    assert cand_calls[1]["files"] == ["InterpreterCheckQuery.cpp"]
    # 2 statements of vocabulary + 1 page + 3 candidate reads (one failed) + 1 diff read
    assert result.statements == 2 + 1 + 2 + 1 and result.rows_written == 2 and len(repo.rows) == 2
    top = repo.rows[0]
    assert top["issue_number"] == 3 and top["pr_number"] == 119348 and top["rank"] == 1
    assert top["frames_symbols"] == 2 and top["frames_files"] == 1 and top["frames_total"] == 4
    assert {r["computed_at"] for r in repo.rows} == {now} and len(
        {r["version"] for r in repo.rows}
    ) == 1
    assert result.outcome()["issues_scored"] == 1 and result.wall_ms >= 0


@pytest.mark.asyncio
async def test_pass_notes_a_namespace_outage_skips_restricted_and_runs_the_next(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "tracker_sources_json",
        tracker_sources_json(
            f'{{"{REPO}": "{NS}", "ClickHouse/other": "tracker-other", "ClickHouse/p": "tracker-p"}}',
            restricted="tracker-p",
        ),
    )
    monkeypatch.setattr(settings, "tracker_credentials_json", tracker_credentials_json())

    class _Down(_Source):
        async def pr_token_stats(self, org, ns):
            raise TrackerUnavailable("credential unset")

    good = _Source([_issue(2)], {2: _rows()[:1]})
    result = wk.TrackerLinksResult()
    await wk._run(
        result,
        now=T0 + timedelta(days=1),
        repo=_Repo(),
        source_factory=lambda org, ns: good if ns == NS else _Down([], {}),
    )
    assert (
        result.namespaces == 1
        and len(result.errors) == 1
        and "tracker_unavailable" in result.errors[0]
    )
    assert result.issues_scored == 1 and result.skipped_restricted == 1  # no private titles in loom


def test_worker_is_registered_with_its_own_deadline(monkeypatch) -> None:
    from server.workers.registry import enabled_workers, worker_meta

    monkeypatch.setattr(settings, "tracker_links_enabled", True)
    spec = {w.name: w for w in enabled_workers()}["tracker_links"]
    assert (
        spec.interval_seconds == settings.tracker_links_interval_seconds
        and spec.deadline_seconds() == 3600.0
    )
    assert worker_meta()["tracker_links"]["enabled"] is True
    monkeypatch.setattr(settings, "tracker_links_enabled", False)
    assert "tracker_links" not in {w.name for w in enabled_workers()}


# ------------------------------------------------------------------ tracker.links


@pytest.fixture
def links_wired(monkeypatch):
    repo = _Repo()
    holder: dict[str, Any] = {"src": _Source([], {})}
    monkeypatch.setattr(qr, "TrackerLinksRepo", lambda: repo)
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: holder["src"])
    return repo, holder


def test_op_is_registered_and_its_input_has_no_one_of_validator() -> None:
    op = OPERATIONS_BY_NAME["tracker.links"]
    assert op.required_role == Role.VIEWER and op.maturity == "experimental" and op.idempotent
    # the "one of" rule lives in the handler after the gate (the gate-first rule): empty args construct
    assert TrackerLinksInput(org=ORG, namespace=NS).issue_number == 0
    from server.transport.mcp_descriptions import TOOL_DESCRIPTIONS

    assert "candidate" in TOOL_DESCRIPTIONS["tracker.links"]


@pytest.mark.asyncio
async def test_links_handler_serves_candidates_with_the_record_beside_them(links_wired) -> None:
    repo, holder = links_wired
    new = T0 + timedelta(days=30)
    repo.by_issue[115506] = [
        _row(115506, 119348, rank=1, score=1.6, at=new),
        _row(115506, 117296, rank=2, score=0.8, at=new),
    ]
    repo.stats = {"issues": 1, "pairs": 2, "last_computed": new}
    holder["src"].record = {115506: [117296]}
    out = await h.handle_tracker_links(
        TrackerLinksInput(org=ORG, namespace=NS, issue_number=115506)
    )
    assert out.verdict == "candidates" and out.subject == {"kind": "issue", "number": 115506}
    assert [i["pr_number"] for i in out.items] == [119348, 117296] and out.epistemic == "candidate"
    assert [i["confirmed_by_record"] for i in out.items] == [False, True]
    assert (
        out.record == {"status": "served", "numbers": [117296]}
        and out.computed_at == new.isoformat()
    )
    assert not out.tracker_unavailable and out.items[0]["epistemic"] == "candidate"
    # the PR direction reads the issues the graph says it fixes
    repo.by_pr[119348] = [_row(115506, 119348, rank=1, score=1.6, at=new)]
    holder["src"].edges = [
        {
            "rel_type": "fixes",
            "direction": "out",
            "dst_kind": "github_issue",
            "dst_ref": f"{REPO}#115506",
        }
    ]
    out = await h.handle_tracker_links(
        TrackerLinksInput(org=ORG, namespace=NS, pr_number=119348, include_record=True)
    )
    assert out.subject["kind"] == "pr" and out.items[0]["confirmed_by_record"] is True
    assert out.record["numbers"] == [115506]
    # the record unavailable: the candidates still stand, the flag says nobody read the graph
    holder["src"].down = {115506}
    out = await h.handle_tracker_links(
        TrackerLinksInput(org=ORG, namespace=NS, issue_number=115506)
    )
    assert (
        out.tracker_unavailable
        and out.record["status"] == "tracker_unavailable"
        and len(out.items) == 2
    )
    assert "confirmed_by_record" in out.items[0] and out.items[0]["confirmed_by_record"] is False
    # no list: computed for the namespace → none; never written → not_computed; record skipped on request
    out = await h.handle_tracker_links(
        TrackerLinksInput(org=ORG, namespace=NS, issue_number=7, include_record=False)
    )
    assert out.verdict == "none" and out.record == {"status": "skipped", "numbers": []}
    assert out.computed_at == new.isoformat()
    repo.stats = {"issues": 0, "pairs": 0, "last_computed": None}
    out = await h.handle_tracker_links(
        TrackerLinksInput(org=ORG, namespace=NS, issue_number=7, include_record=False)
    )
    assert out.verdict == "not_computed" and out.computed_at == ""
    with pytest.raises(ValidationError):
        await h.handle_tracker_links(TrackerLinksInput(org=ORG, namespace=NS))


def test_passes_floor_is_the_score_then_a_closing_keyword() -> None:
    closes = {"score": 2.5, "closes_mention": 2, "bm25_title": 1.0, "bm25_body": 1.0}
    text = {"score": 1.05, "closes_mention": 0, "bm25_title": 6.0, "bm25_body": 4.0}
    assert lk.passes_floor(closes, min_score=1.0)
    assert not lk.passes_floor({**closes, "score": 0.9}, min_score=1.0)
    assert not lk.passes_floor(text, min_score=1.0)  # a text-only lead is never the fix
    assert not lk.passes_floor({**text, "closes_mention": 1}, min_score=1.0)  # a bare mention


@pytest.mark.asyncio
async def test_fix_candidate_respects_the_floor_and_fails_open_to_none(monkeypatch) -> None:
    repo = _Repo()
    new = T0 + timedelta(days=30)
    top = _row(1, 10, rank=1, score=1.05, at=new)
    top.update(bm25_title=6.0, bm25_body=4.0, bm25_norm=1.0)
    runner = _row(1, 11, rank=2, score=0.6, at=new)
    runner.update(bm25_title=3.0, bm25_body=3.0, bm25_norm=0.6)
    repo.by_issue[1] = [top, runner]
    assert await lk.fix_candidate(ORG, NS, 1, repo=repo) is None  # text-only: never taken
    top.update(closes_mention=2, score=2.55)
    got = await lk.fix_candidate(ORG, NS, 1, repo=repo)
    assert got is not None and got["pr_number"] == 10 and got["source"] == "candidate"
    assert got["why"][0] == "closes" and got["margin"] == pytest.approx(1.667, abs=1e-3)
    monkeypatch.setattr(settings, "tracker_links_min_score", 3.0)
    assert await lk.fix_candidate(ORG, NS, 1, repo=repo) is None
    assert await lk.fix_candidate(ORG, NS, 2, repo=repo) is None
    assert await lk.fix_candidate(ORG, "", 1, repo=repo) is None

    class _Broken:
        async def candidates_for_issue(self, *a, **k):
            raise RuntimeError("table missing")

    assert await lk.fix_candidate(ORG, NS, 1, repo=_Broken()) is None


# ---------------------------------------------------------------- consumers


@pytest.fixture
def code_wired(monkeypatch):
    monkeypatch.setattr(hb, "require_code_intel", lambda: None)
    monkeypatch.setattr(hb, "schedule_code_trace", lambda **kw: None)
    monkeypatch.setattr(hbi, "require_code_intel", lambda: None)
    monkeypatch.setattr(hbi, "schedule_code_trace", lambda **kw: None)

    async def _authorized(org, code_ns, *, hides_answer=True):
        return NS if code_ns == CODE_NS else ""

    monkeypatch.setattr(binding, "authorized_tracker_namespace", _authorized)
    monkeypatch.setattr(
        hb, "resolve_branches", lambda org, ns, branches: [("code-clickhouse-26-8", "26.8")]
    )

    async def _as_of(repo, org, ns):
        return {"commit": "head", "status": "fresh"}

    monkeypatch.setattr(hb, "index_as_of", _as_of)
    import server.code.app.search_fanout as fanout

    async def _authorize(org, namespaces, *, op="code.search"):
        return None

    monkeypatch.setattr(fanout, "authorize", _authorize)
    holder: dict[str, Any] = {}
    import server.tracker.store.source as qs

    monkeypatch.setattr(qs, "TrackerSource", lambda org, ns: holder["src"])
    return holder


class _CodeRepo:
    async def pr_facts(self, org, ns, pr_number):
        return {
            "pr_number": pr_number,
            "title": "Keep a part",
            "files": ["src/Storages/MergeTree/MergeTreeData.cpp"],
            "merge_commit_sha": "e6c985" + "0" * 34,
            "merged_at": T0 + timedelta(days=24),
            "labels": [],
        }

    async def backport_prs_titled(self, org, ns, upstream, *, limit=8):
        return []

    async def symbol_names_present(self, org, ns, names):
        return {}

    async def live_paths_present(self, org, ns, paths):
        return set(paths)

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        return []

    async def pr_records_by_number(self, org, ns, numbers):
        return {}


class _TrackerSrc:
    def __init__(self, *, fixes: list | None = None, edges: list | None = None) -> None:
        self.fixes, self.edges = fixes or [], edges or []
        self.change_calls: list[int] = []

    def repos(self):
        return [REPO]

    async def items_by_number(self, org, ns, nums):
        return {
            n: {
                "repo": REPO,
                "number": n,
                "is_pr": 0,
                "title": "regression",
                "state": "open",
                "affected_versions": ["26.8"],
                "fix_versions": [],
                "reproduces_on": ["26.8"],
                "clean_on": ["26.6"],
                "labels": [],
            }
            for n in nums
        }

    async def fix_prs_of_issue(self, org, ns, number):
        return list(self.fixes)

    async def pr_change(self, org, ns, number):
        self.change_calls.append(number)
        return {
            "pr_number": number,
            "title": "Keep a part",
            "url": "u",
            "author": "a",
            "base_branch": "master",
            "minor_version": "26.9",
            "is_merged": 1,
            "is_backport_pr": 0,
            "is_reverted": 0,
            "changelog_category": "Bug Fix",
            "src_components": [],
            "merged_at": T0 + timedelta(days=24),
            "files": [
                "src/Storages/MergeTree/MergeTreeData.cpp",
                "tests/queries/0_stateless/1.sql",
            ],
            "hunk_contexts": [],
            "backport_required": 0,
        }

    async def backport_prs_of(self, org, ns, number):
        return []

    async def edges_from(self, org, ns, refs, rels=None, *, budget_ms=None):
        return list(self.edges)

    async def issue_text(self, org, ns, number, *, chars=6000):
        return "regression"

    async def token_stats(self, org, ns):
        return {}

    async def merged_prs_touching(self, org, ns, paths, versions, **kw):
        self.scan = {"paths": list(paths), "versions": list(versions)}
        return []


@pytest.mark.asyncio
async def test_backport_gap_takes_the_candidate_only_when_the_record_names_no_fix(
    code_wired, monkeypatch
) -> None:
    holder = code_wired
    holder["src"] = _TrackerSrc()
    monkeypatch.setattr(hb, "CodeRepo", lambda: _CodeRepo())
    new = T0 + timedelta(days=30)
    repo = _Repo()
    repo.by_issue[115506] = [_closing_row(115506, 119348, at=new)]
    monkeypatch.setattr(qr, "TrackerLinksRepo", lambda: repo)
    out = await hb.handle_code_backport_gap(
        CodeBackportGapInput(org=ORG, namespace=CODE_NS, issue_number=115506)
    )
    assert (
        out.fix["pr_number"] == 119348
        and out.fix["source"] == "candidate"
        and out.fix["epistemic"] == "candidate"
    )
    assert out.fix_candidate is not None and out.fix_candidate["pr_number"] == 119348
    assert out.issue["fixed_by"] == [] and out.issue["fixed_by_candidate"] == [119348]
    assert out.verdict != "unknown" and "no_fix" not in out.causes
    assert "fix_candidate" in out.model_dump()
    # a record edge wins: the candidate is not read
    holder["src"] = _TrackerSrc(
        fixes=[
            {
                "pr_number": 120000,
                "title": "Real fix",
                "is_merged": 1,
                "is_backport_pr": 0,
                "is_cherrypick_pr": 0,
                "base_branch": "master",
                "minor_version": "26.9",
            }
        ]
    )
    out = await hb.handle_code_backport_gap(
        CodeBackportGapInput(org=ORG, namespace=CODE_NS, issue_number=115506)
    )
    assert out.fix["pr_number"] == 120000 and "source" not in out.fix and out.fix_candidate is None
    assert "fix_candidate" not in out.model_dump()  # byte for byte the candidate-free payload
    # below the floor: no candidate, the plain answer
    holder["src"] = _TrackerSrc()
    repo.by_issue[115506] = [_row(115506, 119348, rank=1, score=0.4, at=new)]
    out = await hb.handle_code_backport_gap(
        CodeBackportGapInput(org=ORG, namespace=CODE_NS, issue_number=115506)
    )
    assert (
        out.verdict == "unknown"
        and "no_fix" in out.causes
        and "fix_candidate" not in out.model_dump()
    )


@pytest.mark.asyncio
async def test_bisect_names_the_fix_candidate_and_scans_its_files_when_no_frame_resolves(
    code_wired, monkeypatch
) -> None:
    holder = code_wired
    holder["src"] = _TrackerSrc()
    monkeypatch.setattr(hbi, "CodeRepo", lambda: _CodeRepo())
    new = T0 + timedelta(days=30)
    repo = _Repo()
    repo.by_issue[115506] = [_closing_row(115506, 119348, at=new)]
    monkeypatch.setattr(qr, "TrackerLinksRepo", lambda: repo)
    out = await hbi.handle_code_bisect_candidates(
        CodeBisectCandidatesInput(org=ORG, namespace=CODE_NS, issue_number=115506, marks=False)
    )
    assert out.fix_candidate is not None and out.fix_candidate["pr_number"] == 119348
    # no frame → the candidate's source files are the scan's paths (tests dropped), disclosed
    assert out.paths == ["src/Storages/MergeTree/MergeTreeData.cpp"]
    assert out.fix_candidate["paths_used"] == out.paths and holder["src"].change_calls == [119348]
    assert holder["src"].scan["paths"] == out.paths and "fix_candidate" in out.model_dump()
    # a caused_by edge: the graph names the culprit, no candidate is read
    holder["src"] = _TrackerSrc(
        edges=[
            {
                "rel_type": "caused_by",
                "direction": "out",
                "dst_kind": "github_pr",
                "dst_ref": f"{REPO}#100",
            }
        ]
    )
    out = await hbi.handle_code_bisect_candidates(
        CodeBisectCandidatesInput(org=ORG, namespace=CODE_NS, issue_number=115506, marks=False)
    )
    assert out.named_by_graph == [100] and out.fix_candidate is None
    assert "fix_candidate" not in out.model_dump()  # byte for byte the candidate-free payload


@pytest.mark.asyncio
async def test_sdk_posts_the_links_fields_it_was_given() -> None:
    from loom.tracker import TrackerClient

    class _Transport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def post(self, op_path, payload, *a, **k):
            self.calls.append((op_path, payload))
            return {}

    class _Agent:
        transport = _Transport()

    ag = _Agent()
    c = TrackerClient(ag)  # type: ignore[arg-type]
    await c.links(namespace=NS, issue_number=115506)
    assert ag.transport.calls[-1] == (
        "/v1/tracker.links",
        {"namespace": NS, "limit": 10, "issue_number": 115506},
    )
    await c.links(namespace=NS, pr_number=119348, limit=5, include_record=False)
    assert ag.transport.calls[-1][1] == {
        "namespace": NS,
        "limit": 5,
        "pr_number": 119348,
        "include_record": False,
    }
