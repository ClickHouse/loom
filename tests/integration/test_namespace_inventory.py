"""namespace.inventory over real rows: counts per plane, members apart from the identities
observed on rows (members counted per person, as `grants.list` folds them), the unset
user never a subject, and a token pinned to one user counting
only that user's rows while documents and members are left out.

Skips without LOOM_BASE_URL (tests/integration/conftest.py).
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org


def _mint(org: str, ns: str, **claims: Any) -> str:
    from server.auth.jwt import mint_token

    return mint_token(
        subject=f"sa:inventory-{uuid.uuid4().hex[:6]}",
        org=org,
        namespaces=[ns],
        roles=["admin"],
        ttl_seconds=900,
        is_service_account=True,
        **claims,
    )


def _call(c: httpx.Client, token: str, op: str, body: dict[str, Any]) -> Any:
    r = c.post(f"/v1/{op}", json=body, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, f"{op}: {r.status_code} {r.text[:400]}"
    return r.json()


@pytest.fixture
def seeded(control_plane_url: str) -> Any:
    org, ns = unique_org("inventory"), "inventory"
    wide = _mint(org, ns)
    with httpx.Client(base_url=control_plane_url, timeout=60.0) as c:
        for user, n in (("u1", 2), ("u2", 1), ("-", 1)):
            for i in range(n):
                _call(
                    c,
                    wide,
                    "memory.set",
                    {
                        "org": org,
                        "namespace": ns,
                        "agent": "bot",
                        "user_id": user,
                        "memory_key": f"pref/{user}/{i}",
                        "value": f"{user} prefers option {i} for the weekly report",
                    },
                )
        for member in ("reader@example.com", "Reader@Example.com", "editor@example.com"):
            body = {"org": org, "namespace": ns, "subject": member, "role": "viewer"}
            _call(c, wide, "grants.create", body)
        _call(
            c,
            wide,
            "knowledge.add",
            {
                "org": org,
                "namespace": ns,
                "source_uri": "docs/report.md",
                "content": "# Weekly report\n\nThe weekly report lists open items.",
            },
        )
        yield c, org, ns, wide


def test_the_inventory_counts_planes_and_keeps_populations_apart(seeded: Any) -> None:
    c, org, ns, wide = seeded
    out = _call(c, wide, "namespace.inventory", {"org": org, "namespace": ns})
    sections = {s["plane"]: s for s in out["sections"]}
    assert sections["memories"]["count"] == 4 and sections["memories"]["latest_at"]
    assert sections["documents"]["count"] == 1
    assert sections["records"]["count"] == 0 and sections["records"]["latest_at"] is None
    assert all(s["complete"] for s in out["sections"]), out["sections"]
    pops = {p["population"]: p for p in out["populations"]}
    subjects = {i["name"]: i["rows"] for i in pops["subjects"]["top"]}
    assert subjects == {"u1": 2, "u2": 1} and pops["subjects"]["total"] == 2
    assert [i["name"] for i in pops["agents"]["top"]] == ["bot"]
    assert pops["authors"]["total"] >= 1
    # Two people (one under two spellings), neither of whom wrote a row.
    assert (pops["members"]["total"], pops["members"]["complete"]) == (2, True)


def test_a_token_pinned_to_one_user_counts_only_that_user(seeded: Any) -> None:
    c, org, ns, _ = seeded
    pinned = _mint(org, ns, read_user_id="u1")
    out = _call(c, pinned, "namespace.inventory", {"org": org, "namespace": ns})
    assert (out["scope"]["read_scope"], out["scope"]["user_id"]) == ("own_user", "u1")
    sections = {s["plane"]: s for s in out["sections"]}
    assert sections["memories"]["count"] == 2
    for plane in ("documents", "integrations"):
        assert sections[plane]["count"] is None and not sections[plane]["complete"]
    pops = {p["population"]: p for p in out["populations"]}
    assert [i["name"] for i in pops["subjects"]["top"]] == ["u1"]
    assert pops["members"]["total"] is None and not pops["members"]["complete"]


def test_a_request_narrowed_to_one_user_counts_that_user(seeded: Any) -> None:
    c, org, ns, wide = seeded
    body = {"org": org, "namespace": ns, "read_scope": "own_user", "user_id": "u2"}
    out = _call(c, wide, "namespace.inventory", body)
    sections = {s["plane"]: s for s in out["sections"]}
    assert sections["memories"]["count"] == 1
    pops = {p["population"]: p for p in out["populations"]}
    assert pops["subjects"]["total"] == 1
