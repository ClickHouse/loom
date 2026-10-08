"""Namespace registry — first-class namespace metadata."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("ns-reg")


@pytest.fixture
def admin_tok_for(mint_test_token):
    """namespace.register is admin-only; mint an admin token for any org
    a test wants to register in. Returns a factory: admin_tok_for(org)."""

    def _mint(org: str) -> str:
        # namespace.list is ORG_ADMIN (a plain admin must not see private
        # namespaces). Mint org_admin so the registry tests can exercise
        # the list-everything intent.
        return mint_test_token(org=org, namespaces=["*"], roles=["org_admin", "admin", "writer"])

    return _mint


@pytest.mark.asyncio
async def test_register_then_get(control_plane_url: str, admin_tok_for) -> None:
    from loom import Agent

    ns = f"ns-{uuid.uuid4().hex[:6]}"
    async with Agent(
        org=ORG,
        namespace=ns,
        agent="w",
        base_url=control_plane_url,
        api_key=admin_tok_for(ORG),
    ) as a:
        await a.namespaces.register(
            owner_email="ops@example.com",
            description="test ns",
            tags=["aurora", "test"],
        )
        info = await a.namespaces.get()
    assert info is not None
    assert info["namespace"] == ns
    assert info["owner_email"] == "ops@example.com"
    assert info["description"] == "test ns"
    assert set(info["tags"]) == {"aurora", "test"}
    assert info["active"] is True


@pytest.mark.asyncio
async def test_get_unregistered_returns_none(control_plane_url: str) -> None:
    """A namespace nobody has written to has no registry row. A data-plane
    write against an unknown namespace succeeds and is what registers it: the row names the writer as owner,
    kind by prefix (memory here), retention 0, and grants nothing."""
    from loom import Agent

    ns = f"ghost-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        # Never written to: not registered.
        assert await a.namespaces.get() is None
        # Data-plane write succeeds without prior registration…
        result = await a.memory.set("k", {"v": 1}, memory_type="semantic")
        assert int(result["version"]) > 0
        # …and is the registration: owner = the token subject.
        got = await a.namespaces.get()
        assert got is not None and got["found"] is True
        assert got["owner_email"] == "test-suite"
        assert got["kind"] == "memory"
        assert got["retention_days"] == 0
        assert "auto-registered" in got["tags"]


@pytest.mark.asyncio
async def test_list_returns_registered(control_plane_url: str, admin_tok_for) -> None:
    from loom import Agent

    org = f"ns-list-{uuid.uuid4().hex[:6]}"
    names = [
        f"alpha-{uuid.uuid4().hex[:4]}",
        f"beta-{uuid.uuid4().hex[:4]}",
        f"gamma-{uuid.uuid4().hex[:4]}",
    ]

    async with Agent(
        org=org,
        namespace=names[0],
        agent="w",
        base_url=control_plane_url,
        api_key=admin_tok_for(org),
    ) as a:
        for name in names:
            await a.namespaces.register(
                owner_email="ops@x.com",
                namespace=name,
                tags=["batch"],
            )
        rows = await a.namespaces.list()

    seen = {r["namespace"] for r in rows}
    assert set(names) <= seen


@pytest.mark.asyncio
async def test_re_register_updates_metadata(control_plane_url: str, admin_tok_for) -> None:
    """Calling register again overwrites owner_email + tags."""
    from loom import Agent

    ns = f"upd-{uuid.uuid4().hex[:6]}"
    async with Agent(
        org=ORG,
        namespace=ns,
        agent="w",
        base_url=control_plane_url,
        api_key=admin_tok_for(ORG),
    ) as a:
        await a.namespaces.register(owner_email="first@x.com", tags=["v1"])
        await a.namespaces.register(owner_email="second@x.com", tags=["v2"])
        info = await a.namespaces.get()
    assert info["owner_email"] == "second@x.com"
    assert "v2" in info["tags"]
    assert "v1" not in info["tags"]


@pytest.mark.asyncio
async def test_list_active_only_filter(control_plane_url: str, admin_tok_for) -> None:
    from loom import Agent

    org = f"ns-act-{uuid.uuid4().hex[:6]}"
    async with Agent(
        org=org,
        namespace="dummy",
        agent="w",
        base_url=control_plane_url,
        api_key=admin_tok_for(org),
    ) as a:
        await a.namespaces.register(
            owner_email="x",
            namespace="live-ns",
            active=True,
        )
        await a.namespaces.register(
            owner_email="x",
            namespace="archived-ns",
            active=False,
        )
        active = await a.namespaces.list(active_only=True)
        all_ns = await a.namespaces.list(active_only=False)
    active_names = {r["namespace"] for r in active}
    all_names = {r["namespace"] for r in all_ns}
    assert "live-ns" in active_names
    assert "archived-ns" not in active_names
    assert "archived-ns" in all_names


@pytest.mark.asyncio
async def test_list_tag_filter(control_plane_url: str, admin_tok_for) -> None:
    from loom import Agent

    org = f"ns-tag-{uuid.uuid4().hex[:6]}"
    async with Agent(
        org=org,
        namespace="dummy",
        agent="w",
        base_url=control_plane_url,
        api_key=admin_tok_for(org),
    ) as a:
        await a.namespaces.register(
            owner_email="x",
            namespace="ml-team",
            tags=["ml"],
        )
        await a.namespaces.register(
            owner_email="x",
            namespace="ops-team",
            tags=["ops"],
        )
        ml_only = await a.namespaces.list(tags=["ml"])
    names = {r["namespace"] for r in ml_only}
    assert "ml-team" in names
    assert "ops-team" not in names


@pytest.mark.asyncio
async def test_register_preserves_auth_fields(control_plane_url, admin_tok_for, ch_client):
    """Self-signup writes loom.namespaces with contact_email +
    password_hash. A subsequent namespace.register (settings update) must
    not zero those fields — that would lock the user out of their own login,
    because the auth check requires `password_hash != ''`.

    This test:
      1. Inserts a namespace row directly into CH with auth fields
         populated (simulates the self-signup state).
      2. Calls namespace.register with settings-only input.
      3. Verifies password_hash + contact_email survived.
    """
    from loom import Agent

    org = f"auth-preserve-{uuid.uuid4().hex[:6]}"
    ns = f"ns-{uuid.uuid4().hex[:6]}"
    pw_hash = "$2b$12$abcdefghijklmnopqrstuv"  # bcrypt-shaped sentinel
    contact = "owner@example.com"

    # Step 1: simulate self-signup state
    ch_client.insert(
        "loom.namespaces",
        [
            [
                org,
                ns,
                contact,  # owner_email
                "self-signup",  # description
                [],  # tags
                True,  # active
                False,  # auto_promote_enabled
                __import__("datetime").datetime.utcnow().replace(microsecond=0),
                __import__("datetime").datetime.utcnow().replace(microsecond=0),
                None,  # conflict_threshold
                0,  # retention_days
                0,  # warm_days
                None,  # duplicate_threshold
                "private",  # visibility
                "",  # rerank_endpoint
                "",  # rerank_prompt_template
                "",  # default_conflict_policy
                "",  # auto_promote_gate
                0,  # evidence_threshold
                "",  # classify_endpoint
                "",  # classify_prompt_template
                contact,  # contact_email
                pw_hash,  # password_hash
                False,  # requires_approval
                0.0,  # daily_cost_cap_usd (Float32, non-null)
            ]
        ],
        column_names=[
            "org",
            "namespace",
            "owner_email",
            "description",
            "tags",
            "active",
            "auto_promote_enabled",
            "created_at",
            "updated_at",
            "conflict_threshold",
            "retention_days",
            "warm_days",
            "duplicate_threshold",
            "visibility",
            "rerank_endpoint",
            "rerank_prompt_template",
            "default_conflict_policy",
            "auto_promote_gate",
            "evidence_threshold",
            "classify_endpoint",
            "classify_prompt_template",
            "contact_email",
            "password_hash",
            "requires_approval",
            "daily_cost_cap_usd",
        ],
    )

    # Also grant the admin token's subject so the register passes authz
    ch_client.insert(
        "loom.namespace_grants",
        [
            [
                org,
                ns,
                "platform-admin",
                "admin",
                __import__("datetime").datetime.utcnow().replace(microsecond=0),
                "test",
                None,
                __import__("time").time_ns(),
            ]
        ],
        column_names=[
            "org",
            "namespace",
            "subject",
            "role",
            "granted_at",
            "granted_by",
            "revoked_at",
            "version",
        ],
    )

    # Step 2: settings update via namespace.register
    async with Agent(
        org=org,
        namespace=ns,
        agent="w",
        base_url=control_plane_url,
        api_key=admin_tok_for(org),
    ) as a:
        await a.namespaces.register(
            owner_email=contact,
            description="updated description",
            tags=["edited"],
        )

    # Step 3: verify auth fields survived
    row = ch_client.query(
        "SELECT contact_email, password_hash FROM loom.namespaces FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String}",
        parameters={"org": org, "ns": ns},
    ).result_rows
    assert row, "namespace row missing after register"
    assert row[0][0] == contact, (
        f"contact_email silently dropped on namespace.register; "
        f"expected {contact!r}, got {row[0][0]!r}"
    )
    assert row[0][1] == pw_hash, (
        "password_hash silently dropped on namespace.register; "
        "a self-signed-up user would be locked out of login"
    )
