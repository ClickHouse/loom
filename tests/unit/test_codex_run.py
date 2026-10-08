"""Codex client behaviour.

1. Codex's MCP client (`clientInfo.name = codex-mcp-client`) writes under the
   agent its plugin hooks use (`codex`) — see test_mcp_client_provenance.
2. A prefix listing says how many keys under the prefix it hid as
   duplicate/superseded, instead of answering `total: 0` while the key exists
   (e.g. the model's own key merged into a peer).
"""

from __future__ import annotations

from typing import Any

import pytest

from server.memory import store as memory_repo
from server.memory.api.models_crud import MemoryListInput
from server.memory.app import query as mq


def _fake_repo(
    monkeypatch: pytest.MonkeyPatch, *, visible: int, every: int
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _nsv(org: str, ns: str) -> int:
        return 0

    async def _list(*a: Any, **k: Any) -> list[Any]:
        return []

    async def _count(identity: Any, **k: Any) -> int:
        calls.append(k)
        wide = k.get("include_duplicates") and k.get("include_superseded")
        return every if wide else visible

    monkeypatch.setattr(memory_repo, "namespace_version", _nsv)
    monkeypatch.setattr(memory_repo, "list_by_namespace", _list)
    monkeypatch.setattr(memory_repo, "count_by_namespace", _count)
    mq.reset_list_counts_cache()
    return calls


def _inp(**kw: Any) -> MemoryListInput:
    return MemoryListInput(org="o", namespace="n", agent="*", **kw)


@pytest.mark.asyncio
async def test_a_prefix_listing_names_the_keys_it_hid(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_repo(monkeypatch, visible=0, every=1)
    out = await mq._handle_memory_list(_inp(prefix="test/x/"))
    assert out.total == 0 and out.hidden == 1
    assert (
        "include_duplicates=true" in out.hidden_hint
        and "include_superseded=true" in out.hidden_hint
    )


@pytest.mark.asyncio
async def test_a_listing_that_shows_keys_pays_no_extra_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_repo(monkeypatch, visible=2, every=3)
    out = await mq._handle_memory_list(_inp(prefix="test/x/"))
    assert out.hidden == 0 and out.hidden_hint == ""
    assert len(calls) == 2  # total + today only


@pytest.mark.asyncio
async def test_no_prefix_or_widened_listing_skips_the_extra_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_repo(monkeypatch, visible=0, every=5)
    out = await mq._handle_memory_list(_inp())
    assert out.hidden == 0
    n_plain = len(calls)
    out = await mq._handle_memory_list(
        _inp(prefix="p/", include_duplicates=True, include_superseded=True)
    )
    assert out.hidden == 0
    assert len(calls) - n_plain == 2  # total + today only: no third count


# ---- 3. one channel for a client that reads the whole result ----

from server.transport.lean_results import STRUCTURED_POINTER, single_channel  # noqa: E402
from server.transport.mcp_identity import _client_name_to_agent  # noqa: E402


def _resp(**result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def test_codex_reads_the_typed_half_and_a_pointer() -> None:
    r = _resp(
        content=[{"type": "text", "text": "long rendered answer " * 200}],
        structuredContent={"hits": [{"memory_key": "k"}]},
    )
    out = single_channel(r, _client_name_to_agent("codex-mcp-client"))
    assert out["result"]["content"] == [{"type": "text", "text": STRUCTURED_POINTER}]
    assert out["result"]["structuredContent"] == {"hits": [{"memory_key": "k"}]}
    assert r["result"]["content"][0]["text"].startswith("long")  # input untouched


def test_other_clients_errors_and_textonly_results_are_unchanged() -> None:
    r = _resp(content=[{"type": "text", "text": "t"}], structuredContent={"a": 1})
    assert single_channel(r, "claude-code") is r
    err = _resp(
        content=[{"type": "text", "text": "409 PINNED"}],
        structuredContent={"code": "x"},
        isError=True,
    )
    assert single_channel(err, "codex") is err
    plain = _resp(content=[{"type": "text", "text": "t"}])
    assert single_channel(plain, "codex") is plain
    img = _resp(content=[{"type": "image", "data": "..."}], structuredContent={"a": 1})
    assert single_channel(img, "codex") is img


# ---- 4. profile.append extends the section it read ----

from unittest.mock import AsyncMock, patch  # noqa: E402

from server.memory.api.models_profile import ProfileAppendInput  # noqa: E402
from server.memory.app.profile import handle_profile_append  # noqa: E402


@pytest.mark.asyncio
async def test_profile_append_writes_to_the_agent_whose_section_it_read() -> None:
    class _Got:
        found, value, char_limit, content_sha256, agent = (
            True,
            "likes tea",
            2048,
            "sha-cc",
            "claude-code",
        )

    class _Set:
        version, memory_id, content_sha256, namespace = 7, "m", "sha2", "n"

    get_mock = AsyncMock(return_value=_Got())
    set_mock = AsyncMock(return_value=_Set())
    inp = ProfileAppendInput(
        org="o", namespace="n", agent="codex", label="preferences", addition="likes coffee"
    )
    with (
        patch("server.memory.app.handlers.handle_memory_get", get_mock),
        patch("server.memory.app.write.handle_memory_set", set_mock),
        patch(
            "server.memory.app.profile_search.schedule_reembed_after_write", lambda *a, **k: None
        ),
    ):
        out = await handle_profile_append(inp)
    mset = set_mock.await_args.args[0]
    assert out.version == 7 and set_mock.await_count == 1
    assert mset.agent == "claude-code" and mset.if_match_sha256 == "sha-cc"
    assert mset.value.endswith("likes coffee")
