"""ClickHouse error codes as symbols.

`src/Common/ErrorCodes.cpp` declares every error code once, through a macro
list — `M(49, LOGICAL_ERROR) \\` inside `#define APPLY_FOR_BUILTIN_ERROR_CODES(M)` —
and the tree-sitter C++ extractor sees a preprocessor body, not declarations.
Without a declaration, `LOGICAL_ERROR`, the identifier searched for most in
ClickHouse, has no symbol: `code.symbol` answers "0 definitions" and
`code.search` falls to the hybrid legs, which for a bare identifier serve
four-line include blocks. This pass reads the macro list by regex and
emits one `constant` symbol per code, `DB::ErrorCodes::<NAME>` with
`= <number>` as its signature, at the line the list names it. Nothing else
changes: the `extern const int NAME;` declarations in other files stay
declarations.
"""

from __future__ import annotations

import re

from server.code.domain.extract.treesitter import Symbol

_ERROR_CODES_SUFFIX = "Common/ErrorCodes.cpp"
_ENTRY_RE = re.compile(r"^\s*M\((\d+),\s*([A-Z][A-Z0-9_]*)\)")


def is_error_codes_path(path: str) -> bool:
    return path.endswith(_ERROR_CODES_SUFFIX)


def error_code_symbols(path: str, source: bytes) -> list[Symbol]:
    """One `constant` symbol per `M(<number>, <NAME>)` line of the error-code
    macro list; empty for any other file."""
    if not is_error_codes_path(path):
        return []
    out: list[Symbol] = []
    seen: set[str] = set()
    for lineno, line in enumerate(source.decode("utf-8", "replace").splitlines(), start=1):
        m = _ENTRY_RE.match(line)
        if not m:
            continue
        number, name = m.group(1), m.group(2)
        if name in seen:
            continue
        seen.add(name)
        out.append(
            Symbol(
                qualified_name=f"DB::ErrorCodes::{name}",
                name=name,
                kind="constant",
                signature=f"= {number}",
                path=path,
                start_line=lineno,
                end_line=lineno,
                has_body=False,
            )
        )
    return out
