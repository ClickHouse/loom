"""The extraction coverage sweep is the namespace's choice."""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app import coverage_sweep_gate as gate

ROOT = Path(__file__).resolve().parents[2]


def test_effective_takes_the_namespace_answer_else_density() -> None:
    s = SimpleNamespace(memory_extract_coverage_sweep_auto_min_statements=10)
    assert gate.effective(None, s) is False  # no messages, no dense turn
    assert gate.effective(True, s) is True  # a namespace opts in
    assert gate.effective(False, s) is False  # and opts out


class _Client:
    def __init__(self, rows: list[dict[str, Any] | None], fail: bool = False) -> None:
        self.rows = rows
        self.fail = fail
        self.calls = 0

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("clickhouse down")
        return self.rows.pop(0) if self.rows else None


@pytest.mark.asyncio
async def test_resolve_is_tri_state_cached_and_invalidated(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.core.ch as ch

    gate.invalidate()
    client = _Client(
        [{"coverage_sweep": True}, {"coverage_sweep": False}, {"coverage_sweep": None}, None]
    )
    monkeypatch.setattr(ch, "get_client", lambda: client)

    assert await gate.resolve("o", "dense") is True
    assert await gate.resolve("o", "dense") is True  # cached: no second read
    assert client.calls == 1
    gate.invalidate("o", "dense")
    assert await gate.resolve("o", "dense") is False  # re-read after invalidation
    assert await gate.resolve("o", "undecided") is None  # NULL inherits
    assert await gate.resolve("o", "unregistered") is None  # no row inherits
    assert await gate.resolve("", "x") is None and client.calls == 4


@pytest.mark.asyncio
async def test_resolve_fails_open_to_inherit(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.core.ch as ch

    gate.invalidate()
    monkeypatch.setattr(ch, "get_client", lambda: _Client([], fail=True))
    assert await gate.resolve("o", "ns") is None


def test_extractor_and_pipeline_thread_the_namespace_switch() -> None:
    from server.memory.app import extract as memory_extract
    from server.memory.app import pipeline as memory_pipeline

    src = inspect.getsource(memory_extract.extract_from_messages)
    assert "coverage_sweep: bool | None = None" in src
    assert "_sweep_effective(coverage_sweep, settings, messages)" in src
    pipe = inspect.getsource(memory_pipeline._step_llm_extract)
    assert "_resolve_sweep(state.org, state.namespace)" in pipe
    assert "coverage_sweep=sweep_override" in pipe


def test_namespace_surface_carries_the_switch() -> None:
    from server.identity.api.models import NamespaceGetOutput, NamespaceRegisterInput

    assert NamespaceRegisterInput.model_fields["coverage_sweep"].default is None
    assert NamespaceGetOutput.model_fields["coverage_sweep"].default is None
    # Preserve-on-update: an unrelated settings write keeps the stored answer
    # (test_namespace_register_preserve pins the explicit-null reset).
    from datetime import datetime

    from server.identity.app.namespace import registry_row

    unrelated = NamespaceRegisterInput(org="o", namespace="n", owner_email="o@x")
    stored = {"coverage_sweep": True, "conversation_logging_enabled": False}
    row = registry_row(unrelated, stored, datetime(2026, 9, 28))
    assert row["coverage_sweep"] is True
    handler = (ROOT / "server" / "identity" / "app" / "namespace.py").read_text()
    assert handler.count("coverage_sweep,") >= 2  # list, get
    assert "_sweep_gate.invalidate(input.org, input.namespace)" in handler
    schema = (ROOT / "schemas" / "schema.sql").read_text()
    assert "`coverage_sweep` Nullable(Bool) DEFAULT NULL" in schema


# The density gate.
_LIST = "Please memorize it. " + " ".join(
    f"{i}. The capital of country number {i} is city number {i}." for i in range(1, 41)
)
_CHAT = [
    {
        "role": "user",
        "content": "I signed up for pottery yesterday! The studio is downtown. My sister came along and loved it.",
    },
    {"role": "assistant", "content": "That sounds like a lovely day."},
    {
        "role": "user",
        "content": "It was. We might go again next month, and maybe try the glazing class too.",
    },
]


def test_density_gate_fires_on_a_statement_list_and_not_on_conversation() -> None:
    from server.memory.app.extract import statement_dense_turns

    s = SimpleNamespace(memory_extract_coverage_sweep_auto_min_statements=10)
    dense = [
        {"role": "user", "content": _LIST},
        {"role": "assistant", "content": "I have memorized it."},
    ]
    assert statement_dense_turns(dense, min_statements=10) == 1
    assert statement_dense_turns(_CHAT, min_statements=10) == 0
    # Prose with many sentences is not a list — a PR review or a code
    # discussion can meet the plain sentence count.
    prose = " ".join(
        f"The reviewer noted that the function in module {i} should handle the empty case before it allocates."
        for i in range(1, 16)
    )
    assert statement_dense_turns([{"role": "user", "content": prose}], min_statements=10) == 0
    bullets = "\n".join(
        f"- item number {i} of the shopping list for the trip" for i in range(1, 13)
    )
    assert statement_dense_turns([{"role": "user", "content": bullets}], min_statements=10) == 1
    assert gate.effective(None, s, dense) is True
    assert gate.effective(None, s, _CHAT) is False
    assert gate.effective(None, s, None) is False
    # An explicit answer still wins over density, both ways.
    assert gate.effective(False, s, dense) is False
    assert gate.effective(True, s, _CHAT) is True
    # The gate off (0) never fires.
    assert (
        gate.effective(
            None, SimpleNamespace(memory_extract_coverage_sweep_auto_min_statements=0), dense
        )
        is False
    )


def test_density_gate_default_and_extractor_pass_the_messages() -> None:
    from server.settings import Settings

    assert Settings.model_fields["memory_extract_coverage_sweep_auto_min_statements"].default == 10
    src = "".join(
        p.read_text() for p in sorted((ROOT / "server" / "memory" / "app" / "extract").glob("*.py"))
    )
    assert "_sweep_effective(coverage_sweep, settings, messages)" in src


def test_slim_sweep_prompt_keeps_the_live_schema_and_drops_the_examples() -> None:
    from server.memory.app.extract.sweep import (
        _COVERAGE_SWEEP_ADDENDUM,
        slim_sweep_system_prompt,
    )
    from server.memory.domain.extraction_prompts import _EXTRACTION_SYSTEM_PROMPT as full

    slim = slim_sweep_system_prompt(full)
    assert "OUTPUT SCHEMA" in slim and '"facts"' in slim and "source_message_indices" in slim
    assert "EXAMPLE 37" not in slim and "Input messages:" not in slim
    assert slim.endswith(_COVERAGE_SWEEP_ADDENDUM)
    assert len(slim) < 0.3 * len(full), (len(slim), len(full))
    # No schema header: the full prompt stands.
    assert slim_sweep_system_prompt("plain prompt") == "plain prompt" + _COVERAGE_SWEEP_ADDENDUM


def test_uncovered_statement_count_and_gate_reason() -> None:
    import inspect

    from server.memory.app import extract as me
    from server.memory.app.extract.coverage import uncovered_statement_count
    from server.settings import Settings

    msgs = [
        {
            "role": "user",
            "content": "I bought two figurines yesterday at the market downtown. Then I made a peach cobbler for the shelter. Later I signed up for taekwondo at the new gym on Main Street.",
        },
        {"role": "assistant", "content": "That sounds like a full day!"},
        {
            "role": "user",
            "content": "It was. My cat Bailey knocked over the cobbler though, so I baked another one late at night.",
        },
    ]
    covered = [
        me.ExtractedFact(
            memory_key="user_figurines",
            value="User bought two figurines at the market downtown.",
            source_message_indices=[0],
        )
    ]
    n = uncovered_statement_count(msgs, covered)
    assert n >= 3, n  # cobbler, taekwondo, the cat turn
    full = covered + [
        me.ExtractedFact(
            memory_key="user_cobbler",
            value="User made a peach cobbler for the shelter.",
            source_message_indices=[0],
        ),
        me.ExtractedFact(
            memory_key="user_taekwondo",
            value="User signed up for taekwondo at the new gym on Main Street.",
            source_message_indices=[0],
        ),
        me.ExtractedFact(
            memory_key="user_cat_bailey",
            value="User's cat Bailey knocked over the cobbler; user baked another one late at night.",
            source_message_indices=[2],
        ),
    ]
    assert uncovered_statement_count(msgs, full) < n
    from server.memory.app.extract import extract as extract_mod

    src = inspect.getsource(extract_mod._coverage_sweep_reason)
    assert 'coverage_sweep_reason = f"uncovered:{_n_unc}"' in src
    assert "_coverage_sweep_reason(" in inspect.getsource(me.extract_from_messages)
    assert Settings.model_fields["memory_extract_coverage_sweep_auto_min_uncovered"].default == 0
