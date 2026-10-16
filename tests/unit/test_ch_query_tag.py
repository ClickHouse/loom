"""Every ClickHouse query names the operation that issued it.

The client passes the task's query tag as `log_comment` on every query,
command and insert; the transports set it per request and the worker
supervisor per pass; an untagged caller sends exactly what it sent before.
"""

from __future__ import annotations

from typing import Any

import pytest

import server.core.ch as ch_mod
from server.core import ch_schema_guard


@pytest.fixture(autouse=True)
def _clear_tag(monkeypatch: pytest.MonkeyPatch) -> Any:
    ch_mod.set_query_tag("")
    ch_mod.describe_cache_clear()
    # The schema guard reads the migration ledger through this
    # same client once per TTL before the first statement — a `query` the
    # call lists below would count whenever another test (or 60 s of suite)
    # left its cache cold. These tests are about the caller's statements
    # carrying the tag; the guard sees nothing pending here and its state is
    # reset after.

    async def _nothing_pending(**_kw: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(ch_schema_guard, "pending_tables", _nothing_pending)
    ch_schema_guard.schema_guard_reset()
    yield
    ch_mod.set_query_tag("")
    ch_mod.describe_cache_clear()
    ch_schema_guard.schema_guard_reset()


def test_query_settings_is_empty_without_a_tag() -> None:
    assert ch_mod.query_settings() == {}
    assert ch_mod.query_settings({"async_insert": 0}) == {"async_insert": 0}


def test_query_settings_adds_log_comment_and_keeps_caller_settings() -> None:
    ch_mod.set_query_tag("memory.search 123e4567")
    assert ch_mod.query_settings() == {"log_comment": "memory.search 123e4567"}
    merged = ch_mod.query_settings({"async_insert": 0, "wait_for_async_insert": 1})
    assert merged == {
        "async_insert": 0,
        "wait_for_async_insert": 1,
        "log_comment": "memory.search 123e4567",
    }


def test_explicit_log_comment_wins_and_tag_is_bounded() -> None:
    ch_mod.set_query_tag("x" * 500)
    assert len(ch_mod.current_query_tag()) == ch_mod._QUERY_TAG_MAX
    assert ch_mod.query_settings({"log_comment": "mine"})["log_comment"] == "mine"


class _FakeAsync:
    """Stands in for clickhouse_connect's AsyncClient; records settings."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def query(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
        self.calls.append(("query", settings))

        class _R:
            column_names = ["a"]
            result_rows = [[1]]

        return _R()

    async def command(self, sql: str, parameters: Any = None, settings: Any = None) -> None:
        self.calls.append(("command", settings))

    async def insert(
        self,
        table: str,
        data: Any,
        column_names: Any = None,
        column_type_names: Any = None,
        settings: Any = None,
    ) -> None:
        self.calls.append(("insert", settings))


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[ch_mod.CHClient, _FakeAsync]:
    fake = _FakeAsync()
    c = ch_mod.CHClient()
    monkeypatch.setattr(type(c), "client", property(lambda self: fake))
    return c, fake


@pytest.mark.asyncio
async def test_every_client_call_carries_the_tag(
    client: tuple[ch_mod.CHClient, _FakeAsync],
) -> None:
    c, fake = client
    ch_mod.set_query_tag("memory.ask req-1")
    await c.query_one("SELECT 1")
    await c.query_all("SELECT 1")
    await c.execute("ALTER TABLE t UPDATE x = 1 WHERE 1")
    await c.insert("loom.t", [{"a": 1}], settings={"async_insert": 0})
    kinds = [k for k, _ in fake.calls]
    # The insert is preceded by the one-per-table DESCRIBE,
    # which carries the tag like every other statement.
    assert kinds == ["query", "query", "command", "query", "insert"]
    for kind, settings in fake.calls:
        assert settings["log_comment"] == "memory.ask req-1", kind
    assert fake.calls[-1][1]["async_insert"] == 0  # caller settings survive the merge


@pytest.mark.asyncio
async def test_untagged_calls_send_no_log_comment(
    client: tuple[ch_mod.CHClient, _FakeAsync],
) -> None:
    c, fake = client
    await c.query_all("SELECT 1")
    await c.execute("SELECT 1")
    await c.insert("loom.t", [{"a": 1}])
    # The only setting an untagged call carries is its class
    # deadline (read 30 s, write 60 s); never a log_comment.
    for _kind, settings in fake.calls:
        assert set(settings) <= {
            "max_execution_time",
            "insert_deduplicate",
            "insert_deduplication_token",
        }
        assert "log_comment" not in settings


def test_worker_identity_tags_the_pass() -> None:
    from server.workers.status import set_worker_identity

    set_worker_identity("usage_fold")
    assert ch_mod.current_query_tag() == "worker.usage_fold"


def test_tag_is_task_local() -> None:
    import asyncio

    async def _run() -> list[str]:
        seen: list[str] = []

        async def one(tag: str) -> None:
            ch_mod.set_query_tag(tag)
            await asyncio.sleep(0)
            seen.append(ch_mod.current_query_tag())

        await asyncio.gather(one("memory.search a"), one("memory.set b"))
        return seen

    assert sorted(asyncio.run(_run())) == ["memory.search a", "memory.set b"]


def test_query_tag_tags_the_block_only() -> None:
    ch_mod.set_query_tag("lifespan")
    with ch_mod.query_tag("instances.heartbeat"):
        assert ch_mod.current_query_tag() == "instances.heartbeat"
    assert ch_mod.current_query_tag() == "lifespan"
