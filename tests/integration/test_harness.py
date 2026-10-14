"""The integration harness itself: redaction, tenant ids and the purge guard.

These run without a server; a harness that leaks a token into a CI log or
purges a tenant it did not mint is a defect in every test that uses it.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from tests.integration._redact import MASK, redact
from tests.integration._resources import minted_orgs, purge, unique_org


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl",
        "GET /cb?code=4f2a9c&state=abc",
        'body {"password": "hunter2"}',
        "LOOM_JWT_SECRET=ci-only-jwt-secret",
        "refresh_token=r-123&x=1",
        "client_secret: zz-top",
    ],
)
def test_redact_masks_every_credential_shape(raw: str) -> None:
    out = redact(raw)
    assert MASK in out
    for secret in ("eyJ", "4f2a9c", "hunter2", "ci-only-jwt-secret", "r-123", "zz-top"):
        assert secret not in out, out


def test_redact_keeps_the_diagnostic_text() -> None:
    line = "POST http://h/v1/memory.get -> 500 rid=1cd5089e status code 500 4ms"
    assert redact(line) == line


def test_unique_org_is_fresh_registered_and_purgeable() -> None:
    a, b = unique_org("harness"), unique_org("harness")
    assert a != b
    assert re.fullmatch(r"it-harness-[0-9a-f]{8}", a)
    assert {a, b} <= minted_orgs()


def test_unique_org_rejects_a_label_outside_the_purgeable_shape() -> None:
    with pytest.raises(ValueError):
        unique_org("Has Spaces")


class _RecordingClient:
    """A ClickHouse stand-in: one org table, deletes recorded, rows gone after."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> Any:
        if "system.tables" in sql:
            rows: list[tuple[Any, ...]] = [("memory",)]
        elif "system.mutations" in sql:
            rows = []
        else:
            rows = [(0,)]
        return type("R", (), {"result_rows": rows})()

    def command(self, sql: str, **_: Any) -> None:
        self.commands.append(sql)


def test_purge_refuses_an_org_the_suite_did_not_mint() -> None:
    client = _RecordingClient()
    with pytest.raises(ValueError, match="did not mint"):
        purge(client, "loom", frozenset({"it-ok-0123abcd", "acme"}))
    assert client.commands == []


def test_purge_deletes_from_every_org_table_and_reports_no_residue() -> None:
    client = _RecordingClient()
    assert purge(client, "loom", frozenset({"it-ok-0123abcd"})) == {}
    assert client.commands == [
        "ALTER TABLE `loom`.`memory` DELETE WHERE org IN %(orgs)s",
        "ALTER TABLE `loom`.`revoked_tokens` "
        "DELETE WHERE arrayExists(p -> startsWith(jti, p), %(prefixes)s)",
    ]
