"""An Agent built with no identity sends the org and namespace empty, for a
single-user server to fill, and names its agent after the script without the
anonymous-agent warning; an anonymous name passed on purpose still warns."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from loom import Agent


def _sent_body(agent: Agent) -> dict[str, object]:
    sent: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"status": "active"}, headers={"X-Request-Id": "r1"})

    async def go() -> None:
        await agent.transport._client.aclose()
        agent.transport._client = httpx.AsyncClient(
            base_url="http://x", transport=httpx.MockTransport(handler)
        )
        await agent.call("memory.set", {"memory_key": "k", "value": "v"})
        await agent.close()

    asyncio.run(go())
    return sent[0]


def test_an_agent_with_no_identity_leaves_it_to_the_server(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="loom"):
        body = _sent_body(Agent(base_url="http://x", coalesce_sets=False))
    assert body["org"] == "" and "namespace" not in body
    assert str(body["agent"]).startswith("sdk-python:")
    assert "anonymous" not in caplog.text


def test_an_anonymous_agent_name_passed_on_purpose_still_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="loom"):
        body = _sent_body(Agent("dev", "local", "*", base_url="http://x", coalesce_sets=False))
    assert body["org"] == "dev" and str(body["agent"]).startswith("sdk-python:")
    assert "anonymous" in caplog.text


def test_an_upload_or_download_needs_the_identity_named() -> None:
    """They run outside the op routes, where no server fills an identity in."""

    async def go() -> None:
        agent = Agent(base_url="http://x")
        try:
            with pytest.raises(ValueError, match="org and namespace"):
                await agent.knowledge.upload(b"x", "a.txt")
            with pytest.raises(ValueError, match="org and namespace"):
                await agent.knowledge.download("doc://a")
        finally:
            await agent.close()

    asyncio.run(go())


def test_only_an_empty_top_level_namespace_is_left_out() -> None:
    from loom.transport import Transport

    assert Transport.without_empty_namespace({"namespace": "", "k": 1}) == {"k": 1}
    named = {"namespace": "support", "k": 1}
    assert Transport.without_empty_namespace(named) is named
    nested = {"entries": [{"namespace": ""}]}
    assert Transport.without_empty_namespace(nested) is nested


def test_a_sync_agent_takes_no_identity() -> None:
    from loom import SyncAgent

    with SyncAgent(base_url="http://x") as sa:
        assert (sa._agent.org, sa._agent.namespace) == ("", "")
        assert sa._agent.agent.startswith("sdk-python:")
