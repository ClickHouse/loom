"""Repository-specific semantic layers kept outside the generic indexer."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CodeIndexProfile:
    name: str
    sql_registration_names: bool = False
    clickhouse_test_conventions: bool = False
    clickhouse_error_codes: bool = False


GENERIC = CodeIndexProfile("generic")
CLICKHOUSE = CodeIndexProfile(
    "clickhouse",
    sql_registration_names=True,
    clickhouse_test_conventions=True,
    clickhouse_error_codes=True,
)


def profile_for_repo(repo: str) -> CodeIndexProfile:
    normalized = repo.lower().strip().removesuffix(".git").strip("/")
    if normalized in {"clickhouse/clickhouse", "github.com/clickhouse/clickhouse"}:
        return CLICKHOUSE
    return GENERIC
