"""Unknown remote completion stays uncertain and cannot cause another send."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from server.cli import cell_each
from server.cli import cell_each_http as client
from server.cli import cell_each_state as history


def _target() -> dict[str, Any]:
    return {
        "cell": "alpha",
        "deployment_id": "deploy-alpha",
        "origin": "https://alpha.example.com",
        "state": "serving",
        "mode": "enforced",
        "mutation_generation": "c33e76ce-f023-4b45-92ea-2e9bfa008583",
        "configuration_digests": ["1" * 64],
        "automation": {"name": "automation-alpha", "thumbprint": "A" * 43},
        "recovery": {"name": "recovery-alpha", "thumbprint": "B" * 43},
    }


def _state(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    state = history.new_state(
        cell_each.parse_command(["settings", "set", "memory_ask_recency_weight", "0.25"]),
        targets=[_target()],
        run_id="a046002d-c888-4b54-852b-167f1cb8a443",
    )
    state["router"] = "https://router.example.com"
    path = directory / "state.json"
    history.write_state(path, state, exclusive=True)
    return state, path


def _preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    async def point(*args: Any, **kwargs: Any) -> None:
        pass

    async def token(*args: Any, **kwargs: Any) -> tuple[str, float]:
        return "unit-bearer", time.monotonic() + 60

    monkeypatch.setattr(client, "_point", point)
    monkeypatch.setattr(client, "_token", token)
    monkeypatch.setattr(client, "_capability", point)


@pytest.mark.parametrize(
    "failure", ["generic_503", "wrong_context", "missing_audit", "untyped_result"]
)
@pytest.mark.asyncio
async def test_ambiguous_action_never_becomes_retryable(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, path = _state(tmp_path)
    _preflight(monkeypatch)
    sends: list[str] = []

    async def request(*args: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        on_disk = history.read_state(path)["outcomes"]["alpha"]
        assert on_disk["status"] == "dispatching" and on_disk["attempt"] == 1
        sends.append("action")
        if failure == "generic_503":
            return 503, {"detail": {"code": "SECURITY_UNAVAILABLE"}}
        answer = {
            "version": 1,
            "status": "completed",
            "context": client._context(_target(), state),
            "action": "settings_set",
            "started_audit": "a" * 64,
            "completed_audit": "b" * 64,
            "result": {
                "ok": True,
                "setting": {
                    "name": "memory_ask_recency_weight",
                    "env": "LOOM_MEMORY_ASK_RECENCY_WEIGHT",
                    "value": 0.25,
                    "default": 0.0,
                    "source": "override",
                    "live": True,
                    "description": "Weight",
                },
            },
        }
        if failure == "wrong_context":
            answer["context"]["expected_deployment_id"] = "foreign"
        elif failure == "missing_audit":
            answer["completed_audit"] = ""
        else:
            answer["result"] = {"ok": True, "credential": "must-not-be-saved"}
        return 200, answer

    monkeypatch.setattr(client, "_request", request)
    manifest = {"keys": {("alpha", "deploy-alpha"): path.parent / "unused.pem"}}
    assert not await client._cell(
        None, _target(), state, manifest, state["router"], "unit-root", path, time.monotonic() + 60
    )
    saved = history.read_state(path)
    assert saved["outcomes"]["alpha"]["status"] == "UNSETTLED"
    assert saved["outcomes"]["alpha"]["attempt"] == 1
    assert saved["outcomes"]["alpha"]["completion"] is None
    assert sends == ["action"]
    assert "must-not-be-saved" not in path.read_text()


@pytest.mark.asyncio
async def test_failed_dispatch_persistence_prevents_action_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, path = _state(tmp_path)
    _preflight(monkeypatch)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("write failed")

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an action cannot precede durable dispatch")

    monkeypatch.setattr(history, "write_state", fail)
    monkeypatch.setattr(client, "_request", forbidden)
    manifest = {"keys": {("alpha", "deploy-alpha"): path.parent / "unused.pem"}}
    with pytest.raises(OSError, match="write failed"):
        await client._cell(
            None,
            _target(),
            state,
            manifest,
            state["router"],
            "unit-root",
            path,
            time.monotonic() + 60,
        )
    assert history.read_state(path)["outcomes"]["alpha"]["status"] == "pending"


@pytest.mark.parametrize(
    "change", ["none", "wrong_phase", "wrong_bot", "boolean_version", "unknown_code"]
)
def test_only_matching_closed_before_business_receipt_is_retryable(
    change: str, tmp_path: Path
) -> None:
    state, _path = _state(tmp_path)
    state["outcomes"]["alpha"].update(status="dispatching", attempt=1)
    answer = {
        "version": 1,
        "status": "refused",
        "phase": "before_business",
        "context": client._context(_target(), state),
        "action": "settings_set",
        "code": "cell_unavailable",
    }
    if change == "wrong_phase":
        answer["phase"] = "after_business"
    elif change == "wrong_bot":
        answer["context"]["expected_bot"] = "recovery-alpha"
    elif change == "boolean_version":
        answer["context"]["version"] = True
    elif change == "unknown_code":
        answer["code"] = "arbitrary_gateway_failure"
    assert client._no_action(answer, _target(), state) == (
        "CELL_UNAVAILABLE" if change == "none" else None
    )


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


@pytest.mark.parametrize(
    "body", [b'{"same":1,"same":2}', b'{"number":NaN}', b'{"number":1e9999}', b"x" * 65]
)
@pytest.mark.asyncio
async def test_bounded_response_rejects_duplicate_nonfinite_and_overflow(body: bytes) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, stream=_Chunks([body])))
    async with httpx.AsyncClient(transport=transport, trust_env=False) as session:
        with pytest.raises(ValueError):
            await client._request(
                session, "GET", "https://alpha.example.com", limit=64, deadline=time.monotonic() + 5
            )


@pytest.mark.parametrize(
    "origin",
    [
        "http://alpha.example.com",
        "https://user@alpha.example.com",
        "https://alpha.example.com/path",
        "https://alpha.example.com?query",
        "https://alpha.example.com#fragment",
        "https://alpha.example.com:bad",
        "https://alpha.example.com\\foreign",
    ],
)
def test_cell_credential_cannot_target_an_unvalidated_origin(origin: str) -> None:
    with pytest.raises(ValueError):
        client._origin(origin)


def test_private_manifest_has_no_ambient_or_recovery_fallback(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "router_token_file": str(directory / "operator"),
                "keys": [
                    {
                        "cell": "alpha",
                        "deployment_id": "deploy-alpha",
                        "key_file": str(directory / "automation"),
                    }
                ],
                "recovery_key_file": str(directory / "recovery"),
            }
        )
    )
    path.chmod(0o600)
    with pytest.raises(ValueError):
        client._manifest(path)


def test_private_reader_requires_a_private_parent_even_at_the_root() -> None:
    with pytest.raises(ValueError):
        history.read_private(Path("/cell-each-private-input"), 64)


def test_lost_read_can_resume_without_clearing_an_unknown_mutation(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "state.json"
    state = history.new_state(
        cell_each.parse_command(["settings", "list"]),
        targets=[_target()],
        run_id="a046002d-c888-4b54-852b-167f1cb8a443",
    )
    history.write_state(path, state, exclusive=True)
    state["outcomes"]["alpha"].update(status="dispatching", attempt=1)
    history.write_state(path, state)
    history.resume_read(path, state)
    assert history.read_state(path)["outcomes"]["alpha"]["status"] == "known-no-action"
    assert history.read_state(path)["outcomes"]["alpha"]["code"] == "READ_INCOMPLETE"


def test_directory_generation_is_the_existing_uuid_contract() -> None:
    target = _target()
    target["mutation_generation"] = 1
    with pytest.raises(ValueError):
        client._target(target)
    assert (
        client._target(_target())["mutation_generation"] == "c33e76ce-f023-4b45-92ea-2e9bfa008583"
    )


def test_wrong_automation_key_is_refused_before_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a foreign key must not sign a client assertion")

    monkeypatch.setattr(jwt, "encode", forbidden)
    with pytest.raises(ValueError):
        client._assertion(pem, _target(), "https://alpha.example.com/oauth/token")


def test_explicit_bot_assertion_keeps_the_selected_identity() -> None:
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from jwt.algorithms import ECAlgorithm

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public = ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
    members = {name: public[name] for name in ("crv", "kty", "x", "y")}
    thumbprint = (
        base64.urlsafe_b64encode(hashlib.sha256(client._encoded(members)).digest())
        .rstrip(b"=")
        .decode()
    )
    endpoint = "https://alpha.example.com/oauth/token"
    token = client.bot_assertion(
        pem, name="selected-recovery", thumbprint=thumbprint, endpoint=endpoint
    )
    claims = jwt.decode(
        token,
        key.public_key(),
        algorithms=["ES256"],
        audience=endpoint,
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["iss"] == claims["sub"] == "selected-recovery"
    assert claims["exp"] - claims["iat"] == 120
    assert jwt.get_unverified_header(token)["kid"] == thumbprint


def test_historical_policy_display_does_not_apply_new_input_size_limits() -> None:
    row = {
        "pattern_id": "a046002d-c888-4b54-852b-167f1cb8a443",
        "pattern": "é" * 4096 + "\n\t\0",
        "pattern_type": "substring",
        "reason": "旧" * 1024,
        "created_by": "historical-writer",
        "hit_count": 0,
        "last_hit_at": None,
        "created_at": "2026-01-01T00:00:00",
    }
    client._result("blacklist_global_list", {"rows": [row], "truncated": False}, {"limit": 50})
    with pytest.raises(ValueError):
        client._result(
            "blacklist_global_list", {"rows": [row, row], "truncated": False}, {"limit": 50}
        )


@pytest.mark.asyncio
async def test_repeated_preflight_refusals_are_durable_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, path = _state(tmp_path)

    async def refuse(*args: Any, **kwargs: Any) -> None:
        raise ValueError("authority unavailable before business")

    monkeypatch.setattr(client, "_point", refuse)
    for attempt in (1, 2):
        assert not await client._cell(
            None, _target(), state, {}, state["router"], "unit-root", path, time.monotonic() + 60
        )
        saved = history.read_state(path)["outcomes"]["alpha"]
        assert saved["status"] == "known-no-action" and saved["attempt"] == attempt


@pytest.mark.asyncio
async def test_recorded_revocation_without_finished_cleanup_remains_unsettled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "state.json"
    state = history.new_state(
        cell_each.parse_command(["revoke-token", "--subject", "sa:obsolete"]),
        targets=[_target()],
        run_id="a046002d-c888-4b54-852b-167f1cb8a443",
    )
    state["router"] = "https://router.example.com"
    history.write_state(path, state, exclusive=True)
    _preflight(monkeypatch)

    async def partial(*args: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        return 503, {
            "detail": {
                "code": "SUBJECT_REVOCATION_INCOMPLETE",
                "recorded": True,
                "sessions_at_least": 2,
                "tokens_at_least": 1,
            }
        }

    monkeypatch.setattr(client, "_request", partial)
    manifest = {"keys": {("alpha", "deploy-alpha"): directory / "unused.pem"}}
    assert not await client._cell(
        None, _target(), state, manifest, state["router"], "unit-root", path, time.monotonic() + 60
    )
    outcome = history.read_state(path)["outcomes"]["alpha"]
    assert outcome["status"] == "UNSETTLED" and outcome["attempt"] == 1
    assert outcome["completion"] is None


@pytest.mark.asyncio
async def test_confirmed_typed_action_persists_only_safe_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, path = _state(tmp_path)
    _preflight(monkeypatch)

    async def request(*args: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        assert history.read_state(path)["outcomes"]["alpha"]["status"] == "dispatching"
        return 200, {
            "version": 1,
            "status": "completed",
            "context": client._context(_target(), state),
            "action": "settings_set",
            "started_audit": "a" * 64,
            "completed_audit": "b" * 64,
            "result": {
                "ok": True,
                "setting": {
                    "name": "memory_ask_recency_weight",
                    "env": "LOOM_MEMORY_ASK_RECENCY_WEIGHT",
                    "value": 0.25,
                    "default": 0.0,
                    "source": "override",
                    "live": True,
                    "description": "raw-response-must-not-be-persisted",
                },
            },
        }

    monkeypatch.setattr(client, "_request", request)
    manifest = {"keys": {("alpha", "deploy-alpha"): path.parent / "unused.pem"}}
    assert await client._cell(
        None, _target(), state, manifest, state["router"], "unit-root", path, time.monotonic() + 60
    )
    outcome = history.read_state(path)["outcomes"]["alpha"]
    assert outcome["status"] == "completed" and outcome["attempt"] == 1
    assert outcome["completion"]["started_audit"] == "a" * 64
    assert outcome["completion"]["completed_audit"] == "b" * 64
    assert "result" not in outcome["completion"]
    assert "raw-response-must-not-be-persisted" not in path.read_text()


@pytest.mark.asyncio
async def test_same_client_does_not_forward_cookies_between_requests() -> None:
    cookies: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cookies.append(request.headers.get("cookie"))
        return httpx.Response(
            200,
            headers={"Set-Cookie": "token=must-not-travel; Domain=.example.com; Path=/"},
            stream=_Chunks([b"{}"]),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as session:
        await client._request(
            session, "GET", "https://alpha.example.com", limit=64, deadline=time.monotonic() + 5
        )
        await client._request(
            session, "GET", "https://beta.example.com", limit=64, deadline=time.monotonic() + 5
        )
    assert cookies == [None, None]
