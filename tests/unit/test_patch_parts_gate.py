"""The main fact leg carries the codes flag only while
loom.memory_embeddings is patch-free, and the patch-part count is a gauge."""

from __future__ import annotations

import pytest

from server.core.vectors import patch_parts as P
from server.core.vectors.quantized_setting import set_quantized_codes_supported
from server.core.vectors.search_settings import vector_search_settings
from server.memory.app.retrieval import vector_search
from tests.unit.test_memory_search_service import StubCH, _q


@pytest.fixture(autouse=True)
def _fresh():
    P.reset()
    set_quantized_codes_supported(True)
    yield
    P.reset()
    set_quantized_codes_supported(None)


class _CH(StubCH):
    def __init__(self, patch_parts=0, fail=False):
        super().__init__()
        self.patch_parts = patch_parts
        self.fail = fail
        self.probes = 0

    async def query_all(self, sql, params=None):
        if "system.parts" in sql:
            self.probes += 1
            if self.fail:
                raise RuntimeError("Code: 497. Not enough privileges")
            assert params == {"db": "loom", "t": "memory_embeddings"}
            return [{"patch_parts": self.patch_parts}]
        return await super().query_all(sql, params)


def test_probe_statement_counts_active_patch_parts_of_one_table() -> None:
    assert "startsWith(name, 'patch')" in P.PATCH_PARTS_SQL
    assert "system.parts" in P.PATCH_PARTS_SQL and "AND active" in P.PATCH_PARTS_SQL
    assert "{db:String}" in P.PATCH_PARTS_SQL and "{t:String}" in P.PATCH_PARTS_SQL


@pytest.mark.asyncio
async def test_patch_free_only_on_a_zero_reading_and_a_failed_probe_is_not_free() -> None:
    assert await P.patch_free(_CH(0), ttl_seconds=60) is True
    P.reset()
    assert await P.patch_free(_CH(5), ttl_seconds=60) is False
    assert P.last_reading() == 5
    P.reset()
    assert await P.patch_free(_CH(fail=True), ttl_seconds=60) is False
    assert P.last_reading() is None


@pytest.mark.asyncio
async def test_probe_is_cached_for_the_ttl(monkeypatch) -> None:
    ch = _CH(0)
    clock = [1000.0]
    monkeypatch.setattr(P.time, "monotonic", lambda: clock[0])
    assert await P.patch_free(ch, ttl_seconds=60) and ch.probes == 1
    clock[0] += 30
    assert await P.patch_free(ch, ttl_seconds=60) and ch.probes == 1  # cached
    ch.patch_parts = 3
    clock[0] += 31
    assert await P.patch_free(ch, ttl_seconds=60) is False and ch.probes == 2  # refreshed


def test_mode_parsing() -> None:
    assert P.codes_first_mode(None) == "auto" and P.codes_first_mode("AUTO") == "auto"
    assert P.codes_first_mode("on") == "on" and P.codes_first_mode(True) == "on"
    assert P.codes_first_mode("off") == "off" and P.codes_first_mode(False) == "off"
    assert P.codes_first_mode("garbage") == "auto"


class _S:
    vector_search_quantized_codes = True
    memory_search_fact_codes_first = "auto"
    memory_embeddings_patch_parts_probe_seconds = 60


@pytest.mark.asyncio
async def test_fact_codes_first_follows_the_mode_and_the_probe() -> None:
    s = _S()
    assert await P.fact_codes_first(_CH(0), settings_obj=s) is True
    P.reset()
    assert await P.fact_codes_first(_CH(2), settings_obj=s) is False
    s.memory_search_fact_codes_first = "on"
    assert await P.fact_codes_first(_CH(2), settings_obj=s) is True  # pinned on
    s.memory_search_fact_codes_first = "off"
    P.reset()
    assert await P.fact_codes_first(_CH(0), settings_obj=s) is False  # pinned off
    s.memory_search_fact_codes_first = "on"
    set_quantized_codes_supported(False)
    assert await P.fact_codes_first(_CH(0), settings_obj=s) is False  # server lacks the name
    set_quantized_codes_supported(True)
    s.vector_search_quantized_codes = False
    assert await P.fact_codes_first(_CH(0), settings_obj=s) is False  # deployment flag off


def test_vector_search_settings_quantized_override() -> None:
    assert "vector_search_use_quantized_codes = 1" in vector_search_settings()
    off = vector_search_settings(quantized=False, fetch_multiplier=10.0)
    assert "vector_search_use_quantized_codes" not in off
    assert "vector_search_index_fetch_multiplier = 10.0" in off
    on = vector_search_settings(quantized=True, fetch_multiplier=1.0)
    assert on == (
        "SETTINGS vector_search_index_fetch_multiplier = 1.0, vector_search_use_quantized_codes = 1"
    )
    set_quantized_codes_supported(False)
    assert "vector_search_use_quantized_codes" not in vector_search_settings(quantized=True)


def _fact_sql(ch: StubCH) -> str:
    return next(s for s, _ in ch.query_all_calls if "FROM loom.memory_embeddings AS e FINAL" in s)


def _settings_tail(sql: str) -> str:
    return sql.rsplit("SETTINGS", 1)[1]


@pytest.mark.asyncio
async def test_main_leg_carries_the_codes_flag_only_on_a_patch_free_table() -> None:
    ch = _CH(0)
    await vector_search(_q(execution_planes=["fact"]), ch=ch)
    tail = _settings_tail(_fact_sql(ch))
    assert "vector_search_use_quantized_codes = 1" in tail
    assert (
        "vector_search_index_fetch_multiplier = 3.0" in tail
    )  # the fleet's x3; x1 failed identity
    # The thread pin is a setting, off by default.
    assert "max_threads" not in tail and "query_plan_direct_read_from_text_index = 0" in tail

    P.reset()
    ch = _CH(5)
    await vector_search(_q(execution_planes=["fact"]), ch=ch)
    tail = _settings_tail(_fact_sql(ch))
    assert "vector_search_use_quantized_codes" not in tail  # patch parts: the codes-free fragment
    assert "vector_search_index_fetch_multiplier = 3.0" in tail

    P.reset()
    ch = _CH(fail=True)
    await vector_search(_q(execution_planes=["fact"]), ch=ch)
    assert "vector_search_use_quantized_codes" not in _settings_tail(_fact_sql(ch))


@pytest.mark.asyncio
async def test_main_leg_pinned_off_never_probes(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_fact_codes_first", "off")
    ch = _CH(0)
    await vector_search(_q(execution_planes=["fact"]), ch=ch)
    assert ch.probes == 0
    assert "vector_search_use_quantized_codes" not in _settings_tail(_fact_sql(ch))
