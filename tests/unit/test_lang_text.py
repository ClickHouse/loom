"""A text file no parser claims is still chunked and
searchable; binaries, oversized and empty files stay out; `.c` parses with
the C++ grammar. An entry without prefixes covers its tree
and the backfill's coverage line says nothing."""

from __future__ import annotations

import pytest

from server.cli.code_backfill import prefix_coverage_note
from server.code.app.index.ingest_parse import _parse_for_ingest
from server.code.domain.extract.lang_text import TEXT_LANG, TEXT_MAX_BYTES, text_lang_for_path


@pytest.fixture(autouse=True)
def _intel_on(monkeypatch):
    monkeypatch.setattr("server.settings.settings.code_intel_enabled", True)


def test_code_without_a_parser_and_plain_text_are_kept_as_text() -> None:
    for path, src in {
        "app/Main.kt": b"fun main() {}",
        "lib/a.rb": b"def a; end",
        "web/x.php": b"<?php f();",
        "ios/A.swift": b"func a() {}",
        "core/B.scala": b"object B",
        "conf/app.conf": b"a 1",
        "notes.txt": b"hello",
        "setup.cfg": b"[flake8]\nmax-line-length = 100\n",
        "gradle/build.gradle": b"plugins {}",
        "schema.graphql": b"type Q { a: ID }",
        "LICENSE": b"MIT",
        "Justfile": b"build:\n  cargo build",
    }.items():
        assert text_lang_for_path(path, src) == TEXT_LANG, path
        pf, err = _parse_for_ingest(path, src)
        assert pf is not None and pf.lang == TEXT_LANG and pf.symbols == [] and err == "", path


def test_binaries_oversized_and_empty_stay_out_and_claimed_types_are_untouched() -> None:
    assert text_lang_for_path("img/a.png", b"\x89PNG") == ""
    assert text_lang_for_path("dist/app.min.js", b"x") == ""
    assert text_lang_for_path("Cargo.lock", b"[[package]]") == ""
    assert text_lang_for_path("a.txt", b"\x00\x01binary") == ""
    assert text_lang_for_path("a.txt", b"x" * (TEXT_MAX_BYTES + 1)) == ""
    assert text_lang_for_path("a.txt", b"   \n") == ""
    assert _parse_for_ingest("img/a.png", b"\x89PNG")[0] is None
    # the detectors that ran before still own their files
    assert _parse_for_ingest("a.py", b"def a(): pass")[0].lang == "python"
    assert _parse_for_ingest("a.yaml", b"a: 1")[0].lang == "config"
    assert _parse_for_ingest("a.md", b"# T")[0].lang == "doc"


def test_c_parses_with_the_cpp_grammar() -> None:
    pf, err = _parse_for_ingest(
        "src/util.c", b'#include "x.h"\nint add(int a, int b) { return a + b; }\n'
    )
    assert pf is not None and pf.lang == "cpp" and err == ""
    assert [s.name for s in pf.symbols] == ["add"]


def test_whole_tree_prefix_keeps_everything_and_reports_nothing() -> None:
    from server.code.domain.index.repos import WHOLE_TREE

    paths = ["src/a.cpp", "docs/b.md", "CMakeLists.txt", "tla/C.tla"]
    assert all(p.startswith(WHOLE_TREE) for p in paths)
    assert prefix_coverage_note(paths, WHOLE_TREE) == ""
