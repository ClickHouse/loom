"""Actual Directory HTTP and crawl identity rules, without a provider or database."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest

from server.auth import scim
from server.clients._vendor import CredentialRefused, TooLarge, VendorError
from server.knowledge.app import crawl_authority
from server.knowledge.app.crawl_expansion import Limits, expand
from server.knowledge.domain.source_crawl import Options, Person
from server.settings import settings
from tests.unit.test_google_workspace import _http
from tests.unit.test_google_workspace import account as account

pytestmark = pytest.mark.asyncio
_NATIVE_SCIM_READ = scim.deactivated


@pytest.fixture(autouse=True)
def scim_rows(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    ended: set[str] = set()

    class Rows:
        async def query_one(self, sql: str, *, params: dict[str, Any]):
            assert sql == (
                "SELECT 1 AS x FROM loom.scim_users FINAL WHERE id = {id:String} AND active = 0"
            )
            return {"x": 1} if params["id"] in ended else None

    rows = Rows()
    monkeypatch.setattr(scim, "deactivated", _NATIVE_SCIM_READ)
    monkeypatch.setattr(scim, "get_client", lambda: rows)
    return ended


class _Standing:
    current = True

    async def validate(self) -> None:
        if not self.current:
            raise PermissionError("source authority ended")


class _Exclusions:
    epoch = "first"

    def __init__(self) -> None:
        self.denied: set[str] = set()
        self.observed: list[Person] = []

    async def version(self) -> str:
        return self.epoch

    async def contains(self, person: Person) -> bool:
        self.observed.append(person)
        return person.id in self.denied


class _Directory:
    def __init__(self) -> None:
        self.members: dict[str, list[dict[str, Any]]] = {
            "team@example.com": [
                {"type": "USER", "id": "one", "email": "one@example.com"},
                {"type": "GROUP", "id": "nested", "email": "nested@example.com"},
            ],
            "nested": [{"type": "USER", "id": "two", "email": "two@example.com"}],
        }
        self.customer = "workspace"
        self.after_user: Any = lambda: None

    def answer(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            form = parse_qs(request.content.decode())
            claims = jwt.decode(form["assertion"][0], options={"verify_signature": False})
            assert claims["sub"] == "directory@example.com"
            return httpx.Response(
                200,
                json={"access_token": "private-token", "token_type": "Bearer", "expires_in": 60},
            )
        assert request.url.host == "admin.googleapis.com"
        assert request.headers["authorization"] == "Bearer private-token"
        if "/groups/" in request.url.path:
            key = request.url.path.split("/groups/", 1)[1].removesuffix("/members")
            return httpx.Response(200, json={"members": self.members[key]})
        key = request.url.path.rsplit("/", 1)[1]
        identifier = {"one@example.com": "one", "alias@example.com": "one"}.get(key, key)
        principal = key == "directory@example.com"
        self.after_user()
        return httpx.Response(
            200,
            json={
                "id": identifier,
                "primaryEmail": key if principal else f"{identifier}@example.com",
                "aliases": ["alias@example.com"] if identifier == "one" else [],
                "customerId": "workspace" if principal else self.customer,
                "suspended": False,
                "archived": False,
            },
        )


def _options(*subjects: tuple[str, str]) -> Options:
    return Options.parse(
        {
            "directory_subject": "directory@example.com",
            "subjects": [{"type": kind, "email": email} for kind, email in subjects],
        }
    )


async def _run(
    options: Options,
    account: Any,
    standing: _Standing,
    exclusions: _Exclusions,
    *,
    limits: Limits | None = None,
):
    return await expand(
        options,
        account[0],
        standing=standing,
        exclusions=exclusions,
        limits=limits or Limits(body_bytes=16384, member_pages=10, groups=10),
        deadline=time.monotonic() + 10,
    )


async def test_current_scim_deletion_excludes_a_person_and_every_group_entry(
    monkeypatch: pytest.MonkeyPatch, account: Any, scim_rows: set[str]
) -> None:
    directory = _Directory()
    scim_rows.add(scim.user_id("alias@example.com"))
    async with _http(monkeypatch, directory.answer):
        result = await _run(
            _options(("person", "one@example.com"), ("group", "team@example.com")),
            account,
            _Standing(),
            _Exclusions(),
        )
    assert [person.id for person in result.people] == ["two"]
    assert all("one@example.com" not in members for _, members in result.groups)


async def test_complete_nested_groups_and_explicit_alias_share_stable_provider_identity(
    monkeypatch: pytest.MonkeyPatch,
    account: Any,
) -> None:
    directory = _Directory()
    exclusions = _Exclusions()
    async with _http(monkeypatch, directory.answer) as calls:
        result = await _run(
            _options(("group", "team@example.com"), ("person", "alias@example.com")),
            account,
            _Standing(),
            exclusions,
        )
    assert [(p.customer, p.id, p.email) for p in result.people] == [
        ("workspace", "one", "one@example.com"),
        ("workspace", "two", "two@example.com"),
    ]
    assert dict(result.groups) == {
        "nested@example.com": ("two@example.com",),
        "team@example.com": ("one@example.com", "two@example.com"),
    }
    assert result.exclusion_version == "first"
    assert sum(r.url.path.endswith("/users/one") for r in calls) == 1
    assert not any(r.url.path.endswith("/users/alias@example.com") for r in calls)
    assert "example.com" not in repr(result) and "example.com" not in repr(result.people[0])


async def test_excluded_person_is_absent_from_direct_and_all_nested_membership(
    monkeypatch: pytest.MonkeyPatch,
    account: Any,
) -> None:
    directory, exclusions = _Directory(), _Exclusions()
    exclusions.denied.add("two")
    async with _http(monkeypatch, directory.answer):
        result = await _run(
            _options(("group", "team@example.com"), ("person", "two@example.com")),
            account,
            _Standing(),
            exclusions,
        )
    assert [p.id for p in result.people] == ["one"]
    assert dict(result.groups) == {
        "nested@example.com": (),
        "team@example.com": ("one@example.com",),
    }
    assert {p.id for p in exclusions.observed} == {"one", "two"}


@pytest.mark.parametrize("failure", ["cycle", "customer", "external", "group-bound"])
async def test_unproven_or_incomplete_expansion_never_returns_partial_readers(
    monkeypatch: pytest.MonkeyPatch,
    account: Any,
    failure: str,
) -> None:
    directory = _Directory()
    monkeypatch.setattr(
        settings, "knowledge_crawl_max_groups", 1 if failure == "group-bound" else 10
    )
    limits = crawl_authority.configured_bounds().directory
    if failure == "cycle":
        directory.members["nested"] = [{"type": "GROUP", "id": "team", "email": "team@example.com"}]
    elif failure == "customer":
        directory.members["nested"] = [{"type": "CUSTOMER", "id": "workspace"}]
    elif failure == "external":
        directory.customer = "foreign-workspace"
    async with _http(monkeypatch, directory.answer):
        with pytest.raises((VendorError, CredentialRefused, TooLarge)):
            await _run(
                _options(("group", "team@example.com")),
                account,
                _Standing(),
                _Exclusions(),
                limits=limits,
            )


@pytest.mark.parametrize("change", ["standing", "exclusion"])
async def test_loss_during_directory_response_stops_before_another_provider_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    account: Any,
    change: str,
) -> None:
    directory, standing, exclusions = _Directory(), _Standing(), _Exclusions()

    def changed() -> None:
        if change == "standing":
            standing.current = False
        else:
            exclusions.epoch = "second"

    directory.after_user = changed
    async with _http(monkeypatch, directory.answer) as calls:
        with pytest.raises((PermissionError, VendorError)):
            await _run(_options(("group", "team@example.com")), account, standing, exclusions)
    assert len(calls) == 2  # Token and identity response; no members or person request.


async def test_directory_page_budget_is_global_across_selected_groups(
    monkeypatch: pytest.MonkeyPatch,
    account: Any,
) -> None:
    directory = _Directory()
    directory.members["team@example.com"] = []
    directory.members["other@example.com"] = []
    monkeypatch.setattr(settings, "knowledge_crawl_directory_max_pages", 1)
    async with _http(monkeypatch, directory.answer) as calls:
        with pytest.raises(TooLarge, match="page bound"):
            await _run(
                _options(("group", "team@example.com"), ("group", "other@example.com")),
                account,
                _Standing(),
                _Exclusions(),
                limits=crawl_authority.configured_bounds().directory,
            )
    assert sum("/groups/" in r.url.path for r in calls) == 1


async def test_unavailable_exclusion_state_refuses_before_token_exchange(
    monkeypatch: pytest.MonkeyPatch,
    account: Any,
) -> None:
    exclusions = _Exclusions()
    exclusions.epoch = ""
    async with _http(monkeypatch, lambda r: pytest.fail("no HTTP admitted")) as calls:
        with pytest.raises(VendorError, match="exclusions unavailable"):
            await _run(_options(("person", "one@example.com")), account, _Standing(), exclusions)
    assert calls == []
