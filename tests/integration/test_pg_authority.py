"""The Postgres memory authority, against a server run with LOOM_STORAGE_AUTHORITY=postgres.

Each test reads Postgres directly (LOOM_PG_DSN) beside the API: the head a read
serves, the revision journal, the deliveries still owed to ClickHouse and the
idempotency rows. Skipped against a server of the ClickHouse authority, all but the
tests that use the API alone (they hold under either authority).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from tests.integration._eventually import eventually
from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio

# Reads of ClickHouse right after a write: from the replica that has it, on any service.
_SEQUENTIAL = {"select_sequential_consistency": 1}


@pytest.fixture
async def pg_authority(control_plane_url: str) -> str:
    """The DSN of the Postgres authority the server under test runs on; skips otherwise."""
    dsn = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=10) as c:
        health = (await c.get("/v1/health")).json()
    if health.get("storage_authority") != "postgres":
        pytest.skip("the server under test is not of the Postgres authority")
    from server.core import pg

    # These tests also run the authority's code in this process, which reads its own settings.
    assert pg.enabled(), "the pytest process needs LOOM_STORAGE_AUTHORITY=postgres too"
    return dsn


@pytest.fixture
async def pg_conn(pg_authority: str) -> AsyncIterator[Any]:
    import asyncpg

    from server.core import pg

    conn = await asyncpg.connect(pg_authority)
    await pg.init_connection(conn)  # jsonb as Python values, as the server's pool reads it
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def org() -> str:
    return unique_org("pgauth")


@pytest.fixture
def call(control_plane_url: str, mint_test_token: Any, org: str) -> Any:
    token = mint_test_token(org=org, namespaces=["*"], roles=["writer", "admin", "org_admin"])

    async def _call(op: str, body: dict[str, Any], **headers: str) -> httpx.Response:
        # An erasure on a remote ClickHouse runs longer than a write.
        async with httpx.AsyncClient(base_url=control_plane_url, timeout=120) as c:
            return await c.post(
                f"/v1/{op}",
                json={"org": org, **body},
                headers={"Authorization": f"Bearer {token}", **headers},
            )

    return _call


def _set(ns: str, key: str, value: str, **extra: Any) -> dict[str, Any]:
    return {
        "namespace": ns,
        "agent": "a",
        "memory_key": key,
        "memory_type": "semantic",
        "value": value,
        **extra,
    }


async def test_a_write_is_the_postgres_head_and_its_clickhouse_rows(
    call: Any, pg_conn: Any, org: str, ch_client: Any
) -> None:
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    r = await call("memory.set", _set(ns, "k", "the deploy window is tuesday"))
    assert r.status_code == 200, r.text
    out = r.json()
    head = await pg_conn.fetchrow(
        "SELECT memory_id::text AS id, version, content_sha256, seq, row "
        "FROM loom.memory_current WHERE org = $1 AND namespace = $2 AND memory_key = 'k'",
        org,
        ns,
    )
    assert head is not None
    assert (head["id"], head["version"], head["content_sha256"]) == (
        out["memory_id"],
        out["version"],
        out["content_sha256"],
    )
    revision = await pg_conn.fetchrow(
        "SELECT version, actor, source_class, audit FROM loom.memory_revisions "
        "WHERE org = $1 AND namespace = $2 AND seq = $3",
        org,
        ns,
        head["seq"],
    )
    assert revision["version"] == out["version"]
    assert revision["source_class"] == "asserted" and revision["actor"]
    assert revision["audit"]["event_type"] == "memory.revision"
    assert (
        await pg_conn.fetchval(
            "SELECT count(*) FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2", org, ns
        )
        == 0
    )
    got = (await call("memory.get", {"namespace": ns, "agent": "a", "memory_key": "k"})).json()
    assert got["value"] == "the deploy window is tuesday"
    assert (got["memory_id"], got["version"], got["content_sha256"]) == (
        out["memory_id"],
        out["version"],
        out["content_sha256"],
    )
    rows = ch_client.query(
        "SELECT version, content_sha256 FROM loom.memory FINAL "
        "WHERE org = {o:String} AND namespace = {n:String} AND memory_key = 'k'",
        settings=_SEQUENTIAL,
        parameters={"o": org, "n": ns},
    ).result_rows
    assert [(int(v), bytes(s).decode()) for v, s in rows] == [
        (out["version"], out["content_sha256"])
    ]
    # one audit row for the one revision, however often its publish ran
    audits = ch_client.query(
        "SELECT subject FROM loom.audit_log WHERE org = {o:String} AND namespace = {n:String} "
        "AND event_type = 'memory.revision'",
        settings=_SEQUENTIAL,
        parameters={"o": org, "n": ns},
    ).result_rows
    [(subject,)] = audits
    stored = json.loads(subject)
    assert (stored["memory_key"], stored["memory_id"], int(stored["version"])) == (
        "k",
        out["memory_id"],
        out["version"],
    )
    assert stored["content_sha256"] == out["content_sha256"]


async def test_a_revisions_audit_rows_name_its_caller(
    call: Any, pg_conn: Any, org: str, ch_client: Any, control_plane_url: str, mint_test_token: Any
) -> None:
    """A delete's tombstone carries its earlier writer's actor; its revision's audit row
    names the principal that deleted. A revision by id leaves the op's own row beside the
    commit's."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    await call("memory.set", _set(ns, "gone", "the deploy window is tuesday"))
    kept = (await call("memory.set", _set(ns, "kept", "the freeze starts friday"))).json()
    other = mint_test_token(org=org, namespaces=["*"], roles=["writer"], subject="deleter")
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        for op, body in [
            ("memory.delete", {"namespace": ns, "agent": "a", "memory_key": "gone"}),
            ("memory.update_by_id", {"memory_id": kept["memory_id"], "value": "it is monday"}),
        ]:
            r = await c.post(
                f"/v1/{op}", json={"org": org, **body}, headers={"Authorization": f"Bearer {other}"}
            )
            assert r.status_code == 200, r.text
    tombstone = await pg_conn.fetchrow(
        "SELECT actor, audit FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 "
        "AND row->>'memory_key' = 'gone' ORDER BY seq DESC LIMIT 1",
        org,
        ns,
    )
    assert json.loads(tombstone["audit"]["subject"])["tombstoned"] is True
    assert "deleter" in tombstone["audit"]["actor"]
    assert "deleter" not in tombstone["actor"]  # the version row keeps its earlier writer
    revised = await pg_conn.fetchval(
        "SELECT audit FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 "
        "AND memory_id = $3::uuid ORDER BY seq DESC LIMIT 1",
        org,
        ns,
        kept["memory_id"],
    )
    assert "deleter" in revised["actor"]
    events = ch_client.query(
        "SELECT actor FROM loom.audit_log WHERE org = {o:String} AND namespace = {n:String} "
        "AND event_type = 'memory.update_by_id'",
        settings=_SEQUENTIAL,
        parameters={"o": org, "n": ns},
    ).result_rows
    assert len(events) == 1 and "deleter" in events[0][0]


async def test_two_updates_from_one_read_land_once(call: Any, pg_conn: Any) -> None:
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    first = (await call("memory.set", _set(ns, "k", "version one"))).json()
    attempts = await asyncio.gather(
        *(
            call(
                "memory.set",
                _set(
                    ns, "k", f"version two from writer {i}", if_match_sha256=first["content_sha256"]
                ),
            )
            for i in range(4)
        )
    )
    codes = sorted(r.status_code for r in attempts)
    assert codes.count(200) == 1, [r.text for r in attempts]
    assert all(c in (200, 409) for c in codes), codes


async def test_concurrent_restatements_under_other_keys_see_each_other(
    call: Any, pg_conn: Any
) -> None:
    """Each write's verdict covers the ones that committed while it was checked: of
    one fact written at once under six keys, only the first lands as a new fact."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    value = "the staging cluster runs clickhouse twenty six point nine"
    out = await asyncio.gather(*(call("memory.set", _set(ns, f"k{i}", value)) for i in range(6)))
    assert all(r.status_code == 200 for r in out), [r.text for r in out]
    verdicts = [r.json().get("conflict_verdict") for r in out]
    assert verdicts.count("clear") == 1, verdicts


async def test_a_pinned_peer_holds_against_a_close_write(call: Any, pg_conn: Any, org: str) -> None:
    """The same fact written under another key merges into the pinned peer instead of
    landing beside it, and the pinned head is unchanged."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    pinned = await call("memory.pin", _set(ns, "rule", "the on-call rotation is weekly"))
    assert pinned.status_code == 200, pinned.text
    r = await call(
        "memory.set", {**_set(ns, "other", "the on-call rotation is weekly"), "agent": "b"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["conflict_verdict"] == "merge", r.json()
    head = await pg_conn.fetchrow(
        "SELECT version, row->>'pinned' AS pinned FROM loom.memory_current "
        "WHERE org = $1 AND namespace = $2 AND memory_key = 'rule'",
        org,
        ns,
    )
    assert (head["version"], head["pinned"]) == (pinned.json()["version"], "1")


async def test_an_unpublished_commit_is_published_by_the_next_admission(
    call: Any, pg_conn: Any, org: str, ch_client: Any
) -> None:
    """A writer cut off after its commit leaves its delivery leased; once the lease is
    out, the next admission in the namespace publishes it before it checks: the memory
    row, and the search document built from the revision (as the delivery worker
    does, through the same `pg_delivery.publish_claims`)."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    first = (await call("memory.set", _set(ns, "k", "written and published"))).json()
    # A later revision of `k` committed by a writer that stopped before publishing.
    async with pg_conn.transaction():
        seq = await pg_conn.fetchval(
            "UPDATE loom.scope_generations SET generation = generation + 1 "
            "WHERE org = $1 AND scope = $2 RETURNING generation",
            org,
            ns,
        )
        await pg_conn.execute(
            "INSERT INTO loom.memory_revisions (org, namespace, seq, user_id, memory_id, "
            "version, content_sha256, actor, source_class, row, audit) "
            "SELECT org, namespace, $3, user_id, memory_id, version + 1, content_sha256, actor, "
            "source_class, jsonb_set(row, '{version}', to_jsonb(version + 1)), audit "
            "FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 "
            "ORDER BY seq DESC LIMIT 1",
            org,
            ns,
            seq,
        )
        await pg_conn.execute(
            "INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_until) "
            "VALUES ($1, $2, $3, 3, now() - interval '1 second')",
            org,
            ns,
            seq,
        )
    r = await call("memory.set", _set(ns, "k2", "a second fact in the namespace"))
    assert r.status_code == 200, r.text
    assert (
        await pg_conn.fetchval(
            # A delivery taken over stays settled (no target left) for the insert bound.
            "SELECT count(*) FROM loom.memory_deliveries "
            "WHERE org = $1 AND namespace = $2 AND targets <> 0",
            org,
            ns,
        )
        == 0
    )
    versions = ch_client.query(
        "SELECT max(version) FROM loom.memory WHERE org = {o:String} AND namespace = {n:String} "
        "AND memory_key = 'k'",
        settings=_SEQUENTIAL,
        parameters={"o": org, "n": ns},
    ).result_rows
    assert int(versions[0][0]) == first["version"] + 1
    docs = ch_client.query(
        "SELECT max(version) FROM loom.memory_embeddings "
        "WHERE org = {o:String} AND namespace = {n:String} AND memory_key = 'k'",
        settings=_SEQUENTIAL,
        parameters={"o": org, "n": ns},
    ).result_rows
    assert int(docs[0][0]) == first["version"] + 1


async def test_a_retry_after_a_lost_answer_replays_the_commit(
    call: Any, pg_conn: Any, org: str
) -> None:
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    key = uuid.uuid4().hex
    body = _set(ns, "k", "a fact whose answer is lost")
    first = await call("memory.set", body, **{"Idempotency-Key": key})
    assert first.status_code == 200, first.text
    # The answer never reached the caller: the commit stands, the stored answer does not.
    changed = await pg_conn.execute(
        "UPDATE loom.idempotency SET state = 'committed', response_body = NULL, "
        "lease_until = NULL WHERE org = $1 AND state = 'done'",
        org,
    )
    assert changed == "UPDATE 1"
    again = await call("memory.set", body, **{"Idempotency-Key": key})
    assert again.status_code == 200, again.text
    out = again.json()
    assert out["idempotent_replay"] == "committed"
    assert out["memory_id"] == first.json()["memory_id"]
    assert out["version"] == first.json()["version"]
    assert (
        await pg_conn.fetchval(
            "SELECT count(*) FROM loom.memory_revisions WHERE org = $1 AND namespace = $2", org, ns
        )
        == 1
    )
    other = await call(
        "memory.set", _set(ns, "k", "another body under the key"), **{"Idempotency-Key": key}
    )
    assert other.status_code == 409, other.text


async def test_a_bulk_batch_writing_a_key_twice_commits_its_later_value_as_the_head(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A batch that writes one key twice: the head both stores serve is the later value,
    every row it wrote is a revision, and a replay after its answer was lost names the
    rows it committed, in their commit order."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    key = uuid.uuid4().hex
    body = {
        "namespace": ns,
        "agent": "a",
        "entries": [
            {"memory_key": "k", "memory_type": "semantic", "value": "the deploy runs on monday"},
            {"memory_key": "k", "memory_type": "semantic", "value": "the deploy moved to friday"},
        ],
    }
    first = await call("memory.bulk_set", body, **{"Idempotency-Key": key})
    assert first.status_code == 200, first.text
    landed = [(r["memory_key"], r["version"]) for r in first.json()["written"] if r["version"]]
    revisions = await pg_conn.fetch(
        "SELECT row->>'memory_key' AS k, version FROM loom.memory_revisions "
        "WHERE org = $1 AND namespace = $2 ORDER BY seq",
        org,
        ns,
    )
    assert sorted((r["k"], r["version"]) for r in revisions) == sorted(landed)
    head = await pg_conn.fetchrow(
        "SELECT version, row->>'value' AS value FROM loom.memory_current "
        "WHERE org = $1 AND namespace = $2 AND memory_key = 'k'",
        org,
        ns,
    )
    assert head["version"] == max(v for _, v in landed)
    assert "friday" in head["value"]
    got = await call("memory.get", {"namespace": ns, "agent": "a", "memory_key": "k"})
    assert got.status_code == 200 and "friday" in json.dumps(got.json()["value"]), got.text
    await pg_conn.execute(
        "UPDATE loom.idempotency SET state = 'committed', response_body = NULL, "
        "lease_until = NULL WHERE org = $1 AND state = 'done'",
        org,
    )
    again = await call("memory.bulk_set", body, **{"Idempotency-Key": key})
    assert again.status_code == 200, again.text
    assert again.json()["idempotent_replay"] == "committed"
    assert [(r["memory_key"], r["version"]) for r in again.json()["written"]] == [
        (r["k"], r["version"]) for r in revisions
    ]


async def test_another_orgs_id_is_not_found(
    call: Any, pg_conn: Any, control_plane_url: str, mint_test_token: Any
) -> None:
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    mine = (await call("memory.set", _set(ns, "k", "a private fact"))).json()
    other_org = unique_org("pgauth-other")
    token = mint_test_token(org=other_org, namespaces=["*"])
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        r = await c.post(
            "/v1/memory.get_by_id",
            json={"org": other_org, "memory_id": mine["memory_id"]},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    assert r.json().get("found") is False
    assert "a private fact" not in r.text


async def test_a_token_narrowed_to_one_namespace_reads_nothing_of_another(
    call: Any, pg_conn: Any, org: str, control_plane_url: str, mint_test_token: Any
) -> None:
    mine, other = f"pg-{uuid.uuid4().hex[:8]}", f"pg-{uuid.uuid4().hex[:8]}"
    row = (await call("memory.set", _set(other, "k", "a fact of another namespace"))).json()
    token = mint_test_token(org=org, namespaces=[mine])
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        by_id = await c.post(
            "/v1/memory.get_by_id",
            json={"org": org, "memory_id": row["memory_id"]},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert by_id.status_code == 200, by_id.text
    assert by_id.json().get("found") is False
    assert "another namespace" not in by_id.text


async def test_a_nul_character_is_refused_before_anything_commits(
    call: Any, pg_conn: Any, org: str
) -> None:
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    # A value is stored JSON-escaped; a tag is stored as it is.
    r = await call("memory.set", _set(ns, "k", "a fact", tags=["a\x00b"]))
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "VALIDATION_FAILED"
    assert (
        await pg_conn.fetchval(
            "SELECT count(*) FROM loom.memory_revisions WHERE org = $1 AND namespace = $2", org, ns
        )
        == 0
    )


async def test_a_redact_scrubs_the_postgres_journal_and_the_clickhouse_history(
    call: Any, pg_conn: Any, org: str, ch_client: Any
) -> None:
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    first = (await call("memory.set", _set(ns, "k", "the token is hunter2"))).json()
    assert (await call("memory.set", _set(ns, "k", "the token was rotated"))).status_code == 200
    r = await call(
        "memory.redact",
        {"memory_id": first["memory_id"], "version": first["version"], "reason": "leak"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["redacted"] is True, r.json()
    stored = await pg_conn.fetch(
        "SELECT version, row->>'value' AS value FROM loom.memory_revisions "
        "WHERE org = $1 AND namespace = $2 ORDER BY seq",
        org,
        ns,
    )
    assert [(x["version"], "hunter2" in (x["value"] or "")) for x in stored] == [
        (first["version"], False),
        (stored[1]["version"], False),
    ]
    head = await pg_conn.fetchval(
        "SELECT row->>'value' FROM loom.memory_current WHERE org = $1 AND namespace = $2",
        org,
        ns,
    )
    assert "rotated" in head
    history = ch_client.query(
        "SELECT value FROM loom.memory_versions WHERE org = {o:String} AND namespace = {n:String}",
        settings=_SEQUENTIAL,
        parameters={"o": org, "n": ns},
    ).result_rows
    assert history and not any("hunter2" in str(v) for (v,) in history)


async def test_a_clear_tombstones_the_postgres_heads(call: Any, pg_conn: Any, org: str) -> None:
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    for i in range(3):
        assert (await call("memory.set", _set(ns, f"k{i}", f"cleared fact {i}"))).status_code == 200
    r = await call("memory.clear", {"namespace": ns, "agent": "a", "confirm_subject": "a"})
    assert r.status_code == 200, r.text
    live = await pg_conn.fetchval(
        "SELECT count(*) FROM loom.memory_current WHERE org = $1 AND namespace = $2 "
        "AND row->>'tombstoned_at' IS NULL",
        org,
        ns,
    )
    assert live == 0
    got = await call("memory.get", {"namespace": ns, "agent": "a", "memory_key": "k0"})
    assert got.status_code in (200, 404), got.text
    assert "cleared fact" not in got.text


async def test_a_user_erasure_removes_that_users_rows_only(
    call: Any, pg_conn: Any, org: str
) -> None:
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    user = f"u-{uuid.uuid4().hex[:6]}"
    for who in (user, "someone-else"):
        r = await call("memory.set", _set(ns, "k", f"a fact about {who}", user_id=who))
        assert r.status_code == 200, r.text
    r = await call(
        "erasure.request", {"scope_type": "user_id", "scope_value": user, "requester": "t"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed", r.json()
    for table in ("loom.memory_current", "loom.memory_revisions"):
        users = await pg_conn.fetch(
            f"SELECT user_id FROM {table} WHERE org = $1 AND namespace = $2", org, ns
        )
        assert [u["user_id"] for u in users] == ["someone-else"], table


async def test_the_usage_fold_raises_the_postgres_heads_counters(
    call: Any, pg_conn: Any, org: str
) -> None:
    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    row = (await call("memory.set", _set(ns, "k", "a fact that gets recalled"))).json()
    try:
        await pg_memory.fold_usage([(org, ns, row["memory_id"], 2, 5, None)])
        await pg_memory.fold_usage([(org, ns, row["memory_id"], 1, 3, None)])  # never lowered
    finally:
        await pg.close_pool()
    head = await pg_conn.fetchrow(
        "SELECT version, (row->>'evidence_count')::int AS ev, (row->>'recall_count')::int AS rc "
        "FROM loom.memory_current WHERE org = $1 AND namespace = $2",
        org,
        ns,
    )
    assert (head["version"], head["ev"], head["rc"]) == (row["version"], 2, 5)


async def test_a_namespace_erasure_leaves_no_postgres_rows(
    call: Any, pg_conn: Any, org: str
) -> None:
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    for i in range(3):
        assert (
            await call("memory.set", _set(ns, f"k{i}", f"fact {i} to erase"))
        ).status_code == 200
    r = await call(
        "erasure.request", {"scope_type": "namespace", "scope_value": ns, "requester": "t"}
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "completed", out
    steps = {s["step"]: s for s in out["steps"]}
    assert steps["pg_memory.delete"]["rows_affected"] >= 6  # 3 heads + 3 revisions
    for table in ("loom.memory_current", "loom.memory_revisions", "loom.memory_deliveries"):
        assert (
            await pg_conn.fetchval(
                f"SELECT count(*) FROM {table} WHERE org = $1 AND namespace = $2", org, ns
            )
            == 0
        ), table


async def test_concurrent_boots_apply_each_migration_once(
    pg_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three boots migrating one empty database at once: the advisory lock serializes
    them, the first applies every file and the others find nothing pending."""
    from urllib.parse import urlsplit, urlunsplit

    from server.core.schema import pg_migrate
    from server.settings import settings

    name = f"loom_migrate_{uuid.uuid4().hex[:8]}"
    await pg_conn.execute(f'CREATE DATABASE "{name}"')
    try:
        dsn = urlsplit(os.environ["LOOM_PG_DSN"])
        monkeypatch.setattr(settings, "pg_dsn", urlunsplit(dsn._replace(path=f"/{name}")))
        applied = await asyncio.gather(*(pg_migrate.migrate() for _ in range(3)))
        files = [f.name for f in pg_migrate.migration_files()]
        assert sorted(applied, key=len) == [[], [], files]
    finally:
        await pg_conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


async def test_a_request_whose_key_a_retry_took_over_cannot_commit_under_it(
    call: Any, pg_conn: Any, org: str
) -> None:
    """The key's row moved to the retry's token: the first request's commit is refused
    (503 IDEMPOTENT_TAKEN_OVER) and nothing of it lands."""
    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "the first answer"))).status_code == 200
    row = await _head_row(pg_conn, org, ns)
    row["version"] = int(row["version"]) + 1
    key = uuid.uuid4().hex
    await pg_conn.execute(
        "INSERT INTO loom.idempotency (org, key_hash, subject, operation, request_digest, "
        "state, lease_token, lease_until, expires_at) VALUES ($1, $2, 's', 'memory.set', 'h', "
        "'running', 2, "
        "now() + interval '1 minute', now() + interval '1 day')",
        org,
        key,
    )
    try:
        with pytest.raises(pg_memory.IdempotencyFenced) as e:
            await pg_memory.commit_versions([row], [{}], idempotency=(org, key, 1))
    finally:
        await pg.close_pool()
    assert e.value.status_code == 503 and e.value.code == "IDEMPOTENT_TAKEN_OVER"
    assert (
        await pg_conn.fetchval(
            "SELECT count(*) FROM loom.memory_revisions WHERE org = $1 AND namespace = $2", org, ns
        )
        == 1
    )


@pytest.mark.parametrize("ends", ["commit", "rollback", "never"])
async def test_a_settled_head_read_waits_for_the_commit_holding_the_head(
    call: Any, pg_conn: Any, pg_authority: str, org: str, ends: str
) -> None:
    """A commit whose answer was lost may still hold the head's row (on the synchronous
    standby, say): the settled read waits on that row's lock and answers what the commit
    left, or 503 past `pg_commit_lock_timeout_ms`."""
    import asyncpg

    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "the head"))).status_code == 200
    key = (org, ns, "a", "-", "-", "k")
    # Another connection: inside the open transaction pg_stat_activity is one snapshot.
    watcher = await asyncpg.connect(pg_authority)
    holder = pg_conn.get_server_pid()
    tx = pg_conn.transaction()
    open_tx = False
    read: asyncio.Task[dict[Any, int]] | None = None

    async def waiting_on_the_row() -> bool:
        return bool(
            await watcher.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid))",
                holder,
            )
        )

    try:
        await tx.start()
        open_tx = True
        moved = await pg_conn.fetchval(
            "UPDATE loom.memory_current SET version = version + 1 "
            "WHERE org = $1 AND namespace = $2 AND memory_key = 'k' RETURNING version",
            org,
            ns,
        )
        read = asyncio.create_task(pg_memory.settled_head_versions([key]))
        await eventually(waiting_on_the_row, description="the read waits on the head's lock")
        if ends == "never":
            with pytest.raises(pg.PostgresUnavailableError):
                await asyncio.wait_for(read, timeout=10)
            return
        open_tx = False
        await (tx.commit() if ends == "commit" else tx.rollback())
        heads = await asyncio.wait_for(read, timeout=10)
    finally:
        if open_tx:
            await tx.rollback()
        if read is not None:
            read.cancel()
        await watcher.close()
        await pg.close_pool()
    assert heads[key] == (moved if ends == "commit" else moved - 1)


async def test_the_status_before_a_version_is_read_from_the_authoritys_revisions(
    call: Any, pg_conn: Any, org: str
) -> None:
    """The status of the newest revision below the version, of this key and id, that is not
    `superseded`: an Undo skips its retirement and brings back what the key held before it.
    A revision under another org, key part, id or namespace does not count."""
    from server.core import pg
    from server.memory.store import pg_memory

    ns, elsewhere = f"pg-{uuid.uuid4().hex[:8]}", f"pg-{uuid.uuid4().hex[:8]}"
    for namespace, count in ((ns, 3 + 5), (elsewhere, 1)):
        for n in range(count):
            body = _set(namespace, "k", f"v{n}", user_id="u", session_id="s")
            written = await call("memory.set", body)
            assert written.status_code == 200, written.text
    revisions_sql = (
        "SELECT namespace, seq, memory_id, version FROM loom.memory_revisions "
        "WHERE org = $1 AND namespace = $2 ORDER BY version"
    )
    own = await pg_conn.fetch(revisions_sql, org, ns)
    [elsewhere_revision] = await pg_conn.fetch(revisions_sql, org, elsewhere)
    reviewed, active, retired, *off_key = own
    mid = str(reviewed["memory_id"])
    assert {str(r["memory_id"]) for r in own} == {mid}

    async def rewrite(revision: Any, **fields: str) -> None:
        # user_id and memory_id are columns of the revisions as well as fields of `row`.
        done = await pg_conn.execute(
            "UPDATE loom.memory_revisions SET row = row || $4::jsonb, "
            "user_id = coalesce($5, user_id), memory_id = coalesce($6::uuid, memory_id) "
            "WHERE org = $1 AND namespace = $2 AND seq = $3",
            org,
            revision["namespace"],
            revision["seq"],
            fields,
            fields.get("user_id"),
            fields.get("memory_id"),
        )
        assert done == "UPDATE 1"

    # Each of `off_key` differs from the key or the id in one part and names it in its status,
    # so a leak would be the answer. The other namespace's revision takes this id.
    await rewrite(reviewed, status="conflicted")
    await rewrite(retired, status="superseded")
    other_agent, other_session, other_key, other_user, other_id = off_key
    await rewrite(other_agent, agent="b", status="under another agent")
    await rewrite(other_session, session_id="s2", status="under another session_id")
    await rewrite(other_key, memory_key="k2", status="under another memory_key")
    await rewrite(other_user, user_id="u2", status="under another user_id")
    await rewrite(other_id, memory_id=str(uuid.uuid4()), status="under another memory_id")
    await rewrite(elsewhere_revision, memory_id=mid, status="duplicate")
    # Its memory.set ran after every set in `ns`, so with this id it is the newest revision:
    # a read that ignored the namespace would answer it.
    assert int(elsewhere_revision["version"]) > max(int(r["version"]) for r in own)
    # A copy of `active` under another org, newer than every revision of `org`.
    newest = max(int(r["version"]) for r in [*own, elsewhere_revision]) + 1
    await pg_conn.execute(
        "INSERT INTO loom.memory_revisions (org, namespace, seq, user_id, memory_id, version, "
        "content_sha256, actor, source_class, request_id, committed_at, row, audit) "
        "SELECT $3, namespace, seq, user_id, memory_id, $4, "
        "content_sha256, actor, source_class, request_id, committed_at, "
        "row || jsonb_build_object('org', $3::text, 'version', $4::bigint, "
        "'status', 'under another org'), audit "
        "FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 AND seq = $5",
        org,
        ns,
        unique_org("pgauth-other"),
        newest,
        active["seq"],
    )
    key = (org, ns, "a", "u", "s", "k")
    above = newest + 1
    try:
        assert await pg_memory.status_before(key, mid, above) == "active"
        assert await pg_memory.status_before(key, mid, int(active["version"])) == "conflicted"
        assert await pg_memory.status_before(key, mid, int(reviewed["version"])) == ""
        assert await pg_memory.status_before((org, elsewhere, *key[2:]), mid, above) == "duplicate"
    finally:
        await pg.close_pool()


@pytest.mark.usefixtures("pg_authority")
@pytest.mark.parametrize("level", ["high", "medium"])
async def test_the_sweep_relabels_only_the_head_it_scanned(call: Any, org: str, level: str) -> None:
    """The sweep scans ClickHouse, which can lag the authority: a candidate whose head moved
    since its scan, to a version its writer labelled high, is left as that writer wrote it."""
    from server.core import pg
    from server.memory.app import sensitivity_sweep
    from server.memory.store import pg_memory
    from server.memory.store.sensitivity_sweep_sql import SORT_KEY

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    scanned = await call("memory.set", _set(ns, "k", "write to ana@example.com"))
    assert scanned.status_code == 200, scanned.text
    newer = await call("memory.set", _set(ns, "k", "the door code", sensitivity="high"))
    assert newer.status_code == 200, newer.text
    key = (org, ns, "a", "-", "-", "k")
    candidate = sensitivity_sweep.SweepCandidate(
        memory_id=scanned.json()["memory_id"],
        org=org,
        namespace=ns,
        agent="a",
        user_id="-",
        session_id="-",
        memory_key="k",
        level=level,
        shapes=("credential",) if level == "high" else ("email",),
        sort_key=dict(zip(SORT_KEY, key, strict=True)),
        version=scanned.json()["version"],
    )
    assert newer.json()["memory_id"] == scanned.json()["memory_id"]  # moved, not gone
    try:
        result = await sensitivity_sweep.apply([candidate])
        assert (result.relabelled, len(result.left)) == (0, 1) and not result.failed
        head = (await pg_memory.heads_by_keys([key]))[key]
        assert (head["version"], head["sensitivity"]) == (newer.json()["version"], "high")
    finally:
        await pg.close_pool()


@pytest.mark.usefixtures("pg_authority")
async def test_the_sweep_labels_its_key_whatever_other_key_holds_its_id(
    call: Any, pg_conn: Any, ch_client: Any, org: str
) -> None:
    """A namespace restored into itself under another agent holds the id under two keys,
    the copy's head newer: the sweep labels the candidate's own key, whose commit retires
    its vectors, and leaves the copy."""
    from server.core import pg
    from server.core.ch import get_client
    from server.memory.app import sensitivity_sweep
    from server.memory.store import pg_memory, rows
    from server.memory.store.pg_publish import audit_row_for
    from server.memory.store.sensitivity_sweep_sql import SORT_KEY

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    scanned = (await call("memory.set", _set(ns, "k", "the vault password is hunter2"))).json()
    head = await _head_row(pg_conn, org, ns)
    assert head["sensitivity"] == ""  # the candidate's level is stated, not detected
    assert _live_sweep_vectors(ch_client, org, ns, "a") >= 1
    copy = {**head, "agent": "b", "version": rows.next_version()}
    key, copy_key = (org, ns, "a", "-", "-", "k"), (org, ns, "b", "-", "-", "k")
    candidate = sensitivity_sweep.SweepCandidate(
        memory_id=scanned["memory_id"],
        org=org,
        namespace=ns,
        agent="a",
        user_id="-",
        session_id="-",
        memory_key="k",
        level="high",
        shapes=("credential",),
        sort_key=dict(zip(SORT_KEY, key, strict=True)),
        version=scanned["version"],
    )
    ch = get_client()
    await ch.connect()
    try:
        await pg_memory.commit_versions([copy], [audit_row_for(copy, request_id="", writer="")])
        result = await sensitivity_sweep.apply([candidate], ch=ch)
        heads = await pg_memory.heads_by_keys([key, copy_key])
    finally:
        await ch.disconnect()
        await pg.close_pool()
    assert (result.high, result.left, result.failed) == (1, [], [])
    assert heads[key]["version"] > scanned["version"] and heads[key]["sensitivity"] == "high"
    assert _live_sweep_vectors(ch_client, org, ns, "a") == 0
    assert (heads[copy_key]["version"], heads[copy_key]["sensitivity"]) == (copy["version"], "")


@pytest.mark.usefixtures("pg_authority")
async def test_a_relabel_refused_for_a_head_written_since_its_read_fences_nothing(
    call: Any, pg_conn: Any, ch_client: Any, org: str, monkeypatch: Any
) -> None:
    """A head committed between the sweep's read and its commit, at a version below the
    relabel's, refuses the commit: the candidate is left, with no retire or fence at the
    relabel's version hiding the key's vectors."""
    import server.memory.store as memory_repo
    from server.core import pg
    from server.core.ch import get_client
    from server.memory.app import sensitivity_sweep
    from server.memory.store import pg_memory
    from server.memory.store.pg_publish import audit_row_for
    from server.memory.store.sensitivity_sweep_sql import SORT_KEY

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    scanned = (await call("memory.set", _set(ns, "k", "the vault password is hunter2"))).json()
    key = (org, ns, "a", "-", "-", "k")
    candidate = sensitivity_sweep.SweepCandidate(
        memory_id=scanned["memory_id"],
        org=org,
        namespace=ns,
        agent="a",
        user_id="-",
        session_id="-",
        memory_key="k",
        level="high",
        shapes=("credential",),
        sort_key=dict(zip(SORT_KEY, key, strict=True)),
        version=scanned["version"],
    )
    insert = memory_repo.insert_versions_over_heads
    relabel: list[int] = []

    async def written_first(rows: Any, read: Any, **kw: Any) -> int:
        relabel.append(int(rows[0]["version"]))
        newer = {**await _head_row(pg_conn, org, ns), "version": relabel[0] - 1}
        await pg_memory.commit_versions([newer], [audit_row_for(newer, request_id="", writer="")])
        return await insert(rows, read, **kw)

    monkeypatch.setattr(memory_repo, "insert_versions_over_heads", written_first)
    ch = get_client()
    await ch.connect()
    try:
        result = await sensitivity_sweep.apply([candidate], ch=ch)
    finally:
        await ch.disconnect()
        await pg.close_pool()
    assert (result.relabelled, len(result.left), result.failed) == (0, 1, [])
    assert _live_sweep_vectors(ch_client, org, ns, "a") >= 1
    at_relabel = ch_client.query(
        "SELECT count() FROM loom.memory_embeddings FINAL WHERE org = {o:String} "
        "AND namespace = {n:String} AND memory_key = 'k' AND version = {v:UInt64}",
        settings=_SEQUENTIAL,
        parameters={"o": org, "n": ns, "v": relabel[0]},
    ).result_rows[0][0]
    assert at_relabel == 0


@pytest.mark.usefixtures("pg_authority")
async def test_a_relabel_whose_publication_is_pending_owes_its_facts_until_published(
    call: Any, pg_conn: Any, ch_client: Any, org: str, monkeypatch: Any
) -> None:
    """The graph is judged from ClickHouse: a 'high' relabel committed while its publish
    failed leaves the facts its row stated open and owed to the projection repair, which
    waits while ClickHouse lacks the relabel and closes them once the delivery worker has
    published it, resolving the debt."""
    from server.core import pg
    from server.core.ch import get_client
    from server.memory.app import pg_delivery, projection_failures, sensitivity_sweep
    from server.memory.store import pg_memory, pg_publish
    from server.memory.store.sensitivity_sweep_sql import SORT_KEY

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    stated = ("alice", "meet", "bob")  # as stored: the relation is lemmatised
    write = _set(
        ns,
        "k",
        "alice met bob in oslo",
        entities=["alice", "bob", "oslo"],
        relations=[list(stated)],
    )
    scanned = (await call("memory.set", write)).json()
    key = (org, ns, "a", "-", "-", "k")

    def open_triple() -> bool:
        return bool(
            ch_client.query(
                "SELECT count() FROM loom.entity_triples WHERE org = {o:String} "
                "AND namespace = {n:String} AND (subject, relation, object) = "
                "({s:String}, {r:String}, {b:String}) AND valid_to IS NULL "
                "AND invalid_at IS NULL",
                settings={**_SEQUENTIAL, "apply_mutations_on_fly": 1},
                parameters={"o": org, "n": ns, "s": stated[0], "r": stated[1], "b": stated[2]},
            ).result_rows[0][0]
        )

    for _ in range(100):  # the projection lands beside the write's answer
        if open_triple():
            break
        await asyncio.sleep(0.1)
    assert open_triple()
    candidate = sensitivity_sweep.SweepCandidate(
        memory_id=scanned["memory_id"],
        org=org,
        namespace=ns,
        agent="a",
        user_id="-",
        session_id="-",
        memory_key="k",
        level="high",
        shapes=("credential",),
        sort_key=dict(zip(SORT_KEY, key, strict=True)),
        version=scanned["version"],
    )

    from server.core import pg_lease

    async def refused(*_a: Any, **_k: Any) -> None:
        raise pg_lease.PublicationPending()

    publish = pg_publish.publish
    monkeypatch.setattr(pg_publish, "publish", refused)
    monkeypatch.setattr(projection_failures, "OWED_GRACE_S", 0)
    ch = get_client()
    await ch.connect()
    try:
        result = await sensitivity_sweep.apply([candidate], ch=ch)
        assert (result.high, result.failed) == (1, [])
        (owed,) = await projection_failures.list_projection_failures(
            org=org, namespace=ns, projection=projection_failures.SENSITIVITY_FACTS, ch=ch
        )
        (waiting,) = await projection_failures.retry_failures([owed], ch=ch)
        assert waiting.reason == "pending" and open_triple()
        monkeypatch.setattr(pg_publish, "publish", publish)
        await pg_conn.execute(
            "UPDATE loom.memory_deliveries SET lease_until = now() - interval '1 s' "
            "WHERE org = $1 AND namespace = $2",
            org,
            ns,
        )
        await pg_delivery._publish(await pg_memory.claim_due(10, scope=(org, ns)))
        (closed,) = await projection_failures.retry_failures([owed], ch=ch)
        assert closed.resolved
    finally:
        await ch.disconnect()
        await pg.close_pool()
    assert not open_triple()
    assert _live_sweep_vectors(ch_client, org, ns, "a") == 0


def _live_sweep_vectors(ch_client: Any, org: str, ns: str, agent: str) -> int:
    """The live search vectors of `agent`'s key `k`, read with sequential consistency."""
    return int(
        ch_client.query(
            "SELECT count() FROM loom.memory_embeddings FINAL WHERE org = {o:String} "
            "AND namespace = {n:String} AND agent = {a:String} AND memory_key = 'k' "
            "AND tombstoned_at IS NULL",
            settings=_SEQUENTIAL,
            parameters={"o": org, "n": ns, "a": agent},
        ).result_rows[0][0]
    )


async def test_postgres_orders_keys_as_the_commit_sorts_them(pg_conn: Any) -> None:
    """A commit locks heads in Python's order of its keys and every other locker in
    the table's: the key columns' "C" collation keeps the two one order, so two
    writers never take the same locks in opposite orders."""
    keys = ["b", "B", "a_1", "a-1", "Z", "é", "e", "ß", "日本", "😀", "a", "A"]
    ordered = await pg_conn.fetch(
        'SELECT k FROM unnest($1::text[]) AS k ORDER BY k COLLATE "C"', keys
    )
    assert [r["k"] for r in ordered] == sorted(keys)
    collations = await pg_conn.fetch(
        "SELECT table_name, column_name, collation_name FROM information_schema.columns "
        "WHERE table_schema = 'loom' AND table_name = 'memory_current' "
        "AND column_name IN ('org', 'namespace', 'agent', 'user_id', 'session_id', 'memory_key')"
    )
    assert {r["collation_name"] for r in collations} == {"C"}, collations


async def test_search_never_serves_a_hit_the_authority_moved_past(
    call: Any, pg_conn: Any, org: str
) -> None:
    """ClickHouse still ranks the old revision's document; the head in Postgres moved on
    to other content (a commit not yet published). The hit is dropped and counted."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    for key, value in (
        ("deploy", "deploys run on tuesday mornings"),
        ("other", "lunch is at noon"),
    ):
        assert (await call("memory.set", _set(ns, key, value))).status_code == 200
    query = {"namespace": ns, "agent": "a", "query": "when do deploys run", "top_k": 5}
    before = (await call("memory.search", query)).json()
    assert "deploy" in [h["memory_key"] for h in before["results"]], before
    assert before["stale_dropped"] == 0
    await pg_conn.execute(
        "UPDATE loom.memory_current SET version = version + 1, content_sha256 = 'changed', "
        "row = jsonb_set(row, '{version}', to_jsonb(version + 1)) "
        "WHERE org = $1 AND namespace = $2 AND memory_key = 'deploy'",
        org,
        ns,
    )
    after = (await call("memory.search", query)).json()
    assert "deploy" not in [h["memory_key"] for h in after["results"]], after
    assert after["stale_dropped"] == 1


async def test_a_search_sees_through_its_barrier(call: Any, pg_conn: Any, org: str) -> None:
    """A revision committed before the search and left unpublished by its writer: the
    search publishes it before it retrieves, and serves the new version."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    first = (await call("memory.set", _set(ns, "deploy", "deploys run on tuesday"))).json()
    async with pg_conn.transaction():
        seq = await pg_conn.fetchval(
            "UPDATE loom.scope_generations SET generation = generation + 1 "
            "WHERE org = $1 AND scope = $2 RETURNING generation",
            org,
            ns,
        )
        row = await pg_conn.fetchval(
            "UPDATE loom.memory_current SET version = version + 1, seq = $3, "
            "row = jsonb_set(row, '{version}', to_jsonb(version + 1)) "
            "WHERE org = $1 AND namespace = $2 RETURNING row",
            org,
            ns,
            seq,
        )
        await pg_conn.execute(
            "INSERT INTO loom.memory_revisions (org, namespace, seq, user_id, memory_id, "
            "version, content_sha256, actor, source_class, row, audit) "
            "SELECT org, namespace, $3, user_id, memory_id, version + 1, content_sha256, actor, "
            "source_class, $4::jsonb, audit FROM loom.memory_revisions "
            "WHERE org = $1 AND namespace = $2 ORDER BY seq DESC LIMIT 1",
            org,
            ns,
            seq,
            row,
        )
        await pg_conn.execute(
            "INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_until) "
            "VALUES ($1, $2, $3, 3, now() - interval '1 second')",
            org,
            ns,
            seq,
        )
    out = (
        await call("memory.search", {"namespace": ns, "agent": "a", "query": "when do deploys run"})
    ).json()
    hits = {h["memory_key"]: h["version"] for h in out["results"]}
    assert hits.get("deploy") == first["version"] + 1, out
    assert out["stale_dropped"] == 0


async def test_a_read_of_a_key_holding_a_nul_finds_nothing(pg_conn: Any, org: str) -> None:
    """Postgres text holds no NUL, so a key carrying one has no head: not found, as the
    ClickHouse authority answers, never a Postgres error."""
    from server.core import pg
    from server.memory.store import pg_memory

    scope = type("Scope", (), {"scope": "namespace", "narrowed": False})()
    try:
        key = (org, "n", "a", "-", "-", "k\x00")
        assert await pg_memory.head_by_key(key, scope=scope) is None
        assert await pg_memory.heads_by_keys([key]) == {}
    finally:
        await pg.close_pool()


async def test_another_users_head_of_the_key_never_vouches_for_a_deleted_hit(
    call: Any, pg_conn: Any, org: str
) -> None:
    """Users A and B hold one key; A's memory is deleted in Postgres while ClickHouse
    still ranks A's document. B's head under the same key does not make A's hit live."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    for user, value in (
        ("user-b", "the editor of choice is emacs"),
        ("user-a", "the editor of choice is vim"),
    ):
        r = await call("memory.set", _set(ns, "pref/editor", value, user_id=user))
        assert r.status_code == 200, r.text
    query = {"namespace": ns, "agent": "a", "query": "which editor of choice", "top_k": 5}
    before = (await call("memory.search", query)).json()
    assert len([h for h in before["results"] if h["memory_key"] == "pref/editor"]) == 2, before
    await pg_conn.execute(
        "UPDATE loom.memory_current SET row = jsonb_set(row, '{tombstoned_at}', "
        "to_jsonb('2026-01-01T00:00:00'::text)) "
        "WHERE org = $1 AND namespace = $2 AND user_id = 'user-a'",
        org,
        ns,
    )
    after = (await call("memory.search", query)).json()
    excerpts = [h["content_excerpt"] for h in after["results"] if h["memory_key"] == "pref/editor"]
    assert len(excerpts) == 1 and "emacs" in excerpts[0], after
    assert after["stale_dropped"] == 1


async def test_a_write_built_before_an_erasure_never_brings_the_memory_back(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A writer read the head, then the user's erasure ran: its commit is refused (404),
    and nothing of the memory is back in Postgres."""
    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (
        await call("memory.set", _set(ns, "k", "a fact about the user", user_id="u-erased"))
    ).status_code == 200
    row = await _head_row(pg_conn, org, ns)
    r = await call(
        "erasure.request", {"scope_type": "user_id", "scope_value": "u-erased", "requester": "t"}
    )
    assert r.json()["status"] == "completed", r.text
    row["version"] = int(row["version"]) + 1
    try:
        with pytest.raises(pg_memory.MemoryErased):
            await pg_memory.commit_versions([row], [{}])
    finally:
        await pg.close_pool()
    assert (
        await pg_conn.fetchval(
            "SELECT count(*) FROM loom.memory_current WHERE org = $1 AND namespace = $2", org, ns
        )
        == 0
    )


async def test_a_user_erasure_leaves_a_consolidated_row_another_namespace_built(
    call: Any, pg_conn: Any, org: str, ch_client: Any
) -> None:
    """A consolidated row is the user's dependent only where the user wrote its source
    key: a row of another namespace built from a key of the same name stays, in both
    stores."""
    mine, theirs = f"pg-{uuid.uuid4().hex[:8]}", f"pg-{uuid.uuid4().hex[:8]}"
    user = f"u-{uuid.uuid4().hex[:6]}"
    tags = {"tags": ["consolidated", "src:prefs/editor"]}
    for body in (
        _set(mine, "prefs/editor", "vim", user_id=user),
        _set(theirs, "prefs/editor", "emacs", user_id="someone-else"),
        _set(mine, "profile", "uses vim", **tags),
        _set(theirs, "profile", "uses emacs", **tags),
    ):
        assert (await call("memory.set", body)).status_code == 200
    r = await call(
        "erasure.request", {"scope_type": "user_id", "scope_value": user, "requester": "t"}
    )
    assert r.json()["status"] == "completed", r.text
    left = await pg_conn.fetch(
        "SELECT namespace, memory_key FROM loom.memory_current WHERE org = $1 "
        "ORDER BY namespace, memory_key",
        org,
    )
    assert [(x["namespace"], x["memory_key"]) for x in left] == [
        (theirs, "prefs/editor"),
        (theirs, "profile"),
    ]
    live = ch_client.query(
        "SELECT memory_key FROM loom.memory FINAL WHERE org = {o:String} "
        "AND namespace = {n:String} AND tombstoned_at IS NULL ORDER BY memory_key",
        settings=_SEQUENTIAL,
        parameters={"o": org, "n": theirs},
    ).result_rows
    assert [r[0] for r in live] == ["prefs/editor", "profile"]
    # The user's own summary is retired from ClickHouse's copy too.
    gone = ch_client.query(
        "SELECT count() FROM loom.memory FINAL WHERE org = {o:String} AND namespace = {n:String} "
        "AND memory_key = 'profile' AND tombstoned_at IS NULL",
        settings=_SEQUENTIAL,
        parameters={"o": org, "n": mine},
    ).result_rows
    assert gone[0][0] == 0


async def test_only_a_claim_taken_over_mid_publish_counts_an_attempt(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A claim is marked held just before its own publish: taken over while marked (its
    publish stopped the process) it counts one attempt; released (a pass cut short) or
    never reached, none."""
    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "a fact to deliver"))).status_code == 200

    async def _due() -> list[Any]:
        # Its revision owing its memory row again, with the lease run out.
        await pg_conn.execute(
            "INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_until) "
            "SELECT org, namespace, seq, 1, now() - interval '1 s' FROM loom.memory_revisions "
            "WHERE org = $1 AND namespace = $2 "
            "ON CONFLICT (org, namespace, seq) DO UPDATE SET lease_until = EXCLUDED.lease_until",
            org,
            ns,
        )
        return await pg_memory.claim_due(10, scope=(org, ns))

    async def _state() -> tuple[int, bool]:
        r = await pg_conn.fetchrow(
            "SELECT attempts, held FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2",
            org,
            ns,
        )
        return int(r["attempts"]), bool(r["held"])

    try:
        claimed = await _due()
        assert [c.taken_over for c in claimed] == [False] and await _state() == (0, False)
        await pg_memory.renew(claimed)  # its publish starts, and the process stops
        assert await _state() == (0, True)
        claimed = await _due()
        assert [c.taken_over for c in claimed] == [True] and await _state() == (1, False)
        await pg_memory.release(await pg_memory.renew(claimed))  # a pass cut short
        claimed = await _due()
        assert [c.taken_over for c in claimed] == [False] and await _state() == (1, False)
    finally:
        await pg_conn.execute(
            "DELETE FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2", org, ns
        )
        await pg.close_pool()


async def test_a_redaction_refuses_a_lock_on_a_profile_key_rebuilt_without_it(
    call: Any, pg_conn: Any, org: str
) -> None:
    """The lock a redaction checks inside its transaction is on every profile key with a
    revision built from the memory, as the handler's check reads them: one whose head was
    rebuilt from other memories since still refuses the scrub of its older revision."""
    from fastapi import HTTPException

    from server.core import pg
    from server.memory.store import pg_memory, rows

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    fact = (await call("memory.set", _set(ns, "k", "the badge pin is 4321"))).json()
    head = await _head_row(pg_conn, org, ns)
    profile = {
        **head,
        "agent": "-",
        "memory_key": "profile",
        "memory_type": "profile",
        "memory_id": str(uuid.uuid4()),
        "source_trace": f"profile.fold:{fact['memory_id']}",
        "version": rows.next_version(),
    }
    rebuilt = {
        **profile,
        "source_trace": f"profile.fold:{uuid.uuid4()}",
        "version": rows.next_version(),
        "lock_mode": "no_delete",
        "lock_reason": "audit",
    }
    try:
        await pg_memory.commit_versions([profile], [{}])
        await pg_memory.commit_versions([rebuilt], [{}])
        with pytest.raises(HTTPException) as refused:
            await pg_memory.redact(
                (org, ns, "a", "-", "-", "k"),
                fact["memory_id"],
                int(fact["version"]),
                when=datetime.now(UTC),
                reason_blob="",
                head=(fact["memory_id"], int(fact["version"])),
            )
        assert refused.value.status_code == 423
    finally:
        await pg.close_pool()
    still = await pg_conn.fetchval(
        "SELECT count(*) FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 "
        "AND row->>'redacted_at' IS NOT NULL",
        org,
        ns,
    )
    assert still == 0


async def test_a_copy_over_a_head_locked_since_its_read_answers_contended(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A copy writer (a delete's tombstone, a touch) built from a head read before a lock
    never drops the lock: 503, and its retry reads the locked head."""
    from server.core import pg
    from server.memory.store import lifecycle, pg_memory, rows

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "a fact to lock"))).status_code == 200
    read = rows._hydrate(await _head_row(pg_conn, org, ns))
    locked = await call(
        "memory.lock",
        {"namespace": ns, "agent": "a", "memory_key": "k", "mode": "no_delete", "reason": "audit"},
    )
    assert locked.json()["locked"] is True, locked.text
    tombstone = rows.build_memory_version_row(
        read, version=rows.next_version(), tombstoned_at=datetime.now(UTC)
    )
    try:
        with pytest.raises(pg_memory.AdmissionContendedError):
            await lifecycle.insert_versions_over_heads([tombstone], [read])
    finally:
        await pg.close_pool()
    head = await _head_row(pg_conn, org, ns)
    assert head.get("lock_mode") == "no_delete" and head.get("tombstoned_at") is None


async def test_a_redaction_over_a_head_written_since_its_check_answers_contended(
    call: Any, pg_conn: Any, org: str
) -> None:
    """The live head a redaction checked is checked again inside its transaction: one
    written or restored since answers 503 and nothing is scrubbed."""
    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    first = (await call("memory.set", _set(ns, "k", "the pin is 0000"))).json()
    second = (await call("memory.set", _set(ns, "k", "the pin changed"))).json()
    assert (await call("memory.set", _set(ns, "k", "the pin changed again"))).status_code == 200
    try:
        with pytest.raises(pg_memory.AdmissionContendedError):
            await pg_memory.redact(
                (org, ns, "a", "-", "-", "k"),
                first["memory_id"],
                int(first["version"]),
                when=datetime.now(UTC),
                reason_blob="",
                head=(second["memory_id"], int(second["version"])),  # before the last write
            )
    finally:
        await pg.close_pool()
    scrubbed = await pg_conn.fetchval(
        "SELECT count(*) FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 "
        "AND row->>'redacted_at' IS NOT NULL",
        org,
        ns,
    )
    assert scrubbed == 0


async def test_a_redaction_scrubs_the_copies_of_its_version_on_the_key(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A touch and the delete copied the redacted version's text: the transaction scrubs
    them with it, in the journal and in the key's tombstoned head, and returns them, on a
    retry too."""
    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    first = (await call("memory.set", _set(ns, "k", "the safe code is 4711"))).json()
    touched = await call(
        "memory.touch", {"namespace": ns, "agent": "a", "memory_key": "k", "ttl_seconds": 3600}
    )
    assert touched.status_code == 200, touched.text
    gone = await call("memory.delete", {"namespace": ns, "agent": "a", "memory_key": "k"})
    assert gone.status_code == 200, gone.text
    later = [
        int(r["version"])
        for r in await pg_conn.fetch(
            "SELECT version FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 "
            "AND version > $3 ORDER BY version",
            org,
            ns,
            int(first["version"]),
        )
    ]
    try:
        _, copies = await pg_memory.redact(
            (org, ns, "a", "-", "-", "k"),
            first["memory_id"],
            int(first["version"]),
            when=datetime.now(UTC),
            reason_blob="",
            head=None,
        )
        # A retry after a ClickHouse step failed names the same copies again; they keep
        # the first call's reason, the named version takes the retry's.
        _, again = await pg_memory.redact(
            (org, ns, "a", "-", "-", "k"),
            first["memory_id"],
            int(first["version"]),
            when=datetime.now(UTC),
            reason_blob="retry",
            head=None,
        )
    finally:
        await pg.close_pool()
    assert copies == again == later and len(later) == 2
    reasons = {
        int(r["version"]): r["reason"]
        for r in await pg_conn.fetch(
            "SELECT version, row->>'redaction_reason' AS reason FROM loom.memory_revisions "
            "WHERE org = $1 AND namespace = $2",
            org,
            ns,
        )
    }
    assert reasons == {int(first["version"]): "retry", **{v: "" for v in later}}
    texts = await pg_conn.fetch(
        "SELECT row->>'value' AS value FROM loom.memory_revisions WHERE org = $1 "
        "AND namespace = $2",
        org,
        ns,
    )
    assert all("4711" not in str(r["value"]) for r in texts)
    assert "4711" not in str((await _head_row(pg_conn, org, ns)).get("value"))


async def test_a_copy_of_a_version_redacted_since_its_read_answers_contended(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A rollback copies an old revision's text: a redaction of that revision committed
    after the copy read it refuses the copy, so its text never comes back as a head."""
    from server.core import pg
    from server.memory.store import lifecycle, pg_memory, rows

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    first = (await call("memory.set", _set(ns, "k", "the vault code is 9183"))).json()
    second = (await call("memory.set", _set(ns, "k", "the vault code changed"))).json()
    old = await pg_conn.fetchval(
        "SELECT row FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 AND version = $3",
        org,
        ns,
        int(first["version"]),
    )
    head = rows._hydrate(await _head_row(pg_conn, org, ns))
    copy = rows.build_memory_version_row(
        rows._hydrate(pg_memory.decode_row(old)),
        version=rows.version_over(head),
    )
    try:
        await pg_memory.redact(
            (org, ns, "a", "-", "-", "k"),
            first["memory_id"],
            int(first["version"]),
            when=datetime.now(UTC),
            reason_blob="",
            head=(second["memory_id"], int(second["version"])),
        )
        with pytest.raises(pg_memory.AdmissionContendedError):
            await lifecycle.insert_versions_over_heads(
                [copy], [head], sources=[(first["memory_id"], int(first["version"]))]
            )
    finally:
        await pg.close_pool()
    assert "9183" not in str((await _head_row(pg_conn, org, ns)).get("value"))


@pytest.mark.parametrize("since", ["deleted", "rewritten"])
async def test_a_summary_over_a_source_moved_since_its_read_answers_contended(
    call: Any, pg_conn: Any, org: str, since: str
) -> None:
    """A merge's summary names its merged rows as sources that must still be their keys'
    live heads: a source deleted or rewritten after the merge read it refuses the summary."""
    from server.core import pg
    from server.memory.store import lifecycle, pg_memory, rows

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    written = [(await call("memory.set", _set(ns, k, f"a fact {k}"))).json() for k in ("k1", "k2")]
    sources = [(w["memory_id"], int(w["version"])) for w in written]
    template = rows._hydrate(await _head_row(pg_conn, org, ns, "k2"))
    summary = rows.build_memory_version_row(
        template,
        memory_id=str(uuid.uuid4()),
        memory_key="summary",
        value="a fact both keys hold",
        version=rows.next_version(),
    )
    if since == "deleted":
        moved = await call("memory.delete", {"namespace": ns, "agent": "a", "memory_key": "k1"})
    else:
        moved = await call("memory.set", _set(ns, "k1", "a fact k1, changed"))
    assert moved.status_code == 200, moved.text
    try:
        with pytest.raises(pg_memory.AdmissionContendedError):
            await lifecycle.insert_versions_over_heads(
                [summary], [None], sources=sources, live_sources=True
            )
        assert not await pg_conn.fetchval(
            "SELECT count(*) FROM loom.memory_current WHERE org = $1 AND namespace = $2 "
            "AND memory_key = 'summary'",
            org,
            ns,
        )
        # the same copy of revisions that still exist, not asked to be live, commits
        await lifecycle.insert_versions_over_heads([summary], [None], sources=sources)
    finally:
        await pg.close_pool()
    assert (await _head_row(pg_conn, org, ns, "summary"))["value"] == "a fact both keys hold"


async def test_a_summary_only_the_authority_holds_goes_with_its_last_source(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A summary whose `src:` tags ClickHouse has not received yet is still found, and
    judged on the authority: deleting its last source tombstones its head there."""
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    for key in ("k1", "summary"):
        assert (await call("memory.set", _set(ns, key, f"a fact {key}"))).status_code == 200
    # The summary's revision naming k1 as its source is committed, its delivery owed.
    await _unpublished(
        pg_conn, org, ns, "summary", {"tags": ["consolidated", "src:k1"]}, pg_memory.MEMORY_ROW
    )
    gone = await call("memory.delete", {"namespace": ns, "agent": "a", "memory_key": "k1"})
    assert gone.status_code == 200, gone.text
    assert gone.json().get("dependents_tombstoned") == 1, gone.text
    head = await _head_row(pg_conn, org, ns, "summary")
    assert head.get("tombstoned_at") is not None


async def test_a_summary_left_a_source_is_downgraded_with_its_vectors_and_searchable_at_once(
    call: Any, pg_conn: Any, org: str, ch_client: Any
) -> None:
    """A summary that keeps a live source is downgraded under its commit's lease: its
    vectors take the downgrade at the row's version, the document they make is settled,
    so no delivery holds the namespace and a search answers at once."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    for key in ("k1", "k2"):
        assert (await call("memory.set", _set(ns, key, f"a fact {key}"))).status_code == 200
    tags = ["consolidated", "src:k1", "src:k2"]
    made = await call("memory.set", _set(ns, "summary", "a summary of k1 and k2", tags=tags))
    assert made.status_code == 200, made.text
    gone = await call("memory.delete", {"namespace": ns, "agent": "a", "memory_key": "k1"})
    assert gone.status_code == 200, gone.text
    assert gone.json().get("dependents_downgraded") == 1, gone.text
    head = await _head_row(pg_conn, org, ns, "summary")
    assert head["status"] == "conflicted" and "stale-source:k1" in head["tags"]
    owed = await pg_conn.fetchval(
        "SELECT count(*) FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2", org, ns
    )
    assert owed == 0
    found = await call("memory.search", {"namespace": ns, "query": "a summary of k1 and k2"})
    assert found.status_code == 200, found.text
    hits = {h["memory_key"]: h for h in found.json()["results"]}
    assert "summary" in hits and hits["summary"]["version"] == head["version"], found.text
    rows = ch_client.query(
        "SELECT version, status, tags FROM loom.memory_embeddings FINAL "
        "WHERE org = {o:String} AND namespace = {n:String} AND memory_key = 'summary'",
        settings=_SEQUENTIAL,
        parameters={"o": org, "n": ns},
    ).result_rows
    assert rows and all(
        int(v) == int(head["version"]) and st == "conflicted" and "stale-source:k1" in t
        for v, st, t in rows
    )


async def test_a_summary_left_no_source_is_tombstoned_with_its_vectors_holding_no_text(
    call: Any, pg_conn: Any, org: str, ch_client: Any
) -> None:
    """A summary whose last source goes is tombstoned, and its vectors are retired at the
    tombstone's version with no text or vector, so none outlives a redaction's purge."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k1", "a fact k1"))).status_code == 200
    tags = ["consolidated", "src:k1"]
    made = await call("memory.set", _set(ns, "summary", "a summary of k1", tags=tags))
    assert made.status_code == 200, made.text
    gone = await call("memory.delete", {"namespace": ns, "agent": "a", "memory_key": "k1"})
    assert gone.status_code == 200, gone.text
    assert gone.json().get("dependents_tombstoned") == 1, gone.text
    head = await _head_row(pg_conn, org, ns, "summary")
    assert head.get("tombstoned_at") is not None
    rows = ch_client.query(
        "SELECT version, tombstoned_at IS NOT NULL, content_excerpt, embedding_dim "
        "FROM loom.memory_embeddings FINAL "
        "WHERE org = {o:String} AND namespace = {n:String} AND memory_key = 'summary'",
        settings=_SEQUENTIAL,
        parameters={"o": org, "n": ns},
    ).result_rows
    assert rows and all(
        int(v) == int(head["version"]) and tombstoned and excerpt == "" and int(dim) == 0
        for v, tombstoned, excerpt, dim in rows
    ), rows


async def test_a_set_over_a_delete_from_a_clock_ahead_becomes_the_head(
    call: Any, pg_conn: Any, org: str
) -> None:
    """The tombstone is the authority's head and ClickHouse may not hold it: the set
    goes above it, or its commit lands below and the key still reads as deleted."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "the first value"))).status_code == 200
    gone = await call("memory.delete", {"namespace": ns, "agent": "a", "memory_key": "k"})
    assert gone.status_code == 200, gone.text
    ahead = int(time.time_ns()) + 15 * 60 * 10**9  # a replica whose clock runs 15 min ahead
    await pg_conn.execute(
        "UPDATE loom.memory_current SET version = $3, "
        "row = jsonb_set(row, '{version}', to_jsonb($3::bigint)) "
        "WHERE org = $1 AND namespace = $2 AND memory_key = 'k'",
        org,
        ns,
        ahead,
    )
    written = await call("memory.set", _set(ns, "k", "the value written after"))
    assert written.status_code == 200, written.text
    assert int(written.json()["version"]) > ahead
    head = await _head_row(pg_conn, org, ns, "k")
    assert head.get("tombstoned_at") is None and "written after" in str(head.get("value"))


async def test_a_redaction_leaves_another_key_holding_the_same_id_alone(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A namespace restored into itself under another agent holds the memory's id under a
    second key, with the same text: the redaction of the first key's version scrubs that
    key's copies only, and the target is the version's own key."""
    from server.core import pg
    from server.memory.store import pg_memory, rows
    from server.memory.store.pg_publish import audit_row_for

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    first = (await call("memory.set", _set(ns, "k", "the badge number is 99"))).json()
    copy = {**await _head_row(pg_conn, org, ns), "agent": "b", "version": rows.next_version()}
    gone = await call("memory.delete", {"namespace": ns, "agent": "a", "memory_key": "k"})
    assert gone.status_code == 200, gone.text
    tombstone = await pg_conn.fetchval(
        "SELECT version FROM loom.memory_current WHERE org = $1 AND namespace = $2 AND agent = 'a'",
        org,
        ns,
    )
    try:
        await pg_memory.commit_versions([copy], [audit_row_for(copy, request_id="", writer="")])
        _, copies = await pg_memory.redact(
            (org, ns, "a", "-", "-", "k"),
            first["memory_id"],
            int(first["version"]),
            when=datetime.now(UTC),
            reason_blob="",
            head=None,
        )
        # The redaction's target is the named version's own key, not the id's newest row.
        target = await pg_memory.history_target(org, first["memory_id"], int(first["version"]))
    finally:
        await pg.close_pool()
    assert target is not None and target["agent"] == "a"
    assert copies == [int(tombstone)]
    other = await pg_conn.fetchrow(
        "SELECT row->>'redacted_at' AS redacted, row->>'value' AS value "
        "FROM loom.memory_current WHERE org = $1 AND namespace = $2 AND agent = 'b'",
        org,
        ns,
    )
    assert other["redacted"] is None and "99" in other["value"]
    revisions = {
        (r["agent"], int(r["version"])): r["value"]
        for r in await pg_conn.fetch(
            "SELECT row->>'agent' AS agent, version, row->>'value' AS value "
            "FROM loom.memory_revisions WHERE org = $1 AND namespace = $2",
            org,
            ns,
        )
    }
    assert "99" not in revisions[("a", int(tombstone))]
    assert "99" in revisions[("b", int(copy["version"]))]


async def test_a_search_document_is_judged_by_its_own_keys_revision(
    call: Any, pg_conn: Any, org: str
) -> None:
    """The same id under two keys (a namespace restored into itself under another agent),
    the first written again: the copy's document, its version lifted above that write,
    is judged by its own key's revision and stays live."""
    from server.core import pg
    from server.memory.store import pg_memory, rows
    from server.memory.store.pg_publish import audit_row_for

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "the badge number is 99"))).status_code == 200
    head = await _head_row(pg_conn, org, ns)
    copy = {**head, "agent": "b", "version": rows.next_version()}
    try:
        await pg_memory.commit_versions([copy], [audit_row_for(copy, request_id="", writer="")])
        rewritten = (await call("memory.set", _set(ns, "k", "the badge was reissued"))).json()
        lifted = int(rewritten["version"]) + 1
        doc = (ns, "b", "-", "-", "k", str(head["memory_type"]), lifted)
        live = await pg_memory.live_versions(org, [doc])
    finally:
        await pg.close_pool()
    assert live == {doc}


async def test_a_delete_not_yet_in_clickhouse_is_restored(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A restore routes from the head Postgres holds: a delete committed there and not yet
    delivered to ClickHouse, where the key still reads live, is undone."""
    from server.core import pg
    from server.memory.store import pg_memory, rows

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "a fact to delete"))).status_code == 200
    head = rows._hydrate(await _head_row(pg_conn, org, ns))
    tombstone = rows.build_memory_version_row(
        head, version=rows.next_version(), tombstoned_at=datetime.now(UTC)
    )
    try:
        await pg_memory.commit_versions([tombstone], [{}])  # committed, not published
    finally:
        await pg.close_pool()
    # Held, so the delivery worker cannot publish the tombstone before the restore runs.
    await pg_conn.execute(
        "UPDATE loom.memory_deliveries SET lease_until = now() + interval '10 minutes' "
        "WHERE org = $1 AND namespace = $2",
        org,
        ns,
    )
    restored = await call("memory.restore", {"namespace": ns, "agent": "a", "memory_key": "k"})
    assert restored.status_code == 200 and restored.json()["restored"] is True, restored.text
    assert (await _head_row(pg_conn, org, ns)).get("tombstoned_at") is None
    await pg_conn.execute(
        "DELETE FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2", org, ns
    )


async def test_an_export_shows_the_authoritys_heads_before_clickhouse_has_them(
    call: Any, pg_conn: Any, org: str
) -> None:
    """`erasure.export` reads memory from the authority: a key committed and not yet
    delivered to ClickHouse is in it, and a delete committed and not yet delivered
    takes its key out."""
    from server.core import pg
    from server.memory.store import pg_memory, rows

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "gone", "a fact to delete"))).status_code == 200
    head = rows._hydrate(await _head_row(pg_conn, org, ns))
    tombstone = rows.build_memory_version_row(
        head, version=rows.next_version(), tombstoned_at=datetime.now(UTC)
    )
    added = rows.build_memory_version_row(
        head,
        memory_key="new",
        memory_id=uuid.uuid4(),
        value='"a fact only Postgres holds"',
        version=rows.next_version(),
    )
    try:
        await pg_memory.commit_versions([tombstone], [{}])  # committed, not published
        await pg_memory.commit_versions([added], [{}])
    finally:
        await pg.close_pool()
    # Held, so the delivery worker cannot publish them before the export reads.
    await pg_conn.execute(
        "UPDATE loom.memory_deliveries SET lease_until = now() + interval '10 minutes' "
        "WHERE org = $1 AND namespace = $2",
        org,
        ns,
    )
    try:
        r = await call(
            "erasure.export",
            {
                "scope_type": "namespace",
                "scope_value": ns,
                "requester": "t",
                "include_tombstoned": False,
            },
        )
        assert r.status_code == 200, r.text
        exported = r.json()["memory"]["rows"]
        assert [(m["memory_key"], m["value"]) for m in exported] == [
            ("new", '"a fact only Postgres holds"')
        ]
        r = await call(
            "erasure.export", {"scope_type": "namespace", "scope_value": ns, "requester": "t"}
        )
        assert sorted(m["memory_key"] for m in r.json()["memory"]["rows"]) == ["gone", "new"]
    finally:
        await pg_conn.execute(
            "DELETE FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2", org, ns
        )


async def test_a_users_export_shows_only_visible_heads_before_its_limit(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A `user_id` export reads the user's heads across namespaces, leaves out the
    hidden ones and the collections not listed before the LIMIT, so a hidden head never
    takes a visible head's place."""
    from server.core import pg
    from server.erasure.store.exports import Shown, export_memory_heads
    from server.memory.store import pg_memory, rows

    tag = uuid.uuid4().hex[:8]
    hidden, shown_ns, unlisted, other = (
        f"pg-a-{tag}",
        f"pg-b-{tag}",
        f"col-c-{tag}",
        f"pg-d-{tag}",
    )
    for ns in (hidden, shown_ns):
        r = await call("memory.set", _set(ns, "k", f"fact in {ns}", user_id="u-exp"))
        assert r.status_code == 200, r.text
    r = await call("memory.set", _set(other, "k", "another user's fact", user_id="u-else"))
    assert r.status_code == 200, r.text
    # A collection's head, committed straight to the authority (writing a collection
    # takes a grant to it): first in key order, so a filter after the LIMIT would
    # serve nothing.
    head = rows._hydrate(await _head_row(pg_conn, org, shown_ns))
    in_collection = rows.build_memory_version_row(
        head, namespace=unlisted, memory_id=uuid.uuid4(), version=rows.next_version()
    )
    try:
        await pg_memory.commit_versions([in_collection], [{}])
        got = await export_memory_heads(
            org,
            "user_id",
            "u-exp",
            include_tombstoned=True,
            limit=1,
            shown=Shown(hidden=(hidden,), collections=()),
        )
        every = await export_memory_heads(org, "user_id", "u-exp", include_tombstoned=True, limit=9)
    finally:
        await pg.close_pool()
        await pg_conn.execute(
            "DELETE FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2", org, unlisted
        )
    assert [m["namespace"] for m in got] == [shown_ns]
    assert [m["namespace"] for m in every] == sorted([hidden, shown_ns, unlisted])


async def test_a_label_or_a_demote_read_before_a_lock_answers_contended(
    call: Any, pg_conn: Any, org: str
) -> None:
    """`memory.apply`'s label and demote built from a head read before a lock answer 503;
    the head keeps its lock and its tags and confidence."""
    from server.core import pg
    from server.memory.api.models_apply import ApplyDemote, ApplyLabel
    from server.memory.app import apply
    from server.memory.store import pg_memory, rows

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "a fact to label"))).status_code == 200
    read = rows._hydrate(await _head_row(pg_conn, org, ns))
    locked = await call(
        "memory.lock",
        {"namespace": ns, "agent": "a", "memory_key": "k", "mode": "no_delete", "reason": "audit"},
    )
    assert locked.json()["locked"] is True, locked.text
    try:
        with pytest.raises(pg_memory.AdmissionContendedError):
            await apply._label(read, ApplyLabel(tags=["reviewed"]), reason="")
        with pytest.raises(pg_memory.AdmissionContendedError):
            await apply._demote(read, ApplyDemote(confidence_factor=0.5), reason="")
    finally:
        await pg.close_pool()
    head = await _head_row(pg_conn, org, ns)
    assert head.get("lock_mode") == "no_delete" and "reviewed" not in (head.get("tags") or [])
    assert float(head["confidence"]) == float(read.confidence)


async def test_a_status_flip_read_before_a_lock_answers_contended(
    call: Any, pg_conn: Any, org: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`memory.dedup_resolve`'s status flip, handed a head read before a lock, answers 503
    and leaves the head as the lock left it."""
    from server.core import pg
    from server.memory.store import dedup, pg_memory, rows

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "a fact to tag"))).status_code == 200
    # tagged a duplicate when it was read
    stale = rows._hydrate(await _head_row(pg_conn, org, ns)).model_copy(
        update={"status": "duplicate", "duplicate_of": "canon-id"}
    )
    locked = await call(
        "memory.lock",
        {"namespace": ns, "agent": "a", "memory_key": "k", "mode": "no_delete", "reason": "audit"},
    )
    assert locked.json()["locked"] is True, locked.text

    async def _stale_by_id(*_: Any, **__: Any) -> Any:
        return stale

    monkeypatch.setattr(dedup, "get_by_id", _stale_by_id)
    try:
        with pytest.raises(pg_memory.AdmissionContendedError):
            await dedup.clear_duplicate_tag(
                org, str(stale.memory_id), namespace=ns, duplicate_of="canon-id"
            )
    finally:
        await pg.close_pool()
    head = await _head_row(pg_conn, org, ns)
    assert head.get("lock_mode") == "no_delete"


def _entity_item(org: str, ns: str, head: dict[str, Any], entities: list[str]) -> dict[str, Any]:
    """The entity pass's item for entities extracted from `head`, as the extractor sends it."""
    return {
        "org": org,
        "namespace": ns,
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": head["memory_key"],
        "version": int(head["version"]),
        "content_sha256": head["content_sha256"],
        "entities": entities,
    }


@pytest.mark.parametrize("since", ["locked", "revised"])
async def test_the_entity_pass_follows_the_text_it_read(
    call: Any, pg_conn: Any, org: str, since: str
) -> None:
    """Entities extracted before a lock land on the lock's version, which keeps its lock:
    it holds the same text. A revision to other text keeps its own entities."""
    from server.core import pg
    from server.core.ch import get_client
    from server.memory.store import entities

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "a fact to tag"))).status_code == 200
    read = await _head_row(pg_conn, org, ns, "k")
    if since == "locked":
        body = {
            "namespace": ns,
            "agent": "a",
            "memory_key": "k",
            "mode": "no_delete",
            "reason": "audit",
        }
        assert (await call("memory.lock", body)).json()["locked"] is True
    else:
        assert (await call("memory.set", _set(ns, "k", "another fact"))).status_code == 200
    ch = get_client()
    await ch.connect()
    try:
        written = await entities.apply_entities_batch([_entity_item(org, ns, read, ["vault"])])
    finally:
        await ch.disconnect()
        await pg.close_pool()
    head = await _head_row(pg_conn, org, ns, "k")
    if since == "locked":
        assert written["memory"] == 1 and head["entities"] == ["vault"]
        assert head["lock_mode"] == "no_delete" and int(head["version"]) > int(read["version"])
    else:
        assert written["memory"] == 0 and "vault" not in (head.get("entities") or [])


async def test_the_entity_pass_lands_the_keys_that_did_not_move(
    call: Any, pg_conn: Any, org: str, ch_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write to one key between the pass's read and its commit refuses the commit; the
    pass reads again and writes the other key's entities, on its head and on its search
    document, and the newer write's document keeps its own."""
    from server.core import pg
    from server.core.ch import get_client
    from server.memory.store import entities

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    for key in ("k1", "k2"):
        assert (
            await call("memory.set", _set(ns, key, f"{key} works with Alice"))
        ).status_code == 200
    items = [
        _entity_item(org, ns, await _head_row(pg_conn, org, ns, key), [key]) for key in ("k1", "k2")
    ]
    real = entities.insert_versions_over_heads
    commits: list[list[str]] = []

    async def write_k2_first(rows: Any, read: Any, *, ch: Any, then: Any) -> Any:
        commits.append(sorted(r["memory_key"] for r in rows))
        if len(commits) == 1:
            moved = await call("memory.set", _set(ns, "k2", "k2 works with Bob"))
            assert moved.status_code == 200, moved.text
        return await real(rows, read, ch=ch, then=then)

    monkeypatch.setattr(entities, "insert_versions_over_heads", write_k2_first)
    ch = get_client()
    await ch.connect()
    try:
        written = await entities.apply_entities_batch(items)
    finally:
        await ch.disconnect()
        await pg.close_pool()
    assert commits == [["k1", "k2"], ["k1"]]
    assert written["memory"] == 1
    assert (await _head_row(pg_conn, org, ns, "k1"))["entities"] == ["k1"]
    k2 = await _head_row(pg_conn, org, ns, "k2")
    assert k2["value"] == '"k2 works with Bob"' and "k2" not in (k2.get("entities") or [])
    documents = ch_client.query(
        "SELECT memory_key, entities FROM loom.memory_embeddings FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL",
        parameters={"org": org, "ns": ns},
    ).result_rows
    assert {key for key, _ in documents} == {"k1", "k2"}, documents
    assert all(entities == ["k1"] for key, entities in documents if key == "k1"), documents
    assert all("k2" not in entities for key, entities in documents if key == "k2"), documents


async def test_a_reject_is_the_authoritys_head_and_clickhouses_row(
    call: Any, pg_conn: Any, org: str, ch_client: Any
) -> None:
    """memory.reject commits a revision over the head and publishes it: the Postgres head
    and ClickHouse's memory row both read rejected, with the reason."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "a fact to reject"))).status_code == 200
    before = int((await _head_row(pg_conn, org, ns, "k"))["version"])
    body = {"namespace": ns, "agent": "a", "memory_key": "k", "rejection_reason": "wrong"}
    rejected = await call("memory.reject", body)
    assert rejected.status_code == 200 and rejected.json()["rejected"] is True, rejected.text
    head = await _head_row(pg_conn, org, ns, "k")
    assert (head["status"], head["rejection_reason"]) == ("rejected", "wrong")
    assert int(head["version"]) > before
    row = ch_client.query(
        "SELECT status, rejection_reason FROM loom.memory FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND memory_key = 'k'",
        parameters={"org": org, "ns": ns},
    ).result_rows
    assert row == [("rejected", "wrong")]

    # the vectors are retired at the head's version too, so search serves no hit of it
    async def document_rejected() -> bool:
        rows = ch_client.query(
            "SELECT status, version FROM loom.memory_embeddings FINAL "
            "WHERE org = {org:String} AND namespace = {ns:String} AND memory_key = 'k'",
            settings=_SEQUENTIAL,
            parameters={"org": org, "ns": ns},
        ).result_rows
        return rows == [("rejected", int(head["version"]))]

    await eventually(document_rejected, description="the vector row reads rejected at the head")


async def test_health_reports_the_synchronous_standby(
    control_plane_url: str, pg_authority: str
) -> None:
    """The CI primary waits for its one standby (`dev/ci_start_postgres.sh`)."""
    if os.environ.get("LOOM_PG_STANDBY") == "0":
        pytest.skip("Postgres started without a synchronous standby")
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        health = (await c.get("/v1/health?include=postgres")).json()
    assert health["postgres"]["standby"] == "synchronous", health


async def test_a_strong_bulk_read_opens_the_axes_as_clickhouse_does(
    call: Any, pg_conn: Any, org: str
) -> None:
    """The heads a bulk touch or a proposal decision copies come from Postgres with the
    identity's axes opened as ClickHouse's bulk read opens them: '*' opens an axis, a
    pinned user filters under the wildcard agent, and the newest head answers a key."""
    from server.core import pg
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import reads

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    for agent, user, value in (("a", "u1", "u1's note"), ("b", "u2", "u2's note")):
        body = {**_set(ns, "k", value), "agent": agent, "user_id": user}
        assert (await call("memory.set", body)).status_code == 200, value

    async def _users(agent: str, user: str) -> list[str]:
        ident = reads.Identity(org=org, namespace=ns, agent=agent, user_id=user, session_id="-")
        got = await reads.get_many_by_keys(ident, ["k", "none"], strong=True, scope=WHOLE_NAMESPACE)
        return [r.user_id for r in got]

    try:
        assert await _users("*", "u1") == ["u1"]
        assert await _users("a", "*") == ["u1"]
        assert await _users("*", "-") == ["u2"]  # every user: the newest head
        assert await _users("a", "u2") == []
    finally:
        await pg.close_pool()


async def test_a_long_value_found_through_its_chunk_is_served(call: Any, pg_conn: Any) -> None:
    """A long value whose match is only in a passage past its embedded head comes back as
    its memory: the chunk folded onto it is checked by its parent's key."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    filler = " ".join(f"routine maintenance log line {i} with nothing notable." for i in range(90))
    value = filler + " The quarterly zebrafish calibration happens in the north lab."
    assert (await call("memory.set", _set(ns, "lab-log", value))).status_code == 200
    out = (
        await call(
            "memory.search",
            {"namespace": ns, "agent": "a", "query": "zebrafish calibration north lab"},
        )
    ).json()
    assert "lab-log" in [h["memory_key"] for h in out["results"]], out
    assert out["stale_dropped"] == 0


async def _parked_page(call: Any, query: dict[str, Any], handle: str) -> dict[str, Any]:
    for _ in range(50):
        page = (await call("memory.search", {**query, "page": handle})).json()
        if page.get("abstain_code") != "page_expired":
            return page
        await asyncio.sleep(0.1)
    raise AssertionError(f"page {handle} was never readable")


async def test_a_page_is_refilled_and_the_next_page_skips_the_stale_hit(
    call: Any, pg_conn: Any, org: str
) -> None:
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    for i in range(8):
        r = await call("memory.set", _set(ns, f"deploy-{i}", f"deploy note {i}: the release train"))
        assert r.status_code == 200, r.text
    query = {"namespace": ns, "agent": "a", "query": "deploy note release train", "top_k": 3}
    first = (await call("memory.search", {**query, "paging": True})).json()
    keys = [h["memory_key"] for h in first["results"]]
    assert len(keys) == 3 and first["next_page"], first
    # One served key and one parked key are deleted in Postgres only. A page is parked
    # by an asynchronous insert: read it once it is there.
    parked = await _parked_page(call, query, first["next_page"])
    victims = [keys[0], parked["results"][0]["memory_key"]]
    await pg_conn.execute(
        "UPDATE loom.memory_current SET row = jsonb_set(row, '{tombstoned_at}', "
        "to_jsonb('2026-01-01T00:00:00'::text)) "
        "WHERE org = $1 AND namespace = $2 AND memory_key = ANY($3::text[])",
        org,
        ns,
        victims,
    )
    again = (await call("memory.search", {**query, "paging": True})).json()
    served = [h["memory_key"] for h in again["results"]]
    assert len(served) == 3 and not set(served) & set(victims), again
    assert again["stale_dropped"] >= 1
    later = await _parked_page(call, query, again["next_page"])
    assert not {h["memory_key"] for h in later["results"]} & set(victims), later


async def test_the_census_finds_an_erased_memory_that_came_back(
    call: Any, pg_conn: Any, org: str
) -> None:
    from server.core import pg
    from server.erasure.store.steps_pg import pg_residue

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "a fact to erase"))).status_code == 200
    await pg_conn.execute(
        "CREATE TEMP TABLE kept AS SELECT * FROM loom.memory_current "
        f"WHERE org = '{org}' AND namespace = '{ns}'"
    )
    r = await call(
        "erasure.request", {"scope_type": "namespace", "scope_value": ns, "requester": "t"}
    )
    assert r.json()["status"] == "completed", r.text
    # The head is put back, as a write that slipped past the erasure would.
    columns = "org, namespace, agent, user_id, session_id, memory_key, memory_id, version, seq, content_sha256, row"
    await pg_conn.execute(f"INSERT INTO loom.memory_current ({columns}) SELECT {columns} FROM kept")
    try:
        residue = await pg_residue(org, "namespace", ns)
    finally:
        await pg.close_pool()
    assert residue["pg:loom.memory_current"] == 1, residue


async def test_a_folded_chunk_is_served_by_its_writers_head(
    call: Any, pg_conn: Any, org: str
) -> None:
    """The check a chunk folded onto its parent gets, against the real heads: its
    writer's live head of the parent key passes, another user's does not."""
    from server.core import pg
    from server.memory.api.models_search import _MemorySearchHit
    from server.memory.app.search import pg_validation
    from server.memory.app.set_chunks import set_chunk_identities

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "long", "a long value " * 200))).status_code == 200

    def folded(user: str) -> Any:
        hit = _MemorySearchHit(
            memory_key="long",
            memory_type="semantic",
            content_excerpt="x",
            distance=0.1,
            similarity=0.9,
            namespace=ns,
            score_components={"folded_from_chunk": 1.0},
        )
        index, sha = min(set_chunk_identities("a long value " * 200, "semantic", "long"))
        hit._row_chunk = ("a", user, index, sha)
        return hit

    try:
        stale = await pg_validation.stale_flags(org, ns, [folded("-"), folded("someone")])
    finally:
        await pg.close_pool()
    assert stale == [False, True]


async def test_a_namespace_named_like_a_policy_scope_is_refused(call: Any, pg_conn: Any) -> None:
    r = await call("memory.set", _set("*", "k", "a fact"))
    assert r.status_code in (400, 403, 422), r.text


async def test_an_erasure_takes_an_in_flight_requests_claim(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A claim a request still holds is fenced by the org's erasure: its commit then
    finds no claim under its token and is refused, so it stores no answer."""
    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "a fact in flight"))).status_code == 200
    row = await _head_row(pg_conn, org, ns)
    key = uuid.uuid4().hex
    await pg_conn.execute(
        "INSERT INTO loom.idempotency (org, key_hash, subject, operation, request_digest, "
        "state, lease_token, lease_until, expires_at) VALUES ($1, $2, 's', 'memory.set', 'h', "
        "'running', 1, now() + interval '1 minute', now() + interval '1 day')",
        org,
        key,
    )
    r = await call(
        "erasure.request", {"scope_type": "namespace", "scope_value": ns, "requester": "t"}
    )
    assert r.json()["status"] == "completed", r.text
    # Fenced, not deleted: a retry takes it over above the erased request's token.
    assert (
        await pg_conn.fetchval(
            "SELECT lease_token FROM loom.idempotency WHERE org = $1 AND key_hash = $2", org, key
        )
        == 2
    )
    row["memory_id"], row["version"] = str(uuid.uuid4()), int(row["version"]) + 1
    try:
        with pytest.raises(pg_memory.IdempotencyFenced):
            await pg_memory.commit_versions([row], [{}], idempotency=(org, key, 1))
    finally:
        await pg.close_pool()


async def test_scoped_claims_come_back_in_journal_order(call: Any, pg_conn: Any, org: str) -> None:
    """A namespace's claims come back in the order they were taken, its journal's: the
    pass publishes them oldest first."""
    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    for i in range(6):
        assert (await call("memory.set", _set(ns, f"k{i}", f"fact {i}"))).status_code == 200
    seqs = [
        r["seq"]
        for r in await pg_conn.fetch(
            "SELECT seq FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 "
            "ORDER BY seq DESC",
            org,
            ns,
        )
    ]
    for seq in seqs:  # newest first, so the table's own order is not the answer
        await pg_conn.execute(
            "INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_until) "
            "VALUES ($1, $2, $3, 1, now() - interval '1 second') ON CONFLICT DO NOTHING",
            org,
            ns,
            seq,
        )
    try:
        claimed = await pg_memory.claim_due(10, scope=(org, ns))
        assert [c.seq for c in claimed] == sorted(seqs)
    finally:
        await pg_conn.execute(
            "DELETE FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2", org, ns
        )
        await pg.close_pool()


async def test_a_delivery_past_its_attempts_is_parked_or_gets_its_stub_at_its_claim(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A delivery past LOOM_PG_DELIVERY_MAX_ATTEMPTS that is not parked (the setting was
    lowered) never holds its namespace back unseen: one owing its memory row, only its
    audit row or only its decision record's retirement is parked by the claim, and one
    owing its search document and not its memory row is claimed for its stub, unless that
    stub stopped the process (held)."""
    from server.core import pg
    from server.memory.store import pg_memory
    from server.settings import settings

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    for i in range(6):
        assert (await call("memory.set", _set(ns, f"k{i}", f"fact {i}"))).status_code == 200
    seqs = [
        r["seq"]
        for r in await pg_conn.fetch(
            "SELECT seq FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 ORDER BY seq",
            org,
            ns,
        )
    ]
    # The fifth owes only its document, and its stub stopped the process; the last owes
    # only its decision record's retirement.
    owes = (1, 2, 4, 6, 2, pg_memory.DECISION_RECORD)
    for seq, targets, held in zip(seqs, owes, (False,) * 4 + (True, False), strict=True):
        await pg_conn.execute(
            "INSERT INTO loom.memory_deliveries "
            "(org, namespace, seq, targets, lease_until, attempts, held) "
            "VALUES ($1, $2, $3, $4, now() - interval '1 second', $5, $6)",
            org,
            ns,
            seq,
            targets,
            settings.pg_delivery_max_attempts,
            held,
        )
    try:
        claimed = await pg_memory.claim_due(10, scope=(org, ns))
        assert sorted(c.targets for c in claimed) == [2, 6]
        assert await pg_memory.claim_due(10, scope=(org, ns)) == []  # the rest are leased
    finally:
        await pg.close_pool()
    parked = await pg_conn.fetch(
        "SELECT targets, attempts, fenced_until BETWEEN now() - interval '1 minute' "
        "AND now() AS kept FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2 "
        "AND lease_until = 'infinity' ORDER BY targets",
        org,
        ns,
    )
    limit = settings.pg_delivery_max_attempts
    assert [(r["targets"], r["attempts"]) for r in parked] == [
        (1, limit),
        (2, limit + 1),
        (4, limit),
        (pg_memory.DECISION_RECORD, limit),
    ]
    # Each park keeps the last holder's lease, which a wait still covers.
    assert all(r["kept"] for r in parked)


async def test_a_hit_of_another_type_or_older_content_is_not_served(
    call: Any, pg_conn: Any, org: str
) -> None:
    from server.core import pg
    from server.memory.api.models_search import _MemorySearchHit
    from server.memory.app.search import pg_validation

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    first = (await call("memory.set", _set(ns, "k", "the first content"))).json()
    second = (await call("memory.set", _set(ns, "k", "the second content"))).json()

    def hit(memory_type: str, version: int) -> Any:
        h = _MemorySearchHit(
            memory_key="k",
            memory_type=memory_type,
            content_excerpt="x",
            distance=0.1,
            similarity=0.9,
            namespace=ns,
        )
        h._row_identity, h._row_version = ("a", "-", "-"), version
        return h

    current = hit("semantic", second["version"])
    try:
        stale = await pg_validation.stale_flags(
            org,
            ns,
            [current, hit("episodic", second["version"]), hit("semantic", first["version"])],
        )
    finally:
        await pg.close_pool()
    assert stale == [False, True, True]


async def test_a_retry_that_retakes_an_erased_claim_never_lets_the_old_request_commit(
    call: Any, pg_conn: Any, org: str
) -> None:
    """An erasure fences a claim in flight (its token advanced); a retry then takes the
    key over above it. The original request, still holding the first token, is refused."""
    from server.core import pg
    from server.erasure.store.steps_pg import delete_pg_idempotency
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "a fact"))).status_code == 200
    row = await _head_row(pg_conn, org, ns)
    key = uuid.uuid4().hex
    await pg_conn.execute(
        "INSERT INTO loom.idempotency (org, key_hash, subject, operation, request_digest, "
        "state, lease_token, lease_until, expires_at) VALUES ($1, $2, 's', 'memory.set', 'h', "
        "'running', 1, now() + interval '1 minute', now() + interval '1 day')",
        org,
        key,
    )
    row["memory_id"], row["memory_key"] = str(uuid.uuid4()), "k2"
    try:
        await delete_pg_idempotency(org)
        # The retry's takeover, as `check_owned` does past the claim's lease.
        await pg_conn.execute(
            "UPDATE loom.idempotency SET lease_token = lease_token + 1, "
            "lease_until = now() + interval '1 minute' WHERE org = $1 AND key_hash = $2",
            org,
            key,
        )
        with pytest.raises(pg_memory.IdempotencyFenced):
            await pg_memory.commit_versions([row], [{}], idempotency=(org, key, 1))
    finally:
        await pg.close_pool()
    assert (
        await pg_conn.fetchval(
            "SELECT lease_token FROM loom.idempotency WHERE org = $1 AND key_hash = $2", org, key
        )
        == 3
    )


async def test_a_redaction_fences_the_claim_still_sending_the_old_text(
    call: Any, pg_conn: Any, org: str, ch_client: Any
) -> None:
    """A delivery claimed before a redaction is fenced by it: its holder can no longer
    renew it, and the next claim publishes the scrubbed text."""
    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    first = (await call("memory.set", _set(ns, "k", "the token is hunter2"))).json()
    assert (await call("memory.set", _set(ns, "k", "the token was rotated"))).status_code == 200
    seq = await pg_conn.fetchval(
        "SELECT seq FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 AND version = $3",
        org,
        ns,
        first["version"],
    )
    # A worker's claim of the first revision, its lease run out long ago and no one else's
    # claim since: its holder (a slow publish) would renew it under the same token.
    await pg_conn.execute(
        "INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_token, "
        "lease_until) VALUES ($1, $2, $3, $4, 2, now() - interval '10 minutes')",
        org,
        ns,
        seq,
        pg_memory.MEMORY_ROW | pg_memory.SEARCH_DOC,
    )
    r = await call(
        "memory.redact",
        {"memory_id": first["memory_id"], "version": first["version"], "reason": "leak"},
    )
    assert r.status_code == 200 and r.json()["redacted"] is True, r.text
    fenced = await pg_conn.fetchrow(
        "SELECT lease_token, lease_until <= now() AS ended FROM loom.memory_deliveries "
        "WHERE org = $1 AND namespace = $2 AND seq = $3",
        org,
        ns,
        seq,
    )
    assert (fenced["lease_token"], fenced["ended"]) == (3, True)
    claim = pg_memory.Claimed(
        org=org,
        namespace=ns,
        seq=seq,
        targets=pg_memory.MEMORY_ROW | pg_memory.SEARCH_DOC,
        lease_token=2,
        attempts=0,
        row={},
        audit={},
        deadline=0.0,
    )
    try:
        assert await pg_memory.renew([claim]) == []
    finally:
        await pg.close_pool()
    # A search's barrier publishes the delivery under a new claim.
    r = await call("memory.search", {"namespace": ns, "query": "the token"})
    assert r.status_code == 200, r.text
    owed = await pg_conn.fetchval(
        "SELECT coalesce(max(targets), 0) FROM loom.memory_deliveries "
        "WHERE org = $1 AND namespace = $2 AND seq = $3",
        org,
        ns,
        seq,
    )
    assert owed == 0
    # Every copy, merged or not: the row republished and the history it fed.
    for table in ("loom.memory", "loom.memory_versions"):
        values = ch_client.query(
            f"SELECT value FROM {table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
            settings=_SEQUENTIAL,
            parameters={"o": org, "n": ns},
        ).result_rows
        assert values and not any("hunter2" in str(v) for (v,) in values), table


@pytest.mark.parametrize("lease", ["live", "parked"])
async def test_a_redaction_reads_a_live_claims_lease_and_leaves_a_parked_one_parked(
    call: Any, pg_conn: Any, org: str, lease: str
) -> None:
    """Called in-process: the wait it returns is the live holder's lease, which the fence
    ends and keeps, so a retry of the call waits it out again; a parked delivery (whose
    rows ClickHouse never took) is fenced but stays parked."""
    from datetime import UTC, datetime

    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    first = (await call("memory.set", _set(ns, "k", "the token is hunter2"))).json()
    assert (await call("memory.set", _set(ns, "k", "the token was rotated"))).status_code == 200
    seq = await pg_conn.fetchval(
        "SELECT seq FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 AND version = $3",
        org,
        ns,
        first["version"],
    )
    # A park keeps the lease it ended, here one past a second ago.
    until, fence = (
        ("now() + interval '30 seconds'", "NULL")
        if lease == "live"
        else ("'infinity'", "now() - interval '1 second'")
    )
    await pg_conn.execute(
        "INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_token, "
        f"lease_until, fenced_until) VALUES ($1, $2, $3, $4, 2, {until}, {fence})",
        org,
        ns,
        seq,
        pg_memory.MEMORY_ROW,
    )

    async def _redact() -> float:
        (left,), _ = await pg_memory.redact(
            (org, ns, "a", "-", "-", "k"),
            first["memory_id"],
            first["version"],
            when=datetime.now(UTC),
            reason_blob="leak",
            head=await _live_head(pg_conn, org, ns),
        )
        return float(left)

    try:
        left = await _redact()
        retried = await _redact()
    finally:
        await pg.close_pool()
    after = await pg_conn.fetchrow(
        "SELECT lease_token, lease_until = 'infinity' AS parked, lease_until <= now() AS ended "
        "FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2 AND seq = $3",
        org,
        ns,
        seq,
    )
    assert after["lease_token"] == 4
    if lease == "live":
        assert 25 < retried <= left <= 30 and after["ended"]
    else:
        assert -3 < retried <= left < 0 and after["parked"]


async def test_a_retried_redaction_waits_out_a_copy_it_scrubbed_before(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A touch copied the redacted text: the first call scrubs the copy and ends its
    delivery's lease, and a retry (one stopped while it waited) still reads that lease,
    though the copy is already redacted."""
    from datetime import UTC, datetime

    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    first = (await call("memory.set", _set(ns, "k", "the safe code is 4711"))).json()
    touched = await call(
        "memory.touch", {"namespace": ns, "agent": "a", "memory_key": "k", "ttl_seconds": 3600}
    )
    assert touched.status_code == 200, touched.text
    gone = await call("memory.delete", {"namespace": ns, "agent": "a", "memory_key": "k"})
    assert gone.status_code == 200, gone.text
    copy_seq = await pg_conn.fetchval(
        "SELECT seq FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 "
        "AND version > $3 ORDER BY version LIMIT 1",
        org,
        ns,
        int(first["version"]),
    )
    await pg_conn.execute(
        "INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_token, "
        "lease_until) VALUES ($1, $2, $3, $4, 2, now() + interval '30 seconds')",
        org,
        ns,
        copy_seq,
        pg_memory.MEMORY_ROW,
    )

    async def _redact() -> list[float]:
        leases, _ = await pg_memory.redact(
            (org, ns, "a", "-", "-", "k"),
            first["memory_id"],
            int(first["version"]),
            when=datetime.now(UTC),
            reason_blob="leak",
            head=None,
        )
        return leases

    try:
        first_wait = max(await _redact())
        retried_wait = max(await _redact())
    finally:
        await pg.close_pool()
    assert 25 < retried_wait <= first_wait <= 30


async def test_a_claim_on_a_just_replaced_lease_moves_its_audit_row_to_the_outbox(
    call: Any, pg_conn: Any, org: str
) -> None:
    """The audit row goes to the outbox, due once the replaced holder's insert of it can
    no longer land, and the delivery owes it no more."""
    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "a fact"))).status_code == 200
    seq = await pg_conn.fetchval(
        "SELECT seq FROM loom.memory_revisions WHERE org = $1 AND namespace = $2", org, ns
    )
    await pg_conn.execute(
        "INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_token, "
        "lease_until) VALUES ($1, $2, $3, $4, 2, now() + interval '30 seconds')",
        org,
        ns,
        seq,
        pg_memory.MEMORY_ROW | pg_memory.AUDIT_ROW,
    )
    claim = pg_memory.Claimed(
        org=org,
        namespace=ns,
        seq=int(seq),
        targets=pg_memory.MEMORY_ROW | pg_memory.AUDIT_ROW,
        lease_token=2,
        attempts=0,
        row={},
        audit={},
        deadline=0.0,
        replaced_lease_s=-2.0,
    )
    try:
        await pg_memory.defer_audits([claim], 10.0)
    finally:
        await pg.close_pool()
    due_in = await pg_conn.fetchval(
        "SELECT extract(epoch FROM due_at - now()) FROM loom.audit_outbox "
        "WHERE org = $1 AND namespace = $2",
        org,
        ns,
    )
    assert 6 < float(due_in) <= 8  # 10 s past a lease that ended 2 s ago
    targets = await pg_conn.fetchval(
        "SELECT targets FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2 AND seq = $3",
        org,
        ns,
        seq,
    )
    assert targets == pg_memory.MEMORY_ROW


async def test_a_claim_measures_the_lease_it_replaced_by_its_fence(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A redaction fenced the delivery: its lease ended, but the holder may send until its
    fence, and the claim measures what it replaced by that."""
    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "a fact"))).status_code == 200
    seq = await pg_conn.fetchval(
        "SELECT seq FROM loom.memory_revisions WHERE org = $1 AND namespace = $2", org, ns
    )
    await pg_conn.execute(
        "INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_token, "
        "lease_until, fenced_until) VALUES ($1, $2, $3, $4, 2, now() - interval '1 second', "
        "now() + interval '20 seconds')",
        org,
        ns,
        seq,
        pg_memory.AUDIT_ROW,
    )
    try:
        (claim,) = await pg_memory.claim_due(10, scope=(org, ns))
    finally:
        await pg.close_pool()
    assert 15 < claim.replaced_lease_s <= 20


async def test_a_user_erasure_run_again_waits_out_what_an_earlier_run_removed(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A user's scope reads the org's evidence: a run that finds the user's rows gone still
    waits out the publishers the earlier run removed."""
    from server.core import pg
    from server.erasure.store import steps_pg
    from server.memory.store import pg_memory

    ns, user = f"pg-{uuid.uuid4().hex[:8]}", f"u-{uuid.uuid4().hex[:6]}"
    assert (await call("memory.set", _set(ns, "k", "a fact", user_id=user))).status_code == 200
    seq = await pg_conn.fetchval(
        "SELECT seq FROM loom.memory_revisions WHERE org = $1 AND namespace = $2", org, ns
    )
    await pg_conn.execute(
        "INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_token, "
        "lease_until) VALUES ($1, $2, $3, $4, 2, now() + interval '20 seconds')",
        org,
        ns,
        seq,
        pg_memory.MEMORY_ROW,
    )
    try:
        removed, leases = await steps_pg._erase_once(org, None, user)
        assert removed == 2 and 15 < max(leases) <= 20
        removed, leases = await steps_pg._erase_once(org, None, user)
        assert removed == 0 and 15 < max(leases) <= 20
    finally:
        await pg.close_pool()


async def _owing_old_vectors(pg_conn: Any, org: str, ns: str) -> int:
    """A later revision of `k`, made 'high' and now its head, committed by a writer that
    stopped before retiring the old vectors: its delivery owes only that, its lease run
    out. Its seq."""
    from server.memory.store import pg_memory

    return await _unpublished(pg_conn, org, ns, "k", {"sensitivity": "high"}, pg_memory.OLD_VECTORS)


async def _unpublished(
    pg_conn: Any,
    org: str,
    ns: str,
    key: str,
    change: dict[str, Any],
    targets: int,
    lease: timedelta = timedelta(seconds=-1),
) -> int:
    """A later revision of `key` with `change`, now its head, committed by a writer that
    stopped before publishing it: its delivery owes `targets`, its lease ending `lease`
    from now (run out by default). Its seq."""
    async with pg_conn.transaction():
        seq = await pg_conn.fetchval(
            "UPDATE loom.scope_generations SET generation = generation + 1 "
            "WHERE org = $1 AND scope = $2 RETURNING generation",
            org,
            ns,
        )
        await pg_conn.execute(
            "INSERT INTO loom.memory_revisions (org, namespace, seq, user_id, memory_id, "
            "version, content_sha256, actor, source_class, row, audit) "
            "SELECT org, namespace, $3, user_id, memory_id, version + 1, content_sha256, actor, "
            "source_class, row || $5::jsonb || jsonb_build_object('version', version + 1), "
            "audit FROM loom.memory_revisions "
            "WHERE org = $1 AND namespace = $2 AND row->>'memory_key' = $4 "
            "ORDER BY seq DESC LIMIT 1",
            org,
            ns,
            seq,
            key,
            change,
        )
        await pg_conn.execute(
            "UPDATE loom.memory_current c SET version = r.version, seq = r.seq, row = r.row "
            "FROM loom.memory_revisions r WHERE (r.org, r.namespace, r.seq) = ($1, $2, $3) "
            "AND (c.org, c.namespace) = ($1, $2) AND c.memory_key = r.row->>'memory_key'",
            org,
            ns,
            seq,
        )
        await pg_conn.execute(
            "INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_until) "
            "VALUES ($1, $2, $3, $4, now() + $5::interval)",
            org,
            ns,
            seq,
            targets,
            lease,
        )
    return int(seq)


@pytest.mark.usefixtures("pg_authority")
async def test_heads_by_keys_read_a_deleted_head_from_postgres_only_when_asked(
    call: Any, org: str
) -> None:
    """`heads_by_keys` leaves a deleted head out unless asked, and `head_versions` answers
    its version all the same."""
    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    for k in ("live", "deleted"):
        assert (await call("memory.set", _set(ns, k, f"a fact about {k}"))).status_code == 200
    deleted = {"namespace": ns, "agent": "a", "memory_key": "deleted"}
    assert (await call("memory.delete", deleted)).status_code == 200
    keys = [(org, ns, "a", "-", "-", k) for k in ("live", "deleted")]
    try:
        assert sorted(k[5] for k in await pg_memory.heads_by_keys(keys)) == ["live"]
        both = await pg_memory.heads_by_keys(keys, include_tombstoned=True)
        assert sorted(k[5] for k in both) == ["deleted", "live"]
        assert both[keys[1]]["tombstoned_at"] is not None
        versions = await pg_memory.head_versions(keys)
        assert versions[keys[1]] == int(both[keys[1]]["version"])
    finally:
        await pg.close_pool()


async def test_reconcile_status_holds_back_what_the_authority_has_not_published(
    call: Any, pg_conn: Any, org: str, ch_client: Any
) -> None:
    """A reject committed with its vectors copied but its memory row not published, a
    delete not published and an erased key are held back; a published divergence is
    mirrored. Once the reject publishes, its key no longer diverges."""
    import server.memory.store as memory_repo
    from server.core import pg
    from server.core.ch import get_client, sequential_reads
    from server.memory.app import pg_delivery
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    keys = ("rejected", "deleted", "erased", "published")
    for k in keys:
        assert (
            await call("memory.set", _set(ns, k, f"an embedded fact about {k}"))
        ).status_code == 200
    ident = {k: (org, ns, "a", "-", "-", k) for k in keys}

    def statuses() -> dict[str, str]:
        rows = ch_client.query(
            "SELECT memory_key, status FROM loom.memory_embeddings FINAL WHERE org = {o:String} "
            "AND namespace = {n:String} AND tombstoned_at IS NULL",
            settings=_SEQUENTIAL,
            parameters={"o": org, "n": ns},
        ).result_rows
        return dict(rows)

    def memory_status(key: str) -> str:
        rows = ch_client.query(
            "SELECT status FROM loom.memory FINAL WHERE org = {o:String} AND namespace = {n:String} "
            "AND memory_key = {k:String}",
            settings=_SEQUENTIAL,
            parameters={"o": org, "n": ns, "k": key},
        ).result_rows
        return str(rows[0][0]) if rows else ""

    ch = get_client()
    async with contextlib.AsyncExitStack() as cleanup:
        cleanup.push_async_callback(pg.close_pool)
        await ch.connect()
        cleanup.push_async_callback(ch.disconnect)
        # The hour-leased deliveries must not outlive the test.
        cleanup.push_async_callback(
            pg_conn.execute,
            "DELETE FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2",
            org,
            ns,
        )
        # Leased for an hour, so the delivery worker leaves them; the test releases the
        # reject's alone.
        owed = pg_memory.MEMORY_ROW | pg_memory.AUDIT_ROW
        an_hour = timedelta(hours=1)
        rejected = {"status": "rejected"}
        seq = await _unpublished(pg_conn, org, ns, "rejected", rejected, owed, an_hour)
        version, sha = await pg_conn.fetchrow(
            "SELECT version, content_sha256 FROM loom.memory_current WHERE org = $1 "
            "AND namespace = $2 AND memory_key = 'rejected'",
            org,
            ns,
        )
        with sequential_reads():
            await memory_repo.reject_embedding_rows([(ident["rejected"], int(version), sha)], ch=ch)
        tombstoned = {"tombstoned_at": "2026-01-01T00:00:00"}
        await _unpublished(pg_conn, org, ns, "deleted", tombstoned, owed, an_hour)
        for k in ("deleted", "erased", "published"):
            await memory_repo.reemit_embedding_rows_for_keys(
                [(ident[k], {"status": "superseded"})], ch=ch
            )
        await pg_conn.execute(
            "DELETE FROM loom.memory_current WHERE org = $1 AND namespace = $2 "
            "AND memory_key = 'erased'",
            org,
            ns,
        )
        out = await memory_repo.reconcile_embedding_status(org, ns, dry_run=False, ch=ch)
        assert (out["held_back"], out["divergent_rows"], out["mirrored"]) == (3, 1, 1)
        assert statuses() == {
            "rejected": "rejected",
            "deleted": "superseded",
            "erased": "superseded",
            "published": "active",
        }
        await pg_conn.execute(
            "UPDATE loom.memory_deliveries SET lease_until = now() - interval '1 second' "
            "WHERE org = $1 AND namespace = $2 AND seq = $3",
            org,
            ns,
            seq,
        )
        # Whoever claims it, this test or the server's delivery worker, publishes it.
        published_here = await pg_delivery.deliver_namespace(org, ns)

        async def published() -> bool:
            return memory_status("rejected") == "rejected"

        await eventually(
            published,
            description=f"the reject published ({published_here} deliveries by this test)",
        )
        out = await memory_repo.reconcile_embedding_status(org, ns, dry_run=True, ch=ch)
        assert (out["held_back"], out["divergent_rows"]) == (2, 0)
        assert statuses()["rejected"] == "rejected"


async def test_the_worker_retires_the_old_vectors_a_revision_owes_in_clickhouse(
    call: Any, pg_conn: Any, org: str, ch_client: Any
) -> None:
    """The writer stopped before retiring: the delivery worker tombstones every live vector
    below the revision's version with a row holding no text or vector, and the delivery
    owes nothing more; a second retirement writes nothing."""
    import server.memory.store as memory_repo
    from server.core import pg
    from server.core.ch import get_client
    from server.memory.app import pg_delivery
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "an embedded fact"))).status_code == 200
    seq = await _owing_old_vectors(pg_conn, org, ns)

    def live_vectors() -> int:
        return int(
            ch_client.query(
                "SELECT count() FROM loom.memory_embeddings FINAL WHERE org = {o:String} "
                "AND namespace = {n:String} AND memory_key = 'k' AND tombstoned_at IS NULL",
                settings=_SEQUENTIAL,
                parameters={"o": org, "n": ns},
            ).result_rows[0][0]
        )

    assert live_vectors() >= 1
    ch = get_client()
    await ch.connect()
    try:
        claims = await pg_memory.claim_due(10, scope=(org, ns))
        await pg_delivery._publish(claims)
        assert live_vectors() == 0
        retired = ch_client.query(
            "SELECT content_excerpt, embedding_dim FROM loom.memory_embeddings FINAL "
            "WHERE org = {o:String} AND namespace = {n:String} AND memory_key = 'k'",
            settings=_SEQUENTIAL,
            parameters={"o": org, "n": ns},
        ).result_rows
        assert retired and all(r == ("", 0) for r in retired)
        head = await pg_conn.fetchval(
            "SELECT version FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 "
            "AND seq = $3",
            org,
            ns,
            seq,
        )
        assert (
            await memory_repo.retire_vectors_for_keys([((org, ns, "a", "-", "-", "k"), head)]) == 0
        )
        targets = await pg_conn.fetchval(
            "SELECT targets FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2 "
            "AND seq = $3",
            org,
            ns,
            seq,
        )
        assert targets == 0  # taken over: settled for the insert bound, owing nothing
    finally:
        await ch.disconnect()
        await pg.close_pool()


async def test_a_restores_failed_search_document_is_left_to_the_worker(
    call: Any, pg_conn: Any, org: str, ch_client: Any
) -> None:
    """The restore commits; its mirror fails, so the delivery still owes the search
    document, and one worker pass writes it and owes nothing more."""
    from server.core import pg
    from server.core.ch import get_client
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import pg_delivery, restore
    from server.memory.store import pg_memory, rows

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "a fact to bring back"))).status_code == 200
    gone = await call("memory.delete", {"namespace": ns, "agent": "a", "memory_key": "k"})
    assert gone.status_code == 200, gone.text
    head = rows._hydrate(await _head_row(pg_conn, org, ns))
    row = rows.build_memory_version_row(
        head, version=rows.version_over(head), tombstoned_at=None, expires_at=None
    )

    async def _mirror() -> None:
        raise RuntimeError("insert into loom.memory_embeddings unavailable")

    ch = get_client()
    await ch.connect()
    try:
        await restore._insert_restore(
            MemoryRestoreInput(org=org, namespace=ns, agent="a", memory_key="k"),
            row,
            head,
            ch=ch,
            what="the restore",
            mirror=_mirror,
        )
        seq = await pg_conn.fetchval(
            "SELECT seq FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 "
            "AND version = $3",
            org,
            ns,
            int(row["version"]),
        )
        owed = await pg_conn.fetchval(
            "UPDATE loom.memory_deliveries SET lease_until = now() - interval '1 second' "
            "WHERE org = $1 AND namespace = $2 AND seq = $3 RETURNING targets",
            org,
            ns,
            seq,
        )
        assert owed & pg_memory.SEARCH_DOC
        await pg_delivery._publish(await pg_memory.claim_due(10, scope=(org, ns)))
        served = ch_client.query(
            "SELECT version FROM loom.memory_embeddings FINAL WHERE org = {o:String} "
            "AND namespace = {n:String} AND memory_key = 'k' AND tombstoned_at IS NULL",
            settings=_SEQUENTIAL,
            parameters={"o": org, "n": ns},
        ).result_rows
        assert [int(v) for (v,) in served] == [int(row["version"])]
        targets = await pg_conn.fetchval(
            "SELECT targets FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2 "
            "AND seq = $3",
            org,
            ns,
            seq,
        )
        assert targets == 0
    finally:
        await ch.disconnect()
        await pg.close_pool()


async def test_a_summary_over_a_merged_key_commits_over_its_own_source(
    call: Any, pg_conn: Any, org: str
) -> None:
    """The merged row at the summary's key is both a source the commit holds live and the
    head it writes over: checked before the head moves, the summary commits."""
    from server.core import pg
    from server.memory.store import lifecycle, rows

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    written = [(await call("memory.set", _set(ns, k, f"a fact {k}"))).json() for k in ("k1", "k2")]
    head = rows._hydrate(await _head_row(pg_conn, org, ns, "k1"))
    summary = rows.build_memory_version_row(
        head, value="a fact both keys hold", version=rows.version_over(head)
    )
    try:
        await lifecycle.insert_versions_over_heads(
            [summary],
            [head],
            sources=[(w["memory_id"], int(w["version"])) for w in written],
            live_sources=True,
        )
    finally:
        await pg.close_pool()
    after = await _head_row(pg_conn, org, ns, "k1")
    assert (after["value"], int(after["version"])) == ("a fact both keys hold", summary["version"])


async def test_a_summarys_failed_vector_is_left_to_the_worker(
    call: Any, pg_conn: Any, org: str, ch_client: Any
) -> None:
    """A merge commits its summary; the vector insert fails, so the delivery still owes
    the search document, and one worker pass writes it at the summary's version."""
    from server.core import pg
    from server.core.ch import get_client
    from server.embedding import get_embedder
    from server.memory.api.models_admin import MemoryConsolidateInput
    from server.memory.app import consolidate, pg_delivery
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    for key in ("k1", "k2"):
        assert (await call("memory.set", _set(ns, key, f"a fact {key}"))).status_code == 200
    cluster = [await _head_row(pg_conn, org, ns, key) for key in ("k1", "k2")]
    ch = get_client()

    class _NoVectors:
        async def insert(self, table: str, rows: Any, **kw: Any) -> None:
            if table == "loom.memory_embeddings":
                raise RuntimeError("insert into loom.memory_embeddings unavailable")
            await ch.insert(table, rows, **kw)

        def __getattr__(self, name: str) -> Any:
            return getattr(ch, name)

    await ch.connect()
    try:
        row = await consolidate._write_canonical(
            _NoVectors(),
            MemoryConsolidateInput(org=org, namespace=ns, agent="a"),
            get_embedder(),
            cluster,
            canonical_key="summary",
            canonical_value="a fact both keys hold",
            now_naive=datetime.now(UTC).replace(tzinfo=None),
            claimed=set(),
        )
        seq = await pg_conn.fetchval(
            "SELECT seq FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 "
            "AND memory_id = $3::uuid AND version = $4",
            org,
            ns,
            str(row["memory_id"]),
            int(row["version"]),
        )
        owed = await pg_conn.fetchval(
            "UPDATE loom.memory_deliveries SET lease_until = now() - interval '1 second' "
            "WHERE org = $1 AND namespace = $2 AND seq = $3 RETURNING targets",
            org,
            ns,
            seq,
        )
        assert owed & pg_memory.SEARCH_DOC
        await pg_delivery._publish(await pg_memory.claim_due(10, scope=(org, ns)))
        served = ch_client.query(
            "SELECT version, session_id FROM loom.memory_embeddings FINAL "
            "WHERE org = {o:String} AND namespace = {n:String} AND memory_key = 'summary' "
            "AND tombstoned_at IS NULL",
            settings=_SEQUENTIAL,
            parameters={"o": org, "n": ns},
        ).result_rows
        assert [(int(v), s) for v, s in served] == [(int(row["version"]), "-")]
    finally:
        await ch.disconnect()
        await pg.close_pool()


async def test_an_undos_failed_search_rows_are_left_to_the_worker(
    call: Any, pg_conn: Any, org: str, ch_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Undo commits; re-serving its search rows fails, so the delivery still owes the
    document, and one worker pass writes it at the Undo's version with the restored
    status, owing nothing more."""
    import server.memory.store as memory_repo
    from server.core import pg
    from server.core.ch import get_client
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import pg_delivery, restore
    from server.memory.store import lifecycle, pg_memory, rows

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "a fact to retire"))).status_code == 200
    head = rows._hydrate(await _head_row(pg_conn, org, ns))
    retired = rows.build_memory_version_row(
        head, version=rows.version_over(head), status="superseded", superseded_by="other"
    )

    async def _fails(*_: Any, **__: Any) -> int:
        raise RuntimeError("insert into loom.memory_embeddings unavailable")

    monkeypatch.setattr(memory_repo, "unsupersede_embeddings", _fails)
    ch = get_client()
    await ch.connect()
    try:
        await lifecycle.insert_versions_over_heads([retired], [head])
        superseded = rows._hydrate(await _head_row(pg_conn, org, ns))
        version, status = await restore._write_restore(
            MemoryRestoreInput(org=org, namespace=ns, agent="a", memory_key="k"),
            superseded,
            ch=ch,
        )
        seq = await pg_conn.fetchval(
            "SELECT seq FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 "
            "AND version = $3",
            org,
            ns,
            version,
        )
        owed = await pg_conn.fetchval(
            "UPDATE loom.memory_deliveries SET lease_until = now() - interval '1 second' "
            "WHERE org = $1 AND namespace = $2 AND seq = $3 RETURNING targets",
            org,
            ns,
            seq,
        )
        assert owed & pg_memory.SEARCH_DOC
        await pg_delivery._publish(await pg_memory.claim_due(10, scope=(org, ns)))
        served = ch_client.query(
            "SELECT version, status FROM loom.memory_embeddings FINAL WHERE org = {o:String} "
            "AND namespace = {n:String} AND memory_key = 'k' AND tombstoned_at IS NULL "
            "ORDER BY version DESC LIMIT 1",
            settings=_SEQUENTIAL,
            parameters={"o": org, "n": ns},
        ).result_rows
        assert [(int(v), str(st)) for v, st in served] == [(version, status)]

        # A retry finding the Undo landed copies no search rows outside a lease.
        copied: list[Any] = []

        async def _copy(*a: Any, **kw: Any) -> int:
            copied.append(a)
            return 0

        monkeypatch.setattr(memory_repo, "unsupersede_embeddings", _copy)
        retried = await restore._restore_superseded(
            MemoryRestoreInput(org=org, namespace=ns, agent="a", memory_key="k"), ch
        )
        assert retried.restored is True and retried.version == version and copied == []
        targets = await pg_conn.fetchval(
            "SELECT targets FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2 "
            "AND seq = $3",
            org,
            ns,
            seq,
        )
        assert targets == 0
    finally:
        await ch.disconnect()
        await pg.close_pool()


@pytest.mark.parametrize(
    "op, change",
    [
        ("memory.set", {"sensitivity": "high"}),
        ("memory.set", {"memory_type": "episodic"}),
        ("memory.bulk_set", {"sensitivity": "high"}),
        ("memory.update", {}),
    ],
)
async def test_a_write_made_unembedded_over_an_embedded_key_retires_its_vectors(
    call: Any, pg_conn: Any, org: str, ch_client: Any, op: str, change: dict[str, Any]
) -> None:
    """A write made 'high' or retyped to a type that is not searchable owes the retirement
    of the old vectors with its commit, retires them with no text, and owes nothing
    after."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "an embedded fact"))).status_code == 200
    # memory.update takes no level: the detector makes its new value 'high'.
    value = "the ssn is 123-45-6789" if op == "memory.update" else "the same fact, changed"
    body: dict[str, Any] = {**_set(ns, "k", value), **change}
    if op == "memory.bulk_set":
        body = {"namespace": ns, "agent": "a", "entries": [body]}
    elif op == "memory.update":
        body = {"namespace": ns, "memory_key": "k", "value": value}
    assert (await call(op, body)).status_code == 200
    live = ch_client.query(
        "SELECT count() FROM loom.memory_embeddings FINAL WHERE org = {o:String} "
        "AND namespace = {n:String} AND memory_key = 'k' AND tombstoned_at IS NULL",
        settings=_SEQUENTIAL,
        parameters={"o": org, "n": ns},
    ).result_rows[0][0]
    assert live == 0
    # No retirement holds text a redaction would have to purge: every tombstone, read
    # unmerged, since a 'high' write's fence at the same version wins a FINAL read.
    tombstones = ch_client.query(
        "SELECT countIf(content_excerpt != '' OR embedding_dim > 0), count() "
        "FROM loom.memory_embeddings WHERE org = {o:String} AND namespace = {n:String} "
        "AND memory_key = 'k' AND tombstoned_at IS NOT NULL",
        settings=_SEQUENTIAL,
        parameters={"o": org, "n": ns},
    ).result_rows[0]
    assert tombstones[0] == 0 and tombstones[1] > 0
    owed = await pg_conn.fetchval(
        "SELECT count(*) FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2 "
        "AND targets <> 0",
        org,
        ns,
    )
    assert owed == 0


async def test_a_claim_that_lost_its_delivery_moves_no_audit_row(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A fence moved the delivery's token past the claim's: the claim moves nothing and
    leaves the targets to the delivery's holder."""
    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "a fact"))).status_code == 200
    seq = await pg_conn.fetchval(
        "SELECT seq FROM loom.memory_revisions WHERE org = $1 AND namespace = $2", org, ns
    )
    owed = pg_memory.MEMORY_ROW | pg_memory.AUDIT_ROW
    await pg_conn.execute(
        "INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_token, "
        "lease_until) VALUES ($1, $2, $3, $4, 3, now() + interval '30 seconds')",
        org,
        ns,
        seq,
        owed,
    )
    claim = pg_memory.Claimed(
        org=org,
        namespace=ns,
        seq=int(seq),
        targets=owed,
        lease_token=2,
        attempts=0,
        row={},
        audit={},
        deadline=0.0,
        replaced_lease_s=-2.0,
    )
    try:
        await pg_memory.defer_audits([claim], 10.0)
    finally:
        await pg.close_pool()
    assert not await pg_conn.fetchval(
        "SELECT count(*) FROM loom.audit_outbox WHERE org = $1 AND namespace = $2", org, ns
    )
    targets = await pg_conn.fetchval(
        "SELECT targets FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2 AND seq = $3",
        org,
        ns,
        seq,
    )
    assert targets == owed


async def test_an_erasure_run_again_waits_out_what_an_earlier_run_removed(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A run that committed its deletes and stopped before its wait leaves only evidence:
    the next run finds nothing to delete and still waits out the publishers it removed. A
    run that removed no held delivery leaves nothing to wait for."""
    from server.core import pg
    from server.erasure.store import steps_pg
    from server.memory.store import pg_memory

    held, idle = (f"pg-{uuid.uuid4().hex[:8]}" for _ in range(2))
    for ns in (held, idle):
        assert (await call("memory.set", _set(ns, "k", "a fact"))).status_code == 200
    seq = await pg_conn.fetchval(
        "SELECT seq FROM loom.memory_revisions WHERE org = $1 AND namespace = $2", org, held
    )
    await pg_conn.execute(
        "INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_token, "
        "lease_until) VALUES ($1, $2, $3, $4, 2, now() + interval '20 seconds')",
        org,
        held,
        seq,
        pg_memory.MEMORY_ROW,
    )
    try:
        removed, (first,) = await steps_pg._erase_once(org, [held], None)
        assert removed == 2 and 15 < first <= 20
        removed, (again,) = await steps_pg._erase_once(org, [held], None)
        assert removed == 0 and 15 < again <= first
        assert await steps_pg._erase_once(org, [idle], None) == (2, [])
        assert await steps_pg._erase_once(org, [idle], None) == (0, [])
    finally:
        await pg.close_pool()


@pytest.mark.parametrize("then", ["parked", "settled"])
async def test_a_fenced_holder_is_waited_out_after_its_delivery_moves_on(
    call: Any, pg_conn: Any, org: str, then: str
) -> None:
    """The claim after a fence parks the delivery, or publishes and settles it. A park
    keeps the failed claim's lease, which a retried redaction waits out; a settled
    delivery is kept by the sweep until its fence and the insert bound have passed,
    however often it is fenced again."""
    from datetime import UTC, datetime

    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    first = (await call("memory.set", _set(ns, "k", "the token is hunter2"))).json()
    assert (await call("memory.set", _set(ns, "k", "the token was rotated"))).status_code == 200
    seq = await pg_conn.fetchval(
        "SELECT seq FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 AND version = $3",
        org,
        ns,
        first["version"],
    )
    await pg_conn.execute(
        "INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_token, "
        "lease_until) VALUES ($1, $2, $3, $4, 2, now() + interval '30 seconds')",
        org,
        ns,
        seq,
        pg_memory.MEMORY_ROW,
    )

    async def _redact() -> float:
        (left,), _ = await pg_memory.redact(
            (org, ns, "a", "-", "-", "k"),
            first["memory_id"],
            first["version"],
            when=datetime.now(UTC),
            reason_blob="leak",
            head=await _live_head(pg_conn, org, ns),
        )
        return float(left)

    async def _settled_since(*, lease: float, fence: float) -> None:
        """Move the settled delivery's lease end and fence to these seconds from now."""
        await pg_conn.execute(
            "UPDATE loom.memory_deliveries SET lease_until = now() + make_interval(secs => $4), "
            "fenced_until = now() + make_interval(secs => $5) "
            "WHERE org = $1 AND namespace = $2 AND seq = $3",
            org,
            ns,
            seq,
            lease,
            fence,
        )

    async def _kept() -> bool:
        await pg_memory.sweep_settled(1000)
        return bool(
            await pg_conn.fetchval(
                "SELECT count(*) FROM loom.memory_deliveries "
                "WHERE org = $1 AND namespace = $2 AND seq = $3",
                org,
                ns,
                seq,
            )
        )

    try:
        assert 25 < await _redact() <= 30
        # The fence ended the lease: the next claim takes the delivery at once.
        (claim,) = await pg_memory.claim_due(10, scope=(org, ns))
        if then == "parked":
            await pg_memory.record_failure([claim], RuntimeError("refused"), park=True)
            # The park keeps the failed claim's lease: its send may still land.
            assert 25 < await _redact() <= 30
            await pg_conn.execute(
                "UPDATE loom.memory_deliveries SET fenced_until = now() - interval '1 hour' "
                "WHERE org = $1 AND namespace = $2 AND seq = $3",
                org,
                ns,
                seq,
            )
            assert await _redact() < 0  # a park long past waits nothing
        else:
            await pg_memory.complete([(org, ns, seq)], claim.targets, tokens=claim.lease_token)
            assert await _kept()
            # A retry fences the delivery again: the sweep still keeps it for the first.
            assert 25 < await _redact() <= 30
            assert await _kept()
            bound = pg.insert_bound_s()
            # Its lease past the insert bound, the fence past but inside it: kept.
            await _settled_since(lease=-(bound + 5), fence=-(bound - 5))
            assert await _kept()
            await _settled_since(lease=-(bound + 5), fence=-(bound + 5))
            assert not await _kept()
    finally:
        await pg.close_pool()


@pytest.mark.parametrize("lease", ["held", "ran_out"])
async def test_a_failed_publish_parks_its_delivery_with_the_time_it_failed(
    call: Any, pg_conn: Any, org: str, lease: str
) -> None:
    """The holder parking a delivery has made its last send: what a later wait covers is
    the time of the failure, or the lease if it ran out before, never the rest of a
    lease it will not use."""
    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "a fact"))).status_code == 200
    seq = await pg_conn.fetchval(
        "SELECT seq FROM loom.memory_revisions WHERE org = $1 AND namespace = $2", org, ns
    )
    await pg_conn.execute(
        "INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_until) "
        "VALUES ($1, $2, $3, $4, now() - interval '1 second') "
        "ON CONFLICT (org, namespace, seq) DO UPDATE SET targets = EXCLUDED.targets, "
        "lease_until = EXCLUDED.lease_until, lease_token = 1",
        org,
        ns,
        seq,
        pg_memory.MEMORY_ROW,
    )
    try:
        (claim,) = await pg_memory.claim_due(10, scope=(org, ns))  # leased for 30 s
        if lease == "ran_out":
            await pg_conn.execute(
                "UPDATE loom.memory_deliveries SET lease_until = now() - interval '10 seconds' "
                "WHERE org = $1 AND namespace = $2 AND seq = $3",
                org,
                ns,
                seq,
            )
        assert await pg_memory.record_failure([claim], RuntimeError("refused"), park=True) == 1
    finally:
        await pg.close_pool()
    kept = await pg_conn.fetchval(
        "SELECT extract(epoch FROM fenced_until - now()) FROM loom.memory_deliveries "
        "WHERE org = $1 AND namespace = $2 AND seq = $3 AND lease_until = 'infinity'",
        org,
        ns,
        seq,
    )
    if lease == "held":
        assert -2 < kept <= 0
    else:
        assert -12 < kept <= -9


async def test_a_parked_delivery_always_keeps_what_a_wait_covers(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A wait reads a parked delivery's `fenced_until`: a park with none is refused."""
    import asyncpg

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "a fact"))).status_code == 200
    seq = await pg_conn.fetchval(
        "SELECT seq FROM loom.memory_revisions WHERE org = $1 AND namespace = $2", org, ns
    )
    await pg_conn.execute(
        "DELETE FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2 AND seq = $3",
        org,
        ns,
        seq,
    )
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pg_conn.execute(
            "INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_until) "
            "VALUES ($1, $2, $3, 1, 'infinity')",
            org,
            ns,
            seq,
        )


async def test_a_settled_delivery_is_neither_renewed_nor_parked_nor_released(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A delivery its holder settled is kept for the insert bound and the sweep owns it:
    that holder, under its own token, neither renews, parks nor releases it."""
    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "a fact"))).status_code == 200
    seq = await pg_conn.fetchval(
        "SELECT seq FROM loom.memory_revisions WHERE org = $1 AND namespace = $2", org, ns
    )
    # Settled by the claim that took it over, its publish still marked started, kept for
    # the insert bound.
    await pg_conn.execute(
        "INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_token, "
        "lease_until, held) VALUES ($1, $2, $3, 0, 2, now() + interval '30 seconds', true) "
        "ON CONFLICT (org, namespace, seq) DO UPDATE SET targets = 0, lease_token = 2, "
        "lease_until = EXCLUDED.lease_until, held = true",
        org,
        ns,
        seq,
    )
    claim = pg_memory.Claimed(
        org=org,
        namespace=ns,
        seq=seq,
        targets=pg_memory.MEMORY_ROW,
        lease_token=2,
        attempts=9,
        row={},
        audit={},
        deadline=0.0,
    )
    try:
        assert await pg_memory.renew([claim]) == []
        assert await pg_memory.record_failure([claim], RuntimeError("x"), park=True) == 0
        await pg_memory.release([claim])
    finally:
        await pg.close_pool()
    row = await pg_conn.fetchrow(
        "SELECT lease_until = 'infinity' AS parked, held FROM loom.memory_deliveries "
        "WHERE org = $1 AND namespace = $2 AND seq = $3",
        org,
        ns,
        seq,
    )
    assert (row["parked"], row["held"]) == (False, True)


async def test_a_retry_that_took_over_a_fenced_claim_stores_no_residue(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A request in flight across an erasure is fenced; its client's retry takes the
    claim over and stores its answer. That answer is the retry's, claimed after the
    erasure, so the census does not count it."""
    from server.core import pg
    from server.erasure.store.steps_pg import pg_residue

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    body, key = _set(ns, "k", "a fact in flight"), uuid.uuid4().hex
    assert (await call("memory.set", body, **{"Idempotency-Key": key})).status_code == 200
    # The request as it was before the erasure: claimed long ago, still running.
    await pg_conn.execute(
        "UPDATE loom.idempotency SET state = 'running', response_body = NULL, "
        "lease_until = now() + interval '1 minute', created_at = now() - interval '1 hour' "
        "WHERE org = $1",
        org,
    )
    r = await call(
        "erasure.request", {"scope_type": "namespace", "scope_value": ns, "requester": "t"}
    )
    assert r.json()["status"] == "completed", r.text
    retry = await call("memory.set", body, **{"Idempotency-Key": key})
    assert retry.status_code == 200, retry.text
    assert (
        await pg_conn.fetchval("SELECT state FROM loom.idempotency WHERE org = $1", org) == "done"
    )
    try:
        residue = await pg_residue(org, "namespace", ns)
    finally:
        await pg.close_pool()
    assert residue["pg:loom.idempotency"] == 0, residue


@pytest.mark.parametrize("fenced", [True, False])
async def test_an_answer_claimed_before_the_step_is_residue(
    call: Any, pg_conn: Any, org: str, fenced: bool
) -> None:
    """The other direction of the census bound: an answer claimed before the step last ran
    (one it missed) counts, and with no fence row, as when the step never ran, every
    answer does."""
    from server.core import pg
    from server.erasure.store.steps_pg import pg_residue

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    key = uuid.uuid4().hex
    assert (
        await call("memory.set", _set(ns, "k", "a fact"), **{"Idempotency-Key": key})
    ).status_code == 200
    await pg_conn.execute(
        "UPDATE loom.idempotency SET created_at = now() - interval '1 hour' WHERE org = $1", org
    )
    if fenced:
        await pg_conn.execute(
            "INSERT INTO loom.idempotency_fences (org, fenced_at) VALUES ($1, now())", org
        )
    try:
        residue = await pg_residue(org, "namespace", ns)
    finally:
        await pg.close_pool()
    assert residue["pg:loom.idempotency"] == 1, residue


async def test_a_claim_met_by_the_fence_is_retried_and_stamped_after_it(
    call: Any, pg_conn: Any, org: str
) -> None:
    """While an erasure's step holds the org's claim lock, a keyed request is refused at
    once (`IDEMPOTENT_IN_PROGRESS`); once the fence is in, its retry claims above it and
    is no residue."""
    from server.core import pg
    from server.erasure.store.steps_pg import pg_residue

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    body, key = _set(ns, "k", "a fact"), uuid.uuid4().hex
    async with pg_conn.transaction():
        await pg.fence_lock(pg_conn, org)  # the step's fence
        refused = await call("memory.set", body, **{"Idempotency-Key": key})
        await pg_conn.execute(
            "INSERT INTO loom.idempotency_fences (org, fenced_at) VALUES ($1, clock_timestamp())",
            org,
        )
    assert refused.status_code == 503, refused.text
    assert refused.json()["detail"]["code"] == "IDEMPOTENT_IN_PROGRESS", refused.text
    assert (await call("memory.set", body, **{"Idempotency-Key": key})).status_code == 200
    after = await pg_conn.fetchval(
        "SELECT i.created_at > f.fenced_at FROM loom.idempotency i "
        "JOIN loom.idempotency_fences f USING (org) WHERE i.org = $1",
        org,
    )
    assert after is True
    try:
        residue = await pg_residue(org, "namespace", ns)
    finally:
        await pg.close_pool()
    assert residue["pg:loom.idempotency"] == 0, residue


async def test_a_trace_leaves_out_a_candidate_whose_head_moved_on(
    call: Any, pg_conn: Any, org: str
) -> None:
    """Against the real heads: a candidate whose head shows other content than its
    document (a revision not yet published) is left out of memory.search_trace and
    counted; a current one is listed."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    for key, value in (
        ("moved", "postgres keeps the heads"),
        ("kept", "postgres keeps the journal"),
    ):
        assert (await call("memory.set", _set(ns, key, value))).status_code == 200
    # The head of `moved` now carries content its search document was not built from.
    await pg_conn.execute(
        "UPDATE loom.memory_current SET content_sha256 = repeat('0', 64) "
        "WHERE org = $1 AND namespace = $2 AND memory_key = 'moved'",
        org,
        ns,
    )
    r = await call("memory.search_trace", {"namespace": ns, "query": "postgres keeps", "top_k": 10})
    assert r.status_code == 200, r.text
    keys = [h["memory_key"] for h in r.json()["results"]]
    assert "kept" in keys and "moved" not in keys
    assert r.json()["stale_dropped"] == 1


async def _live_head(pg_conn: Any, org: str, ns: str) -> tuple[str, int] | None:
    """The namespace's one live head, (memory_id, version), as `memory.redact` checks it."""
    row = await pg_conn.fetchrow(
        "SELECT memory_id, version FROM loom.memory_current WHERE org = $1 AND namespace = $2 "
        "AND row->>'tombstoned_at' IS NULL",
        org,
        ns,
    )
    return (str(row["memory_id"]), int(row["version"])) if row else None


async def _head_row(pg_conn: Any, org: str, ns: str, key: str | None = None) -> dict[str, Any]:
    """The namespace's one head, or the head of `key`."""
    from server.memory.store import pg_memory

    stored = await pg_conn.fetchval(
        "SELECT row FROM loom.memory_current WHERE org = $1 AND namespace = $2 "
        "AND ($3::text IS NULL OR memory_key = $3)",
        org,
        ns,
        key,
    )
    return pg_memory.decode_row(stored)


async def test_redacting_an_older_version_leaves_a_live_head_of_the_same_text(
    call: Any, pg_conn: Any, org: str
) -> None:
    """Redacting a version whose text the live head still shows changes nothing of the
    head: a later write built from the head keeps the text."""
    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    first = (await call("memory.set", _set(ns, "k", "the office is in Berlin"))).json()
    pinned = await call("memory.pin", _set(ns, "k", "the office is in Berlin"))
    assert pinned.status_code == 200, pinned.text
    r = await call(
        "memory.redact",
        {"memory_id": first["memory_id"], "version": first["version"], "reason": "old"},
    )
    assert r.json()["redacted"] is True, r.text
    head = await _head_row(pg_conn, org, ns)
    head["version"], head["pinned"] = int(head["version"]) + 10**9, False
    try:
        await pg_memory.commit_versions([head], [{}])
    finally:
        await pg.close_pool()
    assert "Berlin" in str((await _head_row(pg_conn, org, ns))["value"])


async def test_a_committed_replay_whose_memory_was_erased_is_not_found(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A key whose commit an erasure removed answers 404, never 200 with no memory."""
    from server.core import pg
    from server.erasure.store.steps_pg import delete_pg_memory_by_namespace

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    body, key = _set(ns, "k", "a fact"), uuid.uuid4().hex
    assert (await call("memory.set", body, **{"Idempotency-Key": key})).status_code == 200
    # Its answer lost after the commit: the key holds the committed revision only.
    await pg_conn.execute(
        "UPDATE loom.idempotency SET state = 'committed', response_body = NULL, "
        "lease_until = now() WHERE org = $1",
        org,
    )
    try:
        await delete_pg_memory_by_namespace(org, ns)
    finally:
        await pg.close_pool()
    retry = await call("memory.set", body, **{"Idempotency-Key": key})
    assert retry.status_code == 404, retry.text


async def test_a_redacted_head_is_not_served_until_a_new_value_is_written(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A scrubbed head keeps its hash, so its old search document would still match it:
    search leaves it out and counts it. A new value written over it is served."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    first = (await call("memory.set", _set(ns, "k", "the office is in Berlin"))).json()
    # The head as a profile redaction leaves it: scrubbed in Postgres, its ClickHouse
    # document not yet.
    await pg_conn.execute(
        "UPDATE loom.memory_current SET row = row || jsonb_build_object('redacted_at', "
        "'2026-01-01 00:00:00') WHERE org = $1 AND namespace = $2",
        org,
        ns,
    )
    found = (await call("memory.search", {"namespace": ns, "query": "office Berlin"})).json()
    assert "k" not in [h["memory_key"] for h in found["results"]]
    assert found["stale_dropped"] >= 1
    r = await call(
        "memory.update_by_id", {"memory_id": first["memory_id"], "value": "the office is in Madrid"}
    )
    assert r.status_code == 200, r.text
    found = (await call("memory.search", {"namespace": ns, "query": "office Madrid"})).json()
    assert "k" in [h["memory_key"] for h in found["results"]]


async def test_a_redaction_waits_for_a_commit_holding_its_namespace(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A redaction takes the namespace's generation first, as a commit does: while a
    commit holds it, the redaction waits, so no commit lands between its statements."""
    from datetime import UTC, datetime

    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    first = (await call("memory.set", _set(ns, "k", "the token is hunter2"))).json()
    assert (await call("memory.set", _set(ns, "k", "the token was rotated"))).status_code == 200
    live = await _live_head(pg_conn, org, ns)
    try:
        async with pg_conn.transaction():
            # A commit in the namespace, holding its generation.
            await pg_conn.execute(
                "SELECT 1 FROM loom.scope_generations WHERE org = $1 AND scope = $2 FOR UPDATE",
                org,
                ns,
            )
            redacting = asyncio.create_task(
                pg_memory.redact(
                    (org, ns, "a", "-", "-", "k"),
                    first["memory_id"],
                    first["version"],
                    when=datetime.now(UTC),
                    reason_blob="leak",
                    head=live,
                )
            )
            await asyncio.sleep(0.5)
            assert not redacting.done()
        await asyncio.wait_for(redacting, timeout=10)
    finally:
        await pg.close_pool()
    scrubbed = await pg_conn.fetchval(
        "SELECT row->>'value' FROM loom.memory_revisions WHERE org = $1 AND namespace = $2 "
        "AND version = $3",
        org,
        ns,
        first["version"],
    )
    assert "hunter2" not in scrubbed


async def test_erasing_a_namespace_leaves_its_restored_copy_writable(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A namespace restored from another's snapshot keeps its memory ids: erasing the
    source leaves the copy's memories writable and out of the source's census."""
    from server.core import pg
    from server.erasure.store.steps_pg import pg_residue

    source, copy = f"pg-{uuid.uuid4().hex[:8]}", f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(source, "k", "a fact to copy"))).status_code == 200
    snapshot = await call("namespace.snapshot", {"namespace": source})
    assert snapshot.status_code == 200, snapshot.text
    restored = await call(
        "namespace.restore", {"target_namespace": copy, "payload": snapshot.json()}
    )
    assert restored.status_code == 200, restored.text
    r = await call(
        "erasure.request", {"scope_type": "namespace", "scope_value": source, "requester": "t"}
    )
    assert r.json()["status"] == "completed", r.text
    again = await call("memory.set", _set(copy, "k", "the copy is written again"))
    assert again.status_code == 200, again.text
    try:
        residue = await pg_residue(org, "namespace", source)
    finally:
        await pg.close_pool()
    assert residue["pg:loom.memory_current"] == residue["pg:loom.memory_revisions"] == 0


async def test_a_restore_leaves_out_the_memories_an_erasure_removed_while_evidence_lasts(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A restore into the namespace writes the snapshot's memories but those an erasure
    removed from it, counts the ones left out, and is done for that snapshot. The
    evidence expires by its age, not by the sweep: a snapshot first restored after it
    writes the memory, and an erasure of it again starts its evidence again."""
    from server.core import pg
    from server.memory.store import pg_memory
    from server.settings import settings

    ns, erased_user = f"pg-{uuid.uuid4().hex[:8]}", f"u-{uuid.uuid4().hex[:6]}"
    first = await call("memory.set", _set(ns, "k1", "the erased user's fact", user_id=erased_user))
    second = await call("memory.set", _set(ns, "k2", "another user's fact", user_id="other"))
    assert first.status_code == 200 and second.status_code == 200, (first.text, second.text)
    snapshot = await call("namespace.snapshot", {"namespace": ns})
    assert snapshot.status_code == 200, snapshot.text

    async def _erase() -> None:
        r = await call(
            "erasure.request",
            {"scope_type": "user_id", "scope_value": erased_user, "requester": "t"},
        )
        assert r.json()["status"] == "completed", r.text

    async def _restore(snapshot_id: str) -> Any:
        payload = {**snapshot.json(), "snapshot_id": snapshot_id}
        r = await call("namespace.restore", {"target_namespace": ns, "payload": payload})
        assert r.status_code == 200, r.text
        return r.json()

    async def _get(key: str, user: str) -> Any:
        got = await call(
            "memory.get", {"namespace": ns, "agent": "a", "memory_key": key, "user_id": user}
        )
        assert got.status_code == 200, got.text
        return got.json()

    async def _age_evidence() -> None:
        await pg_conn.execute(
            "UPDATE loom.erased_memories "
            "SET erased_at = now() - make_interval(hours => $3 + 1) "
            "WHERE org = $1 AND namespace = $2",
            org,
            ns,
            settings.pg_erasure_evidence_hours,
        )

    await _erase()
    gone = await call(
        "memory.delete", {"namespace": ns, "agent": "a", "memory_key": "k2", "user_id": "other"}
    )
    assert gone.status_code == 200, gone.text
    sid = snapshot.json()["snapshot_id"]
    restored = await _restore(sid)
    assert restored["restored"] is True and restored["counts"]["erased_skipped"] == 1
    assert (await _restore(sid.upper()))["restored"] is False  # a replay, in any spelling
    assert (await _get("k1", erased_user)).get("found") is False
    back = await _get("k2", "other")
    assert back["memory_id"] == second.json()["memory_id"]
    assert back["value"] == "another user's fact"

    mid = first.json()["memory_id"]
    try:
        await _age_evidence()
        assert await pg_memory.erased_ids(org, ns, [mid]) == set()  # expired, not yet swept
        # A replay is done; the memory stays out of that snapshot's restore.
        replay = await _restore(sid)
        assert replay["restored"] is False and replay["counts"]["erased_skipped"] == 1
        assert (await _get("k1", erased_user)).get("found") is False
        # Another snapshot id restores it: its commit is no longer refused.
        again = await _restore(str(uuid.uuid4()))
        assert again["counts"]["erased_skipped"] == 0
        assert (await _get("k1", erased_user))["memory_id"] == mid
        # Erased again while the old evidence row is still there: the evidence restarts.
        await _erase()
        assert await pg_memory.erased_ids(org, ns, [mid]) == {mid}
        await _age_evidence()
        await pg_memory.sweep_erasure_evidence(10_000)
    finally:
        await pg.close_pool()
    left = await pg_conn.fetchval(
        "SELECT count(*) FROM loom.erased_memories WHERE org = $1 AND namespace = $2", org, ns
    )
    assert left == 0


async def test_deleting_a_decision_retires_its_record(call: Any, org: str, ch_client: Any) -> None:
    """A decision's record goes with its writer's row, judged by the heads the authority
    holds: the deleted writer's record is retired at once."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    made = await call(
        "decision.record",
        {
            "namespace": ns,
            "agent": "a",
            "category": "storage",
            "scenario": "which store holds live memory",
            "reasoning": "one authority",
            "outcome": "postgres",
        },
    )
    assert made.status_code == 200, made.text
    key = made.json()["memory_key"]
    gone = await call("memory.delete", {"namespace": ns, "agent": "a", "memory_key": key})
    assert gone.status_code == 200 and gone.json()["tombstoned"] is True, gone.text
    status = ch_client.query(
        "SELECT status FROM loom.memory_records FINAL "
        "WHERE org = {o:String} AND namespace = {n:String} AND memory_key = {k:String}",
        settings=_SEQUENTIAL,
        parameters={"o": org, "n": ns, "k": key},
    ).result_rows
    assert [r[0] for r in status] == ["tombstoned"]


async def test_a_decision_deleted_without_its_publish_loses_its_record_when_delivered(
    call: Any, pg_conn: Any, org: str, ch_client: Any
) -> None:
    """A decision's tombstone committed with nothing after it (its writer's answer lost)
    owes its record's retirement, which the delivery worker does once it publishes it
    (here through a search's barrier); a decision recorded again before an old tombstone
    is delivered keeps its new record."""
    from server.core import pg
    from server.memory.store import inserts, pg_memory, rows

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    decision = {
        "namespace": ns,
        "agent": "a",
        "category": "storage",
        "scenario": "which store holds live memory",
        "reasoning": "one authority",
    }

    async def record(outcome: str) -> tuple[str, str]:
        made = await call("decision.record", {**decision, "outcome": outcome})
        assert made.status_code == 200, made.text
        return str(made.json()["memory_key"]), str(made.json()["record_id"])

    async def delete_unpublished(key: str) -> None:
        stored = await pg_conn.fetchval(
            "SELECT row FROM loom.memory_current WHERE org = $1 AND namespace = $2 "
            "AND memory_key = $3",
            org,
            ns,
            key,
        )
        head = rows._hydrate(pg_memory.decode_row(stored))
        now = datetime.now(UTC).replace(tzinfo=None)
        tombstone = rows.build_memory_version_row(
            head, version=rows.next_version(), tombstoned_at=now
        )
        try:
            await inserts.insert_memory_versions([tombstone], publish_now=False)
        finally:
            await pg.close_pool()

    def statuses(key: str) -> dict[str, str]:
        """Each record of the key by its id, with its status."""
        return {
            str(r[0]): str(r[1])
            for r in ch_client.query(
                "SELECT toString(record_id), status FROM loom.memory_records FINAL "
                "WHERE org = {o:String} AND namespace = {n:String} AND memory_key = {k:String}",
                settings=_SEQUENTIAL,
                parameters={"o": org, "n": ns, "k": key},
            ).result_rows
        }

    async def deliver() -> None:
        found = await call("memory.search", {"namespace": ns, "query": "live memory"})
        assert found.status_code == 200, found.text

    key, first = await record("postgres")
    await delete_unpublished(key)
    assert statuses(key) == {first: "active"}  # nothing retired it before its publication
    owed = await pg_conn.fetchval(
        "SELECT targets FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2 "
        "ORDER BY seq DESC LIMIT 1",
        org,
        ns,
    )
    assert owed & pg_memory.DECISION_RECORD, owed
    await deliver()
    assert statuses(key) == {first: "tombstoned"}
    chain = await call("decision.chain", {"namespace": ns, "agent": "a", "memory_key": key})
    assert chain.status_code == 200 and chain.json().get("seed_decision") is None, chain.text

    # Deleted again unpublished, then recorded anew before the old tombstone is delivered.
    again, _ = await record("still postgres")
    await delete_unpublished(again)
    _, newest = await record("postgres, again")
    await deliver()
    # The new record survives the old tombstone, and is the key's only active one.
    assert [rid for rid, status in statuses(key).items() if status == "active"] == [newest]


async def _one_decision_record(call: Any, ns: str) -> str:
    made = await call(
        "decision.record",
        {
            "namespace": ns,
            "agent": "a",
            "category": "storage",
            "scenario": "which store holds live memory",
            "reasoning": "one authority",
            "outcome": "postgres",
        },
    )
    assert made.status_code == 200, made.text
    return str(made.json()["memory_key"])


def _record_rows(ch_client: Any, org: str, ns: str, key: str) -> list[tuple[str, int]]:
    return [
        (str(r[0]), int(r[1]))
        for r in ch_client.query(
            "SELECT status, version FROM loom.memory_records FINAL WHERE org = {o:String} "
            "AND namespace = {n:String} AND memory_key = {k:String}",
            settings=_SEQUENTIAL,
            parameters={"o": org, "n": ns, "k": key},
        ).result_rows
    ]


@pytest.mark.usefixtures("pg_authority")
async def test_a_records_retirement_copies_only_records_below_its_version(
    call: Any, org: str, ch_client: Any
) -> None:
    """Under Postgres the retirement reads its live writers apart from the copy, so it
    copies only records below the version it drew before that read: a record written since
    is kept even when its writer is not among the writers read."""
    from server.core.ch import get_client
    from server.memory.store import records

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    key = await _one_decision_record(call, ns)
    [(status, version)] = _record_rows(ch_client, org, ns, key)
    assert status == "active"
    ch = get_client()
    await ch.connect()
    try:
        # Written at the version the retirement drew: not older, so kept.
        await records.tombstone_decision_records(org, ns, [key], version=version, ch=ch, live=[])
        assert [s for s, _ in _record_rows(ch_client, org, ns, key)] == ["active"]
        await records.tombstone_decision_records(
            org, ns, [key], version=version + 1, ch=ch, live=[]
        )
        assert [s for s, _ in _record_rows(ch_client, org, ns, key)] == ["tombstoned"]
    finally:
        await ch.disconnect()


@pytest.mark.usefixtures("pg_authority")
async def test_a_records_retirement_past_its_start_deadline_copies_nothing(
    call: Any, org: str, ch_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retirement that would start after its deadline is refused by the server, so it
    never lands after an erasure waited out its publisher."""
    import time

    from server.core import rmt_carry
    from server.core.ch import get_client
    from server.core.errors import BackendUnavailableError
    from server.memory.store import records

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    key = await _one_decision_record(call, ns)
    [(_, version)] = _record_rows(ch_client, org, ns, key)
    monkeypatch.setattr(rmt_carry, "send_deadline", lambda: time.time() - 60)
    ch = get_client()
    await ch.connect()
    try:
        with pytest.raises(BackendUnavailableError, match="too late"):
            await records.tombstone_decision_records(
                org, ns, [key], version=version + 1, ch=ch, live=[]
            )
    finally:
        await ch.disconnect()
    assert [s for s, _ in _record_rows(ch_client, org, ns, key)] == ["active"]


async def test_a_restore_whose_publication_goes_pending_is_published_by_the_worker(
    call: Any, pg_conn: Any, org: str, ch_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restore of more than one batch whose first publication fails commits the rest
    for the delivery worker, at once claimable; a delivery pass then publishes every
    memory to ClickHouse and leaves nothing owed."""
    from server.core import pg
    from server.core.ch import get_client
    from server.identity.api.models import _SnapshotMemoryRow
    from server.memory.app import snapshot_rows
    from server.memory.app.pg_delivery import deliver_namespace
    from server.memory.store import pg_publish
    from server.settings import settings

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    memories = [
        _SnapshotMemoryRow(
            memory_id=str(uuid.uuid4()),
            agent="a",
            user_id="-",
            session_id="-",
            run_id="-",
            memory_key=f"k{i}",
            memory_type="semantic",
            value=f'"fact {i}"',
            confidence=1.0,
            source_trace="",
            pinned=False,
        )
        for i in range(5)
    ]
    rows = snapshot_rows.checked_rows(
        snapshot_rows.restore_rows(
            memories, org=org, target_namespace=ns, rebind_agent=None, version_base=1, actor="t"
        )
    )
    real = pg_publish.publish_rows
    failed: list[int] = []

    async def _refused_once(*args: Any, **kwargs: Any) -> None:
        if not failed:
            failed.append(1)
            raise TimeoutError("ClickHouse did not answer")
        await real(*args, **kwargs)

    monkeypatch.setattr(settings, "pg_restore_batch_rows", 2)
    monkeypatch.setattr(pg_publish, "publish_rows", _refused_once)
    ch = get_client()
    await ch.connect()
    try:
        assert await snapshot_rows.insert_version_rows(rows, ch=ch) is True
        monkeypatch.setattr(pg_publish, "publish_rows", real)
        # Only the refused batch may still hold its writer's lease; the rest are due at once.
        leased = await pg_conn.fetchval(
            "SELECT count(*) FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2 "
            "AND targets <> 0 AND lease_until > now()",
            org,
            ns,
        )
        assert leased <= 2
        owed = -1
        for _ in range(40):  # the refused batch waits out its lease; the server may help
            await deliver_namespace(org, ns)
            owed = await pg_conn.fetchval(
                "SELECT count(*) FROM loom.memory_deliveries "
                "WHERE org = $1 AND namespace = $2 AND targets <> 0",
                org,
                ns,
            )
            if not owed:
                break
            await asyncio.sleep(0.5)
        assert owed == 0
    finally:
        await ch.disconnect()
        await pg.close_pool()
    heads = await pg_conn.fetchval(
        "SELECT count(*) FROM loom.memory_current WHERE org = $1 AND namespace = $2", org, ns
    )
    assert heads == 5
    landed = ch_client.query(
        "SELECT count() FROM loom.memory FINAL WHERE org = {o:String} AND namespace = {n:String}",
        settings=_SEQUENTIAL,
        parameters={"o": org, "n": ns},
    ).result_rows
    assert landed[0][0] == 5


@pytest.mark.parametrize(
    ("scope_type", "published"), [("namespace", False), ("user_id", False), ("namespace", True)]
)
async def test_an_erasure_keeps_the_audit_row_clickhouse_never_got(
    call: Any, pg_conn: Any, org: str, ch_client: Any, scope_type: str, published: bool
) -> None:
    """A revision whose audit row is still owed (here parked, as after ClickHouse refused
    it) is erased at once: the audit row goes to the outbox, due once no publisher of it
    can still land it, and the delivery pass publishes it to ClickHouse once (a row a
    publisher already landed is not written again)."""
    from server.core import pg
    from server.core.ch import get_client
    from server.memory.app.pg_delivery import publish_audit_outbox

    ns, user = f"pg-{uuid.uuid4().hex[:8]}", f"u-{uuid.uuid4().hex[:6]}"
    assert (await call("memory.set", _set(ns, "k", "a fact", user_id=user))).status_code == 200
    seq = await pg_conn.fetchval(
        "SELECT seq FROM loom.memory_revisions WHERE org = $1 AND namespace = $2", org, ns
    )
    row_hash = await pg_conn.fetchval(
        "SELECT audit->>'row_hash' FROM loom.memory_revisions WHERE org = $1 AND namespace = $2",
        org,
        ns,
    )
    if not published:  # a hash ClickHouse has never seen: the published row is this one
        row_hash = uuid.uuid4().hex
        await pg_conn.execute(
            "UPDATE loom.memory_revisions "
            "SET audit = jsonb_set(audit, '{row_hash}', to_jsonb($4::text)) "
            "WHERE org = $1 AND namespace = $2 AND seq = $3",
            org,
            ns,
            seq,
            row_hash,
        )
    await pg_conn.execute(
        "INSERT INTO loom.memory_deliveries (org, namespace, seq, targets, lease_until, "
        "fenced_until) VALUES ($1, $2, $3, 4, 'infinity', now() - interval '1 hour')",
        org,
        ns,
        seq,
    )
    value = ns if scope_type == "namespace" else user
    before = await pg_conn.fetchval("SELECT now()")
    r = await call(
        "erasure.request", {"scope_type": scope_type, "scope_value": value, "requester": "t"}
    )
    assert r.json()["status"] == "completed", r.text
    assert not await pg_conn.fetchval(
        "SELECT count(*) FROM loom.memory_revisions WHERE org = $1 AND namespace = $2", org, ns
    )
    # Due once no publisher of it can still land it, then published by a delivery pass.
    assert await pg_conn.fetchval(
        "SELECT due_at >= $2::timestamptz + make_interval(secs => $3) "
        "FROM loom.audit_outbox WHERE org = $1",
        org,
        before,
        pg.insert_bound_s(),
    )
    await pg_conn.execute("UPDATE loom.audit_outbox SET due_at = now() WHERE org = $1", org)
    ch = get_client()
    await ch.connect()
    try:
        await publish_audit_outbox(100)  # the server's own pass may take it first
    finally:
        await ch.disconnect()
        await pg.close_pool()
    await _until(pg_conn, "SELECT count(*) = 0 FROM loom.audit_outbox WHERE org = $1", org)
    published = ch_client.query(
        "SELECT count() FROM loom.audit_log WHERE org = {o:String} AND namespace = {n:String} "
        "AND row_hash = {h:String}",
        settings=_SEQUENTIAL,
        parameters={"o": org, "n": ns, "h": row_hash},
    ).result_rows
    assert published[0][0] == 1


async def test_a_redaction_leaves_the_restored_copy_of_its_version_alone(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A namespace restored from another's snapshot keeps the memory's id under fresh
    versions: redacting the source's version scrubs the source alone, never the copy."""
    source, copy = f"pg-{uuid.uuid4().hex[:8]}", f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(source, "k", "first text"))).status_code == 200
    second = await call("memory.set", _set(source, "k", "second text"))
    assert second.status_code == 200, second.text
    snapshot = await call("namespace.snapshot", {"namespace": source})
    restored = await call(
        "namespace.restore", {"target_namespace": copy, "payload": snapshot.json()}
    )
    assert restored.status_code == 200, restored.text
    assert (await call("memory.set", _set(source, "k", "third text"))).status_code == 200
    out = second.json()
    r = await call("memory.redact", {"memory_id": out["memory_id"], "version": out["version"]})
    assert r.json()["redacted"] is True, r.text
    got = await call("memory.get", {"namespace": copy, "agent": "a", "memory_key": "k"})
    assert got.json()["value"] == "second text", got.text
    head = await _head_row(pg_conn, org, copy)
    assert "second text" in str(head["value"]) and head.get("redacted_at") is None


async def test_an_outbox_row_clickhouse_keeps_refusing_is_parked_and_retried_by_hand(
    call: Any, pg_conn: Any, org: str, control_plane_url: str
) -> None:
    """An audit row ClickHouse refuses LOOM_PG_DELIVERY_MAX_ATTEMPTS times is parked,
    counted in /v1/health, and published once the documented reset makes it due."""
    from server.core import pg
    from server.core.ch import get_client
    from server.memory.app.pg_delivery import publish_audit_outbox
    from server.settings import settings

    ns, row_hash = f"pg-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    # A value no audit_log column takes: every insert of it is refused.
    audit = {"org": org, "namespace": ns, "row_hash": row_hash, "event_type": ["refused"]}
    await pg_conn.execute(
        "INSERT INTO loom.audit_outbox (org, namespace, row_hash, audit, due_at, attempts) "
        "VALUES ($1, $2, $3, $4::jsonb, now(), $5)",
        org,
        ns,
        row_hash,
        {**audit, "ts": "2026-01-01T00:00:00+00:00"},
        settings.pg_delivery_max_attempts - 1,
    )
    ch = get_client()
    await ch.connect()
    try:
        await publish_audit_outbox(100)  # or the server's own pass
        await _until(
            pg_conn,
            "SELECT due_at = 'infinity' AND last_error <> '' FROM loom.audit_outbox WHERE org = $1",
            org,
        )
        async with httpx.AsyncClient(base_url=control_plane_url, timeout=10) as c:
            health = (await c.get("/v1/health?include=postgres")).json()
            assert health["postgres"]["parked_audits"] >= 1
        # The fix the operator makes, then the reset docs/operations/postgres.md gives.
        await pg_conn.execute(
            'UPDATE loom.audit_outbox SET audit = audit || \'{"event_type": "memory.revision"}\' '
            "WHERE org = $1",
            org,
        )
        await pg_conn.execute(
            "UPDATE loom.audit_outbox SET attempts = 0, due_at = now() WHERE org = $1", org
        )
        await publish_audit_outbox(100)
    finally:
        await ch.disconnect()
        await pg.close_pool()
    await _until(pg_conn, "SELECT count(*) = 0 FROM loom.audit_outbox WHERE org = $1", org)


async def _until(pg_conn: Any, sql: str, *args: Any) -> None:
    """Wait for `sql` to answer true: a delivery pass of the server under test may be
    the one that does the work."""
    for _ in range(100):
        if await pg_conn.fetchval(sql, *args):
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"never true: {sql}")


async def test_an_id_op_on_a_restored_source_reads_the_source(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A namespace restored from another's snapshot keeps the memory ids under newer
    versions: an op addressing the source by id still finds the source's row, and a
    lookup by id alone finds neither."""
    source, copy = f"pg-{uuid.uuid4().hex[:8]}", f"pg-{uuid.uuid4().hex[:8]}"
    out = (await call("memory.set", _set(source, "k", "a fact to copy"))).json()
    snapshot = await call("namespace.snapshot", {"namespace": source})
    restored = await call(
        "namespace.restore", {"target_namespace": copy, "payload": snapshot.json()}
    )
    assert restored.status_code == 200, restored.text
    r = await call(
        "memory.outcome",
        {"namespace": source, "memory_id": out["memory_id"], "outcome": "positive"},
    )
    assert r.json()["recorded"] is True, r.text
    # By id alone, the id two namespaces hold is not chosen between.
    by_id = await call("memory.get_by_id", {"memory_id": out["memory_id"]})
    assert by_id.json()["found"] is False, by_id.text


async def test_list_and_bulk_get_serve_what_the_authority_holds(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A row deleted or rewritten in the authority but not in ClickHouse is not read back
    by memory.list (either order, or a recovery listing, which shows a published delete);
    a redacted head both stores hold is. memory.bulk_get answers the authority's heads, a
    rewrite not yet published included and a 'high' head past its window left out."""
    from server.core import pg
    from server.memory.store import inserts, pg_memory, rows

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    for key in ("kept", "gone", "redacted", "deleted", "rewritten", "scrubbed"):
        r = await call("memory.set", _set(ns, key, f"the {key} fact"))
        assert r.status_code == 200, r.text
    high = _set(ns, "retained", "the badge pin", sensitivity="high", ttl_seconds=1)
    assert (await call("memory.set", high)).status_code == 200
    # Another identity's live row of a key whose newest head is 'high' and expires.
    assert (
        await call("memory.set", {**_set(ns, "shared", "b's note"), "agent": "b"})
    ).status_code == 200
    shared = _set(ns, "shared", "a's secret", sensitivity="high", ttl_seconds=1)
    assert (await call("memory.set", shared)).status_code == 200
    # A head scrubbed in place, as a profile redaction leaves one, in both stores.
    stored = await pg_conn.fetchval(
        "SELECT row FROM loom.memory_current WHERE org = $1 AND namespace = $2 "
        "AND memory_key = 'redacted'",
        org,
        ns,
    )
    head = rows._hydrate(pg_memory.decode_row(stored))
    scrubbed = rows.build_memory_version_row(
        head,
        version=rows.next_version(),
        redacted_at=datetime.now(UTC).replace(tzinfo=None, microsecond=0),
    )
    stored = await pg_conn.fetchval(
        "SELECT row FROM loom.memory_current WHERE org = $1 AND namespace = $2 "
        "AND memory_key = 'rewritten'",
        org,
        ns,
    )
    head = rows._hydrate(pg_memory.decode_row(stored))
    rewrite = rows.build_memory_version_row(
        head, version=rows.next_version(), value='"the rewritten fact, again"'
    )
    try:
        await inserts.insert_memory_versions([scrubbed], publish_now=False)
    finally:
        await pg.close_pool()
    # A search's barrier publishes it to ClickHouse first.
    assert (await call("memory.search", {"namespace": ns, "query": "fact"})).status_code == 200
    try:
        await inserts.insert_memory_versions([rewrite], publish_now=False)  # left unpublished
    finally:
        await pg.close_pool()
    await asyncio.sleep(1.5)  # past the 'high' row's retention window
    for key in ("deleted", "scrubbed"):
        r = await call("memory.delete", {"namespace": ns, "agent": "a", "memory_key": key})
        assert r.status_code == 200, r.text
    # A published delete redacted in the authority whose ClickHouse scrub has not run.
    await pg_conn.execute(
        "UPDATE loom.memory_current SET row = row || jsonb_build_object('redacted_at', "
        "'2026-01-01 00:00:00') WHERE org = $1 AND namespace = $2 AND memory_key = 'scrubbed'",
        org,
        ns,
    )
    await pg_conn.execute(
        # A delete as the authority commits it: a newer version, deleted.
        "UPDATE loom.memory_current SET version = version + 1, row = row || jsonb_build_object("
        "'tombstoned_at', '2026-01-01T00:00:00', 'status', 'tombstoned', "
        "'version', version + 1) "
        "WHERE org = $1 AND namespace = $2 AND memory_key = 'gone'",
        org,
        ns,
    )
    for order in ({}, {"sort_by": "score"}):
        listed = (await call("memory.list", {"namespace": ns, "agent": "a", **order})).json()
        keys = {e["memory_key"] for e in listed["entries"]}
        assert {"kept", "redacted"} <= keys and not {"gone", "deleted", "rewritten"} & keys, listed
    recovery = {"namespace": ns, "agent": "a", "include_tombstoned": True}
    listed = (await call("memory.list", recovery)).json()
    deleted = {e["memory_key"]: e for e in listed["entries"] if e.get("tombstoned_at")}
    # A published delete, and one the authority holds that ClickHouse does not, listed as
    # the authority holds it; not the delete whose redaction ClickHouse lacks.
    assert {"deleted", "gone"} <= set(deleted) and "scrubbed" not in deleted, listed
    assert deleted["gone"]["status"] == "tombstoned", listed
    asked = ["kept", "gone", "redacted", "rewritten", "retained"]
    got = (
        await call("memory.bulk_get", {"namespace": ns, "agent": "a", "memory_keys": asked})
    ).json()
    assert sorted(got["results"]) == ["kept", "redacted", "rewritten"], got
    assert sorted(got["missing"]) == ["gone", "retained"], got
    assert got["results"]["rewritten"]["value"] == "the rewritten fact, again", got
    # Under an open agent axis the expired 'high' head hides no other identity's live row.
    body = {"namespace": ns, "agent": "*", "memory_keys": ["shared"]}
    got = (await call("memory.bulk_get", body)).json()
    assert got["results"]["shared"]["value"] == "b's note", got


async def test_parked_heads_page_in_the_deliveries_key_order(
    call: Any, pg_conn: Any, org: str
) -> None:
    """Live heads whose memory row is parked come a page at a time after a (namespace,
    seq) cursor; a head no longer live, or whose parked revision it no longer is (the key
    written since), does not."""
    from server.core import pg
    from server.memory.store import pg_memory

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    for key in ("a", "b", "c", "d"):
        assert (await call("memory.set", _set(ns, key, f"the {key} fact"))).status_code == 200
    seqs = await pg_conn.fetch(
        "SELECT seq, row->>'memory_key' AS key FROM loom.memory_revisions "
        "WHERE org = $1 AND namespace = $2 ORDER BY seq",
        org,
        ns,
    )
    park = (
        "INSERT INTO loom.memory_deliveries AS d (org, namespace, seq, targets, lease_until, "
        "fenced_until) VALUES ($1, $2, $3, $4, $5::text::timestamptz, now()) "
        "ON CONFLICT (org, namespace, seq) DO UPDATE SET targets = EXCLUDED.targets, "
        "lease_until = EXCLUDED.lease_until, fenced_until = EXCLUDED.fenced_until"
    )
    for r in seqs:
        await pg_conn.execute(park, org, ns, r["seq"], 1, "infinity")
    await pg_conn.execute(
        "UPDATE loom.memory_current SET row = jsonb_set(row, '{tombstoned_at}', "
        "to_jsonb('2026-01-01T00:00:00'::text)) WHERE org = $1 AND namespace = $2 "
        "AND memory_key = 'c'",
        org,
        ns,
    )
    assert (await call("memory.set", _set(ns, "d", "the d fact, again"))).status_code == 200
    try:
        first = await pg_memory.parked_heads(org, ns, after=None, limit=1)
        second = await pg_memory.parked_heads(org, ns, after=(ns, first[0].seq), limit=1)
        last = await pg_memory.parked_heads(org, ns, after=(ns, second[0].seq), limit=1)
        ids = {
            r["memory_key"]: str(r["memory_id"])
            for r in await pg_conn.fetch(
                "SELECT memory_key, memory_id FROM loom.memory_current "
                "WHERE org = $1 AND namespace = $2",
                org,
                ns,
            )
        }
        assert [h.memory_id for h in first + second] == [ids["a"], ids["b"]]
        assert [h.value_text for h in first] == [json.dumps("the a fact")]
        assert last == []
    finally:
        await pg.close_pool()
        for r in seqs:
            await pg_conn.execute(park, org, ns, r["seq"], 0, "-infinity")


async def test_an_org_blacklist_sweep_reaches_a_head_clickhouse_keeps_refusing(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A head whose delivery is parked is not in ClickHouse to match: the sweep judges
    the authority's head itself, and tombstones it when it matches."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    secret = f"tok-{uuid.uuid4().hex[:10]}"
    assert (await call("memory.set", _set(ns, "k", "nothing to refuse"))).status_code == 200
    # The authority's head carries the secret and its delivery is parked: ClickHouse
    # holds only the older text.
    await pg_conn.execute(
        "UPDATE loom.memory_current SET row = jsonb_set(row, '{value}', to_jsonb($3::text)) "
        "WHERE org = $1 AND namespace = $2",
        org,
        ns,
        json.dumps(f"the key is {secret}"),
    )
    seq = await pg_conn.fetchval(
        "SELECT max(seq) FROM loom.memory_revisions WHERE org = $1 AND namespace = $2", org, ns
    )
    park = (
        "INSERT INTO loom.memory_deliveries AS d (org, namespace, seq, targets, lease_until, "
        "fenced_until) VALUES ($1, $2, $3, $4, $5::text::timestamptz, now()) "
        "ON CONFLICT (org, namespace, seq) DO UPDATE SET targets = EXCLUDED.targets, "
        "lease_until = EXCLUDED.lease_until, fenced_until = EXCLUDED.fenced_until"
    )
    await pg_conn.execute(park, org, ns, seq, 1, "infinity")
    try:
        added = await call(
            "blacklist.add", {"namespace": ns, "pattern": secret, "scope": "org", "sweep": True}
        )
        assert added.status_code == 200, added.text
        assert added.json()["swept"] == 1, added.text
        assert (await _head_row(pg_conn, org, ns)).get("tombstoned_at") is not None
    finally:
        await pg_conn.execute(park, org, ns, seq, 0, "-infinity")


async def test_an_org_blacklist_sweep_tombstones_a_source_and_its_restored_copy(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A sweep names each row's namespace: the copy a restore made under the same ids
    is tombstoned with the source, neither skipped as ambiguous."""
    source, copy = f"pg-{uuid.uuid4().hex[:8]}", f"pg-{uuid.uuid4().hex[:8]}"
    secret = f"tok-{uuid.uuid4().hex[:10]}"
    assert (await call("memory.set", _set(source, "k", f"the key is {secret}"))).status_code == 200
    snapshot = await call("namespace.snapshot", {"namespace": source})
    restored = await call(
        "namespace.restore", {"target_namespace": copy, "payload": snapshot.json()}
    )
    assert restored.status_code == 200, restored.text
    added = await call(
        "blacklist.add",
        {"namespace": source, "pattern": secret, "scope": "org", "sweep": True},
    )
    assert added.status_code == 200, added.text
    for ns in (source, copy):
        head = await _head_row(pg_conn, org, ns)
        assert head.get("tombstoned_at") is not None, ns


async def test_a_rollback_is_the_authoritys_head_above_the_one_it_replaces(
    call: Any, pg_conn: Any, org: str
) -> None:
    """namespace.rollback under Postgres: a key changed since `as_of` is reinstated above
    its head, and a key born since is deleted, both in the authority."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    assert (await call("memory.set", _set(ns, "k", "as it was"))).status_code == 200
    await asyncio.sleep(1.5)
    as_of = datetime.now(UTC)
    await asyncio.sleep(1.5)
    assert (await call("memory.set", _set(ns, "k", "changed since"))).status_code == 200
    assert (await call("memory.set", _set(ns, "born", "born since"))).status_code == 200
    changed = int((await _head_row(pg_conn, org, ns, "k"))["version"])
    body = {"namespace": ns, "as_of": as_of.isoformat(), "dry_run": False}
    rolled = await call("namespace.rollback", body)
    assert rolled.status_code == 200, rolled.text
    assert (rolled.json()["reinstate"], rolled.json()["remove"]) == (1, 1), rolled.text
    head = await _head_row(pg_conn, org, ns, "k")
    assert head["value"] == '"as it was"' and int(head["version"]) > changed
    born = await _head_row(pg_conn, org, ns, "born")
    assert born.get("tombstoned_at") is not None


async def test_a_rollback_to_a_high_version_retires_the_embedded_heads_vectors(
    call: Any, pg_conn: Any, org: str, ch_client: Any
) -> None:
    """Reinstating a 'high' version over an embedded head: the publish retires the head's
    vectors with no text, and nothing is owed after."""
    ns = f"pg-{uuid.uuid4().hex[:8]}"
    high = {**_set(ns, "k", "a secret note"), "sensitivity": "high"}
    assert (await call("memory.set", high)).status_code == 200
    await asyncio.sleep(1.5)
    as_of = datetime.now(UTC)
    await asyncio.sleep(1.5)
    assert (await call("memory.set", _set(ns, "k", "an embedded fact"))).status_code == 200
    body = {"namespace": ns, "as_of": as_of.isoformat(), "dry_run": False}
    rolled = await call("namespace.rollback", body)
    assert rolled.status_code == 200 and rolled.json()["reinstate"] == 1, rolled.text
    assert (await _head_row(pg_conn, org, ns, "k"))["sensitivity"] == "high"
    live = ch_client.query(
        "SELECT count() FROM loom.memory_embeddings FINAL WHERE org = {o:String} "
        "AND namespace = {n:String} AND memory_key = 'k' AND tombstoned_at IS NULL",
        settings=_SEQUENTIAL,
        parameters={"o": org, "n": ns},
    ).result_rows[0][0]
    assert live == 0
    texts = ch_client.query(
        "SELECT countIf(content_excerpt != '' OR embedding_dim > 0) "
        "FROM loom.memory_embeddings WHERE org = {o:String} AND namespace = {n:String} "
        "AND memory_key = 'k' AND tombstoned_at IS NOT NULL",
        settings=_SEQUENTIAL,
        parameters={"o": org, "n": ns},
    ).result_rows[0][0]
    assert texts == 0
    owed = await pg_conn.fetchval(
        "SELECT count(*) FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2 "
        "AND targets <> 0",
        org,
        ns,
    )
    assert owed == 0


async def test_a_finding_never_cites_a_row_the_authority_moved_past(
    call: Any, pg_conn: Any, org: str
) -> None:
    """A report cites a sampled row only while the authority serves it: a newer version
    committed with its delivery parked (no barrier waits for it) leaves the finding
    ClickHouse's row raised, uncited, and memory.get serves the newer version."""
    from server.core import pg
    from server.memory.store import pg_memory, rows
    from server.memory.store.pg_publish import audit_row_for

    ns = f"pg-{uuid.uuid4().hex[:8]}"
    blob = json.dumps({"service": "billing", "owner": "payments-team"})
    written = await call("memory.set", _set(ns, "billing/owner", blob))
    assert written.status_code == 200, written.text
    before = await call("namespace.audit", {"namespace": ns, "sample_limit": 5})
    cited = next(f for f in before.json()["findings"] if f["code"] == "double_encoded_values")
    assert [e["version"] for e in cited["evidence"]] == [written.json()["version"]]

    newer = {**await _head_row(pg_conn, org, ns), "version": rows.next_version()}
    try:
        await pg_memory.commit_versions([newer], [audit_row_for(newer, request_id="", writer="")])
    finally:
        await pg.close_pool()
    # Parked by hand, as `pg_memory.record_failure` parks one: no barrier waits for it
    # and no worker claims it.
    await pg_conn.execute(
        "UPDATE loom.memory_deliveries "
        "SET fenced_until = LEAST(lease_until, now()), lease_until = 'infinity' "
        "WHERE org = $1 AND namespace = $2 AND targets <> 0",
        org,
        ns,
    )

    after = (await call("namespace.audit", {"namespace": ns, "sample_limit": 5})).json()
    found = next(f for f in after["findings"] if f["code"] == "double_encoded_values")
    assert found["count"] == 1 and found["evidence"] == []
    assert after["coverage"]["complete"] is True
    got = await call("memory.get", {"namespace": ns, "agent": "a", "memory_key": "billing/owner"})
    assert got.json()["version"] == newer["version"]
