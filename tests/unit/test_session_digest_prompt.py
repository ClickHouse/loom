"""The session digest's prompt and output budget, and the transcript cap
behind `memory_session_digest_max_input_chars`."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app import session_summary as ss


class _LLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any):
        self.calls.append((system, user, max_tokens))
        return SimpleNamespace(
            text="Events:\n- on 2024-03-11 the user ran a 5k", tokens_in=10, tokens_out=5
        )


class _Emb:
    model_name = "fake-embed"

    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import server.llm.client as llm_client
    from server.memory.app import bulk_set as _bulk_set_mod
    from server.memory.app import session_summary as _session_summary_mod
    from server.settings import settings

    llm = _LLM()
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: llm)
    monkeypatch.setattr(_bulk_set_mod, "get_embedder", lambda: _Emb())
    monkeypatch.setattr(_session_summary_mod, "get_embedder", lambda: _Emb())
    monkeypatch.setattr(settings, "memory_session_digest_max_input_chars", 3000)
    return SimpleNamespace(llm=llm, settings=settings, monkeypatch=monkeypatch)


def _msgs(n: int = 3, size: int = 100) -> list[dict[str, str]]:
    return [{"role": "user", "content": ("x" * size)} for _ in range(n)]


@pytest.mark.asyncio
async def test_the_digest_is_dated_prose_under_its_budget(env) -> None:
    draft = await ss._draft_session_summary(
        SimpleNamespace(session_id="s"), _msgs(), "2024-03-11", operator_policy=""
    )
    system, user, max_tokens = env.llm.calls[0]
    assert system == ss._SESSION_SUMMARY_SYSTEM and max_tokens == 160
    assert "Session date: 2024-03-11" in user
    assert draft is not None and draft.embedding_model == "fake-embed"


def test_transcript_cap_follows_the_setting(env) -> None:
    long = _msgs(n=10, size=1000)  # ~10k chars
    p = ss._build_session_summary_user_prompt(long, "2024-01-01")
    assert len(p) < 3000 + 200
    env.monkeypatch.setattr(env.settings, "memory_session_digest_max_input_chars", 12000)
    p2 = ss._build_session_summary_user_prompt(long, "2024-01-01")
    assert len(p2) > 9000
    env.monkeypatch.setattr(env.settings, "memory_session_digest_max_input_chars", 0)
    assert len(ss._build_session_summary_user_prompt(long, "2024-01-01")) < 3000 + 200, (
        "0 = default"
    )
