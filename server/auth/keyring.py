"""Token signing keys: the root secret, and the keys in `loom.signing_keys`.

`LOOM_JWT_SECRET` is the one secret an operator carries. It signs and verifies
as kid `default`, and it seals the private half of every other signing key (as
it seals every stored credential, `server/core/secrets_at_rest.py`), so a Loom
that reaches this ClickHouse with the same root secret verifies every token any
other Loom of the deployment minted.

Every process loads the keyring at boot and every
`jwt_keyring_refresh_seconds`, and records that interval in
`loom.keyring_readers`. `rotate` writes a new key whose `activate_at` lies
at least two of the slowest process's intervals ahead, so each process
verifies the key before any token carries it. The newest active key signs.
`retire` stops a key verifying, `default` included (an RS256-only deployment
retires it); a gateway's key of the same kid (`jwt_public_keys_json`) is the
gateway operator's and verifies until it leaves that map, unless it is a key
this deployment retired.

The `default` row records an HMAC of the root secret: a process whose
`LOOM_JWT_SECRET` does not match it holds another root, so the keyring
commands and `loom mint-token` refuse to run there and `/v1/health` and
`loom doctor` say so. The workers leader (or a keyring command) writes it.

A PEM RSA `LOOM_JWT_SECRET` seals, and signs and verifies RS256 as kid
`default` unless `default` is retired. A gateway key of kid `default`
(`jwt_public_keys_json`) verifies beside it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import hashlib
import hmac
import logging
import math
import os
import secrets
import socket
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from server.core import readiness
from server.core.errors import BackendUnavailableError, LoomServerError
from server.core.repo_base import _CHClient
from server.core.secrets_at_rest import seal, unseal
from server.settings import settings

log = logging.getLogger(__name__)

ROOT_KID = "default"
ALGORITHMS = ("HS256", "RS256")
_PURPOSE = "signing_key"
_COLUMNS = (
    "kid",
    "algorithm",
    "sealed_key",
    "public_pem",
    "created_at",
    "activate_at",
    "retired_at",
)


@dataclass(frozen=True)
class SigningKey:
    kid: str
    algorithm: str
    # HS256: the shared secret. RS256: the private key PEM.
    secret: str
    public_pem: str
    activate_at: datetime


class NoSigningKey(LoomServerError):
    """Kid `default` is retired (or the root secret signs neither HS256 nor
    RS256) and no keyring key is active here: nothing can be signed until an
    operator acts, so it carries no Retry-After."""

    code = "NO_SIGNING_KEY"
    status_code = 503


_OTHER_ROOT = (
    "LOOM_JWT_SECRET here is not the servers' root secret (the 'default' row of "
    "loom.signing_keys does not match it); run with theirs. If the root was "
    "replaced on purpose, `loom signing-key record-root` records this one"
)


@dataclass(frozen=True)
class _View:
    """The keyring as one read gave it, opened with this root secret."""

    keys: dict[str, SigningKey] = dataclasses.field(default_factory=dict)
    retired: frozenset[str] = frozenset()
    # Kids whose key does not unseal with this root secret.
    unsealable: frozenset[str] = frozenset()
    # The `default` row: "match" (sealed under this root secret), "other"
    # (another root's), "absent", or None before a read.
    root_row: str | None = None
    # The ClickHouse server's clock minus this host's: `activate_at` is on the
    # server's clock.
    skew: timedelta = timedelta(0)
    # time.monotonic() of the read that gave this view; None = never read.
    read_at: float | None = None
    # Tokens may be judged: a read landed, or an offline mint signs as `default`.
    # Until then a retired root or a rotated kid is not known.
    loaded: bool = False
    # The RS256 public keys of retired rows (`_key_id`).
    retired_public: frozenset[bytes] = frozenset()
    # A digest of the root secret this view was opened with.
    opened_with: str = ""


_view = _View()
_warned: set[str] = set()


def loaded() -> bool:
    return _view.loaded


def snapshot_age_seconds() -> float:
    """Seconds since this process last read the keyring; inf before the first
    read. Readiness fails past the revocation snapshot's staleness bound."""
    return math.inf if _view.read_at is None else time.monotonic() - _view.read_at


def stale() -> bool:
    """Read, but longer ago than `jwt_revocation_max_stale_seconds`: a retire
    since may be unseen, so nothing signs or verifies until a read lands. An
    offline mint that never read is not stale."""
    if _view.read_at is None:
        return False
    return snapshot_age_seconds() > float(settings.jwt_revocation_max_stale_seconds)


def readiness_check() -> readiness.CheckResult:
    """Loaded, fresh, and recorded under this process's root secret: with
    another root, tokens the other processes sign are refused here."""
    from server.auth.revocation import token_state_ready

    if _view.root_row == "other" and settings.auth_actually_required():
        return readiness.CheckResult(False, _OTHER_ROOT)
    return token_state_ready(loaded(), snapshot_age_seconds(), "signing keyring")


def sign_with_root_only() -> None:
    """A process that cannot read the keyring but must sign (an offline CLI
    mint, a test client): sign as kid `default`."""
    global _view
    _view = dataclasses.replace(_view, loaded=True)


def load_for_offline_mint(*, run: Callable[..., Any] | None = None) -> bool:
    """For a process that mints without serving (a CLI, a dev or bench
    script): read the keyring once, or sign as kid `default` when ClickHouse
    cannot be read. `run` is the CLI's `run_connected`; without it the read
    uses a client of its own. True when it was read. Raises ValueError when
    the keyring was recorded under another root secret: no server would
    accept what this process signs."""

    async def _read() -> bool:
        from server.core.ch import CHClient

        ch = CHClient()
        await ch.connect()
        try:
            return await refresh(ch=ch)
        finally:
            await ch.disconnect()

    try:
        if run is not None:
            read = bool(run(lambda ch: refresh(ch=ch)))
        else:
            # A thread of its own, so a caller already inside an event loop works too.
            with ThreadPoolExecutor(1) as pool:
                read = pool.submit(asyncio.run, _read()).result()
    except Exception:  # noqa: BLE001 — an offline mint signs with the root secret
        read = False
    if not read:
        print("  · signing keyring not read; signing as kid 'default'", file=sys.stderr)
        sign_with_root_only()
    elif _view.root_row == "other":
        raise ValueError(_OTHER_ROOT)
    return read


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def server_now(view: _View | None = None) -> datetime:
    """The ClickHouse server's clock, as of the read that gave `view` (by
    default this process's last keyring read)."""
    return _now() + (_view if view is None else view).skew


def _root() -> SigningKey:
    return SigningKey(ROOT_KID, "HS256", str(settings.jwt_secret), "", datetime.min)


@functools.lru_cache(maxsize=4)
def _hmac_usable(secret: str) -> bool:
    """False for a PEM or SSH key, which PyJWT refuses as an HMAC secret."""
    import jwt
    from jwt.algorithms import HMACAlgorithm

    try:
        HMACAlgorithm(HMACAlgorithm.SHA256).prepare_key(secret)
    except jwt.InvalidKeyError:
        return False
    return True


def root_is_hmac_key() -> bool:
    """Whether LOOM_JWT_SECRET can sign HS256; a PEM root cannot."""
    return _hmac_usable(str(settings.jwt_secret))


@functools.lru_cache(maxsize=4)
def _pem_root(secret: str) -> SigningKey | None:
    """A PEM RSA LOOM_JWT_SECRET as the RS256 key of kid `default`; None for
    any other secret."""
    if "PRIVATE KEY" not in secret:
        return None
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    try:
        private = serialization.load_pem_private_key(secret.encode(), password=None)
    except (ValueError, TypeError):
        return None
    if not isinstance(private, rsa.RSAPrivateKey):
        return None
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return SigningKey(ROOT_KID, "RS256", secret, public, datetime.min)


# The minimum root key sizes of RFC 7518 sections 3.2 and 3.3: an HMAC secret as long as
# the hash, an RSA modulus of 2048 bits. Only the length is checked, not the randomness.
MIN_HMAC_ROOT_BYTES = 32
MIN_RSA_ROOT_BITS = 2048


def root_is_weak() -> bool:
    """Whether LOOM_JWT_SECRET is below the minimum key size: an HMAC secret under 32
    bytes or an RSA key under 2048 bits."""
    secret = str(settings.jwt_secret)
    if root_is_hmac_key():
        return len(secret.encode()) < MIN_HMAC_ROOT_BYTES
    key = _pem_root(secret)
    if key is None:
        return False
    from cryptography.hazmat.primitives import serialization

    public = serialization.load_pem_public_key(key.public_pem.encode())
    return getattr(public, "key_size", 0) < MIN_RSA_ROOT_BITS


def _keys(view: _View) -> dict[str, SigningKey]:
    """The view's keys, plus a PEM root as `default` unless `default` is
    retired."""
    if ROOT_KID in view.retired:
        return view.keys
    key = _pem_root(str(settings.jwt_secret))
    return view.keys if key is None else {**view.keys, ROOT_KID: key}


def root_algorithm() -> str:
    """What LOOM_JWT_SECRET signs as kid `default`: HS256, RS256 (a PEM RSA key) or ''."""
    if root_is_hmac_key():
        return "HS256"
    return "RS256" if _pem_root(str(settings.jwt_secret)) else ""


def _root_signs(retired: frozenset[str]) -> bool:
    return ROOT_KID not in retired and root_is_hmac_key()


def _signer(view: _View) -> SigningKey | None:
    now = server_now(view)
    active = [k for k in _keys(view).values() if k.activate_at <= now]
    if active:
        return max(active, key=lambda k: (k.activate_at, k.kid))
    return _root() if _root_signs(view.retired) else None


def _require_fresh() -> None:
    """Nothing is signed from a keyring never read, or read too long ago: a
    retire since may be unseen."""
    if not _view.loaded:
        raise BackendUnavailableError("the signing keyring is not loaded yet; retry shortly")
    if stale():
        raise BackendUnavailableError("the signing keyring is stale; retry shortly")


def signing_key() -> SigningKey:
    """The newest active key, else the root secret."""
    _require_fresh()
    key = _signer(_view)
    if key is None:
        if ROOT_KID in _view.retired:
            raise NoSigningKey("kid 'default' is retired and no other signing key is active here")
        raise NoSigningKey(
            "LOOM_JWT_SECRET is neither an HMAC secret nor an RSA private key, and no "
            "signing key is active here: run `loom signing-key rotate`"
        )
    return key


def rs256_signing_key() -> SigningKey | None:
    """The newest active RS256 key, for a token a party outside this deployment
    verifies against the JWKS (`/v1/.well-known/jwks.json`); None when every
    active key is HS256, whose secret no one else may hold."""
    _require_fresh()
    rsa = _active_rs256(_view, server_now(_view))
    return max(rsa, key=lambda k: (k.activate_at, k.kid)) if rsa else None


def _active_rs256(view: _View, now: datetime) -> list[SigningKey]:
    return [k for k in _keys(view).values() if k.algorithm == "RS256" and k.activate_at <= now]


def status(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """This process's view of the keyring, for `/v1/health`: the kid that
    signs ('' for none), the kids that do not unseal, and the `default` row's
    state. With `rows` (a `READ_SQL` result), the view they give with this
    root secret, for `loom doctor`; this process's own is left alone."""
    view = _view if rows is None else _parse(rows)
    loaded = view.loaded or rows is not None
    signer = _signer(view) if loaded else None
    return {
        "loaded": loaded,
        "signing_kid": signer.kid if signer else "",
        "unsealable": sorted(view.unsealable),
        "root_row": view.root_row,
    }


def verification_keys(kid: str, algorithm: str) -> list[str]:
    """The keys that may verify a token of `kid` signed with `algorithm`, in
    order: a keyring key or the root secret, then a gateway's public key of
    that kid (`jwt_public_keys_json`). An RS256 token of kid `default` (the PEM
    root's, or a gateway's without a kid) tries the PEM root and the gateway's
    `default` both. A retire stops this deployment's keys only: the
    gateway's are its operator's."""
    keys = _keys(_view)  # never a retired kid
    key = keys.get(kid)
    if key is None and kid == ROOT_KID and algorithm == "HS256":
        return [_root().secret] if _root_signs(_view.retired) else []
    out: list[str] = []
    if key is not None and key.algorithm == algorithm:
        out.append(key.public_pem if algorithm == "RS256" else key.secret)
    gateway = settings.jwt_public_key_map().get(kid) if algorithm == "RS256" else None
    if gateway and not _retired_key(gateway) and _key_id(gateway) not in map(_key_id, out):
        out.append(gateway)
    return out


@functools.lru_cache(maxsize=256)
def _key_id(public_pem: str) -> bytes:
    """The key a public PEM holds, as its SubjectPublicKeyInfo DER, whatever its
    layout (line endings, PKCS#1); the text itself when it does not parse."""
    from cryptography.hazmat.primitives import serialization

    try:
        key = serialization.load_pem_public_key(public_pem.strip().encode())
    except (ValueError, TypeError):
        return public_pem.encode()
    return key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _retired_key(public_pem: str) -> bool:
    """A key this deployment retired (a row's, or the PEM root's with `default`),
    under whatever kid the gateway map lists it: the map does not bring it back."""
    key = _key_id(public_pem)
    if key in _view.retired_public:
        return True
    pem = _pem_root(str(settings.jwt_secret))
    return ROOT_KID in _view.retired and pem is not None and key == _key_id(pem.public_pem)


def public_keys() -> dict[str, str]:
    """kid → public PEM of this deployment's RS256 keys, for the JWKS."""
    return {k.kid: k.public_pem for k in _keys(_view).values() if k.algorithm == "RS256"}


READ_SQL = (
    "SELECT kid, algorithm, sealed_key, public_pem, activate_at, "
    "retired_at IS NOT NULL AS retired, now64(3) AS server_now "
    "FROM loom.signing_keys FINAL"
)


async def refresh(*, ch: _CHClient | None = None) -> bool:
    """Re-read the keyring; False when the read failed (the last one is kept)."""
    return await _read(ch=ch) is not None


async def _read(*, ch: _CHClient | None = None) -> _View | None:
    """Re-read the keyring and install the view; None when the read failed."""
    from server.core.ch import get_client, sequential_reads

    global _view
    try:
        # Sequentially consistent: a rotation or retirement another process
        # acknowledged is in the read, whichever replica answers.
        with sequential_reads():
            rows = await (ch or get_client()).query_all(READ_SQL)
    except Exception as e:  # noqa: BLE001 — keep the last keyring on a CH glitch
        log.warning("signing keyring read failed: %s — keeping %d keys", e, len(_view.keys))
        return None
    view = _keep_retired(_parse(rows), _view)
    view = dataclasses.replace(view, read_at=time.monotonic(), loaded=True)
    for kid in sorted(view.unsealable - _warned):
        _warned.add(kid)
        log.warning("signing key %s does not unseal with this root secret; skipped", kid)
    if view.root_row == "other" and "root" not in _warned:
        _warned.add("root")
        log.error(
            "LOOM_JWT_SECRET is not the root secret loom.signing_keys was recorded under: "
            "keys the other processes sign with do not open here"
        )
    _view = view
    return view


def _keep_retired(view: _View, seen: _View) -> _View:
    """`view` with every retirement `seen` held: a retired kid never signs or
    verifies again, so a read that misses its retirement cannot undo it."""
    retired = view.retired | seen.retired
    if retired == view.retired and seen.retired_public <= view.retired_public:
        return view
    return dataclasses.replace(
        view,
        keys={kid: k for kid, k in view.keys.items() if kid not in retired},
        retired=retired,
        retired_public=view.retired_public | seen.retired_public,
    )


def _parse(rows: list[dict[str, Any]]) -> _View:
    """The keyring `rows` hold, opened with this root secret. Rows that do not
    say what time it is keep the last read's skew."""
    local_now = _now()
    keys: dict[str, SigningKey] = {}
    unsealable: set[str] = set()
    root_row = "absent"
    for r in rows:
        kid = str(r["kid"])
        if kid == ROOT_KID:
            # Kid `default` is always the root secret; its row only records it.
            if r["sealed_key"]:
                root_row = "match" if _is_this_root(str(r["sealed_key"])) else "other"
            continue
        if r["retired"]:
            continue
        secret = unseal(_PURPOSE, str(r["sealed_key"]), warn=False)
        if not secret and kid in _view.keys and _view.opened_with == _opener():
            # Resealed under a root this process does not hold yet (a rotation
            # under way): the key it already opened still verifies, until this
            # process is replaced.
            keys[kid] = _view.keys[kid]
            continue
        if not secret:
            unsealable.add(kid)
            continue
        keys[kid] = SigningKey(
            kid, str(r["algorithm"]), secret, str(r["public_pem"]), r["activate_at"]
        )
    at = rows[0].get("server_now") if rows else None
    skew = at.replace(tzinfo=None) - local_now if isinstance(at, datetime) else _view.skew
    return _View(
        keys,
        frozenset(str(r["kid"]) for r in rows if r["retired"]),
        frozenset(unsealable),
        root_row,
        skew,
        retired_public=frozenset(
            _key_id(str(r["public_pem"])) for r in rows if r["retired"] and r["public_pem"]
        ),
        opened_with=_opener(),
    )


def _opener() -> str:
    return hashlib.sha256(str(settings.jwt_secret).encode()).hexdigest()


_ROOT_LABEL = b"loom.signing_keys.root"
_ROOT_PREFIX = "hmac-sha256:"


def _root_record(secret: str | None = None) -> str:
    """What the `default` row stores: an HMAC of a fixed label under the root
    secret (or `secret`), which says whether a process holds it and reveals
    nothing else."""
    root = str(settings.jwt_secret) if secret is None else secret
    mac = hmac.new(root.encode(), _ROOT_LABEL, hashlib.sha256)
    return _ROOT_PREFIX + mac.hexdigest()


def records_root(recorded: str, secret: str) -> bool:
    """Whether the `default` row's `recorded` value is `secret`'s."""
    return hmac.compare_digest(recorded, _root_record(secret))


def _is_this_root(recorded: str) -> bool:
    return hmac.compare_digest(recorded, _root_record())


async def record_root(*, ch: _CHClient, force: bool = False) -> None:
    """Write the `default` row for this root secret, when it has none (or
    always, with `force`: the operator replaced the root on purpose). A
    retired `default` stays retired: the row is written one version above the
    one read, so a retire written meanwhile stays the newer row."""
    row = await ch.query_one(
        f"SELECT {', '.join(_COLUMNS)}, version FROM loom.signing_keys FINAL "
        "WHERE kid = {kid:String}",
        params={"kid": ROOT_KID},
    )
    if row is not None and row["sealed_key"] and not force:
        return
    version = int(row["version"]) + 1 if row else 1
    row = row or _root_row_values()
    await ch.insert(
        "loom.signing_keys",
        [{**row, "sealed_key": _root_record(), "version": version}],
        settings={"async_insert": 0},
    )


def _root_row_values() -> dict[str, Any]:
    return {
        "kid": ROOT_KID,
        "algorithm": "HS256",
        "sealed_key": "",
        "public_pem": "",
        "created_at": _now(),
        "activate_at": datetime(1970, 1, 1),
        "retired_at": None,
    }


async def _same_root(ch: _CHClient) -> _View:
    """Refuse a keyring change unless this process read the keyring and holds
    the root secret it was recorded under: a key sealed with another root does
    not open on the servers, and which keys sign here is not what they see.
    Returns the view read: the change judges it, not `_view`, which the
    refresh loop may replace meanwhile."""
    view = await _read(ch=ch)
    if view is None:
        raise ValueError("the keyring could not be read; nothing was changed")
    if view.root_row == "other":
        raise ValueError(_OTHER_ROOT)
    if view.root_row == "absent" and not settings.jwt_secret_is_dev_default():
        await record_root(ch=ch)
    return view


# A failed root record is retried this long after (a read-only user would
# otherwise log a warning every refresh).
_RECORD_RETRY_S = 600.0
_record_retry_at = 0.0


def _is_leader() -> bool:
    """This process holds the deployment's workers lease."""
    from server.workers import leader

    return leader.leads_deployment()


async def _record_root_if_absent() -> None:
    """The workers leader records the root on a keyring without it; any
    other process would be trusted on first use, so a mistyped secret on
    one replica's first boot would be recorded as the deployment's."""
    from server.core.ch import get_client

    global _view, _record_retry_at
    if _view.root_row != "absent" or settings.jwt_secret_is_dev_default() or not _is_leader():
        return
    if time.monotonic() < _record_retry_at:
        return
    try:
        await record_root(ch=get_client())
    except Exception as e:  # noqa: BLE001 — the record is advisory; retried later
        _record_retry_at = time.monotonic() + _RECORD_RETRY_S
        log.warning("could not record the root secret in loom.signing_keys: %s", e)
        return
    _view = dataclasses.replace(_view, root_row="match")


READERS_TABLE = "loom.keyring_readers"
# How often a process re-records its refresh interval; rows older than twice
# this are not counted.
_ANNOUNCE_S = 3600.0
_announce_at = 0.0


async def _announce_refresh() -> None:
    """Record this process's refresh interval, hourly, for `rotate`'s floor."""
    from server.core.ch import get_client

    global _announce_at
    if not _view.loaded or time.monotonic() < _announce_at:
        return
    _announce_at = time.monotonic() + _ANNOUNCE_S
    row = {
        "process": f"{socket.gethostname()[:64]}:{os.getpid()}",
        "refresh_seconds": float(settings.jwt_keyring_refresh_seconds),
    }
    try:
        await get_client().insert(
            READERS_TABLE, [row], settings={"async_insert": 1, "wait_for_async_insert": 0}
        )
    except Exception as e:  # noqa: BLE001 — advisory (a read-only user); retried hourly
        log.info("could not record the keyring refresh interval: %s", e)


async def slowest_refresh_s(*, ch: _CHClient) -> float | None:
    """The longest refresh interval a process recorded within two announce
    periods; None when none did (no process serves yet, or a read-only user)."""
    row = await ch.query_one(
        f"SELECT max(refresh_seconds) AS s FROM {READERS_TABLE} "
        "WHERE seen_at > now64(3) - toIntervalSecond({age:UInt32})",
        params={"age": int(2 * _ANNOUNCE_S)},
    )
    # max() of no rows is 0, and every interval is at least 1.
    return float(row["s"]) if row and row["s"] else None


def start_refresh_loop() -> asyncio.Task[None]:
    async def _run() -> None:
        from server.core.ch import set_query_tag

        set_query_tag("auth.keyring_refresh")
        while True:
            await _record_root_if_absent()
            await _announce_refresh()
            # Until the first read lands, retry within seconds: tokens wait on it.
            period = float(settings.jwt_keyring_refresh_seconds) if _view.loaded else 2.0
            await asyncio.sleep(max(1.0, period))
            await refresh()

    return asyncio.get_running_loop().create_task(_run(), name="loom-keyring-refresh")


async def _ch_now(ch: _CHClient) -> datetime:
    """The ClickHouse server's clock, which `activate_at` and `retired_at` use."""
    row = await ch.query_one("SELECT now64(3) AS now")
    return row["now"] if row else server_now()


def _new_key(algorithm: str) -> tuple[str, str]:
    """(secret or private PEM, public PEM) for a fresh key."""
    if algorithm == "HS256":
        return secrets.token_urlsafe(48), ""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return pem, public


async def rotate(algorithm: str, *, activate_in_s: float, ch: _CHClient) -> SigningKey:
    """Write a new key that signs from `activate_in_s` from now on the
    ClickHouse server's clock: it must exceed two of the servers' keyring
    refresh intervals, so every process verifies the key before it signs."""
    if algorithm not in ALGORITHMS:
        raise ValueError(f"algorithm must be one of {ALGORITHMS}")
    await _same_root(ch)
    secret, public = _new_key(algorithm)
    now = await _ch_now(ch)
    key = SigningKey(
        f"k{now:%Y%m%d%H%M%S}-{secrets.token_hex(3)}",
        algorithm,
        secret,
        public,
        now + timedelta(seconds=activate_in_s),
    )
    await ch.insert(
        "loom.signing_keys",
        [_row(key, created_at=now, retired_at=None)],
        settings={"async_insert": 0},
    )
    return key


# The deployment-wide claim a retire holds from its check to its write.
RETIRE_CLAIM = "signing-key-retire"
_LAPSED = "the retire claim lapsed before its write; nothing was changed"
_LOST = (
    "the retire claim was lost while it ran; the retire may have been written: "
    "check `loom signing-key list`"
)


async def retire(kid: str, *, ch: _CHClient) -> bool:
    """Stop `kid` verifying. False when no such key exists. One retire runs at
    a time across the deployment: its claim is renewed while it runs (lost, the
    retire is cancelled) and checked again before its write, and it reads the
    keyring with sequential consistency, so two at once cannot each leave the
    other's key as the last signer."""
    from server.core import leases
    from server.core.ch import sequential_reads

    lease = await leases.take(RETIRE_CLAIM, ttl_s=60, ch=ch)
    if lease is None:
        raise ValueError("another `loom signing-key retire` is running; retry in a moment")
    try:
        async with lease.renewed():
            with sequential_reads():
                return await _retire(kid, ch=ch, held=lambda: lease.held)
    except leases.LeaseLost:
        raise ValueError(_LOST) from None


async def _retire(kid: str, *, ch: _CHClient, held: Callable[[], bool]) -> bool:
    view = await _same_root(ch)
    row = await ch.query_one(
        f"SELECT {', '.join(_COLUMNS)} FROM loom.signing_keys FINAL WHERE kid = {{kid:String}}",
        params={"kid": kid},
    )
    if row is None and kid != ROOT_KID:
        return False
    now = await _ch_now(ch)
    signers = {k.kid for k in _keys(view).values() if k.activate_at <= now}
    if _root_signs(view.retired):
        signers.add(ROOT_KID)
    if signers <= {kid}:
        raise ValueError(f"retiring {kid!r} would leave no key to sign with; rotate first")
    rsa = {k.kid for k in _active_rs256(view, now)}
    if settings.inference_token_audience and rsa == {kid}:
        # The inference gateway verifies only RS256: model calls would stop.
        raise ValueError(
            f"retiring {kid!r} would leave no RS256 key to sign the inference gateway's "
            "identity tokens (LOOM_INFERENCE_TOKEN_AUDIENCE is set here); rotate one with "
            "--algorithm RS256 first"
        )
    if not held():
        raise ValueError(_LAPSED)
    row = row or {**_root_row_values(), "sealed_key": _root_record()}
    await ch.insert("loom.signing_keys", [{**row, "retired_at": now}], settings={"async_insert": 0})
    return True


async def listing(*, ch: _CHClient) -> list[dict[str, Any]]:
    return await ch.query_all(
        "SELECT kid, algorithm, created_at, activate_at, retired_at "
        "FROM loom.signing_keys FINAL ORDER BY activate_at"
    )


def _row(key: SigningKey, *, created_at: datetime, retired_at: datetime | None) -> dict[str, Any]:
    return {
        "kid": key.kid,
        "algorithm": key.algorithm,
        "sealed_key": seal(_PURPOSE, key.secret),
        "public_pem": key.public_pem,
        "created_at": created_at,
        "activate_at": key.activate_at,
        "retired_at": retired_at,
    }


def _reset_for_tests(*, loaded: bool = True) -> None:
    global _view, _record_retry_at, _announce_at
    _view = _View(read_at=time.monotonic() if loaded else None, loaded=loaded)
    _record_retry_at = _announce_at = 0.0
    _warned.clear()
    _pem_root.cache_clear()
