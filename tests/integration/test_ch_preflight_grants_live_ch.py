"""The preflight's grant checks against a real server: a user given everything
on loom.* (as the admin SQL grants it) passes the migration check. From 26.9 the ALTER group holds the
global-only ALTER HANDLER, so `CHECK GRANT ALTER ON loom.*` fails for such a
user; the migration grants name ALTER TABLE and ALTER VIEW instead."""

from __future__ import annotations

import secrets
from typing import Any

import pytest

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


@pytest.mark.parametrize("need", ["migrate", "serve", "read"])
def test_a_user_with_everything_on_loom_passes(loom_scoped_user: Any, need: str) -> None:
    assert str(loom_scoped_user.command(f"CHECK GRANT {preflight.GRANTS[need]} ON loom.*")) == "1"
