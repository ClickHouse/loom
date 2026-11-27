"""Which indexed paths are not source: test, example and benchmark trees, and prose
files. Pure. `code.search` demotes both for a question about the implementation.

Each class is one pattern in the syntax Python's `re` and ClickHouse's `match` share
(no lookaround), so the ranking's test in Python and the lexical leg's pool cut in SQL
read the same definition (`server.code.store.index.sql.path_penalty_sql`).
"""

from __future__ import annotations

import re

# Test, example, fuzzer and benchmark code is indexed on purpose (agents ask "how is X
# tested"): a directory of that name anywhere in the path, or a C++ test file by name.
NOISE_PATH_PATTERN = (
    r"(?:^|/)(?:tests?|examples?|fuzzers|benchmarks)(?:/|$)"
    r"|(?:^|/)(?:test_[^/]+|[^/]+_(?:test|fuzz))\.(?:h|hpp|cpp|cc|cxx)$"
)

# Documentation, release notes and changelogs, by extension or by a changelog-style
# name, never by a directory word (`src/Coordination/Changelog.cpp` is code). A bug
# report's words are the ones the release notes repeat, so prose outranks the source it
# describes: asked with public bug reports, a prose file ranked first on a quarter of
# them although every fix changed only source.
PROSE_PATH_PATTERN = (
    r"(?i:\.(?:md|mdx|markdown|rst|adoc|txt)$"
    r"|(?:^|/)(?:CHANGELOG|CHANGES|HISTORY|NEWS|RELEASE[-_]?NOTES)(?:\.[a-z]+)?$)"
)
# What the prose pattern matches and is code: `CMakeLists.txt` is build code, and a
# changelog-style name with a code extension is a module.
PROSE_EXEMPT_PATTERN = (
    r"(?i:(?:^|/)cmakelists\.txt$)"
    r"|\.(?:py|pyi|h|hpp|c|cc|cpp|cxx|go|rs|ts|tsx|js|jsx|java|kt|sql|sh)$"
)

_NOISE_RE = re.compile(NOISE_PATH_PATTERN)
_PROSE_RE = re.compile(PROSE_PATH_PATTERN)
_PROSE_EXEMPT_RE = re.compile(PROSE_EXEMPT_PATTERN)


def is_noise_path(path: str) -> bool:
    """True for a file under a test, example, fuzzer or benchmark directory, or a C++
    test file by name."""
    return bool(_NOISE_RE.search(path))


def is_prose_path(path: str) -> bool:
    """True for a documentation or release-notes file (`CMakeLists.txt` is build code)."""
    return bool(_PROSE_RE.search(path)) and not _PROSE_EXEMPT_RE.search(path)
