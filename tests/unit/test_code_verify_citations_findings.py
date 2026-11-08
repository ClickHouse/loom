"""code.verify_citations: the shapes review findings use —
bare `Filename.ext:NNN`, backticked function names — resolve, a SQL-level
name falls through to the factory-registered class and the quoted
registration string, and `pr_number` says per citation whether it sits in
the PR's hunks. Over the base citation tests' stubs plus a stubbed git runner."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from server.code.domain.index import citations as cit
from tests.unit.test_code_verify_citations_resolve import _as_of, _Repo

SSH = "src/Server/SSH/SSHPtyHandler.cpp"
NPY = "src/Processors/Formats/Impl/NpyRowInputFormat.cpp"
SUMMAP = "src/AggregateFunctions/AggregateFunctionSumMap.cpp"
MERGE = "05c3edcab4349bcf1ff155e32ac74dc321edea0c"

FINDING = """getClientExitCode() uses logical AND (&&) returning bool
Why: SSHPtyHandler.cpp:104 → getClientExitCode() → `SSHPtyHandler.cpp:566` sendExitStatus(1);
`NpyRowInputFormat::readRow` (line 495) and `readRow()` guard eof; `sumMap` and `dictGetOrNull`
are SQL functions; `max_execution_time`, `return`, `t.c`, x.a:3, `isEnum(to_type)` are not
citations; `SetUtils.cpp` is.
"""


# ------------------------------------------------------------------ extraction


def test_extract_basename_locations_and_bare_names() -> None:
    ex = cit.extract(FINDING)
    by = {c.text: c for c in ex.citations}
    loc = by["SSHPtyHandler.cpp:104"]
    assert loc.kind == "location" and loc.form == "basename" and loc.start_line == 104
    assert by["SSHPtyHandler.cpp:566"].form == "basename", "backticked basename:line too"
    assert by["SetUtils.cpp"].kind == "path" and by["SetUtils.cpp"].form == "basename"
    assert by["getClientExitCode"].kind == "name" and by["getClientExitCode"].form == "bare_name"
    assert by["getClientExitCode"].occurrences == 2, "call form twice, one citation"
    assert by["sumMap"].form == "bare_name" and by["dictGetOrNull"].form == "bare_name"
    assert by["readRow"].form == "bare_name", "`readRow()` is a call"
    assert by["NpyRowInputFormat::readRow"].form == "", "the qualified pass keeps its citation"
    for junk in (
        "max_execution_time",
        "return",
        "t.c",
        "x.a:3",
        "isEnum",
        "to_type",
        "sendExitStatus",
    ):
        assert junk not in by, junk
    texts = [c.text for c in ex.citations]
    assert texts.index("getClientExitCode") < texts.index("SSHPtyHandler.cpp:104")


def test_bare_name_never_rereads_a_qualified_tail_or_prose() -> None:
    ex = cit.extract(
        "`ASTStreamSettings::readJSON` calls `readJSON`; the iPhone word, `eof()`, `f()`, "
        "`toString(x)`, `CamelCase`, size() and check_limits() and `Poco::Foo`"
    )
    by = {c.text: c for c in ex.citations}
    assert by["ASTStreamSettings::readJSON"].form == ""
    assert by["readJSON"].form == "bare_name", "a backticked bare use is its own citation"
    assert "iPhone" not in by, "camelCase prose outside backticks is not a citation"
    assert "eof" not in by and "f" not in by, "all-lower calls need three characters"
    assert "toString" not in by, "a call with arguments is not a bare-name citation"
    assert "CamelCase" not in by, "a PascalCase word is a type, not this grammar"
    assert by["size"].form == "bare_name", "an all-lower word with empty parens is a call"
    assert by["check_limits"].form == "bare_name", "snake_case with parens is a call"
    assert ex.skipped == [{"kind": "name", "text": "Poco::Foo", "reason": "external_scope"}]


def test_extract_line_refs() -> None:
    ex = cit.extract(
        "`NpyRowInputFormat::readRow` (line 495) and lines 178-179 reject it; Line 12 too; "
        "line 3.5 no; #line 7 no; online 9 no; `src/A.cpp:10` at line 11"
    )
    refs = [c for c in ex.citations if c.form == "line_ref"]
    assert [(c.text, c.start_line, c.end_line) for c in refs] == [
        ("line 495", 495, 495),
        ("lines 178-179", 178, 179),
        ("line 12", 12, 12),
        ("line 11", 11, 11),
    ]
    assert all(c.kind == "location" and not c.path for c in refs)


def test_bind_line_refs_to_the_preceding_resolved_citation() -> None:
    ex = cit.extract(
        "line 2 first; `Cls::readRow` (line 495) then lines 10-12; Other.cpp:7 and line 8"
    )
    by = {c.text: c for c in ex.citations}
    by["Cls::readRow"].verdict, by["Cls::readRow"].target = "resolved", {"path": "src/N.cpp"}
    by["Other.cpp:7"].verdict, by["Other.cpp:7"].path = "resolved", "src/Other.cpp"
    bound = cit.bind_line_refs(ex.citations, ex.skipped)
    assert [(c.text, c.path) for c in bound] == [
        ("src/N.cpp:495", "src/N.cpp"),
        ("src/N.cpp:10-12", "src/N.cpp"),
        ("src/Other.cpp:8", "src/Other.cpp"),
    ]
    assert ex.skipped == [{"kind": "location", "text": "line 2", "reason": "no_anchor"}]
    assert "line 2" not in {c.text for c in ex.citations}, "an unanchored reference is dropped"


def test_bare_file_extensions_are_narrow() -> None:
    ex = cit.extract(
        "`t.c` and `ab.h` and `Field.h` and `config.xml` and data.bin:3 and Foo.cpp:12-14"
    )
    texts = {c.text for c in ex.citations}
    assert "t.c" not in texts and "ab.h" not in texts, "backticked basenames need a 3-char stem"
    assert "Field.h" in texts and "config.xml" in texts
    assert "data.bin:3" not in texts, "binary extensions are not bare-file citations"
    assert "Foo.cpp:12-14" in texts


def test_parse_hunks_and_in_ranges() -> None:
    diff = (
        "diff --git a/src/A.cpp b/src/A.cpp\n--- a/src/A.cpp\n+++ b/src/A.cpp\n"
        "@@ -10,3 +10,4 @@ ctx\n+x\n@@ -50,2 +51,0 @@\n-y\n@@ -70 +71 @@\n-z\n+w\n"
        "diff --git a/src/B.cpp b/src/B.cpp\n--- a/src/B.cpp\n+++ /dev/null\n@@ -1,5 +0,0 @@\n"
    )
    h = cit.parse_hunks(diff)
    assert h["src/A.cpp"] == [(10, 13), (51, 52), (71, 71)]
    assert h["src/B.cpp"] == [cit.WHOLE_FILE], "a deleted file is wholly in the diff"
    assert cit.in_ranges(h["src/A.cpp"], 12, 12) and cit.in_ranges(h["src/A.cpp"], 8, 10)
    assert not cit.in_ranges(h["src/A.cpp"], 14, 50)
    assert cit.in_ranges(h["src/A.cpp"], 52, 52), "a deletion touches the lines it sits between"
    assert cit.in_ranges(h["src/B.cpp"], 400, 400)
    assert cit.parse_hunks("") == {}


# --------------------------------------------------------------------- handler


def _repo() -> _Repo:
    return _Repo(
        files={
            SSH: {"lang": "cpp", "loc": 700, "indexed_commit": "abc"},
            NPY: {"lang": "cpp", "loc": 600, "indexed_commit": "abc"},
            SUMMAP: {"lang": "cpp", "loc": 300, "indexed_commit": "abc"},
            "src/Interpreters/SetUtils.cpp": {"lang": "cpp", "loc": 100, "indexed_commit": "abc"},
            "tests/other/SetUtils.cpp": {"lang": "cpp", "loc": 10, "indexed_commit": "abc"},
        },
        spans=[
            {
                "path": SSH,
                "qualified_name": "DB::SSHPtyHandler::getClientExitCode",
                "symbol_id": "s1",
                "kind": "function",
                "tier": "treesitter",
                "start_line": 100,
                "end_line": 110,
            },
            {
                "path": SSH,
                "qualified_name": "DB::SSHPtyHandler::run",
                "symbol_id": "s2",
                "kind": "function",
                "tier": "treesitter",
                "start_line": 500,
                "end_line": 600,
            },
            {
                "path": NPY,
                "qualified_name": "DB::NpyRowInputFormat::readRow",
                "symbol_id": "s3",
                "kind": "function",
                "tier": "treesitter",
                "start_line": 470,
                "end_line": 510,
            },
        ],
        names={
            "getClientExitCode": [
                {
                    "qualified_name": "DB::SSHPtyHandler::getClientExitCode",
                    "path": SSH,
                    "kind": "function",
                    "tier": "treesitter",
                    "start_line": 100,
                    "end_line": 110,
                }
            ],
            "NpyRowInputFormat::readRow": [
                {
                    "qualified_name": "DB::NpyRowInputFormat::readRow",
                    "path": NPY,
                    "kind": "function",
                    "tier": "treesitter",
                    "start_line": 470,
                    "end_line": 510,
                }
            ],
            "readRow": [
                {
                    "qualified_name": "DB::NpyRowInputFormat::readRow",
                    "path": NPY,
                    "kind": "function",
                    "tier": "treesitter",
                    "start_line": 470,
                    "end_line": 510,
                },
                {
                    "qualified_name": "DB::CSVRowInputFormat::readRow",
                    "path": "src/x.cpp",
                    "kind": "function",
                    "tier": "treesitter",
                    "start_line": 1,
                    "end_line": 9,
                },
            ],
            "AggregateFunctionSumMap": [
                {
                    "qualified_name": "DB::AggregateFunctionSumMap",
                    "path": SUMMAP,
                    "kind": "class",
                    "tier": "treesitter",
                    "start_line": 40,
                    "end_line": 200,
                }
            ],
        },
    )


class _Git:
    """A stubbed mirror: the ledger-less PR is found in the first-parent log,
    the merge commit's file list and -U0 hunks are canned, grep answers the
    registration string."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.grep_hits: dict[str, list[str]] = {
            '"dictGetOrNull"': [
                "src/Functions/FunctionsExternalDictionaries.h",
                "src/Functions/tests/gtest_functions_stress.cpp",
            ],
        }

    async def resolve(self, git_dir: str, rev: str) -> str:
        self.calls.append(("rev-parse", rev))
        return rev

    async def run(self, git_dir: str, *args: str, timeout_s: float = 0) -> str:
        self.calls.append(("run", *args[:2]))
        if args[0] == "log":
            return f"{MERGE} Merge pull request #74989 from ClickHouse/fix-ssh\nffff Backport of #74989 (#80000)\n"
        if args[0] == "diff" and "--name-only" in args and args[-1] == MERGE:
            return f"{SSH}\nsrc/Interpreters/SetUtils.cpp\n"
        if args[0] == "diff" and "-U0" in args:
            return (
                f"diff --git a/{SSH} b/{SSH}\n--- a/{SSH}\n+++ b/{SSH}\n@@ -100,3 +100,6 @@\n+a\n"
                "@@ -560,2 +563,0 @@\n-b\n"
            )
        if args[0] == "diff" and "--name-only" in args:  # the at_commit leg: nothing changed since
            return ""
        return ""

    async def show(self, git_dir: str, sha: str, path: str, max_bytes: int = 0) -> bytes | None:
        self.calls.append(("show", path))
        return b"x\n" * 700

    async def grep(
        self, git_dir: str, rev: str, literal: str, pathspecs: tuple[str, ...], **kw: Any
    ) -> list[str]:
        self.calls.append(("grep", literal))
        return self.grep_hits.get(literal, [])


def _run(
    repo: _Repo,
    text: str,
    *,
    git: _Git | None = None,
    cfg: Any = None,
    merge_facts: dict[str, Any] | None = None,
    **kw: Any,
) -> Any:
    from server.code.api.models_verify import CodeVerifyCitationsInput
    from server.code.app import citations as h

    git = git or _Git()

    async def facts(org: str, ns: str, **k: Any) -> dict[str, Any] | None:
        return merge_facts

    repo.pr_merge_facts = facts  # type: ignore[attr-defined]

    async def go() -> Any:
        with (
            patch.object(h, "CodeRepo", return_value=repo),
            patch.object(h, "index_as_of", new=_as_of),
            patch.object(h, "schedule_code_trace", new=lambda **k: None),
            patch("server.code.domain.index.repos.repo_for_namespace", return_value=cfg),
            patch("server.code.domain.index.repos.configured_repos", return_value=[]),
            patch.object(h.settings, "code_git_dir", "/nonexistent/mirror.git"),
            patch.object(h.gitrepo, "resolve_commit", new=git.resolve),
            patch.object(h.gitrepo, "run", new=git.run),
            patch.object(h.gitrepo, "show_file", new=git.show),
            patch.object(h.gitrepo, "grep_paths", new=git.grep),
        ):
            return await h.handle_code_verify_citations(
                CodeVerifyCitationsInput(org="o", namespace="n", text=text, **kw)
            )

    return asyncio.run(go())


def test_basename_resolves_through_the_file_table() -> None:
    repo = _repo()
    out = _run(repo, "SSHPtyHandler.cpp:104 and `SetUtils.cpp` and Nope.cpp:3 and `Field.h`")
    by = {c["text"]: c for c in out.citations}
    ssh = by["SSHPtyHandler.cpp:104"]
    assert ssh["verdict"] == "resolved" and ssh["reason"] == "partial_path"
    assert ssh["path"] == SSH and ssh["target"]["matched_by"] == "basename"
    assert ssh["target"]["symbol"] == "DB::SSHPtyHandler::getClientExitCode"
    assert ssh["form"] == "basename"
    assert (
        by["SetUtils.cpp"]["verdict"] == "ambiguous"
        and by["SetUtils.cpp"]["reason"] == "several_files"
    )
    assert len(by["SetUtils.cpp"]["candidates"]) == 2
    assert by["Nope.cpp:3"]["verdict"] == "unresolved" and by["Field.h"]["verdict"] == "unresolved"
    assert (
        "suffix",
        "n",
        ["SSHPtyHandler.cpp", "SetUtils.cpp", "Nope.cpp", "Field.h"],
    ) in repo.calls
    assert not any(c[0] == "grep" for c in []), "no bare names: no mirror grep"
    assert out.pr_diff == {} and "in_pr_diff" not in out.epistemic, (
        "payload as before without pr_number"
    )
    assert all("in_pr_diff" not in c for c in out.citations)


def test_bare_names_resolve_by_symbol_then_registration_class_then_string() -> None:
    repo, git = _repo(), _Git()
    out = _run(
        repo, "`getClientExitCode`, `readRow()`, `sumMap`, `dictGetOrNull`, `noSuchThing`", git=git
    )
    by = {c["text"]: c for c in out.citations}
    assert (
        by["getClientExitCode"]["verdict"] == "resolved" and by["getClientExitCode"]["reason"] == ""
    )
    assert by["getClientExitCode"]["target"]["path"] == SSH
    assert (
        by["readRow"]["verdict"] == "ambiguous" and by["readRow"]["reason"] == "several_definitions"
    )
    assert by["sumMap"]["verdict"] == "resolved" and by["sumMap"]["reason"] == "registration_class"
    assert by["sumMap"]["target"]["qualified_name"] == "DB::AggregateFunctionSumMap"
    assert by["sumMap"]["target"]["matched_as"] == "AggregateFunctionSumMap"
    tried = [c[2] for c in repo.calls if c[0] == "name"]
    assert tried.index("FunctionSumMap") < tried.index("AggregateFunctionSumMap"), (
        "classes in order"
    )
    assert "TableFunctionSumMap" not in tried, "stops at the first class that binds"
    assert by["dictGetOrNull"]["verdict"] == "resolved"
    assert by["dictGetOrNull"]["reason"] == "registration_string"
    assert by["dictGetOrNull"]["target"] == {
        "path": "src/Functions/FunctionsExternalDictionaries.h",
        "matched_by": "registration_string",
        "namespace": "n",
    }
    assert (
        by["noSuchThing"]["verdict"] == "unresolved"
        and by["noSuchThing"]["reason"] == "name_unknown"
    )
    greps = [c for c in git.calls if c[0] == "grep"]
    assert sorted(greps) == [("grep", '"dictGetOrNull"'), ("grep", '"noSuchThing"')], (
        "only the names every table missed reach the mirror"
    )
    assert out.verdict == "has_unresolved" and out.counts["kind_name"] == 5


def test_shorthands_fold_into_the_full_citation() -> None:
    repo, git = _repo(), _Git()
    text = (
        f"[SSHPtyHandler.cpp:104]({SSH}:104) again SSHPtyHandler.cpp:104; `SetUtils.cpp`; "
        "`NpyRowInputFormat::readRow` then `readRow()` and readRow(); `sumMap`"
    )
    out = _run(repo, text, git=git)
    by = {c["text"]: c for c in out.citations}
    assert f"{SSH}:104" in by and "SSHPtyHandler.cpp:104" not in by
    assert by[f"{SSH}:104"]["occurrences"] == 3 and "form" not in by[f"{SSH}:104"]
    assert "readRow" not in by and by["NpyRowInputFormat::readRow"]["occurrences"] == 3
    assert by["SetUtils.cpp"]["verdict"] == "ambiguous", "an ambiguous basename keeps its row"
    assert by["sumMap"]["form"] == "bare_name", "a bare name with no qualified host stays"
    assert [c["text"] for c in out.citations] == [
        f"{SSH}:104",
        "SetUtils.cpp",
        "NpyRowInputFormat::readRow",
        "sumMap",
    ]
    assert out.counts["total"] == 4


def test_combinator_forms_strip_to_the_registered_stem() -> None:
    from server.code.app.citations import _combinator_stems

    assert _combinator_stems("countIfOrNull") == ["countIf", "count"]
    assert _combinator_stems("uniqExactIf") == ["uniqExact"]
    assert _combinator_stems("anyLast_respect_nulls") == ["anyLast"]
    assert _combinator_stems("sumMap") == ["sum"], "Map is a combinator too; the stem is tried last"
    assert _combinator_stems("If") == [] and _combinator_stems("readRow") == []
    repo, git = _repo(), _Git()
    repo.names["AggregateFunctionCount"] = [
        {
            "qualified_name": "DB::AggregateFunctionCount",
            "path": "src/AggregateFunctions/AggregateFunctionCount.cpp",
            "kind": "class",
            "tier": "treesitter",
            "start_line": 1,
            "end_line": 90,
        }
    ]
    git.grep_hits['"topKWeighted"'] = ["src/AggregateFunctions/AggregateFunctionTopK.cpp"]
    out = _run(repo, "`countIfOrNull` and `topKWeightedIf` and `sumMap`", git=git)
    by = {c["text"]: c for c in out.citations}
    assert by["countIfOrNull"]["verdict"] == "resolved"
    assert by["countIfOrNull"]["reason"] == "registration_combinator"
    assert by["countIfOrNull"]["target"]["matched_as"] == "AggregateFunctionCount"
    tried = [c[2] for c in repo.calls if c[0] == "name"]
    assert "AggregateFunctionCountIf" in tried and tried.index(
        "AggregateFunctionCountIf"
    ) < tried.index("AggregateFunctionCount"), "the class of the name, then of each stem"
    assert by["topKWeightedIf"]["verdict"] == "resolved"
    assert by["topKWeightedIf"]["reason"] == "registration_string"
    assert by["topKWeightedIf"]["target"]["matched_as"] == "topKWeighted"
    greps = [c[1] for c in git.calls if c[0] == "grep"]
    assert greps.index('"topKWeightedIf"') < greps.index('"topKWeighted"'), (
        "the name, then its stem"
    )
    assert '"topK"' not in greps, "topKWeighted is a registered function in its own right"
    assert by["sumMap"]["reason"] == "registration_class", (
        "the class of the name wins before any stem"
    )


def test_registration_paths_prefer_the_file_named_after_the_function() -> None:
    from server.code.app.citations import _registration_paths

    hits = [
        "src/DataTypes/DataTypeCustomSimpleAggregateFunction.cpp",
        SUMMAP,
        "src/AggregateFunctions/tests/gtest_x.cpp",
    ]
    assert _registration_paths("sumMap", hits) == [SUMMAP]
    assert _registration_paths("other", hits) == hits[:2], "no named file: every live hit stays"
    assert _registration_paths("x", ["src/Functions/tests/a.cpp"]) == []


def test_pr_number_gives_in_pr_diff_from_the_ledger_merge_commit() -> None:
    repo, git = _repo(), _Git()
    text = (
        f"SSHPtyHandler.cpp:104, {SSH}:300, `SSHPtyHandler.cpp:564`, `SetUtils.cpp`, "
        f"NpyRowInputFormat.cpp:495, `getClientExitCode`, `readRow()`, `NpyRowInputFormat::readRow`, #120096"
    )
    out = _run(
        repo, text, git=git, pr_number=74989, merge_facts={"merge_sha": MERGE.encode("ascii")}
    )
    by = {c["text"]: c for c in out.citations}
    assert out.pr_diff["merge_sha"] == MERGE and out.pr_diff["source"] == "ledger"
    assert out.at_commit["requested"] == MERGE, "the merge commit is the default at_commit"
    assert by["SSHPtyHandler.cpp:104"]["in_pr_diff"] == "true", "inside +100,6"
    assert by[f"{SSH}:300"]["in_pr_diff"] == "false", "the file is in the PR, the line is not"
    assert by["SSHPtyHandler.cpp:564"]["in_pr_diff"] == "true", "a deletion between 563 and 564"
    assert by["SetUtils.cpp"]["in_pr_diff"] == "unknown", "ambiguous path: no path to check"
    assert by["NpyRowInputFormat.cpp:495"]["in_pr_diff"] == "false", "file untouched by the PR"
    assert by["getClientExitCode"]["in_pr_diff"] == "true", "the definition span meets a hunk"
    assert "readRow" not in by, "the bare `readRow()` folds into the qualified citation beside it"
    assert by["NpyRowInputFormat::readRow"]["occurrences"] == 2
    assert by["NpyRowInputFormat::readRow"]["in_pr_diff"] == "false"
    assert "in_pr_diff" not in by["#120096"], "numbers have no place in a diff"
    assert out.pr_diff["files_in_diff"] == 2
    assert (out.pr_diff["in_diff"], out.pr_diff["off_diff"], out.pr_diff["unknown"]) == (3, 3, 1)
    assert out.epistemic["in_pr_diff"] == "exact"
    assert "in_pr_diff compares cited lines" in out.note
    runs = [c for c in git.calls if c[0] == "run"]
    assert ("run", "log", "head0000") not in runs, "the ledger answered: no first-parent log"
    u0 = [c for c in git.calls if c[0] == "run" and c[1] == "diff"]
    assert len(u0) >= 2, "file list, then the -U0 hunks of the cited files"


def test_line_ref_is_read_judged_and_checked_against_the_hunks() -> None:
    repo, git = _repo(), _Git()
    out = _run(
        repo,
        "line 9 of the output; `NpyRowInputFormat::readRow` (line 495) is untested; "
        "`getClientExitCode` at line 102 and line 300",
        git=git,
        pr_number=74989,
        merge_facts={"merge_sha": MERGE},
    )
    by = {c["text"]: c for c in out.citations}
    ref = by[f"{NPY}:495"]
    assert ref["form"] == "line_ref" and ref["verdict"] == "resolved"
    assert ref["target"]["symbol"] == "DB::NpyRowInputFormat::readRow"
    assert ref["target"]["matched_by"] == "line_ref" and ref["in_pr_diff"] == "false"
    assert by[f"{SSH}:102"]["in_pr_diff"] == "true" and by[f"{SSH}:300"]["in_pr_diff"] == "false"
    assert {"kind": "location", "text": "line 9", "reason": "no_anchor"} in out.skipped
    assert ("functions", "n", [NPY, SSH]) in repo.calls or (
        "functions",
        "n",
        [SSH, NPY],
    ) in repo.calls, "one more span read, over just the anchored paths"
    assert out.counts["kind_location"] == 3 and out.counts["kind_name"] == 2


def test_name_in_a_file_changed_since_the_merge_answers_unknown_off_hunk() -> None:
    class _Changed(_Git):
        async def run(self, git_dir: str, *args: str, timeout_s: float = 0) -> str:
            if args[0] == "diff" and "--name-only" in args and args[-1] != MERGE and "--" in args:
                self.calls.append(("run", *args[:2]))
                return f"{NPY}\n"  # the at_commit leg: NPY changed since the merge
            return await _Git.run(self, git_dir, *args)

    git = _Changed()
    out = _run(
        _repo(),
        "`NpyRowInputFormat::readRow` and `getClientExitCode`",
        git=git,
        pr_number=74989,
        merge_facts={"merge_sha": MERGE},
    )
    by = {c["text"]: c for c in out.citations}
    assert by["NpyRowInputFormat::readRow"]["in_pr_diff"] == "false", "not a file the PR touched"
    assert by["NpyRowInputFormat::readRow"]["target"]["at_commit"] == {"changed_since": True}
    assert by["getClientExitCode"]["in_pr_diff"] == "true"
    diff_calls = [c for c in git.calls if c[0] == "run" and c[1] == "diff"]
    assert len(diff_calls) == 3, "since-merge diff over the names' files, then the PR's two"


def test_pr_number_falls_back_to_the_first_parent_log_and_unknown_pr_is_disclosed() -> None:
    repo, git = _repo(), _Git()
    out = _run(repo, "SSHPtyHandler.cpp:104", git=git, pr_number=74989, merge_facts=None)
    assert out.pr_diff["merge_sha"] == MERGE and out.pr_diff["source"] == "mirror_log"
    assert ("run", "log", "head0000") in git.calls
    assert out.citations[0]["in_pr_diff"] == "true"

    class _NoLog(_Git):
        async def run(self, git_dir: str, *args: str, timeout_s: float = 0) -> str:
            self.calls.append(("run", *args[:2]))
            return "" if args[0] == "log" else await _Git.run(self, git_dir, *args)

    out = _run(_repo(), "SSHPtyHandler.cpp:104 `getClientExitCode`", git=_NoLog(), pr_number=4242)
    assert out.pr_diff["merge_sha"] == "" and out.pr_diff["error"] == "pr_unknown"
    assert out.at_commit == {}, "no merge commit: no at_commit leg"
    assert {c["in_pr_diff"] for c in out.citations} == {"unknown"}
    assert out.pr_diff["unknown"] == 2 and "neither the ledger nor the mirror" in out.note


def test_explicit_at_commit_wins_over_the_merge_commit() -> None:
    repo, git = _repo(), _Git()
    out = _run(
        repo,
        f"{SSH}:104",
        git=git,
        pr_number=74989,
        at_commit="abc123",
        merge_facts={"merge_sha": MERGE},
    )
    assert out.at_commit["requested"] == "abc123"
    assert out.pr_diff["merge_sha"] == MERGE and out.citations[0]["in_pr_diff"] == "true"


def test_merge_subject_rule() -> None:
    from server.code.app.citations import _is_merge_subject

    assert _is_merge_subject("Merge pull request #74989 from ClickHouse/fix-ssh", 74989)
    assert _is_merge_subject("Fix the thing (#74989)", 74989)
    assert not _is_merge_subject("Merge pull request #749891 from x/y", 74989)
    assert not _is_merge_subject("Backport of #74989 to 25.3 (#80000)", 74989)
