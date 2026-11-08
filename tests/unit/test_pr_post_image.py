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
        return {
            "pr_number": kw["pr_number"],
            "overlay": True,
            "head_sha": kw["head_sha"],
            "overlay_note": "1 changed file(s) whose parse timed out",
        }

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
    assert out.overlay_note == "1 changed file(s) whose parse timed out"


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
    real = ingest_parse.parse_for_ingest

    def counting(path: str, source: bytes) -> Any:
        parsed.append(path)
        return real(path, source)

    monkeypatch.setattr(ingest_parse, "parse_for_ingest", counting)
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
    monkeypatch.setattr(settings, "code_pr_overlay_parse_window_seconds", 1e-9)
    ov = asyncio.run(po.load_posted_overlay(_Repo(), "acme", "code-ch", _img()))
    assert ov.ok and ov.symbols == [] and ov.unreached == 1
    assert po.NOT_REACHED in ov.disclosure()["overlay_note"]


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
        scope = ("acme", "code-ch")
        return await asyncio.gather(
            *(po._file_facts(scope, PATH, src, "d", deadline) for _ in range(2))
        )

    first, second = asyncio.run(both())
    assert calls == [PATH]
    assert first is not None and second is not None and first[1] == second[1] != ()


def test_a_file_whose_parse_runs_past_the_deadline_is_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "code_parse_timeout_seconds", 0.2)
    slow = b"package x\nvar s = " + b"`" * 60_000
    sources = {PATH: POST.encode(), "src/slow.go": slow}
    ov = po.PrOverlay(
        pr_number=7, changed_paths=list(sources), hunks={p: [(1, 1)] for p in sources}
    )

    async def read(path: str) -> bytes:
        return sources[path]

    asyncio.run(po._parse_post_image(_Repo(), "acme", "code-ch", ov, read=read, path_prefixes=()))
    assert "checkPartsLimit" in {s.name for s in ov.symbols}
    assert any(e.to_qualified_name.endswith("checkPartsLimit") for e in ov.edges)
    assert ov.note == "1 changed file(s) whose parse timed out" and ov.unreached == 0


def test_an_open_brief_over_a_partial_overlay_is_served_unstored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.index import review_brief_build as rbb

    note = f"1 changed file(s) {po.NOT_REACHED}: the request's parse time was spent"

    async def build_brief(inp: Any, **kw: Any) -> dict[str, Any]:
        return {"pr_number": inp.pr_number, "overlay_note": note}

    class Repo:
        inserted: list[Any] = []

        async def get_state(self, *a: Any) -> str:
            return ""

        async def insert_rows(self, table: str, rows: list[Any]) -> None:
            self.inserted.append(table)

    monkeypatch.setattr(rbb, "build_brief", build_brief)
    repo = Repo()
    payload = asyncio.run(
        rbb.build_for_open(
            repo,
            "acme",
            "code-ch",
            git_dir="",
            pr_number=7,
            title="",
            head_sha="cd" * 20,
            base_sha="ef" * 20,
            call=None,
            diff=DIFF,  # type: ignore[arg-type]
        )
    )
    assert repo.inserted == [] and payload["overlay_note"] == note and payload["stored"] is False
    note = "1 changed file(s) whose parse timed out"  # stable: stored with the note
    payload = asyncio.run(
        rbb.build_for_open(
            repo,
            "acme",
            "code-ch",
            git_dir="",
            pr_number=7,
            title="",
            head_sha="cd" * 20,
            base_sha="ef" * 20,
            call=None,
            diff=DIFF,  # type: ignore[arg-type]
        )
    )
    assert repo.inserted == ["code_review_briefs"] and "stored" not in payload


def test_the_event_loop_runs_while_an_overlay_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_parse_timeout_seconds", 0.5)
    slow = (b"package x\nvar s = " + b"`" * 60_000).decode()
    img = po.PostImage(pr_number=7, files={"src/slow.go": slow}, diff="")
    ticks = 0

    async def run() -> None:
        nonlocal ticks
        parse = asyncio.ensure_future(po.load_posted_overlay(_Repo(), "acme", "code-ch", img))
        while not parse.done():
            await asyncio.sleep(0.01)
            ticks += 1
        await parse

    asyncio.run(run())
    assert ticks >= 5  # a parse on the event loop would let it tick once


def test_one_overlay_parse_runs_at_a_time_in_a_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    active, most = 0, 0
    lock = threading.Lock()

    def parse(path: str, source: bytes) -> Any:
        nonlocal active, most
        with lock:
            active += 1
            most = max(most, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return None, po._Facts((), frozenset(), False)

    monkeypatch.setattr(po, "_parsed_facts", parse)

    async def many() -> None:
        deadline = time.monotonic() + 60
        scope = ("acme", "code-ch")
        await asyncio.gather(
            *(po._file_facts(scope, f"f{i}.go", b"", f"d{i}", deadline) for i in range(5))
        )

    asyncio.run(many())
    assert most == 1


def _slow_facts(seconds: float) -> Any:
    def parse(path: str, source: bytes) -> Any:
        time.sleep(seconds)
        return None, po._Facts((path,), frozenset(), False)

    return parse


def test_a_request_waiting_past_its_window_leaves_the_file_unreached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(po, "_parsed_facts", _slow_facts(1.0))

    async def two() -> list[Any]:
        scope = ("acme", "code-ch")
        first = asyncio.ensure_future(
            po._file_facts(scope, "a.go", b"", "a", time.monotonic() + 60)
        )
        await asyncio.sleep(0.05)
        second = await po._file_facts(scope, "b.go", b"", "b", time.monotonic() + 0.05)
        return [await first, second]

    first, second = asyncio.run(two())
    assert first is not None and second is None


def test_a_parse_is_kept_when_its_request_went_away(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(po, "_parsed_facts", _slow_facts(0.1))

    async def cancelled() -> None:
        task = asyncio.ensure_future(
            po._file_facts(("acme", "code-ch"), "a.go", b"", "a", time.monotonic() + 60)
        )
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.3)

    asyncio.run(cancelled())
    assert po._FILE_FACTS.get(("a.go", "a")) == po._Facts(("a.go",), frozenset(), False)


def test_another_namespace_does_not_wait_for_its_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(po, "_parsed_facts", _slow_facts(0.3))

    async def two() -> list[Any]:
        first = asyncio.ensure_future(
            po._file_facts(("acme", "one"), "a.go", b"", "a", time.monotonic() + 60)
        )
        await asyncio.sleep(0.05)
        second = await po._file_facts(("acme", "two"), "b.go", b"", "b", time.monotonic() + 0.05)
        return [await first, second]

    first, second = asyncio.run(two())
    assert first is not None and second is not None


def test_a_started_parse_is_waited_for_one_deadline_past_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "code_parse_timeout_seconds", 0.1)
    monkeypatch.setattr(po, "_parsed_facts", _slow_facts(0.5))

    async def late() -> tuple[Any, float]:
        scope = ("acme", "code-ch")
        asyncio.ensure_future(po._file_facts(scope, "a.go", b"", "a", time.monotonic() + 60))
        await asyncio.sleep(0.02)
        start = time.monotonic()
        # the window has passed: the running parse is waited for one deadline at most
        got = await po._file_facts(scope, "a.go", b"", "a", time.monotonic() - 1)
        waited = time.monotonic() - start
        await asyncio.sleep(0.6)
        return got, waited

    got, waited = asyncio.run(late())
    assert got is None and waited < 0.3
    assert po._FILE_FACTS.get(("a.go", "a")) is not None


def test_edges_not_reached_past_the_window_are_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    source = POST.encode()
    ov = po.PrOverlay(pr_number=7, changed_paths=[PATH], hunks={PATH: [(1, 1)]})

    async def read(path: str) -> bytes:
        return source

    asyncio.run(po._parse_post_image(_Repo(), "acme", "code-ch", ov, read=read, path_prefixes=()))
    po._FILE_EDGES.clear()  # the facts stay cached: the edges need a parse again
    monkeypatch.setattr(settings, "code_pr_overlay_parse_window_seconds", 1e-9)
    monkeypatch.setattr(settings, "code_parse_timeout_seconds", 1e-9)
    again = po.PrOverlay(pr_number=7, changed_paths=[PATH], hunks={PATH: [(1, 1)]})
    asyncio.run(
        po._parse_post_image(_Repo(), "acme", "code-ch", again, read=read, path_prefixes=())
    )
    assert again.symbols and again.edges == [] and again.unreached == 1
