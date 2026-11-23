"""Source CLI behavior with the installed SDK and actual transport hooks."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
from pathlib import Path

import httpx
import pytest
from loom import source_cli as cli
from loom.cli_context import CONTEXT_ENV, session_environment
from loom.errors import LoomError
from loom.session_protection import (
    AutomaticCaptureSuppressed,
    SessionProtection,
    SessionProtectionUnavailable,
)
from loom.transport import Transport

REQUEST_ID = "10000000-0000-4000-8000-000000000001"
SLOT = "20000000-0000-4000-8000-000000000002"


def pointer(tmp_path: Path) -> tuple[SessionProtection, Path]:
    directory = tmp_path / "application"
    directory.mkdir(mode=0o700)
    family = SessionProtection.create(base=tmp_path / "families")
    path = directory / "session.json"
    cli.save_session_pointer(path, family)
    return family, path


def arguments(path: Path, *command: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    cli.register_source_cli(parser.add_subparsers(required=True))
    return parser.parse_args(
        [
            "source",
            *command,
            "--server-url",
            "https://loom.invalid",
            "--org",
            "test-org",
            "--session-file",
            str(path),
        ]
    )


def factory(respond, opened: list[Transport] | None = None):
    def create(*args, **kwargs):
        transport = Transport(*args, **kwargs)
        transport._client._transport = httpx.MockTransport(respond)
        transport._client._mounts.clear()
        if opened is not None:
            opened.append(transport)
        return transport

    return create


@pytest.mark.parametrize(
    ("command", "op"),
    [
        (("connection", "profiles"), "knowledge.source.mcp_profiles"),
        (("connection", "connect"), "knowledge.connection.mcp_connect"),
        (("connection", "list"), "knowledge.connection.mcp_list"),
        (("connection", "inspect"), "knowledge.connection.mcp_inspect"),
        (("connection", "discover"), "knowledge.connection.mcp_discover"),
        (("connection", "browse"), "knowledge.connection.mcp_browse"),
        (("connection", "disconnect"), "knowledge.connection.mcp_disconnect"),
        *(
            ((name,), "knowledge.source.mcp_" + name)
            for name in (
                "preview",
                "add",
                "update",
                "list",
                "inspect",
                "pause",
                "resume",
                "refresh",
                "read",
                "remove",
            )
        ),
        (("destination", "list"), "collection.list"),
        (("destination", "create"), "collection.create"),
        (("destination", "grant"), "grants.create"),
        (("destination", "grants"), "grants.list"),
        (("destination", "preview"), "knowledge.source.mcp_preview"),
        (("recording", "list"), "knowledge.source.list"),
        (("recording", "sync-now"), "knowledge.source.sync_now"),
        (("retained", "list"), "knowledge.source.list"),
        (("retained", "inspect"), "knowledge.source.inspect"),
        (("retained", "remove"), "knowledge.source.remove"),
    ],
)
def test_commands_select_exact_common_operation(tmp_path, command, op):
    args = arguments(tmp_path / "unused", *command)
    assert args.source_spec.op == op
    assert args.intent == "automatic"


@pytest.mark.parametrize("mode", ["live", "snapshot", "selected_refresh"])
@pytest.mark.parametrize("policy", ["owner_only", "collection_copy"])
def test_add_preserves_exact_selection_and_publication_fields(tmp_path, mode, policy):
    data = {
        "namespace": "col-destination",
        "slot": SLOT,
        "mode": mode,
        "reader_policy": policy,
        "resource_ids": ["resource:one", "resource:two"],
        "request_id": REQUEST_ID,
        "label": "Selected documents",
        "confirm_copy_authority": True,
    }
    request = tmp_path / "request.json"
    request.write_text(json.dumps(data))
    args = arguments(tmp_path / "unused", "add", "--request-file", str(request))
    assert cli._request(args, args.source_spec) == {"org": "test-org", **data}


def test_update_preserves_revision_without_converting_it(tmp_path):
    data = {
        "namespace": "col-destination",
        "name": "opaque",
        "if_scope_revision": 7,
        "resource_ids": ["resource:two"],
        "mode": "selected_refresh",
        "label": None,
        "confirm_copy_authority": False,
    }
    request = tmp_path / "request.json"
    request.write_text(json.dumps(data))
    args = arguments(tmp_path / "unused", "update", "--request-file", str(request))
    assert cli._request(args, args.source_spec) == {"org": "test-org", **data}


@pytest.mark.parametrize(
    "data",
    [
        {"links": ["https://example.invalid/private"]},
        {"items": [{"id": "private"}]},
        {"include": ["pdf"]},
        {"bearer": "private-secret"},
        {"credential": "private-secret"},
        {"request_id": "not-the-original-uuid"},
    ],
)
def test_native_fields_and_argv_secrets_are_rejected_without_echo(tmp_path, data):
    request = tmp_path / "request.json"
    request.write_text(json.dumps(data))
    args = arguments(tmp_path / "unused", "add", "--request-file", str(request))
    with pytest.raises(cli.InputError) as caught:
        cli._request(args, args.source_spec)
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("flag", ["--link", "--item-json", "--include", "--token", "--bearer"])
def test_removed_flags_have_no_parser_path(tmp_path, flag):
    with pytest.raises(SystemExit) as caught:
        arguments(tmp_path / "unused", "add", flag, "value")
    assert caught.value.code == 2


def test_connect_rejects_abbreviated_secret_flags(tmp_path):
    with pytest.raises(SystemExit) as caught:
        arguments(tmp_path / "unused", "connection", "connect", "--bearer", "value")
    assert caught.value.code == 2


def test_recording_add_keeps_config_and_private_credential(tmp_path, monkeypatch):
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "namespace": "col-recordings",
                "name": "notes",
                "config": {"provider": "zoom", "ids": ["meeting"]},
                "dry_run": True,
            }
        )
    )
    secret = tmp_path / "credential"
    secret.write_text("private-credential\n")
    secret.chmod(0o600)
    monkeypatch.setenv("LOOM_API_KEY", "explicit-environment-token")
    args = arguments(
        tmp_path / "unused",
        "recording",
        "add",
        "--request-file",
        str(request),
        "--credential-file",
        str(secret),
    )
    body = cli._request(args, args.source_spec)
    assert cli._credentials(args, args.source_spec, body) == "explicit-environment-token"
    assert body == {
        "org": "test-org",
        "namespace": "col-recordings",
        "name": "notes",
        "kind": "recording",
        "config": {"provider": "zoom", "ids": ["meeting"]},
        "dry_run": True,
        "credential": "private-credential",
    }


@pytest.mark.parametrize("kind", ["gdrive", "notion", "linear"])
def test_recording_cannot_register_native_documents(tmp_path, kind):
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps({"namespace": "col-destination", "name": "native", "config": {}, "kind": kind})
    )
    args = arguments(
        tmp_path / "unused",
        "recording",
        "add",
        "--request-file",
        str(path),
        "--credential-file",
        "-",
    )
    with pytest.raises(cli.InputError, match="Only recording"):
        cli._request(args, args.source_spec)


def test_private_read_survives_reopen_and_blocks_other_namespace_write(tmp_path):
    family, path = pointer(tmp_path)
    seen = []
    opened = []

    def respond(request):
        seen.append(request)
        assert request.url.path == "/v1/knowledge.source.mcp_read"
        assert request.headers.get_list("x-loom-session-protection") == ["v1"]
        return httpx.Response(200, json={"mirrored": True, "text": "private-content"})

    async def scenario():
        out = await cli.call_source(
            cli._COMMANDS["read"],
            {
                "org": "test-org",
                "namespace": "col-private",
                "name": "source",
                "resource_id": "resource:one",
            },
            server_url="https://loom.invalid",
            token="token",
            family=cli.open_session(str(path)),
            automatic=False,
            transport_factory=factory(respond, opened),
        )
        assert out["text"] == "private-content"
        restored = cli.open_session(str(path))
        assert family.protected and restored.protected
        with pytest.raises(AutomaticCaptureSuppressed):
            await cli.call_source(
                cli._DESTINATION_COMMANDS["grant"],
                {"org": "other-org", "namespace": "col-team", "subject": "other", "role": "writer"},
                server_url="https://other.invalid",
                token="changed-token",
                family=restored,
                transport_factory=factory(respond, opened),
            )

    asyncio.run(scenario())
    assert len(seen) == len(opened) == 1
    assert opened[0]._client.is_closed


@pytest.mark.parametrize("status", [429, 500, 503])
def test_uncertain_connect_sends_original_uuid_once_and_closes(tmp_path, status):
    family, _ = pointer(tmp_path)
    seen = []
    opened = []
    body = {"org": "test-org", "profile_id": "approved", "request_id": REQUEST_ID}

    def respond(request):
        seen.append(request)
        assert request.headers["x-request-id"] == REQUEST_ID
        assert json.loads(request.content) == body
        return httpx.Response(status, json={"detail": {"code": "TEST_FAILURE"}})

    async def scenario():
        with pytest.raises(LoomError):
            await cli.call_source(
                cli._CONNECTION_COMMANDS["connect"],
                body,
                server_url="https://loom.invalid",
                token="token",
                family=family,
                transport_factory=factory(respond, opened),
            )

    asyncio.run(scenario())
    assert len(seen) == 1
    assert opened[0]._client.is_closed


@pytest.mark.parametrize("target", ["connect", "add"])
def test_reconciliation_preserves_original_uuid(tmp_path, target):
    body = {"request_id": REQUEST_ID, "namespace": "col-selected", "slot": SLOT}
    out = io.StringIO()
    command = cli._CONNECTION_COMMANDS["connect"] if target == "connect" else cli._COMMANDS["add"]
    cli._reconcile(command, body, out)
    assert REQUEST_ID in out.getvalue()
    assert "inspect" in out.getvalue()
    if target == "add":
        assert "namespace, slot" in out.getvalue()


def test_pointer_is_exclusive_private_and_reopens_same_family(tmp_path):
    family, path = pointer(tmp_path)
    assert cli.open_session(str(path)).pointer() == family.pointer()
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        cli.save_session_pointer(path, SessionProtection.create(base=tmp_path / "other"))
    assert cli.open_session(str(path)).pointer() == family.pointer()


@pytest.mark.parametrize("damage", ["missing", "public", "symlink", "malformed", "lost-proof"])
def test_restore_damage_refuses_without_new_family(tmp_path, damage):
    family, path = pointer(tmp_path)
    if damage == "missing":
        path.unlink()
    elif damage == "public":
        path.chmod(0o644)
    elif damage == "symlink":
        original = path.with_name("original")
        path.rename(original)
        path.symlink_to(original)
    elif damage == "malformed":
        path.write_text('{"version":1,"pointer":{}}')
    else:
        (family.base / family.id / "state").unlink()
    before = sorted(item.name for item in family.base.iterdir())
    with pytest.raises((OSError, cli.InputError, SessionProtectionUnavailable)):
        cli.open_session(str(path))
    assert sorted(item.name for item in family.base.iterdir()) == before


@pytest.mark.parametrize("raw", ['{"limit":1,"limit":2}', "[]", '{"secret":'])
def test_malformed_and_duplicate_json_never_echoes_body(raw):
    with pytest.raises(cli.InputError) as caught:
        cli._json_object(raw)
    assert "secret" not in str(caught.value)


def test_bounded_requests_and_private_secret_file(tmp_path):
    path = tmp_path / "input"
    path.write_bytes(b"x" * (cli.MAX_REQUEST_BYTES + 1))
    with pytest.raises(cli.InputError, match="byte limit"):
        cli._read_input(str(path), cli.MAX_REQUEST_BYTES)
    path.write_text("secret")
    path.chmod(0o644)
    with pytest.raises(cli.InputError, match="private regular file"):
        cli._read_input(str(path), cli.MAX_SECRET_BYTES, private=True)


def test_conflicting_namespace_cannot_redirect_selection(tmp_path):
    path = tmp_path / "request.json"
    path.write_text('{"namespace":"col-original"}')
    args = arguments(
        tmp_path / "unused", "list", "--namespace", "col-other", "--request-file", str(path)
    )
    with pytest.raises(cli.InputError, match="conflicts"):
        cli._request(args, args.source_spec)


def test_private_and_team_destinations_use_ordinary_collections(tmp_path):
    for extra in ({}, {"parent": "col-team"}):
        path = tmp_path / "request.json"
        path.write_text(json.dumps({"namespace": "col-notes", "title": "Notes", **extra}))
        args = arguments(tmp_path / "unused", "destination", "create", "--request-file", str(path))
        assert cli._request(args, args.source_spec) == {
            "org": "test-org",
            "namespace": "col-notes",
            "title": "Notes",
            **extra,
        }
    preview = arguments(tmp_path / "unused", "destination", "preview")
    assert preview.source_spec is cli._COMMANDS["preview"]


def _mock_http(monkeypatch, respond):
    client = httpx.AsyncClient

    def create(*args, **kwargs):
        return client(*args, transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", create)


def _argv(path, *command):
    return [
        "source",
        *command,
        "--server-url",
        "https://loom.invalid",
        "--org",
        "test-org",
        "--session-file",
        str(path),
    ]


def test_actual_cli_read_then_automatic_grant_uses_same_restored_proof(
    tmp_path, monkeypatch, capsys
):
    family, path = pointer(tmp_path)
    monkeypatch.setenv(CONTEXT_ENV, session_environment(str(path))[CONTEXT_ENV])
    monkeypatch.setenv("LOOM_API_KEY", "test-token")
    seen = []

    def respond(request):
        seen.append(request)
        assert request.url.path == "/v1/knowledge.source.mcp_read"
        assert request.headers.get_list("x-loom-session-protection") == ["v1"]
        return httpx.Response(200, json={"mirrored": True, "text": "private-content"})

    _mock_http(monkeypatch, respond)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"namespace": "col-private", "name": "opaque", "resource_id": "r:one"})
    )
    assert (
        cli.main(_argv(path, "read", "--request-file", str(request), "--intent", "explicit")) == 0
    )
    assert json.loads(capsys.readouterr().out)["text"] == "private-content"
    request.write_text(
        json.dumps({"namespace": "col-other", "subject": "another", "role": "viewer"})
    )
    assert cli.main(_argv(path, "destination", "grant", "--request-file", str(request))) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "private-content" not in output.err
    assert "Session protection refused" in output.err
    assert len(seen) == 1 and family.protected


def test_stdin_inputs_cannot_share_one_stream(tmp_path, capsys):
    _, path = pointer(tmp_path)
    assert (
        cli.main(_argv(path, "recording", "add", "--request-file", "-", "--credential-file", "-"))
        == 2
    )
    assert "Only one input" in capsys.readouterr().err


def test_cli_missing_pointer_refuses_before_any_transport(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("No request can begin without restore proof")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    assert (
        cli.main(
            _argv(
                tmp_path / "missing",
                "list",
                "--namespace",
                "col-destination",
                "--intent",
                "explicit",
            )
        )
        == 2
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "unavailable" in output.err
    assert not (tmp_path / "missing").exists()


def test_cli_reconciles_connect_by_original_uuid_without_resubmitting(
    tmp_path, monkeypatch, capsys
):
    _, path = pointer(tmp_path)
    monkeypatch.setenv(CONTEXT_ENV, session_environment(str(path))[CONTEXT_ENV])
    monkeypatch.setenv("LOOM_API_KEY", "test-token")
    seen = []

    def respond(request):
        seen.append(request)
        if len(seen) == 1:
            assert request.headers["x-request-id"] == REQUEST_ID
            return httpx.Response(
                503, json={"detail": {"code": "TEST_FAILURE", "message": "private-diagnostic"}}
            )
        assert request.url.path == "/v1/knowledge.connection.mcp_inspect"
        assert json.loads(request.content) == {"org": "test-org", "request_id": REQUEST_ID}
        return httpx.Response(200, json={"found": True, "reconciliation": "present", "slot": SLOT})

    _mock_http(monkeypatch, respond)
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"profile_id": "approved", "request_id": REQUEST_ID}))
    assert cli.main(_argv(path, "connection", "connect", "--request-file", str(request))) == 1
    output = capsys.readouterr()
    assert len(seen) == 1 and output.out == ""
    assert REQUEST_ID in output.err and "private-diagnostic" not in output.err
    request.write_text(json.dumps({"request_id": REQUEST_ID}))
    assert cli.main(_argv(path, "connection", "inspect", "--request-file", str(request))) == 0
    assert json.loads(capsys.readouterr().out)["reconciliation"] == "present"
    assert len(seen) == 2


@pytest.mark.parametrize("request_id", ["", "not-a-uuid", None, 1])
def test_connect_requires_original_uuid_without_generating_one(tmp_path, request_id):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"profile_id": "approved", "request_id": request_id}))
    args = arguments(tmp_path / "unused", "connection", "connect", "--request-file", str(request))
    with pytest.raises(cli.InputError, match="original canonical UUID"):
        cli._request(args, args.source_spec)


@pytest.mark.parametrize("raw", ['{"limit":NaN}', '{"limit":Infinity}', '{"limit":-Infinity}'])
def test_nonfinite_values_are_not_json_inputs(raw):
    with pytest.raises(cli.InputError):
        cli._json_object(raw)


def test_private_error_status_is_observed_before_error_release(tmp_path):
    family, _ = pointer(tmp_path)
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(403, json={"mirrored": True, "detail": {"code": "ACCESS_DENIED"}})

    async def scenario():
        with pytest.raises(LoomError):
            await cli.call_source(
                cli._COMMANDS["read"],
                {"org": "test-org"},
                server_url="https://loom.invalid",
                token="token",
                family=family,
                automatic=False,
                transport_factory=factory(respond),
            )
        assert family.protected
        with pytest.raises(AutomaticCaptureSuppressed):
            await cli.call_source(
                cli._RECORDING_COMMANDS["add"],
                {"org": "other-org", "namespace": "col-recordings"},
                server_url="https://other.invalid",
                token="changed-token",
                family=family,
                transport_factory=factory(respond),
            )

    asyncio.run(scenario())
    assert len(seen) == 1
