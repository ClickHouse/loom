"""One reading of the `Authorization` header for every route and both
transports: blank is no credential, `Bearer <token>` is the token whatever
the scheme's case, and anything else is refused like a bad token."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import HTTPException

from server.auth.bearer import bearer_token, verify_bearer_header
from server.auth.jwt import TokenError

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("header", [None, "", "   "])
def test_a_blank_header_is_no_credential(header: str | None) -> None:
    assert bearer_token(header) is None
    assert verify_bearer_header(header) is None


@pytest.mark.parametrize("header", ["Bearer abc", "bearer abc", "BEARER  abc ", "  Bearer abc"])
def test_the_scheme_is_case_insensitive_and_the_token_trimmed(header: str) -> None:
    assert bearer_token(header) == "abc"


@pytest.mark.parametrize(
    "header", ["Basic dXNlcjpwYXNz", "Bearer", "Bearer   ", "Token abc", "abc"]
)
def test_anything_else_is_a_credential_loom_cannot_read(header: str) -> None:
    with pytest.raises(TokenError):
        bearer_token(header)
    with pytest.raises(HTTPException) as e:
        verify_bearer_header(header)
    assert e.value.status_code == 401 and e.value.detail["code"] == "AUTH_INVALID"


def test_no_module_parses_the_header_itself() -> None:
    """Only server/auth/bearer.py splits an Authorization header, so no copy
    can match the scheme case-sensitively or take an empty token as anonymous
    where the rest refuse it."""
    hits = []
    for path in (ROOT / "server").rglob("*.py"):
        if path.name == "bearer.py" and path.parent.name == "auth":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "startswith"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and str(node.args[0].value).lower().startswith("bearer")
            ):
                hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert hits == []
