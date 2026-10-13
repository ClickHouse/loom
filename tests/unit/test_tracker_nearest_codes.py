"""The tracker nearest read joins the coded vector layout.

`TrackerEmbedRepo.nearest` is a brute-force cosine scan over
loom.qa_item_embeddings that reads ~500 MB a call on a production-sized table.
With the column on `Quantized('rabitq', …)` the scan reads the codes and
rescores a shortlist, which needs the bare `ORDER BY distance ASC LIMIT n`
shape and the settings fragment naming this table's column.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.core.schema.vector_codec_ddl import CODED_TABLES, rewrite_statement
from server.core.vectors import codes_state as vector_codes_state
from server.core.vectors import quantized_setting
from server.core.vectors.placeholders import PLACEHOLDER_COLUMNS
from server.settings import settings
from server.tracker.store import embed as qr


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.sql: list[str] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.sql.append(" ".join(sql.split()))
        return [dict(r) for r in self.rows]


def _hit(kind: str, number: int, distance: float) -> dict[str, Any]:
    return {
        "kind": kind,
        "number": number,
        "source_id": "",
        "component": "",
        "is_open": 0,
        "created_at": None,
        "distance": distance,
    }


@pytest.fixture
def _codes(monkeypatch: pytest.MonkeyPatch):
    quantized_setting.set_quantized_codes_supported(True)
    monkeypatch.setattr(settings, "vector_search_quantized_codes", True)
    yield
    quantized_setting.set_quantized_codes_supported(None)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_codes")
async def test_coded_column_sends_the_codes_flag_with_the_tracker_multiplier(monkeypatch) -> None:
    vector_codes_state.set_coded({"loom.qa_item_embeddings.embedding"})
    monkeypatch.setattr(settings, "tracker_embed_fetch_multiplier", 10.0)
    ch = _CH([])
    await qr.TrackerEmbedRepo(ch).nearest("o", "ns", vector=[0.1] * 4, dim=4)  # type: ignore[arg-type]
    q = ch.sql[-1]
    assert "ORDER BY distance ASC LIMIT {limit:UInt32} SETTINGS" in q
    assert "vector_search_use_quantized_codes = 1" in q
    assert "vector_search_index_fetch_multiplier = 10.0" in q


@pytest.mark.asyncio
@pytest.mark.usefixtures("_codes")
async def test_plain_column_keeps_the_flag_off() -> None:
    vector_codes_state.set_coded({"loom.memory_embeddings.embedding"})
    ch = _CH([])
    await qr.TrackerEmbedRepo(ch).nearest("o", "ns", vector=[0.1] * 4, dim=4)  # type: ignore[arg-type]
    assert "vector_search_use_quantized_codes" not in ch.sql[-1]


@pytest.mark.asyncio
async def test_equal_distances_still_come_back_in_kind_number_order() -> None:
    ch = _CH([_hit("pr", 9, 0.2), _hit("pr", 3, 0.1), _hit("issue", 7, 0.2), _hit("issue", 2, 0.2)])
    rows = await qr.TrackerEmbedRepo(ch).nearest("o", "ns", vector=[0.1] * 4, dim=4)  # type: ignore[arg-type]
    assert [(r["kind"], r["number"]) for r in rows] == [
        ("pr", 3),
        ("issue", 2),
        ("issue", 7),
        ("pr", 9),
    ]


def test_the_table_is_on_the_coded_layout_with_a_placeholder_rule() -> None:
    assert CODED_TABLES["qa_item_embeddings"] == (("embedding",), None)
    assert PLACEHOLDER_COLUMNS["loom.qa_item_embeddings"] == ("embedding",)
    ddl = rewrite_statement(
        "CREATE TABLE IF NOT EXISTS loom.qa_item_embeddings\n(\n"
        "    `number` UInt64,\n    `embedding` Array(Float32) DEFAULT [],\n"
        "    `embedding_dim` UInt16 DEFAULT 0\n)",
        3072,
    )
    assert (
        "`embedding` Array(BFloat16) DEFAULT CAST(arrayWithConstant(3072, 0), 'Array(BFloat16)') CODEC(Quantized('rabitq', 3072, 0))"
        in (ddl or "")
    )
