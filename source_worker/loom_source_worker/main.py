"""Start the companion behind a TLS proxy on a private Unix socket."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import socket
import ssl
import stat
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import fields
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
import uvicorn
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_public_key
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.applications import Starlette

from .credential_store import VaultCredentialStore
from .direct_backend import ApprovedProfile, DirectBackend
from .http_service import create_app
from .mcp_client import (
    MCPBearer,
    MCPDataPolicy,
    MCPLimits,
    MCPSourceClient,
    MCPSourceConfig,
    MCPSourceError,
    MCPToolMapping,
)
from .read_service import ReadLimits, ReadService

_CONFIG = Path("/etc/loom-source-worker/config.json")
_VERIFIER = Path("/etc/loom-source-worker/grant-verifier.pem")
_VAULT_CA = Path("/etc/loom-source-worker/vault-ca.pem")
_VAULT_TOKEN = Path("/run/loom-source-worker/credentials/vault-token")
_SOCKET = Path("/run/loom-source-worker/socket/worker.sock")
_CONFIG_BYTES = 2 * 1024 * 1024
_MAX_ADMITTED_WIRE_BYTES = 16 * 1024 * 1024
_TOKEN_BYTES = 8192
_BACKLOG = 64
_CERTIFICATE = rb"-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----"


class StartupError(Exception):
    """A startup refusal whose message contains no configuration values."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True, strict=True)


class _Vault(_Strict):
    origin: Annotated[str, Field(min_length=1, max_length=2048)]
    mount: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")]
    timeout_seconds: Annotated[float, Field(gt=0, le=10, allow_inf_nan=False)] = 3
    custom_ca: bool = False


class _Settings(_Strict):
    transport: Literal["tls_proxy_unix"]
    worker_id: UUID
    issuer: Annotated[str, Field(min_length=1, max_length=255)]
    vault: _Vault
    read_limits: ReadLimits
    profiles: Annotated[tuple[ApprovedProfile, ...], Field(min_length=1, max_length=4096)]


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate configuration field")
        result[name] = value
    return result


def _known_fields(value: Any, model: Any) -> None:
    if not isinstance(value, dict) or value.keys() - {field.name for field in fields(model)}:
        raise ValueError("unknown configuration field")


def _visible_ascii(value: str, maximum: int) -> bool:
    return 0 < len(value) <= maximum and all(33 <= ord(char) <= 126 for char in value)


def _https_origin(value: str) -> bool:
    if not _visible_ascii(value, 2048) or any(char in value for char in "%\\?#@"):
        return False
    url = httpx.URL(value)
    return (
        url.scheme == "https"
        and bool(url.host)
        and (url.port is None or 1 <= url.port <= 65_535)
        and url.raw_path == b"/"
        and str(url).removesuffix("/") == value
    )


async def _deny_validation_read() -> None:
    raise MCPSourceError("denied")


def _parse_settings(raw: bytes) -> _Settings:
    try:
        if not 0 < len(raw) <= _CONFIG_BYTES:
            raise ValueError("configuration size")
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        if not isinstance(document, dict):
            raise ValueError("configuration object")
        profiles = document.get("profiles")
        if not isinstance(profiles, list) or not 1 <= len(profiles) <= 4096:
            raise ValueError("profile count")
        _known_fields(document.get("read_limits"), ReadLimits)
        for profile in profiles:
            _known_fields(profile, ApprovedProfile)
            source = profile.get("config")
            _known_fields(source, MCPSourceConfig)
            if "approved_resource_ids" in source:
                raise ValueError("profile cannot select resources")
            for name, model in (("limits", MCPLimits), ("data_policy", MCPDataPolicy)):
                if name in source:
                    _known_fields(source[name], model)
            for name in ("read", "search"):
                if source.get(name) is not None:
                    _known_fields(source[name], MCPToolMapping)
        settings = _Settings.model_validate_json(json.dumps(document, allow_nan=False))
        if (
            settings.worker_id.int == 0
            or not _visible_ascii(settings.issuer, 255)
            or not _https_origin(settings.vault.origin)
        ):
            raise ValueError("invalid worker identity or Vault origin")
        digests = set()
        for profile in settings.profiles:
            if (
                not 1 <= len(profile.organizations) <= 256
                or len(set(profile.organizations)) != len(profile.organizations)
                or not all(_visible_ascii(org, 255) for org in profile.organizations)
                or len(profile.link_origins) > 32
                or len(set(profile.link_origins)) != len(profile.link_origins)
                or not all(_https_origin(origin) for origin in profile.link_origins)
            ):
                raise ValueError("invalid profile scope")
            if (
                settings.read_limits.concurrent * profile.config.limits.max_wire_bytes
                > _MAX_ADMITTED_WIRE_BYTES
            ):
                raise ValueError("worker wire budget exceeded")
            audience = profile.config.bearer_audience
            if audience is not None and not _visible_ascii(audience, 2048):
                raise ValueError("invalid profile audience")
            credential = MCPBearer("profile-validation", audience) if audience is not None else None
            client = MCPSourceClient(
                profile.config, credential, validate_access=_deny_validation_read
            )
            _ = client.config_sha256
            digest = profile.digest
            if digest in digests:
                raise ValueError("duplicate profile")
            digests.add(digest)
        return settings
    except (ValueError, TypeError, OverflowError, RecursionError, MCPSourceError, httpx.InvalidURL):
        raise StartupError("invalid worker configuration") from None


def _read_file(path: Path, maximum: int, *, secret: bool = False) -> bytes:
    with open(path, "rb", opener=_open_nonblocking) as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > maximum
            or (
                secret
                and (
                    info.st_uid not in {0, os.geteuid()}
                    or stat.S_IMODE(info.st_mode) not in {0o400, 0o440}
                )
            )
        ):
            raise OSError("invalid worker file")
        value = source.read(maximum + 1)
    if not 0 < len(value) <= maximum:
        raise OSError("invalid worker file size")
    return value


def _open_nonblocking(path: str, flags: int) -> int:
    return os.open(path, flags | os.O_NONBLOCK | os.O_CLOEXEC)


def _read_verifier(path: Path) -> Ed25519PublicKey:
    try:
        pem = _read_file(path, 4096).replace(b"\r\n", b"\n").removesuffix(b"\n")
        key = load_pem_public_key(pem)
        if not isinstance(key, Ed25519PublicKey) or pem != key.public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        ).removesuffix(b"\n"):
            raise ValueError("invalid verifier type or encoding")
        return key
    except (OSError, ValueError, UnsupportedAlgorithm):
        raise StartupError("invalid grant verifier file") from None


def _read_vault_token(path: Path) -> SecretStr:
    try:
        token = _read_file(path, _TOKEN_BYTES + 1, secret=True).removesuffix(b"\n").decode("ascii")
        if not _visible_ascii(token, _TOKEN_BYTES):
            raise ValueError("invalid token format")
        return SecretStr(token)
    except (OSError, ValueError):
        raise OSError("invalid Vault token file") from None


def _vault_tls(path: Path) -> ssl.SSLContext:
    try:
        pem = _read_file(path, 65_536)
        if not re.fullmatch(rb"\s*(?:" + _CERTIFICATE + rb"\s*)+", pem):
            raise ValueError("invalid CA encoding")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=pem.decode("ascii"))
        return context
    except (OSError, ValueError):
        raise StartupError("invalid Vault CA file") from None


def _application(settings: _Settings) -> Starlette:
    verifier = _read_verifier(_VERIFIER)
    token = partial(_read_vault_token, _VAULT_TOKEN)
    try:
        token()
    except OSError:
        raise StartupError("invalid Vault token file") from None
    store = VaultCredentialStore(
        origin=settings.vault.origin,
        mount=settings.vault.mount,
        worker_id=settings.worker_id,
        token=token,
        timeout_seconds=settings.vault.timeout_seconds,
        tls=_vault_tls(_VAULT_CA) if settings.vault.custom_ca else None,
    )
    service = ReadService(
        worker_id=settings.worker_id,
        issuer=settings.issuer,
        verifier=verifier,
        backend=DirectBackend(store, settings.profiles),
        limits=settings.read_limits,
    )
    app = create_app(service)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await store.aclose()

    app.router.lifespan_context = lifespan
    return app


@contextmanager
def _listen(path: Path) -> Iterator[socket.socket]:
    directory = os.open(path.parent, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise StartupError("worker socket directory must be worker-owned mode 0700")
        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            prior = path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(prior.st_mode) or prior.st_uid != os.geteuid():
                raise StartupError("worker socket path is occupied")
            path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            try:
                path.chmod(0o600)
                listener.listen(_BACKLOG)
                listener.setblocking(False)
                yield listener
            finally:
                path.unlink(missing_ok=True)
    finally:
        os.close(directory)


def _server_config(app: Starlette, limits: ReadLimits) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        uds=str(_SOCKET),
        workers=1,
        loop="asyncio",
        http="h11",
        ws="none",
        lifespan="on",
        access_log=False,
        log_config=None,
        proxy_headers=False,
        forwarded_allow_ips="",
        server_header=False,
        backlog=_BACKLOG,
        limit_concurrency=limits.concurrent + _BACKLOG,
        h11_max_incomplete_event_size=32_768,
        timeout_keep_alive=1,
        timeout_graceful_shutdown=30,
    )


def main() -> None:
    logging.disable(sys.maxsize)
    os.environ.pop("SSLKEYLOGFILE", None)
    os.umask(0o077)
    try:
        if len(sys.argv) != 1:
            raise StartupError("no arguments are accepted")
        try:
            raw = _read_file(_CONFIG, _CONFIG_BYTES)
        except OSError:
            raise StartupError("invalid worker configuration file") from None
        settings = _parse_settings(raw)
        app = _application(settings)
        with _listen(_SOCKET) as listener:
            server = uvicorn.Server(_server_config(app, settings.read_limits))
            server.run(sockets=[listener])
            if not server.started:
                raise StartupError("worker listener did not start")
    except StartupError as error:
        raise SystemExit(f"loom-source-worker: {error}") from None
    except Exception:  # noqa: BLE001 — dependency errors can expose configuration or request data
        raise SystemExit("loom-source-worker: worker startup or listener failed") from None


if __name__ == "__main__":
    main()
