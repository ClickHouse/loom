"""The dispatch heuristics switch off only for a namespace whose compiler tier holds a
LIVE `overrides` edge: a count over raw rows kept counting edges tombstoned since."""

from __future__ import annotations

from typing import Any

from server.code.app.symbol import dispatch
from server.code.app.ttl_cache import clear_all
from server.code.store.index import CodeRepo


class _CH:
    def __init__(self, row: Any) -> None:
        self.row = row
        self.sql = ""

    async def query_one(self, sql: str, params: Any = None, **_: Any) -> Any:
        self.sql = sql
        return self.row


async def test_the_probe_reads_liveness_per_edge_key() -> None:
    repo = CodeRepo.__new__(CodeRepo)
    repo._ch = _CH(None)  # type: ignore[attr-defined]
    clear_all()
    assert await dispatch._namespace_has_override_edges(repo, "o", "n") is False
    sql = repo._ch.sql  # type: ignore[attr-defined]
    assert "argMax(tombstoned_at IS NULL, version)" in sql and "HAVING is_live = 1" in sql

    repo._ch = _CH({"one": 1})  # type: ignore[attr-defined]
    clear_all()
    assert await dispatch._namespace_has_override_edges(repo, "o", "n") is True
    clear_all()


async def test_a_failed_probe_is_kept_seconds_not_the_hour(monkeypatch) -> None:
    """A read error fails closed (the heuristics stay on), but only for
    PROBE_ERROR_TTL_S: the next probe after it reads again instead of serving the
    error's answer for an hour. The ctor-coverage probe follows the same rule."""
    import time as _time

    from server.code.app.symbol import coverage

    class _Down:
        async def query_one(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("replica restarting")

        async def query_all(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("replica restarting")

    repo = CodeRepo.__new__(CodeRepo)
    repo._ch = _Down()  # type: ignore[attr-defined]
    clear_all()
    assert await dispatch._namespace_has_override_edges(repo, "o", "n") is False
    assert await coverage._ctor_edge_coverage(repo, "o", "n") == (False, False)
    real = _time.monotonic
    monkeypatch.setattr(_time, "monotonic", lambda: real() + dispatch.PROBE_ERROR_TTL_S + 1)
    assert ("o", "n") not in dispatch._OVR_CACHE
    assert ("o", "n") not in coverage._CTOR_CACHE
    monkeypatch.setattr(_time, "monotonic", real)
    clear_all()
