"""`lessons` on code answers: the memories of their top files, bounded, read only from
readable namespaces, stale ones marked, never stored in the search memo, skipped on
opt-out."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from server.code.api.models_navigation import CodeOutlineInput, CodeOutlineOutput
from server.code.api.models_search import CodeSearchInput, CodeSearchOutput
from server.code.app import result_lessons, search_memo
from server.memory.app import code_plane_reads, path_lessons
from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings


def _hit(
    key: str,
    ns: str = "mem-home",
    *,
    state: str = "",
    value: str = "",
    files: tuple[str, ...] = ("a.py",),
    tags: tuple[str, ...] = (),
    value_truncated: bool = False,
) -> dict[str, Any]:
    return {
        "memory_key": key,
        "memory_type": "semantic",
        "tags": list(tags),
        "namespace": ns,
        "value": json.dumps(value or f"Note on {key}"),
        "value_truncated": value_truncated,
        "code_state": state,
        "matched_files": list(files),
        "memory_id": f"{ns}/{key}",
    }


def _listing(by_path: dict[str, list[dict[str, Any]]], seen: list[Any]) -> Any:
    async def _rows(inp: Any) -> tuple[list[dict[str, Any]], bool]:
        seen.append(inp)
        return by_path.get(inp.path, []), False

    return _rows


async def _lessons(
    monkeypatch: pytest.MonkeyPatch,
    by_path: dict[str, list[dict[str, Any]]],
    paths: list[str],
    *,
    nss: tuple[str, ...] = ("mem-home", "code-repo"),
    budget: int = 2000,
    seen: list[Any] | None = None,
) -> list[dict[str, Any]]:
    monkeypatch.setattr(
        path_lessons, "listing_rows", _listing(by_path, [] if seen is None else seen)
    )
    found = await path_lessons.lessons_for_paths(
        "acme", list(nss), "code-repo", paths, token_budget=budget
    )
    return found.rows


@pytest.mark.asyncio
async def test_rows_merge_in_path_order_once_each_with_the_code_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []
    by_path = {
        "a.py": [_hit("design/a", state="changed"), _hit("rule/x", "code-repo")],
        "b.py": [_hit("design/a", state="fresh"), _hit("design/b", files=("b.py",))],
    }
    rows = await _lessons(monkeypatch, by_path, ["a.py", "b.py"], seen=seen)
    assert [(r["path"], r["memory_key"]) for r in rows] == [
        ("a.py", "design/a"),
        ("a.py", "rule/x"),
        ("b.py", "design/b"),
    ]
    assert rows[0]["code_state"] == "changed" and rows[0]["excerpt"] == "Note on design/a"
    # one read per path from the caller's namespace, the trunk read beside it (its team
    # rows and its code links), never on its own as well
    assert [(i.namespace, i.path) for i in seen] == [("mem-home", "a.py"), ("mem-home", "b.py")]
    assert all(i.extra_namespaces == ["code-repo"] for i in seen)


@pytest.mark.asyncio
async def test_the_trunk_alone_is_read_when_it_is_all_the_caller_may_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []
    await _lessons(monkeypatch, {}, ["a.py"], nss=("code-repo",), seen=seen)
    assert [(i.namespace, i.extra_namespaces) for i in seen] == [("code-repo", ["code-repo"])]


@pytest.mark.asyncio
async def test_several_caller_namespaces_share_one_trunk_read_per_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []
    nss = ("a", "b", "c", "code-repo")
    await _lessons(monkeypatch, {}, ["x.py"], nss=nss, seen=seen)
    assert [(i.namespace, i.extra_namespaces) for i in seen] == [
        ("a", ["code-repo", "b"]),
        ("c", ["code-repo"]),
    ]
    assert all(i.top_k == path_lessons.PER_PATH_ROWS == 50 for i in seen)


@pytest.mark.asyncio
async def test_nothing_readable_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    assert await _lessons(monkeypatch, {}, ["a.py"], nss=(), seen=seen) == []
    assert seen == []


@pytest.mark.asyncio
async def test_another_file_of_the_same_name_brings_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    by_path = {
        "server/code/handler.py": [
            _hit("other", files=("server/memory/handler.py",)),
            _hit("bare", files=("handler.py",)),
            _hit("tail", files=("code/handler.py",)),
            _hit("absolute", files=("/home/u/loom/server/code/handler.py",)),
            _hit("linked", state="moved", files=()),
            _hit("exact", files=("server/code/handler.py",)),
        ]
    }
    rows = await _lessons(monkeypatch, by_path, ["server/code/handler.py"])
    assert [r["memory_key"] for r in rows] == ["bare", "tail", "absolute", "linked", "exact"]
    # a longer relative path is another directory's file, however deep; an absolute one
    # ending in the path names it
    mono = {
        "src/index.ts": [
            _hit("other-package", files=("packages/web/src/index.ts",)),
            _hit("one-folder", files=("frontend/src/index.ts",)),
            _hit("absolute", files=("/home/u/widgets/src/index.ts",)),
        ]
    }
    rows = await _lessons(monkeypatch, mono, ["src/index.ts"])
    assert [r["memory_key"] for r in rows] == ["absolute"]


async def _left_out(paths: list[str]) -> int:
    """`lessons_left_out` of the read `_lessons` last stubbed."""
    found = await path_lessons.lessons_for_paths(
        "acme", ["mem-home", "code-repo"], "code-repo", paths, token_budget=2000
    )
    return found.left_out


@pytest.mark.asyncio
async def test_a_row_another_row_supersedes_is_never_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    newer = _hit("rule/never", value="Never X. " * 60, tags=("constraint",), value_truncated=True)
    newer["supersedes_key"] = "rule/always"
    newer["supersedes_ids"] = ["mem-home/rule/always"]
    older = _hit("rule/always", value="Always X.")
    other = _hit("note/z")
    rows = await _lessons(monkeypatch, {"a.py": [newer, older, other]}, ["a.py"])
    # the newer constraint cannot print whole and the row it replaces is stale: both are
    # left out, and counted
    assert [r["memory_key"] for r in rows] == ["note/z"]
    assert await _left_out(["a.py"]) == 2
    # a superseder about another file of the same name still marks this file's row stale
    elsewhere = dict(newer, value=json.dumps("Never X."), value_truncated=False)
    elsewhere["matched_files"] = ["lib/a.py"]
    rows = await _lessons(monkeypatch, {"a.py": [elsewhere, older]}, ["a.py"])
    assert rows == []
    assert await _left_out(["a.py"]) == 1
    # served, the newer row names the one it replaces
    shown = dict(newer, value=json.dumps("Never X."), value_truncated=False)
    rows = await _lessons(monkeypatch, {"a.py": [older, shown]}, ["a.py"])
    assert [(r["memory_key"], r.get("supersedes_key")) for r in rows] == [
        ("rule/never", "rule/always")
    ]


@pytest.mark.asyncio
async def test_a_stale_row_stays_out_for_every_file_and_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long = "word " * 200
    newer = _hit("rule/new", value=long, files=("b.py",))
    newer["supersedes_ids"] = ["mem-home/rule/old"]
    older = _hit("rule/old", value="Keep 32.", files=("a.py", "b.py"))
    monkeypatch.setattr(
        path_lessons, "listing_rows", _listing({"a.py": [older], "b.py": [older, newer]}, [])
    )
    # the older row comes first, in a.py's read, and its superseder only in b.py's
    found = await path_lessons.lessons_for_paths(
        "acme", ["mem-home"], "", ["a.py", "b.py"], token_budget=60
    )
    assert found.rows == []
    assert found.left_out == 2


@pytest.mark.asyncio
async def test_a_row_left_out_for_one_file_but_served_for_another_is_not_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deep = "server/a/much/longer/path/x.py"
    shared = _hit("note/shared", value="Short.", files=(deep, "x.py"))
    big = _hit("note/big", value="word " * 40, files=(deep,))
    monkeypatch.setattr(
        path_lessons, "listing_rows", _listing({deep: [big, shared], "x.py": [shared]}, [])
    )
    # the cap fits the shared row under the short path only: a row's cost includes its path
    cap = path_lessons._tokens(path_lessons._lesson(shared, "x.py") or {})
    found = await path_lessons.lessons_for_paths(
        "acme", ["mem-home"], "", [deep, "x.py"], token_budget=cap
    )
    assert [(r["path"], r["memory_key"]) for r in found.rows] == [("x.py", "note/shared")]
    assert found.left_out == 1  # the big row; the shared one was served


@pytest.mark.asyncio
async def test_a_root_file_takes_only_rows_naming_it_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    by_path = {
        "Makefile": [
            _hit("frontend", files=("frontend/Makefile",)),
            _hit("abs", files=("/home/u/frontend/Makefile",)),  # any directory's Makefile
            _hit("root", files=("Makefile",)),
        ]
    }
    rows = await _lessons(monkeypatch, by_path, ["Makefile"])
    assert [r["memory_key"] for r in rows] == ["root"]


@pytest.mark.asyncio
async def test_the_token_cap_holds_on_serialized_rows_and_a_note_is_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long = "word " * 200
    by_path = {"a.py": [_hit("k/long", value=long), _hit("k/short"), _hit("k/2", value=long)]}
    rows = await _lessons(monkeypatch, by_path, ["a.py"], budget=110)
    # the long row fits alone; the next long one would pass the cap, the short one fits
    assert [r["memory_key"] for r in rows] == ["k/long", "k/short"]
    assert len(rows[0]["excerpt"]) == path_lessons.EXCERPT_CHARS
    assert rows[0]["excerpt"].endswith("…")
    assert sum(len(json.dumps(r, ensure_ascii=False)) // 4 + 1 for r in rows) <= 110


@pytest.mark.asyncio
async def test_a_decision_is_served_whole_or_not_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    long = "Never call allocate() here. " * 20
    by_path = {
        "a.py": [
            _hit("design/whole", value=long, tags=("decision",)),
            _hit("design/cut", value=long, tags=("constraint",), value_truncated=True),
        ]
    }
    rows = await _lessons(monkeypatch, by_path, ["a.py"])
    assert [r["memory_key"] for r in rows] == ["design/whole"]
    assert rows[0]["excerpt"] == " ".join(long.split())
    # too long for the cap: left out, never cut
    assert await _lessons(monkeypatch, by_path, ["a.py"], budget=60) == []


def _trunk(org: str, ns: str) -> str:
    """code-* namespaces index a repository each, their own trunk; others index none."""
    return ns if ns.startswith("code-") else ""


def _stub_reads(
    monkeypatch: pytest.MonkeyPatch, readable: list[str], calls: list[dict[str, Any]]
) -> None:
    async def _nss(org: str, code_ns: str, extra: list[str]) -> list[str]:
        return [n for n in [*extra, code_ns] if n in readable]

    async def _read(
        org: str, nss: list[str], trunk: str, paths: list[str], **kw: Any
    ) -> path_lessons.Lessons:
        calls.append({"nss": nss, "trunk": trunk, "paths": paths, **kw})
        return path_lessons.Lessons(
            [{"path": paths[0], "memory_key": "k", "namespace": nss[0], "excerpt": "t"}]
        )

    monkeypatch.setattr(code_plane_reads, "lesson_namespaces", _nss)
    monkeypatch.setattr(path_lessons, "lessons_for_paths", _read)
    monkeypatch.setattr("server.code.app.memory_links.trunk_namespace", _trunk)
    monkeypatch.setattr("server.identity.app.request_scope.request_narrowed", lambda: False)


def _input(**kw: Any) -> CodeSearchInput:
    return CodeSearchInput(org="acme", namespace="code-repo", query="q", **kw)


@pytest.mark.asyncio
async def test_reach_only_readable_namespaces_and_the_top_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    _stub_reads(monkeypatch, ["code-repo", "mem-home"], calls)
    monkeypatch.setattr(settings, "code_result_lessons_paths", 2)
    inp = _input(lesson_namespaces=["mem-home", "someone-else"])
    found = await result_lessons.lessons_for(
        inp, ["./a.py", "", "a.py", "/b.py", "c.py"], time.monotonic()
    )
    assert found.rows and calls[0]["nss"] == ["mem-home", "code-repo"]
    assert calls[0]["trunk"] == "code-repo"
    # code.impact passes the caller's spelling: one file, read under the index's path
    assert calls[0]["paths"] == ["a.py", "b.py"]
    # another repository's code namespace, though readable, brings none of its links
    _stub_reads(monkeypatch, ["code-repo", "mem-home", "code-other"], calls)
    await result_lessons.lessons_for(
        _input(lesson_namespaces=["mem-home", "code-other"]), ["a.py"], time.monotonic()
    )
    assert calls[-1]["nss"] == ["mem-home", "code-repo"]
    assert calls[0]["token_budget"] == settings.code_result_lessons_tokens


@pytest.mark.asyncio
async def test_a_namespace_the_caller_cannot_read_is_never_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real reach chain (`lesson_namespaces` -> `readable_namespaces` -> the role
    check), only the role check faked."""
    from server.auth import authz

    async def _in_scope(org: str, ns: str, role: Any, *, hides_answer: bool = True) -> bool:
        return ns in {"mem-home", "code-repo"}

    calls: list[dict[str, Any]] = []

    async def _read(
        org: str, nss: list[str], trunk: str, paths: list[str], **kw: Any
    ) -> path_lessons.Lessons:
        calls.append({"nss": nss, "trunk": trunk})
        return path_lessons.Lessons([])

    monkeypatch.setattr(authz, "row_in_scope", _in_scope)
    monkeypatch.setattr("server.auth.context.current_auth", lambda: object())
    monkeypatch.setattr("server.code.app.memory_links.trunk_namespace", _trunk)
    monkeypatch.setattr("server.identity.app.request_scope.request_narrowed", lambda: False)
    monkeypatch.setattr(path_lessons, "lessons_for_paths", _read)
    inp = _input(lesson_namespaces=["mem-home", "someone-else"])
    await result_lessons.lessons_for(inp, ["a.py"], time.monotonic())
    assert calls == [{"nss": ["mem-home", "code-repo"], "trunk": "code-repo"}]


@pytest.mark.asyncio
async def test_an_unreadable_trunk_is_not_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _stub_reads(monkeypatch, ["mem-home"], calls)
    await result_lessons.lessons_for(
        _input(lesson_namespaces=["mem-home"]), ["a.py"], time.monotonic()
    )
    assert calls[0]["nss"] == ["mem-home"] and calls[0]["trunk"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["argument", "setting", "narrowed", "no_paths"])
async def test_opt_out_skips_the_read(monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    calls: list[dict[str, Any]] = []
    _stub_reads(monkeypatch, ["code-repo"], calls)
    paths = ["a.py"]
    inp = _input()
    if how == "argument":
        inp = _input(include_lessons=False)
    elif how == "setting":
        monkeypatch.setattr(settings, "code_result_lessons", False)
    elif how == "narrowed":
        monkeypatch.setattr("server.identity.app.request_scope.request_narrowed", lambda: True)
    else:
        paths = []
    assert (await result_lessons.lessons_for(inp, paths, time.monotonic())).rows == []
    assert calls == []


@pytest.mark.asyncio
async def test_a_slow_or_failing_read_leaves_no_lessons(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_reads(monkeypatch, ["code-repo"], [])

    async def _slow(*a: Any, **kw: Any) -> Any:
        await asyncio.sleep(5)
        return path_lessons.Lessons([{"memory_key": "late"}])

    async def _fails(*a: Any, **kw: Any) -> path_lessons.Lessons:
        raise RuntimeError("ClickHouse down")

    monkeypatch.setattr(settings, "code_enrich_timeout_s", 0.05)
    for read in (_slow, _fails):
        monkeypatch.setattr(path_lessons, "lessons_for_paths", read)
        assert (await result_lessons.lessons_for(_input(), ["a.py"], time.monotonic())).rows == []


@pytest.mark.asyncio
async def test_a_call_past_its_deadline_reads_no_lessons(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _stub_reads(monkeypatch, ["code-repo"], calls)
    late = time.monotonic() - 1.0  # the call started a second ago
    assert (await result_lessons.lessons_for(_input(deadline_ms=200), ["a.py"], late)).rows == []
    assert calls == []
    # a deadline with less left than the enrichment bound cuts the bound to what is left
    monkeypatch.setattr(settings, "code_enrich_timeout_s", 5.0)
    assert 0 < result_lessons._bound_s(_input(deadline_ms=1000), time.monotonic() - 0.5) < 0.6
    # a deadline under 200 ms reads as 200, as the search reads it
    assert 0.1 < result_lessons._bound_s(_input(deadline_ms=100), time.monotonic() - 0.05) <= 0.2
    # no deadline: the enrichment bound alone
    assert (await result_lessons.lessons_for(_input(deadline_ms=0), ["a.py"], late)).rows
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_the_op_handler_attaches_lessons_after_its_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    _stub_reads(monkeypatch, ["code-repo"], calls)

    async def _outline(inp: CodeOutlineInput) -> CodeOutlineOutput:
        return CodeOutlineOutput(path="src/a.py", entries=[], total=0, returned=0, elided=0)

    wrapped = result_lessons.WithLessons(_outline, result_lessons.outline_paths)
    out = await wrapped(CodeOutlineInput(org="acme", namespace="code-repo", path="src/a.py"))
    assert out.lessons[0]["memory_key"] == "k" and calls[0]["paths"] == ["src/a.py"]


@pytest.mark.parametrize(
    "name",
    [
        "code.search",
        "code.callers",
        "code.callees",
        "code.impact",
        "code.outline",
        "code.context_pack",
    ],
)
def test_no_lessons_leave_no_key_on_the_wire(name: str) -> None:
    model = OPERATIONS_BY_NAME[name].output_model
    required = {n: f.annotation for n, f in model.model_fields.items() if f.is_required()}
    samples = {str: "", int: 0, float: 0.0, bool: False}
    fields = {}
    for n, ann in required.items():
        if ann in samples:
            fields[n] = samples[ann]
        elif "Literal" in str(ann):
            fields[n] = ann.__args__[0]
        else:
            fields[n] = []
    out = model(**fields)
    assert "lessons" not in out.model_dump(), name
    assert "lessons_left_out" not in out.model_dump(), name
    out.lessons_left_out = 2  # rows left out still say so with none served
    assert out.model_dump()["lessons_left_out"] == 2, name
    assert "lessons" not in out.model_dump(), name
    out.lessons = [{"memory_key": "k"}]
    assert out.model_dump()["lessons"] == [{"memory_key": "k"}], name


@pytest.mark.asyncio
async def test_a_one_entry_namespaces_search_reads_that_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    asked: list[str] = []
    _stub_reads(monkeypatch, ["code-other", "mem-home"], calls)

    def _resolve(org: str, ns: str) -> tuple[str, str]:
        asked.append(ns)
        return org, ns

    monkeypatch.setattr("server.code.app.common.resolve_code_identity", _resolve)
    inp = CodeSearchInput(
        org="acme", query="q", namespaces=["code-other"], lesson_namespaces=["mem-home"]
    )
    await result_lessons.lessons_for(inp, ["a.py"], time.monotonic())
    assert asked == ["code-other"] and calls[0]["trunk"] == "code-other"


def _search_out(**kw: Any) -> CodeSearchOutput:
    return CodeSearchOutput(results=[], route="hybrid", **kw)


def test_an_abstained_or_several_repository_search_reads_no_lessons() -> None:
    assert result_lessons.search_paths(_input(), _search_out(abstain=True)) == []
    several = _input(namespaces=["code-repo", "code-other"])
    assert result_lessons.search_paths(several, _search_out()) == []


@pytest.mark.asyncio
async def test_the_search_memo_never_stores_lessons(monkeypatch: pytest.MonkeyPatch) -> None:
    stored: list[dict[str, Any]] = []

    class _Repo:
        async def insert_search_memo_row(self, row: dict[str, Any]) -> None:
            stored.append(row)

    monkeypatch.setattr("server.code.store.index.CodeRepo", _Repo)
    out = _search_out(lessons=[{"memory_key": "private", "namespace": "mem-home"}])
    search_memo.put("acme", "code-repo", "k", out, indexed_commit="c1")
    for _ in range(100):  # the write is fire-and-forget
        if stored:
            break
        await asyncio.sleep(0.01)
    assert stored and "private" not in stored[0]["payload"]


def test_every_code_result_op_is_wrapped_with_its_files() -> None:
    from server.code.app.context import handle_code_context_pack
    from server.code.app.search.handler import handle_code_search
    from server.code.app.symbol.graph import handle_code_callees, handle_code_callers
    from server.code.app.symbol.impact import handle_code_impact
    from server.code.app.symbol.outline import handle_code_outline

    wanted = {
        "code.search": (handle_code_search, result_lessons.search_paths),
        "code.callers": (handle_code_callers, result_lessons.edge_paths),
        "code.callees": (handle_code_callees, result_lessons.edge_paths),
        "code.impact": (handle_code_impact, result_lessons.impact_paths),
        "code.outline": (handle_code_outline, result_lessons.outline_paths),
        "code.context_pack": (handle_code_context_pack, result_lessons.pack_paths),
    }
    for name, (inner, paths_of) in wanted.items():
        op = OPERATIONS_BY_NAME[name]
        assert op.handler.handler is inner, name
        assert op.handler.paths_of is paths_of, name
        assert {"include_lessons", "lesson_namespaces"} <= set(op.input_model.model_fields)
        assert "lessons" in op.output_model.model_fields


def test_each_answer_names_its_files_best_first() -> None:
    from types import SimpleNamespace as NS

    from server.code.api.models_context import CodeContextPackOutput
    from server.code.api.models_symbol import CodeImpactInput, CodeImpactOutput

    edges = NS(edges=[NS(path="caller.py"), NS(path="other.py")])
    assert result_lessons.edge_paths(None, edges) == ["caller.py", "other.py"]
    impact = CodeImpactOutput(
        impacted_symbols=[{"path": "user.py"}], impacted_tests=[], co_change_files=[]
    )
    impact._paths = ["seed.py"]
    inp = CodeImpactInput(org="acme", namespace="code-repo", files=["seed.py"])
    assert result_lessons.impact_paths(inp, impact) == ["seed.py", "user.py"]
    pack = CodeContextPackOutput(context="", tokens_used=0, source="task")
    assert result_lessons.pack_paths(None, pack) == []
    pack._paths = ["a.py", "b.py"]
    assert result_lessons.pack_paths(None, pack) == ["a.py", "b.py"]
