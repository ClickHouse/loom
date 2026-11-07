"""The code index as other packages read it: a namespace's `code_index_state` rows
(which the tracker and the namespace taxonomy also publish their shared views to), the
issue / PR items and the merged-PR ledger, and the path and churn reads of the tracker's
test signal. Other packages reach the store through `code_index()`, never directly."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from server.code.domain.index.state import IndexState


class CodeIndexReads(Protocol):
    """The code store's reads and state writes other packages use."""

    async def get_state(self, org: str, ns: str, key: str) -> str: ...

    async def set_state(
        self, org: str, ns: str, key: str, value: str, detail: str = ""
    ) -> None: ...

    async def all_state(self, org: str, ns: str) -> IndexState: ...

    async def state_values_for_keys(
        self, keys: list[str]
    ) -> dict[str, dict[tuple[str, str], str]]: ...

    async def items_by_number(
        self, org: str, ns: str, numbers: list[int]
    ) -> dict[int, dict[str, Any]]: ...

    async def prs_by_number(
        self, org: str, ns: str, numbers: list[int]
    ) -> dict[int, dict[str, Any]]: ...

    async def merge_commits_of(
        self, org: str, ns: str, pr_numbers: list[int]
    ) -> dict[int, str]: ...

    async def chunks_for_paths(
        self, org: str, ns: str, model: str, paths: list[str], *, per_path: int = 2
    ) -> list[dict[str, Any]]: ...

    async def paths_by_stem(
        self, org: str, ns: str, stems: list[str], *, limit: int = 40
    ) -> list[dict[str, Any]]: ...

    async def sources_of_tests(
        self, org: str, ns: str, test_paths: list[str], *, limit: int = 20
    ) -> list[str]: ...

    async def pr_churn_touching(
        self,
        org: str,
        ns: str,
        *,
        paths: list[str],
        prefix: str = "",
        since: datetime,
        limit: int = 50,
    ) -> list[dict[str, Any]]: ...


def code_index() -> CodeIndexReads:
    """The code store over the process's ClickHouse client. A disconnected client
    raises here."""
    from server.code.store.index import CodeRepo

    return CodeRepo()
