"""The identity sentinel `Agent.call` stamps when a call names no run, user or
session ("-") reads as unset: memory.search with `run_id: "-"` reads every run's
rows, memory.update with `user_id` / `session_id` "-" (or `agent: "*"`) resolves
the key across the namespace, a startup context.pack reads every user's rows, and
run.begin mints an id of its own. A named value still narrows."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


from tests.integration._eventually import eventually  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("id-sentinel")


@pytest.mark.asyncio
async def test_search_with_the_run_sentinel_reads_rows_written_inside_a_run(
    control_plane_url: str, mint_test_token
) -> None:
    from loom import Agent

    ns = f"rs-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG)
    async with Agent(
        org=ORG, namespace=ns, agent="w", base_url=control_plane_url, api_key=tok
    ) as a:
        run_id = (await a.run.begin(goal="Pick the deploy window"))["run_id"]
        await a.memory.set(
            "deploy:window",
            "Deploys go out on Tuesday mornings.",
            memory_type="semantic",
            run_id=run_id,
        )
        await a.memory.set(
            "deploy:freeze", "Deploys pause during the Tuesday freeze.", memory_type="semantic"
        )

    # Another agent reads every author's rows, so the writer's read-your-writes pass
    # cannot add the rows back.
    body = {"agent": "*", "query": "Tuesday deploys", "top_k": 10, "auto_touch": False}
    async with Agent(
        org=ORG, namespace=ns, agent="r", base_url=control_plane_url, api_key=tok
    ) as a:

        async def keys(want: set[str], **scope: str) -> set[str]:
            out = await a.call("memory.search", body, **scope)
            found = {h["memory_key"] for h in out["results"]}
            return found if found == want else set()

        # Embeddings land a moment after the write, so each read is polled.
        await eventually(
            lambda: keys({"deploy:window"}, run_id=run_id),
            description="a named run_id reads only that run's row",
        )
        await eventually(
            lambda: keys({"deploy:window", "deploy:freeze"}),
            description='the stamped run_id "-" reads every run\'s rows',
        )


@pytest.mark.asyncio
async def test_update_with_the_identity_sentinels_resolves_the_key_across_identities(
    control_plane_url: str, mint_test_token
) -> None:
    from loom import Agent

    ns = f"us-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG)
    async with Agent(
        org=ORG, namespace=ns, agent="w", base_url=control_plane_url, api_key=tok
    ) as a:
        await a.memory.set(
            "deploy:day",
            "Deploys go out on Tuesday.",
            memory_type="semantic",
            user_id="u1",
            session_id="s1",
        )
        # The writer's own call stamps user_id and session_id "-".
        own = await a.call("memory.update", {"memory_key": "deploy:day", "value": "Wednesday."})

    async with Agent(
        org=ORG, namespace=ns, agent="r", base_url=control_plane_url, api_key=tok
    ) as a:
        other = await a.call(
            "memory.update", {"agent": "*", "memory_key": "deploy:day", "value": "Thursday."}
        )

    async with Agent(
        org=ORG, namespace=ns, agent="w", base_url=control_plane_url, api_key=tok
    ) as a:
        got = await a.memory.get("deploy:day", user_id="u1", session_id="s1")

    assert (own["updated"], own["reason"]) == (True, "")
    assert other["updated"] is True and other["new_version"] > own["new_version"]
    assert got is not None and got["value"] == "Thursday."


@pytest.mark.asyncio
async def test_startup_pack_with_the_identity_sentinels_reads_every_users_rows(
    control_plane_url: str, mint_test_token
) -> None:
    from loom import Agent

    ns = f"sp-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG)
    async with Agent(
        org=ORG, namespace=ns, agent="w", base_url=control_plane_url, api_key=tok
    ) as a:
        await a.memory.set(
            "deploy:day",
            "Deploys go out on Tuesday.",
            memory_type="semantic",
            user_id="u1",
            session_id="s1",
        )

        # `Agent.call` stamps user_id and session_id "-".
        async def memories() -> str:
            pack = await a.call("context.pack", {"mode": "startup", "query": "", "top_k": 5})
            text = pack["sections"].get("memories", "")
            return text if "Deploys go out on Tuesday." in text else ""

        await eventually(memories, description="the startup pack recalls another user's row")


@pytest.mark.asyncio
async def test_run_begin_with_the_run_sentinel_opens_a_run_of_its_own(
    control_plane_url: str, mint_test_token
) -> None:
    from loom import Agent

    ns = f"rb-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG)
    async with Agent(
        org=ORG, namespace=ns, agent="w", base_url=control_plane_url, api_key=tok
    ) as a:
        # `Agent.call` stamps run_id "-": each begin gets a server-made id.
        first = await a.call("run.begin", {"goal": "Pick the deploy window"})
        second = await a.call("run.begin", {"goal": "Pick the deploy day"})
        got = await a.call("run.get", {"run_id": first["run_id"]})

    assert "-" not in (first["run_id"], second["run_id"])
    assert first["run_id"] != second["run_id"]
    assert got["run"]["goal"] == "Pick the deploy window"
