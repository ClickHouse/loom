"""Concurrent run.begin calls by two users on one run_id keep both runs.

Tokens pinned to users U and V begin the same caller-supplied run_id at the
same moment, ten times. Exactly one keeps the id and the other is handed a
fresh one, and under FINAL each run holds its own goal and owner. A re-sent
begin by the owner keeps its id. A begin drops its claim once its row lands, so
begins that are never completed leave no Keeper key, and the run's row still
sends another user's begin to a fresh id.

Without a Keeper (no loom.agent_run_claims) those tests skip, and the last one
runs instead: a narrowed token's new supplied id is replaced by a server-made
one, its own existing run keeps its id, and a namespace-wide token keeps it.
Skips without LOOM_BASE_URL (tests/integration/conftest.py).
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


def _has_keeper(ch_client: Any) -> bool:
    rows = ch_client.query(
        "SELECT engine FROM system.tables WHERE database = 'loom' AND name = 'agent_run_claims'"
    ).result_rows
    return bool(rows) and rows[0][0] == "KeeperMap"


@pytest.fixture
def keeper(ch_client: Any) -> None:
    if not _has_keeper(ch_client):
        pytest.skip("needs loom.agent_run_claims (a ClickHouse Keeper)")


async def _call(c: httpx.AsyncClient, token: str, op: str, body: dict[str, Any]) -> Any:
    r = await c.post(f"/v1/{op}", json=body, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, f"{op}: {r.status_code} {r.text[:400]}"
    return r.json()


@pytest.mark.asyncio
async def test_concurrent_begins_on_one_run_id_keep_both_runs(
    control_plane_url: str, keeper: None
) -> None:
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
async def test_the_owner_re_begins_its_run_under_the_same_id(
    control_plane_url: str, keeper: None
) -> None:
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
    control_plane_url: str, ch_client: Any, keeper: None
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
    control_plane_url: str, ch_client: Any, keeper: None
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


@pytest.mark.asyncio
async def test_without_keeper_a_narrowed_new_supplied_id_is_replaced(
    control_plane_url: str, ch_client: Any
) -> None:
    if _has_keeper(ch_client):
        pytest.skip("needs a server without a Keeper")
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
            assert run_id not in ids and len(set(ids)) == 2, ids
            for rid, user in zip(ids, (U, V), strict=True):
                got = await _call(c, wide, "run.get", {"org": org, "namespace": ns, "run_id": rid})
                assert got["found"] and got["run"]["user_id"] == user, (i, user)
        # U's run exists (opened namespace-wide): U re-opens it, V does not take it.
        mine = {**ident, "user_id": U, "run_id": "mine", "goal": "u"}
        assert (await _call(c, wide, "run.begin", mine))["run_id"] == "mine"
        assert (await _call(c, toks[U], "run.begin", mine))["run_id"] == "mine"
        other = await _call(c, toks[V], "run.begin", {**mine, "user_id": V, "goal": "v"})
        assert other["run_id"] != "mine"
        got = await _call(c, wide, "run.get", {"org": org, "namespace": ns, "run_id": "mine"})
    assert got["run"]["user_id"] == U and got["run"]["goal"] == "u"


@pytest.mark.asyncio
async def test_a_session_scoped_token_begins_and_completes_its_own_run(
    control_plane_url: str,
) -> None:
    """A token narrowed to one session resolves its scope from the body's user and
    session; run.complete carries them as run.begin does, so the token closes its own
    run (and a run of another session stays out of reach)."""
    org, ns = unique_org("run-claims"), "run-claims"
    tok = _mint(org, ns, read_user_id=U, read_scope="own_session")
    wide = _mint(org, ns)
    ident = {"org": org, "namespace": ns, "user_id": U, "session_id": "s1"}
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60.0) as c:
        begun = await _call(c, tok, "run.begin", {**ident, "agent": "a", "goal": "mine"})
        run_id = begun["run_id"]
        done = await _call(
            c, tok, "run.complete", {**ident, "run_id": run_id, "status": "completed"}
        )
        assert done["run_id"] == run_id and done["status"] == "completed"
        got = await _call(c, wide, "run.get", {"org": org, "namespace": ns, "run_id": run_id})
        assert got["found"] and got["run"]["status"] == "completed"
        r = await c.post(
            "/v1/run.complete",
            json={**ident, "session_id": "s2", "run_id": run_id, "status": "failed"},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code != 200 or r.json().get("status") != "failed", r.text[:300]
