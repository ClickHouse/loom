"""audit.list — tenant-scoped audit_log query with keyset pagination.

Audited operations: memory.delete, memory.promote, memory.demote,
erasure.request, erasure.completed, erasure.exported. We exercise a few
of those to populate the log, then assert audit.list surfaces them
correctly and paginates.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("audit-list")


# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)


@pytest.fixture
def admin_tok(mint_test_token):
    """audit.list requires ADMIN; mint a wildcard-namespace admin token for
    the read side of every test in this module."""
    return mint_test_token(org=ORG, namespaces=["*"], roles=["admin", "writer"])


async def _wait_for_audit(ch_client, ns: str, expected_min: int, timeout_s: float = 5.0) -> int:
    """audit rows are written synchronously inside the handler, but the
    request's audit row insert is sequential — wait briefly for completeness."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    last = 0
    while asyncio.get_event_loop().time() < deadline:
        rows = ch_client.query(
            """
            SELECT count() FROM loom.audit_log
            WHERE org = {org:String} AND namespace = {ns:String}
            """,
            parameters={"org": ORG, "ns": ns},
        ).result_rows
        last = int(rows[0][0])
        if last >= expected_min:
            return last
        await asyncio.sleep(0.1)
    return last


async def _emit_audited_ops(base_url: str, ns: str, n: int) -> None:
    """memory.delete is audited; fire N deletes to seed audit_log."""
    from loom import Agent

    async with Agent(org=ORG, namespace=ns, agent="w", base_url=base_url) as a:
        for i in range(n):
            await a.memory.set(f"k{i}", {"v": i}, memory_type="semantic")
            await a.memory.delete(f"k{i}")


@pytest.mark.asyncio
async def test_empty_namespace_returns_no_rows(control_plane_url: str, admin_tok) -> None:
    from loom import Agent

    ns = f"audlist-empty-{uuid.uuid4().hex[:6]}"
    async with Agent(
        org=ORG,
        namespace=ns,
        agent="reader",
        base_url=control_plane_url,
        api_key=admin_tok,
    ) as a:
        result = await a.audit.list(limit=10)
    assert result["rows"] == []
    assert result["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_surfaces_audited_ops(control_plane_url: str, ch_client, admin_tok) -> None:
    from loom import Agent

    ns = f"audlist-{uuid.uuid4().hex[:6]}"
    await _emit_audited_ops(control_plane_url, ns, n=3)
    await _wait_for_audit(ch_client, ns, expected_min=3)

    async with Agent(
        org=ORG,
        namespace=ns,
        agent="reader",
        base_url=control_plane_url,
        api_key=admin_tok,
    ) as a:
        result = await a.audit.list(limit=100)

    assert len(result["rows"]) >= 3
    event_types = {r["event_type"] for r in result["rows"]}
    assert "memory.delete" in event_types
    # Cross-tenancy guard
    for row in result["rows"]:
        assert row["org"] == ORG
        assert row["namespace"] == ns


@pytest.mark.asyncio
async def test_event_type_filter(control_plane_url: str, ch_client, admin_tok) -> None:
    from loom import Agent

    ns = f"audlist-flt-{uuid.uuid4().hex[:6]}"
    await _emit_audited_ops(control_plane_url, ns, n=3)
    await _wait_for_audit(ch_client, ns, expected_min=3)

    async with Agent(
        org=ORG,
        namespace=ns,
        agent="reader",
        base_url=control_plane_url,
        api_key=admin_tok,
    ) as a:
        result = await a.audit.list(event_type="memory.delete", limit=100)
    assert result["rows"]
    assert all(r["event_type"] == "memory.delete" for r in result["rows"])


@pytest.mark.asyncio
async def test_cursor_pagination_walks_all_rows(
    control_plane_url: str, ch_client, admin_tok
) -> None:
    """Issue many audited ops; walk through with a small page size and
    verify we see every row exactly once."""
    from loom import Agent

    ns = f"audlist-pg-{uuid.uuid4().hex[:6]}"
    await _emit_audited_ops(control_plane_url, ns, n=7)
    await _wait_for_audit(ch_client, ns, expected_min=7)

    seen: list[str] = []
    cursor = None
    async with Agent(
        org=ORG,
        namespace=ns,
        agent="reader",
        base_url=control_plane_url,
        api_key=admin_tok,
    ) as a:
        # Page size 3; expect to see all 7 deleted-events plus the
        # accompanying delete rows.
        for _ in range(10):  # safety bound
            page = await a.audit.list(limit=3, cursor=cursor, event_type="memory.delete")
            for r in page["rows"]:
                seen.append(r["request_id"])
            cursor = page.get("next_cursor")
            if not cursor:
                break

    # No duplicates across pages
    assert len(seen) == len(set(seen)), f"duplicate request_ids across pages: {seen}"
    assert len(seen) >= 7, f"expected ≥7 deleted events; saw {len(seen)}"


@pytest.mark.asyncio
async def test_cross_namespace_isolation(control_plane_url: str, ch_client, admin_tok) -> None:
    """Two namespaces emit deletes; each only sees its own rows."""
    from loom import Agent

    ns_a = f"audlist-a-{uuid.uuid4().hex[:6]}"
    ns_b = f"audlist-b-{uuid.uuid4().hex[:6]}"

    await _emit_audited_ops(control_plane_url, ns_a, n=2)
    await _emit_audited_ops(control_plane_url, ns_b, n=2)
    await _wait_for_audit(ch_client, ns_a, expected_min=2)
    await _wait_for_audit(ch_client, ns_b, expected_min=2)

    async with Agent(
        org=ORG,
        namespace=ns_a,
        agent="r",
        base_url=control_plane_url,
        api_key=admin_tok,
    ) as a:
        page_a = await a.audit.list(limit=100)
    async with Agent(
        org=ORG,
        namespace=ns_b,
        agent="r",
        base_url=control_plane_url,
        api_key=admin_tok,
    ) as a:
        page_b = await a.audit.list(limit=100)

    assert all(r["namespace"] == ns_a for r in page_a["rows"])
    assert all(r["namespace"] == ns_b for r in page_b["rows"])
    assert page_a["rows"] and page_b["rows"]


@pytest.mark.asyncio
async def test_rows_include_row_hash(control_plane_url: str, ch_client, admin_tok) -> None:
    """row_hash comes back as a string, not bytes."""
    from loom import Agent

    ns = f"audlist-h-{uuid.uuid4().hex[:6]}"
    await _emit_audited_ops(control_plane_url, ns, n=1)
    await _wait_for_audit(ch_client, ns, expected_min=1)

    async with Agent(
        org=ORG, namespace=ns, agent="r", base_url=control_plane_url, api_key=admin_tok
    ) as a:
        page = await a.audit.list(limit=10)
    row = page["rows"][0]
    assert isinstance(row["row_hash"], str)
    assert len(row["row_hash"]) == 64  # sha-256 hex
