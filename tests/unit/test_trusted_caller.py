"""LOOM_TRUSTED_CALLERS + X-Loom-On-Behalf-Of impersonation.

The mechanism: when an authenticated request's JWT subject is on the
operator-curated LOOM_TRUSTED_CALLERS allowlist AND the request carries
an `X-Loom-On-Behalf-Of` header, the HTTP transport substitutes the
effective subject before handler dispatch. Grants resolution + memory
writes downstream see the impersonated identity; the audit trail
records the actual caller as `impersonated_by`.

These tests lock in the contract without needing a live server.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import server.auth.bearer as bearer_mod

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


class _FakeRequest:
    """Minimal duck-type for the bits of Request _extract_auth touches."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


def _auth(subject: str = "sa:gateway"):
    from server.auth.jwt import AuthInfo, Role

    return AuthInfo(
        subject=subject,
        org="clickhouse",
        namespaces=("*",),
        roles=(Role.WRITER,),
        is_service_account=True,
    )


def test_extract_auth_honors_obo_header_for_trusted_caller() -> None:
    """sa:gateway is on LOOM_TRUSTED_CALLERS → header replaces subject."""
    from server.transport import http as http_mod

    req = _FakeRequest(
        {
            "Authorization": "Bearer fake-token",
            "X-Loom-On-Behalf-Of": "bob@example.com",
        }
    )

    with (
        patch.object(bearer_mod, "verify_token", return_value=_auth("sa:gateway")),
        patch.object(http_mod.settings, "trusted_callers", "sa:gateway,sa:other"),
    ):
        out = http_mod._extract_auth(req)  # type: ignore[arg-type]

    assert out is not None
    assert out.subject == "bob@example.com"
    assert out.impersonated_by == "sa:gateway"


def test_extract_auth_ignores_obo_when_caller_not_trusted() -> None:
    """sa:imposter not in LOOM_TRUSTED_CALLERS → header is ignored."""
    from server.transport import http as http_mod

    req = _FakeRequest(
        {
            "Authorization": "Bearer fake-token",
            "X-Loom-On-Behalf-Of": "bob@example.com",
        }
    )

    with (
        patch.object(bearer_mod, "verify_token", return_value=_auth("sa:imposter")),
        patch.object(http_mod.settings, "trusted_callers", "sa:gateway"),
    ):
        out = http_mod._extract_auth(req)  # type: ignore[arg-type]

    assert out is not None
    assert out.subject == "sa:imposter"  # unchanged
    assert out.impersonated_by == ""


def test_extract_auth_ignores_obo_when_no_trusted_callers_configured() -> None:
    """Empty LOOM_TRUSTED_CALLERS → impersonation feature is off entirely."""
    from server.transport import http as http_mod

    req = _FakeRequest(
        {
            "Authorization": "Bearer fake-token",
            "X-Loom-On-Behalf-Of": "bob@example.com",
        }
    )

    with (
        patch.object(bearer_mod, "verify_token", return_value=_auth("sa:gateway")),
        patch.object(http_mod.settings, "trusted_callers", ""),
    ):
        out = http_mod._extract_auth(req)  # type: ignore[arg-type]

    assert out is not None
    assert out.subject == "sa:gateway"
    assert out.impersonated_by == ""


def test_extract_auth_trusted_caller_without_obo_header_is_noop() -> None:
    """Trusted caller making a normal request (no impersonation) — works as
    today; subject stays the JWT's actual sub."""
    from server.transport import http as http_mod

    req = _FakeRequest({"Authorization": "Bearer fake-token"})

    with (
        patch.object(bearer_mod, "verify_token", return_value=_auth("sa:gateway")),
        patch.object(http_mod.settings, "trusted_callers", "sa:gateway"),
    ):
        out = http_mod._extract_auth(req)  # type: ignore[arg-type]

    assert out is not None
    assert out.subject == "sa:gateway"
    assert out.impersonated_by == ""


def test_agent_event_carries_impersonated_by() -> None:
    """The audit row serializes the field for the agent_events column."""
    from datetime import UTC, datetime

    from server.observability.events import AgentEvent

    e = AgentEvent(
        ts=datetime.now(UTC),
        org="o",
        namespace="n",
        agent="a",
        user_id="bob@example.com",
        session_id="-",
        operation="memory.set",
        request_id="r",
        latency_ms=10,
        success=True,
        impersonated_by="sa:gateway",
    )
    row = e.to_row()
    assert row["impersonated_by"] == "sa:gateway"
    assert row["user_id"] == "bob@example.com"
