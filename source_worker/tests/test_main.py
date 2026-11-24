import json
import ssl
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from loom_source_worker.main import (
    _CONFIG_BYTES,
    StartupError,
    _parse_settings,
    _read_vault_token,
    _read_verifier,
    _server_config,
    _vault_tls,
)
from starlette.applications import Starlette


@pytest.fixture
def config():
    return {
        "transport": "tls_proxy_unix",
        "worker_id": "83d061b4-2542-4db7-adc0-4b81b3280c08",
        "issuer": "deployment-source-grants",
        "vault": {"origin": "https://vault.example:8200", "mount": "worker-connections"},
        "read_limits": {
            "concurrent": 8,
            "concurrent_per_org": 2,
            "timeout_seconds": 20,
            "content_bytes": 100_000,
        },
        "profiles": [
            {
                "organizations": ["tenant"],
                "link_origins": ["https://docs.example"],
                "config": {"endpoint": "https://mcp.example/rpc"},
            }
        ],
    }


def test_resource_profile_and_transport_settings(config):
    settings = _parse_settings(json.dumps(config).encode())
    assert str(settings.worker_id) == config["worker_id"]
    assert settings.vault.origin == "https://vault.example:8200"
    assert settings.vault.timeout_seconds == 3
    assert settings.profiles[0].organizations == ("tenant",)
    assert settings.profiles[0].config.approved_resource_ids == ()
    with pytest.raises(FrozenInstanceError):
        settings.profiles[0].config.endpoint = "https://other.example/rpc"

    transport = _server_config(Starlette(), settings.read_limits)
    assert transport.uds == "/run/loom-source-worker/socket/worker.sock"
    assert transport.workers == 1
    assert transport.ws == "none"
    assert not transport.access_log
    assert transport.log_config is None
    assert not transport.proxy_headers


def test_mapped_read_profile_is_validated_at_startup(config):
    source = config["profiles"][0]["config"]
    source.update(
        bearer_audience="https://mcp.example",
        approved_tools=["read_document"],
        read={
            "name": "read_document",
            "argument_name": "id",
            "input_schema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
                "required": ["id", "text"],
                "additionalProperties": False,
            },
            "id_path": ["id"],
            "title_path": None,
            "uri_path": None,
        },
    )
    settings = _parse_settings(json.dumps(config).encode())
    assert settings.profiles[0].config.read.id_path == ("id",)
    assert len(settings.profiles[0].digest) == 64
    source["read"]["argument_nam"] = "id"
    with pytest.raises(StartupError, match="^invalid worker configuration$"):
        _parse_settings(json.dumps(config).encode())


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("transport",), "http"),
        (("worker_id",), "00000000-0000-0000-0000-000000000000"),
        (("issuer",), "issuer\n"),
        (("vault", "origin"), "http://vault.example"),
        (("vault", "origin"), "https://vault.example/path"),
        (("vault", "origin"), "https://vault.example?"),
        (("vault", "origin"), "https://user:password@vault.example"),
        (("vault", "origin"), "https://vault.example:0"),
        (("vault", "origin"), "https://vault.example:65536"),
        (("vault", "mount"), "connections/team"),
        (("vault", "token"), "unrecognized-secret"),
        (("vault", "timeout_seconds"), 11),
        (("vault", "timeout_seconds"), float("nan")),
        (("vault", "custom_ca"), "true"),
        (("read_limits", "concurrent"), "8"),
        (("read_limits", "concurrent_per_org"), 9),
        (("read_limits", "extra"), 1),
        (("profiles",), []),
        (("profiles", 0, "organizations"), ["tenant", "tenant"]),
        (("profiles", 0, "link_origins"), ["https://docs.example/path"]),
        (("profiles", 0, "config", "endpoint"), "http://mcp.example/rpc"),
        (("profiles", 0, "config", "bearer_audience"), "https://other.example"),
        (("profiles", 0, "config", "approved_resource_ids"), []),
        (("profiles", 0, "config", "approved_tools"), ["unmapped"]),
        (("profiles", 0, "config", "limits"), {"max_requests": 257}),
        (("profiles", 0, "config", "data_policy"), {"unknown": 1}),
    ],
)
def test_refuses_invalid_configuration_without_echo(config, path, value):
    target = config
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = value
    with pytest.raises(StartupError, match="^invalid worker configuration$"):
        _parse_settings(json.dumps(config).encode())


def test_transport_is_required_and_duplicate_profiles_are_refused(config):
    transport = config.pop("transport")
    with pytest.raises(StartupError, match="^invalid worker configuration$"):
        _parse_settings(json.dumps(config).encode())
    config["transport"] = transport
    config["profiles"].append(config["profiles"][0])
    with pytest.raises(StartupError, match="^invalid worker configuration$"):
        _parse_settings(json.dumps(config).encode())


@pytest.mark.parametrize(
    "raw", [b'{"vault":{"origin":"a","origin":"b"}}', b"{}\xff", b" " * (_CONFIG_BYTES + 1)]
)
def test_refuses_ambiguous_or_oversize_json(raw):
    with pytest.raises(StartupError, match="^invalid worker configuration$"):
        _parse_settings(raw)


def test_worker_loads_only_a_single_public_verifier(tmp_path: Path):
    path = tmp_path / "verifier.pem"
    signer = Ed25519PrivateKey.generate()
    public = signer.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    private = signer.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    path.write_bytes(public)
    assert (
        _read_verifier(path).public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo) == public
    )
    for value in (private, public + private, b"x" * 4097):
        path.write_bytes(value)
        with pytest.raises(StartupError, match="^invalid grant verifier file$"):
            _read_verifier(path)
        with pytest.raises(StartupError, match="^invalid Vault CA file$"):
            _vault_tls(path)


def test_custom_vault_roots_preserve_hostname_verification(tmp_path: Path):
    signer = Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "test CA")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(signer.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(signer, None)
    )
    path = tmp_path / "vault-ca.pem"
    path.write_bytes(certificate.public_bytes(Encoding.PEM))
    context = _vault_tls(path)
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.cert_store_stats()["x509_ca"] == 1


def test_vault_token_is_read_from_restricted_file_on_each_use(tmp_path: Path):
    mount = tmp_path / "vault-token"
    current = tmp_path / "current"
    rotated = tmp_path / "rotated"
    current.write_bytes(b"test-first\n")
    current.chmod(0o440)
    mount.symlink_to(current)
    assert _read_vault_token(mount).get_secret_value() == "test-first"
    rotated.write_bytes(b"test-rotated")
    rotated.chmod(0o400)
    rotated.replace(current)
    assert _read_vault_token(mount).get_secret_value() == "test-rotated"
    current.chmod(0o444)
    with pytest.raises(OSError, match="^invalid Vault token file$"):
        _read_vault_token(mount)


@pytest.mark.parametrize("raw", [b"", b"test token", b"test\nsecond", b"a" * 8193])
def test_invalid_token_file_does_not_echo_contents(tmp_path: Path, raw: bytes):
    path = tmp_path / "vault-token"
    path.write_bytes(raw)
    path.chmod(0o400)
    with pytest.raises(OSError, match="^invalid Vault token file$"):
        _read_vault_token(path)
