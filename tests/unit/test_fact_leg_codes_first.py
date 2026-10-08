"""The main fact statement's thread pin is a setting. At the
default (pin 0) the main fact statement is one statement under FINAL with
no `max_threads` line; pin 4 adds only the pin line."""

from __future__ import annotations

import pytest

from server.core.vectors.quantized_setting import set_quantized_codes_supported
from server.memory.app.retrieval import vector_search
from tests.unit.test_memory_search_service import StubCH, _hit, _q


@pytest.fixture(autouse=True)
def _pinned(monkeypatch: pytest.MonkeyPatch):
    from server.core.vectors import patch_parts
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_key_axis", "off")
    monkeypatch.setattr(settings, "memory_search_fact_codes_first", "off", raising=False)
    set_quantized_codes_supported(True)
    patch_parts.reset()
    yield
    set_quantized_codes_supported(None)
    patch_parts.reset()


def _fact_calls(ch: StubCH) -> list[tuple[str, dict]]:
    return [c for c in ch.query_all_calls if "loom.memory_embeddings AS e" in c[0]]


@pytest.mark.asyncio
async def test_defaults_render_one_unpinned_final_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defaults: the thread pin off. One FINAL
    statement, no quantized stage 1, no identity restriction, no
    `max_threads`; with the pin set to 4 the statement differs only by the
    pin line."""
    from server.settings import settings

    assert settings.memory_search_fact_leg_max_threads == 0
    ch = StubCH()
    ch.query_all_returns.append([_hit()])
    await vector_search(_q(search_mode="semantic", execution_planes=["fact"]), ch=ch)
    calls = _fact_calls(ch)
    assert len(calls) == 1
    sql, params = calls[0]
    assert "FROM loom.memory_embeddings AS e FINAL" in sql
    assert "arrayZip" not in sql and "stage1_limit" not in params
    assert "max_threads" not in sql
    assert "vector_search_use_quantized_codes" not in sql  # codes mode pinned off above
    # Pin 4: the same text, the only difference being the pin line.
    monkeypatch.setattr(settings, "memory_search_fact_leg_max_threads", 4, raising=False)
    ch2 = StubCH()
    ch2.query_all_returns.append([_hit()])
    await vector_search(_q(search_mode="semantic", execution_planes=["fact"]), ch=ch2)
    sql4 = _fact_calls(ch2)[0][0]
    assert sql4.count("max_threads = 4,") == 1
    assert sql4.replace("max_threads = 4,\n            ", "") == sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pin", "expect"), [(4, "max_threads = 4,"), (8, "max_threads = 8,"), (0, None)]
)
async def test_main_statement_thread_pin_follows_the_setting(
    monkeypatch: pytest.MonkeyPatch, pin: int, expect: str | None
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_fact_leg_max_threads", pin, raising=False)
    ch = StubCH()
    ch.query_all_returns.append([_hit()])
    await vector_search(_q(search_mode="semantic", execution_planes=["fact"]), ch=ch)
    ((sql, _),) = _fact_calls(ch)
    if expect is None:
        assert "max_threads" not in sql
    else:
        assert sql.count(expect) == 1
    # The rest of the SETTINGS list is untouched.
    assert (
        "query_plan_direct_read_from_text_index = 0" in sql and "optimize_read_in_order = 1" in sql
    )
