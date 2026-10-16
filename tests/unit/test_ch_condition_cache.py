"""The query condition cache guard in server/core/ch.py: every Loom session
runs with the cache off when any replica predates ClickHouse's vector-read fix."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# The conftest stubs it for fake clients; this file tests the real one.
from server.core.ch import _turn_off_unsafe_condition_cache as _real_cache_guard
from server.core.ch import condition_cache_unsafe


def test_the_condition_cache_is_unsafe_only_below_26_8() -> None:
    assert condition_cache_unsafe("26.6.1.10")
    assert condition_cache_unsafe("26.4.1.2509")
    assert not condition_cache_unsafe("26.8.1.187")
    assert not condition_cache_unsafe("26.10.1.1")


@pytest.mark.parametrize(
    ("replicas", "turned_off"),
    [(["26.8.1.1", "26.4.1.2509"], True), (["26.10.1.1"], False), (None, True)],
)
@pytest.mark.asyncio
async def test_a_session_turns_the_condition_cache_off_when_any_replica_is_old(
    replicas: list[str] | None, turned_off: bool
) -> None:
    class _Client:
        def __init__(self) -> None:
            self.settings: dict[str, object] = {}
            self.tags: set[object] = set()

        async def query(self, sql: str, settings: dict[str, object]) -> SimpleNamespace:
            self.tags.add(settings.get("log_comment"))
            if "clusterAllReplicas" in sql:
                if replicas is None:
                    raise RuntimeError("Code: 701. Requested cluster 'default' not found")
                return SimpleNamespace(result_rows=[(v,) for v in replicas])
            return SimpleNamespace(result_rows=[("26.5.1.882",)])

        def set_client_setting(self, key: str, value: object) -> None:
            self.settings[key] = value

    client = _Client()
    await _real_cache_guard(client)  # type: ignore[arg-type]
    assert (client.settings == {"use_query_condition_cache": 0}) is turned_off
    assert client.tags == {"ch.condition_cache_probe"}
