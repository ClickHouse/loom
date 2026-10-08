"""A call site binds only within its language class.

The name table `CodeRepo.name_candidates` returns is one dictionary over
every parser and tier; rung 3 of the resolver ("unique repo-wide name")
bound a bash `seq` to a C++ method of that name. The gate narrows the table
by the caller's language class before the stamped resolver sees it, and a
shell caller never binds a program name at all.
"""

from __future__ import annotations

import pytest

from server.code.domain.extract import name_scope as ns
from server.code.domain.extract.edges_treesitter import extract_edges
from server.code.domain.extract.treesitter import ParsedFile, Symbol, symbol_id

_CPP_SEQ = ("id-cpp", "DB::QueryRunnerJobCompletion::seq", "method")
_SH_HELPER = ("id-sh", "tests.queries.shell_config::wait_for_query_to_start", "function")
_PY_HELPER = ("id-py", "tests.integration.helpers.cluster::ClickHouseCluster::start", "method")
_BARE_CPP = ("id-bare", "parseQuery", "function")


def test_qualified_name_class_reads_the_scope() -> None:
    assert ns.qualified_name_class("DB::MergeTreeData::getSettings") == ns.NATIVE
    assert ns.qualified_name_class("parseQuery") == ns.NATIVE
    assert (
        ns.qualified_name_class("tests.queries.shell_config::wait_for_query_to_start") == ns.SCRIPT
    )
    assert (
        ns.qualified_name_class("ci.jobs.scripts.create_release::ReleaseInfo::docker") == ns.SCRIPT
    )
    # a C++ identifier cannot contain '.', so a dotted scope is never misread
    assert ns.qualified_name_class("a.b::c") == ns.SCRIPT
    # ...except the anonymous-namespace shape, which embeds the TU's path:
    # 2,198 live edges bind to these from C++ callers and must keep doing so
    assert (
        ns.qualified_name_class(
            "DB::$anonymous_namespace_src/Interpreters/TreeRewriter.cpp::CustomizeVisitor"
        )
        == ns.NATIVE
    )
    assert ns.qualified_name_class("$anonymous_namespace_src/IO/x.cpp::f") == ns.NATIVE
    # ...and the variadic template pack, the other native `.` (12 live edges)
    assert (
        ns.qualified_name_class("DB::GatherUtils::ArraySinkCreator<Type, Types...>::create")
        == ns.NATIVE
    )
    assert (
        ns.qualified_name_class("FormatStringHelperImpl::FormatStringHelperImpl<Args...>")
        == ns.NATIVE
    )


def test_language_class_covers_the_graphed_languages_only() -> None:
    assert ns.language_class("cpp") == ns.NATIVE
    assert ns.language_class("python") == ns.SCRIPT
    assert ns.language_class("shell") == ns.SCRIPT
    assert ns.language_class("sql") == ""
    assert ns.language_class("") == ""


@pytest.mark.parametrize("caller_lang", ["shell", "python"])
def test_script_callers_never_bind_native_definitions(caller_lang: str) -> None:
    table = {"seq": [_CPP_SEQ], "start": [_PY_HELPER, ("id-cpp2", "DB::Server::start", "method")]}
    gated = ns.gate_name_table(table, caller_lang)
    assert "seq" not in gated  # the only candidate was native; the name records a miss
    assert gated["start"] == [_PY_HELPER]


def test_native_callers_never_bind_script_definitions() -> None:
    table = {
        "wait_for_query_to_start": [_SH_HELPER],
        "parseQuery": [_BARE_CPP, _PY_HELPER],
    }
    gated = ns.gate_name_table(table, "cpp")
    assert "wait_for_query_to_start" not in gated
    assert gated["parseQuery"] == [_BARE_CPP]


def test_shell_callers_drop_program_names_before_binding() -> None:
    # a repository that defines a shell function called `seq` still does not
    # get an edge from `seq 1 10`: the program specification wins
    table = {"seq": [("id", "tests.queries.helpers::seq", "function")], "sort": [_SH_HELPER]}
    gated = ns.gate_name_table(table, "shell")
    assert gated == {}
    # the same table is untouched for a Python caller (Python has no `seq` program)
    assert ns.gate_name_table(table, "python") == table


def test_unknown_language_leaves_the_table_alone() -> None:
    table = {"x": [_CPP_SEQ, _SH_HELPER]}
    assert ns.gate_name_table(table, "sql") == table
    assert ns.gate_name_table({}, "shell") == {}


def test_program_table_is_a_frozenset_of_plain_names() -> None:
    assert isinstance(ns.SHELL_COMMANDS, frozenset)
    assert {"seq", "grep", "timeout", "clickhouse-client", "source"} <= ns.SHELL_COMMANDS
    assert all(n == n.strip() and " " not in n for n in ns.SHELL_COMMANDS)


def test_resolver_records_a_miss_not_a_native_edge_for_a_shell_call() -> None:
    """End to end through the stamped resolver: with the gated table a shell
    function calling `seq` yields no edge (rung 3 finds no candidate)."""
    pytest.importorskip("tree_sitter_bash")
    from server.code.domain.extract.lang_bash import parse_bash

    src = b"#!/usr/bin/env bash\nrun() {\n  seq 1 3\n  helper\n}\n"
    pf: ParsedFile = parse_bash("tests/queries/0_stateless/01_x.sh", src)
    helper = Symbol(
        qualified_name="tests.queries.shell_config::helper",
        name="helper",
        kind="function",
        signature="",
        path="tests/queries/shell_config.sh",
        start_line=1,
        end_line=2,
    )
    wide = {
        "seq": [(symbol_id(_CPP_SEQ[1], ""), _CPP_SEQ[1], "method")],
        "helper": [(helper.id, helper.qualified_name, "function")],
    }
    with_gate = extract_edges(pf, ns.gate_name_table(wide, pf.lang))
    targets = {e.to_qualified_name for e in with_gate}
    assert targets == {helper.qualified_name}
    without_gate = extract_edges(pf, wide)
    assert _CPP_SEQ[1] in {e.to_qualified_name for e in without_gate}, (
        "the fixture must reproduce the defect the gate closes"
    )
