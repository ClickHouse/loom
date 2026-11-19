"""Two independently configured HTTPS cells and a real private router directory."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from server.auth import machines
from server.cli import cell_bot_proof
from server.cli import cell_each_http as wire
from tests.integration._pg_database import create_database, drop_database
from tests.integration._ports import free_port
from tests.integration._resources import ch_client_from_env, ch_target, unique_org
from tests.integration._spawn import ROOT, loom_server


def private_file(path: Path, value: str | bytes) -> Path:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value.encode() if isinstance(value, str) else value)
    return path


def tls_pair(home: Path, name: str) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return (
        private_file(home / f"{name}.crt", certificate.public_bytes(serialization.Encoding.PEM)),
        private_file(
            home / f"{name}.pem",
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        ),
    )


def child(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # stderr can include a DSN on a bootstrap failure, so never put it in an assertion.
    result = subprocess.run(
        [sys.executable, "-m", *command],
        cwd=ROOT,
        env={**os.environ, **env},
        text=True,
        capture_output=True,
        timeout=240,
    )
    if result.returncode:
        # Allow only migration names, error classes and numeric codes into CI output.
        # The streams may hold connection credentials, beyond generic log redaction.
        migrations = {path.name for path in (ROOT / "schemas" / "migrations").glob("*.sql")}
        stages = re.findall(r"(?m)^\s*[→✓✗] (\d{3}_[a-z0-9_]{1,160}\.sql)", result.stdout)
        kinds = re.findall(
            r"\b(DatabaseError|OperationalError|PreflightError|ValidationError|"
            r"ConnectionError|TimeoutError|KeeperRequired)\b",
            result.stderr,
        )
        codes = re.findall(r"\bCode: (\d{1,5})\b", result.stderr)
        detail = {
            "exit": result.returncode,
            "migration": [name for name in stages if name in migrations][-1:],
            "error_kinds": sorted(set(kinds)),
            "clickhouse_codes": sorted(set(codes))[:8],
        }
        raise AssertionError(f"Private fixture command {command[:1]} failed: {detail}")
    return result


async def _register(directory: str, node: dict[str, Any]) -> None:
    import asyncpg

    conn = await asyncpg.connect(directory)
    try:
        await conn.execute(
            "INSERT INTO loom_router.cells "
            "(cell, is_first, host, state, mode, credential_sha256, deployment_id, current_fp) "
            "VALUES ($1, $2, $3, 'serving', 'enforced', $4, $5, $6::jsonb)",
            node["cell"],
            node["cell"] == "alpha",
            node["origin"].removeprefix("https://"),
            hashlib.sha256(node["credential"].encode()).hexdigest(),
            node["deployment_id"],
            json.dumps(node["fingerprint"]),
        )
    finally:
        await conn.close()


def _cell_env(name: str, dsn: str, database: str, router: str, ca: Path) -> dict[str, str]:
    port = free_port()
    origin = f"https://127.0.0.1:{port}"
    return {
        "LOOM_ENV_FILE": "",
        "LOOM_CELL": name,
        "LOOM_PG_DSN": dsn,
        "LOOM_STORAGE_AUTHORITY": "postgres",
        "LOOM_CH_DATABASE": database,
        "LOOM_CH_TELEMETRY_DATABASE": database + "_telemetry",
        "LOOM_CH_READER_USER": "",
        "LOOM_CH_CLUSTER": "",
        "LOOM_PUBLIC_BASE_URL": origin,
        "LOOM_OAUTH_ISSUER": origin,
        "LOOM_OAUTH_ENABLED": "true",
        "LOOM_AUTH_REQUIRED": "true",
        "LOOM_MCP_AUTH_MODE": "required",
        "LOOM_JWT_SECRET": secrets.token_hex(32),
        "LOOM_JWT_ISSUER": origin,
        "LOOM_JWT_AUDIENCE": "loom-platform",
        "LOOM_JWT_TRUSTED_ISSUERS": "",
        "LOOM_JWT_END_USER_ISSUERS_JSON": "",
        "LOOM_OIDC_ISSUER": "",
        "LOOM_OIDC_CLIENT_ID": "",
        "LOOM_SCIM_TOKEN": "",
        "LOOM_SELF_SIGNUP": "off",
        "LOOM_CODE_ORG": "",
        "LOOM_TRACKER_ORG": "",
        "LOOM_DOCUMENT_S3_BUCKET": "",
        "LOOM_DOCUMENT_S3_PREFIX": "originals",
        "LOOM_BACKGROUND_WORKERS": "off",
        "LOOM_EMBEDDING_MODEL": "fake-ngram-1024",
        "LOOM_LLM_PROVIDER": "fake",
        "LOOM_CELL_ROUTER_URL": router,
        "LOOM_CELL_ROUTER_TOKEN": "lcr_" + secrets.token_hex(64),
        "LOOM_CELL_PROBE_PORT": str(free_port()),
        "LOOM_BOOT_MIGRATE": "true",
        "LOOM_TLS_CERT_FILE": "",
        "LOOM_TLS_KEY_FILE": "",
        "SSL_CERT_FILE": str(ca),
    }


def _keys(home: Path, name: str) -> dict[str, Any]:
    private, public = machines.generate_key()
    thumbprint = json.loads(machines.public_jwks(public))["keys"][0]["kid"]
    return {
        "name": name,
        "thumbprint": thumbprint,
        "key_file": private_file(home / f"{name}.pem", private),
        "public_key": public.decode(),
    }


def _bots(node: dict[str, Any], home: Path, ca: Path) -> None:
    env = node["env"]
    now = int(datetime.now(UTC).timestamp())
    token = jwt.encode(
        {
            "sub": "native-operator",
            "org": node["org"],
            "namespaces": ["*"],
            "roles": ["platform_admin"],
            "iss": env["LOOM_JWT_ISSUER"],
            "aud": env["LOOM_JWT_AUDIENCE"],
            "sa": False,
            "iat": now,
            "exp": now + 3600,
            "jti": str(uuid.uuid4()),
        },
        env["LOOM_JWT_SECRET"],
        algorithm="HS256",
        headers={"kid": "default"},
    )
    node["admin_token"] = token
    with httpx.Client(verify=ssl.create_default_context(cafile=str(ca)), trust_env=False) as client:
        for purpose in ("automation", "recovery"):
            bot = node[purpose] = _keys(home, f"{purpose}-{node['cell']}")
            answer = client.post(
                node["origin"] + "/v1/admin/bots.create",
                headers={"Authorization": "Bearer " + token},
                json={
                    "name": bot["name"],
                    "org": node["org"],
                    "namespace_roles": [],
                    "public_key_pem": bot["public_key"],
                    "platform_admin": True,
                },
                timeout=30,
            )
            assert answer.status_code == 200, "Normal platform-admin bot registration failed"


def bot_key(bot: dict[str, Any]) -> cell_bot_proof.BotKey:
    return cell_bot_proof.BotKey(bot["name"], bot["thumbprint"], bot["key_file"])


def _provision(node: dict[str, Any], router: str, operator: str, ca: Path) -> None:
    command = ["server.cli", "cell", "automation", "--cell", node["cell"], "--router", router]
    for purpose in ("automation", "recovery"):
        bot = node[purpose]
        command += [
            f"--{purpose}-bot",
            bot["name"],
            f"--{purpose}-thumbprint",
            bot["thumbprint"],
            f"--{purpose}-key",
            str(bot["key_file"]),
        ]
    answer = child(
        [*command, "--ca-file", str(ca)],
        {**node["env"], "LOOM_ROUTER_OPERATOR_TOKEN": operator},
    )
    public = json.loads(answer.stdout)
    assert public == {
        "version": 1,
        "cell": node["cell"],
        "deployment_id": node["deployment_id"],
        **{p: {k: node[p][k] for k in ("name", "thumbprint")} for p in ("automation", "recovery")},
    }


@contextmanager
def fleet(home: Path) -> Iterator[dict[str, Any]]:
    admin = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    ch_target()
    home.mkdir(mode=0o700)
    certificate = tls_pair(home, "trusted")
    operator = "lco_" + secrets.token_hex(64)
    router_port = free_port()
    router = f"https://127.0.0.1:{router_port}"
    with ExitStack() as stack:
        router_name, router_dsn = asyncio.run(create_database(admin))
        stack.callback(lambda: asyncio.run(drop_database(admin, router_name)))
        router_env = {
            "LOOM_ENV_FILE": "",
            "LOOM_ROUTER_PG_DSN": router_dsn,
            "LOOM_PG_DSN": "",
            "LOOM_TLS_CERT_FILE": "",
            "LOOM_TLS_KEY_FILE": "",
            "LOOM_ROUTER_OPERATOR_SHA256": hashlib.sha256(operator.encode()).hexdigest(),
        }
        child(["server.cli", "router", "migrate"], router_env)
        stack.enter_context(
            loom_server(
                "each-router", router_env, port=router_port, tls_files=certificate, router=True
            )
        )
        ch = ch_client_from_env(database="default")
        stack.callback(ch.close)
        nodes = []
        for name in ("alpha", "beta"):
            database = "each_" + uuid.uuid4().hex
            for db in (database, database + "_telemetry"):
                ch.command(f"CREATE DATABASE `{db}`")
                stack.callback(ch.command, f"DROP DATABASE `{db}` SYNC")
            pg_name, dsn = asyncio.run(create_database(admin))
            stack.callback(lambda n=pg_name: asyncio.run(drop_database(admin, n)))
            env = _cell_env(name, dsn, database, router, certificate[0])
            child(["server.core.schema.migrate"], env)
            result = home / f"{name}.json"
            org = unique_org("each-" + name)
            child(["tests.integration.fixtures.cell_each_seed", str(result), org], env)
            node = {
                "cell": name,
                "org": org,
                "env": env,
                "origin": env["LOOM_PUBLIC_BASE_URL"],
                "credential": env["LOOM_CELL_ROUTER_TOKEN"],
                **json.loads(result.read_text()),
            }
            asyncio.run(_register(router_dsn, node))
            node["process"] = stack.enter_context(ExitStack())
            node["process"].enter_context(
                loom_server(
                    "each-" + name,
                    env,
                    port=int(node["origin"].rsplit(":", 1)[1]),
                    tls_files=certificate,
                )
            )
            _bots(node, home, certificate[0])
            _provision(node, router, operator, certificate[0])
            nodes.append(node)
        token_file = private_file(home / "operator.token", operator)
        manifest = private_file(
            home / "credentials.json",
            json.dumps(
                {
                    "version": 1,
                    "router_token_file": str(token_file),
                    "ca_file": str(certificate[0]),
                    "keys": [
                        {
                            "cell": n["cell"],
                            "deployment_id": n["deployment_id"],
                            "key_file": str(n["automation"]["key_file"]),
                        }
                        for n in nodes
                    ],
                }
            ),
        )
        yield {
            "home": home,
            "nodes": nodes,
            "router": router,
            "operator": operator,
            "certificate": certificate,
            "manifest": manifest,
            "router_dsn": router_dsn,
        }


def target(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "cell": node["cell"],
        "deployment_id": node["deployment_id"],
        "origin": node["origin"],
        "automation": {k: node["automation"][k] for k in ("name", "thumbprint")},
        "recovery": {k: node["recovery"][k] for k in ("name", "thumbprint")},
    }


def admin_read(
    f: dict[str, Any], node: dict[str, Any], method: str, body: dict[str, Any]
) -> dict[str, Any]:
    with httpx.Client(
        verify=ssl.create_default_context(cafile=str(f["certificate"][0])), trust_env=False
    ) as client:
        answer = client.post(
            node["origin"] + "/v1/admin/" + method,
            headers={"Authorization": "Bearer " + node["admin_token"]},
            json=body,
            timeout=30,
        )
        assert answer.status_code == 200, "Ordinary owner readback failed"
        return answer.json()


async def access(f: dict[str, Any], node: dict[str, Any]) -> str:
    import time

    async with httpx.AsyncClient(
        verify=wire._tls({"ca_file": f["certificate"][0]}), trust_env=False
    ) as client:
        token, _end = await wire._token(
            client, target(node), node["automation"]["key_file"], time.monotonic() + 30
        )
        answer = await client.get(
            node["origin"] + "/v1/whoami", headers={"Authorization": "Bearer " + token}
        )
        assert answer.status_code == 200
        identity = answer.json()
        assert identity["subject"] == "sa:" + node["automation"]["name"]
        assert identity["is_service_account"] is True
        assert 0 < identity["exp"] - identity["iat"] <= 60
        return token


@contextmanager
def router_trust(f: dict[str, Any], node: dict[str, Any], fault: str) -> Iterator[None]:
    """Restart only this fixture's cell, retaining its real stores and public identity."""
    changes = (
        {"SSL_CERT_FILE": str(tls_pair(f["home"], "untrusted")[0])}
        if fault == "untrusted"
        else {"LOOM_CELL_ROUTER_URL": f["router"].replace("127.0.0.1", "localhost")}
    )
    node["process"].close()
    port = int(node["origin"].rsplit(":", 1)[1])
    try:
        with loom_server(
            "each-refusal", {**node["env"], **changes}, port=port, tls_files=f["certificate"]
        ):
            yield
    finally:
        node["process"].enter_context(
            loom_server("each-" + node["cell"], node["env"], port=port, tls_files=f["certificate"])
        )
