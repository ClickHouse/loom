"""The description writer never runs on the fake LLM client.

With `llm.provider = fake` (or a keyed provider whose key is absent, which falls
back to the fake client) the fake's echo would be stored as the chunk's `llm`
description and embedded. The ingest pass gives those chunks structural context
instead, and `loom code describe` refuses to start."""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.app.index import pass_stream
from server.code.app.index.pass_budget import CodeIngestResult
from server.llm import client as llm_client
from server.settings import settings


def _stream() -> pass_stream.CommitStream:
    cfg = SimpleNamespace(
        org="o", namespace="ns", path_prefixes=(), embed_reuse_from=None, git_dir="/nowhere"
    )
    return pass_stream.CommitStream(cfg, None, "head", "base", ["sha"], CodeIngestResult())


@pytest.fixture
def prepare_env(monkeypatch: pytest.MonkeyPatch) -> None:
    async def under_cap(*_a: Any) -> bool:
        return False

    async def no_denylist(*_a: Any) -> None:
        return None

    monkeypatch.setattr(settings, "code_context_description_enabled", True)
    monkeypatch.setattr("server.observability.budget.namespace_over_cap", under_cap)
    monkeypatch.setattr("server.code.app.index.denylist.load_denylist", no_denylist)
    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: object())


def _no_client() -> Any:
    raise AssertionError("the description writer must not build a client")


async def test_a_fake_provider_describes_nothing(
    monkeypatch: pytest.MonkeyPatch, prepare_env: None
) -> None:
    monkeypatch.setattr(settings, "llm_provider", "fake")
    monkeypatch.setattr(llm_client, "get_llm_client", _no_client)
    stream = _stream()
    ctx = await stream._prepare()
    assert ctx["describe"] is False
    assert ctx["llm"] is None
    assert stream.result.note == "describe skipped: the llm provider is fake"


async def test_a_keyed_provider_without_its_key_describes_nothing(
    monkeypatch: pytest.MonkeyPatch, prepare_env: None
) -> None:
    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(llm_client, "provider_key_missing", lambda _p: True)
    monkeypatch.setattr(llm_client, "get_llm_client", _no_client)
    ctx = await _stream()._prepare()
    assert ctx["describe"] is False
    assert ctx["llm"] is None


async def test_a_real_provider_still_describes(
    monkeypatch: pytest.MonkeyPatch, prepare_env: None
) -> None:
    real = object()
    monkeypatch.setattr(llm_client, "llm_is_fake", lambda: False)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: real)
    ctx = await _stream()._prepare()
    assert ctx["describe"] is True
    assert ctx["llm"] is real


async def test_the_describe_cli_refuses_a_fake_provider(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server.cli import code_maintenance

    async def no_connect(*_a: Any) -> None:
        raise AssertionError("refused before connecting")

    monkeypatch.setattr(settings, "llm_provider", "fake")
    monkeypatch.setattr(llm_client, "get_llm_client", _no_client)
    monkeypatch.setattr(code_maintenance, "connect_code_plane", no_connect)
    assert await code_maintenance._describe(argparse.Namespace()) == 2
    assert "fake llm" in capsys.readouterr().err
