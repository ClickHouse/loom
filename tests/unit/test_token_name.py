"""A token's name is checked before anything is minted or saved: a name holding a NUL
cannot be signed into a token, so it is a 422 on every route that names a token."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from server.identity.app import me_service_accounts as tokens
from server.settings import settings


@pytest.mark.parametrize("name", ["", "   ", "x" * 65, "ci\x00bot", "agent · x", "ci:review"])
def test_a_name_off_its_shape_is_a_422(name: str) -> None:
    with pytest.raises(HTTPException) as err:
        tokens.token_name(name)
    assert err.value.status_code == 422
    assert err.value.detail["code"] == "VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_service_accounts_create_refuses_a_nul_in_the_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    request: Any = SimpleNamespace(headers={})
    body = {"name": "ci\x00bot", "namespaces": ["support"]}
    with pytest.raises(HTTPException) as err:
        await tokens.me_service_accounts_create(request, body)
    assert err.value.status_code == 422
    assert err.value.detail["code"] == "VALIDATION_FAILED"
