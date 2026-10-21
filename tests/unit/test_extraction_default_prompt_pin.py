"""The default extraction prompt, byte for byte.

The production extraction prompt is calibrated, and the extractor's response
cache keys on the sha256 of the exact system + user text. A
change to the default path's prompt changes what gets stored and cold-starts
that cache, so it must be deliberate: this pins the text every default-path
completion sends. Update the digests only when the prompt is meant to change.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from server.llm.client import LLMResult
from server.settings import Settings

_FACTS = '{"facts": [{"key": "user_city", "value": "Lives in Lisbon since 2021."}]}'
_MSGS: list[dict[str, object]] = [
    {"role": "user", "content": "I moved to Lisbon in 2021 and I still live there today."},
    {"role": "assistant", "content": "Noted: you live in Lisbon (since 2021)."},
]


class _Provider:
    provider = "fake"
    model = "fake-extractor"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, **kw: Any) -> LLMResult:
        self.calls.append((system, user))
        return LLMResult(
            text=_FACTS, tokens_in=10, tokens_out=10, provider=self.provider, model=self.model
        )


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@pytest.fixture
def defaults(monkeypatch: pytest.MonkeyPatch) -> _Provider:
    from server.memory.app.extract import client, extract, prompts, sweep

    # Every module of the extractor that reads `settings` reads the defaults.
    defaults = Settings(_env_file=None)  # type: ignore[call-arg]
    for mod in (extract, prompts, sweep):
        monkeypatch.setattr(mod, "settings", defaults)
    provider = _Provider()
    monkeypatch.setattr(client, "get_llm_client", lambda: provider)
    return provider


@pytest.mark.parametrize(
    ("compact", "system_sha", "user_sha"),
    [
        (
            False,
            "b5cb9721fd00f250b8c22f96472d8d8f78499463a51f270c524542e3daaeda70",
            "19501f97701264f74e9b1027bdaac0afd413a525c974b1b535f6940fb5faef92",
        ),
        (
            True,
            "125e2de9155dec71b34de1967a94acfd8a223015862817ea05eaf56f8b404945",
            "19501f97701264f74e9b1027bdaac0afd413a525c974b1b535f6940fb5faef92",
        ),
    ],
)
@pytest.mark.asyncio
async def test_the_default_extraction_prompt_is_pinned(
    defaults: _Provider, compact: bool, system_sha: str, user_sha: str
) -> None:
    from server.memory.app.extract import extract_from_messages

    await extract_from_messages(
        _MSGS,
        judge=False,
        observation_date="2026-09-01",
        coverage_sweep=False,
        llm_cache=False,
        compact_prompt=compact,
        operator_policy="",
    )
    system, user = defaults.calls[0]
    assert (_sha(system), _sha(user)) == (system_sha, user_sha)
