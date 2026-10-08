"""ClickHouse error codes are symbols. `src/Common/ErrorCodes.cpp`
declares them through a macro list the tree-sitter extractor cannot see;
the parse hook reads the list and emits one `constant` symbol per code."""

from __future__ import annotations

from server.code.app.index import ingest_parse
from server.code.domain.extract.error_codes import error_code_symbols, is_error_codes_path

_SAMPLE = b"""#include <Common/ErrorCodes.h>

#define APPLY_FOR_BUILTIN_ERROR_CODES(M) \\
    M(0, OK) \\
    M(1, UNSUPPORTED_METHOD) \\
    M(49, LOGICAL_ERROR) \\
    M(1000, POCO_EXCEPTION) \\

namespace DB
{
namespace ErrorCodes
{
    APPLY_FOR_ERROR_CODES(M)
}
}
"""


def test_the_macro_list_yields_one_constant_symbol_per_code() -> None:
    syms = error_code_symbols("src/Common/ErrorCodes.cpp", _SAMPLE)
    assert [s.name for s in syms] == ["OK", "UNSUPPORTED_METHOD", "LOGICAL_ERROR", "POCO_EXCEPTION"]
    le = next(s for s in syms if s.name == "LOGICAL_ERROR")
    assert le.qualified_name == "DB::ErrorCodes::LOGICAL_ERROR" and le.kind == "constant"
    assert le.signature == "= 49" and le.start_line == le.end_line == 6 and le.has_body is False
    assert le.id  # a stable symbol id from (qualified_name, signature)


def test_only_the_error_codes_file_is_read_and_other_paths_yield_nothing() -> None:
    assert is_error_codes_path("src/Common/ErrorCodes.cpp")
    assert is_error_codes_path("contrib/x/src/Common/ErrorCodes.cpp")
    assert not is_error_codes_path("src/Common/ErrorCodes.h")
    assert error_code_symbols("src/Common/Exception.cpp", _SAMPLE) == []


def test_the_cpp_parse_hook_appends_the_error_codes_to_the_files_symbols() -> None:
    pf, err = ingest_parse._parse_for_ingest("src/Common/ErrorCodes.cpp", _SAMPLE)
    assert err == "" and pf is not None and pf.lang == "cpp"
    names = {s.qualified_name for s in pf.symbols}
    assert "DB::ErrorCodes::LOGICAL_ERROR" in names and "DB::ErrorCodes::OK" in names
    # any other C++ file gains nothing from the hook
    pf2, _ = ingest_parse._parse_for_ingest(
        "src/Common/Exception.cpp", b"namespace DB { void f() {} }\n"
    )
    assert pf2 is not None and not any(s.kind == "constant" for s in pf2.symbols)
