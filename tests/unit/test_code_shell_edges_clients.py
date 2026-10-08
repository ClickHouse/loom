"""A shell file's edges and facets for any repository — a
client table with dialects, includes resolved to repository paths, invoked
scripts as includes, programs as facets instead of unresolved calls."""

from __future__ import annotations

from types import SimpleNamespace

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract import lang_shell_islands as li
from server.code.domain.extract.chunker import chunk_file


def _units(path: str, text: str) -> li.ShellUnits:
    pf = parse_file(path, text.encode())
    assert pf is not None and pf.lang == "shell"
    return li.shell_units(pf, chunk_file(pf))


def test_client_table_reads_every_client_under_its_dialect() -> None:
    u = _units(
        "scripts/load.sh",
        "\n".join(
            [
                "#!/bin/bash",
                'psql -h db -c "SELECT count() FROM users"',
                "mysql -uroot -e 'SHOW TABLES'",
                'sqlite3 app.db "SELECT name FROM sqlite_master"',
                "sqlite3 app.db .tables",
                'duckdb -c "CREATE TABLE t AS SELECT 1"',
                'bq query "SELECT 1"',
                '$CLICKHOUSE_CLIENT -q "SELECT version()"',
                "trino --execute 'SELECT 2'",
                "",
            ]
        ),
    )
    by_line = {i.line: i for i in u.islands}
    assert {ln: i.dialect for ln, i in by_line.items()} == {
        2: "postgres",
        3: "mysql",
        4: "sqlite",
        6: "duckdb",
        7: "bigquery",
        8: "clickhouse",
        9: "trino",
    }
    assert by_line[4].text == "SELECT name FROM sqlite_master"  # positional, looks like SQL
    assert 5 not in by_line  # `.tables` is a dot-command, not a query
    dialects = {(f.name, f.line) for f in u.facets if f.kind == li.FACET_DIALECT}
    assert ("postgres", 2) in dialects and ("clickhouse", 8) in dialects
    assert all(f.confidence == 1.0 for f in u.facets if f.kind == li.FACET_DIALECT)


def test_resolve_include_is_script_relative_and_drops_what_is_not_a_repo_file() -> None:
    assert li.resolve_include("test/fast/Unit tests/x", "../../../nvm.sh") == "nvm.sh"
    assert li.resolve_include("test/x.bats", "test_helper.bash") == "test/test_helper.bash"
    assert li.resolve_include("a/b.sh", "./c.sh") == "a/c.sh"
    assert li.resolve_include("a/b.sh", "/dev/null") == ""
    assert li.resolve_include("a/b.sh", "/etc/bash_completion") == ""
    assert li.resolve_include("a/b.sh", "../../outside.sh") == ""  # leaves the repository
    assert li.resolve_include("a/b.sh", "$BASH_IT/lib/x.bash") == ""  # a runtime value
    assert (
        li.resolve_include("t/x.bats", "$BATS_LIB_PATH/bats-support/load.bash")
        == "bats-support/load.bash"
    )


def test_include_targets_cover_source_directive_and_invocation() -> None:
    pf = parse_file(
        "libexec/bats-core/bats-exec-test",
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "# shellcheck source=lib/bats-core/common.bash",
                'source "$BATS_ROOT/lib/bats-core/common.bash"',
                "# shellcheck source=SCRIPTDIR/../helpers.bash",
                "# shellcheck source=../shell_config.sh",
                ". ../lib/tracing.bash",
                "source /etc/profile",
                "./scripts/prepare.sh --fast",
                "bash tools/build.sh",
                "python3 -u tools/gen.py out",
                "tools/run.sh",
                "exec bats-preprocess",
                "",
            ]
        ).encode(),
    )
    assert pf is not None
    # Relative targets also carry a `?cwd:` alternate (the
    # working-directory reading) that the ingest keeps only when the namespace
    # holds the file; here nothing is indexed, so the alternates settle away
    from server.code.domain.extract.lang_shell_islands import CWD_MARK, settle_cwd_includes

    assert "?cwd:scripts/prepare.sh" in pf.includes and "?cwd:lib/tracing.bash" not in pf.includes
    assert [i for i in pf.includes if i.startswith(CWD_MARK)] == [
        "?cwd:scripts/prepare.sh",
        "?cwd:tools/build.sh",
        "?cwd:tools/gen.py",
        "?cwd:tools/run.sh",
    ]
    pf.includes = settle_cwd_includes(pf.includes, set())
    assert pf.includes == [  # resolved at parse (include_targets), once
        "libexec/lib/tracing.bash",  # `.` resolved against the script's directory
        "lib/bats-core/common.bash",  # shellcheck source= is repository-relative
        "libexec/helpers.bash",  # SCRIPTDIR/ is script-relative
        "libexec/shell_config.sh",  # and so is a `../` spelling (ClickHouse's)
        "libexec/bats-core/scripts/prepare.sh",
        "libexec/bats-core/tools/build.sh",
        "libexec/bats-core/tools/gen.py",
        "libexec/bats-core/tools/run.sh",
    ]


def test_shell_units_write_resolved_includes_and_program_facets() -> None:
    u = _units(
        "test/fast/Unit tests/nvm_ls_current",
        "\n".join(
            [
                "#!/bin/sh",
                "cleanup () { unset -f nvm_ls_current; }",
                ". ../../../nvm.sh",
                "nvm_ls_current | grep foo",
                "curl -s https://example.org | jq .version",
                "set -e; echo done; cd /tmp",
                "",
            ]
        ),
    )
    programs = {f.name for f in u.facets if f.kind == li.FACET_PROGRAM}
    assert "curl" in programs and "jq" in programs
    assert not programs & li.SHELL_BUILTINS  # set/echo/cd/grep are not facts about a script
    assert all(
        f.confidence == 1.0 and f.ordinal == 0 for f in u.facets if f.kind == li.FACET_PROGRAM
    )


def test_parse_resolves_the_file_includes_once_for_every_pass() -> None:
    path = "test/fast/Unit tests/nvm_ls_current"
    pf = parse_file(path, b"#!/bin/sh\n. ../../../nvm.sh\n. ../../common.sh\nsource /dev/null\n")
    assert pf is not None
    assert pf.includes == ["nvm.sh", "test/common.sh"]  # at parse: the edges-only pass sees it too
    li.shell_units(pf, chunk_file(pf))
    assert pf.includes == ["nvm.sh", "test/common.sh"]  # the chunk stage does not resolve again


def test_bats_load_targets_resolve_through_the_same_path() -> None:
    pf = parse_file(
        "test/completion/aliases.completion.bats",
        "\n".join(
            [
                "load ../test_helper",
                "load ../../completion/available/capistrano.completion",
                "bats_load_library bats-support",
                '@test "x" {',
                "  run true",
                "}",
                "",
            ]
        ).encode(),
    )
    assert pf is not None
    assert pf.includes == [
        "test/test_helper.bash",
        "completion/available/capistrano.completion.bash",
        "bats-support/load.bash",
    ]


def test_drop_program_misses_keeps_only_names_somebody_could_define() -> None:
    def miss(name: str) -> SimpleNamespace:
        return SimpleNamespace(callee_name=name)

    kept = li.drop_program_misses(
        [
            miss("curl"),
            miss("$CLICKHOUSE_CLIENT"),
            miss("./run.sh"),
            miss("../x.sh"),
            miss("/usr/bin/env"),
            miss(":"),
            miss("[["),
            miss('"$@"'),
            miss("nvm_ls_current"),
            miss("assert_success"),
            miss("skip"),
            miss('"${pre_command[@]}"'),
            miss(""),
            miss("npm"),
            miss("setopt"),
            miss("\\grep"),
            miss("2"),
        ]
    )
    assert [m.callee_name for m in kept] == ["nvm_ls_current", "assert_success"]
