"""Unit tests for the session-summary plane write side.

Two halves:
  - build_session_summary_row — the single source of truth for
    loom.session_summaries INSERT rows. Guards the RMT(version) silent-data-loss
    class (a write omits a column -> FINAL surfaces the schema DEFAULT over a
    prior real value).
  - _write_session_summary — the inline ingest writer. Stubs the LLM, embedder,
    and CH client so the row shape (mirror-every-column), the date-label carriage,
    and the fail-open skips run CH-free / network-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest

from server.memory.store import (
    SESSION_SUMMARY_PERSISTED_COLS,
    build_session_summary_row,
)

# ----------------------------------------------------------------------
# build_session_summary_row — mirror discipline
# ----------------------------------------------------------------------


def test_builder_emits_every_persisted_column() -> None:
    row = build_session_summary_row(org="o", namespace="n", session_id="s")
    # No column may be silently missing — that is the whole point (RMT trap).
    assert set(row) == set(SESSION_SUMMARY_PERSISTED_COLS)


def test_unknown_column_raises() -> None:
    # A typo'd column name is a bug, not a silently dropped value.
    with pytest.raises(ValueError, match="unknown columns"):
        build_session_summary_row(org="o", embeddings=[1, 2, 3])  # note the typo


def test_non_nullable_datetime_never_none() -> None:
    row = build_session_summary_row(org="o")
    # created_at is non-Nullable DateTime in the DDL; the builder must fill it so
    # an insert never sends NULL into a non-Nullable column.
    assert isinstance(row["created_at"], datetime)


def test_version_stamped_when_unset() -> None:
    row = build_session_summary_row(org="o")
    # ReplacingMergeTree(version): a re-ingest of the same session must win FINAL,
    # so an unset version is stamped from the nanosecond clock (non-zero).
    assert isinstance(row["version"], int)
    assert row["version"] > 0


def test_overrides_are_applied() -> None:
    row = build_session_summary_row(
        org="o",
        session_id="s1",
        summary_embedding=[0.1, 0.2],
        embedding_dim=2,
        key_facts=["a", "b"],
    )
    assert row["org"] == "o"
    assert row["session_id"] == "s1"
    assert row["summary_embedding"] == [0.1, 0.2]
    assert row["embedding_dim"] == 2
    assert row["key_facts"] == ["a", "b"]


def test_defaults_match_schema_for_omitted_columns() -> None:
    row = build_session_summary_row(org="o")
    assert row["summary_embedding"] == []
    assert row["key_facts"] == []
    assert row["valid_at"] is None
    assert row["tombstoned_at"] is None
    assert row["embedding_dim"] == 0
    assert row["agent"] == "-"
    assert row["user_id"] == "-"


# ----------------------------------------------------------------------
# Pure helpers — _top_key_facts + _build_session_summary_user_prompt
# ----------------------------------------------------------------------


def test_top_key_facts_never_carry_a_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    # The digest is listed by memory.digests and sent to the outcome judge; a fact whose
    # text reads as a credential stays out of its key facts, whatever the autodetect setting.
    from server.memory.app.extract.handler import _top_key_facts
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_sensitivity_autodetect", False)
    token = "ghp_" + "a" * 36
    facts = [{"value": f"the deploy token is {token}"}, {"value": "user lives in Berlin"}]
    assert _top_key_facts(facts) == ["user lives in Berlin"]


def test_the_judge_prompt_drops_a_stored_credential_key_fact() -> None:
    # Digests an earlier build wrote may hold one; the outcome judge's prompt drops it.
    from server.memory.app.session_outcomes import judge_prompt

    token = "ghp_" + "a" * 36
    _system, user = judge_prompt("a session", [f"the token is {token}", "ran the tests"])
    assert token not in user and "ran the tests" in user


def test_top_key_facts_dedups_and_caps() -> None:
    from server.memory.app.extract.handler import (
        _SESSION_SUMMARY_MAX_KEY_FACTS,
        _top_key_facts,
    )

    facts = [
        {"value": "user lives in Berlin"},
        {"value": "user lives in Berlin"},  # dup -> dropped
        {"value": {"city": "Berlin"}},  # dict -> json string surface
        {"value": ""},  # empty -> dropped
        {"value": "fact a"},
        {"value": "fact b"},
        {"value": "fact c"},
        {"value": "fact d"},
        {"value": "fact e"},
    ]
    out = _top_key_facts(facts)
    assert len(out) <= _SESSION_SUMMARY_MAX_KEY_FACTS
    assert out[0] == "user lives in Berlin"
    # The dict value coerces to a JSON string surface, not dropped.
    assert any("Berlin" in s and "{" in s for s in out)


def test_user_prompt_carries_date_and_truncates() -> None:
    from server.memory.app.extract.handler import (
        _SESSION_SUMMARY_MAX_INPUT_CHARS,
        _build_session_summary_user_prompt,
    )

    msgs = [{"role": "user", "content": "x" * 10000}]
    prompt = _build_session_summary_user_prompt(msgs, "2024-03-11")
    assert "Session date: 2024-03-11" in prompt
    assert "[user]" in prompt
    # The transcript body is bounded; the header + framing add a little.
    assert len(prompt) < _SESSION_SUMMARY_MAX_INPUT_CHARS + 200


def test_user_prompt_unknown_date() -> None:
    from server.memory.app.extract.handler import _build_session_summary_user_prompt

    prompt = _build_session_summary_user_prompt([{"role": "user", "content": "hi"}], None)
    assert "Session date: unknown" in prompt


# ----------------------------------------------------------------------
# _write_session_summary — end-to-end with stubs
# ----------------------------------------------------------------------


@dataclass
class _FakeInput:
    org: str = "o"
    namespace: str = "n"
    agent: str = "a"
    user_id: str = "u"
    session_id: str = "sess-1"


@dataclass
class _FakeState:
    extracted_facts: list[dict[str, Any]] = field(default_factory=list)
    observation_date: str | None = None


class _FakeLLM:
    provider = "fake"
    model = "fake-echo-1"

    def __init__(self, text: str = "on 2024-03-11, the user moved to Berlin.") -> None:
        self._text = text
        self.calls: list[tuple[str, str, int]] = []

    async def complete(self, system, user, max_tokens=1024, response_format=None):
        from server.llm.client import LLMResult

        self.calls.append((system, user, max_tokens))
        return LLMResult(
            text=self._text,
            tokens_in=10,
            tokens_out=5,
            provider=self.provider,
            model=self.model,
        )


class _FakeEmbedder:
    def __init__(self, vec: list[float] | None = None) -> None:
        self.model_name = "fake-ngram-8"
        self.dim = 8
        self._vec = [0.1] * 8 if vec is None else vec

    async def embed(self, text: str) -> list[float]:
        return list(self._vec)


class _FakeCH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, rows, settings or {}))


@pytest.fixture
def _patch_clients(monkeypatch):
    """Patch get_llm_client / get_embedder / get_client used by the writer."""
    import server.llm.client as llm_mod
    from server.memory.app import bulk_set as _bulk_set_mod
    from server.memory.app import session_summary as _session_summary_mod

    llm = _FakeLLM()
    embedder = _FakeEmbedder()
    ch = _FakeCH()
    # _write_session_summary imports get_llm_client lazily from server.llm.client
    monkeypatch.setattr(llm_mod, "get_llm_client", lambda: llm)
    monkeypatch.setattr(_bulk_set_mod, "get_embedder", lambda: embedder)
    monkeypatch.setattr(_session_summary_mod, "get_embedder", lambda: embedder)
    monkeypatch.setattr(_bulk_set_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_session_summary_mod, "get_client", lambda: ch)
    return llm, embedder, ch


@pytest.mark.asyncio
async def test_write_summary_inserts_full_row(_patch_clients) -> None:
    from server.memory.app.extract.handler import _write_session_summary

    llm, embedder, ch = _patch_clients
    state = _FakeState(
        extracted_facts=[{"value": "user moved to Berlin"}, {"value": "likes jazz"}],
        observation_date="2024-03-11",
    )
    await _write_session_summary(_FakeInput(), [{"role": "user", "content": "hi"}], state)

    assert len(ch.inserts) == 1
    table, rows, settings = ch.inserts[0]
    assert table == "loom.session_summaries"
    # Synchronous insert so the immediate post-ingest read sees it.
    assert settings.get("async_insert") == 0
    assert settings.get("wait_for_async_insert") == 1
    assert len(rows) == 1
    row = rows[0]
    # Mirror-every-column: every persisted column is present in the insert dict.
    assert set(row) == set(SESSION_SUMMARY_PERSISTED_COLS)
    assert row["summary_text"]
    assert row["summary_embedding"] == [0.1] * 8
    assert row["embedding_model"] == "fake-ngram-8"
    assert row["embedding_dim"] == 8
    # key_facts derived from extracted_facts (no extra LLM call).
    assert row["key_facts"] == ["user moved to Berlin", "likes jazz"]
    # valid_at carries the parsed session date (LABEL).
    assert isinstance(row["valid_at"], datetime)
    assert row["valid_at"].year == 2024 and row["valid_at"].month == 3
    # Exactly one cheap LLM call, max_tokens bounded.
    assert len(llm.calls) == 1
    assert llm.calls[0][2] == 160


@pytest.mark.asyncio
async def test_write_summary_null_valid_at_when_undated(_patch_clients) -> None:
    from server.memory.app.extract.handler import _write_session_summary

    _llm, _embedder, ch = _patch_clients
    state = _FakeState(extracted_facts=[{"value": "f"}], observation_date=None)
    await _write_session_summary(_FakeInput(), [{"role": "user", "content": "hi"}], state)

    row = ch.inserts[0][1][0]
    assert row["valid_at"] is None


@pytest.mark.asyncio
async def test_write_summary_skips_when_no_facts(_patch_clients) -> None:
    from server.memory.app.extract.handler import _write_session_summary

    llm, _embedder, ch = _patch_clients
    state = _FakeState(extracted_facts=[], observation_date="2024-03-11")
    await _write_session_summary(_FakeInput(), [{"role": "user", "content": "hi"}], state)

    # Nothing to summarize -> no LLM call, no insert.
    assert llm.calls == []
    assert ch.inserts == []


@pytest.mark.asyncio
async def test_write_summary_skips_on_empty_digest(monkeypatch, _patch_clients) -> None:
    from server.memory.app.extract.handler import _write_session_summary

    llm, _embedder, ch = _patch_clients
    llm._text = "   "  # whitespace-only digest
    state = _FakeState(extracted_facts=[{"value": "f"}], observation_date="2024-03-11")
    await _write_session_summary(_FakeInput(), [{"role": "user", "content": "hi"}], state)

    assert ch.inserts == []  # empty digest -> no row


@pytest.mark.asyncio
async def test_write_summary_skips_on_empty_embedding(monkeypatch, _patch_clients) -> None:
    from server.memory.app import bulk_set as _bulk_set_mod
    from server.memory.app import session_summary as _session_summary_mod
    from server.memory.app.extract.handler import _write_session_summary

    llm, _embedder, ch = _patch_clients
    monkeypatch.setattr(_bulk_set_mod, "get_embedder", lambda: _FakeEmbedder(vec=[]))
    monkeypatch.setattr(_session_summary_mod, "get_embedder", lambda: _FakeEmbedder(vec=[]))
    state = _FakeState(extracted_facts=[{"value": "f"}], observation_date="2024-03-11")
    await _write_session_summary(_FakeInput(), [{"role": "user", "content": "hi"}], state)

    assert ch.inserts == []  # dim-0 row is dead weight to the read leg -> skip


@pytest.mark.asyncio
async def test_write_summary_propagates_for_caller_failopen(monkeypatch, _patch_clients) -> None:
    """The writer itself raises on LLM error; the CALLER wraps it (the
    set_from_messages try/except records a projection failure). So the writer's
    contract is: raise, don't swallow — the fail-open boundary is one level up."""
    import server.llm.client as llm_mod
    from server.memory.app.extract.handler import _write_session_summary

    class _BoomLLM:
        async def complete(self, *a, **k):
            raise RuntimeError("llm down")

    monkeypatch.setattr(llm_mod, "get_llm_client", lambda: _BoomLLM())
    state = _FakeState(extracted_facts=[{"value": "f"}], observation_date="2024-03-11")
    with pytest.raises(RuntimeError, match="llm down"):
        await _write_session_summary(_FakeInput(), [{"role": "user", "content": "hi"}], state)
