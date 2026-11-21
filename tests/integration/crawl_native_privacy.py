"""Native crawl publication and HTTP ACL isolation on an exclusively owned backend.

Run this module explicitly in the crawl-native-privacy workflow. The full app and
inline workers share one process; ordinary integration jobs use an external server.
Verified SSO contacts are trusted fixture inputs, not enrollment coverage.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, unquote

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.integration._eventually import eventually
from tests.integration._resources import unique_org


# This process exercises normal boot instead of the integration suite's in-process
# shortcuts for tests that do not start an app.
@pytest.fixture(autouse=True)
def _seed_test_api_key() -> Iterator[None]:
    yield


@pytest.fixture(autouse=True)
def _code_registry_read() -> Iterator[None]:
    yield


@pytest.fixture(autouse=True)
def _quick_intent_marks() -> Iterator[None]:
    yield


@pytest.fixture
async def provider(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    from server.clients import gdrive
    from server.clients import google_workspace as google

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    canary = uuid.uuid4().hex
    content = f"# Quarterly renewal plan\n\nScope owners and release readiness. {canary}\n"
    directory = "directory@example.test"
    reader = "reader-a@example.test"
    outside_reader = "reader-c@example.test"
    file_id = "native-privacy-document"
    metadata = {
        "id": file_id,
        "version": "1",
        "name": "Quarterly renewal plan",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "webViewLink": f"https://docs.google.com/document/d/{file_id}",
        "permissions": [
            {"type": "user", "role": "reader", "emailAddress": address}
            for address in (reader, outside_reader)
        ],
        "owners": [{"me": True, "emailAddress": reader}],
    }
    scopes: set[str] = set()
    exported: list[str] = []
    unexpected: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            assert request.method == "POST" and request.url.path == "/token"
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
            claims = jwt.decode(
                form["assertion"][0],
                private.public_key(),
                algorithms=["RS256"],
                audience="https://oauth2.googleapis.com/token",
            )
            assert claims["iss"] == "service@example.test"
            scope = claims["scope"]
            scopes.add(scope)
            if scope in {s.value for s in gdrive.WriteScope}:
                assert claims["sub"] == directory
                return httpx.Response(400, json={"error": "unauthorized_client"})
            assert scope in {s.value for s in gdrive.ReadScope}
            assert claims["sub"] in {directory, reader}
            drive = claims["sub"] == reader
            assert not drive or scope == gdrive.ReadScope.DRIVE.value
            return httpx.Response(
                200,
                json={
                    "access_token": "native-drive" if drive else "native-directory",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": scope,
                },
            )
        assert request.method == "GET"
        if request.url.host == "admin.googleapis.com":
            assert request.headers["authorization"] == "Bearer native-directory"
            assert request.url.path.startswith("/admin/directory/v1/users/")
            key = unquote(request.url.path.rsplit("/", 1)[1])
            assert key in {directory, reader, "provider-person"}
            is_directory = key == directory
            return httpx.Response(
                200,
                json={
                    "id": "provider-directory" if is_directory else "provider-person",
                    "primaryEmail": directory if is_directory else reader,
                    "aliases": [],
                    "customerId": "native-workspace",
                    "suspended": False,
                    "archived": False,
                },
            )
        assert request.url.host == "www.googleapis.com", request.url.host
        assert request.headers["authorization"] == "Bearer native-drive"
        if request.url.path == "/drive/v3/files":
            return httpx.Response(200, json={"files": [metadata], "incompleteSearch": False})
        if request.url.path == f"/drive/v3/files/{file_id}":
            return httpx.Response(200, json=metadata)
        assert request.url.path == f"/drive/v3/files/{file_id}/export", request.url.path
        exported.append(file_id)
        return httpx.Response(200, text=content)

    async def dispatch(request: httpx.Request) -> httpx.Response:
        try:
            return await handle(request)
        except AssertionError:
            unexpected.append(f"{request.method} {request.url.host}{request.url.path}")
            raise

    async with httpx.AsyncClient(transport=httpx.MockTransport(dispatch)) as vendor:
        monkeypatch.setattr(google, "get_client", lambda: vendor)
        monkeypatch.setattr(gdrive, "get_client", lambda: vendor)
        yield {
            "canary": canary,
            "content": content,
            "reader": reader,
            "outside_reader": outside_reader,
            "directory": directory,
            "scopes": scopes,
            "exported": exported,
            "unexpected": unexpected,
            "credential": json.dumps(
                {
                    "type": "service_account",
                    "client_email": "service@example.test",
                    "private_key": pem,
                }
            ),
        }
        assert not unexpected, unexpected


@pytest.fixture
async def native_app(provider: dict[str, Any]) -> AsyncIterator[httpx.AsyncClient]:
    from server.main import app

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:

            async def ready() -> bool:
                assert not provider["unexpected"], provider["unexpected"]
                result = await client.get("/v1/health/ready")
                assert result.status_code in {200, 503}, result.text
                return result.status_code == 200

            await eventually(ready, max_wait=60, description="normal app readiness")
            yield client


def _bearer(subject: str, org: str, namespaces: list[str], role: str) -> str:
    from server.auth.jwt import mint_token, verify_token
    from server.auth.reader_identity import reader_subject

    bearer = mint_token(
        subject,
        org,
        namespaces,
        [role],
        is_service_account=False,
        session_id=uuid.uuid4().hex,
        ttl_seconds=1800,
    )
    verified = verify_token(bearer)
    assert verified.subject == subject
    assert reader_subject(verified) == subject
    return bearer


async def _post(
    client: httpx.AsyncClient, bearer: str, op: str, body: dict[str, Any]
) -> httpx.Response:
    return await client.post(f"/v1/{op}", json=body, headers={"Authorization": f"Bearer {bearer}"})


@pytest.fixture
async def identities(native_app: httpx.AsyncClient, provider: dict[str, Any]) -> dict[str, Any]:
    from server.auth.reader_identity import resolve_reader
    from server.core.ch import get_client, set_query_tag

    set_query_tag("test.crawl_native_privacy.identities")
    ch = get_client()
    org, other_org = unique_org("crawl-native"), unique_org("crawl-other")
    # Account login handles are distinct from gateway grant principals (user:*).
    subjects = {
        "admin": "registrant",
        "a": "reader-a",
        "b": "reader-b",
        "c": "reader-c",
    }
    contacts = {
        "admin": "registrant@example.test",
        "a": provider["reader"],
        "b": "reader-b@example.test",
        "c": provider["outside_reader"],
    }
    # These actual rows represent already-verified upstream SSO contact evidence.
    rows = [
        {
            "org": org,
            "namespace": f"identity-{name}",
            "owner_email": subjects[name],
            "contact_email": contact,
            "password_hash": "",
            "active": True,
        }
        for name, contact in contacts.items()
    ]
    rows.append(
        {
            "org": other_org,
            "namespace": "identity-a",
            "owner_email": subjects["a"],
            "contact_email": contacts["a"],
            "password_hash": "",
            "active": True,
        }
    )
    await ch.insert("loom.namespaces", rows)
    for row in rows:
        identity = await resolve_reader(row["org"], row["owner_email"])
        assert identity is not None and identity.subject == row["owner_email"]
        assert identity.emails == frozenset({row["contact_email"]})
    return {"org": org, "other_org": other_org, "subjects": subjects, "contacts": contacts}


@pytest.fixture
async def registered_crawl(
    native_app: httpx.AsyncClient, identities: dict[str, Any], provider: dict[str, Any]
) -> None:
    from server.core.ch import set_query_tag

    set_query_tag("test.crawl_native_privacy.registration")
    org, subjects = identities["org"], identities["subjects"]
    admin = _bearer(subjects["admin"], org, ["*"], "org_admin")
    for namespace in ("col-crawl-documents", "col-other-documents"):
        response = await _post(
            native_app, admin, "collection.create", {"org": org, "namespace": namespace}
        )
        assert response.status_code == 200, response.text
    for who, namespace in (
        ("a", "col-crawl-documents"),
        ("b", "col-crawl-documents"),
        ("c", "col-other-documents"),
    ):
        response = await _post(
            native_app,
            admin,
            "grants.create",
            {"org": org, "namespace": namespace, "subject": subjects[who], "role": "viewer"},
        )
        assert response.status_code == 200, response.text
    response = await _post(
        native_app,
        admin,
        "knowledge.source.add",
        {
            "org": org,
            "namespace": "col-crawl-documents",
            "name": "private-documents",
            "kind": "gdrive",
            "credential_mode": "crawl",
            "credential": provider["credential"],
            "items": [{"type": "filter", "kinds": ["docs"]}],
            "include": ["docs"],
            "crawl_options": {
                "directory_subject": provider["directory"],
                "subjects": [{"type": "person", "email": provider["reader"]}],
            },
        },
    )
    assert response.status_code == 200, response.text
    response = await _post(
        native_app,
        admin,
        "knowledge.source.sync_now",
        {"org": org, "namespace": "col-crawl-documents", "name": "private-documents"},
    )
    assert response.status_code == 200, response.text


@pytest.fixture
async def published_crawl(
    registered_crawl: None, identities: dict[str, Any], provider: dict[str, Any]
) -> None:
    from server.clients import gdrive
    from server.core.ch import get_client, set_query_tag
    from server.knowledge.domain.sources import SYNC_WORKER_ACTOR

    set_query_tag("test.crawl_native_privacy.publication")
    ch, org = get_client(), identities["org"]

    async def published() -> dict[str, Any] | None:
        assert not provider["unexpected"], provider["unexpected"]
        return await ch.query_one(
            "SELECT namespace, content, access_json, actor FROM loom.documents FINAL "
            "WHERE org = {org:String} AND actor = {actor:String} LIMIT 1",
            params={"org": org, "actor": SYNC_WORKER_ACTOR},
        )

    document = await eventually(published, max_wait=180, description="native crawl publication")
    assert provider["canary"] in document["content"]
    assert document["actor"] == SYNC_WORKER_ACTOR and document["namespace"].startswith("acl-")
    acl = json.loads(document["access_json"])
    assert provider["reader"] in acl["emails"]
    assert identities["contacts"]["b"] not in acl["emails"]
    assert provider["outside_reader"] in acl["emails"]
    assert provider["exported"]
    assert provider["scopes"] == {s.value for s in (*gdrive.ReadScope, *gdrive.WriteScope)}
    source = await ch.query_one(
        "SELECT toString(source_id) AS source_id, crawl_generation "
        "FROM loom.knowledge_sources FINAL WHERE org = {org:String} "
        "AND namespace = {ns:String} AND name = {name:String} LIMIT 1",
        params={"org": org, "ns": "col-crawl-documents", "name": "private-documents"},
    )
    assert source is not None and source["crawl_generation"]

    async def complete_listing() -> dict[str, Any] | None:
        assert not provider["unexpected"], provider["unexpected"]
        return await ch.query_one(
            "SELECT crawl_generation, crawl_exclusion_generation "
            "FROM loom.knowledge_source_listings FINAL WHERE org = {org:String} "
            "AND namespace = {ns:String} AND source_id = {id:String} LIMIT 1",
            params={"org": org, "ns": document["namespace"], "id": source["source_id"]},
        )

    listing = await eventually(complete_listing, max_wait=30, description="complete native listing")
    assert listing["crawl_generation"] == source["crawl_generation"]
    assert listing["crawl_exclusion_generation"]


async def test_native_crawl_canary_obeys_document_acl_and_tenant_scope(
    native_app: httpx.AsyncClient,
    identities: dict[str, Any],
    provider: dict[str, Any],
    published_crawl: None,
) -> None:
    from server.core.ch import set_query_tag

    set_query_tag("test.crawl_native_privacy.reads")
    org, subjects = identities["org"], identities["subjects"]
    query = {
        "org": org,
        "namespace": "col-crawl-documents",
        "query": "quarterly renewal plan scope owners release readiness",
    }
    allowed = _bearer(subjects["a"], org, ["col-crawl-documents"], "viewer")

    async def admitted() -> bool:
        assert not provider["unexpected"], provider["unexpected"]
        result = await _post(native_app, allowed, "knowledge.search", query)
        assert result.status_code == 200, result.text
        return provider["canary"] in result.text

    assert await eventually(admitted, max_wait=30, description="authorized HTTP canary")
    denied = _bearer(subjects["b"], org, ["col-crawl-documents"], "viewer")
    response = await _post(native_app, denied, "knowledge.search", query)
    assert response.status_code == 200, response.text
    assert provider["canary"] not in response.text
    # C has a document ACL entry but only another namespace's grant. A's other-org
    # identity also has a matching contact, so the document ACL alone cannot deny it.
    for bearer in (
        _bearer(subjects["c"], org, ["col-other-documents"], "viewer"),
        _bearer(subjects["a"], identities["other_org"], ["*"], "viewer"),
    ):
        response = await _post(native_app, bearer, "knowledge.search", query)
        assert response.status_code in {200, 403, 404}, response.text
        assert provider["canary"] not in response.text
