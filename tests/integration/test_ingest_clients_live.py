"""The Python SDK's file ingest and the ingest CLI against a running server and its
ClickHouse: the bytes are uploaded as they are, the worker pass (run in this process)
sends them to the extractor at LOOM_DOCUMENT_EXTRACTOR_URL, and the document is
published as a revision the SDK and the CLI report.

- `ingest_file` publishes a Markdown file, and `wait_for_ingest` returns the job once
  it is ready, with the revision `knowledge.get` serves.
- The CLI ingests a file and waits for it, printing the revision it published; a second
  run publishes the next revision; `--no-wait` prints the job and returns.
- `loom demo` uploads its file, waits for the job and finds the passage it asked about.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests.integration._resources import unique_org

pytestmark = [
    pytest.mark.usefixtures("ch_target_required"),
    pytest.mark.skipif(
        not os.environ.get("LOOM_DOCUMENT_EXTRACTOR_URL"),
        reason="LOOM_DOCUMENT_EXTRACTOR_URL names no extractor for the worker pass",
    ),
]

RUNBOOK = b"# Runbook\n\n## Restarts\n\nRestart the ingest worker after a config change.\n"


async def _drain(until: Any) -> None:
    """Run worker passes (one due job each) until `until()` is true."""
    from server.core.ch import get_client
    from server.knowledge.workers import ingest

    ch = get_client()
    await ch.connect()
    try:
        for _ in range(60):
            if await until():
                return
            await ingest.run_pass()
            await asyncio.sleep(0.2)
    finally:
        await ch.disconnect()
    raise AssertionError("the job did not end")


@pytest.mark.asyncio
async def test_ingest_file_publishes_a_revision_the_sdk_waits_for(
    control_plane_url: str, mint_test_token: Any, tmp_path: Path
) -> None:
    from loom import Agent

    org, ns = unique_org("ingest-sdk"), f"docs-{uuid.uuid4().hex[:6]}"
    doc = tmp_path / "runbook.md"
    doc.write_bytes(RUNBOOK)
    token = mint_test_token(org=org, namespaces=[ns], roles=["writer"])
    async with Agent(org, ns, "ingest-test", base_url=control_plane_url, api_key=token) as a:
        job = await a.knowledge.ingest_file(doc, wait=False, tags=["ops"])

        async def ended() -> bool:
            status = await a.knowledge.ingest_status(job["ingestion_id"])
            return status["state"] not in ("pending", "processing")

        await _drain(ended)
        status = await a.knowledge.wait_for_ingest(job["ingestion_id"], timeout_s=5)
        assert (status["state"], status["revision"], status["format"]) == ("ready", 1, "markdown")
        assert status["chunks"] >= 1 and status["filename"] == "runbook.md"
        got = await a.knowledge.get(f"file://{doc.resolve()}")
        assert got["revision"] == 1 and got["format"] == "markdown" and got["tags"] == ["ops"]
        assert "Restart the ingest worker" in got["content"]


@pytest.mark.asyncio
async def test_the_cli_uploads_waits_and_publishes_the_next_revision(
    control_plane_url: str, mint_test_token: Any, tmp_path: Path, capfd: Any
) -> None:
    from loom import Agent

    from dev import loom_ingest

    org, ns = unique_org("ingest-cli"), f"docs-{uuid.uuid4().hex[:6]}"
    doc = tmp_path / "runbook.md"
    doc.write_bytes(RUNBOOK)
    token = mint_test_token(org=org, namespaces=[ns], roles=["writer"])
    args = [str(doc), "--base-url", control_plane_url, "--token", token, "--org", org]
    args += ["--namespace", ns, "--source-uri", "repo://runbook.md", "--timeout", "60"]

    for revision in (1, 2):
        cli = asyncio.ensure_future(asyncio.to_thread(loom_ingest.main, args))

        async def done(cli: asyncio.Future[int] = cli) -> bool:
            return cli.done()

        await _drain(done)
        assert await cli == 0
        assert f"revision={revision} " in capfd.readouterr().out

    assert await asyncio.to_thread(loom_ingest.main, [*args, "--no-wait"]) == 0
    assert "queued → ingestion_id=" in capfd.readouterr().out
    async with Agent(org, ns, "ingest-test", base_url=control_plane_url, api_key=token) as a:
        got = await a.knowledge.get("repo://runbook.md")
    assert (got["revision"], got["title"]) == (2, "runbook")
    assert got["metadata"]["ingested_from"] == f"file://{doc.resolve()}"


@pytest.mark.asyncio
async def test_loom_demo_ingests_its_file_and_searches_it(
    control_plane_url: str, mint_test_token: Any, tmp_path: Path, capfd: Any
) -> None:
    import argparse

    import server.cli.demo as demo

    org, ns = unique_org("ingest-demo"), f"docs-{uuid.uuid4().hex[:6]}"
    (tmp_path / "AGENTS.md").write_bytes(RUNBOOK)
    token = mint_test_token(org=org, namespaces=[ns], roles=["writer"])
    parser = argparse.ArgumentParser()
    demo.register_demo_cli(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(
        ["demo", "--base-url", control_plane_url, "--token", token, "--org", org]
        + ["--namespace", ns, "--root", str(tmp_path), "--search-only"]
        + ["--question", "When is the ingest worker restarted?"]
    )
    run = asyncio.ensure_future(asyncio.to_thread(demo._cmd_demo, args))

    async def done() -> bool:
        return run.done()

    await _drain(done)
    assert await run == 0
    out = capfd.readouterr().out
    assert "ingested AGENTS.md → revision=1" in out and "format=markdown state=ready" in out
    assert "AGENTS.md#" in out and "Restart the ingest worker" in out
