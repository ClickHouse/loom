"""Two native bot creates on fresh paired stores leave only their exact control rows."""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import secrets
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from server import boot
from server.auth import machines
from server.core import cell, ch_names, pg, provider_process
from server.core import ch as ch_core
from server.insights.store import provider_participants
from server.observability import audit
from server.settings import settings
from tests.integration._pg_database import own_database
from tests.integration._spawn import ROOT
from tests.integration.test_cell_attach import (
    cell_clickhouse_databases as cell_clickhouse_databases,
)


async def _catalog(ch, conn):
    aliases = [ch_names.database(), ch_names.telemetry_database()]
    tables = await ch.query_all(
        "SELECT database, name, engine FROM system.tables "
        "WHERE database IN {dbs:Array(String)} AND engine NOT IN ('View', 'MaterializedView') "
        "ORDER BY database, name LIMIT 4097",
        {"dbs": aliases},
    )
    assert len(tables) <= 4096
    columns = await ch.query_one(
        "SELECT count() AS n FROM system.columns WHERE database IN {dbs:Array(String)}",
        {"dbs": aliases},
    )
    assert columns["n"] <= 65536
    counts = {}
    for row in tables:
        name = ch_names.logical(row["database"]) + "." + row["name"]
        assert name not in counts
        count = await ch.query_one(
            "SELECT count() AS n FROM {db:Identifier}.{table:Identifier}",
            {"db": row["database"], "table": row["name"]},
        )
        counts[name] = int(count["n"])
    relations = await conn.fetch(
        "SELECT n.nspname, c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'p') AND n.nspname NOT LIKE 'pg_%' "
        "AND n.nspname <> 'information_schema' ORDER BY 1, 2 LIMIT 4097"
    )
    assert len(relations) <= 4096
    pg_counts = {}
    for row in relations:
        schema, relation = row["nspname"], row["relname"]
        # Names come only from the owned database's catalog, never a CLI argument.
        quoted = '"' + schema.replace('"', '""') + '"."' + relation.replace('"', '""') + '"'
        pg_counts[schema + "." + relation] = await conn.fetchval("SELECT count(*) FROM " + quoted)
    return counts, pg_counts


def _only_counts(counts, permitted):
    assert {name: count for name, count in counts.items() if count and name not in permitted} == {}


async def _fixed_records(ch, conn):
    return {
        "ch_migrations": await ch.query_all(
            "SELECT * FROM loom._migrations ORDER BY filename, applied_at, applied_by, checksum"
        ),
        "ch_authority": await ch.query_all("SELECT * FROM loom.storage_authority"),
        "pg_migrations": [
            dict(row)
            for row in await conn.fetch("SELECT * FROM loom.pg_migrations ORDER BY filename")
        ],
        "pg_authority": [dict(row) for row in await conn.fetch("SELECT * FROM loom.authority")],
    }


async def _create_bot(env, name, public_key):
    command = [
        sys.executable,
        "-m",
        "server.cli",
        "bot",
        "create",
        "--name",
        name,
        "--org",
        "bootstrap-admin",
        "--platform-admin",
        "--public-key",
        str(public_key),
    ]
    process = await asyncio.create_subprocess_exec(
        *command, cwd=ROOT, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        # Capture the actual child birth before it can be reaped or its PID reused.
        birth = provider_process._start_ticks(process.pid)
        proof = provider_process.host_proof().split("|")
        proof[4:6] = [str(process.pid), birth]
        expected_proof = provider_process.certified("|".join(proof))
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        assert process.returncode == 0, "Owned native bot creation failed"
        assert f"bot {name}: tokens for sa:{name} in bootstrap-admin" in stdout.decode()
        return expected_proof
    finally:
        first = sys.exception()
        if process.returncode is None:
            try:
                process.terminate()
                try:
                    await asyncio.wait_for(process.communicate(), timeout=20)
                except TimeoutError:
                    process.kill()
                    await asyncio.wait_for(process.communicate(), timeout=5)
            except BaseException as error:  # Preserve the native failure after owned cleanup.
                if first is None:
                    raise
                first.add_note(f"native cleanup also failed: {type(error).__name__}")


@pytest.mark.asyncio
async def test_two_public_key_bots_bootstrap_without_tenancy_and_retire(
    cell_clickhouse_databases, monkeypatch, tmp_path
):
    async with own_database() as dsn:
        await _bootstrap(cell_clickhouse_databases, monkeypatch, tmp_path, dsn)


async def _bootstrap(cell_clickhouse_databases, monkeypatch, tmp_path, dsn):
    import asyncpg

    ch = None
    conn = None
    previous_deployment = cell.deployment_id()
    fixed_ch = {"loom._migrations", "loom.storage_authority"}
    fixed_pg = {"loom.pg_migrations", "loom.authority"}
    home = tmp_path / "native-bootstrap"
    home.mkdir(mode=0o700)
    env = {key: value for key, value in os.environ.items() if not key.startswith("LOOM_")}
    env.update(
        {
            key: value
            for key, value in os.environ.items()
            if key.startswith(("LOOM_CH_", "LOOM_PG_"))
        }
    )
    env.update(
        {
            "LOOM_ENV_FILE": "",
            "LOOM_CELL": "",
            "LOOM_CELL_ROUTER_TOKEN": "",
            "LOOM_PG_DSN": dsn,
            "LOOM_STORAGE_AUTHORITY": "postgres",
            "LOOM_CH_DATABASE": cell_clickhouse_databases[0],
            "LOOM_CH_TELEMETRY_DATABASE": cell_clickhouse_databases[1],
            "LOOM_CH_READER_USER": "",
            "LOOM_CH_CLUSTER": "",
            "LOOM_DOCUMENT_S3_BUCKET": "",
            "LOOM_DOCUMENT_S3_PREFIX": "originals",
            "LOOM_BACKGROUND_WORKERS": "off",
            "LOOM_EMBEDDING_MODEL": "fake-ngram-1024",
            "LOOM_LLM_PROVIDER": "fake",
            "LOOM_JWT_SECRET": secrets.token_hex(32),
            "LOOM_AUTH_REQUIRED": "true",
            "LOOM_MCP_AUTH_MODE": "required",
            "LOOM_BOOT_MIGRATE": "true",
            "LOOM_INSTANCE_LABEL": "ci:bot-bootstrap",
        }
    )
    for key, value in {
        "cell": "",
        "cell_router_token": "",
        "storage_authority": "postgres",
        "pg_dsn": dsn,
        "ch_database": cell_clickhouse_databases[0],
        "ch_telemetry_database": cell_clickhouse_databases[1],
        "ch_reader_user": "",
        "ch_cluster": "",
        "document_s3_bucket": "",
        "boot_migrate": True,
    }.items():
        monkeypatch.setattr(settings, key, value)
    ch = ch_core.CHClient()
    monkeypatch.setattr(ch_core, "_ch", ch)
    try:
        await pg.close_pool()
        pg.expect_deployment(None)
        await ch.connect()
        conn = await asyncpg.connect(dsn)
        before_ch, before_pg = await _catalog(ch, conn)
        _only_counts(before_ch, {"loom._migrations"})
        _only_counts(before_pg, {"loom.pg_migrations"})
        assert before_pg["loom.pg_migrations"] > 0
        # This is the ordinary initializer, not hand-inserted authority or cell state.
        await boot.require_storage_authority(ch)
        fixed = await _fixed_records(ch, conn)
        assert len(fixed["ch_authority"]) == len(fixed["pg_authority"]) == 1
        authority = {
            key: fixed["ch_authority"][0][key] for key in ("authority", "epoch", "deployment_id")
        }
        assert authority["authority"] == "postgres" and int(authority["epoch"]) == 1
        assert {key: fixed["pg_authority"][0][key] for key in authority} == authority
        assert str(UUID(authority["deployment_id"])) == authority["deployment_id"]
        baseline_ch, baseline_pg = await _catalog(ch, conn)
        assert baseline_ch["loom.storage_authority"] == 1
        _only_counts(baseline_ch, fixed_ch)
        _only_counts(baseline_pg, fixed_pg)
        backend_births = {
            (row["pid"], row["backend_start"])
            for row in await conn.fetch(
                "SELECT pid, backend_start FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
        }
        proofs = []
        bots = {}
        for purpose in ("automation", "recovery"):
            bot = purpose + "-" + uuid4().hex[:12]
            _private, public = machines.generate_key()
            with (public_file := home / (bot + ".pub.pem")).open("xb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(public)
            bots[bot] = machines.public_jwks(public)
            proofs.append(await _create_bot(env, bot, public_file))
        assert len(set(proofs)) == len(bots) == 2
        assert len({json.loads(public)["keys"][0]["kid"] for public in bots.values()}) == 2
        assert {
            (row["pid"], row["backend_start"])
            for row in await conn.fetch(
                "SELECT pid, backend_start FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
        } <= backend_births
        final_ch, final_pg = await _catalog(ch, conn)
        assert final_ch.keys() == baseline_ch.keys() and final_pg.keys() == baseline_pg.keys()
        _only_counts(
            final_ch,
            fixed_ch | {"loom.machine_identities", "loom.audit_log", "loom._provider_participants"},
        )
        _only_counts(final_pg, fixed_pg)
        assert {name: final_ch[name] for name in fixed_ch} == {
            name: baseline_ch[name] for name in fixed_ch
        }
        assert await _fixed_records(ch, conn) == fixed
        assert final_ch["loom.machine_identities"] == final_ch["loom.audit_log"] == 2
        actor = "cli:" + getpass.getuser()
        stored_bots = await ch.query_all(
            "SELECT name, active, updated_by, updated_at, version "
            "FROM loom.machine_identities LIMIT 3"
        )
        assert len(stored_bots) == 2 and {row["name"] for row in stored_bots} == set(bots)
        for row in stored_bots:
            assert row["active"] == 1 and row["updated_by"] == actor
            assert row["version"] > 0 and row["updated_at"]
        for bot, public in bots.items():
            row = await machines.get(bot, ch=ch)
            assert row == {
                "name": bot,
                "org": "bootstrap-admin",
                "ns_roles": [],
                "public_keys": public,
                "issuer": "",
                "audience": "",
                "claims": {},
                "agent_claim": "",
                "jwks_uri": "",
                "max_token_s": 3600,
                "max_agents": 1000,
                "max_access_token_s": machines.TOKEN_TTL,
                "platform_admin": True,
            }
        rows = await ch.query_all(
            "SELECT ts, event_type, org, namespace, actor, subject, request_id, row_hash, token_id "
            "FROM loom.audit_log ORDER BY ts"
        )
        assert {json.loads(row["subject"])["name"] for row in rows} == set(bots)
        for row in rows:
            assert row["event_type"] == "auth.machine_created"
            assert (
                row["org"],
                row["namespace"],
                row["actor"],
                row["request_id"],
                row["token_id"],
            ) == ("bootstrap-admin", "-", actor, "", "")
            assert json.loads(row["subject"]) == {
                "name": json.loads(row["subject"])["name"],
                "key": True,
                "issuer": "",
                "max_access_token_s": machines.TOKEN_TTL,
                "platform_admin": True,
            }
            assert audit.row_matches_hash(row)
        retired = await ch.query_all(
            "SELECT participant_id, transition_id, state, version, stale_after_s, host_proof "
            "FROM loom._provider_participants FINAL"
        )
        assert final_ch["loom._provider_participants"] <= 64
        history = await ch.query_all(
            "SELECT participant_id, transition_id, state, version, host_proof "
            "FROM loom._provider_participants LIMIT 65"
        )
        assert len(history) <= 64
        assert {str(row["participant_id"]) for row in history} == {
            str(row["participant_id"]) for row in retired
        }
        assert all(row["host_proof"] in proofs for row in history)
        assert all(row["state"] in {"running", "drained", "retired"} for row in history)
        assert all(str(row["transition_id"]) == str(UUID(int=0)) for row in history)
        assert len(retired) == 2
        assert len({str(row["participant_id"]) for row in retired}) == 2
        assert {row["host_proof"] for row in retired} == set(proofs)
        for row in retired:
            assert row["state"] == "retired" and row["stale_after_s"] == 0
            assert str(row["transition_id"]) == str(UUID(int=0)) and row["version"] > 0
            assert provider_process.has_certificate(row["host_proof"])
            assert provider_process.proves_stopped(row["host_proof"])
            assert await provider_participants.running_queries(row["participant_id"], ch=ch) == 0
        receipt = {
            "schema": "loom.native-bot-bootstrap.v1",
            "native_commands": 2,
            "bots": sorted(bots),
            "participant_ids": sorted(str(row["participant_id"]) for row in retired),
            "ch_counts": final_ch,
            "pg_counts": final_pg,
        }
        encoded = json.dumps(receipt, indent=2) + "\n"
        (home / "write-census.json").write_text(encoded)
        if evidence := os.environ.get("NATIVE_BOT_BOOTSTRAP_CENSUS"):
            await asyncio.to_thread(Path(evidence).write_text, encoded)
    finally:
        first = sys.exception()
        for close in (
            conn.close if conn is not None else None,
            pg.close_pool,
            ch.disconnect,
        ):
            if close is not None:
                try:
                    await close()
                except BaseException as error:  # noqa: BLE001 — every owned close retains the first failure.
                    if first is None:
                        first = error
                    else:
                        first.add_note(f"bootstrap cleanup also failed: {type(error).__name__}")
        pg.expect_deployment(None)
        cell.set_deployment_id(previous_deployment)
        if first is not None:
            raise first
