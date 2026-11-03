"""An exploration rate withholds the top procedure and says so."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS
from typing import Any

import pytest

from server.memory.api.models_context import ContextPackInput, ContextPackOutput
from server.memory.app.context_pack import handle_context_pack
from server.observability.event_args import project_result


def _matches() -> Any:
    return NS(
        matches=[
            NS(
                steps=["a", "b"],
                namespace="ns",
                memory_key="procedure_top",
                name="top",
                situation="release",
                status="exercised",
                fail_prone_steps=[],
            ),
            NS(
                steps=["c", "d"],
                namespace="ns",
                memory_key="procedure_second",
                name="second",
                situation="release",
                status="authored",
                fail_prone_steps=[],
            ),
        ]
    )


def _wire(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _match(inp: Any) -> Any:
        return _matches()

    monkeypatch.setattr("server.memory.app.procedure.handle_procedure_match", _match)


def _pack(**kw: Any) -> ContextPackOutput:
    return asyncio.run(
        handle_context_pack(
            ContextPackInput(
                org="o",
                namespace="ns",
                agent="a",
                query="ship the release",
                situation="release",
                include_knowledge=False,
                only_sections=["procedures"],
                **kw,
            )
        )
    )


def test_rate_one_withholds_the_top_match_and_names_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch)
    out = _pack(procedure_explore_rate=1.0)
    assert out.procedure_keys_withheld == ["procedure_top"]
    assert out.procedure_keys == ["procedure_second"]
    assert "[ns/top]" not in out.sections["procedures"]
    assert "[ns/second by=unknown]" in out.sections["procedures"]
    meta = out.section_meta["procedures"]
    assert meta["explore"] is True and meta["withheld"] == ["procedure_top"]
    assert meta["source_count"] == 1


def test_rate_zero_is_the_default_and_shows_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch)
    out = _pack()
    assert out.procedure_keys == ["procedure_top", "procedure_second"]
    assert out.procedure_keys_withheld == []
    assert "explore" not in out.section_meta["procedures"]
    out0 = _pack(procedure_explore_rate=0.0)
    assert out0.procedure_keys_withheld == []


def test_the_draw_is_deterministic_per_session_and_situation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app.context_pack import _explore_this_pack

    def _inp(session: str, situation: str = "release") -> ContextPackInput:
        return ContextPackInput(
            org="o",
            namespace="ns",
            agent="a",
            query="q",
            situation=situation,
            session_id=session,
            procedure_explore_rate=0.5,
        )

    a = _explore_this_pack(_inp("s1"), "release")
    assert a == _explore_this_pack(_inp("s1"), "release")
    draws = [_explore_this_pack(_inp(f"s{i}"), "release") for i in range(400)]
    share = sum(draws) / len(draws)
    assert 0.4 < share < 0.6  # the rate holds across sessions


def test_withholding_the_only_match_leaves_no_section_but_a_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _one(inp: Any) -> Any:
        return NS(matches=[_matches().matches[0]])

    monkeypatch.setattr("server.memory.app.procedure.handle_procedure_match", _one)
    out = _pack(procedure_explore_rate=1.0)
    assert "procedures" not in out.sections
    assert out.procedure_keys == [] and out.procedure_keys_withheld == ["procedure_top"]


def test_withheld_keys_ride_the_event() -> None:
    out = ContextPackOutput(
        rendered="",
        sections={},
        section_count=0,
        token_budget=10,
        procedure_keys=["a"],
        procedure_keys_withheld=["b", "c"],
    )
    projected = project_result(out)
    assert projected["out_procedure_keys"] == "a"
    assert projected["out_procedure_keys_withheld"] == "b,c"
