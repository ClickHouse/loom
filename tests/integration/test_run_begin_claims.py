"""Concurrent run.begin calls by two users on one run_id keep both runs.

Tokens pinned to users U and V begin the same caller-supplied run_id at the
same moment, ten times. Exactly one keeps the id and the other is handed a
fresh one, and under FINAL each run holds its own goal and owner. A re-sent
begin by the owner keeps its id. A begin drops its claim once its row lands, so
begins that are never completed leave no Keeper key, and the run's row still
sends another user's begin to a fresh id.

Needs a server whose ClickHouse has a Keeper (loom.agent_run_claims); skips
without LOOM_BASE_URL (tests/integration/conftest.py).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

U, V = "user-u", "user-v"


def _mint(org: str, ns: str, **claims: Any) -> str:
    from server.auth.jwt import mint_token

    return mint_token(
        subject=f"sa:run-claims-{uuid.uuid4().hex[:6]}",
        org=org,
        namespaces=[ns],
        roles=["writer"],
        ttl_seconds=900,
        is_service_account=True,
        **claims,
    )


async def _call(c: httpx.AsyncClient, token: str, op: str, body: dict[str, Any]) -> Any:
    r = await c.post(f"/v1/{op}", json=body, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, f"{op}: {r.status_code} {r.text[:400]}"
    return r.json()


@pytest.mark.asyncio
async def test_concurrent_begins_on_one_run_id_keep_both_runs(control_plane_url: str) -> None:
    org, ns = unique_org("run-claims"), "run-claims"
    toks = {U: _mint(org, ns, read_user_id=U), V: _mint(org, ns, read_user_id=V)}
    wide = _mint(org, ns)
    ident = {"org": org, "namespace": ns, "agent": "a"}
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60.0) as c:
        for i in range(10):
            run_id = f"shared-{i}-{uuid.uuid4().hex[:6]}"
            outs = await asyncio.gather(
                *(
                    _call(
                        c,
                        toks[user],
                        "run.begin",
                        {**ident, "user_id": user, "run_id": run_id, "goal": f"{user} {i}"},
                    )
                    for user in (U, V)
                )
            )
            ids = [o["run_id"] for o in outs]
            assert ids.count(run_id) == 1 and len(set(ids)) == 2, ids
            for rid, user in zip(ids, (U, V), strict=True):
                got = await _call(c, wide, "run.get", {"org": org, "namespace": ns, "run_id": rid})
                assert got["found"], (i, user)
                assert got["run"]["user_id"] == user and got["run"]["goal"] == f"{user} {i}"


@pytest.mark.asyncio
async def test_the_owner_re_begins_its_run_under_the_same_id(control_plane_url: str) -> None:
    org, ns = unique_org("run-claims"), "run-claims"
    tok = _mint(org, ns, read_user_id=U)
    body = {"org": org, "namespace": ns, "agent": "a", "user_id": U, "run_id": "mine", "goal": "g"}
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60.0) as c:
        first = await _call(c, tok, "run.begin", body)
        again = await _call(c, tok, "run.begin", body)
    assert first["run_id"] == again["run_id"] == "mine"
    assert again["started_at"] == first["started_at"]


@pytest.mark.asyncio
async def test_a_closed_run_drops_its_claim_and_still_refuses_another_user(
    control_plane_url: str, ch_client: Any
) -> None:
    org, ns = unique_org("run-claims"), "run-claims"
    u_tok, v_tok = _mint(org, ns, read_user_id=U), _mint(org, ns, read_user_id=V)
    ident = {"org": org, "namespace": ns, "agent": "a", "run_id": "closed"}
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60.0) as c:
        await _call(c, u_tok, "run.begin", {**ident, "user_id": U, "goal": "u"})
        await _call(c, u_tok, "run.complete", {**ident, "status": "completed"})
        left = ch_client.query(
            "SELECT count() FROM loom.agent_run_claims WHERE org = {org:String}",
            parameters={"org": org},
        ).result_rows[0][0]
        assert left == 0
        out = await _call(c, v_tok, "run.begin", {**ident, "user_id": V, "goal": "v"})
        assert out["run_id"] != "closed"
        wide = _mint(org, ns)
        got = await _call(c, wide, "run.get", {"org": org, "namespace": ns, "run_id": "closed"})
    assert got["run"]["user_id"] == U and got["run"]["status"] == "completed"


@pytest.mark.asyncio
async def test_begins_never_completed_leave_no_claims(
    control_plane_url: str, ch_client: Any
) -> None:
    org, ns = unique_org("run-claims"), "run-claims"
    tok = _mint(org, ns, read_user_id=U)
    ident = {"org": org, "namespace": ns, "agent": "a", "user_id": U, "goal": "g"}
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60.0) as c:
        await asyncio.gather(
            *(_call(c, tok, "run.begin", {**ident, "run_id": f"open-{i}"}) for i in range(25)),
            *(_call(c, tok, "run.begin", ident) for _ in range(5)),
        )
    left = ch_client.query(
        "SELECT count() FROM loom.agent_run_claims WHERE org = {org:String}",
        parameters={"org": org},
    ).result_rows[0][0]
    assert left == 0
