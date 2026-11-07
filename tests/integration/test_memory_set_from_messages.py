"""End-to-end test of memory.set_from_messages — the LLM-extraction path.

Runs against a live Loom + CH like the other integration tests. The
test exercises whatever LLM provider the server is configured for —
fake-extract in CI without OPENAI_API_KEY, real openai in deployments
that set it. Both produce a parseable JSON response and at least one
extracted fact.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._eventually import eventually  # noqa: E402
from tests.integration._resources import minted_identity  # noqa: E402

# No flaky marker (fake-provider mode): set_from_messages writes to loom.memory
# synchronously and the fake extractor is deterministic; the memory.list read-back
# below is polled via eventually() to bridge any FINAL-visibility lag under load.
# NOTE: with a REAL LLM provider (OPENAI_API_KEY set) the exact extracted keys
# become provider-dependent — restore reruns for that mode if you run it in CI.


def _post(url: str, path: str, body: dict, token: str | None = None) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(base_url=url, timeout=20.0) as c:
        return c.post(path, json=body, headers=headers)


@pytest.mark.asyncio
async def test_set_from_messages_writes_extracted_facts(
    control_plane_url,
    mint_test_token,
) -> None:
    """A messages payload runs through the fake extractor and lands as
    one or more memory rows. memory.list then surfaces them by key."""
    suffix = uuid.uuid4().hex[:6]
    me = minted_identity(mint_test_token, f"sfm-{suffix}")

    r = _post(
        control_plane_url,
        "/v1/memory.set_from_messages",
        {
            "org": me["org"],
            "namespace": me["namespace"],
            "agent": "a",
            "user_id": "-",
            "messages": [
                {"role": "user", "content": "My dog Bowie is seven years old."},
                {"role": "assistant", "content": "Noted that Bowie is seven."},
                {
                    "role": "user",
                    "content": "Also we deploy on Tuesdays and Thursdays.",
                },
            ],
        },
        me["token"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Server may be configured for `fake` (CI default) or `openai`
    # (deployments with OPENAI_API_KEY). The response should surface
    # *some* provider name and the extracted facts should land; the
    # exact provider isn't load-bearing on this test's contract.
    assert body["llm_provider"] in {"fake", "openai", "anthropic"}, body
    assert len(body["written"]) >= 1, body
    keys_written = {row["memory_key"] for row in body["written"]}

    # The keys returned should be the same ones memory.list surfaces. Poll until
    # all written keys are visible (bridges any FINAL-visibility lag under load).
    async def _all_listed():
        lst = _post(
            control_plane_url,
            "/v1/memory.list",
            {
                "org": me["org"],
                "namespace": me["namespace"],
                "agent": "a",
                "limit": 50,
            },
            me["token"],
        )
        if lst.status_code != 200:
            return None
        keys = {e["memory_key"] for e in lst.json()["entries"]}
        return keys if keys_written <= keys else None

    keys_listed = await eventually(
        _all_listed, description="extracted keys to surface in memory.list"
    )
    assert keys_written <= keys_listed, (
        f"extracted keys {keys_written} missing from list {keys_listed}"
    )


@pytest.mark.asyncio
async def test_set_from_messages_empty_returns_no_writes(
    control_plane_url,
    mint_test_token,
) -> None:
    """Empty messages list is a 400 (Pydantic validation): min_length=1."""
    suffix = uuid.uuid4().hex[:6]
    me = minted_identity(mint_test_token, f"sfm-empty-{suffix}")

    r = _post(
        control_plane_url,
        "/v1/memory.set_from_messages",
        {
            "org": me["org"],
            "namespace": me["namespace"],
            "agent": "a",
            "messages": [],
        },
        me["token"],
    )
    assert r.status_code in (400, 422), r.text


@pytest.mark.asyncio
async def test_set_from_messages_extra_tags_merge(
    control_plane_url,
    mint_test_token,
) -> None:
    """`extra_tags` are unioned with the per-fact extracted tags."""
    suffix = uuid.uuid4().hex[:6]
    me = minted_identity(mint_test_token, f"sfm-tags-{suffix}")

    r = _post(
        control_plane_url,
        "/v1/memory.set_from_messages",
        {
            "org": me["org"],
            "namespace": me["namespace"],
            "agent": "a",
            "user_id": "-",
            "messages": [{"role": "user", "content": "Bowie is my dog and he is seven."}],
            "extra_tags": ["session-2026-05-20"],
        },
        me["token"],
    )
    assert r.status_code == 200, r.text
    written = r.json()["written"]
    assert written
    written_keys = {row["memory_key"] for row in written}

    # Poll until every written key is visible in list, then assert the tag merge.
    async def _all_listed():
        lst = _post(
            control_plane_url,
            "/v1/memory.list",
            {
                "org": me["org"],
                "namespace": me["namespace"],
                "agent": "a",
                "limit": 50,
            },
            me["token"],
        )
        if lst.status_code != 200:
            return None
        by_key = {e["memory_key"]: e for e in lst.json()["entries"]}
        return by_key if written_keys <= set(by_key) else None

    entries_by_key = await eventually(
        _all_listed, description="written keys to surface in memory.list"
    )
    for row in written:
        tags = entries_by_key[row["memory_key"]]["tags"]
        assert "session-2026-05-20" in tags, tags
