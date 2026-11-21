"""Fixed administration caps precede JSON parsing when the global cap is off."""

from __future__ import annotations

from typing import Any

import pytest

PATHS = [
    ("capabilities", 1024),
    ("settings_list", 65536),
    ("settings_set", 65536),
    ("settings_history", 65536),
    ("signing_keys_list", 65536),
    ("signing_keys_rotate", 65536),
    ("signing_keys_retire", 65536),
    ("subject_revoke", 65536),
]


async def _request(path: str, body: bytes, *, declared: bool) -> tuple[int, list[bytes], int]:
    from server.transport.body_limit import BodyLimitMiddleware

    messages: list[dict[str, Any]] = []
    delivered: list[bytes] = []
    reads = 0
    chunks = [body] if declared else [body[:-1], body[-1:]]

    async def receive() -> dict[str, Any]:
        nonlocal reads
        reads += 1
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            delivered.append(message["body"])
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    scope = {
        "type": "http",
        "path": path,
        "method": "POST",
        "headers": [(b"content-length", str(len(body)).encode())] if declared else [],
    }
    await BodyLimitMiddleware(downstream, max_bytes=0)(scope, receive, send)
    return messages[0]["status"], delivered, reads


@pytest.mark.parametrize(("action", "limit"), PATHS)
@pytest.mark.parametrize("declared", [True, False], ids=["declared", "chunked"])
async def test_feature_body_overflow_is_cut_off_before_materialization(
    action: str, limit: int, declared: bool
) -> None:
    body = b"x" * (limit + 1)
    status, delivered, reads = await _request(f"/v1/admin/cell.{action}", body, declared=declared)
    assert status == 413
    assert len(b"".join(delivered)) <= limit
    if declared:
        assert reads == 0 and delivered == []


@pytest.mark.parametrize(("action", "limit"), PATHS)
async def test_feature_body_at_the_limit_reaches_the_handler(action: str, limit: int) -> None:
    body = b"x" * limit
    status, delivered, _ = await _request(f"/v1/admin/cell.{action}", body, declared=False)
    assert status == 200 and b"".join(delivered) == body


async def test_disabling_global_cap_keeps_ordinary_paths_unchanged() -> None:
    body = b"x" * 65537
    status, delivered, _ = await _request("/ordinary", body, declared=True)
    assert status == 200 and b"".join(delivered) == body


@pytest.fixture
def authenticated_settings_router(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any]:
    from unittest.mock import AsyncMock

    from fastapi import FastAPI

    from server.insights.app import cell_admin_routes as routes
    from server.insights.app.admin_routes import require_admin
    from server.insights.app.cell_admin_auth import AutomationBinding, VerifiedCellAdmin
    from server.settings import settings
    from tests.unit.principals import principal

    auth = principal(
        "platform_admin", namespaces=(), subject="sa:automation-alpha", token_id="automation-token"
    )

    async def verified_admin() -> Any:
        return auth

    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "")
    verified = VerifiedCellAdmin(
        auth, AutomationBinding("alpha", "deploy-alpha", "automation-alpha", "A" * 43)
    )
    monkeypatch.setattr(routes, "verify_automation", AsyncMock(return_value=verified))
    business = AsyncMock(return_value={"updated": True})
    audit = AsyncMock(side_effect=["1" * 64, "2" * 64])
    monkeypatch.setattr(routes.actions, "settings_set", business)
    monkeypatch.setattr(routes.audit, "emit_strict", audit)
    app = FastAPI()
    app.include_router(routes.cell_admin_router)
    app.dependency_overrides[require_admin] = verified_admin
    return app, business, audit


def _settings_action_prefix() -> bytes:
    import json

    from server.core.hashing import sha256_value

    arguments = {
        "name": "memory_ask_recency_weight",
        "value": "0.25",
        "clear": False,
        "expected": None,
    }
    context = {
        "version": 1,
        "expected_cell": "alpha",
        "expected_deployment_id": "deploy-alpha",
        "expected_bot": "automation-alpha",
        "run_id": "a046002d-c888-4b54-852b-167f1cb8a443",
        "attempt": 1,
        "command_digest": sha256_value({"action": "settings_set", "arguments": arguments}),
    }
    return json.dumps({"context": context, **arguments}).encode()


async def _settings_chunks(app: Any, prefix: bytes, suffix: bytes) -> list[dict[str, Any]]:
    from server.transport.body_limit import BodyLimitMiddleware

    chunks = [prefix, suffix]
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/v1/admin/cell.settings_set",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1),
        "server": ("cell.example", 443),
    }
    await BodyLimitMiddleware(app, max_bytes=0)(scope, receive, send)
    assert chunks == []
    return messages


@pytest.mark.parametrize("suffix_byte", [b" ", b"!"], ids=["whitespace", "invalid-json"])
async def test_valid_action_prefix_overflow_never_runs_business_or_audit(
    suffix_byte: bytes, authenticated_settings_router: tuple[Any, Any, Any]
) -> None:
    import json

    app, business, audit = authenticated_settings_router
    prefix = _settings_action_prefix()
    suffix = suffix_byte * (65537 - len(prefix))
    messages = await _settings_chunks(app, prefix, suffix)
    assert messages[0]["status"] == 413
    body = json.loads(b"".join(message.get("body", b"") for message in messages))
    assert body["detail"]["code"] == "PAYLOAD_TOO_LARGE"
    assert body["detail"]["limit_bytes"] == 65536
    business.assert_not_awaited()
    audit.assert_not_awaited()


async def test_valid_action_at_limit_runs_business_and_both_audits(
    authenticated_settings_router: tuple[Any, Any, Any],
) -> None:
    import json

    app, business, audit = authenticated_settings_router
    prefix = _settings_action_prefix()
    messages = await _settings_chunks(app, prefix, b" " * (65536 - len(prefix)))
    assert messages[0]["status"] == 200
    body = json.loads(b"".join(message.get("body", b"") for message in messages))
    assert body["status"] == "completed" and body["result"] == {"updated": True}
    business.assert_awaited_once()
    assert [call.args[0] for call in audit.await_args_list] == [
        "cell.each_started",
        "cell.each_completed",
    ]
