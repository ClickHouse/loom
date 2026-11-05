"""`Agent.call` (generic typed call with the identity stamped and
an idempotency key for idempotent ops), `Agent.probe`, and the field
passthroughs on `memory.search` / `knowledge.search`."""

from __future__ import annotations

from typing import Any

import pytest
from loom import Agent, SyncAgent


class _FakeTransport:
    def __init__(self, reply: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.reply = reply or {"ok": True, "results": []}
        self.before_post = None

    async def post(
        self,
        op_path: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
        *,
        timeout_s: float | None = None,
    ):
        self.calls.append((op_path, dict(payload), idempotency_key))
        return dict(self.reply)

    async def aclose(self) -> None:
        return None

    def token_claims(self) -> None:
        return None


def _agent(reply: dict[str, Any] | None = None) -> tuple[Agent, _FakeTransport]:
    a = Agent("acme", "support", "assistant", base_url="http://x", api_key="k", auto_run=False)
    t = _FakeTransport(reply)
    a.transport = t  # type: ignore[assignment]
    a.memory._agent = a  # clients hold the agent, which now holds the fake
    return a, t


async def test_call_stamps_identity_with_sentinels_and_the_body_wins() -> None:
    a, t = _agent()
    await a.call("memory.set", {"memory_key": "k", "value": "v"}, user_id="u1", tags=["t"])
    path, body, key = t.calls[0]
    assert path == "/v1/memory.set"
    assert body == {
        "org": "acme",
        "namespace": "support",
        "agent": "assistant",
        "user_id": "u1",
        "session_id": "-",
        "run_id": "-",
        "memory_key": "k",
        "value": "v",
        "tags": ["t"],
    }
    assert key, "memory.set is idempotent → an Idempotency-Key is generated"
    await a.call(
        "memory.set", {"memory_key": "k"}, agent_id="orchestrated", idempotency_key="fixed"
    )
    assert t.calls[1][1]["agent"] == "orchestrated" and t.calls[1][2] == "fixed"


async def test_call_sends_no_key_for_non_idempotent_or_unknown_ops() -> None:
    a, t = _agent()
    await a.call("memory.set_from_messages", {"messages": []})
    assert t.calls[0][2] is None
    await a.call("memory.newer_than_this_sdk", {"x": 1})
    assert t.calls[1][0] == "/v1/memory.newer_than_this_sdk" and t.calls[1][2] is None
    await a.call("memory.get", {"memory_key": "k"})  # read-only: the server keeps no replay
    assert t.calls[2][2] is None


async def test_probe_reports_missing_ops_fields_and_unknown_server_ops() -> None:
    a, t = _agent(
        {
            "build_commit": "abc1234",
            "schema_version": "1",
            "ops": [
                {
                    "name": "memory.search",
                    "request_fields": ["org", "namespace", "query", "exclude_keys"],
                },
                {"name": "memory.ask", "request_fields": ["org", "namespace", "query"]},
                {"name": "memory.from_the_future", "request_fields": []},
            ],
        }
    )
    p = await a.probe(
        ops=["memory.search", "context.pack"],
        fields={"memory.search": ["exclude_keys", "uri_prefix"]},
    )
    assert t.calls[0][0] == "/v1/ops.capabilities"
    assert p["ok"] is False
    assert p["missing_ops"] == ["context.pack"]
    assert p["missing_fields"] == {"memory.search": ["uri_prefix"]}
    assert p["unknown_to_sdk"] == ["memory.from_the_future"]
    assert p["build_commit"] == "abc1234"
    await a.can_call("memory.search")
    assert len(t.calls) == 1, "probe refreshed the capabilities can_call reads"


async def test_memory_search_passes_exclude_keys_and_extra_fields() -> None:
    a, t = _agent()
    await a.memory.search(
        "seat", exclude_keys=["seen_a", "seen_b"], top_k=3, recipe="fast", summary_cap=1
    )
    path, body, _ = t.calls[0]
    assert path == "/v1/memory.search"
    assert body["exclude_keys"] == ["seen_a", "seen_b"]
    assert body["top_k"] == 3 and body["recipe"] == "fast"
    assert body["summary_cap"] == 1, "an unknown-to-the-SDK field rides the body"


async def test_knowledge_search_passes_uri_prefix_and_extra_fields() -> None:
    a, t = _agent()
    await a.knowledge.search("deploy", uri_prefix=["repo://loom/docs/"], collapse_sections=False)
    path, body, _ = t.calls[0]
    assert path == "/v1/knowledge.search"
    assert body["uri_prefix"] == ["repo://loom/docs/"]
    assert body["collapse_sections"] is False


async def test_knowledge_synthesize_sends_datasets_with_or_without_documents() -> None:
    a, t = _agent()
    orders = {"source_uri": "repo://orders.csv", "dataset_id": "ds_1", "alias": "o"}
    await a.knowledge.synthesize(None, "Total amount?", datasets=[orders])
    path, body, _ = t.calls[0]
    assert path == "/v1/knowledge.synthesize"
    assert (body["source_uris"], body["datasets"], body["task"]) == ([], [orders], "Total amount?")
    await a.knowledge.synthesize(["doc://a"], "Summarize")  # the call it always took
    assert t.calls[1][1]["source_uris"] == ["doc://a"] and "datasets" not in t.calls[1][1]


def test_identity_helper() -> None:
    a, _ = _agent()
    assert a.identity() == {
        "org": "acme",
        "namespace": "support",
        "agent": "assistant",
        "user_id": "-",
        "session_id": "-",
        "run_id": "-",
    }
    assert a.identity(session_id="s1")["session_id"] == "s1"
    # The typed clients build the same tuple; only an active run_scope adds its run.
    assert a.memory._identity(None, None) == a.identity()
    assert a.memory._identity("u", "s", agent_id="x") == a.identity(
        user_id="u", session_id="s", agent_id="x"
    )


@pytest.mark.parametrize("op", ["memory.get", "knowledge.search", "ops.capabilities"])
def test_ops_table_agrees_with_the_registry_on_idempotency(op: str) -> None:
    from loom.ops import OPS

    from server.operations.registry import OPERATIONS_BY_NAME

    assert OPS[op].idempotent == OPERATIONS_BY_NAME[op].idempotent


def _mock_agent(**kw: Any) -> tuple[Agent, list[tuple[str, dict[str, Any]]]]:
    import json

    import httpx

    seen: list[tuple[str, dict[str, Any]]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, json.loads(req.content or b"{}")))
        if req.url.path == "/v1/mentor.brief":
            hints = [{"code": f"c{i}", "severity": "info", "text": f"hint {i}"} for i in range(4)]
            return httpx.Response(200, json={"hints": hints})
        if req.url.path == "/v1/mentor.guide":
            return httpx.Response(200, json={"version": "1", "part": "skill", "text": "# loom"})
        return httpx.Response(200, json={"results": []})

    a = Agent("acme", "support", "assistant", base_url="http://x", api_key="k", **kw)
    a.transport._client = httpx.AsyncClient(
        base_url="http://x", transport=httpx.MockTransport(handler)
    )
    return a, seen


async def test_mentor_posts_the_v1_route_and_logs_three_hints(
    caplog: pytest.LogCaptureFixture,
) -> None:
    a, seen = _mock_agent()
    with caplog.at_level("INFO", logger="loom"):
        brief = await a.mentor(window_seconds=3600)
    await a.close()
    path, body = seen[0]
    assert path == "/v1/mentor.brief"
    assert body["org"] == "acme" and body["namespace"] == "support"
    assert body["agent"] == "assistant" and body["window_seconds"] == 3600
    assert len(brief["hints"]) == 4
    assert [r.getMessage() for r in caplog.records if "loom mentor" in r.getMessage()] == [
        f"loom mentor (c{i}, info): hint {i}" for i in range(3)
    ]


async def test_guide_returns_the_text_of_the_requested_part() -> None:
    a, seen = _mock_agent()
    text = await a.guide("reference")
    await a.close()
    path, body = seen[0]
    assert path == "/v1/mentor.guide" and body["part"] == "reference"
    assert text == "# loom"


def test_sync_agent_guide_blocks_and_returns_the_text() -> None:
    import httpx

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"version": "1", "part": "skill", "text": "# loom"})

    with SyncAgent("acme", "support", "assistant", base_url="http://x", api_key="k") as sa:
        sa.transport._client = httpx.AsyncClient(
            base_url="http://x", transport=httpx.MockTransport(handler)
        )
        assert sa.guide() == "# loom"


@pytest.mark.parametrize("via_env", [False, True])
async def test_mentor_brief_runs_once_in_the_background_on_the_first_call(
    via_env: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    if via_env:
        monkeypatch.setenv("LOOM_MENTOR_BRIEF", "on")
    a, seen = _mock_agent(**({} if via_env else {"mentor_brief": True}))
    await a.call("memory.search", query="q")
    await a.call("memory.search", query="q")
    assert a._mentor_brief_task is not None
    await asyncio.wait_for(a._mentor_brief_task, 1.0)
    await a.close()
    assert sorted(p for p, _ in seen) == ["/v1/memory.search"] * 2 + ["/v1/mentor.brief"]


async def test_mentor_brief_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOOM_MENTOR_BRIEF", raising=False)
    a, seen = _mock_agent()
    await a.call("memory.search", query="q")
    await a.close()
    assert [p for p, _ in seen] == ["/v1/memory.search"]
