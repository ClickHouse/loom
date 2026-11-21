"""Normal later preparation reaches both key-proof bots through its public router."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from server import boot
from server.cli.cell_attach_beats import widened_window_s
from server.core import cell, pg, provider_process
from server.core import ch as ch_core
from server.insights.store import provider_participants
from server.observability import audit
from server.settings import settings
from tests.integration._cell_each_fleet import (
    _cell_env,
    _keys,
    _provision,
    child,
    private_file,
    tls_pair,
)
from tests.integration._eventually import wait_until
from tests.integration._pg_database import create_database
from tests.integration._ports import free_port
from tests.integration._resources import ch_client_from_env, ch_target, unique_org
from tests.integration._spawn import ROOT, loom_server

_SOURCE_PATHS = (
    "server/boot.py",
    "server/auth/machines.py",
    "server/auth/oauth.py",
    "server/core/pg_profile.py",
    "server/core/provider_process.py",
    "server/insights/app/cell_admin_auth.py",
    "server/insights/store/provider_participants.py",
    "server/cli/cell_prepare.py",
    "server/cli/cell_automation.py",
    "server/cli/cell_bot_proof.py",
    "server/cli/cell_each_http.py",
    "server/core/cell.py",
    "server/router/app.py",
    "server/router/directory.py",
    "tests/integration/_cell_each_fleet.py",
    "tests/integration/_spawn.py",
    "tests/integration/test_cell_prepared_bot_native.py",
)


def _source():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in _SOURCE_PATHS}


def _git():
    return subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD", "HEAD^{tree}"],
        text=True,
    ).splitlines()


def _observe_births(monkeypatch, prefix):
    """Observe real Popen objects; their existing supervisors still own all cleanup."""
    original = subprocess.Popen
    births = []

    def observe(*args, **kwargs):
        process = original(*args, **kwargs)
        env = kwargs.get("env") or {}
        if not env.get("LOOM_INSTANCE_LABEL", "").startswith(prefix):
            return process
        item = {"process": process, "node": env["LOOM_INSTANCE_LABEL"], "error": None}
        births.append(item)
        try:
            proof = provider_process.host_proof().split("|")
            proof[4:6] = [str(process.pid), provider_process._start_ticks(process.pid)]
            item["proof"] = provider_process.certified("|".join(proof))
            assert os.stat(f"/proc/{process.pid}").st_uid == os.geteuid()
            stat = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()
            assert int(stat[1]) == os.getpid()
            argv = list(args[0])
            command = argv[argv.index("-m") + 1 :]
            item["participant"] = command[:2] == ["uvicorn", "server.main:app"] or (
                command[:2] == ["server.cli", "bot"]
                or command[:3]
                in (["server.cli", "cell", "prepare"], ["server.cli", "cell", "attach"])
            )
        except BaseException as error:  # noqa: BLE001 — the real supervisor must receive its child and reap it.
            item["error"] = type(error).__name__
        return process

    monkeypatch.setattr(subprocess, "Popen", observe)
    return births


async def _initialize(env):
    """The ordinary paired authority initializer, before either bot or target web boot."""
    previous = cell.deployment_id()
    with pytest.MonkeyPatch.context() as patch:
        for key, value in {
            "cell": "",
            "cell_router_token": "",
            "storage_authority": "postgres",
            "pg_durability": "synchronous",
            "pg_dsn": env["LOOM_PG_DSN"],
            "ch_database": env["LOOM_CH_DATABASE"],
            "ch_telemetry_database": env["LOOM_CH_TELEMETRY_DATABASE"],
            "ch_reader_user": "",
            "ch_cluster": "",
            "document_s3_bucket": "",
            "boot_migrate": True,
        }.items():
            patch.setattr(settings, key, value)
        patch.setattr(boot, "SKIPPED_PG_MIGRATIONS", boot.SKIPPED_PG_MIGRATIONS)
        ch = ch_core.CHClient()
        patch.setattr(ch_core, "_ch", ch)
        try:
            await pg.close_pool()
            pg.expect_deployment(None)
            await ch.connect()
            await boot.require_storage_authority(ch)
            return cell.deployment_id()
        finally:
            first = sys.exception()
            for close in (pg.close_pool, ch.disconnect):
                try:
                    await close()
                except BaseException as error:  # noqa: BLE001 — both owned clients must close, preserving the first failure.
                    if first is None:
                        first = error
                    else:
                        first.add_note(f"authority cleanup also failed: {type(error).__name__}")
            pg.expect_deployment(None)
            cell.set_deployment_id(previous)
            if first is not None:
                raise first


def _cleanup(errors, action, *args, **kwargs):
    try:
        action(*args, **kwargs)
    except BaseException as error:  # noqa: BLE001 — every owned cleanup runs before the first failure is raised.
        errors.append(error)


@contextmanager
def _report_cleanup_errors(errors):
    """Report store callbacks after their stack unwinds without replacing its first failure."""
    try:
        yield
    finally:
        original = sys.exception()
        selected = original if original is not None else next(iter(errors), None)
        if selected is not None:
            try:
                for error in errors[:32]:
                    if error is selected:
                        continue
                    BaseException.add_note(
                        selected, f"owned cleanup also failed: {type(error).__name__[:80]}"
                    )
                    notes = getattr(error, "__notes__", ())
                    for note in notes[:8]:
                        if isinstance(note, str) and re.fullmatch(
                            r"(?:retention evidence|cleanup client close|authority cleanup|"
                            r"source postcheck)"
                            r" also failed: [A-Za-z_][A-Za-z_0-9]{0,79}",
                            note[:160],
                        ):
                            BaseException.add_note(selected, note[:160])
                        else:
                            BaseException.add_note(selected, "owned cleanup has an unreported note")
                    if len(notes) > 8:
                        BaseException.add_note(selected, "owned cleanup has more secondary notes")
                if len(errors) > 32:
                    BaseException.add_note(selected, "more owned cleanup failures were collected")
            except BaseException as reporting:  # noqa: BLE001 — reporting must preserve the first native or cleanup failure.
                BaseException.add_note(
                    selected, f"cleanup reporting also failed: {type(reporting).__name__[:80]}"
                )
        if original is None and selected is not None:
            raise selected


def _enter(stack, errors, context):
    value = context.__enter__()
    stack.callback(_cleanup, errors, context.__exit__, None, None, None)
    return value


def _drop(cleanup, kind, name, action, *args):
    births = cleanup["births"]
    attempted = False
    try:
        assert cleanup["certified"] and not cleanup["errors"], (
            "Owned stores require complete cleanup certification"
        )
        assert all(item["process"].poll() is not None for item in births), (
            "Owned runtime remains: its stores must be retained"
        )
        assert _source() == cleanup["source"] and _git() == cleanup["git"]
        attempted = True
        action(*args)
    except BaseException as error:  # Retain exact owned resource identifiers and the first failure.
        try:
            cleanup["retained"].append(
                {"kind": kind, "database": name, "deletion_attempted": attempted}
            )
            encoded = (
                json.dumps(
                    {
                        "schema": "loom.native-prepared-bot-proof.incomplete.v1",
                        "retained_or_unconfirmed_stores": cleanup["retained"],
                    },
                    indent=2,
                )
                + "\n"
            )
            print(encoded)
            if evidence := os.environ.get("NATIVE_PREPARED_BOT_PROOF"):
                Path(evidence).write_text(encoded)
        except BaseException as reporting:  # noqa: BLE001 — failed retention evidence must preserve the actual cleanup refusal.
            error.add_note(f"retention evidence also failed: {type(reporting).__name__}")
        raise


def _drop_clickhouse(name):
    client = ch_client_from_env(database="")
    try:
        client.command(f"DROP DATABASE `{name}` SYNC")
    finally:
        first = sys.exception()
        try:
            client.close()
        except BaseException as error:  # Preserve a failed database deletion.
            if first is None:
                raise
            first.add_note(f"cleanup client close also failed: {type(error).__name__}")


async def _drop_postgres(dsn, name):
    import asyncpg

    client = await asyncpg.connect(dsn)
    try:
        # An unexpected backend refuses this ordinary DROP; it must never be terminated.
        await client.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        first = sys.exception()
        try:
            await client.close()
        except BaseException as error:  # Preserve a failed database deletion.
            if first is None:
                raise
            first.add_note(f"cleanup client close also failed: {type(error).__name__}")


def _operator(node, router, operator):
    return {
        **node["env"],
        "LOOM_CELL_ROUTER_URL": router,
        "LOOM_ROUTER_OPERATOR_TOKEN": operator,
        "LOOM_CELL_ROUTER_TOKEN": "",
    }


def _prepare(node, router, operator, *, later):
    command = [
        "server.cli",
        "cell",
        "prepare",
        "--cell",
        node["cell"],
        "--host",
        node["origin"].removeprefix("https://"),
        "--credential-sha256",
        hashlib.sha256(node["credential"].encode()).hexdigest(),
        "--router",
        router,
        "--wait",
        "120",
    ]
    if later:
        command += ["--probe-url", node["probe"], "--upstream-url", node["upstream"]]
    end = time.monotonic() + 120
    answer = json.loads(child(command, _operator(node, router, operator)).stdout)
    assert answer["is_first"] is (not later)
    assert answer["mode"] == ("enforced" if later else "recording")
    return end


def _ready(node, certificate, end):
    context = ssl.create_default_context(cafile=str(certificate))
    with httpx.Client(verify=context, follow_redirects=False, trust_env=False, timeout=5) as client:

        def ready():
            response = client.get(node["origin"] + "/v1/health")
            if response.status_code != 200:
                return False
            body = response.json()
            return body.get("readiness", {}).get("ready") is True

        remaining = end - time.monotonic()
        assert remaining > 0, "The original preparation budget expired"
        wait_until(
            ready, max_wait=remaining, interval=0.2, description="prepared own-host readiness"
        )
        response = client.get(node["origin"] + "/v1/health/deployment")
        assert response.status_code == 200
        assert response.json()["deployment_id"] == node["deployment_id"]


def _retired(births, nodes):
    participant_ids = []
    expected = {label: set() for label in nodes}
    for item in births:
        assert item["error"] is None, "An actual child birth could not be certified"
        process = item["process"]
        assert process.returncode is not None
        assert provider_process.proves_stopped(item["proof"])
        if not item["participant"]:
            continue
        node = nodes[item["node"]]
        expected[item["node"]].add(item["proof"])
        rows = (
            node["ch"]
            .query(
                "SELECT participant_id, state, stale_after_s, transition_id, version "
                "FROM {db:Identifier}._provider_participants FINAL WHERE host_proof={proof:String}",
                parameters={"db": node["database"], "proof": item["proof"]},
            )
            .result_rows
        )
        assert len(rows) == 1
        identity, state, stale, transition, version = rows[0]
        assert state == "retired" and stale == 0 and str(transition) == str(UUID(int=0))
        assert version > 0 and provider_process.has_certificate(item["proof"])
        assert provider_participants.running_queries_sync(node["ch"], identity) == 0
        participant_ids.append(str(identity))
    assert len(participant_ids) == len(set(participant_ids))
    for label, node in nodes.items():
        rows = (
            node["ch"]
            .query(
                "SELECT host_proof FROM {db:Identifier}._provider_participants FINAL LIMIT 65",
                parameters={"db": node["database"]},
            )
            .result_rows
        )
        assert len(rows) <= 64 and {row[0] for row in rows} == expected[label]
    return participant_ids


def _token_writes(node):
    params = {"db": node["database"]}
    ch = node["ch"]
    assert ch.query(
        "SELECT count() FROM {db:Identifier}.machine_identities",
        parameters=params,
    ).result_rows == [(2,)]
    assert ch.query(
        "SELECT count() FROM {db:Identifier}.oauth_state",
        parameters=params,
    ).result_rows == [(0,)]
    marks = ch.query(
        "SELECT claim_key, value, expires_at FROM {db:Identifier}.oauth_signin_state LIMIT 3",
        parameters=params,
    ).result_rows
    assert len(marks) == 2
    for key, value, expires in marks:
        assert re.fullmatch(r"assertion:[0-9a-f]{64}", key)
        assert re.fullmatch(r"used:[0-9a-f]{16}", value) and type(expires) is int and expires > 0
    result = ch.query(
        "SELECT ts, event_type, org, namespace, actor, subject, request_id, row_hash, token_id "
        "FROM {db:Identifier}.audit_log ORDER BY ts LIMIT 5",
        parameters=params,
    )
    rows = [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]
    assert len(rows) == 4
    assert {row["event_type"] for row in rows} == {"auth.machine_created", "auth.machine_token"}
    tokens = [row for row in rows if row["event_type"] == "auth.machine_token"]
    assert len(tokens) == 2 and {row["actor"] for row in tokens} == {
        "sa:" + node[p]["name"] for p in ("automation", "recovery")
    }
    for row in rows:
        assert row["org"] == node["org"] and row["namespace"] == "-"
        assert audit.row_matches_hash(row)
    for row in tokens:
        subject = json.loads(row["subject"])
        assert subject.keys() == {"jti", "by", "ttl_s", "platform_admin"}
        assert subject["by"] == "key" and subject["ttl_s"] == 60
        assert subject["platform_admin"] is True and subject["jti"]
    return {"machine_rows": 2, "assertion_replay_marks": 2, "token_audits": 2}


async def _local_prepared(dsn):
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        first = await conn.fetchrow("SELECT is_first, cell FROM loom.cell_first")
        assert dict(first) == {"is_first": False, "cell": "later"}
        assert await conn.fetchval("SELECT mode FROM loom.tenancy_mode") == "enforced"
        assert await conn.fetchval("SELECT count(*) FROM loom.tenancy") == 0
        own = await conn.fetchrow("SELECT current_fp, next_fp FROM loom.cell_self")
        return {
            name: json.loads(value) if value is not None else None for name, value in own.items()
        }
    finally:
        await conn.close()


async def _no_backends(admin_dsn, databases):
    import asyncpg

    conn = await asyncpg.connect(admin_dsn)
    try:
        return (
            await conn.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = ANY($1::text[]) "
                "AND backend_type = 'client backend'",
                databases,
            )
            == 0
        )
    finally:
        await conn.close()


@dataclass
class _NativeCase:
    resources: ExitStack
    stores: ExitStack
    cleanup: dict
    cleanup_errors: list
    births: list
    admin: Any
    admin_dsn: str
    home: Path
    router: str
    router_dsn: str
    router_name: str
    certificate: tuple[Path, Path]
    prefix: str
    nodes: dict
    operator: str


def _fresh_node(case, name, public_port):
    pg_name, dsn = asyncio.run(create_database(case.admin_dsn))
    case.stores.callback(
        _cleanup,
        case.cleanup_errors,
        _drop,
        case.cleanup,
        "postgres",
        pg_name,
        lambda n=pg_name: asyncio.run(_drop_postgres(case.admin_dsn, n)),
    )
    database = "native_bots_" + uuid4().hex
    for db in (database, database + "_telemetry"):
        case.admin.command(f"CREATE DATABASE `{db}`")
        case.stores.callback(
            _cleanup,
            case.cleanup_errors,
            _drop,
            case.cleanup,
            "clickhouse",
            db,
            _drop_clickhouse,
            db,
        )
    env = _cell_env(name, dsn, database, case.router, case.certificate[0])
    api_port = free_port()
    origin = f"https://127.0.0.1:{public_port}"
    env.update(
        {
            "LOOM_PUBLIC_BASE_URL": origin,
            "LOOM_OAUTH_ISSUER": origin,
            "LOOM_JWT_ISSUER": origin,
            "LOOM_INSTANCE_LABEL": case.prefix + ":" + name,
            "LOOM_INSTANCE_HEARTBEAT_SECONDS": "1",
            "LOOM_PG_DURABILITY": "synchronous",
            "LOOM_EMBEDDING_PROVIDER": "fake",
            "LOOM_LLM_MODEL": "fake-echo-1",
            "LOOM_REQUIRE_REAL_PROVIDERS": "false",
            "LOOM_SHUTDOWN_READY_DELAY_S": "0",
        }
    )
    credential = env["LOOM_CELL_ROUTER_TOKEN"]
    node = {
        "cell": name,
        "env": env,
        "origin": origin,
        "database": database,
        "credential": credential,
        "upstream": f"http://127.0.0.1:{api_port}",
        "probe": "http://127.0.0.1:" + env["LOOM_CELL_PROBE_PORT"],
        "api_port": api_port,
        "pg_name": pg_name,
        "org": unique_org("native-bots-" + name),
    }
    bootstrap_env = {**env, "LOOM_CELL": "", "LOOM_CELL_ROUTER_TOKEN": ""}
    child(["server.core.schema.migrate"], bootstrap_env)
    node["deployment_id"] = asyncio.run(_initialize(bootstrap_env))
    for purpose in ("automation", "recovery"):
        bot_name = purpose + "-" + uuid4().hex[:12]
        key_file = case.home / (bot_name + ".pem")
        public_file = case.home / (bot_name + ".pub.pem")
        for path in (key_file, public_file):
            case.resources.callback(_cleanup, case.cleanup_errors, path.unlink, missing_ok=True)
        bot = node[purpose] = _keys(case.home, bot_name)
        private_file(public_file, bot["public_key"])
        child(
            [
                "server.cli",
                "bot",
                "create",
                "--name",
                bot["name"],
                "--org",
                node["org"],
                "--platform-admin",
                "--public-key",
                str(public_file),
            ],
            bootstrap_env,
        )
    node["ch"] = ch_client_from_env(database=database)
    case.resources.callback(_cleanup, case.cleanup_errors, node["ch"].close)
    return node


def _prove_normal_cycle(case):
    first, later = list(case.nodes.values())
    router_env = {
        **first["env"],
        "LOOM_CELL": "",
        "LOOM_PG_DSN": "",
        "LOOM_ROUTER_PG_DSN": case.router_dsn,
        "LOOM_CELL_ROUTER_TOKEN": "",
        "LOOM_ROUTER_OPERATOR_SHA256": hashlib.sha256(case.operator.encode()).hexdigest(),
        "LOOM_ROUTER_SHARED_HOST": first["origin"].removeprefix("https://"),
        "LOOM_ROUTER_UPSTREAM_URL": first["upstream"],
        "LOOM_ROUTER_UPSTREAM_PROBE_URL": first["probe"],
        "LOOM_INSTANCE_LABEL": case.prefix + ":router",
    }
    child(["server.cli", "router", "migrate"], router_env)

    with ExitStack() as services:
        _enter(
            services,
            case.cleanup_errors,
            loom_server(
                "native-first-router",
                router_env,
                port=httpx.URL(first["origin"]).port,
                tls_files=case.certificate,
                router=True,
            ),
        )
        # Only the existing FIRST deployment follows its supported CELL-unset rollout.
        with ExitStack() as initial:
            _enter(
                initial,
                case.cleanup_errors,
                loom_server(
                    "native-first-open",
                    {**first["env"], "LOOM_CELL": ""},
                    port=first["api_port"],
                ),
            )
            _prepare(first, case.router, case.operator, later=False)
        if case.cleanup_errors:
            raise case.cleanup_errors[0]
        _enter(
            services,
            case.cleanup_errors,
            loom_server(
                "native-first-cell",
                first["env"],
                port=first["api_port"],
            ),
        )
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(settings, "instance_heartbeat_seconds", 1.0)
            window = widened_window_s()
        # Stopped unset writers must age past the normal first-attach source bound.
        time.sleep(window)
        child(
            [
                "server.cli",
                "cell",
                "attach",
                "--cell",
                first["cell"],
                "--router",
                case.router,
                "--ch-allow-user",
                ch_target()["username"],
            ],
            _operator(first, case.router, case.operator),
        )
        _ready(first, case.certificate[0], time.monotonic() + 45)
        _provision(
            {**first, "env": _operator(first, case.router, case.operator)},
            case.router,
            case.operator,
            case.certificate[0],
        )
        _enter(
            services,
            case.cleanup_errors,
            loom_server(
                "native-later-router",
                router_env,
                port=httpx.URL(later["origin"]).port,
                tls_files=case.certificate,
                router=True,
            ),
        )
        _enter(
            services,
            case.cleanup_errors,
            loom_server(
                "native-later-cell",
                later["env"],
                port=later["api_port"],
            ),
        )
        with httpx.Client(timeout=5, trust_env=False) as client:
            gated = client.get(later["upstream"] + "/v1/health").json()
        assert gated["readiness"]["checks"]["cell"]["ok"] is False
        end = _prepare(later, case.router, case.operator, later=True)
        _ready(later, case.certificate[0], end)
        own = asyncio.run(_local_prepared(later["env"]["LOOM_PG_DSN"]))
        with httpx.Client(
            verify=ssl.create_default_context(cafile=str(case.certificate[0])),
            trust_env=False,
            follow_redirects=False,
            timeout=15,
        ) as client:
            response = client.get(
                case.router + "/cells/v1/operator/cells/later",
                headers={"Authorization": "Bearer " + case.operator},
            )
        assert response.status_code == 200
        registered = response.json()
        assert registered["state"] == "attaching" and registered["mode"] == "enforced"
        assert registered["upstream_url"] == later["upstream"]
        assert (registered["current_fp"], registered["next_fp"]) == (
            own["current_fp"],
            own["next_fp"],
        )
        _provision(
            {**later, "env": _operator(later, case.router, case.operator)},
            case.router,
            case.operator,
            case.certificate[0],
        )
        assert asyncio.run(_local_prepared(later["env"]["LOOM_PG_DSN"])) == own
        token_writes = _token_writes(later)
        # Normal provisioning proves both keys through OAuth, whoami and bots.list.
        receipt = {
            "schema": "loom.native-prepared-bot-proof.v1",
            "token_writes": token_writes,
            "first_cell": first["cell"],
            "later_cell": later["cell"],
            "later_deployment_id": later["deployment_id"],
            "bots": {
                p: {k: later[p][k] for k in ("name", "thumbprint")}
                for p in ("automation", "recovery")
            },
        }
    if case.cleanup_errors:
        raise case.cleanup_errors[0]
    receipt["retired_participant_ids"] = _retired(case.births, case.nodes)
    wait_until(
        lambda: asyncio.run(
            _no_backends(
                case.admin_dsn,
                [case.router_name, *(node["pg_name"] for node in case.nodes.values())],
            )
        ),
        max_wait=5,
        interval=0.1,
        description="owned native backends closed",
    )
    return receipt


@pytest.fixture
def native_environment(monkeypatch):
    admin_dsn = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    ch_target()
    native_ch_env = {key: value for key, value in os.environ.items() if key.startswith("LOOM_CH_")}
    for key in list(os.environ):
        if key.startswith("LOOM_"):
            monkeypatch.delenv(key)
    for key, value in native_ch_env.items():
        monkeypatch.setenv(key, value)
    return admin_dsn


def test_normal_prepared_later_cell_proves_both_bootstrap_keys(
    native_environment, monkeypatch, tmp_path
):
    admin_dsn = native_environment
    source = _source()
    source_git = _git()
    prefix = "ci:prepared-bots:" + uuid4().hex
    home = tmp_path / "prepared-bots"
    home.mkdir(mode=0o700)
    births = _observe_births(monkeypatch, prefix)
    cleanup_errors = []
    cleanup = {
        "certified": False,
        "errors": cleanup_errors,
        "births": births,
        "source": source,
        "git": source_git,
        "retained": [],
    }
    with _report_cleanup_errors(cleanup_errors), ExitStack() as stores:
        try:
            with ExitStack() as resources:
                for path in (home / "native.crt", home / "native.pem"):
                    resources.callback(_cleanup, cleanup_errors, path.unlink, missing_ok=True)
                certificate = tls_pair(home, "native")
                operator = "lco_" + secrets.token_hex(64)
                router_name, router_dsn = asyncio.run(create_database(admin_dsn))
                stores.callback(
                    _cleanup,
                    cleanup_errors,
                    _drop,
                    cleanup,
                    "postgres",
                    router_name,
                    lambda: asyncio.run(_drop_postgres(admin_dsn, router_name)),
                )
                admin = ch_client_from_env(database="")
                resources.callback(_cleanup, cleanup_errors, admin.close)
                first_public, later_public = free_port(), free_port()
                router = f"https://127.0.0.1:{first_public}"

                case = _NativeCase(
                    resources=resources,
                    stores=stores,
                    cleanup=cleanup,
                    cleanup_errors=cleanup_errors,
                    births=births,
                    admin=admin,
                    admin_dsn=admin_dsn,
                    home=home,
                    router=router,
                    router_dsn=router_dsn,
                    router_name=router_name,
                    certificate=certificate,
                    prefix=prefix,
                    nodes={},
                    operator=operator,
                )
                for name, public_port in (("first", first_public), ("later", later_public)):
                    node = _fresh_node(case, name, public_port)
                    case.nodes[node["env"]["LOOM_INSTANCE_LABEL"]] = node
                receipt = _prove_normal_cycle(case)
                receipt.update(source_sha256=source, head=source_git[0], tree=source_git[1])
            if cleanup_errors:
                raise cleanup_errors[0]
            assert receipt is not None
            assert _source() == source and _git() == source_git
            if evidence := os.environ.get("NATIVE_PREPARED_BOT_PROOF"):
                Path(evidence).write_text(json.dumps(receipt, indent=2) + "\n")
        finally:
            original = sys.exception()
            selected = original if original is not None else next(iter(cleanup_errors), None)
            try:
                assert _source() == source and _git() == source_git
            except BaseException as error:  # noqa: BLE001 — source evidence must retain an earlier native or cleanup failure.
                if selected is None:
                    selected = error
                else:
                    selected.add_note(f"source postcheck also failed: {type(error).__name__}")
            cleanup["certified"] = original is None and selected is None
            if original is None and selected is not None:
                raise selected
