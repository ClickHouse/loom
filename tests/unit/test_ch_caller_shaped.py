"""Caller-shaped reads run under ceilings a request cannot widen, and every
query of a request carries the verified caller's quota key."""

from __future__ import annotations

import contextvars
from typing import Any

import pytest

import server.core.ch as ch
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.core.errors import ValidationError
from server.operations.base import Role
from server.settings import settings

_SELECT = "SELECT path FROM loom.code_chunks WHERE match(content, {p:String})"


@pytest.fixture(autouse=True)
def _ceilings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ch_caller_shaped_max_execution_s", 30.0)
    monkeypatch.setattr(settings, "ch_caller_shaped_max_memory_bytes", 4_000_000_000)
    monkeypatch.setattr(settings, "ch_caller_shaped_max_rows_to_read", 500_000_000)


def _isolated(fn: Any) -> Any:
    """Run `fn` in a copy of the context, so a context var it sets does not leak."""
    return contextvars.copy_context().run(fn)


def _caller_shaped_settings(sql: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    with ch.caller_shaped():
        return ch.statement_settings(sql, extra)


def test_a_caller_shaped_read_carries_the_ceilings() -> None:
    out = _caller_shaped_settings(_SELECT)
    assert out["max_execution_time"] == 30
    assert out["max_memory_usage"] == 4_000_000_000
    assert out["max_rows_to_read"] == 500_000_000
    assert out["read_overflow_mode"] == "throw"


def test_a_per_call_value_cannot_widen_a_ceiling_but_may_lower_it() -> None:
    wider = {"max_execution_time": 3600, "max_memory_usage": 0, "max_rows_to_read": 10**12}
    out = _caller_shaped_settings(_SELECT, {**wider, "read_overflow_mode": "break"})
    assert out["max_execution_time"] == 30
    assert out["max_memory_usage"] == 4_000_000_000  # 0 is unlimited in ClickHouse
    assert out["max_rows_to_read"] == 500_000_000
    assert out["read_overflow_mode"] == "throw"
    assert _caller_shaped_settings(_SELECT, {"max_execution_time": 2})["max_execution_time"] == 2


def test_a_statement_timeout_scope_cannot_widen_it() -> None:
    with ch.statement_timeout(3600):
        assert _caller_shaped_settings(_SELECT)["max_execution_time"] == 30


def test_reads_outside_the_scope_and_writes_inside_it_are_untouched() -> None:
    out = ch.statement_settings(_SELECT)
    assert "max_rows_to_read" not in out and "max_memory_usage" not in out
    insert = _caller_shaped_settings("INSERT INTO loom.code_traces VALUES")
    assert "max_rows_to_read" not in insert and "read_overflow_mode" not in insert


def test_a_row_cap_hit_inside_the_scope_is_a_422() -> None:
    err = RuntimeError("Code: 158. DB::Exception: Limit for rows exceeded")
    with ch.caller_shaped(), pytest.raises(ValidationError, match="narrow it"):
        ch._raise_if_caller_cap(err)
    ch._raise_if_caller_cap(err)  # outside the scope: left to the caller as before


def _principal(org: str, subject: str) -> AuthInfo:
    return AuthInfo(
        subject=subject, org=org, namespaces=("n",), roles=(Role.VIEWER,), is_service_account=False
    )


def test_every_query_of_a_request_carries_the_tokens_quota_key() -> None:
    def _bound() -> tuple[dict[str, Any], dict[str, Any]]:
        set_current_auth(_principal("acme", "alice"))
        # a body-derived or per-call value never replaces the token's key
        return ch.statement_settings(_SELECT, {"quota_key": "victim:bob"}), ch.query_settings()

    read, insert = _isolated(_bound)
    assert read["quota_key"] == "acme:alice"
    assert insert["quota_key"] == "acme:alice"


def test_no_principal_no_quota_key() -> None:
    def _anonymous() -> dict[str, Any]:
        set_current_auth(_principal("acme", "alice"))
        set_current_auth(None)
        return ch.statement_settings(_SELECT)

    assert "quota_key" not in _isolated(_anonymous)


class _ScopeSpy:
    """A ClickHouse client that records whether each read ran caller-shaped."""

    def __init__(self) -> None:
        self.shaped: list[bool] = []

    async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        self.shaped.append(ch.caller_shaped_var.get())
        return []


@pytest.mark.asyncio
async def test_memory_aggregate_and_record_query_read_caller_shaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_records import MemoryRecordQueryInput
    from server.memory.api.models_search import MemoryAggregateInput
    from server.memory.app import query as query_mod
    from server.memory.app import records as records_mod

    spy = _ScopeSpy()
    monkeypatch.setattr(query_mod, "get_client", lambda: spy)
    monkeypatch.setattr(records_mod, "get_client", lambda: spy)
    await query_mod.handle_memory_aggregate(MemoryAggregateInput(org="o", namespace="n", agent="a"))
    await records_mod.handle_memory_record_query(
        MemoryRecordQueryInput(org="o", namespace="n", schema_name="s")
    )
    assert spy.shaped == [True, True]
    assert ch.caller_shaped_var.get() is False  # nothing leaks out of the handler


@pytest.mark.asyncio
async def test_code_grep_and_a_patterned_code_search_run_caller_shaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.api.models_grep import CodeGrepInput
    from server.code.api.models_search import CodeSearchInput
    from server.code.app import grep as grep_mod
    from server.code.app.search import handler as search_mod

    seen: list[bool] = []

    async def _record(_input: Any) -> Any:
        seen.append(ch.caller_shaped_var.get())

    monkeypatch.setattr(grep_mod, "_code_grep", _record)
    monkeypatch.setattr(search_mod, "_code_search", _record)
    await grep_mod.handle_code_grep(CodeGrepInput(pattern="ab"))
    await search_mod.handle_code_search(CodeSearchInput(query="q", pattern="ab"))
    await search_mod.handle_code_search(CodeSearchInput(query="q", path_globs=["src/**"]))
    await search_mod.handle_code_search(CodeSearchInput(query="q"))
    assert seen == [True, True, True, False]
