"""End-to-end check for the two-mode memory capture split.

memory.set is the autonomous-mode path (Claude-judged; default `pinned=False`).
memory.pin is the explicit-mode path (user-driven; forces `pinned=True`).
Both write to `loom.memory`; only the `pinned` column differs. The
dashboard renders a PINNED badge on the explicit ones.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx
import pytest

from tests.integration._resources import unique_org

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# No flaky-rerun marker: every read-after-write below goes through `_list_until`,
# which polls memory.list until the freshly-written key is visible (RMT FINAL view
# of a new insert can lag a beat).


def _mint(namespace: str) -> str:
    from server.auth.jwt import mint_token

    return mint_token(
        subject="test-memory-pin",
        org="test-org",
        namespaces=[namespace],
        roles=["writer"],
        ttl_seconds=600,
        is_service_account=True,
    )


def _post(base_url: str, path: str, token: str, body: dict) -> dict:
    r = httpx.post(
        f"{base_url}{path}",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    assert r.status_code == 200, f"{path} {r.status_code}: {r.text}"
    return r.json()


async def _list_until(
    base_url: str, token: str, identity: dict, expect_key: str, timeout_s: float = 15.0
) -> dict:
    """memory.list with retry — CH Cloud's FINAL view of fresh inserts can lag a beat."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    last = None
    while asyncio.get_event_loop().time() < deadline:
        last = _post(base_url, "/v1/memory.list", token, identity)
        if any(e["memory_key"] == expect_key for e in last["entries"]):
            return last
        await asyncio.sleep(0.2)
    return last  # let caller assert / fail meaningfully


@pytest.mark.asyncio
async def test_memory_set_defaults_pinned_false() -> None:
    """memory.set via HTTP writes a row with pinned=False."""
    base = os.environ.get("LOOM_BASE_URL")
    if not base:
        pytest.skip("LOOM_BASE_URL not set — start the control plane first")
    ns = f"pintest-{os.urandom(4).hex()}"
    tok = _mint(ns)
    identity = {"org": "test-org", "namespace": ns, "agent": "test-agent"}

    body = {
        **identity,
        "memory_key": "auto_probe",
        "memory_type": "semantic",
        "value": "autonomous",
    }
    out = _post(base, "/v1/memory.set", tok, body)
    assert out.get("pinned") is False, out

    listing = await _list_until(base, tok, identity, expect_key="auto_probe")
    row = next((e for e in listing["entries"] if e["memory_key"] == "auto_probe"), None)
    assert row is not None, listing
    assert row["pinned"] is False, row


@pytest.mark.asyncio
async def test_memory_pin_writes_pinned_true() -> None:
    """memory.pin forces pinned=True regardless of input."""
    base = os.environ.get("LOOM_BASE_URL")
    if not base:
        pytest.skip("LOOM_BASE_URL not set — start the control plane first")
    ns = f"pintest-{os.urandom(4).hex()}"
    tok = _mint(ns)
    identity = {"org": "test-org", "namespace": ns, "agent": "test-agent"}

    body = {
        **identity,
        "memory_key": "pin_probe",
        "memory_type": "semantic",
        "value": "explicit",
    }
    out = _post(base, "/v1/memory.pin", tok, body)
    assert out.get("pinned") is True, out

    listing = await _list_until(base, tok, identity, expect_key="pin_probe")
    row = next((e for e in listing["entries"] if e["memory_key"] == "pin_probe"), None)
    assert row is not None, listing
    assert row["pinned"] is True, row


@pytest.mark.asyncio
async def test_both_modes_coexist_in_one_list() -> None:
    """One namespace holds both autonomous and pinned rows, distinguishable in list."""
    base = os.environ.get("LOOM_BASE_URL")
    if not base:
        pytest.skip("LOOM_BASE_URL not set — start the control plane first")
    ns = f"pintest-{os.urandom(4).hex()}"
    tok = _mint(ns)
    identity = {"org": "test-org", "namespace": ns, "agent": "test-agent"}

    _post(
        base,
        "/v1/memory.set",
        tok,
        {
            **identity,
            "memory_key": "coexist_auto",
            "memory_type": "semantic",
            "value": "autonomous",
        },
    )
    _post(
        base,
        "/v1/memory.pin",
        tok,
        {
            **identity,
            "memory_key": "coexist_pin",
            "memory_type": "semantic",
            "value": "pinned",
        },
    )

    # Poll for BOTH keys: async_insert can surface them out of order, so waiting
    # only for coexist_pin could observe a listing where coexist_auto isn't in yet.
    await _list_until(base, tok, identity, expect_key="coexist_auto")
    listing = await _list_until(base, tok, identity, expect_key="coexist_pin")
    by_key = {e["memory_key"]: e["pinned"] for e in listing["entries"]}
    assert by_key.get("coexist_auto") is False, by_key
    assert by_key.get("coexist_pin") is True, by_key


# The fields the dashboard's pin sends back from the row it read (frontend/src/api.ts
# pinMemory): memory.pin writes a whole new version, so a field left out is lost.
_PIN_ECHOED = (
    "value",
    "memory_type",
    "tags",
    "entities",
    "confidence",
    "sensitivity",
    "source_trace",
    "source_resource_id",
    "valid_at",
    "valid_to",
    "char_limit",
)


async def _get_until(base_url: str, token: str, body: dict, *, pinned: bool) -> dict:
    """memory.get with retry until the row is found with the given pin state."""
    deadline = asyncio.get_event_loop().time() + 15.0
    got: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        got = _post(base_url, "/v1/memory.get", token, body)
        if got.get("found") and got.get("pinned") is pinned:
            return got
        await asyncio.sleep(0.2)
    return got


@pytest.mark.asyncio
async def test_pin_of_a_read_row_keeps_its_fields(mint_test_token) -> None:
    """A pin that sends back what memory.get served keeps every carried field."""
    base = os.environ.get("LOOM_BASE_URL")
    if not base:
        pytest.skip("LOOM_BASE_URL not set — start the control plane first")
    org, ns = unique_org("memory-pin"), "pins"
    tok = mint_test_token(org=org, namespaces=[ns])
    key = {"org": org, "namespace": ns, "agent": "test-agent", "memory_key": "freeze"}

    _post(
        base,
        "/v1/memory.set",
        tok,
        {
            **key,
            "memory_type": "procedural",
            "value": "deploys freeze on Fridays",
            "tags": ["infra", "prod"],
            "entities": ["deploy"],
            "confidence": 0.8,
            "sensitivity": "medium",
            "source_trace": "conversation:t1",
            "source_resource_id": "doc-1",
            "valid_at": "2026-01-01T00:00:00Z",
            "valid_to": "2099-01-01T00:00:00Z",
            "char_limit": 200,
        },
    )
    before = await _get_until(base, tok, key, pinned=False)
    assert before.get("found"), before
    assert before["source_trace"] == "conversation:t1", before
    assert before["source_resource_id"] == "doc-1", before

    out = _post(base, "/v1/memory.pin", tok, {**key, **{f: before[f] for f in _PIN_ECHOED}})
    assert out.get("pinned") is True, out

    after = await _get_until(base, tok, key, pinned=True)
    assert after.get("pinned") is True, after
    assert {f: after[f] for f in _PIN_ECHOED} == {f: before[f] for f in _PIN_ECHOED}
    assert after["source_class"] == before["source_class"] == "extracted", after
