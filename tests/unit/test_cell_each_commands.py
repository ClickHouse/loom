"""Only the closed org-free command shapes become fixed administration requests."""

from __future__ import annotations

import hashlib
import importlib
import json
from typing import Any

import pytest


def _parse(argv: list[str]) -> Any:
    return importlib.import_module("server.cli.cell_each").parse_command(argv)


@pytest.mark.parametrize(
    ("argv", "action", "arguments", "mutating"),
    [
        (["settings", "list"], "settings_list", {"prefix": ""}, False),
        (
            ["settings", "list", "--prefix", "memory_"],
            "settings_list",
            {"prefix": "memory_"},
            False,
        ),
        (
            ["settings", "set", "memory_ask_recency_weight", "0.25"],
            "settings_set",
            {
                "name": "memory_ask_recency_weight",
                "value": "0.25",
                "clear": False,
                "expected": None,
            },
            True,
        ),
        (
            ["settings", "reset", "memory_ask_recency_weight", "--expect", "0.25"],
            "settings_set",
            {"name": "memory_ask_recency_weight", "value": None, "clear": True, "expected": "0.25"},
            True,
        ),
        (
            ["settings", "history", "memory_ask_recency_weight"],
            "settings_history",
            {"name": "memory_ask_recency_weight", "limit": 50},
            False,
        ),
        (["signing-key", "list"], "signing_keys_list", {}, False),
        (
            ["signing-key", "rotate"],
            "signing_keys_rotate",
            {"algorithm": "HS256", "activate_in": 300.0},
            True,
        ),
        (
            ["signing-key", "rotate", "--algorithm", "RS256", "--activate-in", "600"],
            "signing_keys_rotate",
            {"algorithm": "RS256", "activate_in": 600.0},
            True,
        ),
        (["signing-key", "retire", "key-1"], "signing_keys_retire", {"kid": "key-1"}, True),
        (
            ["revoke-token", "--subject", "sa:worker", "--reason", "retire"],
            "subject_revoke",
            {"subject": "sa:worker", "reason": "retire"},
            True,
        ),
        (
            ["revoke-token", "--subject", "person@example.com"],
            "subject_revoke",
            {"subject": "person@example.com", "reason": ""},
            True,
        ),
    ],
)
def test_closed_selector_preserves_cli_defaults_and_raw_setting_values(
    argv: list[str], action: str, arguments: dict[str, Any], mutating: bool
) -> None:
    command = _parse(argv)
    assert command.action == action
    assert command.path == f"/v1/admin/cell.{action}"
    assert command.arguments == arguments
    assert command.mutating is mutating
    expected = hashlib.sha256(
        json.dumps(
            {"action": action, "arguments": arguments}, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    assert command.digest == expected


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["call", "settings.set"],
        ["/v1/admin/settings.set"],
        ["sh", "-c", "true"],
        ["cell", "each", "--", "settings", "list"],
        ["settings", "history"],
        ["settings", "history", "x", "--limit", "51"],
        ["settings", "history", "x", "--limit", "0"],
        ["settings", "set", "x", "1", "--data", "@payload"],
        ["settings", "set", "x", "1", ";", "signing-key", "rotate"],
        ["signing-key", "record-root"],
        ["signing-key", "rotate", "--algorithm", "ES256"],
        ["signing-key", "rotate", "--activate-in", "nan"],
        ["signing-key", "rotate", "--activate-in", "inf"],
        ["signing-key", "rotate", "--activate-in", "1e9999"],
        ["revoke-token", "--subject", "acme/user:member"],
        ["revoke-token", "--subject", "sa:worker", "--org", "acme"],
        ["revoke-token", "--subject", "sa:worker", "--issued-before", "2030-01-01"],
        ["revoke-token", "--subject", "sa:worker", "--expires-at", "2030-01-01"],
        ["revoke-token", "--subject", "sa:worker", "--scim"],
        ["revoke-token", "--jti", "token-1"],
        ["revoke-token", "--list"],
        ["bot", "create", "worker"],
        ["migrate"],
    ],
)
def test_unsupported_command_never_becomes_an_action(argv: list[str]) -> None:
    with pytest.raises((ValueError, SystemExit)) as refused:
        _parse(argv)
    if isinstance(refused.value, SystemExit):
        assert refused.value.code != 0


def test_digest_keeps_the_normal_server_ascii_json_semantics() -> None:
    from server.core.hashing import sha256_value

    command = _parse(["revoke-token", "--subject", "sa:worker", "--reason", "clé"])
    assert command.digest == sha256_value(
        {"action": command.action, "arguments": command.arguments}
    )


def test_rejected_command_cannot_echo_untrusted_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = "untrusted-argument-should-not-print"
    with pytest.raises((ValueError, SystemExit)):
        _parse(["call", marker])
    captured = capsys.readouterr()
    assert marker not in captured.out + captured.err
