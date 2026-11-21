"""The reject integration check observes the vector it is about to copy."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tests.integration import test_pg_authority as integration


class Publication:
    def __init__(self, delayed: bool) -> None:
        self.delayed = delayed
        self.head = {"version": 11, "status": "active", "rejection_reason": ""}
        self.document = None if delayed else ("active", 11)
        self.read_pending = asyncio.Event()
        self.published = asyncio.Event()
        if not delayed:
            self.published.set()

    async def publish(self) -> None:
        await self.read_pending.wait()
        self.document = ("active", 11)
        self.published.set()

    async def call(self, op: str, body: dict[str, Any]) -> httpx.Response:
        assert body["agent"] == "a" and body["memory_key"] == "k"
        if op == "memory.set":
            assert body["memory_type"] == "semantic" and body["value"] == "a fact to reject"
            return httpx.Response(200, json={})
        assert op == "memory.reject" and body["rejection_reason"] == "wrong"
        self.head = {"version": 12, "status": "rejected", "rejection_reason": "wrong"}
        if self.document is not None:
            self.document = ("rejected", 12)
        return httpx.Response(200, json={"rejected": True})

    async def fetchval(self, sql: str, org: str, ns: str, key: str) -> dict[str, Any]:
        assert "FROM loom.memory_current" in sql and org == "fixture-org" and key == "k"
        assert ns.startswith("pg-")
        return dict(self.head)

    def query(
        self, sql: str, *, parameters: dict[str, Any], settings: dict[str, Any] | None = None
    ) -> Any:
        assert parameters["org"] == "fixture-org" and parameters["ns"].startswith("pg-")
        if "FROM loom.memory_embeddings" in sql:
            assert settings == integration._SEQUENTIAL
            rows = [] if self.document is None else [self.document]
            if self.document is None:
                self.read_pending.set()
            return SimpleNamespace(result_rows=rows)
        assert "FROM loom.memory FINAL" in sql
        return SimpleNamespace(result_rows=[(self.head["status"], self.head["rejection_reason"])])

    async def eventually(
        self,
        predicate: Any,
        *,
        description: str,
        max_wait: float = 15.0,
        interval: float = 0.1,
    ) -> None:
        assert (max_wait, interval) == (15.0, 0.1)
        if await predicate():
            return
        # The first empty read releases publication, without a clock or database.
        assert self.read_pending.is_set()
        await self.published.wait()
        assert await predicate(), description


@pytest.mark.asyncio
@pytest.mark.parametrize("delayed", [False, True], ids=["published", "publication-pending"])
async def test_reject_check_waits_for_the_initial_vector(
    monkeypatch: pytest.MonkeyPatch, delayed: bool
) -> None:
    publication = Publication(delayed)
    monkeypatch.setattr(integration, "eventually", publication.eventually)
    publisher = asyncio.create_task(publication.publish()) if delayed else None
    try:
        await integration.test_a_reject_is_the_authoritys_head_and_clickhouses_row(
            publication.call, publication, "fixture-org", publication
        )
    finally:
        if publisher is not None:
            publisher.cancel()
            await asyncio.gather(publisher, return_exceptions=True)
    assert publication.document == ("rejected", 12)
