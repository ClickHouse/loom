"""Typing fixture: the blocking client proxies a consumer reads off
`SyncAgent`, as its module docstring shows them. Must type-check under
mypy --strict. Never executed."""

from __future__ import annotations

from loom import SyncAgent

with SyncAgent("acme", "support", "bot", api_key="k") as sa:
    sa.memory.set("last_pr", 12345)
    got = sa.memory.get("last_pr")
    hits = sa.memory.search("flaky test", top_k=20)
    _ = (got, hits)
