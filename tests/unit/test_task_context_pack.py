"""A context pack for a task, a repo map with call-graph hubs,
code.enclosing inside documents and config files, and no bare tool
descriptions."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from server.code.app.index import task_pack as tp
from server.code.store.index import CodeRepo
from server.settings import settings


def _pr(n: int, title: str, files: list[str], **kw: Any) -> dict[str, Any]:
    return {"pr_number": n, "title": title, "files": files, "is_revert": False, **kw}


# ------------------------------------------------------------ pure helpers


def test_task_terms_drop_stop_words_and_plurals() -> None:
    assert tp.task_terms("add a new tracker op") == {"tracker", "op"}
    assert tp.task_terms("Add columns to loom.memory") == {"column", "loom", "memory"}


def test_title_score_counts_shared_words_and_favours_a_feat_for_an_add() -> None:
    assert tp.title_score("add a new tracker op", "feat(tracker): a new op") == 2.5
    assert tp.title_score("add a new tracker op", "fix(tracker): an op refuses") == 2.0
    assert tp.title_score("change the primer", "feat(tracker): unrelated") == 0.0


def test_top_files_group_by_path_and_split_docs() -> None:
    hits = [
        {"path": "server/tracker/api/tracker.py", "start_line": 1},
        {"path": "docs/decisions/13090-x.md"},
        {"path": "server/tracker/api/tracker.py", "start_line": 90},
        {"path": "server/tracker/app/handlers.py"},
        {"path": "README.md"},
    ]
    code, docs = tp.top_files(hits)
    assert [h["path"] for h in code] == [
        "server/tracker/api/tracker.py",
        "server/tracker/app/handlers.py",
    ]
    assert code[0]["start_line"] == 1, "a file's best (first) hit stands for it"
    assert [d["path"] for d in docs] == ["docs/decisions/13090-x.md", "README.md"]


def test_top_files_prefers_implementation_over_supporting_hits() -> None:
    hits = [
        {"path": "tests/test_widget.py", "symbol": "test_widget"},
        {"path": "pyproject.toml"},
        {"path": "server/widget.py"},
        {"path": "server/widget.py", "symbol": "server.widget::build"},
        {"path": "server/api.py", "symbol": "server.api::route"},
    ]
    code, _ = tp.top_files(hits, cap=2)
    assert [h["path"] for h in code] == ["server/widget.py", "server/api.py"]
    assert code[0]["symbol"] == "server.widget::build"


def test_pick_anchors_prefers_the_folder_most_hits_sit_in() -> None:
    paths = [
        "frontend/src/views/admin/Health.tsx",
        "server/workers/supervisor.py",
        "server/observability/background.py",
        "server/workers/registry.py",
    ]
    assert tp.pick_anchors(paths) == ["server/workers/supervisor.py", "server/workers/registry.py"]
    assert tp.pick_anchors(["a/x.py", "b/y.py"]) == ["a/x.py", "b/y.py"], "ties keep search rank"


def test_co_change_floors_and_exclusions() -> None:
    anchor = "server/tracker/api/tracker.py"
    prs = [
        _pr(1, "a", [anchor, "sdk/python/loom/ops.py", "docs/decisions/1-a.md"]),
        _pr(2, "b", [anchor, "sdk/python/loom/ops.py", "server/cli.py"]),
        _pr(3, "c", [anchor, "sdk/python/loom/ops.py", "server/cli.py"]),
        _pr(4, "d", [anchor, "lonely.py"]),
        _pr(5, "revert", [anchor, "server/cli.py"], is_revert=True),
        _pr(6, "mass", [anchor, *(f"f{i}.py" for i in range(tp.MASS_PR_FILES + 1))]),
    ]
    got = {c["path"]: (c["prs"], c["of"]) for c in tp.co_changed(prs, anchor)}
    assert got == {"sdk/python/loom/ops.py": (3, 4), "server/cli.py": (2, 4)}, (
        "one-off files, decision docs, reverts and mass PRs never vote"
    )
    held_out = {c["path"] for c in tp.co_changed(prs, anchor, exclude_pr=1)}
    assert held_out == {"sdk/python/loom/ops.py", "server/cli.py"}
    assert all(c["of"] == 3 for c in tp.co_changed(prs, anchor, exclude_pr=1))


def test_past_prs_rank_by_title_match_then_newest_and_skip_the_excluded() -> None:
    prs = [
        _pr(10, "feat(tracker): a new op", ["a.py"]),
        _pr(12, "feat(tracker): another op", ["b.py"]),
        _pr(11, "fix(ui): colours", ["c.py"]),
        _pr(12, "feat(tracker): another op", ["b.py"]),  # the same PR from a second anchor
    ]
    got = [p["pr_number"] for p in tp.past_prs("add a new tracker op", prs)]
    assert got == [12, 10], "no title match -> not a template; a PR shows once"
    assert [p["pr_number"] for p in tp.past_prs("add a new tracker op", prs, exclude_pr=12)] == [10]


def test_pr_file_list_puts_source_first_and_counts_the_decision_doc() -> None:
    files = ["docs/decisions/9-x.md", "tests/unit/test_x.py", "docs/mcp.md", "server/x.py"]
    assert (
        tp.pr_file_list(files) == "server/x.py, tests/unit/test_x.py, docs/mcp.md (a decision doc)"
    )
    many = [f"server/m{i}.py" for i in range(10)]
    assert tp.pr_file_list(many, cap=3).endswith("(+7 more)")


def test_render_trims_whole_sections_from_the_bottom() -> None:
    code = [{"path": "server/x.py", "start_line": 1, "end_line": 9, "symbol": "server.x::f"}]
    co = [{"path": f"server/c{i}.py", "prs": 3, "of": 4, "anchor": "server/x.py"} for i in range(8)]
    full = tp.render("t", code, [{"path": "docs/x.md"}], co, [], ["tests/t.py"], [], 2500)
    assert "## Docs that explain it" in full and "## Related tests" in full
    small = tp.render("t", code, [{"path": "docs/x.md"}], co, [], ["tests/t.py"], [], 120)
    assert "## Where this lives" in small and "## Docs that explain it" not in small


# ------------------------------------------------------------ the builder


class _PackRepo:
    """code_pr_history + symbols + tests for build_task_pack."""

    def __init__(self, by_path: dict[str, list[dict]], under: dict[str, list[dict]]) -> None:
        self.by_path, self.under = by_path, under
        self.under_calls: list[str] = []
        self._ch = self

    # The pack's statements, run over this fake's `query_all`
    live_callers_of_symbols = CodeRepo.live_callers_of_symbols
    related_tests_for_paths = CodeRepo.related_tests_for_paths

    async def pr_history_for(self, org, ns, *, paths, limit=5, **_kw):
        return list(self.by_path.get(paths[0], []))

    async def pr_history_under(self, org, ns, *, prefix, limit=60):
        self.under_calls.append(prefix)
        return list(self.under.get(prefix, []))

    async def symbol_ids_for_names(self, org, ns, names):
        return {n: [f"id:{n}"] for n in names}

    async def query_all(self, sql, params):  # packs._callers_of / _tests_for
        if "code_edges" in sql:
            return [
                {
                    "f": "server.a::run",
                    "t": "server.x::f",
                    "path": "server/a.py",
                    "line": 3,
                    "is_live": 1,
                },
                {
                    "f": "server.a::run",
                    "t": "server.x::f",
                    "path": "server/a.py",
                    "line": 7,
                    "is_live": 1,
                },
                {
                    "f": "server.b::go",
                    "t": "server.x::f",
                    "path": "server/b.py",
                    "line": 1,
                    "is_live": 1,
                },
            ]
        return [{"test_path": "tests/unit/test_x.py"}]


def test_builder_reads_a_thin_files_folder_and_lists_one_line_per_caller() -> None:
    mig = "schemas/migrations/1580_x.sql"
    under = {
        "schemas/migrations/": [
            _pr(
                n,
                f"feat(memory): column {n}",
                [f"schemas/migrations/{n}.sql", "schemas/schema.sql", "tests/analyzer/shapes.txt"],
            )
            for n in range(1, 6)
        ]
    }
    repo = _PackRepo({mig: [_pr(1580, "feat: preview", [mig])]}, under)
    hits = [
        {"path": mig, "start_line": 1, "end_line": 9, "symbol": ""},
        {"path": "server/x.py", "start_line": 1, "end_line": 9, "symbol": "server.x::f"},
    ]
    text, named = asyncio.run(
        tp.build_task_pack(
            repo,
            "o",
            "n",
            task="add a column to loom.memory",
            hits=hits,
            token_budget=2500,
        )
    )
    assert repo.under_calls == ["schemas/migrations/"], "one PR on the file -> its folder's PRs"
    assert "- schemas/schema.sql — with a file under schemas/migrations/ in 5 of 5 PRs" in text
    assert text.count("server.a::run") == 1, "a caller calling twice is one lead"
    assert "server.b::go" in text
    assert "tests/unit/test_x.py" in text and "schemas/schema.sql" in named


def test_builder_keeps_the_file_history_when_it_is_rich_enough() -> None:
    anchor = "server/tracker/api/tracker.py"
    rich = [_pr(n, "feat(tracker): an op", [anchor, "sdk/python/loom/ops.py"]) for n in range(1, 5)]
    repo = _PackRepo({anchor: rich}, {})
    text, _ = asyncio.run(
        tp.build_task_pack(
            repo,
            "o",
            "n",
            task="add a new tracker op",
            hits=[{"path": anchor, "start_line": 1, "end_line": 2}],
            token_budget=2500,
        )
    )
    assert repo.under_calls == []
    assert f"- sdk/python/loom/ops.py — with {anchor} in 4 of 4 PRs" in text
    assert "## Merged PRs that made a change like this" in text


# ------------------------------------------------------------ the op


def _code_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")


class _Found:
    def __init__(self, results: list[dict]) -> None:
        self.results = results


def test_context_pack_with_a_task_answers_a_briefing(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.api.models_context import CodeContextPackInput
    from server.code.app import context as hc
    from server.code.app.search import handler as hs

    _code_settings(monkeypatch)
    seen: list[Any] = []

    async def search(inp):
        seen.append(inp)
        return _Found([{"path": "server/x.py", "start_line": 1, "end_line": 2}])

    async def build(repo, org, ns, *, task, hits, token_budget, exclude_pr=0):
        return f"# Task briefing: {task}", ["server/x.py"]

    monkeypatch.setattr(hs, "handle_code_search", search)
    monkeypatch.setattr(tp, "build_task_pack", build)
    monkeypatch.setattr(hc, "schedule_code_trace", lambda **k: None)
    out = asyncio.run(
        hc.handle_code_context_pack(CodeContextPackInput(task="add a new tracker op"))
    )
    assert out.source == "task" and out.context == "# Task briefing: add a new tracker op"
    assert out._paths == ["server/x.py"]  # the briefing's files: what its `lessons` read
    assert seen[0].query == "add a new tracker op" and seen[0].bodies == "none"


def test_context_pack_task_with_no_hits_falls_back_to_the_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.api.models_context import CodeContextPackInput
    from server.code.app import context as hc
    from server.code.app.search import handler as hs

    _code_settings(monkeypatch)

    async def search(inp):
        return _Found([])

    async def render_map(repo, org, ns, prefix, budget):
        return "# Repo map"

    async def docs(org, ns, text, budget):
        return text

    monkeypatch.setattr(hs, "handle_code_search", search)
    monkeypatch.setattr(hc, "_render_map", render_map)
    monkeypatch.setattr(hc, "_with_repo_docs", docs)
    monkeypatch.setattr(hc, "schedule_code_trace", lambda **k: None)
    out = asyncio.run(hc.handle_code_context_pack(CodeContextPackInput(task="zzz")))
    assert out.source == "orientation" and out.context == "# Repo map"


# ------------------------------------------------------------ the map


def test_map_alternates_include_and_call_graph_hubs() -> None:
    from server.code.app.context import blend_map_entries

    pr_rows = [
        {
            "scope_path": "frontend/src/lib/errors.ts",
            "pagerank": 0.2,
            "in_degree": 92,
            "summary": "errors",
        },
        {"scope_path": "server/core/ch.py", "pagerank": 0.01, "in_degree": 1, "summary": ""},
        {"scope_path": "frontend/src/api.ts", "pagerank": 0.1, "in_degree": 86, "summary": ""},
        {"scope_path": "server/lang_python.py", "pagerank": 0.0, "in_degree": 0, "summary": "x"},
    ]
    callers = [
        {"path": "server/core/ch.py", "callers": 211},
        {"path": "server/repo.py", "callers": 59},
    ]
    out = blend_map_entries(pr_rows, callers, {"server/repo.py": "the repo"})
    assert out == [
        "- **frontend/src/lib/errors.ts** (included by 92) — errors",
        "- **server/core/ch.py** (called from 211 files)",
        "- **server/repo.py** (called from 59 files) — the repo",
        "- **frontend/src/api.ts** (included by 86)",
    ], "a file on both lists shows once; a pagerank-0 card is not an include hub"
    # no stats at all: today's pagerank map, pagerank-0 cards included
    assert len(blend_map_entries(pr_rows, [], {})) == 4


# ------------------------------------------------------------ enclosing


def test_section_rows_name_the_heading_and_the_block() -> None:
    from server.code.app.symbol import section_rows

    rows = section_rows(
        {
            "docs/a.md": [
                {
                    "scope": "docs.a::Title::Context",
                    "kind": "comment_block",
                    "start_line": 5,
                    "end_line": 37,
                }
            ],
            "pyproject.toml": [{"scope": "", "kind": "other", "start_line": 1, "end_line": 120}],
        }
    )
    md, toml = rows["docs/a.md"][0], rows["pyproject.toml"][0]
    assert (md["name"], md["kind"], md["start_line"], md["end_line"]) == (
        "Context",
        "section",
        5,
        37,
    )
    assert (toml["name"], toml["kind"]) == ("pyproject.toml block", "block")


class _EncRepo:
    def __init__(self) -> None:
        self.section_calls: list[list[str]] = []

    async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
        return [
            {
                "symbol_id": "f",
                "qualified_name": "server.x::f",
                "name": "f",
                "signature": "",
                "path": "server/x.py",
                "start_line": 10,
                "end_line": 20,
                "tier": "treesitter",
            }
        ]

    async def types_for_paths(self, org, ns, paths, *, limit=400):
        return []

    async def chunk_spans_for_paths(self, org, ns, paths):
        self.section_calls.append(list(paths))
        return {
            "docs/a.md": [
                {
                    "scope": "docs.a::T::Decision",
                    "kind": "comment_block",
                    "start_line": 38,
                    "end_line": 81,
                }
            ]
        }


def test_enclosing_resolves_a_document_line_to_its_section(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.api.models_navigation import CodeEnclosingInput
    from server.code.app.symbol import enclosing as h

    _code_settings(monkeypatch)
    fake = _EncRepo()

    async def as_of(*a, **k):
        return {"commit": "x"}

    async def go():
        with (
            patch.object(h, "CodeRepo", return_value=fake),
            patch.object(h, "index_as_of", new=as_of),
        ):
            return await h.handle_code_enclosing(
                CodeEnclosingInput(
                    locations=["docs/a.md:40", "server/x.py:12", "server/x.py:99", "docs/a.md:5"]
                )
            )

    out = asyncio.run(go())
    md, code, module_level, before = out.results
    assert (md["symbol"], md["kind"], md["start_line"], md["end_line"]) == (
        "Decision",
        "section",
        38,
        81,
    )
    assert (code["symbol"], code["kind"]) == ("f", "function")
    assert module_level["symbol"] == "", "a code file's module-level line is unchanged: no section"
    assert before["symbol"] == "", "a line outside every chunk stays unresolved"
    assert fake.section_calls == [["docs/a.md"]], "the chunk read only for document paths"
    assert out.resolved == 2 and out.paths_indexed == 2


# ------------------------------------------------------------ descriptions


@pytest.mark.parametrize(
    "toolset", ["core", "coding", "tracker", "tasks", "core,coding,tracker,tasks", "all"]
)
def test_no_listed_tool_carries_the_bare_fallback_description(toolset: str) -> None:
    from server.transport.mcp_tools import _tools_descriptor

    tools = asyncio.run(_tools_descriptor(toolset=toolset))
    bare = [t["name"] for t in tools if t["description"].startswith("Loom operation `")]
    assert bare == [], bare


def test_context_pack_lists_task_and_says_to_pass_it() -> None:
    from server.transport.mcp_tools import _tools_descriptor

    tools = {t["name"]: t for t in asyncio.run(_tools_descriptor(toolset="coding"))}
    pack = tools["loom_code_context_pack"]
    assert "task" in pack["inputSchema"]["properties"]
    assert "`task`" in pack["description"]
