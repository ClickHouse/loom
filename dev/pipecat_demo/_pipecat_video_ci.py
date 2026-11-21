"""Film one text turn with the native qualification assertions and observed values."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from loom import Agent
from loom.session_protection import SessionProtection

from tests.integration._resources import unique_org

EXAMPLE = Path(__file__).resolve().parents[2] / "sdk/python/examples/pipecat_text_memory.py"


def write_json_exclusive(path: Path, payload: Any) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out:
        json.dump(payload, out, indent=2)


async def test_pipecat_text_example_recalls_and_stores_the_real_turn(
    control_plane_url: str,
    mint_test_token: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ch_client: Any,
) -> None:
    # This explicit CI-only case does not adopt a developer's found local service.
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    url = urlsplit(control_plane_url)
    assert url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"} and url.port == 7778
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=10) as client:
        assert (await client.get("/v1/me/service_accounts")).status_code == 401
        health = (await client.get("/v1/health")).json()
        assert health.get("fake_llm") is True and health.get("fake_embedding") is True

    org, namespace, app, user, conversation = (
        unique_org("pipecat-demo"),
        "text-demo",
        "pipecat-demo",
        "user-a",
        "call-a",
    )
    # A brand-new independent application family exists before its first content.
    # The example itself only reopens this pointer; no existing state is replaced.
    family = await asyncio.to_thread(SessionProtection.create, base=tmp_path / "family-store")
    pointer_path = tmp_path / "pointer.json"
    await asyncio.to_thread(write_json_exclusive, pointer_path, family.pointer())
    admin = mint_test_token(
        org=org, namespaces=[namespace], roles=["admin"], subject="pipecat-demo-setup"
    )
    token = mint_test_token(
        org=org, namespaces=[namespace], roles=["writer"], subject="pipecat-demo-app"
    )
    async with Agent(
        org, namespace, app, base_url=control_plane_url, api_key=admin, auto_run=False
    ) as setup:
        await setup.namespaces.register(owner_email="pipecat-demo@example.test")
        await setup.memory.set("seat", "I prefer aisle seats.", user_id=user)
    scope = {"org": org, "namespace": namespace, "agent": app, "user": user}
    sql = (
        "SELECT evidence_id,org,namespace,agent,user_id,session_id,role,content,actor,source_type "
        "FROM loom.memory_evidence FINAL WHERE org=%(org)s AND namespace=%(namespace)s "
        "AND agent=%(agent)s AND user_id=%(user)s AND tombstoned_at IS NULL"
    )
    before = {
        str(row[0]): tuple(row[1:]) for row in ch_client.query(sql, parameters=scope).result_rows
    }

    for name, value in {
        "LOOM_BASE_URL": control_plane_url,
        "LOOM_ORG": org,
        "LOOM_NAMESPACE": namespace,
        "LOOM_AGENT": app,
        "LOOM_API_KEY": token,
        "LOOM_USER_ID": user,
        "LOOM_CONVERSATION_ID": conversation,
        "LOOM_SESSION_POINTER_FILE": str(pointer_path),
    }.items():
        monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location("native_pipecat_example", EXAMPLE)
    assert spec is not None and spec.loader is not None
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    context = await example.main()
    assert any(
        isinstance(message, dict)
        and message.get("role") == "developer"
        and example.FACT in str(message.get("content", ""))
        for message in context.get_messages()
    )
    assert not family.protected

    native_session = (
        "pipecat-" + hashlib.sha256(json.dumps([user, conversation]).encode()).hexdigest()[:20]
    )
    after = {
        str(row[0]): tuple(row[1:]) for row in ch_client.query(sql, parameters=scope).result_rows
    }
    assert all(after.get(key) == value for key, value in before.items())
    rows = sorted(value for key, value in after.items() if key not in before)
    assert rows == [
        (
            org,
            namespace,
            app,
            user,
            native_session,
            "assistant",
            example.REPLY,
            "assistant",
            "message",
        ),
        (
            org,
            namespace,
            app,
            user,
            native_session,
            "user",
            example.FACT,
            "user",
            "message",
        ),
    ]
    recalled = next(
        str(message["content"])
        for message in context.get_messages()
        if isinstance(message, dict)
        and message.get("role") == "developer"
        and example.FACT in str(message.get("content", ""))
    )
    start = recalled.index(example.FACT)
    observations = {
        "recalled_seed": recalled[start : start + len(example.FACT)],
        "persisted": [{"role": row[5], "content": row[6]} for row in rows],
    }
    receipt = {
        "observations": observations,
        "result": "PASS",
        "pipecat": importlib.metadata.version("pipecat-ai"),
        "sdk_source": "checked-out candidate selected by tests/conftest.py",
        "real_agent": True,
        "auth_on": True,
        "fake_llm": True,
        "fake_embedding": True,
        "recalled_seed_in_native_context": True,
        "stored_native_messages": len(rows),
        "protected_family_audio_interruption_video_registry_qualified": False,
    }
    path = Path(os.environ["LOOM_PIPECAT_EVIDENCE"])
    await asyncio.to_thread(write_json_exclusive, path, receipt)
