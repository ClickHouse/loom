"""Reconnect metadata stays scalar while the statement retains its work charge."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from clickhouse_connect.driver.exceptions import OperationalError

from server.core import ch as ch_mod
from server.core.schema.vector_codec_ddl import CODEC_SETTINGS_SQL
from server.core.vectors import admission, codes_state, quantized_setting
from server.settings import settings
from tests.unit.codec_states import dict_rows

_REAL_PROBE = ch_mod._probe_vector_state
_REAL_CONDITION_PROBE = ch_mod._turn_off_unsafe_condition_cache
SQL = """SELECT memory_key, content_excerpt,
cosineDistance(embedding, {q:Array(Float32)}) AS distance,
cosineDistance(memory_key_embedding, {q:Array(Float32)}) AS key_distance
FROM loom.memory_embeddings FINAL
WHERE org = {org:String} AND namespace = {ns:String}
ORDER BY distance ASC LIMIT 2"""
PARAMS = {"org": "one-org", "ns": "private", "q": [1.0, 0.0]}
ROWS = [
    {
        "memory_key": "one",
        "content_excerpt": "The complete first passage.",
        "distance": 0.1,
        "key_distance": 0.2,
    },
    {
        "memory_key": "two",
        "content_excerpt": "The complete second passage.\nIts next line remains.",
        "distance": 0.3,
        "key_distance": 0.4,
    },
]


def result(rows):
    names = list(rows[0]) if rows else []
    return SimpleNamespace(
        result_rows=[tuple(row[name] for name in names) for row in rows],
        column_names=names,
        summary={},
    )


@pytest.mark.parametrize("boot_failed", [True, False], ids=["failed-advisory-boot", "healthy-boot"])
@pytest.mark.parametrize("vector", [True, False], ids=["weight-two", "scalar-control"])
async def test_reconnect_metadata_does_not_reenter_its_outer_org_charge(
    monkeypatch, boot_failed, vector
):
    for name, value in {
        "ch_pool_maxsize": 2,
        "ch_pool_wait_ms": 5000,
        "vector_scan_max_concurrent_per_org": 2,
        "vector_scan_max_waiters_per_org": 2,
    }.items():
        monkeypatch.setattr(settings, name, value)
    gate = admission._Admission()
    monkeypatch.setattr(admission, "_admission", gate)
    monkeypatch.setattr(ch_mod, "_probe_vector_state", _REAL_PROBE)
    monkeypatch.setattr(ch_mod, "_turn_off_unsafe_condition_cache", _REAL_CONDITION_PROBE)

    async def schema_ready(*args, **kwargs):
        return None

    monkeypatch.setattr(ch_mod, "guard_pending_schema", schema_ready)
    # The real support probe returns false, so no read-triggered refresh can
    # repair failed boot state before reconnect gets the chance to retry it.
    quantized_setting.set_quantized_codes_supported(None)
    replacement_created, metadata_started, release_metadata = (asyncio.Event() for _ in range(3))
    native_calls, metadata_calls, factory_work, version_calls = [], [], [], []
    transient = OperationalError("Network Error: Server disconnected")
    expected_work = admission.VectorWork("one-org", ("private",), 2) if vector else None
    outer_charge = 2 if vector else 0

    def charged_snapshot():
        org = gate.orgs.get("one-org")
        return gate.population, (org.held, org.waiting, len(org.queue)) if org is not None else None

    def assert_outer_charge():
        assert charged_snapshot() == (outer_charge, (2, 0, 0) if vector else None)

    class NativeDriver:
        def __init__(self, generation):
            self.generation, self.closed, self.vector_calls = generation, False, 0

        async def query(self, sql, parameters=None, settings=None, **kwargs):
            parameters, native_settings = dict(parameters or {}), dict(settings or {})
            work = admission.current_work()
            if native_settings.get("log_comment") == "ch.condition_cache_probe":
                assert "version" in sql and parameters == {}
                assert native_settings == {"log_comment": "ch.condition_cache_probe"}
                version_calls.append((self.generation, work, charged_snapshot()))
                assert work == (expected_work if self.generation == 1 else None)
                return result([{"version": "26.8.1.1"}])
            if sql == SQL:
                self.vector_calls += 1
                native_calls.append((self.generation, sql, parameters, native_settings, work))
                assert work == expected_work
                assert parameters == PARAMS
                assert_outer_charge()
                if vector:
                    assert (
                        native_settings["max_rows_to_read"]
                        == settings_global.vector_scan_max_rows_to_read
                    )
                    assert native_settings["read_overflow_mode"] == "throw"
                else:
                    assert "max_rows_to_read" not in native_settings
                if self.generation == 0:
                    raise transient
                return result(ROWS)
            kind = (
                "codec"
                if sql == CODEC_SETTINGS_SQL
                else "columns"
                if sql == codes_state.CODED_COLUMNS_SQL
                else "support"
            )
            metadata_calls.append(
                (self.generation, kind, work, native_settings, charged_snapshot())
            )
            assert work is None
            assert "max_rows_to_read" not in native_settings
            if kind == "support":
                assert self.generation == 0
                assert "clusterAllReplicas" in sql
                assert parameters == {"n": "vector_search_use_quantized_codes"}
                return result([{"replicas": 1, "with_setting": 0}])
            if kind == "columns":
                assert parameters == {"db": "loom"}
            else:
                assert parameters == {}
            if self.generation == 0 and boot_failed:
                raise RuntimeError("initial advisory metadata read unavailable")
            if self.generation == 1:
                assert_outer_charge()
                metadata_started.set()
                await release_metadata.wait()
            if kind == "codec":
                return result(dict_rows(enable_quantized_codec="on"))
            assert kind == "columns"
            return result(
                [
                    {
                        "table": "memory_embeddings",
                        "name": "embedding",
                        "compression_codec": "CODEC(Quantized('rabitq', 2, 0))",
                        "patch_parts": 0,
                    }
                ]
            )

        async def close(self):
            self.closed = True

    # Keep the fake driver's native settings argument separate from the real settings.
    settings_global = settings
    drivers = []

    async def create_native(**kwargs):
        assert kwargs["connector_limit"] == 2 + ch_mod.STREAM_SLOTS
        assert kwargs["tz_source"] == "server"
        driver = NativeDriver(len(drivers))
        drivers.append(driver)
        factory_work.append(admission.current_work())
        if driver.generation == 1:
            assert_outer_charge()
            replacement_created.set()
        return driver

    monkeypatch.setattr(ch_mod.clickhouse_connect, "get_async_client", create_native)
    ch = ch_mod.CHClient()
    caller = None
    try:
        await ch.connect()
        assert quantized_setting.quantized_codes_state() is False
        assert (codes_state._state.probed_at is None) is boot_failed
        assert (codes_state._state.codec_available is None) is boot_failed
        assert [(g, kind) for g, kind, *_ in metadata_calls] == [
            (0, "support"),
            (0, "codec"),
            (0, "columns"),
        ]

        async def read():
            with admission.vector_work("one-org", "private", 2 if vector else 0):
                rows = await ch.query_all(SQL, PARAMS)
                assert admission.current_work() == expected_work
                return rows

        caller = asyncio.create_task(read())
        await asyncio.wait_for(replacement_created.wait(), 1)
        if boot_failed:
            # The factory and first metadata query run in the same task without
            # a yield. It can suspend at this driver gate only after admission.
            assert metadata_started.is_set(), (
                "reconnect metadata queued behind its own vector charge",
                charged_snapshot(),
            )
            assert not caller.done()
            assert_outer_charge()
            release_metadata.set()
        else:
            assert not metadata_started.is_set()
        assert await asyncio.wait_for(asyncio.shield(caller), 1) == ROWS
        assert factory_work == [None, expected_work]
        assert version_calls == [
            (0, None, (0, None)),
            (1, expected_work, (outer_charge, (2, 0, 0) if vector else None)),
        ]
        assert [(g, kind) for g, kind, *_ in metadata_calls] == [
            (0, "support"),
            (0, "codec"),
            (0, "columns"),
        ] + ([(1, "codec"), (1, "columns")] if boot_failed else [])
        assert all(work is None for _, _, work, _, _ in metadata_calls)
        assert (
            codes_state._state.probed_at is not None and codes_state._state.codec_available is True
        )
        assert codes_state.stored_width("memory_embeddings", "embedding") == 2
        assert codes_state._state.refresh is None
        assert len(native_calls) == 2
        assert native_calls[0][1:3] == native_calls[1][1:3] == (SQL, PARAMS)
        assert [call[4] for call in native_calls] == [expected_work, expected_work]
        assert ch.generation == 1 and drivers[0].closed
        assert gate.population == 0 and gate.orgs == {}
        assert ch._inflight == {} and ch._retired == {}
        assert ch._slots is not None and ch._slots._value == 2
    finally:
        release_metadata.set()
        if caller is not None and not caller.done():
            caller.cancel()
        if caller is not None:
            await asyncio.wait_for(asyncio.gather(caller, return_exceptions=True), 1)
        await ch.disconnect()
        for driver in drivers:
            if not driver.closed:
                await driver.close()
        quantized_setting.set_quantized_codes_supported(None)
        assert all(driver.closed for driver in drivers)
        assert gate.population == 0 and gate.orgs == {}
        assert admission.current_work() is None
