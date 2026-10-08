"""code.pr_check: the pure half (the draft's tokens, paths
and issue numbers; the features and the fixed score; the evidence lines; the
three-way verdict with the closed-PR cap), the handler on a stub repository
(four legs, the excluded own PR, an old merged PR capped below the duplicate
line, unavailable legs stated), the op registration, the settings and the
MCP text."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

import server.code.app.pr_check as h
from server.code.api.models_review import CodePrCheckInput
from server.code.domain import pr_check as pc
from server.core.errors import ValidationError
from server.operations.base import Role
from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings

ORG, NS = "acme", "code-app"


# ---------------------------------------------------------------- pure half


def test_draft_collects_tokens_paths_and_issue_numbers_from_text_and_input() -> None:
    d = pc.Draft.build(
        title="Fix QuotaStore.Reserve double count",
        body="<!-- template -->\nFixes #120\n\nRelated to #77 and #120.",
        paths=[" pkg/store/quota.go", "pkg/store/quota.go", ""],
        symbols=["Reserve"],
        issues=[9],
    )
    assert d.paths == ["pkg/store/quota.go"]
    assert d.issues == [9, 120, 77]
    assert "quotastore" in d.title_tokens and "reserve" in d.title_tokens
    assert not d.empty()
    assert pc.Draft.build(title="", body="", paths=[], symbols=[], issues=[]).empty()


def _cand(**kw: Any) -> pc.Candidate:
    base: dict[str, Any] = {
        "number": 41,
        "state": "merged",
        "title": "Fix QuotaStore.Reserve double count",
        "author": "ana",
        "files": ["pkg/store/quota.go", "pkg/store/quota_test.go"],
        "tokens": set("fix quotastore quota store reserve double count".split()),
        "linked_issues": [120],
        "has_body": True,
        "merged_at": datetime(2026, 9, 1, 10, 0),
    }
    base.update(kw)
    return pc.Candidate(**base)


def test_features_and_score_reward_files_issue_and_words_and_evidence_names_each() -> None:
    d = pc.Draft.build(
        title="Fix QuotaStore.Reserve double count",
        body="Fixes #120",
        paths=["pkg/store/quota.go"],
        symbols=["Reserve"],
        issues=[],
    )
    idf = {t: 1.0 for t in d.title_tokens}
    f = pc.features(d, _cand(), idf)
    assert f["text"] == 1.0 and f["issue"] == 1.0 and f["shared_paths"] == ["pkg/store/quota.go"]
    assert f["paths"] == pytest.approx(0.5 * (1.0 + 0.5))
    s = pc.score(f)
    assert s >= 0.9
    lines = pc.evidence(d, _cand(), f)
    assert lines[0].startswith("merged 2026-09-01 by ana")
    assert any(line.startswith("touches 1/1 of your files: pkg/store/quota.go") for line in lines)
    assert "closes the same issue #120" in lines
    assert any(line.startswith("title shares:") for line in lines)


def test_a_title_alone_never_reaches_the_duplicate_line() -> None:
    d = pc.Draft.build(
        title="Fix QuotaStore.Reserve double count", body="", paths=[], symbols=[], issues=[]
    )
    idf = {t: 1.0 for t in d.title_tokens}
    c = _cand(files=[], linked_issues=[], has_body=False, state="open")
    f = pc.features(d, c, idf)
    assert f["text"] == 1.0 and pc.score(f) == pytest.approx(pc.W_TEXT)
    assert pc.score(f) < float(settings.code_pr_check_duplicate_threshold)
    assert "body not indexed for this PR; matched on title and files" in pc.evidence(d, c, f)


def test_mass_changes_do_not_vote_with_their_files() -> None:
    d = pc.Draft.build(title="Rename x", body="", paths=["a.go"], symbols=[], issues=[])
    c = _cand(files=[f"f{i}.go" for i in range(300)] + ["a.go"], tokens=set(), linked_issues=[])
    f = pc.features(d, c, {}, mass_files=200)
    assert f["mass_change"] and f["paths"] == 0.0 and f["shared_paths"] == ["a.go"]
    assert any("mass change" in line for line in pc.evidence(d, c, f))


def test_a_backport_of_the_draft_is_named_as_such() -> None:
    assert pc.backport_of("Backport #118927 to 26.3: Bump openssl") == (118927, "26.3")
    assert pc.backport_of("Cherry pick #114873 to 26.3: Don't use JSON skip indexes") == (
        114873,
        "26.3",
    )
    assert pc.backport_of("Bump openssl") == (0, "")
    d = pc.Draft.build(
        title="Bump openssl",
        body="",
        paths=["contrib/openssl"],
        symbols=[],
        issues=[],
        pr_number=118927,
    )
    c = _cand(
        title="Backport #118927 to 26.3: Bump openssl", files=["contrib/openssl"], linked_issues=[]
    )
    lines = pc.evidence(d, c, pc.features(d, c, {}))
    assert "backport of your PR to 26.3" in lines
    c2 = _cand(
        title="Backport #5 to 26.1: Bump openssl", files=["contrib/openssl"], linked_issues=[]
    )
    assert "backport of #5 to 26.1" in pc.evidence(d, c2, pc.features(d, c2, {}))


def test_idf_table_falls_back_to_one_on_an_empty_corpus() -> None:
    assert pc.idf_table(["a", "b"], [1, 2], 0) == {"a": 1.0, "b": 1.0}
    t = pc.idf_table(["rare", "common"], [1, 900], 1000)
    assert t["rare"] > t["common"] > 0


def test_verdict_bands_and_the_closed_pr_cap() -> None:
    dup, rel = 0.55, 0.2
    c_open, c_closed = _cand(state="open"), _cand(state="closed")
    assert pc.verdict([(c_open, {}, 0.7)], duplicate=dup, related=rel) == ("duplicate_of", False)
    assert pc.verdict([(c_closed, {}, 0.9)], duplicate=dup, related=rel) == ("related", True)
    assert pc.verdict([(c_open, {}, 0.3)], duplicate=dup, related=rel) == ("related", False)
    assert pc.verdict([(c_open, {}, 0.4)], duplicate=dup, related=rel) == ("related", True)
    assert pc.verdict([], duplicate=dup, related=rel) == ("none", False)
    assert pc.relation(c_closed, 0.9, duplicate=dup) == "related"
    assert pc.relation(c_open, 0.6, duplicate=dup) == "duplicate"


# ---------------------------------------------------------------- handler


class _Repo:
    def __init__(self, *, fail: set[str] = frozenset()) -> None:
        self.fail = set(fail)
        self.calls: list[str] = []

    def _maybe(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail:
            raise RuntimeError(f"{name} down")

    async def gh_prs_matching(
        self,
        org: str,
        ns: str,
        *,
        state: str,
        tokens: list[str],
        paths: list[str],
        days: int = 0,
        limit: int = 400,
    ) -> list[dict[str, Any]]:
        self._maybe(state)
        assert tokens and paths
        if state == "closed":
            assert days > 0
            return [
                {
                    "number": 800,
                    "state": "closed",
                    "author": "dee",
                    "title": "Fix QuotaStore double count",
                    "labels": [],
                    "opened_at": datetime(2026, 8, 1),
                    "closed_at": datetime(2026, 8, 3),
                    "files": ["pkg/store/quota.go"],
                }
            ]
        return [
            {
                "number": 900,
                "state": "open",
                "author": "bo",
                "title": "Reserve: fix double count in QuotaStore",
                "labels": ["bug"],
                "opened_at": datetime(2026, 9, 15),
                "files": ["pkg/store/quota.go"],
            },
            {
                "number": 901,
                "state": "open",
                "author": "cy",
                "title": "Docs: quota section",
                "labels": [],
                "opened_at": datetime(2026, 9, 14),
                "files": ["docs/quota.md"],
            },
            {
                "number": 950,
                "state": "open",
                "author": "me",
                "title": "Fix QuotaStore.Reserve double count",
                "labels": [],
                "opened_at": datetime(2026, 9, 17),
                "files": ["pkg/store/quota.go"],
            },
        ]

    async def pr_history_for(
        self, org: str, ns: str, *, paths: list[str], limit: int
    ) -> list[dict[str, Any]]:
        self._maybe("merged_paths")
        return [{"pr_number": 700}, {"pr_number": 600}]

    async def pr_corpus_stats(self, org: str, ns: str, tokens: list[str]) -> tuple[list[int], int]:
        self._maybe("corpus")
        return [1 for _ in tokens], 100

    async def pr_history_search(
        self, org: str, ns: str, tokens: list[str], idfs: list[float], *, pool: int
    ) -> list[dict[str, Any]]:
        self._maybe("text")
        return [{"pr_number": 700}]

    async def prs_linking_issue(
        self, org: str, ns: str, numbers: list[int], *, limit: int
    ) -> list[dict[str, Any]]:
        self._maybe("issues")
        return [{"pr_number": 600}]

    async def merged_prs_full(self, org: str, ns: str, numbers: list[int]) -> list[dict[str, Any]]:
        self._maybe("hydrate")
        rows = {
            700: {
                "pr_number": 700,
                "title": "Fix double count in QuotaStore.Reserve",
                "author": "ana",
                "labels": [],
                "files": ["pkg/store/quota.go", "pkg/store/quota_test.go"],
                "merged_at": datetime(2026, 9, 1),
                "opened_at": datetime(2026, 8, 30),
                "linked_issues": [],
                "lexemes": "fix double count in quotastore quota store reserve",
            },
            600: {
                "pr_number": 600,
                "title": "Quota reservations",
                "author": "old",
                "labels": [],
                "files": ["pkg/store/quota.go"],
                "merged_at": datetime(2024, 1, 1),
                "opened_at": datetime(2023, 12, 20),
                "linked_issues": [120],
                "lexemes": "quota reservations fix double count reserve quotastore store",
            },
        }
        return [rows[n] for n in numbers if n in rows]


@pytest.fixture
def stub(monkeypatch):
    repo = _Repo()
    monkeypatch.setattr(h, "CodeRepo", lambda: repo)
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
    monkeypatch.setattr(settings, "code_pr_check_duplicate_threshold", 0.55, raising=False)
    monkeypatch.setattr(settings, "code_pr_check_related_threshold", 0.2, raising=False)
    return repo


def _input(**kw: Any) -> CodePrCheckInput:
    base: dict[str, Any] = {
        "org": ORG,
        "namespace": NS,
        "title": "Fix QuotaStore.Reserve double count",
        "body": "Fixes #120",
        "paths": ["pkg/store/quota.go"],
        "symbols": ["Reserve"],
    }
    base.update(kw)
    return CodePrCheckInput(**base)


@pytest.mark.asyncio
async def test_handler_finds_the_open_and_merged_duplicates_and_caps_closed_and_old(stub) -> None:
    out = await h.handle_code_pr_check(_input(pr_number=950))
    assert out.verdict == "duplicate_of" and out.epistemic == "heuristic"
    by = {it["pr_number"]: it for it in out.items}
    assert 950 not in by  # the draft's own PR is excluded
    assert by[900]["state"] == "open" and by[900]["relation"] == "duplicate"
    assert by[700]["state"] == "merged" and by[700]["relation"] == "duplicate"
    # the closed attempt and the two-year-old merge never read as duplicates
    assert by[800]["state"] == "closed" and by[800]["relation"] == "related"
    assert by[600]["relation"] == "related" and by[600]["score"] < 0.55
    assert "closes the same issue #120" in by[600]["evidence"]
    assert 901 not in by  # below the related line
    assert set(by[700]["found_by"]) == {"paths", "text"} and by[600]["found_by"] == [
        "issue",
        "old",
        "paths",
    ]
    assert out.legs["open"] == 3 and out.legs["merged_hydrated"] == 2
    assert out.items[0]["score"] >= out.items[-1]["score"]
    assert out.draft["issues"] == [120] and out.thresholds == {"duplicate": 0.55, "related": 0.2}


@pytest.mark.asyncio
async def test_handler_none_carries_no_items_and_needs_a_draft(stub) -> None:
    out = await h.handle_code_pr_check(
        _input(title="Add telemetry exporter", body="", paths=["pkg/otel/export.go"], symbols=[])
    )
    assert out.verdict == "none" and out.items == [] and out.undecided is False
    with pytest.raises(ValidationError):
        await h.handle_code_pr_check(_input(title="", body="", paths=[], symbols=[]))


@pytest.mark.asyncio
async def test_unavailable_legs_are_stated_never_a_500(monkeypatch) -> None:
    repo = _Repo(fail={"open", "closed", "merged_paths", "corpus", "issues", "hydrate"})
    monkeypatch.setattr(h, "CodeRepo", lambda: repo)
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
    out = await h.handle_code_pr_check(_input())
    assert out.verdict == "none" and out.epistemic == "unavailable"
    assert out.legs["open"].startswith("unavailable: RuntimeError")
    assert out.legs["merged_text"].startswith("unavailable")
    repo = _Repo(fail={"closed"})
    monkeypatch.setattr(h, "CodeRepo", lambda: repo)
    out = await h.handle_code_pr_check(_input())
    assert out.verdict == "duplicate_of" and out.epistemic == "lower_bound"


# ---------------------------------------------------------------- registration


def test_op_registered_viewer_experimental_with_settings_and_mcp_text() -> None:
    op = OPERATIONS_BY_NAME["code.pr_check"]
    assert op.required_role == Role.VIEWER and op.maturity == "experimental" and op.idempotent
    assert op.input_model is CodePrCheckInput
    assert (
        0
        < settings.code_pr_check_related_threshold
        < settings.code_pr_check_duplicate_threshold
        < 1
    )
    assert settings.code_pr_check_merged_days > 0 and settings.code_pr_check_mass_files > 0
    from server.transport.mcp_descriptions import TOOL_DESCRIPTIONS

    text = TOOL_DESCRIPTIONS["code.pr_check"]
    assert "duplicate_of" in text and "heuristic" in text and "title alone" in text


def test_the_pr_titled_with_the_drafts_subject_outranks_one_whose_body_mentions_it() -> None:
    draft = pc.Draft.build(
        title="Enable use_top_k_dynamic_filtering by default",
        body="",
        paths=[],
        symbols=[],
        issues=[],
    )
    words = set(draft.title_tokens)
    titled = pc.Candidate(number=1, state="merged", title=draft.title, tokens=set(words))
    mentions = pc.Candidate(
        number=2,
        state="merged",
        title="Cap fuzzer depth in CI",
        tokens=set(words) | {"cap", "fuzzer"},
    )
    idf = {t: 1.0 for t in draft.title_tokens}
    f_titled = pc.features(draft, titled, idf)
    f_mentions = pc.features(draft, mentions, idf)
    assert pc.score(f_titled) > pc.score(f_mentions)
    assert any(line.startswith("title shares:") for line in pc.evidence(draft, titled, f_titled))
    assert not any(
        line.startswith("title shares:") for line in pc.evidence(draft, mentions, f_mentions)
    )
    assert any(
        line.startswith("body mentions:") for line in pc.evidence(draft, mentions, f_mentions)
    )
