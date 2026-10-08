"""A googletest body is a test case (kind `test`), never a
call target. fmt: `f.descriptor()` inside `TEST(buffered_file_test,
descriptor)` bound to the test itself (the file's one `descriptor`)."""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_cpp")

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import extract_edges
from server.code.domain.extract.name_scope import gate_name_table

HEADER = b"""
namespace fmt {
class file {
public:
    int descriptor() const { return fd_; }
    int fd_ = 0;
};
}
"""
TEST = b"""
#include "os.h"
TEST(buffered_file_test, descriptor) {
    fmt::file f;
    f.descriptor();
}
"""


def test_a_gtest_body_is_kind_test_and_never_a_call_target() -> None:
    files = {"include/fmt/os.h": HEADER, "test/os-test.cc": TEST}
    parsed = {rel: parse_file(rel, src) for rel, src in files.items()}
    tests = [
        s
        for s in parsed["test/os-test.cc"].symbols
        if s.qualified_name == "buffered_file_test::descriptor"
    ]
    assert tests and tests[0].kind == "test"
    table: dict[str, list[tuple[str, str, str]]] = {}
    for pf in parsed.values():
        for s in pf.symbols:
            table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    edges = extract_edges(parsed["test/os-test.cc"], gate_name_table(table, "cpp"))
    targets = {e.to_qualified_name for e in edges if e.edge_kind == "calls"}
    assert "buffered_file_test::descriptor" not in targets
    assert "fmt::file::descriptor" in targets, targets
