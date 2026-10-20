"""The preflight's grant checks against a real server: a user given everything
on loom.* (as the admin SQL grants it) passes every check, the server's
OPTIMIZE, TRUNCATE and SYSTEM REFRESH VIEW included. From 26.9 the ALTER group
holds the global-only ALTER HANDLER, so `CHECK GRANT ALTER ON loom.*` fails for
such a user; the server grants name ALTER TABLE and ALTER VIEW instead."""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from server.core.ch_names import quote, telemetry_database
from server.core.schema import preflight
from tests.integration._resources import ch_client_from_env


@pytest.fixture
def loom_scoped_user() -> Any:
    admin = ch_client_from_env()
    name = f"preflight_{secrets.token_hex(4)}"
    password = f"Pf-{secrets.token_hex(12)}"
    try:
        admin.command(f"CREATE USER {name} IDENTIFIED BY '{password}'")
    except Exception as e:  # noqa: BLE001 — a login without access management cannot run this
        pytest.skip(f"cannot create users here: {str(e)[:120]}")
    try:
        # What the admin SQL grants; Cloud refuses GRANT ALL.
        admin.command(f"GRANT CURRENT GRANTS ON loom.* TO {name}")
        user = ch_client_from_env(username=name, password=password, database="")
        yield user
        user.close()
    finally:
        admin.command(f"DROP USER IF EXISTS {name}")
        admin.close()


@pytest.mark.parametrize("grants", [*preflight.GRANTS.values(), preflight.REFRESH_GRANT])
def test_a_user_with_everything_on_loom_passes(loom_scoped_user: Any, grants: str) -> None:
    assert str(loom_scoped_user.command(f"CHECK GRANT {grants} ON loom.*")) == "1"


def test_the_telemetry_grants_need_their_own_grant_and_it_covers_them(
    loom_scoped_user: Any,
) -> None:
    check = f"CHECK GRANT {preflight.TELEMETRY_GRANTS} ON {quote(telemetry_database())}.*"
    # Everything on loom.* is not enough: the migration that creates the
    # database would fail halfway.
    assert str(loom_scoped_user.command(check)) == "0"
    admin = ch_client_from_env()
    try:
        name = str(loom_scoped_user.command("SELECT currentUser()"))
        admin.command(f"GRANT CURRENT GRANTS ON {quote(telemetry_database())}.* TO {name}")
    finally:
        admin.close()
    assert str(loom_scoped_user.command(check)) == "1"
