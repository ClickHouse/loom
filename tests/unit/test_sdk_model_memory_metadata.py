"""Model-selected memory tools preserve references and explicit correction receipts."""

from __future__ import annotations

import json

import httpx
import pytest
from loom import Agent
from loom.errors import LoomError


def _agent(reply: dict, status: int = 200) -> tuple[Agent, list]:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(status, json=reply, headers={"X-Request-Id": "memory-result"})

    agent = Agent("test", "memory", "consumer", api_key="fixture", mentor_brief=False)
    agent.transport._client = httpx.AsyncClient(
        base_url="http://example.invalid", transport=httpx.MockTransport(handle)
    )
    return agent, calls


@pytest.mark.parametrize("files", [["src/app.py"], []])
async def test_remember_model_schema_and_request_keep_explicit_metadata(files: list[str]) -> None:
    receipt = {"memory_id": "m", "version": 3, "content_sha256": "sha", "code_state": "unresolved"}
    agent, calls = _agent(receipt)
    try:
        tools = agent.tools()
        schema = next(x for x in tools.definitions() if x["name"] == "loom_remember")
        assert {"files", "tags", "source_resource_id"} <= schema["parameters"]["properties"].keys()
        result = json.loads(
            await tools.execute(
                "loom_remember",
                {
                    "memory_key": "app/rationale",
                    "value": "A fact",
                    "files": files,
                    "tags": ["repo:test"],
                    "source_resource_id": "src/app.py:8",
                },
            )
        )
        assert calls[0][1]["files"] == files
        assert calls[0][1]["tags"] == ["repo:test"]
        assert calls[0][1]["source_resource_id"] == "src/app.py:8"
        assert result["code_state"] == "unresolved"
        assert result["stored"] is True
    finally:
        await agent.close()


@pytest.mark.parametrize("field,value", [("files", "src/app.py"), ("tags", [9])])
async def test_bad_metadata_refuses_before_sending(field: str, value: object) -> None:
    agent, calls = _agent({})
    try:
        with pytest.raises(LoomError):
            await agent.tools().execute(
                "loom_remember", {"memory_key": "k", "value": "v", field: value}
            )
        assert not calls
    finally:
        await agent.close()


async def test_update_is_opt_in_and_keeps_the_server_receipt() -> None:
    receipt = {"updated": True, "memory_id": "m", "new_version": 9, "content_sha256": "new"}
    agent, calls = _agent(receipt)
    try:
        assert len(agent.tools().definitions()) == 6
        assert not agent.tools().has("loom_update")
        tools = agent.tools(only=["loom_update"], user_id="user-one", session_id="new-session")
        assert tools.has("loom_update")
        result = json.loads(
            await tools.execute(
                "loom_update",
                {
                    "memory_key": "app/rationale",
                    "value": "Corrected fact",
                    "if_match_sha256": "old",
                },
            )
        )
        assert result == receipt
        assert calls == [
            (
                "/v1/memory.update",
                {
                    "org": "test",
                    "namespace": "memory",
                    "agent": "*",
                    "user_id": "user-one",
                    "session_id": "-",
                    "run_id": "-",
                    "memory_key": "app/rationale",
                    "value": "Corrected fact",
                    "if_match_sha256": "old",
                },
            )
        ]
    finally:
        await agent.close()


async def test_update_ambiguity_is_not_retried_as_a_new_fact() -> None:
    agent, calls = _agent({"detail": {"code": "AMBIGUOUS_KEY", "message": "narrow the key"}}, 409)
    try:
        with pytest.raises(LoomError):
            await agent.tools(only=["loom_update"], user_id="bound-user").execute(
                "loom_update", {"memory_key": "k", "value": "v"}
            )
        assert len(calls) == 1
        assert calls[0][0] == "/v1/memory.update"
    finally:
        await agent.close()


async def test_model_metadata_cannot_replace_the_bound_identity() -> None:
    agent, calls = _agent({"memory_id": "m", "version": 1})
    try:
        await agent.tools(user_id="bound-user").execute(
            "loom_remember",
            {
                "memory_key": "k",
                "value": "v",
                "org": "other",
                "namespace": "other",
                "user_id": "other-user",
                "files": None,
                "source_resource_id": None,
            },
        )
        assert calls[0][1]["org"] == "test"
        assert calls[0][1]["namespace"] == "memory"
        assert calls[0][1]["user_id"] == "bound-user"
        assert "files" not in calls[0][1]
        assert "source_resource_id" not in calls[0][1]
    finally:
        await agent.close()


@pytest.mark.parametrize("user_id", [None, "", "-", "  ", " user ", "\u001fuser"])
async def test_key_correction_cannot_widen_an_unnamed_user_scope(user_id: str | None) -> None:
    agent, calls = _agent({})
    try:
        with pytest.raises(LoomError, match="application-bound named user_id"):
            agent.tools(only=["loom_update"], user_id=user_id)
        assert calls == []
    finally:
        await agent.close()
