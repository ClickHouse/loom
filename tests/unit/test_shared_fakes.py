"""The shared fakes keep the real clients' contracts: same call signatures, a
loud failure on an unexpected call, and a record a test can assert on."""

from __future__ import annotations

import inspect
import re

import pytest
from clickhouse_connect.driver.external import ExternalData

from server.core.ch import CHClient
from server.llm.client import LLMClient, LLMResult
from tests.unit.ch_fake import RecordingCH
from tests.unit.llm_fake import ScriptedLLM


@pytest.mark.parametrize("method", ["query_one", "query_all", "execute", "insert"])
def test_recording_ch_accepts_every_parameter_the_real_client_takes(method: str) -> None:
    real = inspect.signature(getattr(CHClient, method)).parameters
    fake = inspect.signature(getattr(RecordingCH, method)).parameters
    assert set(real) - {"self"} <= set(fake), f"{method} lost a parameter the server passes"


async def test_reads_are_answered_by_the_first_live_route() -> None:
    ch = (
        RecordingCH()
        .on("FROM loom.memory", [{"k": 1}], times=1)
        .on("FROM loom.memory", [{"k": 2}])
        .on(re.compile(r"count\(\)"), lambda sql, params: [{"n": params["lim"]}])
    )
    assert await ch.query_all("SELECT * FROM loom.memory") == [{"k": 1}]
    assert await ch.query_one("SELECT * FROM loom.memory FINAL") == {"k": 2}
    assert await ch.query_one("SELECT count() FROM t", {"lim": 3}) == {"n": 3}
    assert [s.kind for s in ch.statements("loom.memory")] == ["query_all", "query_one"]
    assert ch.unused_routes() == []


async def test_an_unrouted_read_fails_with_the_statement() -> None:
    ch = RecordingCH().on("FROM loom.memory", [])
    with pytest.raises(AssertionError, match="unrouted query_all: 'SELECT 1 FROM loom.facts'"):
        await ch.query_all("SELECT 1\n  FROM loom.facts")
    assert await RecordingCH(strict=False).query_one("SELECT 1") is None


async def test_answers_are_copies() -> None:
    rows = [{"k": 1}]
    ch = RecordingCH().on("x", rows)
    (await ch.query_all("x"))[0]["k"] = 99
    assert rows == [{"k": 1}]


async def test_inserts_and_executes_are_recorded() -> None:
    ch = RecordingCH()
    row = {"memory_key": "k"}
    await ch.insert("loom.memory", [row])
    row["memory_key"] = "mutated after the call"
    await ch.execute(
        "ALTER TABLE loom.memory DELETE WHERE 1", {"a": 1}, settings={"mutations_sync": 2}
    )
    assert ch.inserted("memory") == [{"memory_key": "k"}]
    assert ch.statements("ALTER", kind="execute")[0].params == {"a": 1}


async def test_external_data_is_recorded_with_its_execute() -> None:
    ch = RecordingCH()
    payload = ExternalData(
        file_name="updates", data=b'{"n":1}\n', fmt="JSONEachRow", structure=["n UInt64"]
    )
    await ch.execute("INSERT INTO t SELECT * FROM updates", external_data=payload)
    assert ch.statements("INSERT", kind="execute")[0].external_data is payload


async def test_install_serves_get_client(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.core.ch

    ch = RecordingCH().install(monkeypatch)
    assert server.core.ch.get_client() is ch


def test_scripted_llm_is_an_llm_client() -> None:
    llm: LLMClient = ScriptedLLM(["ok"])
    assert llm.provider == "fake"


async def test_scripted_llm_replies_in_order_and_raises_scripted_faults() -> None:
    llm = ScriptedLLM(["one", TimeoutError("provider timed out"), "three"])
    first = await llm.complete("sys", "hello world", max_tokens=5)
    assert isinstance(first, LLMResult) and first.text == "one" and first.tokens_in == 3
    with pytest.raises(TimeoutError):
        await llm.complete("sys", "again")
    assert (await llm.complete("sys", "x", response_format={"type": "json_object"})).text == "three"
    assert llm.calls[2].response_format == {"type": "json_object"}
    with pytest.raises(AssertionError, match="unscripted LLM call #4"):
        await llm.complete("sys", "one too many")


async def test_scripted_llm_can_repeat_its_last_reply() -> None:
    llm = ScriptedLLM(["same"], repeat_last=True)
    assert [(await llm.complete("s", "u")).text for _ in range(3)] == ["same"] * 3
