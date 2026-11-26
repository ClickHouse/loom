"""Worker route provisioning consumes deployment-owned file references only."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from cryptography.x509.oid import NameOID

from server.core import pg
from server.knowledge.app.mcp_worker_route_config import load_worker_routes
from server.knowledge.app.mcp_worker_routes import open_worker_routes
from server.knowledge.app.mcp_worker_runtime import current_directory, source_worker_runtime
from server.settings import Settings

_ROUTE = {
    "worker_id": "d59962ea-a918-497a-a284-984c5ddc7dfd",
    "origin": "https://worker.example",
    "issuer": "loom",
    "trust_ca_file": "trust.pem",
    "client_certificate_file": "trust.pem",
    "client_key_file": "client.pem",
    "grant_key_file": "grant.pem",
    "profiles": {"org": [{"digest": "f" * 64, "modes": ["live"]}]},
}


def _write_private_key(path, key) -> None:
    path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))


@pytest.mark.asyncio
@pytest.mark.parametrize("pg_authority", [True, False])
async def test_provision_operator_pins_trust_and_loads_only_loom_keys(
    tmp_path, monkeypatch, pg_authority
) -> None:
    monkeypatch.setattr(pg, "enabled", lambda: pg_authority)
    client = Ed25519PrivateKey.generate()
    grant = Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(client.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(client, algorithm=None)
    )
    (tmp_path / "trust.pem").write_bytes(cert.public_bytes(Encoding.PEM))
    _write_private_key(tmp_path / "client.pem", client)
    _write_private_key(tmp_path / "grant.pem", grant)
    path = tmp_path / "routes.json"
    path.write_text(json.dumps({"version": 1, "workers": [_ROUTE]}))
    (route,) = load_worker_routes(path)
    assert route.tls.cert_store_stats()["x509_ca"] == 1
    assert route.grant_key.public_key().public_bytes_raw() == grant.public_key().public_bytes_raw()
    async with open_worker_routes((route,)) as routes:
        assert routes.permits(route.worker_id, "org", "f" * 64, "live", False)
    assert current_directory() is None
    monkeypatch.setenv("LOOM_KNOWLEDGE_SOURCE_WORKER_ROUTES_FILE", str(path))
    async with source_worker_runtime(Settings().knowledge_source_worker_routes_file):
        directory = current_directory()
        if pg_authority:
            assert directory is not None
            assert directory.worker(route.worker_id) is not None
        else:
            assert directory is None
    assert current_directory() is None


@pytest.mark.asyncio
async def test_runtime_without_configuration_leaves_directory_disabled() -> None:
    async with source_worker_runtime(""):
        assert current_directory() is None


def test_route_file_rejects_inline_upstream_credentials_without_echo(tmp_path) -> None:
    path = tmp_path / "routes.json"
    path.write_text(
        json.dumps({"version": 1, "workers": [{**_ROUTE, "upstream_bearer": "not-a-real-token"}]})
    )
    with pytest.raises(ValueError, match="^invalid source worker route configuration$") as error:
        load_worker_routes(path)
    assert error.value.__suppress_context__
