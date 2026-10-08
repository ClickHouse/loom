"""code.symbol on a bare name: source definitions rank above test, example,
fuzzer and benchmark paths (a Python test helper must not outrank DB::Field
once tests/ is indexed)."""

from __future__ import annotations

import pytest

from server.code.store.index import demote_noise_paths, is_noise_path


@pytest.mark.parametrize(
    "path, noise",
    [
        ("tests/integration/helpers/dictionary.py", True),
        ("src/Storages/MergeTree/tests/gtest_parts.cpp", True),
        ("src/Common/examples/hashes.cpp", True),
        ("src/Parsers/fuzzers/lexer_fuzzer.cpp", True),
        ("utils/benchmarks/bench.cpp", True),
        ("src/Core/test_field.cpp", True),
        ("src/Core/Field_test.cpp", True),
        ("pkg/store_test.go", True),
        ("ci/praktika/cidb.py", True),
        ("contrib/llvm-project/x.cpp", True),
        ("third_party/foo/bar.py", True),
        ("src/Core/Field.h", False),
        ("src/Processors/Executors/PipelineExecutor.cpp", False),
        ("server/code/app/symbol/symbol.py", False),
        ("contest/latest.cpp", False),  # 'test' inside a word is not a test dir
        ("", False),
    ],
)
def test_is_noise_path(path, noise) -> None:
    assert is_noise_path(path) is noise


def test_demote_is_a_stable_partition() -> None:
    rows = [
        {
            "qualified_name": "tests.helpers::Field",
            "path": "tests/integration/helpers/dictionary.py",
        },
        {"qualified_name": "DB::Field", "path": "src/Core/Field.h"},
        {"qualified_name": "DB::Field::Field", "path": "src/Core/Field.h"},
        {"qualified_name": "gtest::Field", "path": "src/Core/tests/gtest_field.cpp"},
    ]
    out = demote_noise_paths(rows)
    assert [r["qualified_name"] for r in out] == [
        "DB::Field",
        "DB::Field::Field",
        "tests.helpers::Field",
        "gtest::Field",
    ]
    assert demote_noise_paths([]) == []
