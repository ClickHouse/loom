"""Per-merge fast-tier ingest for the code plane.

The `code_ingest` worker runs one pass per tick over every configured repository; the
pass itself is `server.code.app.index.ingest_pass`, which `code.refresh` runs too.
"""

from __future__ import annotations

from server.code.app.index import ingest_pass
from server.code.app.index.pass_budget import CodeIngestResult


async def run_pass() -> CodeIngestResult:
    """One pass over every configured repository."""
    return await ingest_pass.run_pass()
