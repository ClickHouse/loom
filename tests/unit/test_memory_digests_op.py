"""`memory.digests`: a namespace's session digests are readable
on the wire so their fidelity to the turns they compress can be audited."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory.api.models_temporal import MemoryDigestsInput, MemoryDigestsOutput
from server.memory.app import curation as cur
from server.operations.registry import ALL_OPERATIONS


class _FakeCH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows, self.params, self.sql = rows, {}, ""

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.sql, self.params = sql, dict(params or {})
        rows = self.rows
        if after := self.params.get("after"):
            rows = [
                row
                for row in rows
                if (
                    row["created_at"],
                    row["session_id"],
                    row.get("agent", "-"),
                    row.get("user_id", "-"),
                    row.get("embedding_model", ""),
                )
                > after
            ]
        return rows[: int(self.params["lim"])]


def _wire_sources(monkeypatch, rows):
    from tests.unit.test_memory_verify import _row

    for row in rows:
        row.update(agent="a", user_id="-", derived_from=["k1"], key_facts=[row["summary_text"]])

    async def read(keys, **kwargs):
        from server.memory.store.rows import identity_key

        parents = [
            _row(
                namespace="n",
                agent="a",
                session_id=r["session_id"],
                memory_key="k1",
                value=r["summary_text"],
            )
            for r in rows
        ]
        return {identity_key(r): r for r in parents if identity_key(r) in keys}

    monkeypatch.setattr("server.memory.store.summary_sources.memory_rows_by_identity_keys", read)


def test_op_is_registered_for_viewers() -> None:
    op = next(o for o in ALL_OPERATIONS if o.name == "memory.digests")
    assert op.required_role.name == "VIEWER"
    assert op.input_model is MemoryDigestsInput and op.output_model is MemoryDigestsOutput
    assert MemoryDigestsInput(org="o", namespace="n", agent="a").session_id == "*"


@pytest.mark.asyncio
async def test_wildcard_lists_all_sessions_and_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {
            "session_id": f"ns:session_{i}",
            "summary_text": f"digest {i}",
            "key_facts": ["a"],
            "derived_from": ["k1"],
            "valid_at": datetime(2023, 5, i + 1),
            "created_at": datetime(2026, 9, 12),
            "version": 1,
        }
        for i in range(3)
    ]
    _wire_sources(monkeypatch, rows)
    ch = _FakeCH(rows)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    out = await cur.handle_memory_digests(
        MemoryDigestsInput(org="o", namespace="n", agent="a", limit=2)
    )
    assert "session_id = {s:String}" not in ch.sql and ch.params["lim"] == 3
    assert [r.session_id for r in out.rows] == ["ns:session_0", "ns:session_1"] and out.truncated
    assert out.rows[0].key_facts == ["digest 0"] and out.rows[0].derived_from == ["k1"]


@pytest.mark.asyncio
async def test_concrete_session_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _FakeCH([])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    out = await cur.handle_memory_digests(
        MemoryDigestsInput(org="o", namespace="n", agent="a", session_id="ns:session_2")
    )
    assert "session_id = {s:String}" in ch.sql and ch.params["s"] == "ns:session_2"
    assert out.rows == [] and out.truncated is False


@pytest.mark.asyncio
async def test_a_digest_that_reads_as_a_credential_is_withheld(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit._fake_credentials import OPENAI_KEY

    rows = [
        {"session_id": "ns:s1", "summary_text": f"The user pasted {OPENAI_KEY}.", "key_facts": []},
        {"session_id": "ns:s2", "summary_text": "Planned the retreat.", "key_facts": []},
    ]
    _wire_sources(monkeypatch, rows)
    monkeypatch.setattr("server.core.ch.get_client", lambda: _FakeCH(rows))
    out = await cur.handle_memory_digests(MemoryDigestsInput(org="o", namespace="n", agent="a"))
    assert [r.summary_text for r in out.rows] == ["Planned the retreat."]


@pytest.mark.parametrize("withheld", [2, 270])
@pytest.mark.parametrize("qualified", [1, 2])
async def test_withheld_pages_do_not_hide_current_digests(monkeypatch, withheld, qualified):
    rows = [
        {
            "session_id": f"s{i:04}",
            "agent": "a",
            "user_id": "-",
            "embedding_model": "test",
            "created_at": datetime(2026, 1, 1),
            "summary_text": f"fact {i}",
        }
        for i in range(withheld + qualified)
    ]
    _wire_sources(monkeypatch, rows[withheld:])
    monkeypatch.setattr("server.core.ch.get_client", lambda: _FakeCH(rows))
    out = await cur.handle_memory_digests(
        MemoryDigestsInput(org="o", namespace="n", agent="a", limit=1)
    )
    assert [row.session_id for row in out.rows] == [rows[withheld]["session_id"]]
    assert out.truncated is (qualified > 1)
