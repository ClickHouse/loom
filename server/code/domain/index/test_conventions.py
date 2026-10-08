"""Test conventions of the languages the index reads.

A test is a unit that reaches the symbol: the coverage claim's strongest
evidence outside the ClickHouse trees is a `calls` edge from a test unit,
then a test function named for the symbol, then a test file that sits
beside the source by the language's convention. This module holds the
conventions themselves — which paths are test units, which function
names a test of `Bare` (or `Owner::Bare`) conventionally carries, and
where a source file's sibling test would live — as pure functions with
no I/O, so the handler's legs and the tests share one vocabulary.

Data under `testdata/` or `fixtures/` is a payload, not a unit; a
generated `.pb.go` is never a test.
"""

from __future__ import annotations

import posixpath
import re

from server.code.domain.index import coverage_claim as cc

_PAYLOAD_SEGMENTS: frozenset[str] = frozenset(
    {"testdata", "fixtures", "__fixtures__", "__snapshots__"}
)
_TEST_DIR_SEGMENTS: frozenset[str] = frozenset({"tests", "test"})
_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_TS_TEST_RE = re.compile(r"\.(test|spec)\.(ts|tsx|js|jsx|mjs|cjs)$")
_JAVA_TEST_RE = re.compile(r"(^Test[A-Z]\w*|Tests?|IT)\.java$")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_MAX_NAMES = 12
_MAX_SIBLINGS = 8


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def is_test_unit_path(path: str) -> bool:
    """Whether `path` holds test units under the conventions of Go, Python,
    TypeScript / JavaScript, Java / Kotlin, Rust, C / C++, Ruby, bats, or
    the ClickHouse test trees (`coverage_claim.is_test_file`)."""
    p = path.strip().lstrip("./")
    if not p or p.endswith(".pb.go"):
        return False
    segs = _segments(p)
    dirs, base = segs[:-1], segs[-1]
    if any(d in _PAYLOAD_SEGMENTS for d in dirs):
        return False
    if cc.is_test_file(p):
        return True
    in_test_dir = any(d in _TEST_DIR_SEGMENTS or d == "__tests__" for d in dirs)
    if base.endswith("_test.go"):
        return True
    if base.endswith(".py"):
        return (
            base.startswith("test_")
            or base.endswith("_test.py")
            or base == "conftest.py"
            or in_test_dir
        )
    if base.endswith(_JS_EXTS):
        return (
            bool(_TS_TEST_RE.search(base))
            or "__tests__" in dirs
            or any(d in ("cypress", "e2e") for d in dirs)
        )
    if base.endswith((".java", ".kt")):
        return (
            "test" in dirs
            and "src" in dirs
            or bool(_JAVA_TEST_RE.search(base))
            or (base.endswith("Test.kt"))
        )
    if base.endswith(".rs"):
        return base.endswith("_test.rs") or "tests" in dirs
    if base.endswith((".cc", ".cpp", ".cxx", ".c")):
        return (
            base.startswith("gtest_")
            or base.endswith(("_test.cc", "_test.cpp", "_unittest.cc"))
            or in_test_dir
        )
    if base.endswith(".rb"):
        return base.endswith("_spec.rb") or base.startswith("test_")
    return base.endswith(".bats")


def snake(name: str) -> str:
    """`CamelCase` → `camel_case`; an already-snake name is unchanged."""
    return _CAMEL_RE.sub("_", name).lower()


def test_unit_names(bare: str, owner: str = "") -> list[str]:
    """The function names a test of `bare` (a method of `owner`) carries by
    convention — Go's `Test<Name>` / `Test<Owner>_<Name>` (also Benchmark,
    Example, Fuzz), Python's `test_<snake>`, Java's `test<Name>` /
    `<name>Test` / `should<Name>`. TypeScript's `describe` blocks are
    strings, not names: nothing to add. Deduplicated, capped at twelve."""
    bare = bare.strip()
    if not bare:
        return []
    cap = bare[:1].upper() + bare[1:]
    own = owner.strip()
    own_cap = (own[:1].upper() + own[1:]) if own else ""
    names = [f"Test{cap}"]
    if own_cap:
        names += [f"Test{own_cap}_{cap}", f"Test{own_cap}{cap}"]
    names += [f"Benchmark{cap}"]
    if own_cap:
        names.append(f"Benchmark{own_cap}_{cap}")
    names += [f"Example{cap}"]
    if own_cap:
        names.append(f"Example{own_cap}_{cap}")
    names += [f"Fuzz{cap}", f"test_{snake(bare)}"]
    if own:
        names.append(f"test_{snake(own)}_{snake(bare)}")
    names += [f"test{cap}", f"{bare[:1].lower()}{bare[1:]}Test", f"should{cap}"]
    return list(dict.fromkeys(n for n in names if n))[:_MAX_NAMES]


def _py_siblings(directory: str, stem: str, segs: list[str]) -> list[str]:
    out = [
        posixpath.join(directory, f"test_{stem}.py"),
        posixpath.join(directory, f"{stem}_test.py"),
        posixpath.join(directory, "tests", f"test_{stem}.py"),
    ]
    if len(segs) > 1 and segs[0] in ("src", "lib", "app"):
        out.append(posixpath.join("tests", *segs[1:-1], f"test_{stem}.py"))
    return out


def sibling_test_paths(path: str) -> list[str]:
    """Where the test file of `path` would sit by convention: Go's
    `<stem>_test.go` beside it, Python's `test_<stem>.py` beside it or under
    `tests/`, TypeScript's `<stem>.test.ts` / `.spec.ts` / `__tests__/`,
    Java's `src/test/java/…/<Stem>Test.java`, Rust's `tests/<stem>.rs`,
    C++'s `tests/gtest_<stem>.cpp` / `<stem>_test.cpp`. Capped at eight."""
    p = path.strip().lstrip("./")
    segs = _segments(p)
    if not segs:
        return []
    directory = "/".join(segs[:-1])
    base = segs[-1]
    stem, ext = posixpath.splitext(base)
    out: list[str] = []
    if ext == ".go":
        out.append(posixpath.join(directory, f"{stem}_test.go"))
    elif ext == ".py":
        out.extend(_py_siblings(directory, stem, segs))
    elif ext in _JS_EXTS:
        for e in ("ts", "tsx", "js"):
            out += [
                posixpath.join(directory, f"{stem}.test.{e}"),
                posixpath.join(directory, f"{stem}.spec.{e}"),
                posixpath.join(directory, "__tests__", f"{stem}.test.{e}"),
            ]
    elif ext in (".java", ".kt"):
        if "src/main/" in p:
            head, _, tail = p.partition("src/main/")
            rel = tail.split("/", 1)[1] if "/" in tail else tail
            lang_dir = tail.split("/", 1)[0]
            rel_dir = posixpath.dirname(rel)
            out.append(posixpath.join(head, "src/test", lang_dir, rel_dir, f"{stem}Test{ext}"))
    elif ext == ".rs":
        out += [posixpath.join("tests", f"{stem}.rs"), posixpath.join(directory, f"{stem}_test.rs")]
    elif ext in (".cc", ".cpp", ".cxx", ".c", ".h", ".hpp"):
        out += [
            posixpath.join(directory, "tests", f"gtest_{stem}.cpp"),
            posixpath.join(directory, f"{stem}_test.cpp"),
        ]
    return [x for x in dict.fromkeys(out) if x != p][:_MAX_SIBLINGS]


__all__ = ["is_test_unit_path", "sibling_test_paths", "snake", "test_unit_names"]
