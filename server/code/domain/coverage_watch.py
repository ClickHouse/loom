"""A coverage watch: one version of a `loom.code_coverage_watches` row."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class CoverageWatch:
    """A request for one `code.coverage.ready` event on `inbox` once every
    index tier of `namespace` covers `commit_sha`. Each write is a new version
    carrying every column (the table replaces by `version`)."""

    org: str
    namespace: str
    commit_sha: str
    inbox: str
    created_by: str
    created_at: datetime
    delivered_at: datetime | None
    is_deleted: int
    version: int
