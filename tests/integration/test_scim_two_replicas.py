"""SCIM across replicas: a deactivation sent to one replica ends the person's
tokens on another within one revocation refresh, and a reactivation lets them
back on both.

Two Loom processes share one ClickHouse and one public issuer, as replicas
behind one URL do. Each holds its own revocation snapshot, refreshed every
`LOOM_JWT_REVOCATION_CACHE_TTL_SECONDS`.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from tests.integration._fake_idp import CLIENT_ID, CLIENT_SECRET, FakeIdP
from tests.integration._ports import free_port
from tests.integration._resources import unique_org
from tests.integration._spawn import loom_server
from tests.integration.test_scim_deprovisioning import (
    _JWT_SECRET,
    _SCIM,
    _SCIM_TOKEN,
    _authenticated,
    _bearer,
    _cleanup,
    _seed,
    _set_active,
    _sign_in,
    _token,
)

_REFRESH_S = 3.0


@pytest.fixture
def replicas(ch_client) -> Iterator[tuple[str, str, str, Any]]:
    org = unique_org("scim2")
    email = f"scim2-{uuid.uuid4().hex[:6]}@corp.example"
    ns = _seed(ch_client, org, email)
    idp = FakeIdP(email)
    port_a = free_port()
    a = f"http://127.0.0.1:{port_a}"
    env = {
        "LOOM_AUTH_REQUIRED": "true",
        "LOOM_JWT_SECRET": _JWT_SECRET,
        "LOOM_OAUTH_LOGIN_PROVIDER": "oidc",
        "LOOM_OAUTH_ISSUER": a,  # the one public URL both replicas answer for
        "LOOM_OIDC_ISSUER": idp.url,
        "LOOM_OIDC_CLIENT_ID": CLIENT_ID,
        "LOOM_OIDC_CLIENT_SECRET": CLIENT_SECRET,
        "LOOM_SCIM_TOKEN": _SCIM_TOKEN,
        "LOOM_JWT_REVOCATION_CACHE_TTL_SECONDS": str(_REFRESH_S),
    }
    try:
        with (
            idp.running(),
            loom_server("scim-a", env, port=port_a),
            loom_server("scim-b", env) as b,
        ):
            yield a, b, ns, email
    finally:
        _cleanup(ch_client, email)


def _seconds_until(check: Any, limit: float) -> float:
    start = time.monotonic()
    while not check():
        assert time.monotonic() - start < limit, f"still not true after {limit} s"
        time.sleep(0.1)
    return time.monotonic() - start


def test_a_deactivation_on_one_replica_ends_tokens_on_the_other(replicas) -> None:
    a, b, ns, email = replicas
    session = _token(_sign_in(a))
    made = httpx.post(
        f"{a}/v1/me/service_accounts.create",
        headers=_bearer(session),
        json={"name": "ci-bot", "namespaces": [ns], "role": "viewer"},
    )
    assert made.status_code == 200, made.text
    bot = made.json()["token"]
    assert _authenticated(b, session) and _authenticated(b, bot)

    created = httpx.post(f"{a}/scim/v2/Users", headers=_SCIM, json={"userName": email})
    assert created.status_code == 201, created.text
    uid = created.json()["id"]
    assert _set_active(a, uid, False)["active"] is False
    # The replica that served the request refuses at once; the other within a refresh.
    assert not _authenticated(a, session) and not _authenticated(a, bot)
    window = _seconds_until(
        lambda: not _authenticated(b, session) and not _authenticated(b, bot), 3 * _REFRESH_S
    )
    assert window <= _REFRESH_S + 1.5, window
    # B mints nothing for them either: its mint checks read ClickHouse.
    child = httpx.post(
        f"{b}/v1/me/service_accounts.create",
        headers=_bearer(bot),
        json={"name": "late-bot", "namespaces": [ns], "role": "viewer"},
    )
    assert child.status_code in (401, 403), child.text

    assert _set_active(a, uid, True)["active"] is True
    again = _token(_sign_in(a))
    assert _authenticated(a, again)
    # B may hold the deactivation until its next refresh: it fails closed, then lets them in.
    _seconds_until(lambda: _authenticated(b, again), 3 * _REFRESH_S)
    assert not _authenticated(b, session) and not _authenticated(b, bot)  # still dead
