"""A lookup leaves no memory of itself. Loom's own tools are
self-referential under the plugin-installed name too; the digest descent
never serves a raw observation; a fact that names an existing key is the
assistant recalling that memory and is dropped before the write."""

from __future__ import annotations

import asyncio
import inspect

import pytest

import server.memory.app.pipeline as mp
from server.memory.app import pipeline_state as pipeline_state
from server.memory.domain import recall_echoes as echoes
from server.memory.domain.observation_tags import is_self_referential


def test_plugin_installed_loom_tools_are_self_referential() -> None:
    for t in (
        "mcp__plugin_loom_loom__loom_search",
        "mcp__plugin_loom_loom__loom_memory_set",
        "mcp__plugin_loom_loom__loom_code_symbol",
        "mcp__loom__loom_search",
    ):
        assert is_self_referential(t), t
    assert not is_self_referential("mcp__plugin_github_github__create_issue")


def test_digest_descent_never_serves_a_raw_observation() -> None:
    from server.memory.app.search.sql import _digest_descent_where

    class _In:
        agent = ""
        run_id = ""
        tags = None

    where = _digest_descent_where(_In(), {})
    assert "NOT startsWith(memory_key, 'obs:')" in where


def test_key_tokens_find_key_shaped_words_only() -> None:
    text = "You live in Ujjain, saved in Loom as `where_i_live` on 2026-09-19 (#fav_place); see sre/case too"
    assert echoes.key_tokens(text) == ["where_i_live", "fav_place"]
    assert echoes.key_tokens("User lives in Indore.") == []
    assert echoes.key_tokens("a_b is short; snake_case_key counts") == ["snake_case_key"]


def test_recall_echoes_name_existing_keys_but_not_their_own_rewrite() -> None:
    facts = [
        {
            "memory_key": "user_home_location",
            "value": "User lives in Ujjain, saved in Loom as `where_i_live`.",
        },
        {"memory_key": "user_city", "value": "User lives in Indore."},
        {"memory_key": "where_i_live", "value": "indore (was where_i_live: ujjain)"},
        {"memory_key": "note", "value": "mentions unknown_key_here only"},
    ]
    assert echoes.recall_echoes(facts, {"where_i_live", "fav_place"}) == [0]


def test_step_drops_echoes_and_counts_them(monkeypatch: pytest.MonkeyPatch) -> None:
    state = pipeline_state.WorkflowState(
        org="clickhouse",
        namespace="jdoe",
        agent="claude-code",
        user_id="-",
        session_id="s",
        run_id="",
    )
    state.existing_memories = [{"memory_key": "where_i_live", "content_sha256": "x"}]
    state.extracted_facts = [
        {
            "memory_key": "user_home_location",
            "value": "User lives in Ujjain, saved in Loom as `where_i_live`.",
        },
        {"memory_key": "user_city", "value": "User lives in Indore."},
    ]

    async def _no_lookup(state_, tokens):  # the fetched neighbours already name the key
        return {t for t in tokens if t == "where_i_live"}

    monkeypatch.setattr(mp, "_existing_keys_named", _no_lookup)
    out = asyncio.run(mp._step_drop_recall_echoes(state, {}))
    assert [f["memory_key"] for f in out.extracted_facts] == ["user_city"]
    assert out.skipped_recall_echo == 1


def test_step_is_in_the_default_pipeline_before_dedup_and_write() -> None:
    ids = [s.step_id for s in mp.DEFAULT_PIPELINE]
    assert (
        ids.index("loom.llm_extract")
        < ids.index("loom.drop_recall_echoes")
        < ids.index("loom.hash_dedup")
    )
    assert "loom.drop_recall_echoes" in mp.registered_step_ids()
    assert "skipped_recall_echo" in inspect.getsource(mp._step_drop_recall_echoes)


def test_a_paraphrased_echo_under_a_wider_key_is_dropped_when_the_session_named_the_key() -> None:
    """Revision 1: the operator's exit wrote `user_coffee_type` = "User's coffee
    type is Espresso." after the reply said "saved under `coffee_type`"."""
    assert (
        echoes.key_words("user_coffee_type")
        == {"coffee", "type"}
        == echoes.key_words("coffee_type")
    )
    facts = [
        {"memory_key": "user_coffee_type", "value": "User's coffee type is Espresso."},
        {"memory_key": "coffee_type", "value": "espresso"},  # the key's own rewrite stays
        {
            "memory_key": "coffee_grinder_brand",
            "value": "Baratza",
        },  # shares a word, not the subject
        {"memory_key": "user_home_location", "value": "User lives in Indore."},
    ]
    assert echoes.recall_echoes(
        facts, {"coffee_type", "where_i_live"}, recalled_keys={"coffee_type"}
    ) == [0]
    assert echoes.recall_echoes(facts, {"coffee_type", "where_i_live"}) == [], (
        "without the session naming it, a wider key is a new fact"
    )


def test_step_reads_the_keys_the_session_named(monkeypatch: pytest.MonkeyPatch) -> None:
    state = pipeline_state.WorkflowState(
        org="clickhouse",
        namespace="jdoe",
        agent="claude-code",
        user_id="-",
        session_id="s",
        run_id="",
    )
    state.messages = [
        {"role": "user", "content": "what is my coffe type?"},
        {
            "role": "assistant",
            "content": "Espresso. Saved in Loom under the key `coffee_type` on 2026-09-19.",
        },
    ]
    state.extracted_facts = [
        {"memory_key": "user_coffee_type", "value": "User's coffee type is Espresso."},
        {"memory_key": "user_city", "value": "User lives in Indore."},
    ]

    async def _lookup(state_, tokens):
        assert "coffee_type" in tokens, tokens
        return {t for t in tokens if t == "coffee_type"}

    monkeypatch.setattr(mp, "_existing_keys_named", _lookup)
    out = asyncio.run(mp._step_drop_recall_echoes(state, {}))
    assert [f["memory_key"] for f in out.extracted_facts] == ["user_city"]
    assert out.skipped_recall_echo == 1


def test_served_keys_count_as_named_even_when_the_text_never_said_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Codex session that answers "Espresso." without naming the key still
    has `coffee_type` served to it; the hooks pass that in."""
    state = pipeline_state.WorkflowState(
        org="clickhouse",
        namespace="jdoe",
        agent="codex",
        user_id="-",
        session_id="s",
        run_id="",
        recalled_keys=["coffee_type"],
    )
    state.messages = [
        {"role": "user", "content": "what is my coffee type?"},
        {"role": "assistant", "content": "Espresso."},
    ]
    state.extracted_facts = [
        {"memory_key": "user_coffee_type", "value": "User's coffee type is Espresso."},
        {"memory_key": "user_city", "value": "User lives in Indore."},
    ]

    async def _lookup(state_, tokens):
        assert "coffee_type" in tokens
        return {t for t in tokens if t == "coffee_type"}

    monkeypatch.setattr(mp, "_existing_keys_named", _lookup)
    out = asyncio.run(mp._step_drop_recall_echoes(state, {}))
    assert [f["memory_key"] for f in out.extracted_facts] == ["user_city"]
    assert out.skipped_recall_echo == 1


def test_set_from_messages_carries_recalled_keys_into_the_state() -> None:
    from server.memory.api.models_bulk import MemorySetFromMessagesInput

    inp = MemorySetFromMessagesInput(
        org="o",
        namespace="n",
        agent="codex",
        messages=[{"role": "user", "content": "hi"}],
        recalled_keys=["coffee_type"],
    )
    assert inp.recalled_keys == ["coffee_type"]
    pins = inspect.getsource(
        __import__("server.memory.app.extract.handler", fromlist=["x"])._extract_request_pins
    )
    assert '"recalled_keys": list(getattr(input, "recalled_keys", None) or [])' in pins
