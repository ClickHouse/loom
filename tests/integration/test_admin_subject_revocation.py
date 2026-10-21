"""Subject revocation persists through the admin API and concurrent cutoff writers."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from server.auth import revocation
from server.core.ch import get_client
from tests.integration._resources import unique_org


@pytest.fixture(autouse=True)
async def _connected(control_plane_url: str):
    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


async def test_the_admin_route_persists_a_subject_revocation(
    control_plane_url, mint_test_token
) -> None:
    org = unique_org("revoke-subject")
    subject = f"sa:{org}"
    token = mint_test_token(org=org, roles=["platform_admin"])
    async with httpx.AsyncClient(base_url=control_plane_url) as client:
        response = await client.post(
            "/v1/admin/revoke_subject",
            headers={"Authorization": f"Bearer {token}"},
            json={"org": org, "subject": subject, "reason": "credential retired"},
        )
    assert response.status_code == 200, response.text
    row = await revocation.stored(subject)
    assert row is not None and row["issued_before"] is None
    assert row["reason"] == "credential retired"


@pytest.mark.parametrize("stronger_first", [False, True])
async def test_concurrent_cutoffs_never_restore_a_revoked_token(stronger_first: bool) -> None:
    org = unique_org("cutoff-race")
    subject = f"sa:{org}"
    now = datetime.now(UTC).replace(microsecond=0)
    weaker, stronger = now - timedelta(hours=1), now - timedelta(minutes=1)
    instants = [stronger, weaker] if stronger_first else [weaker, stronger]
    await asyncio.gather(
        *(
            revocation.cut_off(subject, issued_before=instant, reason="retired", revoked_by="admin")
            for instant in instants
        )
    )
    row = await revocation.stored(revocation.CUTOFF_PREFIX + subject)
    assert row is not None and row["issued_before"].replace(tzinfo=UTC) == stronger
    revocation._recent.clear()
    await revocation.refresh()
    assert revocation.is_revoked(subject, issued_at=int((now - timedelta(minutes=30)).timestamp()))
