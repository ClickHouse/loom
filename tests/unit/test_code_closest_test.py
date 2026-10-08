"""`code.tests_for` with `closest: true` names the existing
test most like the asked facet set — weighted per-kind Jaccard over whole
facet sets, with the shared and missing facets on every row."""

from __future__ import annotations

import asyncio
from typing import Any

from server.code.app import coverage as cc
from server.code.store.index import CLOSEST_KIND_WEIGHTS, closest_scores

ASK = [("function", "quantileExactWeighted"), ("engine", "Memory"), ("statement_kind", "SELECT")]


def _row(path: str, facets: list[tuple[str, str]]) -> dict[str, Any]:
    return {"path": path, "facets": facets}


def test_identical_set_scores_one_and_missing_is_the_diff() -> None:
    rows = [
        _row("t/exact.sql", ASK),
        _row(
            "t/partial.sql",
            [
                ("function", "quantileExactWeighted"),
                ("engine", "MergeTree"),
                ("statement_kind", "SELECT"),
                ("tag", "long"),
            ],
        ),
        _row("t/tagonly.sql", [("tag", "long")]),
    ]
    out = closest_scores(ASK, rows, limit=10)
    assert [o["path"] for o in out] == ["t/exact.sql", "t/partial.sql", "t/tagonly.sql"]
    assert out[0]["similarity"] == 1.0 and out[0]["missing"] == [] and out[0]["extra_facets"] == 0
    assert out[1]["missing"] == [{"kind": "engine", "name": "Memory"}]
    assert {(s["kind"], s["name"]) for s in out[1]["shared"]} == {
        ("function", "quantileExactWeighted"),
        ("statement_kind", "SELECT"),
    }
    assert out[1]["extra_facets"] == 2  # MergeTree, long
    assert out[2]["similarity"] == 0.0 < out[1]["similarity"]  # shares nothing of the ask


def test_weights_make_a_function_count_more_than_a_tag() -> None:
    ask = [("function", "arrayJoin"), ("tag", "long")]
    rows = [_row("t/fn.sql", [("function", "arrayJoin")]), _row("t/tag.sql", [("tag", "long")])]
    out = closest_scores(ask, rows)
    assert out[0]["path"] == "t/fn.sql" and out[0]["similarity"] > out[1]["similarity"]
    assert CLOSEST_KIND_WEIGHTS["function"] > CLOSEST_KIND_WEIGHTS["tag"]


def test_ties_prefer_the_tighter_exemplar_then_path() -> None:
    ask = [("setting", "dialect")]
    rows = [
        _row("t/b.sql", [("setting", "dialect"), ("tag", "x"), ("tag", "y")]),
        _row("t/a.sql", [("setting", "dialect"), ("tag", "x")]),
        _row("t/c.sql", [("setting", "dialect"), ("tag", "x")]),
    ]
    out = closest_scores(ask, rows)
    assert [o["path"] for o in out] == ["t/a.sql", "t/c.sql", "t/b.sql"]
    assert closest_scores(ask, rows, limit=1)[0]["path"] == "t/a.sql"
    assert closest_scores(ask, []) == []


class _Repo:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def facet_sets_sharing(self, org, ns, facets, *, limit=400):
        self.calls.append(("sharing", list(facets), limit))
        return [_row("t/exact.sql", ASK), _row("t/other.sql", [("engine", "Memory")])]

    async def tests_for_facets(self, org, ns, facets, *, limit=50):
        raise AssertionError("closest must not run the AND read")

    async def facets_present(self, org, ns):
        return True


class _Input:
    org = "clickhouse"
    namespace = "code-clickhouse"
    agent = "-"
    user_id = "-"
    consumer = "test"
    facets = [{"kind": k, "name": n} for k, n in ASK]
    limit = 5
    closest = True


def test_handler_closest_branch(monkeypatch) -> None:
    repo = _Repo()
    monkeypatch.setattr(cc, "CodeRepo", lambda: repo)
    monkeypatch.setattr(cc, "require_code_intel", lambda: None)
    monkeypatch.setattr(cc, "resolve_code_identity", lambda o, n: (o, n))
    monkeypatch.setattr(cc, "schedule_code_trace", lambda **kw: None)
    monkeypatch.setattr(cc, "set_code_span_attributes", lambda **kw: None)
    import server.code.app.common as common

    async def _as_of(repo, org, ns):
        return {}

    monkeypatch.setattr(common, "index_as_of", _as_of)
    out = asyncio.run(cc.handle_code_tests_for(_Input()))
    assert [t["path"] for t in out.tests] == ["t/exact.sql", "t/other.sql"]
    assert out.tests[0]["similarity"] == 1.0 and out.total == 2 and out.verdict == ""
    assert repo.calls == [("sharing", ASK, 400)]
