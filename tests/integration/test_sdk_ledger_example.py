"""The ledger example (`examples/ledger_run.py`) runs against
the integration server and leaves an `agent_runs` row with a non-empty
`memories_used` — the credit-assignment signal the run ledger exists for."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


def _load_example():
    spec = importlib.util.spec_from_file_location("ledger_run", ROOT / "examples" / "ledger_run.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_ledger_example_yields_a_run_with_memories_used(
    control_plane_url: str, mint_test_token
) -> None:
    from loom import Agent

    example = _load_example()
    ns = f"ledger-ex-{os.urandom(4).hex()}"
    tok = mint_test_token(org="test-org", namespaces=["*"])

    out = await example.run_task(
        org="test-org", namespace=ns, base_url=control_plane_url, api_key=tok
    )
    assert out["run_id"], out
    assert out["memories_used"], "the example must name what it relied on"
    assert example.DEPLOY_KEY in out["memories_used"]
    assert out["completed"] is not None and out["completed"].get("status") == "completed"

    async with Agent(
        "test-org", ns, "ledger-example", base_url=control_plane_url, api_key=tok
    ) as agent:
        got = await agent.run.get(out["run_id"])
    assert got.get("found") is True, got
    row = got["run"]
    assert row["status"] == "completed"
    assert row["memories_used"], row
    assert example.DEPLOY_KEY in row["memories_used"]
    assert row["final_response"] == "Ship on Thursday."
    # The write inside the block carried the run_id.
    written = list(got.get("memories_written") or []) + list(row.get("memories_written") or [])
    assert any(str(k).startswith("decision:ship-day:") for k in written), got
