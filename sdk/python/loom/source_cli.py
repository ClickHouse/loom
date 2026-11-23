"""Installed-SDK source CLI; all requests use retained session protection."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO
from uuid import UUID

from loom.cli_context import CONTEXT_ENV, ContextUnavailable, open_context, request_scope, run_sync
from loom.errors import LoomError
from loom.retry import RetryPolicy
from loom.session_protection import (
    AutomaticCaptureSuppressed,
    SessionProtection,
    SessionProtectionUnavailable,
)
from loom.transport import Transport

SOURCE_CLI_API = 2
MAX_REQUEST_BYTES = 1024 * 1024
MAX_SECRET_BYTES = 64 * 1024
MAX_POINTER_BYTES = 4096
_LOCAL_FAILURE = "Local input or session authority is unavailable; no reset or retry was attempted."
_PROTECTION_FAILURE = (
    "Session protection refused this operation; retain the existing context and restore its proof. "
    "No reset or retry was attempted."
)


@dataclass(frozen=True)
class Command:
    op: str
    fields: str = ""
    required: str = ""
    secret: str = ""


_PREVIEW_FIELDS = "namespace slot mode reader_policy resource_ids"
_SOURCE_FIELDS = "namespace name"
_COMMANDS: dict[str, Command] = {
    "preview": Command(
        "knowledge.source.mcp_preview", _PREVIEW_FIELDS, "namespace slot mode resource_ids"
    ),
    "add": Command(
        "knowledge.source.mcp_add",
        _PREVIEW_FIELDS + " request_id label confirm_copy_authority",
        "namespace slot mode resource_ids request_id",
    ),
    "update": Command(
        "knowledge.source.mcp_update",
        _SOURCE_FIELDS + " if_scope_revision resource_ids mode label confirm_copy_authority",
        _SOURCE_FIELDS + " resource_ids mode",
    ),
    "list": Command(
        "knowledge.source.mcp_list", "namespace cursor limit include_retired", "namespace"
    ),
    "inspect": Command(
        "knowledge.source.mcp_inspect", "namespace name slot request_id", "namespace"
    ),
    "pause": Command("knowledge.source.mcp_pause", _SOURCE_FIELDS, _SOURCE_FIELDS),
    "resume": Command("knowledge.source.mcp_resume", _SOURCE_FIELDS, _SOURCE_FIELDS),
    "refresh": Command("knowledge.source.mcp_refresh", _SOURCE_FIELDS, _SOURCE_FIELDS),
    "read": Command(
        "knowledge.source.mcp_read",
        _SOURCE_FIELDS + " resource_id",
        _SOURCE_FIELDS + " resource_id",
    ),
    "remove": Command(
        "knowledge.source.mcp_remove",
        _SOURCE_FIELDS + " confirm delete_documents",
        _SOURCE_FIELDS + " confirm",
    ),
}
_CONNECTION_COMMANDS = {
    "profiles": Command("knowledge.source.mcp_profiles", "query cursor limit"),
    "connect": Command(
        "knowledge.connection.mcp_connect",
        "profile_id request_id label",
        "profile_id request_id",
        "bearer",
    ),
    "list": Command("knowledge.connection.mcp_list", "cursor limit"),
    "inspect": Command("knowledge.connection.mcp_inspect", "slot request_id"),
    "discover": Command("knowledge.connection.mcp_discover", "slot", "slot"),
    "browse": Command("knowledge.connection.mcp_browse", "slot mode query limit", "slot mode"),
    "disconnect": Command("knowledge.connection.mcp_disconnect", "slot", "slot"),
}
_DESTINATION_COMMANDS = {
    "list": Command("collection.list", "limit include_document_counts cursor"),
    "create": Command("collection.create", "namespace title derive_name parent description"),
    "grants": Command("grants.list", "namespace limit cursor fields", "namespace"),
    "grant": Command(
        "grants.create", "namespace subject role granted_by replace", "namespace subject role"
    ),
    "preview": _COMMANDS["preview"],
}
_RECORDING_COMMANDS = {
    "add": Command(
        "knowledge.source.add",
        "namespace name kind config dry_run",
        "namespace name config",
        "credential",
    ),
    "list": Command("knowledge.source.list", "namespace", "namespace"),
    "sync-now": Command("knowledge.source.sync_now", _SOURCE_FIELDS + " full", _SOURCE_FIELDS),
}
_RETAINED_COMMANDS = {
    "list": Command("knowledge.source.list", "namespace", "namespace"),
    "inspect": Command("knowledge.source.inspect", _SOURCE_FIELDS, _SOURCE_FIELDS),
    "remove": Command(
        "knowledge.source.remove",
        _SOURCE_FIELDS + " confirm delete_documents",
        _SOURCE_FIELDS + " confirm",
    ),
}


class InputError(ValueError):
    """A local input failure whose message contains no request or credential values."""


def _private_file(fd: int) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise InputError("Use an owned private regular file (mode 0600), without links.")


@contextlib.contextmanager
def _pointer_directory(path: Path) -> Iterator[int]:
    if os.name != "posix" or not path.is_absolute():
        raise InputError("Session pointers require an absolute path in private POSIX storage.")
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise InputError("The session pointer directory must be owned and mode 0700.")
        yield directory
    finally:
        os.close(directory)


def _read_input(path: str, maximum: int, *, private: bool = False) -> str:
    if path == "-":
        raw = sys.stdin.buffer.read(maximum + 1)
    else:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if private:
                _private_file(stream.fileno())
            elif not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise InputError("Request input must be a regular file or stdin.")
            raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise InputError("Input exceeds its byte limit.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise InputError("Input must be UTF-8 text.") from None


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise InputError("JSON input contains duplicate fields.")
        value[key] = item
    return value


def _json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_object, parse_constant=_nonfinite)
    except (ValueError, RecursionError):
        raise InputError("Input must contain one JSON object without duplicate fields.") from None
    if not isinstance(value, dict):
        raise InputError("Input must contain one JSON object.")
    return value


def _nonfinite(value: str) -> Any:
    raise InputError("JSON input cannot contain nonfinite numbers.")


def save_session_pointer(path: str | Path, family: SessionProtection) -> None:
    """Application provisioning only: persist once, never replace a resumed pointer."""
    family.require_ready()
    raw = json.dumps({"version": 1, "pointer": family.pointer()}).encode()
    with _pointer_directory(Path(path)) as directory:
        fd = os.open(
            Path(path).name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)
        except OSError:
            # Leave incomplete authority in place; restore must refuse instead of resetting it.
            raise InputError(
                "Session pointer persistence failed; restore is unavailable."
            ) from None


def open_session(path: str) -> SessionProtection:
    """Reopen only existing application-owned authority; do not create or repair it."""
    with _pointer_directory(Path(path)) as directory:
        fd = os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            _private_file(stream.fileno())
            raw = stream.read(MAX_POINTER_BYTES + 1)
    if len(raw) > MAX_POINTER_BYTES:
        raise InputError("Session pointer exceeds its byte limit.")
    try:
        value = _json_object(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise InputError("Session pointer is invalid.") from None
    if (
        set(value) != {"version", "pointer"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["pointer"], dict)
    ):
        raise InputError("Session pointer is invalid.")
    family = SessionProtection.open(value["pointer"])
    family.require_ready()
    return family


def _common(parser: argparse.ArgumentParser, command: Command) -> None:
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--org", required=True)
    if "namespace" in command.fields.split():
        parser.add_argument(
            "--namespace", help="Ordinary destination; must agree with the JSON field."
        )
    parser.add_argument(
        "--request-file",
        help="UTF-8 JSON object, at most 1 MiB; - reads stdin. API fields: "
        + (command.fields or "none"),
    )
    parser.add_argument(
        "--token-file", help="Owned mode-0600 token file or - for stdin; otherwise LOOM_API_KEY."
    )
    parser.add_argument(
        "--session-file",
        required=True,
        help="Existing application-owned private session pointer; "
        "never initialized by this command.",
    )
    parser.add_argument(
        "--intent",
        choices=("automatic", "explicit"),
        default="automatic",
        help="Default automatic guards every call; explicit is a direct user action.",
    )
    if command.secret:
        parser.add_argument(
            "--" + command.secret + "-file",
            required=command.secret == "credential",
            help="Owned mode-0600 credential file or - for stdin. Never an argv secret.",
        )
    parser.set_defaults(func=run_command, source_spec=command)


def _commands(sub: Any, commands: dict[str, Command]) -> None:
    for name, command in commands.items():
        parser = sub.add_parser(
            name,
            allow_abbrev=False,
            help=command.op,
            description=command.op
            + ". No automatic retries; pagination and reconciliation are explicit.",
        )
        _common(parser, command)


def register_source_cli(sub: Any) -> None:
    parser = sub.add_parser(
        "source", help="MCP sources, connections, collection destinations and recordings."
    )
    actions = parser.add_subparsers(dest="source_command", required=True)
    _commands(actions, _COMMANDS)
    for name, commands in (
        ("connection", _CONNECTION_COMMANDS),
        ("destination", _DESTINATION_COMMANDS),
        ("recording", _RECORDING_COMMANDS),
        ("retained", _RETAINED_COMMANDS),
    ):
        group = actions.add_parser(name)
        _commands(group.add_subparsers(dest="source_action", required=True), commands)


def run_server_entry(args: argparse.Namespace) -> int:
    """Versioned execution interface for the separately packaged server parser."""
    groups = {
        "connection": _CONNECTION_COMMANDS,
        "destination": _DESTINATION_COMMANDS,
        "recording": _RECORDING_COMMANDS,
        "retained": _RETAINED_COMMANDS,
    }
    if args.source_command in groups:
        args.source_spec = groups[args.source_command][args.source_action]
    else:
        args.source_spec = _COMMANDS[args.source_command]
    return run_command(args)


def _uuid(value: Any) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise InputError(
            "request_id must be the original canonical UUID; the CLI never invents a replacement."
        ) from None


def _request(args: argparse.Namespace, command: Command) -> dict[str, Any]:
    body = (
        _json_object(_read_input(args.request_file, MAX_REQUEST_BYTES)) if args.request_file else {}
    )
    if set(body) - set(command.fields.split()):
        raise InputError(
            "Unexpected request fields; use the API field names in command help. "
            "Credentials require a separate private file or stdin."
        )
    namespace = getattr(args, "namespace", None)
    if namespace is not None:
        if "namespace" in body and body["namespace"] != namespace:
            raise InputError("--namespace conflicts with the request's namespace field.")
        body["namespace"] = namespace
    if any(field not in body for field in command.required.split()):
        raise InputError("Required API fields are missing; see command help.")
    if "request_id" in body and body["request_id"] != "":
        _uuid(body["request_id"])
    if command.op in ("knowledge.connection.mcp_connect", "knowledge.source.mcp_add"):
        _uuid(body["request_id"])
    if command.op == "knowledge.source.add":
        if body.get("kind", "recording") != "recording":
            raise InputError(
                "Only recording is supported here; document sources use MCP source add."
            )
        body["kind"] = "recording"
    return {"org": args.org, **body}


def _credentials(args: argparse.Namespace, command: Command, body: dict[str, Any]) -> str:
    secret_path = getattr(args, command.secret + "_file", None) if command.secret else None
    token = (
        _read_input(args.token_file, MAX_SECRET_BYTES, private=True).strip()
        if args.token_file
        else os.environ.get("LOOM_API_KEY", "").strip()
    )
    if not token:
        raise InputError(
            "Supply --token-file or LOOM_API_KEY; saved human credentials are not borrowed."
        )
    if len(token.encode("utf-8")) > MAX_SECRET_BYTES:
        raise InputError("Token input exceeds its byte limit.")
    if secret_path:
        secret = _read_input(secret_path, MAX_SECRET_BYTES, private=True).strip()
        if not secret:
            raise InputError("The credential input is empty.")
        body[command.secret] = secret
    return token


async def call_source(
    command: Command,
    body: dict[str, Any],
    *,
    server_url: str,
    token: str,
    family: SessionProtection,
    automatic: bool = True,
    transport_factory: Callable[..., Transport] = Transport,
) -> dict[str, Any]:
    """One observed dispatch; callers retain the same family across tasks and scopes."""
    family.require_ready()
    with request_scope(family, "POST", "/v1/" + command.op, explicit=not automatic):
        transport = transport_factory(
            server_url,
            api_key=token,
            session_protection=family,
            retry=RetryPolicy(max_attempts=1),
            pacing=False,
            ratelimit_max_wait_s=0,
        )
        try:
            result = await transport.post_with_meta(
                "/v1/" + command.op, body, request_id=body.get("request_id")
            )
            return result.data
        finally:
            await transport.close()


def _reconcile(command: Command, body: dict[str, Any], err: TextIO) -> None:
    request_id = body.get("request_id")
    if command.op == "knowledge.connection.mcp_connect" and request_id:
        print(
            "Reconcile with connection inspect using the original request_id="
            + request_id
            + "; do not reconnect with a new UUID.",
            file=err,
        )
    elif command.op == "knowledge.source.mcp_add" and request_id:
        print(
            "Reconcile with source inspect using the original namespace, slot and request_id="
            + request_id
            + "; do not add with a new UUID.",
            file=err,
        )


def _inputs(
    args: argparse.Namespace, command: Command
) -> tuple[SessionProtection, dict[str, Any], str]:
    secret_path = getattr(args, command.secret + "_file", None) if command.secret else None
    if [args.request_file, args.token_file, secret_path].count("-") > 1:
        raise InputError("Only one input can consume stdin.")
    capsule = os.environ.get(CONTEXT_ENV)
    if capsule is None and args.intent == "automatic":
        raise InputError(
            "Automatic source commands require inherited LOOM_SOURCE_SESSION_CONTEXT "
            "matching --session-file. Retain the trusted launcher context across commands."
        )
    family = (
        open_context(capsule, session_file=args.session_file)
        if capsule is not None
        else open_session(args.session_file)
    )
    body = _request(args, command)
    return family, body, _credentials(args, command, body)


def _request_failure(exc: LoomError, command: Command, body: dict[str, Any]) -> int:
    code = (
        exc.code
        if isinstance(exc.code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", exc.code)
        else "REQUEST_FAILED"
    )
    print("Source request failed: " + code + ". No retry was attempted.", file=sys.stderr)
    _reconcile(command, body, sys.stderr)
    return 1


def run_command(args: argparse.Namespace) -> int:
    command: Command = args.source_spec
    body: dict[str, Any] = {}
    try:
        family, body, token = _inputs(args, command)
        out = run_sync(
            lambda: call_source(
                command,
                body,
                server_url=args.server_url,
                token=token,
                family=family,
                automatic=args.intent == "automatic",
            )
        )
    except (InputError, ContextUnavailable) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, RecursionError):
        print(_LOCAL_FAILURE, file=sys.stderr)
        _reconcile(command, body, sys.stderr)
        return 2
    except (SessionProtectionUnavailable, AutomaticCaptureSuppressed):
        print(_PROTECTION_FAILURE, file=sys.stderr)
        _reconcile(command, body, sys.stderr)
        return 2
    except LoomError as exc:
        return _request_failure(exc, command, body)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m loom.source_cli")
    register_source_cli(parser.add_subparsers(required=True))
    try:
        args = parser.parse_args(argv)
        return int(args.func(args))
    except KeyboardInterrupt:
        print(
            "Interrupted; retain the original request UUID and session proof for reconciliation.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
