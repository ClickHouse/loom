"""Attach a first cell while two real API processes share its private stores.

The router uses real HTTP upstreams and a test-owned, verified TLS listener. No
heartbeat row or health response is supplied by the test. Requires the same
synchronous Postgres and ClickHouse services as test_cell_attach.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from server.cli import cell_attach, cell_prepare
from server.core import cell_triggers, instances, pg, provider_process
from server.core.ch import query_tag
from server.insights.store import provider_participants
from server.router import app as router_app
from server.settings import settings
from tests.integration import test_cell_attach as attach_fixtures
from tests.integration._ports import free_port
from tests.integration._redact import redact
from tests.integration._resources import unique_org
from tests.integration._spawn import ROOT, loom_server
from tests.integration.test_cell_attach import (
    _args,
    _mode,
    _placed,
    _row,
)

_CREDENTIAL = "lcr_" + "3" * 128
_WAIT_S = 45

cell_clickhouse_databases = attach_fixtures.cell_clickhouse_databases
cell_stores = attach_fixtures.cell_stores


@pytest.fixture
def genuine_cell_processes() -> bool:
    return True


def _tls_files(root: Path) -> tuple[Path, Path, Path]:
    """An ephemeral CA and IP-address leaf, trusted only by this test's clients."""
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "attach test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path, cert_path, key_path = (root / name for name in ("ca.pem", "server.pem", "key.pem"))
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return ca_path, cert_path, key_path


class _RouterServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        # pytest owns the main thread's signals.
        yield


@contextlib.asynccontextmanager
async def _router(port: int, cert: Path, key: Path) -> AsyncIterator[None]:
    router = router_app.Router()
    server = _RouterServer(
        uvicorn.Config(
            router_app.build_app(router),
            host="127.0.0.1",
            port=port,
            ssl_certfile=str(cert),
            ssl_keyfile=str(key),
            log_level="warning",
            timeout_graceful_shutdown=5,
        )
    )
    task = asyncio.create_task(server.serve(), name="attach-test-router")
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError("router stopped before listening")
                await asyncio.sleep(0.05)
        yield
    finally:
        first = sys.exception()
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        except BaseException as error:  # noqa: BLE001 — finish all owned cleanup; keep first failure.
            first = first or error
        finally:
            # A timed-out uvicorn task must not leave its listener or requests running.
            for listener in getattr(server, "servers", ()):
                try:
                    listener.close()
                    await listener.wait_closed()
                except BaseException as error:  # noqa: BLE001 — attempt remaining owned cleanup.
                    first = first or error
            requests = tuple(server.server_state.tasks)
            for request in requests:
                request.cancel()
            for operation in (
                asyncio.wait_for(asyncio.gather(*requests, return_exceptions=True), timeout=5),
                router.close(),
            ):
                try:
                    await operation
                except BaseException as error:  # noqa: BLE001 — keep first; finish router cleanup.
                    first = first or error
        if first is not None:
            raise first


@contextlib.asynccontextmanager
async def _api(name: str, env: dict[str, str], port: int) -> AsyncIterator[str]:
    """Keep the router loop available and retain each worker until its context settles."""
    owned = loom_server(name, {**env, "LOOM_INSTANCE_LABEL": name}, port=port)
    first: BaseException | None = None

    async def finish[T](task: asyncio.Task[T]) -> T:
        nonlocal first
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError as error:
                if first is None:
                    first = error
        return task.result()

    entry = asyncio.create_task(asyncio.to_thread(owned.__enter__))
    try:
        yield await asyncio.shield(entry)
    except BaseException as error:  # noqa: BLE001 — cleanup completes before the first error escapes.
        first = error
    finally:
        try:
            await finish(entry)
        except BaseException as error:  # noqa: BLE001 — a failed entry already ran its own cleanup.
            first = first or error
        else:
            try:
                await finish(
                    asyncio.create_task(asyncio.to_thread(owned.__exit__, None, None, None))
                )
            except BaseException as error:  # noqa: BLE001 — retain the body error after owned cleanup.
                if first is None:
                    first = error
                else:
                    first.add_note(f"{name} process cleanup also failed: {type(error).__name__}")
        if first is not None:
            raise first


def _configure(monkeypatch: pytest.MonkeyPatch, host: str, api: int, probe: int) -> dict[str, str]:
    values = {
        "cell": "first",
        "storage_authority": "postgres",
        "pg_durability": "synchronous",
        "bind_host": "127.0.0.1",
        "serves_traffic": True,
        "pg_dsn": settings.pg_dsn,
        "router_pg_dsn": settings.router_pg_dsn,
        "ch_database": settings.ch_database,
        "ch_telemetry_database": settings.ch_telemetry_database,
        "ch_cluster": "",
        "document_s3_bucket": "",
        "public_base_url": f"https://{host}",
        "oauth_issuer": "",
        "cell_router_url": f"https://{host}",
        "cell_router_token": _CREDENTIAL,
        "cell_probe_port": probe,
        "router_shared_host": host,
        "router_upstream_url": f"http://127.0.0.1:{api}",
        "router_upstream_probe_url": f"http://127.0.0.1:{probe}",
        "instance_heartbeat_seconds": 1,
        "tenancy_mode_cache_s": 1,
        "router_directory_cache_s": 1,
        "background_workers": "off",
        "embedding_provider": "fake",
        "embedding_model": "fake-ngram-3072",
        "embedding_dim": 3072,
        "llm_provider": "fake",
        "llm_model": "fake-echo-1",
        "require_real_providers": False,
        "boot_migrate": False,
        "auth_required": True,
        "identity_provider": "gateway",
        "self_signup": "off",
        "mcp_auth_mode": "required",
    }
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)
    env = {
        f"LOOM_{key.upper()}": str(value).lower() if isinstance(value, bool) else str(value)
        for key, value in values.items()
    }
    # API processes get the cell credential, never directory/operator authority.
    env.update(LOOM_ROUTER_OPERATOR_TOKEN="", LOOM_ROUTER_OPERATOR_SHA256="", LOOM_ROUTER_PG_DSN="")
    return env


async def _identities(client: httpx.AsyncClient, bases: list[str]) -> list[dict[str, Any]]:
    identities = []
    for base in bases:
        response = await client.get(f"{base}/v1/health")
        assert response.status_code == 200, response.text
        health = response.json()
        host, pid = health["instance"].rsplit(":", 1)
        assert host == socket.gethostname()
        text = await asyncio.to_thread(Path(f"/proc/{int(pid)}/stat").read_text)
        stat = text.rsplit(")", 1)[1].split()
        assert int(stat[1]) == os.getpid(), "the API must be this test's child"
        assert stat[0] not in {"Z", "X"}
        identities.append(
            {
                "pid": int(pid),
                "start": stat[19],
                "participant": health["participant_id"],
                "instance": health["deployment_config"]["instance_id"],
                "label": health["instance_label"],
            }
        )
    assert len({i["pid"] for i in identities}) == 2
    assert len({i["participant"] for i in identities}) == 2
    assert len({i["instance"] for i in identities}) == 2
    assert {i["label"] for i in identities} == {"attach-two-a", "attach-two-b"}
    return identities


async def _live(ch: Any, identities: list[dict[str, Any]], mode: str) -> None:
    """Match unfiltered live and cell rows to the exact HTTP and kernel witnesses."""
    expected = {i["instance"] for i in identities}
    async with asyncio.timeout(_WAIT_S):
        while True:
            rows = await instances.read_live(ch, all_deployments=True)
            # KeeperMap PRIMARY KEY instance_id: one current row, no merge ordering.
            cells = await ch.query_all(f"SELECT * FROM {instances.CELL_TABLE}")
            by_id = {r["instance_id"]: r for r in cells}
            if {r["instance_id"] for r in rows} == expected and all(
                i["instance"] in by_id
                and by_id[i["instance"]]["tenancy_mode"] == mode
                and (
                    mode != "enforced"
                    or next(r for r in rows if r["instance_id"] == i["instance"])["serving"]
                )
                for i in identities
            ):
                break
            await asyncio.sleep(0.2)
    processes = await ch.query_all(
        f"SELECT instance_id, pid FROM {instances.TABLE} "
        "WHERE instance_id IN {ids:Array(String)}",
        {"ids": sorted(expected)},
    )
    pids = {r["instance_id"]: int(r["pid"]) for r in processes}
    boot = await asyncio.to_thread(Path("/proc/sys/kernel/random/boot_id").read_text)
    participants = await provider_participants.participants(ch=ch)
    by_participant = {str(r["participant_id"]): r for r in participants}
    for identity in identities:
        assert pids[identity["instance"]] == identity["pid"]
        raw = by_id[identity["instance"]]
        fingerprint = json.loads(raw["fingerprint"])
        assert raw["cell"] == "first"
        assert fingerprint["participant_id"] == identity["participant"]
        participant = by_participant[identity["participant"]]
        assert participant["state"] == "running"
        proof = participant["host_proof"]
        assert provider_process.has_certificate(proof)
        parts = proof.split("|")
        assert parts[0] == socket.gethostname()
        assert parts[1] == boot.strip()
        assert parts[3] == str(os.stat("/proc/self/ns/pid").st_ino)
        assert (parts[4], parts[5]) == (str(identity["pid"]), identity["start"])
        assert not provider_process.proves_stopped(proof)
        if mode == "enforced":
            assert fingerprint["router"] is True
            assert fingerprint["marked"] is True
            assert next(r for r in rows if r["instance_id"] == identity["instance"])["serving"]
        identity["proof"] = proof


async def _retired(ch: Any, identities: list[dict[str, Any]]) -> None:
    """A helper's TERM/KILL/reap alone does not establish guarded retirement."""
    rows = await ch.query_all(
        f"SELECT participant_id, state, host_proof FROM {provider_participants.TABLE} FINAL "
        "WHERE participant_id IN {ids:Array(UUID)}",
        {"ids": [UUID(i["participant"]) for i in identities]},
    )
    by_id = {str(row["participant_id"]): row for row in rows}
    for identity in identities:
        assert provider_process.proves_stopped(identity["proof"])
        row = by_id[identity["participant"]]
        assert row["state"] == "retired", "stopping or killing is not retirement proof"
        assert row["host_proof"] == identity["proof"]
        assert provider_process.has_certificate(row["host_proof"])


async def _mint(env: dict[str, str], org: str) -> str:
    # A fresh native command reads this cell's keyring, not pytest's signing cache.
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "server.cli",
            "mint-token",
            "--subject",
            "attach-two",
            "--org",
            org,
            "--namespaces",
            "n",
            "--roles",
            "writer",
            "--ttl-days",
            "1",
            "--json",
        ],
        cwd=ROOT,
        env={**os.environ, **env, "LOOM_CELL_ROUTER_TOKEN": ""},
        capture_output=True,
        text=True,
        timeout=_WAIT_S,
        check=False,
    )
    assert result.returncode == 0, redact(result.stderr)
    token = json.loads(result.stdout)["token"]
    assert isinstance(token, str) and token
    return token


async def _requests(client: httpx.AsyncClient, bases: list[str], org: str, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    key = {"org": org, "namespace": "n", "agent": "attach-test", "memory_key": "shared"}
    for index, base in enumerate(bases):
        unauthenticated = await client.post(f"{base}/v1/memory.get", json=key)
        assert unauthenticated.status_code == 401, unauthenticated.text
        value = {"writer": index}
        written = await client.post(
            f"{base}/v1/memory.set",
            headers=headers,
            json={**key, "memory_type": "semantic", "value": value},
        )
        assert written.status_code == 200, written.text
        for reader in bases:
            read = await client.post(f"{reader}/v1/memory.get", headers=headers, json=key)
            assert read.status_code == 200, read.text
            assert read.json()["value"] == value


@pytest.mark.asyncio
async def test_attach_with_two_live_api_replicas(
    cell_stores: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stores = cell_stores
    api_a, api_b, probe, router_port = (free_port() for _ in range(4))
    host = f"127.0.0.1:{router_port}"
    env = _configure(monkeypatch, host, api_a, probe)
    ca, certificate, key = _tls_files(tmp_path)
    env["SSL_CERT_FILE"] = str(ca)
    trust = ssl.create_default_context(cafile=str(ca))
    router = httpx.URL(f"https://{host}")
    org = unique_org("attach-two")
    identities: list[dict[str, Any]] = []
    with query_tag("test.cell_attach_two_replicas"):
        async with _router(router_port, certificate, key):
            try:
                async with (
                    _api("attach-two-a", env, api_a) as a,
                    _api("attach-two-b", env, api_b) as b,
                    httpx.AsyncClient(verify=trust, timeout=30) as client,
                ):
                    identities = await _identities(client, [a, b])
                    stores["ids"].extend(i["instance"] for i in identities)
                    await _live(stores["ch"], identities, "open")
                    args = argparse.Namespace(
                        cell="first",
                        host=host,
                        credential_sha256=hashlib.sha256(_CREDENTIAL.encode()).hexdigest(),
                        probe_url="",
                        upstream_url=None,
                        wait=20,
                        router=str(router),
                    )
                    assert (
                        await cell_prepare.prepare(
                            stores["ch"], args, router, httpx.AsyncHTTPTransport(verify=trust)
                        )
                        == 0
                    )
                    await _live(stores["ch"], identities, "recording")
                    await pg.execute("INSERT INTO loom.tenancy (org) VALUES ($1)", org)
                    await stores["ch"].insert(
                        "loom.namespaces", [{"org": org, "namespace": "n", "owner_email": ""}]
                    )
                    capsys.readouterr()
                    assert (
                        await cell_attach.attach(
                            stores["ch"],
                            _args(router=str(router), wait=_WAIT_S),
                            router,
                            httpx.AsyncHTTPTransport(verify=trust),
                        )
                        == 0
                    )
                    assert json.loads(capsys.readouterr().out)["state"] == "serving"
                    assert await _mode() == "enforced"
                    row = await _row()
                    assert (row.state, row.mode) == ("serving", "enforced")
                    assert org in await _placed()
                    async with pg.dedicated() as conn:
                        assert await cell_triggers.missing(conn) == []
                    await _live(stores["ch"], identities, "enforced")
                    token = await _mint(env, org)
                    await _requests(client, [a, b, str(router)], org, token)
                    assert await _identities(client, [a, b]) == [
                        {k: v for k, v in identity.items() if k != "proof"}
                        for identity in identities
                    ]
            finally:
                first = sys.exception()
                if identities:
                    try:
                        await _retired(stores["ch"], identities)
                    except (
                        BaseException
                    ) as error:  # Retirement failure cannot hide the test failure.
                        if first is None:
                            raise
                        first.add_note(
                            f"retirement proof also failed: {type(error).__name__}: {error}"
                        )
