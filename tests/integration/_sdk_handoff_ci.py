"""Run the handoff example in CI's explicitly authenticated server jobs."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import httpx
import pytest
from loom import Agent

from tests.integration._resources import unique_org

EXAMPLE = Path(__file__).resolve().parents[2] / "sdk/python/examples/multi_agent_handoff.py"


async def test_handoff_example_uses_distinct_authenticated_principals(
    control_plane_url: str,
    mint_test_token: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Fail if this file is added to a job whose server ignores the tokens.
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=10) as client:
        assert (await client.get("/v1/me/service_accounts")).status_code == 401

    org, namespace = unique_org("sdk-handoff"), "handoff-example"
    planner_token = mint_test_token(
        org=org, namespaces=[namespace], roles=["admin"], subject="handoff-planner"
    )
    worker_token = mint_test_token(
        org=org, namespaces=[namespace], roles=["writer"], subject="handoff-worker"
    )
    async with Agent(
        org,
        namespace,
        "handoff-planner",
        base_url=control_plane_url,
        api_key=planner_token,
        auto_run=False,
    ) as planner:
        await planner.namespaces.register(owner_email="handoff@example.test")

    for name, value in {
        "LOOM_BASE_URL": control_plane_url,
        "LOOM_ORG": org,
        "LOOM_NAMESPACE": namespace,
        "LOOM_PLANNER_API_KEY": planner_token,
        "LOOM_WORKER_API_KEY": worker_token,
    }.items():
        monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location("native_handoff_example", EXAMPLE)
    assert spec is not None and spec.loader is not None
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    await example.main()

    output = capsys.readouterr().out
    for line in (
        "task: done",
        "result: 7 squared is 49",
        "writer agent: handoff-worker",
        "writer actor: process:handoff-worker",
        "source class: asserted",
        "cleanup: result memory is missing",
    ):
        assert line in output
    assert planner_token not in output and worker_token not in output
