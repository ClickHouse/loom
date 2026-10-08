"""Symbols outside the repo: true edges, never an answer about the repo.

A call to `std::shared_ptr::operator->` is a real edge, but a degree signal or a
neighbour list that counts it is dominated by it (tens of thousands of in-edges in a
large C++ repo). Fan-in, the weekly graph stats and code.symbol's neighbour block drop
these names.
"""

from __future__ import annotations

EXTERNAL_PREFIXES: tuple[str, ...] = (
    "std::",
    "__gnu",
    "__cxx",
    "boost::",
    "testing::",
    "absl::",
    "fmt::",
    "Poco::",
    "google::",
    "grpc::",
    "llvm::",
    "rapidjson::",
    "simdjson::",
    "folly::",
    "re2::",
    "arrow::",
    "parquet::",
    "orc::",
    "Aws::",
    "aws::",
    "curl_",
    "ssl_",
    "zstd",
    "lz4",
)


def is_external_symbol(qualified_name: str) -> bool:
    q = qualified_name or ""
    return not q or q.startswith(EXTERNAL_PREFIXES)
