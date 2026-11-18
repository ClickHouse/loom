"""One app-owned family guards responses and actual SDK dispatches."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest
from loom.agent import Agent
from loom.errors import BackendUnavailable
from loom.session_protection import (
    AutomaticCaptureSuppressed,
    SessionProtection,
    SessionProtectionUnavailable,
    observe_response,
    require_automatic_allowed,
)
from loom.transport import Transport


@pytest.fixture
def family(tmp_path: Path) -> SessionProtection:
    return SessionProtection.create(base=tmp_path / "state")


def test_capsule_is_content_free_private_and_monotonic(family: SessionProtection) -> None:
    directory = family.base / family.id
    assert not family.protected
    assert sorted(p.name for p in directory.iterdir()) == ["root.json", "state"]
    assert json.loads((directory / "root.json").read_bytes()) == {"id": family.id, "version": 1}
    before = (directory / "state").read_bytes()
    restored = SessionProtection.open(family.pointer())
    restored.mark()
    assert family.protected
    assert (directory / "state").read_bytes().startswith(before)
    for path in [family.base, directory, directory / "state", directory / "root.json"]:
        assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("state", [b"", b"0", b"0\n1", b"{}", b"0\n1\n"])
def test_partial_state_is_never_clean(family: SessionProtection, state: bytes) -> None:
    (family.base / family.id / "state").write_bytes(state)
    assert family.protected
    with pytest.raises(AutomaticCaptureSuppressed), family.automatic():
        pytest.fail("an uncertain family ran its callback")


@pytest.mark.parametrize("file", ["root.json", "state"])
def test_missing_restore_never_initializes(family: SessionProtection, file: str) -> None:
    (family.base / family.id / file).unlink()
    assert family.protected
    with pytest.raises(
        SessionProtectionUnavailable, match="^Session protection state could not be confirmed$"
    ):
        family.mark()
    assert not (family.base / family.id / file).exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "permissions"])
def test_planted_state_is_refused_without_changing_target(
    family: SessionProtection, tmp_path: Path, kind: str
) -> None:
    state = family.base / family.id / "state"
    target = tmp_path / "preserve"
    target.write_bytes(b"preserve")
    target.chmod(0o600)
    if kind == "permissions":
        state.chmod(0o666)
    else:
        state.unlink()
        if kind == "symlink":
            state.symlink_to(target)
        else:
            os.link(target, state)
    assert family.protected
    with pytest.raises(SessionProtectionUnavailable):
        family.mark()
    assert target.read_bytes() == b"preserve"


def test_boolean_version_and_bad_pointer_refuse(family: SessionProtection) -> None:
    manifest = family.base / family.id / "root.json"
    manifest.write_text(json.dumps({"version": True, "id": family.id}))
    assert family.protected
    for pointer in [
        family.pointer(),
        {"base": "relative", "id": family.id},
        {**family.pointer(), "extra": True},
    ]:
        with pytest.raises(SessionProtectionUnavailable):
            SessionProtection.open(pointer)


@pytest.mark.parametrize(
    "body",
    [
        {"mirrored": True},
        {"result": {"structuredContent": {"mirrored": True}}},
        {"content": [{"type": "text", "text": '{"mirrored":true}'}]},
        {"items": [{"found": True, "mirrored": True}]},
    ],
)
def test_observation_marks_before_return(family: SessionProtection, body: dict) -> None:
    assert family.observe(body) is body
    assert family.protected


def test_arguments_do_not_choose_a_family_or_clear_its_mark(family: SessionProtection) -> None:
    family.observe({"arguments": {"mirrored": True, "session_id": "other"}})
    assert not family.protected
    family.mark()
    family.observe({"mirrored": False})
    assert family.protected


@pytest.mark.parametrize(
    "body",
    [
        {"mirrored": "true"},
        {"content": [{"type": "text", "text": '{"mirrored":true'}]},
        {"items": [{}] * 10001},
        {"content": [{"type": "text", "text": '{"answer":"' + "x" * (256 * 1024) + '"}'}] * 32},
    ],
)
def test_ambiguous_or_unbounded_envelopes_withhold_and_protect(
    family: SessionProtection, body: dict
) -> None:
    with pytest.raises(SessionProtectionUnavailable):
        family.observe(body)
    assert family.protected


def test_nested_nontext_blocks_share_one_observation_work_budget(family: SessionProtection) -> None:
    blocks = [{"type": "image"}] * 4999
    with pytest.raises(SessionProtectionUnavailable):
        family.observe({"items": [{"content": blocks} for _ in range(5000)]})
    assert family.protected


def test_nested_automatic_context_cannot_replace_a_protected_parent(
    family: SessionProtection, tmp_path: Path
) -> None:
    other = SessionProtection.create(base=tmp_path / "other")
    with family.automatic(), other.automatic():
        family.mark()
        with pytest.raises(AutomaticCaptureSuppressed):
            require_automatic_allowed()
    require_automatic_allowed()
    assert not other.protected


def test_observer_attempts_every_family_when_one_mark_fails(
    family: SessionProtection, tmp_path: Path
) -> None:
    other = SessionProtection.create(base=tmp_path / "other")
    with other.automatic():
        (other.base / other.id / "state").unlink()
        with pytest.raises(SessionProtectionUnavailable):
            observe_response({"mirrored": True}, family)
    assert family.protected


@pytest.mark.asyncio
async def test_agent_retains_the_application_owned_family(family: SessionProtection) -> None:
    agent = Agent(base_url="http://inert.invalid", api_key="test-token", session_protection=family)
    try:
        assert agent.transport.session_protection is family
        family.mark()
        assert agent.transport.session_protection.protected
    finally:
        await agent.transport.close()


@pytest.mark.asyncio
async def test_transport_observes_then_blocks_automatic_and_preserves_explicit(
    family: SessionProtection,
) -> None:
    transport = Transport("http://inert.invalid", session_protection=family)
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"mirrored": True, "answer": "example"})

    transport._client._transport = httpx.MockTransport(respond)
    try:
        with family.automatic():
            assert (await transport.post("/v1/knowledge.get", {}))["answer"] == "example"
            assert family.protected
            with pytest.raises(AutomaticCaptureSuppressed):
                await transport.post("/v1/code.repos", {})
        assert (await transport.post("/v1/memory.set", {}))["answer"] == "example"
        assert len(seen) == 2
        assert all("X-Loom-Session-Protection" not in request.headers for request in seen)
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_mark_during_authentication_prevents_actual_dispatch(
    family: SessionProtection,
) -> None:
    class Auth(httpx.Auth):
        async def async_auth_flow(self, request):
            await asyncio.sleep(0)
            family.mark()
            yield request

    transport = Transport("http://inert.invalid")
    transport._client.auth = Auth()
    seen = []
    transport._client._transport = httpx.MockTransport(
        lambda request: seen.append(request) or httpx.Response(200, json={})
    )
    try:
        with family.automatic(), pytest.raises(AutomaticCaptureSuppressed):
            await transport.post("/v1/memory.set", {})
        assert not seen
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_new_transport_and_server_do_not_reset_ambient_family(
    family: SessionProtection,
) -> None:
    first = Transport("http://first.invalid", api_key="first", session_protection=family)
    second = Transport("http://second.invalid", api_key="second")
    second._client._transport = httpx.MockTransport(
        lambda request: pytest.fail("protected operation was sent")
    )
    try:
        with family.automatic():
            family.mark()
            with pytest.raises(AutomaticCaptureSuppressed):
                await second.request("GET", "/v1/health")
        assert first.session_protection is family
    finally:
        await first.close()
        await second.close()


def test_node_and_python_share_the_same_capsule_format(
    family: SessionProtection, tmp_path: Path
) -> None:
    helper = (
        Path(__file__).resolve().parents[2]
        / "integrations/claude-code-plugin/lib/session-protection.mjs"
    )
    source = f"""
      import {{createProtectionRoot,protectSession,sessionIsProtected}} from {json.dumps(helper.as_uri())};
      const inherited = JSON.parse(process.argv[1]);
      if (sessionIsProtected(inherited)) process.exit(2);
      protectSession(inherited);
      const own = createProtectionRoot({{base:process.argv[2]}});
      process.stdout.write(JSON.stringify(own));
    """
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            source,
            json.dumps(family.pointer()),
            str(tmp_path / "node"),
        ],
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert family.protected
    node_root = SessionProtection.open(json.loads(result.stdout))
    assert not node_root.protected
    node_root.mark()
    assert node_root.protected


@pytest.mark.asyncio
async def test_clean_ambient_context_cannot_override_a_protected_bound_client(
    family: SessionProtection, tmp_path: Path
) -> None:
    other = SessionProtection.create(base=tmp_path / "other")
    family.mark()
    transport = Transport("http://inert.invalid", session_protection=family)
    transport._client._transport = httpx.MockTransport(
        lambda request: pytest.fail("protected client sent")
    )
    try:
        with other.automatic(), pytest.raises(AutomaticCaptureSuppressed):
            await transport.post("/v1/code.repos", {})
        assert not other.protected
    finally:
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["request", "request_json", "post"])
async def test_malformed_json_is_withheld_before_typed_errors(
    family: SessionProtection, method: str
) -> None:
    transport = Transport("http://inert.invalid", session_protection=family)
    transport._client._transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=b'{"mirrored":true,"answer":"private',
            headers={"content-type": "application/json"},
        )
    )
    try:
        with pytest.raises(SessionProtectionUnavailable) as caught:
            if method == "post":
                await transport.post("/v1/knowledge.get", {})
            else:
                await getattr(transport, method)("GET", "/v1/example")
        assert str(caught.value) == "Session protection state could not be confirmed"
        assert family.protected
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_marked_error_is_observed_before_retry(family: SessionProtection) -> None:
    transport = Transport("http://inert.invalid", session_protection=family, pacing=False)
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            500, json={"mirrored": True, "detail": {"code": "INTERNAL_ERROR", "message": "example"}}
        )

    transport._client._transport = httpx.MockTransport(respond)
    try:
        with family.automatic(), pytest.raises(AutomaticCaptureSuppressed):
            await transport.post("/v1/code.repos", {})
        assert family.protected
        assert len(seen) == 1
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_unbound_raw_request_preserves_unparsed_response() -> None:
    transport = Transport("http://inert.invalid")
    transport._client._transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=b'{"unfinished',
            headers={"content-type": "application/json"},
        )
    )
    try:
        response = await transport.request("GET", "/v1/example")
        assert response.content == b'{"unfinished'
    finally:
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["private text", [{"mirrored": True}], None])
async def test_nonobject_error_is_refused_before_error_conversion(
    family: SessionProtection, body
) -> None:
    transport = Transport("http://inert.invalid", session_protection=family)
    transport._client._transport = httpx.MockTransport(
        lambda request: httpx.Response(
            500,
            content=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
        )
    )
    try:
        with pytest.raises(SessionProtectionUnavailable) as caught:
            await transport.post("/v1/code.repos", {})
        assert str(caught.value) == "Session protection state could not be confirmed"
        assert family.protected
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_bound_json_route_is_decoded_once(family: SessionProtection) -> None:
    decoded = []

    class Response(httpx.Response):
        def json(self, **kwargs):
            decoded.append(1)
            return super().json(**kwargs)

    transport = Transport("http://inert.invalid", session_protection=family)
    transport._client._transport = httpx.MockTransport(
        lambda request: Response(200, json={"found": False})
    )
    try:
        assert await transport.request_json("GET", "/v1/example") == {"found": False}
        assert len(decoded) == 1
    finally:
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["network", "cancel"])
@pytest.mark.parametrize("status", [200, 503])
async def test_interrupted_json_protects_before_the_next_automatic_request(
    family: SessionProtection, interruption: str, status: int
) -> None:
    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"mirrored":tr'
            if interruption == "cancel":
                raise asyncio.CancelledError
            raise httpx.ReadError("cut off")

    sends = []

    def respond(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        return httpx.Response(
            status,
            stream=Interrupted(),
            headers={"content-type": "application/json" if status == 200 else "text/plain"},
        )

    transport = Transport("http://inert.invalid", session_protection=family)
    transport._client._transport = httpx.MockTransport(respond)
    expected = asyncio.CancelledError if interruption == "cancel" else BackendUnavailable
    try:
        with family.automatic():
            with pytest.raises(expected):
                await transport.request_json("GET", "/v1/example")
            assert family.protected
            with pytest.raises(AutomaticCaptureSuppressed):
                await transport.request_json("GET", "/v1/example")
        assert len(sends) == 1
    finally:
        await transport.close()


async def _opening_binding_call(transport: Transport, route: str) -> None:
    if route == "post":
        await transport.post("/v1/knowledge.get", {})
    elif route == "stream":
        async with transport.stream("/assistant/chat", json={}) as response:
            await response.aread()
    else:
        await transport.request_json("GET", "/v1/example")


@pytest.mark.asyncio
@pytest.mark.parametrize("automatic", [False, True])
async def test_response_read_retains_every_opening_observer(
    family: SessionProtection, tmp_path: Path, automatic: bool
) -> None:
    ambient = SessionProtection.create(base=tmp_path / "ambient")
    intermediate = SessionProtection.create(base=tmp_path / "intermediate")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    transport = Transport("http://inert.invalid", session_protection=family)

    class Rebind(httpx.Auth):
        async def async_auth_flow(self, request):
            transport.session_protection = intermediate
            yield request

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0)
            transport.session_protection = replacement
            yield b'{"mirrored":true}'

    transport._client.auth = Rebind()
    transport._client._transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, stream=Body(), headers={"content-type": "application/json"}
        )
    )
    try:
        if automatic:
            with ambient.automatic():
                await _opening_binding_call(transport, "post")
        else:
            await _opening_binding_call(transport, "post")
        assert all(root.protected for root in (family, intermediate, replacement))
        assert ambient.protected is automatic
    finally:
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["post", "request_json", "stream", "json_without_content_type"])
@pytest.mark.parametrize("automatic", [False, True])
async def test_opening_binding_observes_after_client_rebind(
    family: SessionProtection, tmp_path: Path, route: str, automatic: bool
) -> None:
    ambient = SessionProtection.create(base=tmp_path / "ambient")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    transport = Transport("http://inert.invalid", session_protection=family)

    class Rebind(httpx.Auth):
        async def async_auth_flow(self, request):
            await asyncio.sleep(0)
            transport.session_protection = replacement
            yield request

    def respond(request: httpx.Request) -> httpx.Response:
        if route == "stream":
            return httpx.Response(
                200,
                content=b'data: {"mirrored":true}\n\n',
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            content=b'{"mirrored":true}',
            headers={}
            if route == "json_without_content_type"
            else {"content-type": "application/json"},
        )

    transport._client.auth = Rebind()
    transport._client._transport = httpx.MockTransport(respond)
    try:
        if automatic:
            with ambient.automatic():
                await _opening_binding_call(transport, route)
        else:
            await _opening_binding_call(transport, route)
        assert family.protected
        assert replacement.protected
        assert ambient.protected is automatic
    finally:
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route,phase",
    [("post", "auth"), ("post", "before_post"), ("request_json", "auth"), ("stream", "auth")],
)
async def test_opening_binding_stops_dispatch_after_rebind_and_mark(
    family: SessionProtection, tmp_path: Path, route: str, phase: str
) -> None:
    ambient = SessionProtection.create(base=tmp_path / "ambient")
    replacement = SessionProtection.create(base=tmp_path / "replacement")
    transport = Transport("http://inert.invalid", session_protection=family)
    sends = []

    async def rebind():
        await asyncio.sleep(0)
        transport.session_protection = replacement
        family.mark()

    class Rebind(httpx.Auth):
        async def async_auth_flow(self, request):
            if phase == "auth":
                await rebind()
            yield request

    async def before_post(path, payload):
        await rebind()
        return {}

    def respond(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        if route == "stream":
            return httpx.Response(
                200, content=b"data: {}\n\n", headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json={})

    if phase == "before_post":
        transport.before_post = before_post
    transport._client.auth = Rebind()
    transport._client._transport = httpx.MockTransport(respond)
    try:
        with ambient.automatic(), pytest.raises(AutomaticCaptureSuppressed):
            await _opening_binding_call(transport, route)
        assert sends == []
        assert not replacement.protected
    finally:
        await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["request", "request_json"])
@pytest.mark.parametrize("status", [200, 204, 304])
async def test_empty_bound_json_requires_a_bodyless_status(
    family: SessionProtection, method: str, status: int
) -> None:
    transport = Transport("http://inert.invalid", session_protection=family)
    transport._client._transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status,
            content=b"",
            headers={"content-type": "application/json"},
        )
    )
    try:
        if status == 200:
            with pytest.raises(SessionProtectionUnavailable):
                await getattr(transport, method)("GET", "/v1/example")
            assert family.protected
            return
        result = await getattr(transport, method)("GET", "/v1/example")
        if method == "request":
            assert result.status_code == status
            assert result.content == b""
        else:
            assert result == {}
        assert not family.protected
    finally:
        await transport.close()
