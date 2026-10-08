"""Every key a search returns is a key a read can answer.

The contract a consumer relies on without reading the code: `memory.search`
hands back keys, `memory.get` / `memory.bulk_get` resolve them. Long
`memory.set` values get verbatim chunks whose ids
(`chunk:memory.set:<key>:<n>`) both reads must understand, and only a test
that walks the round trip catches a miss. This one does, against a live server: a value long
enough to chunk, a query that only the tail of the value answers, then every
returned key through both reads, and the chunk's parent named on the hit.

Runs under the suite's own minted token (the `mint_test_token` fixture), so it
gates in CI, where self-signup is not open.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


def _post(url: str, path: str, body: dict, token: str | None = None) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(base_url=url, timeout=20.0) as c:
        return c.post(path, json=body, headers=headers)


from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("keys-resolve")


def _token(mint_test_token) -> str:
    """The suite's convention (see the `agent` fixture): an operator-supplied
    LOOM_API_KEY wins, else a token minted with the server's own secret."""
    return os.environ.get("LOOM_API_KEY") or mint_test_token(org=ORG, namespaces=["*"])


# A tail sentence no head excerpt contains: the fake n-gram embedder scores a
# query by shared character trigrams, so this text finds the chunk that holds
# it and not the value's first 1,200 characters.
_TAIL = (
    "zorbulent quixative marmalade lattice renders the keeper handshake idempotent "
    "under a split-brain lease rotation"
)


def _long_value() -> str:
    head = " ".join(
        f"Paragraph {i}: the merge scheduler drains the pending queue before it "
        f"admits a new part, and the part log records every decision with its reason."
        for i in range(14)
    )
    assert len(head) > 1200  # past EXCERPT_LIMIT: the tail lives only in chunks
    return head + "\n\n" + _TAIL + "\n\n" + _TAIL.upper()


@pytest.mark.asyncio
async def test_every_returned_key_resolves_and_a_chunk_hit_names_its_parent(
    control_plane_url: str, mint_test_token
) -> None:
    suffix = uuid.uuid4().hex[:6]
    token = _token(mint_test_token)
    ident = {"org": ORG, "namespace": f"rt-{suffix}", "agent": "rt"}
    key = f"lesson_roundtrip_{suffix}"

    r = _post(
        control_plane_url,
        "/v1/memory.set",
        {**ident, "memory_key": key, "memory_type": "semantic", "value": _long_value()},
        token,
    )
    assert r.status_code == 200, r.text
    assert int(r.json().get("chunks_written") or 0) >= 2, r.json()

    # The query only the tail answers; no cap, so the chunk plane may show.
    r = _post(
        control_plane_url,
        "/v1/memory.search",
        {
            **ident,
            "query": _TAIL,
            "top_k": 10,
            "chunk_max_share": 1.0,
            "auto_touch": False,
        },
        token,
    )
    assert r.status_code == 200, r.text
    hits = r.json().get("results") or []
    assert hits, "the tail sentence found nothing"
    keys = [h["memory_key"] for h in hits]
    assert key in keys or any(k.startswith(f"chunk:memory.set:{key}:") for k in keys), keys

    # Every chunk hit names the fact it was cut from.
    for h in hits:
        if str(h["memory_key"]).startswith("chunk:"):
            assert h.get("source") == "chunk", h
            assert h.get("parent_memory_key") == key, h

    # The contract: memory.get answers every key the search returned…
    for k in keys:
        g = _post(control_plane_url, "/v1/memory.get", {**ident, "memory_key": k}, token)
        assert g.status_code == 200, (k, g.text)
        body = g.json()
        assert body.get("found") is True, (k, body)
        if k.startswith("chunk:"):
            assert body["value"]["parent_memory_key"] == key, body

    # …and so does bulk_get, in one call, with a chunk id folded onto its parent.
    b = _post(control_plane_url, "/v1/memory.bulk_get", {**ident, "memory_keys": keys}, token)
    assert b.status_code == 200, b.text
    out = b.json()
    assert out["missing"] == [], out
    assert set(out["results"]) == set(keys), out
    for k in keys:
        if k.startswith("chunk:"):
            assert out["results"][k].get("canonical_key") == key, out["results"][k]


@pytest.mark.asyncio
async def test_a_zero_chunk_cap_is_a_fact_only_read(
    control_plane_url: str, mint_test_token
) -> None:
    """`chunk_max_share=0` returns no chunk keys whatever the deployment's chunk
    floor says (the floor never exceeds the cap)."""
    suffix = uuid.uuid4().hex[:6]
    token = _token(mint_test_token)
    ident = {"org": ORG, "namespace": f"rt0-{suffix}", "agent": "rt"}
    key = f"lesson_factonly_{suffix}"
    r = _post(
        control_plane_url,
        "/v1/memory.set",
        {**ident, "memory_key": key, "memory_type": "semantic", "value": _long_value()},
        token,
    )
    assert r.status_code == 200, r.text
    r = _post(
        control_plane_url,
        "/v1/memory.search",
        {**ident, "query": _TAIL, "top_k": 10, "chunk_max_share": 0.0, "auto_touch": False},
        token,
    )
    assert r.status_code == 200, r.text
    keys = [h["memory_key"] for h in (r.json().get("results") or [])]
    assert not [k for k in keys if k.startswith("chunk:")], keys
