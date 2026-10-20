"""Reader for the ClickHouse server that holds a CI's per-test coverage.

The coverage import reads two tables there: `checks_coverage_lines` (file,
line range, check, test, per run) and `checks` (each check run's commit). The
endpoint, user and database are operator settings (`code_coverage_source_*`);
statements bind their values as HTTP query parameters, so nothing is spliced.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx

from server.clients._http import get_client
from server.clients._http_errors import raise_for_status_with_detail


class CoverageSource(Protocol):
    """What the import needs from a source: rows of one statement."""

    async def rows(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]: ...


def _param_value(v: Any) -> str:
    """A query-parameter value in the text form ClickHouse parses for its
    declared type (arrays and tuples as their literal)."""
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_literal(x) for x in v) + "]"
    return str(v)


def _literal(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return "(" + ",".join(_literal(x) for x in v) + ")"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


class HttpCoverageSource:
    """`CoverageSource` over the ClickHouse HTTP interface. Read-only: every
    statement is a SELECT sent with `readonly=1`-compatible parameters only."""

    def __init__(
        self, url: str, *, user: str, password: str, database: str, timeout_s: float
    ) -> None:
        self._url = url.rstrip("/") + "/"
        self._auth = httpx.BasicAuth(user, password) if user else None
        self._database = database
        self._timeout = httpx.Timeout(
            timeout_s, connect=10.0
        )  # not a setting: a connect; the read bound is the caller's

    async def rows(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        query: dict[str, str] = {"database": self._database, "default_format": "JSONEachRow"}
        for k, v in params.items():
            query[f"param_{k}"] = _param_value(v)
        resp = await get_client().post(
            self._url,
            params=query,
            content=sql.encode(),
            auth=self._auth if self._auth is not None else httpx.USE_CLIENT_DEFAULT,
            timeout=self._timeout,
        )
        raise_for_status_with_detail(resp, "coverage source")
        return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def source_from_settings() -> HttpCoverageSource | None:
    """The configured source, None when `code_coverage_source_url` is empty."""
    from server.settings import settings

    if not settings.code_coverage_source_url:
        return None
    return HttpCoverageSource(
        settings.code_coverage_source_url,
        user=settings.code_coverage_source_user,
        password=settings.code_coverage_source_password,
        database=settings.code_coverage_source_database,
        timeout_s=settings.code_coverage_source_timeout_s,
    )
