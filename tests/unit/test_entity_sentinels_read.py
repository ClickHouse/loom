"""Entity sentinels stay on the row but never reach a caller."""

from __future__ import annotations

import re
from pathlib import Path

from server.memory.domain.entity_sentinels import (
    NONE_SENTINEL,
    PARSE_FAILED_SENTINEL,
    SENTINEL_ENTITIES,
    public_entities,
)
from server.memory.workers import entity_extractor

ROOT = Path(__file__).resolve().parents[2]


def test_public_entities_drops_only_sentinels_and_keeps_order() -> None:
    assert public_entities(["caroline", NONE_SENTINEL, "melanie", PARSE_FAILED_SENTINEL]) == [
        "caroline",
        "melanie",
    ]
    assert public_entities([NONE_SENTINEL]) == []
    assert public_entities(None) == []
    assert public_entities(()) == []


def test_worker_and_readers_share_the_sentinels() -> None:
    assert entity_extractor._NONE_SENTINEL in SENTINEL_ENTITIES
    assert entity_extractor._PARSE_FAILED_SENTINEL in SENTINEL_ENTITIES


def test_point_read_outputs_do_not_emit_raw_entities() -> None:
    """memory.get / list / bulk_get build their outputs through public_entities (memory.get
    and bulk_get through get_output.row_output);
    a new `entities=list(r...)` in these handlers would leak the sentinel again."""
    raw = re.compile(r"entities=list\((r|row)\.")
    for rel in ("server/memory/app/query.py",):
        src = (ROOT / rel).read_text()
        assert not raw.search(src), rel
        assert "public_entities(" in src, rel
    get_src = (ROOT / "server/memory/app/get_output.py").read_text()
    assert 'entities=public_entities(getattr(row, "entities", None))' in get_src
