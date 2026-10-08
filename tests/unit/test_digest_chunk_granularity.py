"""The digest and chunk legs read the namespace, not the
granules around it — loom.session_summaries is created with 128-row granules
and loom.memory_chunks with 256-row granules."""

from __future__ import annotations

import re

from tests.unit.schema_ddl import ddl


def test_session_summaries_and_memory_chunks_have_small_granules() -> None:
    for table, rows in (("session_summaries", 128), ("memory_chunks", 256)):
        block = ddl(table)
        assert re.search(rf"SETTINGS index_granularity = {rows}\b", block), block[-160:]
