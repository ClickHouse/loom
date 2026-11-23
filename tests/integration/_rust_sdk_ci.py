"""Exercise the Rust SDK against the authenticated integration server."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx

from tests.integration._resources import unique_org

ROOT = Path(__file__).resolve().parents[2]


async def test_rust_sdk_live_contracts(control_plane_url: str, mint_test_token: Any) -> None:
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=10) as client:
        assert (await client.get("/v1/me/service_accounts")).status_code == 401
    org = unique_org("rust-sdk")
    env = {
        **os.environ,
        "LOOM_RUST_LIVE_BASE_URL": control_plane_url,
        "LOOM_RUST_LIVE_ORG": org,
        "LOOM_RUST_LIVE_ADMIN_TOKEN": mint_test_token(
            org=org, namespaces=["*"], roles=["admin"], subject="rust-admin"
        ),
        "LOOM_RUST_LIVE_WRITER_TOKEN": mint_test_token(
            org=org, namespaces=["rust-live"], subject="rust-writer"
        ),
        "LOOM_RUST_LIVE_OTHER_TOKEN": mint_test_token(
            org=org, namespaces=["other"], subject="rust-other"
        ),
    }
    process = await asyncio.create_subprocess_exec(
        "cargo",
        "test",
        "--manifest-path",
        str(ROOT / "sdk/rust/Cargo.toml"),
        "--test",
        "live",
        "--test",
        "live_identity",
        "--features",
        "blocking",
        "--",
        "--ignored",
        "--nocapture",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=300)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, output.decode(errors="replace")
