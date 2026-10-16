"""The one-time link that gives an SSO account a password once SSO is off:
it sets it once, only for the accounts it named, and never while SSO is on."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server.auth import oauth, sso_links
from server.identity.app import password_link
from tests.unit.oauth_store_fake import MemoryOAuthStore, oauth_app

_ACCOUNT = {
    "org": "acme",
    "namespace": "u-lee-1a2b3c4d",
    "contact_email": "lee@acme.com",
    "password_hash": "",
    "active": True,
    "updated_at": None,
}


@pytest.fixture
def world(monkeypatch) -> dict[str, Any]:
    from datetime import datetime

    monkeypatch.setattr(oauth, "store", MemoryOAuthStore())
    monkeypatch.setattr(oauth.settings, "oauth_login_provider", "password")
    written: dict[str, Any] = {"rows": [], "links": [], "accounts": [dict(_ACCOUNT)]}
    written["accounts"][0]["updated_at"] = datetime(2026, 1, 1)

    async def accounts(email: str, org: str = "") -> list[dict[str, Any]]:
        return [dict(a) for a in written["accounts"]] if email == "lee@acme.com" else []

    class _CH:
        async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
            written["rows"].extend(rows)

    async def link(email: str, linked: list[tuple[str, str]], by: str) -> None:
        written["links"].append((email, linked, by))

    async def emit(event_type: str, **kw: Any) -> str:
        written.setdefault("events", []).append((event_type, kw))
        return ""

    async def fetch_row(org: str, namespace: str) -> dict[str, Any] | None:
        found = [a for a in written["accounts"] if (a["org"], a["namespace"]) == (org, namespace)]
        return dict(found[0]) if found else None

    monkeypatch.setattr(password_link, "sso_accounts", accounts)
    monkeypatch.setattr(password_link.namespaces, "fetch_row", fetch_row)
    monkeypatch.setattr(password_link, "get_client", lambda: _CH())
    monkeypatch.setattr(sso_links, "link", link)
    monkeypatch.setattr(password_link.audit, "emit", emit)
    return written


def _code() -> str:
    accounts = asyncio.run(password_link.sso_accounts("lee@acme.com"))
    return asyncio.run(password_link.make_link("lee@acme.com", accounts, by="admin:ops"))


def _set(c: TestClient, code: str, password: str, repeat: str | None = None) -> Any:
    return c.post(
        "/oauth/set-password",
        data={"code": code, "password": password, "repeat": password if repeat is None else repeat},
    )


def test_a_link_sets_the_password_once_and_links_the_account(world) -> None:
    c, code = TestClient(oauth_app()), _code()
    assert 'name="code"' in c.get("/oauth/set-password", params={"code": code}).text
    r = _set(c, code, "correct horse")
    assert r.status_code == 200 and "lee@acme.com" in r.text
    [row] = world["rows"]
    assert row["namespace"] == "u-lee-1a2b3c4d" and row["password_hash"].startswith("$argon2")
    assert world["links"] == [("lee@acme.com", [("acme", "u-lee-1a2b3c4d")], "password-link")]
    made, used = world["events"]
    assert (made[0], made[1]["actor"]) == ("auth.password_link_made", "admin:ops")
    assert (used[0], used[1]["subject"]["link_by"]) == ("auth.password_set", "admin:ops")
    assert _set(c, code, "another pass").status_code == 400  # spent
    assert len(world["rows"]) == 1


def test_a_short_or_mistyped_password_keeps_the_link(world) -> None:
    c, code = TestClient(oauth_app()), _code()
    assert _set(c, code, "short").status_code == 400
    assert _set(c, code, "correct horse", "correct hors").status_code == 400
    assert not world["rows"]
    assert _set(c, code, "correct horse").status_code == 200


def test_no_link_works_while_sso_is_on(world, monkeypatch) -> None:
    c, code = TestClient(oauth_app()), _code()
    monkeypatch.setattr(oauth.settings, "oauth_login_provider", "oidc")
    assert c.get("/oauth/set-password", params={"code": code}).status_code == 400
    assert _set(c, code, "correct horse").status_code == 400 and not world["rows"]


def test_an_account_that_changed_since_the_link_is_left_alone(world) -> None:
    c, code = TestClient(oauth_app()), _code()
    world["accounts"].clear()  # given a password, released or renamed meanwhile
    assert _set(c, code, "correct horse").status_code == 400 and not world["rows"]


def test_a_made_up_code_is_refused(world) -> None:
    r = _set(TestClient(oauth_app()), "made-up", "correct horse")
    assert r.status_code == 400 and not re.search("Password set", r.text)
