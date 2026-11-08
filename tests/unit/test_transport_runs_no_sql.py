"""The transport speaks protocol; the domains own their statements.

No string literal under server/transport reads a `loom.` table: the caller
defaults MCP picks read the identity plane through `identity/app`, and the
default a caller lands in is named the way first-contact provisioning names
it (`derive_personal_slug`).
"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SQL = re.compile(r"\bSELECT\b[\s\S]*\bFROM\b[\s\S]*\bloom\.|\bINSERT\s+INTO\s+loom\.")


def test_no_transport_module_holds_a_statement() -> None:
    hits = []
    for path in sorted((ROOT / "server" / "transport").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _SQL.search(node.value):
                    hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert hits == []


def test_a_subject_with_no_slug_defaults_to_the_provisioned_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth.jwt import AuthInfo
    from server.auth.personal_namespace import derive_personal_slug
    from server.identity.app import caller_defaults
    from server.operations.base import Role
    from server.settings import settings
    from server.transport import mcp_identity

    async def _none(*_a: Any, **_k: Any) -> str:
        return ""

    async def _no_grants(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def _aliases(org: str, subject: str, **_k: Any) -> list[str]:
        return [subject]

    async def _none_owner(*_a: Any, **_k: Any) -> None:
        return None

    async def _none_restricted(org: str) -> frozenset[str]:
        return frozenset()

    monkeypatch.setattr(settings, "identity_provider", "trust-network")
    monkeypatch.setattr(caller_defaults, "saved_default", _none)
    monkeypatch.setattr(caller_defaults, "ranked_grants", _no_grants)
    monkeypatch.setattr("server.auth.aliases.subject_aliases", _aliases)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())
    # Nobody owns the personal namespace yet: first contact provisions it.
    monkeypatch.setattr("server.auth.personal_namespace.namespace_owner", _none_owner)

    async def _not_in_use(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr("server.identity.app.directory.IdentityDirectory.in_use", _not_in_use)
    monkeypatch.setattr(
        "server.identity.app.namespace_classification.restricted_set", _none_restricted
    )
    auth = AuthInfo(
        subject="@@@", org="o", namespaces=(), roles=(Role.WRITER,), is_service_account=False
    )
    out = asyncio.run(mcp_identity._caller_defaults(auth, "claude-code"))
    assert out is not None and out["namespace"] == derive_personal_slug("@@@")
    assert out["namespace"] != "personal-you"
