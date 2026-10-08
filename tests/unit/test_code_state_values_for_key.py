"""Other packages read one state key across every namespace through the code index's
reads, not through their own statement on `loom.code_index_state`."""

from __future__ import annotations

import asyncio
from typing import Any

from server.code.app.reads import CodeIndexReads
from server.code.store.index import CodeRepo


class _CH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((" ".join(sql.split()), params))
        return [
            {"namespace": "code-a", "state_value": "tracker-a"},
            {"namespace": "code-b", "state_value": None},
        ]


def test_state_values_for_key_maps_namespace_to_value() -> None:
    ch = _CH()
    reads: CodeIndexReads = CodeRepo(ch)  # type: ignore[arg-type]
    got = asyncio.run(reads.state_values_for_key("tracker_binding"))
    assert got == {"code-a": "tracker-a", "code-b": ""}
    ((sql, params),) = ch.calls
    assert "FROM loom.code_index_state FINAL WHERE state_key = {k:String}" in sql
    assert params == {"k": "tracker_binding"}


def test_tracker_binding_overrides_read_through_the_code_index(monkeypatch: Any) -> None:
    import server.code.app.reads as reads_mod
    import server.core.ch as ch_mod
    from server.tracker.app import binding

    ch = _CH()
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    monkeypatch.setattr(reads_mod, "code_index", lambda: CodeRepo(ch))  # type: ignore[arg-type]
    try:
        got = asyncio.run(binding.refresh_overrides(force=True))
        assert got == {"code-a": "tracker-a", "code-b": ""}
        assert [p for _, p in ch.calls] == [{"k": binding.OVERRIDE_KEY}]
    finally:
        binding.reset_overrides_for_tests()
