"""Statements over loom.code_search_memo: the served-response memo of a repeated
code.search and the ranked pools its `next_cursor` pages read, both keyed by
(org, namespace, key) and bounded by a read-side TTL.
"""

from __future__ import annotations

from typing import Any

from server.code.store.index.base import CodeRepoBase

MEMO_TABLE = "loom.code_search_memo"


class SearchMemoMixin(CodeRepoBase):
    """code.search's memo and cursor-pool rows."""

    async def search_memo_row(
        self, org: str, ns: str, key: str, *, ttl_s: int
    ) -> dict[str, Any] | None:
        """The newest row under `key` written in the last `ttl_s` seconds
        (payload, indexed_commit, route, created_at), or None."""
        row: dict[str, Any] | None = await self._ch.query_one(
            f"SELECT payload, indexed_commit, route, created_at FROM {MEMO_TABLE} "
            "WHERE org = {org:String} AND namespace = {ns:String} AND key = {key:String} "
            "AND created_at > now() - toIntervalSecond({ttl:UInt32}) "
            "ORDER BY created_at DESC LIMIT 1",
            params={"org": org, "ns": ns, "key": key, "ttl": int(ttl_s)},
        )
        return row

    async def insert_search_memo_row(self, row: dict[str, Any]) -> None:
        """One memo or pool row, async-inserted without waiting: a lost row
        costs one recomputation later."""
        await self._ch.insert(
            MEMO_TABLE, [row], settings={"async_insert": 1, "wait_for_async_insert": 0}
        )
