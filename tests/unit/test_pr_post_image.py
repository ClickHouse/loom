"""An open PR's post-image sent by its caller (`pr_files` / `pr_diff`): the
overlay parses it with no git read, so a Loom without a mirror (still cloning it,
or LOOM_GIT_HISTORY=none) answers the PR-aware reads; one the caller sent nothing to
says so."""

from __future__ import annotations

import asyncio
import time
import types
from typing import Any

import pytest

from server.code.api.models_symbol import CodeCallersInput, CodeImpactInput
from server.code.app.index import gitrepo
from server.code.app.index import pr_overlay as po
from server.code.app.index.mirrors import NO_GIT_HISTORY
from server.settings import settings

PATH = "src/Storages/MergeTreeData.cpp"
POST = """\
namespace DB
{
void MergeTreeData::checkPartsLimit(size_t n)
{
    if (n > 100)
        throw Exception("too many parts");
}

void MergeTreeData::loadDataParts()
{
    size_t n = countParts();
    checkPartsLimit(n);
}
}
"""
DIFF = f"""\
diff --git a/{PATH} b/{PATH}
--- a/{PATH}
+++ b/{PATH}
@@ -2,0 +3,6 @@
+void MergeTreeData::checkPartsLimit(size_t n)
@@ -6,0 +13 @@
+    checkPartsLimit(n);
diff --git a/src/Old.cpp b/src/Old.cpp
deleted file mode 100644
--- a/src/Old.cpp
+++ /dev/null
@@ -1 +0,0 @@
-int old() {{ return 1; }}
"""


class _Repo:
    """The master index's name table: `loadDataParts` and `countParts` exist,
    `checkPartsLimit` does not."""

    async def name_candidates(
        self, org: str, ns: str, names: list[str]
    ) -> dict[str, list[tuple[str, str, str]]]:
        table = {
            "loadDataParts": [("id-load", "DB::MergeTreeData::loadDataParts", "method")],
            "countParts": [("id-count", "DB::MergeTreeData::countParts", "method")],
        }
        return {n: table[n] for n in names if n in table}


@pytest.fixture(autouse=True)
def _no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    po.reset_cache_for_tests()

    async def refuse(*a: Any, **kw: Any) -> Any:
        raise AssertionError("a posted post-image must not read git")

    for name in ("run", "show_file", "pull_head_sha", "merge_base", "diff_name_only"):
        monkeypatch.setattr(gitrepo, name, refuse)
    cfg = types.SimpleNamespace(git_dir="/no/mirror", git_branch="master", path_prefixes=[])
    monkeypatch.setattr("server.code.domain.index.repos.repo_for_namespace", lambda org, ns: cfg)


def _img(**kw: Any) -> po.PostImage:
    return po.PostImage(pr_number=7, files={PATH: POST}, diff=DIFF, **kw)


def test_the_posted_overlay_finds_the_added_function_and_its_new_caller() -> None:
    ov = asyncio.run(po.load_posted_overlay(_Repo(), "acme", "code-ch", _img()))
    assert ov.ok and not ov.reason
    assert ov.head_sha.startswith(po.POSTED_PREFIX)
    assert [s.name for s in ov.added] == ["checkPartsLimit"]
    statuses = {s.name: s.status for s in ov.symbols}
    assert statuses["loadDataParts"] == "modified"
    edge = next(e for e in ov.edges if e.to_qualified_name.endswith("checkPartsLimit"))
    assert edge.from_qualified_name.endswith("loadDataParts")
    assert "src/Old.cpp" in ov.changed_paths  # a deletion, named by the diff only


def test_the_callers_head_sha_labels_the_answer() -> None:
    ov = asyncio.run(po.load_posted_overlay(_Repo(), "acme", "code-ch", _img(head_sha="ab" * 20)))
    assert ov.disclosure()["overlay_head_sha"] == "ab" * 20


def test_the_post_image_is_the_requests_for_nested_reads() -> None:
    seen: list[Any] = []

    @po.reads_post_image
    async def inner(inp: Any) -> None:
        seen.append(po.posted(7))

    @po.reads_post_image
    async def outer(inp: Any) -> None:
        seen.append(po.posted(7))
        await inner(CodeImpactInput(pr_number=7))  # no post-image of its own

    asyncio.run(outer(CodeCallersInput(name="x", pr_number=7, pr_files={PATH: POST})))
    assert seen[0] is not None and seen[1] is seen[0]
    assert po.posted(7) is None  # gone after the request
    assert po.posted(8) is None


def test_overlay_for_namespace_reads_the_post_image_without_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "git_history", "none")

    @po.reads_post_image
    async def read(inp: Any) -> po.PrOverlay:
        return await po.overlay_for_namespace(_Repo(), "acme", "code-ch", 7)

    sent = asyncio.run(read(CodeImpactInput(pr_number=7, pr_files={PATH: POST}, pr_diff=DIFF)))
    assert sent.ok and [s.name for s in sent.added] == ["checkPartsLimit"]
    nothing = asyncio.run(read(CodeImpactInput(pr_number=7)))
    assert not nothing.ok and nothing.reason == NO_GIT_HISTORY


def test_a_repeated_post_image_parses_once(monkeypatch: pytest.MonkeyPatch) -> None:
    first = asyncio.run(po.load_posted_overlay(_Repo(), "acme", "code-ch", _img()))
    assert asyncio.run(po.load_posted_overlay(_Repo(), "acme", "code-ch", _img())) is first
    changed = po.PostImage(pr_number=7, files={PATH: POST + "\n// pushed\n"}, diff=DIFF)
    assert asyncio.run(po.load_posted_overlay(_Repo(), "acme", "code-ch", changed)) is not first


def test_diff_paths_names_both_sides() -> None:
    assert po.diff_paths(DIFF) == [PATH, "src/Old.cpp"]


@pytest.mark.parametrize(
    "kw",
    [
        {"pr_files": {PATH: POST}},
        {"pr_number": 7, "pr_files": {"../etc/passwd": "x"}},
        {"pr_number": 7, "pr_files": {"/abs.cpp": "x"}},
        {"pr_number": 7, "pr_files": {f"f{i}.cpp": "" for i in range(51)}},
    ],
)
def test_the_post_image_input_is_checked(kw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        CodeImpactInput(**kw)


# ---- code.review_brief, open PR ------------------------------------------------


class _BriefRepo:
    def __init__(self) -> None:
        self.inserted: list[Any] = []

    async def review_brief(self, *a: Any, **kw: Any) -> None:
        return None

    async def gh_pr_head(self, *a: Any) -> dict[str, str]:
        return {"title": "Limit parts", "author": "someone"}

    async def insert_rows(self, table: str, rows: list[Any]) -> None:
        self.inserted.append((table, rows))


@pytest.mark.parametrize("history", ["local", "none"])
def test_an_open_brief_builds_from_the_post_image_and_is_never_stored(
    monkeypatch: pytest.MonkeyPatch, history: str
) -> None:
    from server.code.api.models_review import CodeReviewBriefInput
    from server.code.app import review_brief as h
    from server.code.app.index import review_brief_build

    monkeypatch.setattr(settings, "git_history", history)
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "resolve_code_identity", lambda o, n: ("acme", "code-ch"))
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
    monkeypatch.setattr(h, "set_code_span_attributes", lambda **kw: None)
    repo = _BriefRepo()
    monkeypatch.setattr(h, "CodeRepo", lambda: repo)
    built: list[dict[str, Any]] = []

    async def build_for_open(repo: Any, org: str, ns: str, **kw: Any) -> dict[str, Any]:
        built.append(kw)
        return {"pr_number": kw["pr_number"], "overlay": True, "head_sha": kw["head_sha"]}

    monkeypatch.setattr(review_brief_build, "build_for_open", build_for_open)
    out = asyncio.run(
        h.handle_code_review_brief(
            CodeReviewBriefInput(
                pr_number=7,
                open=True,
                pr_files={PATH: POST},
                pr_diff=DIFF,
                pr_head_sha="cd" * 20,
                pr_base_sha="ef" * 20,
            )
        )
    )
    [kw] = built
    assert kw["diff"] == DIFF and kw["base_sha"] == "ef" * 20 and kw["head_sha"] == "cd" * 20
    assert kw["changed_paths"] == [PATH, "src/Old.cpp"]
    assert kw["store"] is False and out.stored is False
    assert out.overlay is True and out.overlay_head_sha == "cd" * 20


def test_an_open_brief_without_history_or_a_post_image_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.api.models_review import CodeReviewBriefInput
    from server.code.app import review_brief as h

    monkeypatch.setattr(settings, "git_history", "none")
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "resolve_code_identity", lambda o, n: ("acme", "code-ch"))
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
    monkeypatch.setattr(h, "set_code_span_attributes", lambda **kw: None)
    monkeypatch.setattr(h, "CodeRepo", _BriefRepo)
    out = asyncio.run(h.handle_code_review_brief(CodeReviewBriefInput(pr_number=7, open=True)))
    assert out.source == "missing" and out.overlay_note == NO_GIT_HISTORY


# ---- code.verify_citations -----------------------------------------------------


def test_in_pr_diff_reads_the_posted_diff() -> None:
    from server.code.app.citations import _posted_pr_leg
    from server.code.domain.index import citations as cit

    inside = cit.Citation(kind="location", text=f"{PATH}:13", path=PATH, start_line=13, end_line=13)
    outside = cit.Citation(
        kind="location", text=f"{PATH}:30", path=PATH, start_line=30, end_line=30
    )
    pr = _posted_pr_leg(_img(), [inside, outside])
    assert pr["source"] == "posted" and pr["files_in_diff"] == 2
    assert inside.in_pr_diff == "true" and outside.in_pr_diff == "false"


# ---- freshness on a replica ----------------------------------------------------


def test_a_host_without_the_mirror_reports_the_published_lags() -> None:
    import json
    from datetime import UTC, datetime

    from server.code.app.index_status import STATUS_SNAPSHOT_KEY, published_lags

    snap = {
        "git_head": "h1",
        "clone_head": "tip",
        "fast_tier_lag": 2,
        "compiler_tiers": [{"watermark_key": "scip_commit", "commit": "s1", "lag": 5}],
    }
    state = {STATUS_SNAPSHOT_KEY: {"value": json.dumps(snap), "updated_at": datetime.now(UTC)}}
    marks = {"git_head": "h1", "scip_commit": "s1", "scip_commit_py": ""}
    assert published_lags(state, marks, "tip") == {
        "git_head": 2,
        "scip_commit": 5,
        "scip_commit_py": -1,
    }
    # the clone moved or the head moved since the publisher counted: unknown
    assert published_lags(state, marks, "newer")["git_head"] == -1
    assert published_lags(state, {**marks, "git_head": "h2"}, "tip")["git_head"] == -1


# ---- code.blame_range ----------------------------------------------------------


def test_blame_without_history_says_there_is_no_git_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.api.models_history import CodeBlameRangeInput
    from server.code.app import blame

    monkeypatch.setattr(settings, "git_history", "none")
    monkeypatch.setattr(blame, "require_code_intel", lambda: None)
    monkeypatch.setattr(blame, "resolve_code_identity", lambda o, n: ("acme", "code-ch"))
    monkeypatch.setattr(blame, "repo_siblings", lambda o, n: (None, []))
    monkeypatch.setattr(blame, "CodeRepo", lambda: object())
    out = asyncio.run(
        blame.handle_code_blame_range(CodeBlameRangeInput(path=PATH, start_line=1, end_line=5))
    )
    dumped = out.model_dump()
    assert blame.NO_GIT_HISTORY in str(dumped)


# ---- code.index_status(commit=) ------------------------------------------------


@pytest.mark.parametrize("git_dir", ["/no/mirror", ""])
def test_coverage_of_the_indexed_commit_needs_no_git(git_dir: str) -> None:
    from server.code.app.index import commit_coverage as cc

    cc.reset()
    head = "a" * 40
    tiers = [{"label": "C++", "watermark_key": "scip_commit", "commit": head}]
    out = asyncio.run(cc.commit_coverage(git_dir, head, head, tiers))
    assert out["ready"] is True and out["commits_since"] == 0
    assert out["tiers"][0]["covered"] is True


def test_coverage_without_the_mirror_says_it_did_not_check() -> None:
    from server.code.app.index import commit_coverage as cc

    cc.reset()
    tiers = [{"label": "C++", "watermark_key": "scip_commit", "commit": "b" * 40}]
    gap = "no_git_mirror: cloning acme/code-ch"
    out = asyncio.run(cc.commit_coverage("", "a" * 40, "b" * 40, tiers, gap=gap))
    assert out["ready"] is False and out["tiers"][0]["covered"] is None
    assert out["waiting_on"] == [f"commit not checked: {gap}"]


# ---- a later push of the same PR -----------------------------------------------

OTHER = "src/Storages/StorageMergeTree.cpp"
OTHER_POST = """\
namespace DB
{
void StorageMergeTree::startup()
{
    data.loadDataParts();
}
}
"""


class _CountingRepo(_Repo):
    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    async def name_candidates(
        self, org: str, ns: str, names: list[str]
    ) -> dict[str, list[tuple[str, str, str]]]:
        self.asked.append(list(names))
        return await super().name_candidates(org, ns, names)

    async def get_state(self, org: str, ns: str, key: str) -> str:
        return "head1"


def test_a_push_that_changes_one_file_parses_that_file_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.index import ingest_parse

    parsed: list[str] = []
    real = ingest_parse.parse_file

    def counting(path: str, source: bytes) -> Any:
        parsed.append(path)
        return real(path, source)

    monkeypatch.setattr(ingest_parse, "parse_file", counting)
    repo = _CountingRepo()
    first = po.PostImage(pr_number=7, files={PATH: POST, OTHER: OTHER_POST}, diff="")
    ov1 = asyncio.run(po.load_posted_overlay(repo, "acme", "code-ch", first))
    assert sorted(parsed) == sorted([PATH, OTHER])
    parsed.clear()
    pushed = po.PostImage(
        pr_number=7, files={PATH: POST, OTHER: OTHER_POST + "// pushed\n"}, diff=""
    )
    ov2 = asyncio.run(po.load_posted_overlay(repo, "acme", "code-ch", pushed))
    assert parsed == [OTHER]  # PATH's symbols, names and edges came from the cache
    assert len(repo.asked) == 1  # every name was already known at this index head
    assert {(e.from_qualified_name, e.to_qualified_name) for e in ov2.edges} == {
        (e.from_qualified_name, e.to_qualified_name) for e in ov1.edges
    }
    assert [s.name for s in ov2.added] == [s.name for s in ov1.added]


def test_an_overlay_past_its_parse_time_leaves_the_rest_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "code_parse_timeout_seconds", 1e-9)
    ov = asyncio.run(po.load_posted_overlay(_Repo(), "acme", "code-ch", _img()))
    assert ov.ok and ov.symbols == []
    assert ov.disclosure()["overlay_note"].startswith("1 changed file(s) not fully read")


def test_concurrent_requests_parse_a_file_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = po._parsed_facts

    def counted(path: str, source: bytes) -> Any:
        calls.append(path)
        return real(path, source)

    monkeypatch.setattr(po, "_parsed_facts", counted)

    async def both() -> list[Any]:
        deadline = time.monotonic() + 60
        src = POST.encode()
        return await asyncio.gather(*(po._file_facts(PATH, src, "d", deadline) for _ in range(2)))

    first, second = asyncio.run(both())
    assert calls == [PATH]
    assert first is not None and second is not None and first[1] == second[1] != ()
