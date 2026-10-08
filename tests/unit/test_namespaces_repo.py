"""loom.namespaces has one repository owner for the whole-row and liveness reads."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from server.identity.store import namespaces as namespaces_repo

ROOT = Path(__file__).resolve().parents[2]


class _CH:
    def __init__(self, row: Any) -> None:
        self.row = row
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_one(self, sql: str, params: Any = None) -> Any:
        self.calls.append((sql, dict(params or {})))
        return self.row


async def test_fetch_row_reads_every_column_of_one_namespace() -> None:
    ch = _CH({"org": "o", "namespace": "n", "active": 1})
    assert await namespaces_repo.fetch_row("o", "n", ch=ch) == {
        "org": "o",
        "namespace": "n",
        "active": 1,
    }
    sql, params = ch.calls[0]
    assert "SELECT * FROM loom.namespaces FINAL" in sql and params == {"org": "o", "ns": "n"}
    assert await namespaces_repo.fetch_row("o", "n", ch=_CH(None)) is None


async def test_is_active_skips_a_soft_deleted_row() -> None:
    ch = _CH(None)
    assert await namespaces_repo.is_active("o", "n", ch=ch) is False
    assert "AND active" in ch.calls[0][0]


def test_no_other_module_rereads_the_whole_row() -> None:
    """A whole-row read is how an RMT rewrite carries every column forward; one copy of it."""
    hits = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "server").rglob("*.py")
        if "SELECT * FROM loom.namespaces FINAL\n" in p.read_text()
        or "SELECT * FROM loom.namespaces FINAL WHERE" in p.read_text()
    ]
    assert hits == ["server/identity/store/namespaces.py"]
