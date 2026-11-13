"""A document costs what is on the screen.

Selecting `substring(content, 1, 220)` for a one-line preview would read the
text of every document in the namespace, and a large granule makes
`knowledge.get` read far more than its document. The table stores the
preview as a MATERIALIZED column and keeps 64-row granules; the listing
selects the stored column and never names `content`.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from server.core.tag_sql import TagFilter
from server.knowledge.store import documents as documents_repo
from tests.unit.schema_ddl import ddl


class _CH:
    def __init__(self) -> None:
        self.sqls: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list:
        self.sqls.append(sql)
        return []


@pytest.mark.asyncio
async def test_the_listing_reads_the_stored_preview_and_never_the_content() -> None:
    ch = _CH()
    await documents_repo.list_with_chunk_counts(
        "o", ["n"], tag_filter=TagFilter(("a",), "folded_any"), ch=ch
    )
    (sql,) = ch.sqls  # an empty page reads no chunk counts
    assert "substring(content" not in sql
    # the page's column list names the preview, not the content
    columns = sql[sql.index("SELECT") : sql.index("FROM loom.documents")]
    names = {c.strip() for c in re.split(r"[,\s]+", columns.replace("SELECT", "")) if c.strip()}
    assert "content_preview" in names and "content" not in names


def test_the_documents_table_stores_the_preview_in_small_granules() -> None:
    block = ddl("documents")
    assert "`content_preview` String MATERIALIZED substring(content, 1, 220)" in block
    assert re.search(r"SETTINGS index_granularity = 64\b", block), block[-160:]
