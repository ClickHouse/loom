"""namespace.inventory over real rows: counts per plane, members apart from the identities
observed on rows (members counted per person, as `grants.list` folds them), the unset
user never a subject, and a token pinned to one user counting
only that user's rows, documents and members namespace-wide, and no integrations.

Skips without LOOM_BASE_URL (tests/integration/conftest.py).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

WRITER = "sa:inventory-writer"


def _mint(
    org: str, ns: str, *, subject: str = "", roles: tuple[str, ...] = ("admin",), **claims: Any
) -> str:
    from server.auth.jwt import mint_token

    return mint_token(
        subject=subject or f"sa:inventory-{uuid.uuid4().hex[:6]}",
        org=org,
        namespaces=[ns],
        roles=list(roles),
        ttl_seconds=900,
        is_service_account=True,
        **claims,
    )


_SCHEMA = {
    "name": "report_prefs",
    "fields": [{"name": "owner", "type": "string", "required": True}],
    "created_by": "inventory-test",
}


def _call(c: httpx.Client, token: str, op: str, body: dict[str, Any]) -> Any:
    r = c.post(f"/v1/{op}", json=body, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, f"{op}: {r.status_code} {r.text[:400]}"
    return r.json()


@pytest.fixture
def seeded(control_plane_url: str, ch_client: Any) -> Any:
    org, ns = unique_org("inventory"), "inventory"
    wide = _mint(org, ns, subject=WRITER)
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
        # A pending join request alone grants nothing.
        ch_client.insert(
            "loom.namespace_grants",
            [[org, ns, "asking@example.com", "viewer", datetime.now(UTC), "-", 1, "pending"]],
            column_names=[
                "org",
                "namespace",
                "subject",
                "role",
                "granted_at",
                "granted_by",
                "version",
                "status",
            ],
        )
        # A registered webhook (registering through the op needs a secret and a public host).
        ch_client.insert(
            "loom.memory_webhooks",
            [
                [
                    org,
                    ns,
                    "w1",
                    "https://hooks.example.com/loom",
                    "s",
                    ["memory.set"],
                    datetime.now(UTC),
                    0,
                    1,
                ]
            ],
            column_names=[
                "org",
                "namespace",
                "webhook_id",
                "url",
                "secret_ref",
                "events",
                "created_at",
                "is_deleted",
                "version",
            ],
        )
        for user in ("u1", "u2"):
            body = {"org": org, "namespace": ns, "agent": "bot", "user_id": user, "goal": "report"}
            _call(c, wide, "run.begin", body)
        # One record written twice: its first version stays as a superseded row.
        _call(c, wide, "memory_schema.register", {"org": org, "namespace": ns, **_SCHEMA})
        for owner in ("u1", "u2"):
            body = {
                "org": org,
                "namespace": ns,
                "agent": "bot",
                "user_id": "u1",
                "schema_name": "report_prefs",
                "memory_key": "weekly",
                "fields": {"owner": {"value": owner}},
            }
            _call(c, wide, "memory.record.write", body)
        # Rows no read serves (a duplicate, a superseded, a proposed and a rejected row,
        # under a user of their own), and one served past its expiry until the sweep
        # removes it (under the unset user).
        rows = [
            ["ghost", "duplicate", None],
            ["ghost", "superseded", None],
            ["ghost", "proposed", None],
            ["ghost", "rejected", None],
            ["-", "active", datetime.now(UTC) - timedelta(hours=1)],
        ]
        ch_client.insert(
            "loom.memory",
            [
                [
                    org,
                    ns,
                    "bot",
                    user,
                    "-",
                    f"raw/{i}",
                    "fact",
                    f"stored row {i}",
                    1,
                    datetime.now(UTC),
                    datetime.now(UTC),
                    uuid.uuid4(),
                    status,
                    expires,
                ]
                for i, (user, status, expires) in enumerate(rows)
            ],
            column_names=[
                "org",
                "namespace",
                "agent",
                "user_id",
                "session_id",
                "memory_key",
                "memory_type",
                "value",
                "version",
                "created_at",
                "updated_at",
                "memory_id",
                "status",
                "expires_at",
            ],
        )
        # Grants to the org and a group widen who reads without naming them; an agent and a
        # service account are not people; a revoked grant is gone.
        grants = [
            ("group:eng", None),
            ("everyone", None),
            ("agent:helper", None),
            ("sa:indexer", None),
            ("left@example.com", datetime.now(UTC)),
        ]
        for subject, revoked in grants:
            ch_client.insert(
                "loom.namespace_grants",
                [[org, ns, subject, "viewer", datetime.now(UTC), "-", revoked, 1, "active"]],
                column_names=[
                    "org",
                    "namespace",
                    "subject",
                    "role",
                    "granted_at",
                    "granted_by",
                    "revoked_at",
                    "version",
                    "status",
                ],
            )
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
    # Four written rows and the expired one still served.
    assert sections["memories"]["count"] == 5 and sections["memories"]["latest_at"]
    assert sections["documents"]["count"] == 1
    assert sections["records"]["count"] == 1 and sections["records"]["latest_at"]
    assert sections["runs"]["count"] == 2 and sections["runs"]["latest_at"]
    assert sections["integrations"]["count"] == 1 and sections["integrations"]["latest_at"]
    assert all(s["complete"] for s in out["sections"]), out["sections"]
    pops = {p["population"]: p for p in out["populations"]}
    subjects = {i["name"]: i["rows"] for i in pops["subjects"]["top"]}
    assert subjects == {"u1": 2, "u2": 1} and pops["subjects"]["total"] == 2
    assert [i["name"] for i in pops["agents"]["top"]] == ["bot"]
    # `actor` is stamped from the token: a service account writes as `process:<subject>`.
    assert [(i["name"], i["rows"]) for i in pops["authors"]["top"]] == [(f"process:{WRITER}", 4)]
    # Two people (one under two spellings), neither of whom wrote a row; the org and group
    # grants leave the count incomplete, and the agent, service account, revoked and
    # pending grants are not counted.
    members = pops["members"]
    assert (members["total"], members["complete"]) == (2, False)
    assert members["reason"] == "shared with everyone or a group: those members are not counted"


def test_integrations_are_counted_for_admins_only(seeded: Any) -> None:
    c, org, ns, _ = seeded
    viewer = _mint(org, ns, roles=("viewer",))
    out = _call(c, viewer, "namespace.inventory", {"org": org, "namespace": ns})
    (integrations,) = [s for s in out["sections"] if s["plane"] == "integrations"]
    assert integrations["count"] is None and not integrations["complete"]
    assert all(s["complete"] for s in out["sections"] if s["plane"] != "integrations")


def test_a_token_pinned_to_one_user_counts_only_that_user(seeded: Any) -> None:
    c, org, ns, _ = seeded
    pinned = _mint(org, ns, read_user_id="u1")
    out = _call(c, pinned, "namespace.inventory", {"org": org, "namespace": ns})
    assert (out["scope"]["read_scope"], out["scope"]["user_id"]) == ("own_user", "u1")
    sections = {s["plane"]: s for s in out["sections"]}
    assert sections["memories"]["count"] == 2
    assert sections["records"]["count"] == 1 and sections["runs"]["count"] == 1
    assert sections["documents"]["count"] == 1
    integrations = sections["integrations"]
    assert integrations["count"] is None and not integrations["complete"]
    pops = {p["population"]: p for p in out["populations"]}
    assert [i["name"] for i in pops["subjects"]["top"]] == ["u1"]
    assert pops["members"]["total"] == 2


def test_a_request_narrowed_to_one_user_counts_that_user(seeded: Any) -> None:
    c, org, ns, wide = seeded
    body = {"org": org, "namespace": ns, "read_scope": "own_user", "user_id": "u2"}
    out = _call(c, wide, "namespace.inventory", body)
    sections = {s["plane"]: s for s in out["sections"]}
    assert sections["memories"]["count"] == 1
    pops = {p["population"]: p for p in out["populations"]}
    assert pops["subjects"]["total"] == 1
