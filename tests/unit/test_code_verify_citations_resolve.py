"""code.verify_citations: citations are extracted by shape and
each resolves, or the draft says which did not — over a stubbed repo and a
stubbed git runner."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from server.code.domain.index import citations as cit
from server.settings import settings

BODY = """### Root cause

[`src/IO/WriteHelpers.cpp:122-138`](https://github.com/ClickHouse/ClickHouse/blob/b41b780fee/src/IO/WriteHelpers.cpp#L122-L138)
writes the name; `ASTFunction::formatImplWithoutAlias` (src/Parsers/ASTFunction.cpp:997) reads it.
See also src/Parsers/ExpressionListParsers.cpp, `IAST::createFromJSON`, and `std::string_view`.
Regressed in #120096 (PR 119000 fixed the other half); https://github.com/ClickHouse/ClickHouse/issues/777.
Dead pointer: src/Nope/Missing.cpp:12. Beyond the file: src/IO/WriteHelpers.cpp:99999.
Twice: src/Parsers/ASTFunction.cpp:997.

```sql
SELECT 1 -- #1 is not an issue, nor is src/IO/WriteHelpers.cpp:5 a claim
```
"""


# ------------------------------------------------------------------ extraction


def test_extract_every_shape_dedups_and_keeps_order() -> None:
    ex = cit.extract(BODY)
    by_text = {c.text: c for c in ex.citations}
    loc = by_text["src/IO/WriteHelpers.cpp:122-138"]
    assert loc.kind == "location" and (loc.start_line, loc.end_line) == (122, 138)
    assert loc.rev == "b41b780fee" and loc.occurrences == 2, "link text + blob URL are one citation"
    assert by_text["src/Parsers/ASTFunction.cpp:997"].occurrences == 2
    assert by_text["src/Parsers/ExpressionListParsers.cpp"].kind == "path"
    assert by_text["#120096"].number == 120096 and by_text["#119000"].number == 119000
    assert by_text["#777"].number == 777, "an item URL is a number citation"
    assert by_text["ASTFunction::formatImplWithoutAlias"].kind == "name"
    assert by_text["IAST::createFromJSON"].kind == "name"
    assert "#1" not in by_text, "numbers inside a fenced block are not citations"
    assert by_text["src/IO/WriteHelpers.cpp:5"].in_code_block is True
    assert ex.skipped == [{"kind": "name", "text": "std::string_view", "reason": "external_scope"}]
    texts = [c.text for c in ex.citations]
    assert texts[:3] == [
        "src/IO/WriteHelpers.cpp:122-138",
        "ASTFunction::formatImplWithoutAlias",
        "src/Parsers/ASTFunction.cpp:997",
    ], "first-appearance order in the text"
    assert texts.index("#120096") < texts.index("#777") < texts.index("src/Nope/Missing.cpp:12")
    assert not ex.truncated


def test_extract_edge_shapes() -> None:
    ex = cit.extract(
        "a/src/A.cpp:10:5 and ./src/B.h#L3-L4 and src/C.py:7..9 and DB::Foo::~Foo and "
        "x::operator[] and Fixes #12; issue 34 and Closes #56 and #L99 and #include <x>"
    )
    texts = {c.text for c in ex.citations}
    assert {
        "src/A.cpp:10",
        "src/B.h:3-4",
        "src/C.py:7-9",
        "DB::Foo::~Foo",
        "x::operator[]",
        "#12",
        "#34",
        "#56",
    } <= texts, "word forms keep small numbers"
    assert not any(t.startswith("#L") for t in texts)
    assert "#include" not in texts
    ex = cit.extract(
        "`session_timezone` #79 and row #2 vs #120096 and "
        "https://raw.githubusercontent.com/X/Y/26.8/tests/performance/tpch.xml here"
    )
    assert [c.text for c in ex.citations] == ["#120096"], (
        "bare ordinals and a stray URL are not citations"
    )
    assert {s["text"] for s in ex.skipped} == {"#79", "#2"}
    assert all(s["reason"] == "small_number" for s in ex.skipped)
    ex = cit.extract(
        "verified: raw.githubusercontent.com/ClickHouse/ClickHouse/26.8/tests/performance/tpch.xml "
        "and .github/workflows/ci.yml"
    )
    assert [c.text for c in ex.citations] == [".github/workflows/ci.yml"], (
        "a hostname is not a directory"
    )


def test_extract_limit_and_counts() -> None:
    ex = cit.extract(" ".join(f"#{i}" for i in range(100, 130)), limit=10)
    assert len(ex.citations) == 10 and ex.truncated
    for c in ex.citations:
        c.verdict = "resolved"
    ex.citations[0].verdict = "stale"
    assert cit.top_verdict(ex.citations) == "has_stale"
    ex.citations[1].verdict = "unresolved"
    assert cit.top_verdict(ex.citations) == "has_unresolved", "unresolved outranks stale"
    ex.citations[1].verdict = "ambiguous"
    assert cit.top_verdict(ex.citations) == "has_stale"
    counts = cit.counts_of(ex.citations)
    assert counts["total"] == 10 and counts["ambiguous"] == 1 and counts["kind_number"] == 10
    assert cit.top_verdict([]) == "no_citations"


# --------------------------------------------------------------------- handler


class _Repo:
    def __init__(
        self,
        *,
        files: dict[str, dict[str, Any]] | None = None,
        spans: list[dict[str, Any]] | None = None,
        chunks: dict[str, list[dict[str, Any]]] | None = None,
        items: dict[int, dict[str, Any]] | None = None,
        names: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.files = files or {}
        self.spans = spans or []
        self.chunks = chunks or {}
        self.items = items or {}
        self.names = names or {}
        self.calls: list[tuple[str, str, Any]] = []

    async def file_rows_for_paths(self, org: str, ns: str, paths: list[str]) -> dict[str, Any]:
        self.calls.append(("files", ns, list(paths)))
        return {p: self.files[p] for p in paths if p in self.files}

    async def functions_for_paths(
        self, org: str, ns: str, paths: list[str], **kw: Any
    ) -> list[dict[str, Any]]:
        self.calls.append(("functions", ns, list(paths)))
        return [r for r in self.spans if r["path"] in paths and r.get("kind") != "class"]

    async def types_for_paths(
        self, org: str, ns: str, paths: list[str], **kw: Any
    ) -> list[dict[str, Any]]:
        return [r for r in self.spans if r["path"] in paths and r.get("kind") == "class"]

    async def chunk_spans_for_paths(self, org: str, ns: str, paths: list[str]) -> dict[str, Any]:
        self.calls.append(("chunks", ns, list(paths)))
        return {p: self.chunks[p] for p in paths if p in self.chunks}

    async def items_by_number(self, org: str, ns: str, numbers: list[int]) -> dict[int, Any]:
        self.calls.append(("items", ns, list(numbers)))
        return {n: self.items[n] for n in numbers if n in self.items}

    async def symbols_by_name(
        self, org: str, ns: str, name: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        self.calls.append(("name", ns, name))
        return self.names.get(name, [])

    async def paths_by_suffix(self, org: str, ns: str, suffixes: list[str]) -> dict[str, list[str]]:
        self.calls.append(("suffix", ns, list(suffixes)))
        out: dict[str, list[str]] = {}
        for s in suffixes:
            hits = [p for p in self.files if p.endswith("/" + s)]
            if hits:
                out[s] = hits
        return out

    async def chunks_of_path(
        self, org: str, ns: str, path: str, *, limit: int = 400
    ) -> list[dict[str, Any]]:
        self.calls.append(("chunks_of_path", ns, path))
        if path == "src/Common/ErrorCodes.cpp":
            return [
                {
                    "start_line": 1,
                    "end_line": 3,
                    "content": "    M(36, BAD_ARGUMENTS) \\\n    M(48, NOT_IMPLEMENTED) \\\n",
                }
            ]
        return []


async def _as_of(*_a: Any, **_k: Any) -> dict[str, Any]:
    return {"commit": "head0000", "status": "fresh"}


def _run(repo: _Repo, text: str, **kw: Any) -> Any:
    from server.code.api.models_verify import CodeVerifyCitationsInput
    from server.code.app import citations as h

    async def go() -> Any:
        with (
            patch.object(h, "CodeRepo", return_value=repo),
            patch.object(h, "index_as_of", new=_as_of),
            patch.object(h, "schedule_code_trace", new=lambda **k: None),
            patch("server.code.domain.index.repos.repo_for_namespace", return_value=None),
            patch("server.code.domain.index.repos.configured_repos", return_value=[]),
        ):
            return await h.handle_code_verify_citations(
                CodeVerifyCitationsInput(org="o", namespace="n", text=text, **kw)
            )

    return asyncio.run(go())


WH = "src/IO/WriteHelpers.cpp"
AF = "src/Parsers/ASTFunction.cpp"


def _repo() -> _Repo:
    return _Repo(
        files={
            WH: {"lang": "cpp", "loc": 900, "indexed_commit": "abc"},
            AF: {"lang": "cpp", "loc": 1200, "indexed_commit": "abc"},
            "src/Parsers/ExpressionListParsers.cpp": {
                "lang": "cpp",
                "loc": 4000,
                "indexed_commit": "abc",
            },
        },
        spans=[
            {
                "path": WH,
                "qualified_name": "DB::writeProbablyBackQuotedString",
                "symbol_id": "s1",
                "kind": "function",
                "tier": "treesitter",
                "start_line": 120,
                "end_line": 140,
            },
            {
                "path": AF,
                "qualified_name": "DB::ASTFunction",
                "symbol_id": "s2",
                "kind": "class",
                "tier": "treesitter",
                "start_line": 20,
                "end_line": 1100,
            },
        ],
        chunks={WH: [{"kind": "file_header", "scope": "", "start_line": 1, "end_line": 40}]},
        items={
            120096: {
                "is_pr": True,
                "state": "closed",
                "state_reason": "",
                "title": "Fix any()",
                "merged_at": "2026-09-01 00:00:00",
            }
        },
        names={
            "ASTFunction::formatImplWithoutAlias": [
                {
                    "qualified_name": "DB::ASTFunction::formatImplWithoutAlias",
                    "path": AF,
                    "kind": "method",
                    "tier": "scip",
                    "start_line": 500,
                    "end_line": 900,
                }
            ],
            "IAST::createFromJSON": [
                {
                    "qualified_name": "DB::IAST::createFromJSON",
                    "path": "src/Parsers/IAST.cpp",
                    "kind": "method",
                    "tier": "scip",
                    "start_line": 1,
                    "end_line": 1,
                },
                {
                    "qualified_name": "DB::Test::IAST::createFromJSON",
                    "path": "src/x.cpp",
                    "kind": "method",
                    "tier": "treesitter",
                    "start_line": 1,
                    "end_line": 9,
                },
            ],
        },
    )


def test_handler_verdict_per_citation_and_top_level() -> None:
    repo = _repo()
    out = _run(repo, BODY)
    by = {c["text"]: c for c in out.citations}
    assert by[f"{WH}:122-138"]["verdict"] == "resolved"
    assert by[f"{WH}:122-138"]["target"]["symbol"] == "DB::writeProbablyBackQuotedString"
    assert by[f"{WH}:122-138"]["target"]["span_covers_range"] is True
    assert by[f"{WH}:122-138"]["epistemic"] == "lower_bound"
    assert by[f"{AF}:997"]["verdict"] == "resolved" and by[f"{AF}:997"]["target"]["kind"] == "class"
    assert (
        by[f"{WH}:5"]["verdict"] == "resolved" and by[f"{WH}:5"]["target"]["unit"] == "file_header"
    )
    assert (
        by[f"{WH}:99999"]["verdict"] == "stale" and by[f"{WH}:99999"]["reason"] == "line_beyond_eof"
    )
    assert by[f"{WH}:99999"]["epistemic"] == "exact"
    assert by["src/Nope/Missing.cpp:12"]["verdict"] == "unresolved"
    assert by["src/Nope/Missing.cpp:12"]["reason"] == "path_not_indexed"
    assert by["src/Parsers/ExpressionListParsers.cpp"]["verdict"] == "resolved"
    assert (
        by["#120096"]["verdict"] == "resolved" and by["#120096"]["target"]["title"] == "Fix any()"
    )
    assert by["#120096"]["target"]["is_pr"] is True and by["#120096"]["target"]["merged"] is True
    assert by["#119000"]["verdict"] == "unresolved" and by["#119000"]["reason"] == "number_unknown"
    assert by["#777"]["verdict"] == "unresolved"
    assert by["ASTFunction::formatImplWithoutAlias"]["verdict"] == "resolved"
    assert by["ASTFunction::formatImplWithoutAlias"]["target"]["path"] == AF
    assert by["IAST::createFromJSON"]["verdict"] == "ambiguous"
    assert by["IAST::createFromJSON"]["reason"] == "several_definitions"
    assert len(by["IAST::createFromJSON"]["candidates"]) == 2
    assert out.verdict == "has_unresolved"
    assert out.counts["total"] == len(out.citations) and out.counts["unresolved"] == 3
    assert out.counts["stale"] == 1 and out.counts["ambiguous"] == 1
    assert out.epistemic == {
        "path": "exact",
        "number": "exact",
        "name": "exact",
        "line": "lower_bound",
        "commit": "exact",
    }
    assert out.skipped[0]["text"] == "std::string_view"
    assert out.namespaces == ["n"] and out.at_commit == {}
    assert out.as_of["commit"] == "head0000" and out.trace_run_id
    # spans and chunks are read only for the paths cited with a line
    chunk_paths = next(c for c in repo.calls if c[0] == "chunks")[2]
    assert "src/Parsers/ExpressionListParsers.cpp" not in chunk_paths and WH in chunk_paths


def test_line_outside_every_unit_is_stale_and_no_citations_is_a_verdict() -> None:
    repo = _repo()
    out = _run(repo, f"look at {WH}:60")
    c = out.citations[0]
    assert c["verdict"] == "stale" and c["reason"] == "line_outside_units"
    assert c["epistemic"] == "lower_bound" and out.verdict == "has_stale"
    out = _run(repo, "nothing cited here")
    assert out.verdict == "no_citations" and out.citations == [] and out.counts["total"] == 0


def test_base_namespace_retries_only_the_unresolved() -> None:
    class _Two(_Repo):
        async def items_by_number(self, org: str, ns: str, numbers: list[int]) -> dict[int, Any]:
            self.calls.append(("items", ns, list(numbers)))
            if ns == "base":
                return {n: {"is_pr": False, "state": "open", "title": "issue"} for n in numbers}
            return {}

    repo = _Two(files={WH: {"lang": "cpp", "loc": 10, "indexed_commit": ""}})
    out = _run(repo, f"{WH} #101 #102 src/gone.cpp", base_namespace="base")
    by = {c["text"]: c for c in out.citations}
    assert by["#101"]["verdict"] == "resolved" and by["#101"]["target"]["namespace"] == "base"
    assert by[WH]["target"]["namespace"] == "n"
    assert by["src/gone.cpp"]["verdict"] == "unresolved"
    assert out.namespaces == ["n", "base"]
    base_reads = [c for c in repo.calls if c[1] == "base"]
    assert ("items", "base", [101, 102]) in base_reads
    assert ("files", "base", ["src/gone.cpp"]) in base_reads, "the resolved path is not re-read"


def test_base_namespaces_are_read_only_when_the_caller_may_read_them() -> None:
    """The transport gates `namespace` only: a named `base_namespace` the caller cannot
    read is refused before any read, and the master entry LOOM adds on its own is left
    out when unreadable."""
    import server.auth.authz as authz
    from server.code.app import citations as h
    from server.core.errors import AuthInsufficientError

    async def _only_n(org: str, ns: str, role: Any, *, hides_answer: bool = True) -> bool:
        return ns == "n"

    repo = _repo()
    with patch.object(authz, "row_in_scope", _only_n):
        with pytest.raises(AuthInsufficientError):
            _run(repo, "#101", base_namespace="base")
        assert not any(c[1] == "base" for c in repo.calls), "refused before any base read"
        with patch.object(h, "_namespaces", lambda org, ns, base: [ns, "master"]):
            assert asyncio.run(h._readable_namespaces("o", "n", "")) == ["n"]


def _run_git(repo: _Repo, text: str, *, resolve: Any, show: Any, diff: str = "", **kw: Any) -> Any:
    from server.code.api.models_verify import CodeVerifyCitationsInput
    from server.code.app import citations as h

    calls: list[tuple[str, ...]] = []

    async def fake_resolve(git_dir: str, rev: str) -> str:
        calls.append(("rev-parse", rev))
        return resolve(rev)

    async def fake_run(git_dir: str, *args: str, timeout_s: float = 0) -> str:
        calls.append(("run", *args[:3]))
        return diff

    async def fake_show(git_dir: str, sha: str, path: str, max_bytes: int = 0) -> bytes | None:
        calls.append(("show", path))
        return show(path)

    async def go() -> Any:
        with (
            patch.object(h, "CodeRepo", return_value=repo),
            patch.object(h, "index_as_of", new=_as_of),
            patch.object(h, "schedule_code_trace", new=lambda **k: None),
            patch("server.code.domain.index.repos.repo_for_namespace", return_value=None),
            patch("server.code.domain.index.repos.configured_repos", return_value=[]),
            patch.object(h.gitrepo, "resolve_commit", new=fake_resolve),
            patch.object(h.gitrepo, "run", new=fake_run),
            patch.object(h.gitrepo, "show_file", new=fake_show),
        ):
            out = await h.handle_code_verify_citations(
                CodeVerifyCitationsInput(org="o", namespace="n", text=text, **kw)
            )
        return out, calls

    return asyncio.run(go())


def test_at_commit_leg_marks_changed_absent_and_short_files() -> None:
    repo = _repo()
    text = f"{WH}:130 {AF}:997 src/Nope/Missing.cpp:12 src/Parsers/ExpressionListParsers.cpp"
    out, calls = _run_git(
        repo,
        text,
        at_commit="b41b780f",
        resolve=lambda rev: "b41b780f" + "0" * 32,
        show=lambda p: (
            b"x\n" * 200 if p == WH else (b"y\n" * 50 if p == "src/Nope/Missing.cpp" else None)
        ),
        diff=f"{WH}\nsrc/Nope/Missing.cpp\n",
    )
    by = {c["text"]: c for c in out.citations}
    assert (
        by[f"{WH}:130"]["verdict"] == "stale"
        and by[f"{WH}:130"]["reason"] == "file_changed_since_commit"
    )
    assert by[f"{WH}:130"]["target"]["at_commit"] == {
        "existed": True,
        "lines": 200,
        "changed_since": True,
    }
    assert (
        by[f"{AF}:997"]["verdict"] == "stale"
        and by[f"{AF}:997"]["reason"] == "path_absent_at_commit"
    )
    assert by["src/Nope/Missing.cpp:12"]["verdict"] == "stale"
    assert by["src/Nope/Missing.cpp:12"]["reason"] == "path_changed_or_removed_since_commit"
    assert by["src/Parsers/ExpressionListParsers.cpp"]["verdict"] == "stale", (
        "a bare path absent at the commit"
    )
    assert out.at_commit["known"] is True and out.at_commit["sha"].startswith("b41b780f")
    assert out.at_commit["calls"] == 1 + 1 + 4 and out.at_commit["capped"] is False
    assert calls[0] == ("rev-parse", "b41b780f") and calls[1][:2] == ("run", "diff")
    assert out.verdict == "has_stale"


def test_at_commit_line_beyond_eof_at_commit_and_unknown_commit() -> None:
    repo = _repo()
    out, _ = _run_git(
        repo,
        f"{WH}:130",
        at_commit="deadbeef",
        resolve=lambda rev: "d" * 40,
        show=lambda p: b"x\n" * 100,
    )
    c = out.citations[0]
    assert c["verdict"] == "stale" and c["reason"] == "line_beyond_eof_at_commit"
    assert c["epistemic"] == "lower_bound", (
        "the head check's epistemic is kept; the commit fact is exact"
    )

    def unknown(rev: str) -> str:
        raise ValueError("not a commit")

    out, calls = _run_git(repo, f"{WH}:130", at_commit="nope", resolve=unknown, show=lambda p: None)
    assert out.at_commit["known"] is False and out.at_commit["error"] == "unknown_commit"
    assert out.citations[0]["verdict"] == "resolved", (
        "head checks stand when the mirror does not know the commit"
    )
    assert "not a commit the mirror knows" in out.note
    assert calls == [("rev-parse", "nope")]


def test_at_commit_leg_is_capped_at_twenty_calls() -> None:

    files = {f"src/f{i}.cpp": {"lang": "cpp", "loc": 100, "indexed_commit": ""} for i in range(30)}
    repo = _Repo(files=files)
    text = " ".join(f"src/f{i}.cpp:1" for i in range(30))
    out, calls = _run_git(
        repo, text, at_commit="abc", resolve=lambda rev: "a" * 40, show=lambda p: b"x\n" * 100
    )
    shows = [c for c in calls if c[0] == "show"]
    assert (
        len(shows) == settings.code_citations_git_call_cap - 2 and out.at_commit["capped"] is True
    )
    unchecked = [c for c in out.citations if c["target"].get("at_commit") == {"checked": False}]
    assert len(unchecked) == 30 - (settings.code_citations_git_call_cap - 2)
    assert "capped" in out.note


def test_registered_as_an_experimental_viewer_op_with_sdk_reach() -> None:
    from server.operations.base import Role
    from server.operations.reach_manifest import STATIC_REACH
    from server.operations.registry import ALL_OPERATIONS
    from server.transport.mcp_descriptions import TOOL_DESCRIPTIONS

    op = next(o for o in ALL_OPERATIONS if o.name == "code.verify_citations")
    assert op.required_role == Role.VIEWER and op.idempotent and op.maturity == "experimental"
    assert "sdk_method" in STATIC_REACH["code.verify_citations"]
    assert "at_commit" in TOOL_DESCRIPTIONS["code.verify_citations"]


def test_input_bounds() -> None:
    from pydantic import ValidationError

    from server.code.api.models_verify import CodeVerifyCitationsInput

    with pytest.raises(ValidationError):
        CodeVerifyCitationsInput(text="")
    with pytest.raises(ValidationError):
        CodeVerifyCitationsInput(text="x", limit=0)
    assert CodeVerifyCitationsInput(text="x").limit == 200


def test_partial_paths_resolve_by_suffix_or_are_ambiguous() -> None:
    repo = _repo()
    repo.files["src/Common/Foo.h"] = {"lang": "cpp", "loc": 10, "indexed_commit": ""}
    repo.files["base/Common/Foo.h"] = {"lang": "cpp", "loc": 10, "indexed_commit": ""}
    out = _run(repo, "see Parsers/ASTFunction.cpp:997 and IO/WriteHelpers.cpp and Common/Foo.h")
    by = {c["text"]: c for c in out.citations}
    c = by["Parsers/ASTFunction.cpp:997"]
    assert c["verdict"] == "resolved" and c["reason"] == "partial_path"
    assert (
        c["path"] == AF and c["target"]["matched_by"] == "suffix" and c["target"]["kind"] == "class"
    )
    assert (
        by["IO/WriteHelpers.cpp"]["verdict"] == "resolved"
        and by["IO/WriteHelpers.cpp"]["path"] == WH
    )
    foo = by["Common/Foo.h"]
    assert foo["verdict"] == "ambiguous" and foo["reason"] == "several_files"
    assert {x["path"] for x in foo["candidates"]} == {"src/Common/Foo.h", "base/Common/Foo.h"}
    assert out.verdict == "all_resolved" and out.counts["ambiguous"] == 1
    suffix_call = next(c for c in repo.calls if c[0] == "suffix")
    assert set(suffix_call[2]) == {"Parsers/ASTFunction.cpp", "IO/WriteHelpers.cpp", "Common/Foo.h"}


def test_qualifier_mismatch_and_error_codes() -> None:
    repo = _repo()
    repo.names["runCommandSegments"] = [
        {
            "qualified_name": "DB::runCommandSegments",
            "path": "src/Interpreters/InterpreterAlterQuery.cpp",
            "kind": "function",
            "tier": "treesitter",
            "start_line": 340,
            "end_line": 380,
        }
    ]
    repo.names["Aggregator::add"] = [
        {
            "qualified_name": "DB::A::Aggregator::add",
            "path": "src/a.h",
            "kind": "method",
            "tier": "scip",
            "start_line": 1,
            "end_line": 1,
        },
        {
            "qualified_name": "DB::B::Aggregator::add",
            "path": "src/b.h",
            "kind": "method",
            "tier": "scip",
            "start_line": 1,
            "end_line": 1,
        },
    ]
    out = _run(
        repo,
        "InterpreterAlterQuery::runCommandSegments, Foo::Aggregator::add, "
        "ErrorCodes::BAD_ARGUMENTS, DB::ErrorCodes::NOT_IMPLEMENTED, ErrorCodes::NOPE, "
        "Nowhere::atAll",
    )
    by = {c["text"]: c for c in out.citations}
    c = by["InterpreterAlterQuery::runCommandSegments"]
    assert c["verdict"] == "resolved" and c["reason"] == "qualifier_mismatch"
    assert c["target"]["matched_as"] == "runCommandSegments"
    assert c["target"]["qualified_name"] == "DB::runCommandSegments"
    a = by["Foo::Aggregator::add"]
    assert a["verdict"] == "ambiguous" and a["reason"] == "qualifier_mismatch"
    assert a["target"]["matched_as"] == "Aggregator::add" and len(a["candidates"]) == 2
    assert by["ErrorCodes::BAD_ARGUMENTS"]["verdict"] == "resolved"
    assert by["ErrorCodes::BAD_ARGUMENTS"]["target"]["kind"] == "error_code"
    assert by["DB::ErrorCodes::NOT_IMPLEMENTED"]["verdict"] == "resolved"
    assert by["ErrorCodes::NOPE"]["verdict"] == "unresolved"
    assert (
        by["Nowhere::atAll"]["verdict"] == "unresolved"
        and by["Nowhere::atAll"]["reason"] == "name_unknown"
    )
    lookups = [c[2] for c in repo.calls if c[0] == "name"]
    assert "ErrorCodes::BAD_ARGUMENTS" not in lookups, (
        "the ErrorCodes table answers, not the symbol table"
    )
    assert lookups.count("Nowhere::atAll") == 1 and "atAll" in lookups, "one shorter suffix tried"
    assert [c for c in repo.calls if c[0] == "chunks_of_path"] == [
        ("chunks_of_path", "n", "src/Common/ErrorCodes.cpp")
    ]


def test_stale_line_between_units_names_the_nearest_unit() -> None:
    repo = _repo()
    repo.chunks[WH] = [
        {"kind": "other", "scope": "", "start_line": 50, "end_line": 59},
        {"kind": "other", "scope": "", "start_line": 61, "end_line": 70},
    ]
    out = _run(repo, f"{WH}:60")
    c = out.citations[0]
    assert c["verdict"] == "stale" and c["reason"] == "line_outside_units"
    assert c["target"]["gap_lines"] == 1 and c["target"]["nearest_unit"]["start_line"] in (50, 61)


def test_at_commit_unindexed_but_untouched_path_stays_path_not_indexed() -> None:
    repo = _repo()
    out, _ = _run_git(
        repo,
        "tests/queries/0_stateless/x.reference:3",
        at_commit="abc",
        resolve=lambda rev: "a" * 40,
        show=lambda p: b"x\n" * 8,
        diff="",
    )
    c = out.citations[0]
    assert c["verdict"] == "unresolved" and c["reason"] == "path_not_indexed"
    assert c["target"]["at_commit"] == {"existed": True, "lines": 8, "changed_since": False}
