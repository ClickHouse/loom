"""CMake targets, builds / tests facets and the link graph."""

from __future__ import annotations

from server.code.domain.extract.lang_cmake import (
    cmake_chunks,
    cmake_external_edges,
    cmake_local_edges,
    cmake_units,
    commands,
    is_cmake_path,
    parse_cmake,
)

LIB = b"""file(GLOB SOURCES CONFIGURE_DEPENDS *.cpp)

# The ledger library.
add_library(ledger-lib ${SOURCES} extra/helper.cpp)
target_link_libraries(ledger-lib
    PUBLIC ledger-util
    PRIVATE Boost::headers "$<$<BOOL:${X}>:foo>")

add_library(ledger-util STATIC util.cpp)
function(ledger_add_test name)
  add_executable(${name} ${name}.cpp)
endfunction()
option(BUILD_BENCHMARKS "Build the benchmark binaries" ON)
add_subdirectory(tests)
gtest_discover_tests(ledger-lib)
add_test(NAME smoke COMMAND ledger-util)
"""


def test_paths_and_tokenizer() -> None:
    assert (
        is_cmake_path("src/CMakeLists.txt")
        and is_cmake_path("cmake/Toolchain.cmake")
        and not is_cmake_path("src/a.cpp")
    )
    cmds = commands('set(A "quoted arg" b) # comment\nfoo( x\n  y )\n')
    assert [(c.name, c.args) for c in cmds] == [
        ("set", ["A", "quoted arg", "b"]),
        ("foo", ["x", "y"]),
    ]
    assert cmds[1].start_line == 2 and cmds[1].end_line == 3


def test_targets_facets_links_and_includes() -> None:
    pf = parse_cmake("src/lib/CMakeLists.txt", LIB)
    by = {s.name: s for s in pf.symbols}
    assert (
        by["ledger-lib"].kind == "target"
        and by["ledger-lib"].signature == "library"
        and by["ledger-lib"].doc_comment == "The ledger library."
    )
    assert by["ledger-util"].signature == "library STATIC"
    assert by["ledger_add_test"].kind == "function" and by["BUILD_BENCHMARKS"].kind == "option"
    assert (
        by["BUILD_BENCHMARKS"].signature == "option default ON"
        and by["BUILD_BENCHMARKS"].doc_comment == "Build the benchmark binaries"
    )
    assert by["smoke"].kind == "test"
    u = cmake_units("src/lib/CMakeLists.txt", LIB.decode())
    builds = [(f.name, f.ordinal, f.confidence) for f in u.facets if f.kind == "builds"]
    assert ("src/lib/*.cpp", 1, 0.9) in builds, (
        "a GLOB variable stays a glob, made directory-relative"
    )
    assert ("src/lib/extra/helper.cpp", 1, 1.0) in builds and ("src/lib/util.cpp", 2, 1.0) in builds
    assert [(f.name, f.ordinal) for f in u.facets if f.kind == "tests"] == [
        ("ledger-lib", 1),
        ("ledger-util", 2),
    ]
    assert pf.includes == ["src/lib/tests/CMakeLists.txt"]
    assert [(t, d) for t, d, _l in u.links] == [
        ("ledger-lib", "ledger-util"),
        ("ledger-lib", "Boost::headers"),
    ], "visibility words and generator expressions are not dependencies"


def test_link_edges_local_and_external() -> None:
    pf = parse_cmake("src/lib/CMakeLists.txt", LIB)
    local, external = cmake_local_edges(pf)
    assert [(e.from_qualified_name, e.to_qualified_name, e.edge_kind) for e in local] == [
        ("ledger-lib", "ledger-util", "uses_type")
    ]
    assert [(t, d) for t, d, _l in external] == [("ledger-lib", "Boost::headers")]
    assert cmake_external_edges(pf, external, {}) == [], (
        "an imported target nobody defines is not an edge"
    )
    other = parse_cmake(
        "src/tests/CMakeLists.txt",
        b"add_executable(ledger-test t.cpp)\ntarget_link_libraries(ledger-test PRIVATE ledger-lib)\n",
    )
    local2, external2 = cmake_local_edges(other)
    assert local2 == [] and [(t, d) for t, d, _l in external2] == [("ledger-test", "ledger-lib")]
    ext = cmake_external_edges(other, external2, {"ledger-lib": [("L1", "ledger-lib", "target")]})
    assert [(e.from_qualified_name, e.to_qualified_name, e.confidence) for e in ext] == [
        ("ledger-test", "ledger-lib", 0.9)
    ]


def test_chunks_cover_the_file() -> None:
    pf = parse_cmake("src/lib/CMakeLists.txt", LIB)
    ch = cmake_chunks(pf)
    assert ch[0].kind == "file_header" and ch[0].content.startswith("file(GLOB")
    lib = next(c for c in ch if c.scope == "ledger-lib")
    assert lib.kind == "class_skeleton" and lib.content.startswith("# The ledger library.")
    fn = next(c for c in ch if c.scope == "ledger_add_test")
    assert fn.kind == "function" and "endfunction" in fn.content
    n = LIB.count(b"\n")
    seen = [0] * (n + 2)
    for c in ch:
        for ln in range(c.start_line, c.end_line + 1):
            seen[ln] += 1
    assert all(x <= 1 for x in seen) and sum(seen) >= n - 3


def test_a_line_break_before_the_parenthesis_counts_for_later_commands() -> None:
    cmds = commands("set\n(A b)\nadd_library(x a.cpp)\n")
    assert [(c.name, c.start_line) for c in cmds] == [("set", 1), ("add_library", 3)]
