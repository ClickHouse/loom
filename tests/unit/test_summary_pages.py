"""Source filtering cannot make an unfinished digest scan look complete."""

from __future__ import annotations

from datetime import datetime

import pytest

from server.core.errors import BackendUnavailableError
from server.memory.store import summary_pages
from server.memory.store.rows import identity_key
from tests.unit.test_memory_verify import _row


class Pages:
    def __init__(self, rows, order):
        self.rows, self.order, self.calls = rows, order, []

    def key(self, row):
        if self.order == "session":
            return row["session_id"]
        return (
            row["created_at"],
            row["session_id"],
            row["agent"],
            row["user_id"],
            row["embedding_model"],
        )

    async def query_all(self, sql, params):
        self.calls.append((sql, params))
        assert "org={o:String}" in sql and params["o"] == "o"
        assert params["uid"] == "subject" and "user_id={uid:String}" in sql
        rows = self.rows
        if params["after"] is not None:
            assert "{after:" in sql
            rows = [
                row
                for row in rows
                if (
                    self.key(row) < params["after"]
                    if self.order == "newest"
                    else self.key(row) > params["after"]
                )
            ]
        return rows[: params["lim"]]


async def listing(ch, limit=1):
    return await summary_pages.verified_summary_page(
        ch,
        org="o",
        namespace="n",
        limit=limit,
        order=ch.order,
        where="org={o:String} AND namespace={n:String} AND user_id={uid:String}",
        params={"o": "o", "n": "n", "uid": "subject"},
    )


def candidates(count, order):
    rows = [
        dict(
            agent="a",
            user_id="subject",
            session_id=f"s{i:04}",
            embedding_model="test",
            created_at=datetime(2026, 1, 1),
            d=datetime(2026, 1, 1),
            summary_text=f"fact {i}",
            key_facts=[f"fact {i}"],
            derived_from=["k"],
        )
        for i in range(count)
    ]
    return rows[::-1] if order == "newest" else rows


@pytest.mark.parametrize("order", ["oldest", "newest", "session"])
@pytest.mark.parametrize("withheld", [2, 270])
@pytest.mark.parametrize("qualified", [0, 1, 2])
async def test_pages_fill_from_current_sources(monkeypatch, order, withheld, qualified):
    rows = candidates(withheld + qualified, order)
    heads = [
        _row(
            namespace="n",
            agent="a",
            user_id="subject",
            session_id=row["session_id"],
            memory_key="k",
            value=row["summary_text"],
        )
        for row in rows[withheld:]
    ]

    async def read(keys, **kwargs):
        return {identity_key(row): row for row in heads if identity_key(row) in keys}

    monkeypatch.setattr("server.memory.store.summary_sources.memory_rows_by_identity_keys", read)
    ch = Pages(rows, order)
    result = await listing(ch)
    assert result == rows[withheld:]
    assert len(ch.calls) >= 2
    if order == "session":
        assert all("LIMIT 1 BY session_id" in sql for sql, _ in ch.calls)


async def test_scan_budget_refuses_instead_of_reporting_no_more_digests(monkeypatch):
    monkeypatch.setattr(summary_pages.settings, "memory_digest_listing_max_pages", 2)
    rows = candidates(300, "oldest")
    for row in rows:
        row["derived_from"] = []
    ch = Pages(rows, "oldest")
    with pytest.raises(BackendUnavailableError, match="scan budget"):
        await listing(ch)
    assert len(ch.calls) == 2
