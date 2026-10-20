"""tracker.similar: the pure half (tokens, term selection against
the vocabulary, frame normalisation in step with code.trace, position-decayed
frame score, field boosts, RRF), the source statements (privacy predicate,
time wall, the item left out, BM25F applied upstream, incidents refused on a
standard namespace), and the handler on a stub source (legs named, the
vector leg's null, `tracker_unavailable` when nothing was read)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

import server.tracker.store.source as src
import server.tracker.store.source.base as src_base
import server.tracker.store.source.qa_intel as qi
from server.code.app.symbol import trace as code_trace
from server.operations.base import Role
from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings
from server.tracker.api.models import TrackerSimilarInput
from server.tracker.app import similar as tsim
from server.tracker.app import source as tsrc
from server.tracker.domain import similar as sim
from server.tracker.domain.errors import TrackerUnavailable
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json

ORG, NS, REPO = "clickhouse", "tracker-clickhouse", "ClickHouse/ClickHouse"
PRIV, PREPO = "tracker-example-private", "ClickHouse/example-private"


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    qi.reset_for_tests()
    src.reset_cache_for_tests()
    monkeypatch.setattr(settings, "tracker_org", ORG)
    monkeypatch.setattr(
        settings,
        "tracker_sources_json",
        tracker_sources_json(f'{{"{REPO}": "{NS}", "{PREPO}": "{PRIV}"}}', restricted=PRIV),
    )
    monkeypatch.setattr(settings, "tracker_credentials_json", tracker_credentials_json())
    monkeypatch.setattr(settings, "tracker_live_serve", True)
    monkeypatch.setattr(settings, "tracker_similar_vector_leg", False)
    yield
    qi.reset_for_tests()
    src.reset_cache_for_tests()


# ---------------------------------------------------------------- pure half


def _stats() -> sim.TokenStats:
    return sim.TokenStats(
        n=20000,
        df={
            "avgweighted": 14,
            "float": 316,
            "integer": 633,
            "weights": 149,
            "incorrect": 900,
            "select": 12303,
            "merge": 3000,
            "loaddataparts": 40,
            "mergetreedata": 300,
        },
    )


def test_query_tokens_drop_stopwords_short_numbers_and_repeats() -> None:
    toks = sim.query_tokens(
        "avgWeighted returns incorrect results for Float values, 3 rows; Code: 49"
    )
    # stopwords, a list marker and a two-digit number are gone; case folds
    assert toks == ["avgweighted", "incorrect", "float"]
    assert sim.camel_pieces("avgWeighted returns dateTrunc on Date32 Float") == [
        "avg",
        "weighted",
        "date",
        "trunc",
    ]
    assert sim.query_tokens("1 2 3 02345_keeper_session") == ["02345", "keeper", "session"]
    assert sim.query_tokens("fiddle c0446e10-d2e5 commit abcdef1234 deadbeef cafe") == [
        "fiddle",
        "commit",
        "deadbeef",
        "cafe",
    ]
    # years, issue numbers, pointers and a base64 blob out of a link never become terms
    junk = (
        "since 2027 see 70529 at 0x000000000bd49ed0 "
        + "v0lusaogicagotagqvmgaw50zxj2ywxfzgf5cwptruxfq1qk"
    )
    assert sim.query_tokens(junk) == []
    assert sim.query_tokens("dateTrunc on Date32") == ["datetrunc", "date32"]
    # the three-hex `store/` directory of a UUID named in the text is the
    # UUID's own prefix (issue 87281 spent a term slot on `06f`); a three-hex
    # word with no UUID behind it (ec2, e2e) is still a term
    assert sim.query_tokens(
        "metadata file store/06f/06f90420-f81b-4b4d-b95c-04c24b8c6aaf/events.sql on ec2 e2e"
    ) == ["metadata", "store", "events", "sql", "ec2", "e2e"]
    assert sim.query_tokens("dir 06f without a uuid") == ["dir", "06f", "without", "uuid"]
    assert sim.mentioned_numbers(
        "dup of #70529, see https://github.com/x/y/issues/81631 and /pull/999 #70529"
    ) == [70529, 81631]
    assert sim.error_code_of("DB::Exception: Code: 49. LOGICAL_ERROR") == 49
    assert sim.error_code_of("no code here") == 0
    assert sim.case_variants("avgweighted") == ["AVGWEIGHTED", "Avgweighted", "avgweighted"]


def test_select_terms_prefers_title_and_rare_tokens_under_the_df_budget() -> None:
    stats = _stats()
    toks = ["select", "incorrect", "float", "integer", "weights", "avgweighted", "unseenword"]
    terms = sim.select_terms(toks, stats, prefer=["avgweighted", "incorrect"])
    # title tokens first among the kept, then rarest; `select` (61 % of the corpus) is out
    assert terms[:2] == ["avgweighted", "incorrect"] and "select" not in terms
    assert "unseenword" in terms  # never seen upstream: rarest of all, kept (one at most)
    assert sum(stats.df.get(t, 0) for t in terms) <= sim.DF_BUDGET + 900
    # deferred pieces take only the slots the tokens leave
    assert sim.select_terms(
        ["avgweighted", "weights"], stats, defer=["avg", "float"], max_terms=3
    ) == ["avgweighted", "weights", "avg"]  # rarest piece first
    # every token common: the two rarest still give an answer
    assert sim.select_terms(["select", "merge"], stats) == ["merge", "select"]
    assert sim.select_terms([], stats) == []


def test_frames_normalise_the_way_code_trace_parses() -> None:
    # the two regexes are one shape: a drift here would split the traces the two ops read
    assert sim.TRACE_SYMBOL_RE.pattern == code_trace._TRACE_SYMBOL_RE.pattern
    assert sim.TRACE_FILELINE_RE.pattern == code_trace._TRACE_FILELINE_RE.pattern
    frames = sim.normalise_frames(
        [
            "#0 std::__1::vector<int>::size() const",
            "#1 DB::MergeTreeData::loadDataParts(std::vector<int>) src/Storages/MergeTree/MergeTreeData.cpp:1234",
            "#2 DB::MergeTreeData::~MergeTreeData()",
            "#3 DB::StorageMergeTree::startup() at ./src/Storages/StorageMergeTree.cpp:88",
            "#4 DB::MergeTreeData::loadDataParts(std::vector<int>)",  # a repeat keeps its first place
        ]
    )
    assert [(f.kind, f.token) for f in frames] == [
        ("symbol", "loadDataParts"),
        ("file", "MergeTreeData"),
        ("symbol", "MergeTreeData"),
        ("symbol", "startup"),
        ("file", "StorageMergeTree"),
    ]
    assert frames[1].path == "Storages/MergeTree/MergeTreeData.cpp"
    assert frames[4].path == "Storages/StorageMergeTree.cpp"
    body = (
        "some text\n0. DB::Exception::Exception(...)\n1. DB::MergeTreeData::loadDataParts() @ 0x1\n"
        "#2 DB::StorageMergeTree::startup()\n"
    )
    assert [f.token for f in sim.frames_from_text(body)] == [
        "Exception",
        "loadDataParts",
        "startup",
    ]
    # a symbol named in prose is not a trace: no frames parsed from a body without one
    assert sim.frames_from_text("the crash is in DB::MergeTreeData::loadDataParts, see #1") == []
    assert sim.frames_from_text(body.replace("#2 DB::StorageMergeTree::startup()\n", "")) == []
    # a frame the whole corpus carries is dropped before the read; order kept
    stats = _stats()
    stats.df["exception"] = 12000
    kept = sim.select_frames(
        sim.normalise_frames(
            [
                "DB::Exception::Exception()",
                "DB::MergeTreeData::loadDataParts()",
                "DB::X::neverSeen()",
            ]
        ),
        stats,
    )
    assert [f.token for f in kept] == ["loadDataParts", "neverSeen"]
    # position decay: the innermost shared frame outweighs a later one of equal idf
    stats = _stats()
    fr = sim.normalise_frames(
        ["DB::A::zzzFirstFrame()", "DB::B::zzzSecondFrame()"]
    )  # equal (unseen) idf
    early = sim.frame_score([True, False], fr, stats)
    late = sim.frame_score([False, True], fr, stats)
    assert early > 0 and late > 0 and early > late * 1.1


def test_field_boosts_and_rrf_name_their_reasons() -> None:
    q = sim.Query(component="MergeTree", versions=["26.4"], error_code=49)
    row = {
        "source_id": f"{REPO}#1",
        "bm25": 8.0,
        "primary_component": "MergeTree",
        "affected_versions": ["26.3", "26.4"],
        "error_code": 49,
    }
    other = {
        "source_id": f"{REPO}#2",
        "bm25": 9.0,
        "primary_component": "Keeper",
        "affected_versions": [],
        "error_code": 0,
    }
    ranked = sim.rank_lexical([other, row], q, fields=True)
    # 8 + 0.4 × 9 (top score) = 11.6 beats 9: the equality features re-rank
    assert [r["source_id"] for r in ranked] == [f"{REPO}#1", f"{REPO}#2"]
    assert ranked[0]["why"] == ["lexical", "component", "version", "error_code"]
    plain = sim.rank_lexical([other, row], q, fields=False)
    assert [r["source_id"] for r in plain] == [f"{REPO}#2", f"{REPO}#1"] and plain[0]["why"] == [
        "lexical"
    ]
    fused = sim.rrf({"lexical": ranked, "frames": [{"source_id": f"{REPO}#2", "why": ["frames"]}]})
    assert fused[0]["source_id"] == f"{REPO}#2"  # two legs beat one
    assert fused[0]["why"] == ["lexical", "frames"]
    assert fused[1]["why"] == ["lexical", "component", "version", "error_code"]
    assert fused[0]["score"] == pytest.approx(1 / 62 + 1 / 61)


# ---------------------------------------------------------------- statements


class _Upstream:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.rows = rows or []

    async def __call__(self, sql, params=None, *, credential="qa", **kw):
        self.kwargs.append(dict(kw))
        self.calls.append((" ".join(sql.split()), dict(params or {}), credential))
        return list(self.rows)


@pytest.mark.asyncio
async def test_lexical_statement_carries_the_wall_the_predicate_and_bm25f(monkeypatch) -> None:
    up = _Upstream([])
    monkeypatch.setattr(qi, "query_rows", up)
    s = src.TrackerSource(ORG, NS)
    before = datetime(2026, 8, 31, 14, 38, 18)
    await s.similar_lexical(
        ORG,
        NS,
        kind="issue",
        terms=["avgweighted", "float"],
        idf=[7.2, 4.1],
        avg_title_len=10.4,
        avg_body_len=537.0,
        before=before,
        exclude=f"{REPO}#117352",
    )
    sql, params, cred = up.calls[-1]
    assert cred == "qa" and "FROM qa_intelligence.github_issues FINAL" in sql
    assert "is_private = 0 AND (hasAnyTokens(title, {variants:Array(String)})" in sql
    assert "created_at < {before:DateTime64(3)}" in sql and params["before"] == before
    assert "source_id != {exclude:String}" in sql and params["exclude"] == f"{REPO}#117352"
    # the index admits every spelling; the score uses the lower-cased terms exactly on the
    # title and as substrings on the body head, with the per-term idf the caller computed
    assert params["variants"] == [
        "AVGWEIGHTED",
        "Avgweighted",
        "FLOAT",
        "Float",
        "avgweighted",
        "float",
    ]
    assert params["toks"] == ["avgweighted", "float"] and params["idf"] == [7.2, 4.1]
    assert (
        "tokens(lower(title))" in sql
        and "countSubstringsCaseInsensitive(substring(body, 1, 6000), t)" in sql
    )
    assert params["wt"] == 3.0 and params["k1"] == 1.2 and params["b"] == 0.75
    assert "ORDER BY bm25 DESC" in sql and params["repos"] == [REPO] and params["limit"] == 50
    assert "PREWHERE" not in sql
    # PR kind reads github_prs with the columns it lacks supplied
    await s.similar_lexical(
        ORG,
        NS,
        kind="pr",
        terms=["avgweighted"],
        idf=[7.2],
        avg_title_len=10.0,
        avg_body_len=500.0,
        before=None,
    )
    sql, params, _ = up.calls[-1]
    assert "FROM qa_intelligence.github_prs FINAL" in sql and "created_at <" not in sql
    assert "CAST([] AS Array(String)) AS affected_versions" in sql and "is_merged" in sql
    assert (
        await s.similar_lexical(
            ORG,
            NS,
            kind="incident",
            terms=["x"],
            idf=[1.0],
            avg_title_len=1,
            avg_body_len=1,
            before=None,
        )
        == []
    )


@pytest.mark.asyncio
async def test_frames_item_vocab_and_vector_statements(monkeypatch) -> None:
    up = _Upstream([])
    monkeypatch.setattr(qi, "query_rows", up)
    s = src.TrackerSource(ORG, NS)
    before = datetime(2026, 9, 1)
    await s.similar_frames(
        ORG,
        NS,
        kind="issue",
        frame_tokens=["loadDataParts", "MergeTreeData"],
        paths=["Storages/MergeTree/MergeTreeData.cpp"],
        before=before,
    )
    sql, params, _ = up.calls[-1]
    assert "is_private = 0 AND hasAnyTokens(body, {variants:Array(String)})" in sql
    assert (
        "multiSearchAllPositionsCaseInsensitive( substring(body, 1, 20000), {toks:Array(String)})"
        in sql
    )
    assert params["toks"] == ["loadDataParts", "MergeTreeData"] and "created_at < {before" in sql
    # the index is asked in every spelling, the lower-cased one included
    assert {"loadDataParts", "loaddataparts", "LOADDATAPARTS", "Loaddataparts"} <= set(
        params["variants"]
    )
    # incidents only through a restricted namespace: refused before any read
    n = len(up.calls)
    with pytest.raises(TrackerUnavailable, match="restricted"):
        await s.similar_frames(ORG, NS, kind="incident", frame_tokens=["x"], paths=[], before=None)
    assert len(up.calls) == n
    # the item read: issue first, PR beside it, both keyed and predicated
    await s.similar_item(ORG, NS, 117352)
    sqls = [c[0] for c in up.calls[-2:]]
    assert any("github_issues FINAL WHERE is_private = 0 AND source_id IN" in q for q in sqls)
    assert any("github_prs FINAL WHERE is_private = 0 AND source_id IN" in q for q in sqls)
    assert all("arraySum(arrayMap(x -> abs(x), embedding)) > 0 AS has_embedding" in q for q in sqls)
    assert await s.similar_item(ORG, NS, 0) is None
    # the items a report names: keyed, under the wall, the item itself left out
    await s.similar_mentions(
        ORG,
        NS,
        numbers=[70529, 81631],
        kinds=["issue", "pr"],
        before=before,
        exclude=f"{REPO}#117352",
    )
    sql, params, _ = up.calls[-1]
    assert (
        sql.count("source_id IN {refs:Array(String)} AND created_at < {before:DateTime64(3)}") == 2
    )
    assert "UNION ALL" in sql and params["refs"] == [f"{REPO}#70529", f"{REPO}#81631"]
    assert (
        sql.count("is_private = 0 AND source_id IN") == 2 and params["exclude"] == f"{REPO}#117352"
    )
    assert await s.similar_mentions(ORG, NS, numbers=[1], kinds=["incident"], before=None) == []
    # the vocabulary: one whole read, kept for the IDF TTL, not the row TTL
    monkeypatch.setattr(settings, "tracker_similar_idf_ttl_s", 3600.0)
    up.rows = [{"tok": "avgweighted", "df": 14}]
    stats = await s.token_stats(ORG, NS)
    sql, params, _ = up.calls[-2]
    assert "arrayJoin(arrayDistinct(tokens(lower(" in sql and "HAVING df >= 3" in sql
    assert "is_private = 0 AND splitByChar('#', source_id)[1] IN {repos:Array(String)}" in sql
    assert stats["df"] == {"avgweighted": 14}
    key = next(k for k in src_base._cache if "|similar_vocab|" in k)
    assert src_base._cache[key][0] - __import__("time").monotonic() > 3000
    # the vector leg names QA's own column and skips zero vectors
    await s.similar_vector(ORG, NS, number=117352, before=before)
    sql, params, _ = up.calls[-1]
    assert (
        "cosineDistance(embedding, q)" in sql
        and "arraySum(arrayMap(x -> abs(x), embedding)) > 0" in sql
    )
    assert "source_id NOT IN {refs:Array(String)}" in sql and params["refs"] == [f"{REPO}#117352"]


# ---------------------------------------------------------------- handler


class _Src:
    def __init__(
        self, *, item: dict[str, Any] | None, restricted: bool = False, down: set[str] | None = None
    ):
        self.item, self.down = item, down or set()
        self.cfg = type("C", (), {"restricted": restricted})()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _maybe_down(self, name: str) -> None:
        if name in self.down:
            raise TrackerUnavailable(f"{name} OPEN")

    async def similar_item(self, org, ns, number):
        self.calls.append(("item", {"number": number}))
        self._maybe_down("item")
        return self.item

    async def token_stats(self, org, ns):
        self._maybe_down("stats")
        return {
            "n": 20000,
            "avg_title_len": 10.0,
            "avg_body_len": 540.0,
            "df": {
                "avgweighted": 14,
                "float": 316,
                "integer": 633,
                "weights": 149,
                "incorrect": 900,
                "loaddataparts": 40,
                "executeimpl": 5000,
                "execute": 9000,
            },
        }

    async def similar_lexical(self, org, ns, *, kind, terms, idf, before, exclude, **kw):
        self.calls.append(
            ("lexical", {"kind": kind, "terms": terms, "before": before, "exclude": exclude})
        )
        self._maybe_down("lexical")
        if kind != "issue":
            return []
        return [
            {
                "kind": "issue",
                "repo": REPO,
                "number": 31768,
                "source_id": f"{REPO}#31768",
                "title": "avgWeighted return wrong result with Decimal value",
                "state": "closed",
                "state_reason": "completed",
                "primary_component": "",
                "affected_versions": [],
                "error_code": 0,
                "created_at": datetime(2021, 11, 25),
                "url": "u/31768",
                "bm25": 9.9,
            },
            {
                "kind": "issue",
                "repo": REPO,
                "number": 80550,
                "source_id": f"{REPO}#80550",
                "title": "avgWeighted rounds float value when weight is int",
                "state": "open",
                "state_reason": "",
                "primary_component": "AggregateFunctions",
                "affected_versions": ["26.4"],
                "error_code": 0,
                "created_at": datetime(2025, 5, 20),
                "url": "u/80550",
                "bm25": 8.2,
            },
        ]

    async def similar_frames(self, org, ns, *, kind, frame_tokens, paths, before, exclude, **kw):
        self.calls.append(("frames", {"kind": kind, "frame_tokens": frame_tokens, "paths": paths}))
        self._maybe_down("frames")
        return [
            {
                "kind": "issue",
                "repo": REPO,
                "number": 80550,
                "source_id": f"{REPO}#80550",
                "title": "avgWeighted rounds float value when weight is int",
                "state": "open",
                "state_reason": "",
                "primary_component": "AggregateFunctions",
                "affected_versions": [],
                "error_code": 0,
                "created_at": datetime(2025, 5, 20),
                "url": "u/80550",
                "frame_hits": [True],
                "path_hits": [],
            }
        ]

    async def similar_mentions(self, org, ns, *, numbers, kinds, before, exclude):
        self.calls.append(("mentions", {"numbers": numbers, "kinds": kinds}))
        self._maybe_down("mentions")
        return [
            {
                "kind": "issue",
                "repo": REPO,
                "number": n,
                "source_id": f"{REPO}#{n}",
                "title": f"named #{n}",
                "state": "open",
                "state_reason": "",
                "primary_component": "",
                "affected_versions": [],
                "error_code": 0,
                "created_at": datetime(2025, 1, 1),
                "url": f"u/{n}",
            }
            for n in numbers
            if n == 31768
        ]

    async def similar_vector(self, org, ns, *, number, before, limit=50):
        self.calls.append(("vector", {"number": number}))
        return []

    # The vector leg over loom's own table, and the record's
    # relations (duplicates_of / fixes readers) the handler decorates with
    vector_rows: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    fixes: list[int] = []

    def repos(self):
        return [REPO]

    async def similar_vector_local(
        self,
        org,
        ns,
        *,
        vector,
        dim,
        kinds,
        before,
        exclude,
        limit=50,
        components=None,
        repo=None,
        extra=None,
    ):
        self.calls.append(
            (
                "vector_local",
                {
                    "dim": dim,
                    "kinds": kinds,
                    "before": before,
                    "exclude": exclude,
                    "components": components,
                },
            )
        )
        self._maybe_down("vector")
        return list(self.vector_rows)

    async def duplicates_of(self, org, ns, *, number, limit=20):
        self.calls.append(("dups", {"number": number}))
        self._maybe_down("record")
        return list(self.edges)

    async def fix_prs_of_issue(self, org, ns, number):
        self.calls.append(("fixes", {"number": number}))
        return [{"pr_number": n} for n in self.fixes]


def _item(**over: Any) -> dict[str, Any]:
    return {
        "kind": "issue",
        "source_id": f"{REPO}#117352",
        "title": "avgWeighted returns incorrect results for float values and integer weights",
        "body": "avgWeighted returns 0 for float values (see #31768).\n"
        "0. DB::AggregateFunctionAvgWeighted::loadDataParts() @ 0x1\n"
        "1. DB::Aggregator::executeImpl() @ 0x2\n2. DB::PipelineExecutor::execute() @ 0x3",
        "component": "AggregateFunctions",
        "versions": ["26.4"],
        "error_code": 0,
        "created_at": datetime(2026, 8, 31, 14, 38, 18),
        "has_embedding": False,
        **over,
    }


@pytest.mark.asyncio
async def test_similar_by_number_walls_time_excludes_itself_and_fuses_legs(monkeypatch) -> None:
    s = _Src(item=_item())
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=117352)
    )
    assert not out.tracker_unavailable and out.epistemic == "lower_bound"
    lex = next(c for c in s.calls if c[0] == "lexical")
    assert (
        lex[1]["before"] == datetime(2026, 8, 31, 14, 38, 18)
        and lex[1]["exclude"] == f"{REPO}#117352"
    )
    assert "avgweighted" in lex[1]["terms"][:3]  # the title's rare tokens lead
    assert {c[1]["kind"] for c in s.calls if c[0] == "lexical"} == {"issue", "pr"}
    fr = next(c for c in s.calls if c[0] == "frames")
    # frames parsed from the body when the caller passed none
    assert fr[1]["frame_tokens"] == ["loadDataParts"] and out.query["frames_source"] == "body"
    assert out.legs == {
        "fields": "served",
        "lexical": "served",
        "frames": "served",
        "mentions": "served",
        "vector": "skipped",
    }
    # #80550 is in the lexical (boosted) and frames legs; #31768 is lexical and named in the
    # body: two legs each, the boosted lexical rank breaks the tie
    assert [i["number"] for i in out.items] == [80550, 31768]
    assert out.items[0]["why"] == ["lexical", "component", "version", "frames"]
    assert out.items[1]["why"] == ["lexical", "mentions"] and out.query["mentions"] == [31768]
    assert next(c for c in s.calls if c[0] == "mentions")[1] == {
        "numbers": [31768],
        "kinds": ["issue", "pr"],
    }
    assert out.items[0]["state"] == "open" and out.items[1]["state_reason"] == "completed"
    assert (
        out.query["known"]
        and out.query["number"] == 117352
        and out.query["kinds"] == ["issue", "pr"]
    )


@pytest.mark.asyncio
async def test_similar_legs_can_be_armed_one_at_a_time_and_incidents_need_restricted(
    monkeypatch,
) -> None:
    s = _Src(item=_item())
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(
            org=ORG,
            namespace=NS,
            issue_number=117352,
            legs=["lexical"],
            kinds=["issue", "incident"],
        )
    )
    assert out.legs == {
        "fields": "skipped",
        "lexical": "served",
        "frames": "skipped",
        "mentions": "skipped",
        "vector": "skipped",
    }
    assert [i["number"] for i in out.items] == [31768, 80550]  # BM25F order, no boosts
    assert all(i["why"] == ["lexical"] for i in out.items)
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(
            org=ORG, namespace=NS, issue_number=117352, legs=["frames"], kinds=["issue", "incident"]
        )
    )
    assert out.legs["frames"] == "served" and out.legs["frames_incident"] == "restricted_required"
    assert [c[1]["kind"] for c in s.calls if c[0] == "frames"] == ["issue"]

    # the vector leg: naming it in `legs` arms it whatever the flag says; an
    # empty table for the namespace is `not_computed`, never an empty list read as "nothing near"
    class _Repo:
        live = 0

        async def stored_vector(self, org, ns, kind, number):
            return None

        async def namespace_stats(self, org, ns):
            return {"live": self.live, "dead": 0, "last_updated": None, "dims": []}

    class _Emb:
        model_name, dim = "fake-ngram-8", 8

        async def embed(self, text):
            return [0.5] * 8

    class _NoCursor:
        async def get(self, org, ns, kind):
            return None

    repo = _Repo()
    monkeypatch.setattr(tsim, "_embed_repo", lambda: repo)
    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: _Emb())
    monkeypatch.setattr(
        "server.tracker.app.embed_index.cursor_state", lambda code_repo=None: _NoCursor()
    )
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=117352, legs=["vector"])
    )
    assert out.legs["vector"] == "not_computed" and out.items == [] and not out.tracker_unavailable
    repo.live = 10
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=117352, legs=["vector"])
    )
    assert out.legs["vector"] == "empty"
    vl = next(c for c in s.calls if c[0] == "vector_local")[1]
    assert (
        vl["dim"] == 8
        and vl["exclude"] == ("issue", 117352)
        and vl["before"] == datetime(2026, 8, 31, 14, 38, 18)
    )


@pytest.mark.asyncio
async def test_similar_text_query_and_unavailable_source(monkeypatch) -> None:
    s = _Src(item=None)
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(
            org=ORG,
            namespace=NS,
            text="avgWeighted wrong for float weights\nCode: 43",
            frames=["DB::MergeTreeData::loadDataParts()"],
            kinds=["issue"],
        )
    )
    lex = next(c for c in s.calls if c[0] == "lexical")
    assert lex[1]["before"] is None and lex[1]["exclude"] == "" and not out.query["known"]
    assert out.query["error_code"] == 43 and out.query["frames_source"] == "caller"
    assert out.legs["lexical"] == "served" and out.items
    # one leg down: named, the others serve; every leg down: tracker_unavailable
    s = _Src(item=_item(), down={"frames"})
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=117352)
    )
    assert out.legs["frames"] == "tracker_unavailable" and out.legs["lexical"] == "served"
    assert not out.tracker_unavailable and out.items
    s = _Src(item=_item(), down={"lexical", "frames", "mentions"})
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=117352)
    )
    assert out.tracker_unavailable and out.items == []
    s = _Src(item=_item(), down={"stats"})
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=NS, issue_number=117352)
    )
    assert (
        out.tracker_unavailable and out.items == [] and out.legs["lexical"] == "tracker_unavailable"
    )


def test_similar_op_is_an_experimental_viewer_read_with_validated_input() -> None:
    op = OPERATIONS_BY_NAME["tracker.similar"]
    assert op.required_role == Role.VIEWER and op.maturity == "experimental" and op.idempotent
    assert op.budget_p99_ms == 600 and "namespace" in op.input_model.model_fields
    # the "text, issue_number or frames" rule is not the model's
    assert TrackerSimilarInput(org=ORG, namespace=NS).issue_number == 0
    with pytest.raises(ValueError, match="unknown kinds"):
        TrackerSimilarInput(org=ORG, namespace=NS, text="x y z", kinds=["slack"])
    with pytest.raises(ValueError, match="unknown legs"):
        TrackerSimilarInput(org=ORG, namespace=NS, text="x y z", legs=["graph"])
    assert TrackerSimilarInput(org=ORG, namespace=NS, frames=["DB::A::b()"]).kinds == [
        "issue",
        "pr",
    ]


@pytest.mark.asyncio
async def test_missing_subject_is_refused_in_the_handler_after_the_gate() -> None:
    """The "text, issue_number or frames" rule lives in the handler, after
    the binding gate, so a refused principal
    never learns the op's argument shape from a model-level 422."""
    from server.core.errors import ValidationError

    with pytest.raises(ValidationError, match="text, issue_number or frames is required"):
        await tsim.handle_tracker_similar(TrackerSimilarInput(org=ORG, namespace=NS))
    with pytest.raises(ValidationError):
        await tsim.handle_tracker_similar(
            TrackerSimilarInput(org=ORG, namespace=NS, frames=["  "], text=" ")
        )


@pytest.mark.asyncio
async def test_text_index_statements_turn_the_direct_read_off(monkeypatch) -> None:
    """The upstream's direct read from the text index loses rows under
    ORDER BY … LIMIT over hasAnyTokens; every text-index statement of
    tracker.similar turns it off."""
    up = _Upstream([])
    monkeypatch.setattr(qi, "query_rows", up)
    s = src.TrackerSource(ORG, NS)
    await s.similar_lexical(
        ORG,
        NS,
        kind="issue",
        terms=["keeper"],
        idf=[3.0],
        avg_title_len=10.0,
        avg_body_len=500.0,
        before=None,
    )
    assert up.kwargs[-1].get("query_settings") == {"query_plan_direct_read_from_text_index": 0}
    await s.similar_frames(
        ORG, NS, kind="issue", frame_tokens=["DB::Keeper::run"], paths=[], before=None
    )
    assert up.kwargs[-1].get("query_settings") == {"query_plan_direct_read_from_text_index": 0}


def test_select_terms_keeps_title_precedence_and_caps_unseen_words() -> None:
    """Never-seen tokens are capped at one (a finding's own symbol names
    match no report), and the title keeps its precedence: capping title
    tokens too would drop a gold target from lexical rank 1 to 46
    (`mergetreedata`, `proxy` giving way to `l533`)."""
    stats = sim.TokenStats(
        n=20000,
        df={
            "typed": 171,
            "aggregatefunction": 262,
            "parameter": 719,
            "attach": 723,
            "quantile": 76,
            "float64": 816,
            "getnameimpl": 3,
            "parsefield": 3,
            "fails": 2000,
        },
    )
    title = ["typed", "aggregatefunction", "parameter", "attach", "fails"]
    body = ["quantile", "float64", "trippable", "fieldvisitor", "getnameimpl", "parsefield"]
    terms = sim.select_terms([*title, *body], stats, prefer=title)
    # title tokens first (`fails` is over the df ceiling), one unseen word, then the rarest
    assert terms[:4] == ["typed", "aggregatefunction", "parameter", "attach"]
    assert sum(1 for tok in terms if tok in ("trippable", "fieldvisitor")) == 1
    assert "getnameimpl" in terms and "parsefield" in terms and "quantile" in terms
    assert len(terms) == sim.MAX_TERMS and "fails" not in terms
    assert sim.select_terms(
        ["typed", "parameter", "attach", "quantile"],
        stats,
        prefer=["typed", "parameter", "attach"],
        max_terms=4,
    ) == ["typed", "parameter", "attach", "quantile"]


def test_query_tokens_drop_line_references() -> None:
    """`l533` / `L2247` are a finding's line references — rare in the
    vocabulary, so they won term slots, and no report shares them."""
    assert sim.query_tokens("proxy hides MergeTreeData at L2247 and l533; lazy") == [
        "proxy",
        "hides",
        "mergetreedata",
        "lazy",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("held", ["switched_off", "unread"])
async def test_a_held_vector_leg_answers_the_refusal_not_a_degraded_answer(
    monkeypatch, held
) -> None:
    from server.llm import customer

    error = (
        customer.ModelCallsDisabled("model calls are switched off for org 'clickhouse'")
        if held == "switched_off"
        else customer.SwitchUnread("the model-call switch could not be read")
    )
    s = _Src(item=_item())
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)

    class _Repo:
        async def stored_vector(self, org, ns, kind, number):
            return None  # the query text is embedded

        async def namespace_stats(self, org, ns):
            return {"live": 10, "dead": 0, "last_updated": None, "dims": []}

    class _Refusing:
        model_name, dim = "fake-ngram-8", 8

        async def embed(self, text):
            raise error

    class _NoCursor:
        async def get(self, org, ns, kind):
            return None

    monkeypatch.setattr(tsim, "_embed_repo", lambda: _Repo())
    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: _Refusing())
    monkeypatch.setattr(
        "server.tracker.app.embed_index.cursor_state", lambda code_repo=None: _NoCursor()
    )
    with pytest.raises(type(error)):  # 403 / 503, as memory.search answers
        await tsim.handle_tracker_similar(
            TrackerSimilarInput(org=ORG, namespace=NS, issue_number=117352, legs=["vector"])
        )
