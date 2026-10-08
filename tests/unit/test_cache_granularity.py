"""A cache lookup touches a granule the size of its answer —
loom.idempotency is created with 64-row granules and loom.embedding_cache
with 32-row granules."""

from __future__ import annotations

import re

from tests.unit.schema_ddl import ddl


def test_idempotency_and_embedding_cache_have_small_granules() -> None:
    for table, rows in (("idempotency", 64), ("embedding_cache", 32)):
        block = ddl(table)
        assert re.search(rf"SETTINGS index_granularity = {rows}\b", block), block[-160:]
