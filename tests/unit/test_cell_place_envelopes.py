"""Every accepted fingerprint fits both placement envelopes for every valid org."""

from __future__ import annotations

import hashlib
import itertools
import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from starlette.requests import Request

from server.core import cell
from server.router import app, directory
from server.router.limits import MAX_BODY_BYTES
from server.settings import settings

DEPLOYMENT = str(UUID(int=1))
TOKEN = "lco_" + "7" * 128


def fingerprint():
    return cell.as_value(
        {
            **cell.DEFAULTS,
            "deployment_id": DEPLOYMENT,
            "public_host": "first.example.test",
            "oauth_issuer_host": "first.example.test",
        }
    )


def envelopes(name, value, org=""):
    return (
        {"org": org, "cell": name, "fingerprint": value},
        {"org": org, "deployment_id": DEPLOYMENT, "fingerprint": value},
    )


def attaching(name):
    row = SimpleNamespace(
        cell=name,
        is_first=True,
        state="attaching",
        mode="recording",
        deployment_id=DEPLOYMENT,
        current_fp=None,
        next_fp=None,
        host="first.example.test",
    )
    return SimpleNamespace(row=row, other=None, attach_started_at=None, bucket="", bucket_prefix="")


def begin(value, position):
    return directory.Begin(
        DEPLOYMENT,
        value if position == "current_fp" else fingerprint(),
        value if position == "next_fp" else None,
        "a" * 64,
        {},
    )


@pytest.mark.parametrize("name", ["a", "a" * 32])
@pytest.mark.parametrize("position", ["current_fp", "next_fp"])
def test_begin_reserves_operator_envelope_before_accepting_fingerprint(name, position):
    value = fingerprint()
    live = envelopes(name, value)[0]
    # This fits the previous live-only reservation, but not the operator envelope.
    live_size = len(json.dumps(live)) + 6 * cell.ORG_MAX_BYTES
    value["fields"]["oidc_client_id"] = "x" * (MAX_BODY_BYTES - live_size)
    reason = directory.begin_refusal(attaching(name), begin(value, position))
    assert position in reason and str(MAX_BODY_BYTES) in reason
    assert "oidc_client_id" in reason


@pytest.mark.parametrize("name", ["a", "a" * 32])
@pytest.mark.parametrize("position", ["current_fp", "next_fp"])
@pytest.mark.parametrize("size", ["default", "boundary"])
@pytest.mark.parametrize("org", ["acme", "\x01" * cell.ORG_MAX_BYTES])
async def test_accepted_fingerprint_fits_both_encodings_and_operator_handler(
    monkeypatch, name, position, size, org
):
    value = fingerprint()
    if size == "boundary":
        empty_size = max(len(json.dumps(body)) for body in envelopes(name, value))
        value["fields"]["oidc_client_id"] = "x" * (
            MAX_BODY_BYTES - empty_size - 6 * cell.ORG_MAX_BYTES
        )
    assert cell.named_org(org) == org and not cell.org_name_refusal(org)
    attempt = begin(value, position)
    assert directory.begin_refusal(attaching(name), attempt) == ""
    bodies = envelopes(name, value, org)
    encoded = [
        (json.dumps(body).encode(), httpx.Request("POST", "http://inert.test", json=body).content)
        for body in bodies
    ]
    assert all(len(raw) <= MAX_BODY_BYTES for pair in encoded for raw in pair)
    calls = []

    async def place(actual_org, actual_name, refusal, *, actor, verify):
        calls.append((actual_org, actual_name, actor))
        locked = SimpleNamespace(
            deployment_id=DEPLOYMENT,
            current_fp=attempt.current_fp,
            next_fp=attempt.next_fp,
        )
        assert verify(locked) == ""
        return name, DEPLOYMENT

    monkeypatch.setattr(directory, "place", place)
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(TOKEN.encode()).hexdigest()
    )
    for raw in encoded[1]:
        scope = {
            "type": "http",
            "method": "POST",
            "path": f"/cells/v1/operator/cells/{name}/place",
            "path_params": {"cell": name},
            "headers": [
                (b"authorization", f"Bearer {TOKEN}".encode()),
                (b"content-length", str(len(raw)).encode()),
            ],
        }

        async def receive(raw=raw):
            return {"type": "http.request", "body": raw, "more_body": False}

        response = await app.Router().operator_place(Request(scope, receive))
        assert response.status_code == 200
        assert json.loads(response.body) == {"org": org, "cell": name, "deployment_id": DEPLOYMENT}
    assert calls == [(org, name, "operator")] * 2


OMITTABLE_DEFAULTS = (
    "bearer_issuers",
    "bucket",
    "bucket_prefix",
    "configured_orgs",
    "end_user_orgs",
    "oidc_issuer",
    "scim",
)


@pytest.fixture
def native_fingerprint(monkeypatch):
    configured = SimpleNamespace(
        public_base_url="https://first.example.test",
        oauth_issuer="",
        oidc_issuer="",
        oidc_client_id="",
        scim_token="",
        document_s3_bucket="",
        document_s3_prefix="",
        oidc_jit_org="",
        self_signup="closed",
        self_signup_org="",
        code_org="",
        tracker_org="",
        trusted_issuers=lambda: set(),
        end_user_issuers=lambda: {},
    )
    monkeypatch.setattr(cell, "settings", configured)
    monkeypatch.setattr(cell, "_code_orgs", None)

    def native(client_id):
        configured.oidc_client_id = client_id
        # Both native placement callers serialize this complete field set.
        return cell.as_value(cell.fingerprint(DEPLOYMENT))

    return native


def omitted_values():
    return (
        subset
        for count in range(len(OMITTABLE_DEFAULTS) + 1)
        for subset in itertools.combinations(OMITTABLE_DEFAULTS, count)
    )


def stored_fingerprint(native, omitted, unknown):
    return cell.as_value(
        {
            **{key: value for key, value in native["fields"].items() if key not in omitted},
            **unknown,
        }
    )


def accepted_row(name, attempt):
    return SimpleNamespace(
        cell=name,
        deployment_id=DEPLOYMENT,
        current_fp=attempt.current_fp,
        next_fp=attempt.next_fp,
        state="serving",
        is_first=True,
        mode="enforced",
        closed=False,
        holds=lambda token: True,
    )


async def route_response(monkeypatch, name, attempt, body, operator):
    calls = []
    row = accepted_row(name, attempt)

    async def place(org, actual_name, refusal, *, actor, verify=None):
        assert actual_name == name and refusal(row) == ""
        if verify is not None:
            assert verify(row) == ""
        calls.append((org, actor))
        return name, DEPLOYMENT

    monkeypatch.setattr(directory, "place", place)
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(TOKEN.encode()).hexdigest()
    )
    router = app.Router()

    async def authenticated(request, actual_name):
        assert actual_name == name
        return "inert-cell-credential"

    monkeypatch.setattr(router, "_authenticated", authenticated)
    monkeypatch.setattr(router, "placements", SimpleNamespace(admit=lambda name: True))
    raw = httpx.Request("POST", "http://inert.test", json=body).content
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/cells/v1/placements",
        "path_params": {"cell": name},
        "headers": [
            (b"authorization", f"Bearer {TOKEN}".encode()),
            (b"content-length", str(len(raw)).encode()),
        ],
    }

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    handler = router.operator_place if operator else router.place
    response = await handler(Request(scope, receive))
    return response, calls, len(raw)


@pytest.mark.parametrize("name", ["a", "a" * 32])
@pytest.mark.parametrize("position", ["current_fp", "next_fp"])
@pytest.mark.parametrize("unknown", [{}, {"future": "x"}, {"future": "x" * 512}])
def test_sparse_admission_reserves_matching_native_callers(
    native_fingerprint, name, position, unknown
):
    org = "\x01" * cell.ORG_MAX_BYTES
    for omitted in omitted_values():
        stored = stored_fingerprint(native_fingerprint(""), omitted, unknown)
        empty = max(len(json.dumps(body)) for body in envelopes(name, stored))
        client_id = "x" * (MAX_BODY_BYTES - empty - 6 * cell.ORG_MAX_BYTES)
        stored["fields"]["oidc_client_id"] = client_id
        native = native_fingerprint(client_id)
        attempt = begin(stored, position)
        row = accepted_row(name, attempt)
        assert cell.matches(native["fields"], stored)
        assert app.fingerprint_matches(row, native)
        reason = directory.begin_refusal(attaching(name), attempt)
        if not reason:
            # Admission must cover what these callers send, even if storage is sparse.
            assert all(
                len(httpx.Request("POST", "http://inert.test", json=body).content) <= MAX_BODY_BYTES
                for body in envelopes(name, native, org)
            ), (omitted, unknown)


@pytest.mark.parametrize("name", ["a", "a" * 32])
@pytest.mark.parametrize("position", ["current_fp", "next_fp"])
@pytest.mark.parametrize("operator", [False, True])
async def test_sparse_boundary_refuses_unusable_native_requests(
    monkeypatch, native_fingerprint, name, position, operator
):
    stored = stored_fingerprint(native_fingerprint(""), OMITTABLE_DEFAULTS, {})
    empty = max(len(json.dumps(body)) for body in envelopes(name, stored))
    stored["fields"]["oidc_client_id"] = "x" * (MAX_BODY_BYTES - empty - 6 * cell.ORG_MAX_BYTES)
    native = native_fingerprint(stored["fields"]["oidc_client_id"])
    attempt = begin(stored, position)
    assert cell.matches(native["fields"], stored)
    assert app.fingerprint_matches(accepted_row(name, attempt), native)
    for org in ("acme", "\x01" * cell.ORG_MAX_BYTES):
        assert cell.named_org(org) == org and not cell.org_name_refusal(org)
        body = envelopes(name, native, org)[int(operator)]
        response, calls, size = await route_response(monkeypatch, name, attempt, body, operator)
        if org == "acme":
            assert size <= MAX_BODY_BYTES and response.status_code == 200
            assert len(calls) == 1
        else:
            assert size > MAX_BODY_BYTES
            assert response.status_code == (413 if operator else 400)
            assert not calls
    reason = directory.begin_refusal(attaching(name), attempt)
    assert position in reason and "oidc_client_id" in reason


@pytest.mark.parametrize("name", ["a", "a" * 32])
@pytest.mark.parametrize("position", ["current_fp", "next_fp"])
@pytest.mark.parametrize("operator", [False, True])
@pytest.mark.parametrize("unknown", [{}, {"future": "x" * 512}])
async def test_accepted_sparse_boundary_serves_native_requests(
    monkeypatch, native_fingerprint, name, position, operator, unknown
):
    native = native_fingerprint("")
    # Unknown stored fields keep their existing reservation, although native callers
    # do not emit them. Reserve that representation with all known defaults present.
    reserved = cell.as_value({**native["fields"], **unknown})
    empty = max(len(json.dumps(body)) for body in envelopes(name, reserved))
    client_id = "x" * (MAX_BODY_BYTES - empty - 6 * cell.ORG_MAX_BYTES)
    native = native_fingerprint(client_id)
    stored = stored_fingerprint(native, OMITTABLE_DEFAULTS, unknown)
    attempt = begin(stored, position)
    assert directory.begin_refusal(attaching(name), attempt) == ""
    assert cell.matches(native["fields"], stored)
    assert app.fingerprint_matches(accepted_row(name, attempt), native)
    for org in ("acme", "\x01" * cell.ORG_MAX_BYTES):
        body = envelopes(name, native, org)[int(operator)]
        assert len(json.dumps(body).encode()) <= MAX_BODY_BYTES
        response, calls, size = await route_response(monkeypatch, name, attempt, body, operator)
        assert size <= MAX_BODY_BYTES and response.status_code == 200
        assert len(calls) == 1
    stored["fields"]["future"] = "x" * MAX_BODY_BYTES
    assert position in directory.begin_refusal(attaching(name), begin(stored, position))
