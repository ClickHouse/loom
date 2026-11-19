"""Native fixture failures expose useful metadata without child credentials."""

from __future__ import annotations

import ast
import subprocess

import pytest

from tests.integration import _cell_each_fleet as fleet


def test_failed_child_reports_only_allowed_metadata(monkeypatch):
    secrets = [
        "postgres://operator:private-password@private.example/private_database",
        "Bearer private-bearer-token",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwcml2YXRlIn0.private-signature",
        "password=private-password",
        "001_private_secret.sql",
    ]
    result = subprocess.CompletedProcess(
        args=["python", "-m", "server.core.schema.migrate"],
        returncode=1,
        stdout=(
            "x" * 10000
            + "\n  → 000_migrations.sql\n  ✓ 001_private_secret.sql\n"
            + "\n".join(secrets)
        ),
        stderr="\n".join(secrets) + "\nDatabaseError: Code: 60. refused\nPrivateSecretError",
    )
    monkeypatch.setattr(fleet.subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(AssertionError) as caught:
        fleet.child(["server.core.schema.migrate"], {})
    message = str(caught.value)
    prefix = "Private fixture command ['server.core.schema.migrate'] failed: "
    assert message.startswith(prefix)
    assert ast.literal_eval(message.removeprefix(prefix)) == {
        "exit": 1,
        "migration": ["000_migrations.sql"],
        "error_kinds": ["DatabaseError"],
        "clickhouse_codes": ["60"],
    }
    assert all(secret not in message for secret in secrets)
    assert "PrivateSecretError" not in message
    assert len(message) < 400


def test_successful_child_returns_the_original_result(monkeypatch):
    result = subprocess.CompletedProcess(args=["python"], returncode=0, stdout="value", stderr="")
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(fleet.subprocess, "run", run)
    assert fleet.child(["server.core.schema.migrate"], {}) is result
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 240
    assert calls[0][1]["capture_output"] is True
