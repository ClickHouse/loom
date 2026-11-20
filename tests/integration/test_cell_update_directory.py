"""Configuration reservations, issuer routing and configured placements commit together."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from server.core import cell
from server.router import app, directory
from server.settings import settings
from tests.integration.test_router_directory_pg import dsn as dsn


def fingerprint(name: str, issuer: str, orgs: tuple[str, ...] = ()) -> dict[str, Any]:
    return cell.as_value(
        {
            "deployment_id": f"dep-{name}",
            "public_host": f"{name}.example.test",
            "oauth_issuer_host": f"{name}.example.test",
            "bearer_issuers": [issuer],
            "configured_orgs": list(orgs),
        }
    )


async def observed(name: str, *, claim: bool = True) -> directory.Control:
    found = await directory.attach_read(name)
    expected = directory.Control(
        {
            key: getattr(found.row, key) if hasattr(found.row, key) else getattr(found, key)
            for key in directory.CONTROL_FIELDS
        },
        found.row.mutation_generation,
    )
    if claim:
        found = await directory.claim(name, directory.Control(expected.values, None))
        expected = directory.Control(expected.values, found.row.mutation_generation)
    return expected


@pytest.fixture
async def configured(dsn: str):
    import asyncpg

    await directory.migrate()
    conn = await asyncpg.connect(dsn)
    try:
        for name in ("first", "other"):
            await conn.execute(
                "INSERT INTO loom_router.cells "
                "(cell, is_first, host, state, mode, credential_sha256, deployment_id, "
                "current_fp, keys_fp, issuers, attach_started_at) "
                "VALUES ($1, $2, $3, 'serving', 'enforced', $4, $5, $6::jsonb, $7, $8, now())",
                name,
                name == "first",
                f"{name}.example.test",
                ("a" if name == "first" else "b") * 64,
                f"dep-{name}",
                json.dumps(fingerprint(name, name)),
                "c" * 64,
                [name],
            )
        yield conn
    finally:
        await conn.close()


async def change(
    target: dict[str, Any], action: str = "config", *, expected: directory.Control | None = None
):
    return await directory.update_configuration(
        "first",
        target,
        action,
        keys_fp="c" * 64,
        refusal=app.placement_refusal,
        expected=expected or await observed("first"),
    )


async def stored(conn: Any) -> dict[str, Any]:
    row = await conn.fetchrow(
        "SELECT current_fp, next_fp, issuers, placed, state, mode FROM loom_router.cells WHERE cell='first'"
    )
    return dict(row)


@pytest.mark.asyncio
async def test_update_reserves_both_issuers_and_places_configured_orgs_once(
    configured: Any,
) -> None:
    target = fingerprint("first", "new", ("configured-a", "configured-b"))
    await change(target)
    row = await stored(configured)
    assert json.loads(row["current_fp"]) == fingerprint("first", "first")
    assert json.loads(row["next_fp"]) == target
    assert row["issuers"] == ["first", "new"]
    assert row["placed"] == 2
    assert (row["state"], row["mode"]) == ("serving", "enforced")
    await configured.execute(
        "UPDATE loom_router.cells SET closed=true, placements_max=2 WHERE cell='first'"
    )
    await change(target)
    assert await stored(configured) == row
    assert (
        await configured.fetchval("SELECT count(*) FROM loom_router.audit WHERE action='place'")
        == 2
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["closed", "quota", "other-cell", "overlap", "root-keys"])
async def test_update_refusal_leaves_no_partial_reservation_or_placement(
    configured: Any, blocked: str
) -> None:
    target = fingerprint("first", "new", ("a-new", "z-blocked"))
    if blocked == "closed":
        await configured.execute("UPDATE loom_router.cells SET closed=true WHERE cell='first'")
    elif blocked == "quota":
        await configured.execute("UPDATE loom_router.cells SET placements_max=1 WHERE cell='first'")
    elif blocked == "other-cell":
        await configured.execute(
            "INSERT INTO loom_router.placements (org,cell) VALUES ('z-blocked','other')"
        )
    elif blocked == "overlap":
        target["fields"]["bearer_issuers"] = ["other"]
    else:
        await configured.execute(
            "UPDATE loom_router.cells SET keys_fp=$1 WHERE cell='first'", "d" * 64
        )
    before = await stored(configured)
    with pytest.raises((directory.OperatorRefused, directory.PlacementRefused)):
        await change(target)
    assert await stored(configured) == before
    assert (
        await configured.fetchval("SELECT count(*) FROM loom_router.placements WHERE cell='first'")
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action,target_issuer", [("finish", "new"), ("revert", "first")])
async def test_update_narrowing_retains_permanent_placements(
    configured: Any, action: str, target_issuer: str
) -> None:
    pending = fingerprint("first", "new", ("permanent",))
    await change(pending)
    target = pending if action == "finish" else fingerprint("first", "first")
    await change(target, action)
    row = await stored(configured)
    assert json.loads(row["current_fp"]) == target
    assert row["next_fp"] is None
    assert row["issuers"] == [target_issuer]
    assert row["placed"] == 1
    assert (
        await configured.fetchval("SELECT cell FROM loom_router.placements WHERE org='permanent'")
        == "first"
    )
    await change(target, action)
    assert await stored(configured) == row


@pytest.mark.asyncio
async def test_update_old_generation_cannot_narrow_after_another_claim(configured: Any) -> None:
    target = fingerprint("first", "new")
    await change(target)
    stale = await observed("first")
    await observed("first")
    before = await stored(configured)
    with pytest.raises(directory.OperatorRefused, match="generation"):
        await change(target, "finish", expected=stale)
    assert await stored(configured) == before


@pytest.mark.asyncio
async def test_concurrent_cells_cannot_reserve_one_issuer(configured: Any) -> None:
    first, other = await observed("first"), await observed("other")
    results = await asyncio.wait_for(
        asyncio.gather(
            change(fingerprint("first", "contested", ("first-org",)), expected=first),
            directory.update_configuration(
                "other",
                fingerprint("other", "contested", ("other-org",)),
                "config",
                keys_fp="c" * 64,
                refusal=app.placement_refusal,
                expected=other,
            ),
            return_exceptions=True,
        ),
        timeout=15,
    )
    assert sum(isinstance(result, directory.Attaching) for result in results) == 1
    assert sum(isinstance(result, directory.OperatorRefused) for result in results) == 1
    assert await configured.fetchval("SELECT sum(placed) FROM loom_router.cells") == 1
    assert await configured.fetchval("SELECT count(*) FROM loom_router.placements") == 1


@pytest.mark.asyncio
async def test_unreadable_other_reservation_cannot_be_replaced(configured: Any) -> None:
    await configured.execute(
        "UPDATE loom_router.cells SET next_fp=$1::jsonb WHERE cell='other'", '{"v":2,"fields":{}}'
    )
    before = await stored(configured)
    with pytest.raises(directory.OperatorRefused, match="unreadable"):
        await change(fingerprint("first", "new", ("unplaced",)))
    assert await stored(configured) == before
    assert await configured.fetchval("SELECT count(*) FROM loom_router.placements") == 0


@pytest.mark.asyncio
async def test_attaching_cell_keeps_issuers_unroutable(configured: Any) -> None:
    await configured.execute(
        "UPDATE loom_router.cells SET state='attaching', issuers='{}' WHERE cell='first'"
    )
    await change(fingerprint("first", "new", ("configured",)))
    row = await stored(configured)
    assert row["issuers"] == []
    assert row["state"] == "attaching"
    assert row["placed"] == 1


@pytest.mark.asyncio
async def test_later_attaching_cell_refuses_configured_org_changes(configured: Any) -> None:
    await configured.execute("UPDATE loom_router.cells SET state='attaching' WHERE cell='other'")
    with pytest.raises(directory.OperatorRefused, match="later cell"):
        await directory.update_configuration(
            "other",
            fingerprint("other", "new", ("unplaced",)),
            "config",
            keys_fp="c" * 64,
            refusal=app.placement_refusal,
            expected=await observed("other"),
        )
    assert await configured.fetchval("SELECT count(*) FROM loom_router.placements") == 0
    assert (
        await configured.fetchval("SELECT next_fp FROM loom_router.cells WHERE cell='other'")
        is None
    )


@pytest.mark.asyncio
async def test_update_cannot_replace_another_pending_value(configured: Any) -> None:
    await change(fingerprint("first", "new"))
    before = await stored(configured)
    with pytest.raises(directory.OperatorRefused, match="pending"):
        await change(fingerprint("first", "third", ("unplaced",)))
    assert await stored(configured) == before
    assert await configured.fetchval("SELECT count(*) FROM loom_router.placements") == 0


@pytest.mark.asyncio
async def test_recording_attach_leaves_original_configured_placement_to_attach(
    configured: Any,
) -> None:
    original = fingerprint("first", "first", ("served-before-attach",))
    await configured.execute(
        "UPDATE loom_router.cells SET state='attaching', mode='recording', current_fp=$1::jsonb, issuers='{}' WHERE cell='first'",
        json.dumps(original),
    )
    await change(fingerprint("first", "new", ("served-before-attach",)))
    assert await configured.fetchval("SELECT count(*) FROM loom_router.placements") == 0
    assert (await stored(configured))["placed"] == 0
    before = await stored(configured)
    with pytest.raises(directory.OperatorRefused, match="pending"):
        await change(fingerprint("first", "third", ("served-before-attach", "new-org")))
    assert await stored(configured) == before


@pytest.mark.asyncio
async def test_update_does_not_rebuild_missing_serving_directory_placement(configured: Any) -> None:
    original = fingerprint("first", "first", ("missing-placement",))
    await configured.execute(
        "UPDATE loom_router.cells SET current_fp=$1::jsonb WHERE cell='first'", json.dumps(original)
    )
    before = await stored(configured)
    with pytest.raises(directory.OperatorRefused, match="placement.*missing"):
        await change(fingerprint("first", "new", ("missing-placement",)))
    assert await stored(configured) == before
    assert await configured.fetchval("SELECT count(*) FROM loom_router.placements") == 0


@pytest.mark.asyncio
async def test_configuration_fits_the_existing_operator_statement_budget(
    configured: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = await observed("first")
    original = directory._connection
    statements = []

    @asynccontextmanager
    async def counted():
        async with original() as conn:
            conn.add_query_logger(lambda record: statements.append(record.query))
            yield conn

    monkeypatch.setattr(directory, "_connection", counted)
    await change(
        fingerprint("first", "new", tuple(f"configured-{i}" for i in range(40))), expected=expected
    )
    await asyncio.sleep(0)  # asyncpg schedules completed-query observers on the loop.
    assert len(statements) == 9, statements
    assert directory._bound_s(len(statements)) <= directory.CONTROL_MAX_S
    assert await configured.fetchval("SELECT count(*) FROM loom_router.placements") == 40


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["ok", "refused", "new-claim"])
async def test_configuration_http_requires_store_proof_before_any_change(
    configured: Any,
    monkeypatch: pytest.MonkeyPatch,
    proof: str,
) -> None:
    from starlette.responses import JSONResponse

    token = "lco_configuration-test"
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(token.encode()).hexdigest()
    )
    checked = []

    async def stores_check(self, found, nonces):
        checked.append((found.row.cell, nonces))
        if proof == "new-claim":
            await observed("first")
        return (
            JSONResponse({"error": "proof refused"}, status_code=409)
            if proof == "refused"
            else None
        )

    monkeypatch.setattr(app.Router, "_stores_check", stores_check)
    expected = await observed("first")
    target = fingerprint("first", "new", ("http-org",))
    nonces = {"postgres": "pg-nonce", "clickhouse": "ch-nonce", "bucket": None}
    before = await stored(configured)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.build_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/cells/v1/operator/cells/first/configuration",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "action": "config",
                "target_fp": target,
                "keys_fp": "c" * 64,
                "nonces": nonces,
                "expected": expected.values | {"mutation_generation": str(expected.generation)},
            },
        )
    assert response.status_code == (200 if proof == "ok" else 409), response.text
    assert checked == [("first", nonces)]
    if proof == "ok":
        assert response.json()["next_fp"] == target
        assert (
            await configured.fetchval(
                "SELECT cell FROM loom_router.placements WHERE org='http-org'"
            )
            == "first"
        )
    else:
        assert await stored(configured) == before


@pytest.mark.asyncio
async def test_configuration_accepts_the_actual_instance_key_fingerprint(configured, monkeypatch):
    from server.core.deployment_config import fingerprints as instance_fingerprints

    keys = instance_fingerprints(settings)["keys_fp"]
    assert len(keys) == 32
    await configured.execute("UPDATE loom_router.cells SET keys_fp=$1 WHERE cell='first'", keys)
    token = "lco_configuration-real-keys"
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(token.encode()).hexdigest()
    )

    async def stores_check(*_):
        return None

    monkeypatch.setattr(app.Router, "_stores_check", stores_check)
    compare = await observed("first")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.build_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/cells/v1/operator/cells/first/configuration",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "action": "config",
                "target_fp": fingerprint("first", "new"),
                "keys_fp": keys,
                "nonces": {"postgres": "pg", "clickhouse": "ch", "bucket": None},
                "expected": compare.values | {"mutation_generation": str(compare.generation)},
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["next_fp"] == fingerprint("first", "new")


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["first", "other", None])
async def test_configuration_placement_read_proves_actual_winners_without_mutation(
    configured, winner
):
    target = fingerprint("first", "new", ("configured",))
    await configured.execute(
        "UPDATE loom_router.cells SET next_fp=$1::jsonb WHERE cell='first'", json.dumps(target)
    )
    if winner is not None:
        await configured.execute(
            "INSERT INTO loom_router.placements(org,cell) VALUES ('configured',$1)", winner
        )
        await configured.execute(
            "UPDATE loom_router.cells SET placed=placed+1 WHERE cell=$1", winner
        )
    before = await stored(configured)
    compare = await observed("first")
    if winner == "other":
        with pytest.raises(directory.OperatorRefused, match="other"):
            await directory.configuration_placements(
                "first", ["configured"], expected=compare, keys_fp="c" * 64
            )
    else:
        assert await directory.configuration_placements(
            "first", ["configured"], expected=compare, keys_fp="c" * 64
        ) == (["configured"] if winner else [])
    assert await stored(configured) == before


@pytest.mark.asyncio
async def test_placement_proof_refuses_root_secret_drift_before_local_repair(configured):
    with pytest.raises(directory.OperatorRefused, match="root-secret"):
        await directory.configuration_placements(
            "first", [], expected=await observed("first"), keys_fp="changed"
        )


@pytest.mark.asyncio
async def test_placement_proof_read_is_a_bounded_durable_noop(configured, monkeypatch):
    expected = await observed("first")
    original = directory._connection
    statements = []
    before = await stored(configured)
    wal_before = await configured.fetchval("SELECT pg_current_wal_insert_lsn()")

    @asynccontextmanager
    async def counted():
        async with original() as conn:
            conn.add_query_logger(lambda record: statements.append(record.query))
            yield conn

    monkeypatch.setattr(directory, "_connection", counted)
    assert (
        await directory.configuration_placements("first", [], expected=expected, keys_fp="c" * 64)
        == []
    )
    await asyncio.sleep(0)
    assert len(statements) == 9, statements
    assert directory._bound_s(len(statements)) == 120
    assert directory.CONTROL_MAX_S == 131
    assert directory._bound_s(len(statements)) <= directory.CONTROL_MAX_S
    assert sum("pg_logical_emit_message" in sql for sql in statements) == 1
    assert statements[0] == "BEGIN;" and statements[-1] == "COMMIT;"
    assert await configured.fetchval("SELECT pg_current_wal_insert_lsn()") > wal_before
    assert await stored(configured) == before
