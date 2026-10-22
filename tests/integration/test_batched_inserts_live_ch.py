"""A batched insert (server/core/ch_writes.py, `batched_insert_settings`) is read back
as soon as it returns, concurrent ones share a part, and a batch sent twice with one
deduplication token lands once. Runs on a private table with the deduplication
window Loom's tables carry. On the coded conversation_embeddings table, a vector of
another width in a shared flush fails only its own insert."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, cast

import pytest

from server.core.ch import CHClient, set_query_tag
from server.core.ch_errors import WIDTH_MISMATCH_CODE, error_code
from server.core.ch_writes import batched_insert_settings
from server.core.datetimes import naive_utc_now_seconds
from server.core.vectors import codes_state
from server.knowledge.store import conversations
from server.settings import settings
from tests.integration._eventually import eventually
from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest.fixture
def one_flush_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow concurrent inserts to share a busy window."""
    monkeypatch.setattr(settings, "ch_batched_inserts", True)
    monkeypatch.setattr(settings, "ch_batched_insert_wait_min_ms", 1000)
    monkeypatch.setattr(settings, "ch_batched_insert_wait_max_ms", 1000)


@pytest.mark.asyncio
@pytest.mark.usefixtures("one_flush_window")
async def test_batched_inserts_are_read_back_share_parts_and_land_once_per_token() -> None:
    table = f"loom.batched_insert_{uuid.uuid4().hex[:12]}"
    c = CHClient()
    await c.connect()
    try:
        await c.execute(
            f"CREATE TABLE {table} (k String, v String) ENGINE = MergeTree ORDER BY k "
            "SETTINGS non_replicated_deduplication_window = 1000"
        )
        # No merge may fold the parts the shared-parts check counts.
        await c.execute(f"SYSTEM STOP MERGES {table}")

        # Read back at once: the insert returned after its part was written.
        await c.insert(table, [{"k": "one", "v": "a"}], settings=batched_insert_settings())
        got_one = await c.query_all(f"SELECT k, v FROM {table} WHERE k = 'one'")
        assert got_one == [{"k": "one", "v": "a"}]

        # Concurrent inserts share parts: fewer parts than inserts.
        await asyncio.gather(
            *(
                c.insert(table, [{"k": f"c{i}", "v": "b"}], settings=batched_insert_settings())
                for i in range(40)
            )
        )
        count = await c.query_one(f"SELECT count() AS n FROM {table} WHERE startsWith(k, 'c')")
        assert count is not None and count["n"] == 40
        parts = await c.query_one(
            f"SELECT uniqExact(_part) AS n FROM {table} WHERE startsWith(k, 'c')"
        )
        assert parts is not None and 1 <= parts["n"] < 40

        # A batch sent twice with one token (a retry) lands once, in sequence and at once.
        retry_settings = {
            **batched_insert_settings(),
            "insert_deduplication_token": uuid.uuid4().hex,
        }
        await c.insert(table, [{"k": "retry", "v": "x"}], settings=dict(retry_settings))
        await c.insert(table, [{"k": "retry", "v": "x"}], settings=dict(retry_settings))
        race_settings = {
            **batched_insert_settings(),
            "insert_deduplication_token": uuid.uuid4().hex,
        }
        await asyncio.gather(
            *(
                c.insert(table, [{"k": "race", "v": "y"}], settings=dict(race_settings))
                for _ in range(2)
            )
        )
        got = await c.query_all(
            f"SELECT k, count() AS n FROM {table} WHERE k IN ('retry', 'race') GROUP BY k"
        )
        assert {r["k"]: r["n"] for r in got} == {"retry": 1, "race": 1}
    finally:
        await c.execute(f"DROP TABLE IF EXISTS {table}")
        await c.disconnect()


def _embedding_row(org: str, msg_id: str, vector: list[float]) -> dict[str, Any]:
    return {
        "org": org,
        "namespace": "default",
        "agent": "batched-insert",
        "thread_id": "t1",
        "msg_id": msg_id,
        "role": "user",
        "content_excerpt": msg_id,
        "embedding_model": "batched-insert-test",
        "embedding_dim": len(vector),
        "embedding": vector,
        "user_id": "-",
        "run_id": "-",
        "embed_attempts": 1,
        "embed_last_error": "",
        "embed_failed_at": None,
        "is_saved": False,
        "saved_at": None,
        "ts": naive_utc_now_seconds(),
    }


@pytest.mark.asyncio
@pytest.mark.usefixtures("one_flush_window")
async def test_a_vector_of_another_width_fails_only_its_own_insert_in_a_shared_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keep all 21 requests buffered until the test confirms they share a flush.
    monkeypatch.setattr(settings, "ch_pool_maxsize", 64)
    monkeypatch.setattr(settings, "ch_batched_insert_wait_min_ms", 30000)
    monkeypatch.setattr(settings, "ch_batched_insert_wait_max_ms", 30000)
    set_query_tag("integration.batched-width")
    tasks: list[asyncio.Task[None]] = []
    table = "loom.conversation_embeddings"
    c = CHClient()
    await c.connect()
    try:
        width = codes_state.stored_width(table, "embedding")
        if width is None or width != int(settings.embedding_dim):
            pytest.skip("conversation_embeddings is not coded at the served width here")
        org = unique_org("batched-width")
        query_ids = {msg: f"{org}:{msg}" for msg in ["stale", *(f"m{i}" for i in range(20))]}
        calls: list[tuple[str, Any]] = []
        insert = c.insert

        async def recording_insert(
            table: str, rows: list[dict[str, Any]], settings: dict[str, Any] | None = None
        ) -> None:
            mode = (settings or {}).get("async_insert")
            calls.append((rows[0]["msg_id"], mode))
            # Only the batched send is tagged: the committed resend runs under its own id.
            tag = {"query_id": query_ids[rows[0]["msg_id"]]} if mode == 1 else {}
            await insert(table, rows, settings={**(settings or {}), **tag})

        c.insert = recording_insert  # type: ignore[method-assign]

        async def stale_width_insert() -> None:
            # A replica with a stale reading: its row skips the width guard and
            # reaches ClickHouse in the same flush as the others.
            row = _embedding_row(org, "stale", [0.5] * (width // 2))
            await c._insert_rows(
                table, [row], {**batched_insert_settings(), "query_id": query_ids["stale"]}
            )

        rows = [_embedding_row(org, f"m{i}", [0.5] * width) for i in range(20)]
        good = [conversations.insert_embedding(cast(Any, row), ch=c) for row in rows]
        tasks = [asyncio.create_task(call) for call in [stale_width_insert(), *good]]

        seen: list[Any] = []

        async def shared_buffer() -> bool:
            buffers = await c.query_all(
                "SELECT entries.query_id AS ids FROM system.asynchronous_inserts"
                " WHERE database = {db:String} AND table = 'conversation_embeddings'",
                {"db": settings.ch_database},
            )
            seen[:] = [sorted(buffer["ids"]) for buffer in buffers]
            return any(set(query_ids.values()) <= set(buffer["ids"]) for buffer in buffers)

        try:
            await eventually(shared_buffer, max_wait=10, description="all 21 inserts in one buffer")
        except AssertionError as e:
            raise AssertionError(f"{e}; buffers seen: {seen}") from e
        await c.execute("SYSTEM FLUSH ASYNC INSERT QUEUE loom.conversation_embeddings")
        results = await asyncio.gather(*tasks, return_exceptions=True)

        stale_error = results[0]
        assert stale_error is not None and error_code(stale_error) == WIDTH_MISMATCH_CODE
        assert [r for r in results[1:] if r is not None] == []
        # No DISTINCT: a row that lands twice (a shared flush and its committed resend)
        # reads as n == 2. The vector is compared by length and first element.
        got = await c.query_all(
            "SELECT org, namespace, agent, thread_id, msg_id, role, content_excerpt,"
            " embedding_model, embedding_dim, length(embedding) AS embedding_len,"
            " toFloat64(embedding[1]) AS embedding_first, user_id, run_id, embed_attempts,"
            " embed_last_error, embed_failed_at, is_saved, saved_at, toString(ts) AS ts,"
            " count() AS n"
            f" FROM {table} WHERE org = {{org:String}}"
            " GROUP BY ALL ORDER BY msg_id",
            {"org": org},
        )
        expected = [
            {
                **{k: v for k, v in row.items() if k != "embedding"},
                "embedding_len": len(row["embedding"]),
                "embedding_first": row["embedding"][0],
                "ts": row["ts"].strftime("%Y-%m-%d %H:%M:%S.000"),
                "n": 1,
            }
            for row in sorted(rows, key=lambda row: row["msg_id"])
        ]
        assert [dict(r) for r in got] == expected
        # Each good row went batched first; the stale row failed the flush it shared, and
        # the rows in it were resent committed.
        first: dict[str, Any] = {}
        for msg_id, mode in calls:
            first.setdefault(msg_id, mode)
        assert all(first[f"m{i}"] == 1 for i in range(20))
        assert any(mode == 0 for _, mode in calls)  # at least one row was resent committed
    finally:
        try:
            if any(not t.done() for t in tasks):
                try:
                    await c.execute("SYSTEM FLUSH ASYNC INSERT QUEUE loom.conversation_embeddings")
                finally:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True), timeout=40
                    )
        finally:
            await c.disconnect()
