"""Shell detection for any repository, and bats tests as
function symbols with `load` include edges and bats tags."""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_bash")

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract import lang_bats as lb
from server.code.domain.extract import lang_detect as ld
from server.code.domain.extract.chunker import chunk_file
from tests.unit.code_fixtures import grows_linearly

_BATS = b"""#!/usr/bin/env bats
# bats file_tags=core, slow

load test_helper
load 'helpers/assert.bash'
bats_load_library bats-support

setup() {
  run true
}

# bats test_tags=parallel
@test "parallel test execution with --jobs" {
  run bats --jobs 2 "$FIXTURE_ROOT/parallel"
  [ "$status" -eq 0 ]
}

@test 'quotes: it "works"' {
  [ 1 -eq 1 ]
}
"""


def test_linguist_names_and_shebang_order() -> None:
    assert ld.detect_shell("test/run.bats", b"") == "shell"
    assert ld.detect_shell("home/.zshrc", b"") == "shell"
    assert ld.detect_shell("pkg/PKGBUILD", b"") == "shell"
    assert ld.detect_shell("configure.sh.in", b"") == "shell"
    assert ld.detect_shell("bin/tool", b"#!/usr/bin/env -S bash -e\n") == "shell"
    assert ld.detect_shell("bin/tool", b"#!/bin/dash\n") == "shell"
    assert ld.detect_shell("bin/tool", b"#!/usr/bin/env python3\n") == ""
    # ShellCheck's directive outranks the shebang
    assert ld.detect_shell("bin/tool", b"#!/usr/bin/perl\n# shellcheck shell=sh\n") == "shell"
    assert ld.detect_shell("src/a.cpp", b"int main() {}") == ""
    assert ld.shebang_interpreter(b"#! /bin/sh -e\n") == "sh"


def test_extensionless_test_tree_heuristic() -> None:
    nvm_like = b'die () { echo "$@" ; exit 1; }\n\n. ../../nvm.sh\n\nnvm ls || die "ls failed"\n'
    assert ld.detect_shell("test/fast/Unit tests/nvm ls", nvm_like) == "shell"
    # not under a test-ish directory, or not parseable as shell: not shell
    assert ld.detect_shell("docs/notes", nvm_like) == ""
    assert ld.detect_shell("test/fixture", b"this is (not shell {{{\n") == ""
    assert ld.detect_shell("test/data.bin", b"\x00\x01\x02binary") == ""
    assert ld.detect_shell("test/empty", b"   \n") == ""


def test_bats_rewrite_preserves_length_and_lines() -> None:
    out, descs = lb.rewrite(_BATS)
    assert len(out) == len(_BATS) and out.count(b"\n") == _BATS.count(b"\n")
    assert descs == {13: "parallel test execution with --jobs", 18: 'quotes: it "works"'}
    assert b"@test" not in out
    import re

    assert re.search(
        rb"parallel_test_execution_with_jobs_+\(\) \{", out
    )  # slugged, padded to width


def test_bats_symbols_includes_and_tags() -> None:
    pf = lb.parse_bats("test/parallel.bats", _BATS)
    assert pf.source == _BATS  # the original bytes are what chunks and edges read
    names = {s.name: s for s in pf.symbols}
    assert set(names) == {"setup", "test_parallel_test_execution_with_jobs", "test_quotes_it_works"}
    t = names["test_parallel_test_execution_with_jobs"]
    assert t.qualified_name == "test.parallel::test_parallel_test_execution_with_jobs"
    assert t.signature == '@test "parallel test execution with --jobs"'
    assert (t.start_line, t.end_line) == (13, 16)
    assert pf.includes == [
        "test_helper.bash",
        "helpers/assert.bash",
        "$BATS_LIB_PATH/bats-support/load.bash",
    ]
    file_tags, per_line = lb.tags(_BATS.decode())
    # the line the tag is written on (a blank line above it is not the tag's)
    assert file_tags == ("core", "slow") and per_line == {12: ("parallel",)}
    # chunks read the original text, one per test function
    chunks = chunk_file(pf)
    fn = [c for c in chunks if c.kind == "function"]
    assert any('@test "parallel test execution with --jobs" {' in c.content for c in fn)


def test_ingest_dispatches_bats_and_new_shell_names() -> None:
    pf = parse_file("test/parallel.bats", _BATS)
    assert pf is not None and pf.lang == "shell"
    assert any(s.name == "test_quotes_it_works" for s in pf.symbols)
    rc = parse_file("home/.bashrc", b"alias ll='ls -la'\nexport PATH=$PATH:~/bin\n")
    assert rc is not None and rc.lang == "shell"
    assert parse_file("docs/notes", b"plain prose here\n") is None


def test_a_tag_line_of_spaces_is_read_once() -> None:
    grows_linearly(
        lambda n: lb._TAGS_RE.findall("# bats test_tags=a" + " " * n + "b"), 8_000, 32_000
    )
