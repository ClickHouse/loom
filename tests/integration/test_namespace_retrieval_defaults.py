"""`namespace.register(retrieval_defaults=…)` is what an
unpinned memory.search on that namespace runs; a pinned field is the
caller's; `{}` clears; an omitted field keeps the stored value."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("ns-rd")
DEFAULTS = {
    "search_mode": "rrf",
    "top_k": 3,
    "auto_touch": False,
    "include_tags": True,
    "bodies": "excerpt",
}


def _plan_bits(res: dict) -> dict[str, str]:
    return dict(b.split("=", 1) for b in (res.get("retrieval_plan") or "").split(", ") if "=" in b)


@pytest.mark.asyncio
async def test_namespace_defaults_fill_unpinned_fields_only(
    control_plane_url: str, mint_test_token
) -> None:
    from loom import Agent

    ns = f"rd-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=["*"], roles=["admin", "writer"])
    async with Agent(
        org=ORG, namespace=ns, agent="w", base_url=control_plane_url, api_key=tok
    ) as a:
        await a.namespaces.register(owner_email="ops@example.com", retrieval_defaults=DEFAULTS)
        info = await a.namespaces.get()
        assert info is not None and info["retrieval_defaults"] == DEFAULTS

        for i in range(5):
            await a.memory.set(
                memory_key=f"fact_{i}",
                value=f"Deploy window {i}: Tuesday and Thursday, never Friday (rule {i}).",
                memory_type="semantic",
                tags=["kind:rule", f"n:{i}"],
            )

        # unpinned: the namespace's knobs, named on the plan
        blank = await a.transport.post(
            "/v1/memory.search",
            {"org": ORG, "namespace": ns, "agent": "w", "query": "deploy window rule"},
        )
        bits = _plan_bits(blank)
        assert bits["mode"] == "rrf", blank.get("retrieval_plan")
        assert bits["ns_defaults"] == "search_mode,top_k,auto_touch,include_tags,bodies"
        assert 0 < len(blank["results"]) <= 3
        assert all("tags" in h for h in blank["results"])

        # pinned: the caller's values, nothing from the namespace
        pinned = await a.transport.post(
            "/v1/memory.search",
            {
                "org": ORG,
                "namespace": ns,
                "agent": "w",
                "query": "deploy window rule",
                "search_mode": "semantic",
                "top_k": 5,
                "include_tags": False,
                "bodies": "full",
                "auto_touch": True,
            },
        )
        assert _plan_bits(pinned)["mode"] == "semantic"
        assert "ns_defaults" not in pinned["retrieval_plan"]
        assert len(pinned["results"]) == 5
        assert all("tags" not in h for h in pinned["results"])

        # a settings-only re-register keeps the stored defaults; {} clears them
        await a.namespaces.register(owner_email="ops@example.com", description="settings only")
        assert (await a.namespaces.get())["retrieval_defaults"] == DEFAULTS
        await a.namespaces.register(owner_email="ops@example.com", retrieval_defaults={})
        assert (await a.namespaces.get())["retrieval_defaults"] is None
        cleared = await a.transport.post(
            "/v1/memory.search",
            {"org": ORG, "namespace": ns, "agent": "w", "query": "deploy window rule"},
        )
        assert "ns_defaults" not in cleared["retrieval_plan"]
        assert len(cleared["results"]) == 5  # the wire default k

        # a misspelt knob is a 422, not a silent no-op
        with pytest.raises(Exception) as excinfo:
            await a.namespaces.register(
                owner_email="ops@example.com", retrieval_defaults={"topk": 3}
            )
        assert "422" in str(excinfo.value) or "validation" in str(excinfo.value).lower()
