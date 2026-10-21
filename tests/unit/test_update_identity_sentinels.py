"""memory.update resolves its key with `agent` "*" / "-" and `user_id` /
`session_id` "-" read as unset, as memory.ask does: the SDKs' `Agent.call`
stamps "-" on every call, which must not narrow the key to rows written with
no user and no session."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.api.models_crud import MemoryUpdateInput
from server.memory.app import update as update_mod


async def _resolution_params(**identity: str) -> dict[str, Any]:
    seen: list[dict[str, Any]] = []

    class _CH:
        async def query_all(self, _sql: str, params: Any = None) -> list[dict[str, Any]]:
            seen.append(params)
            return [{"memory_id": "m"}]

    inp = MemoryUpdateInput(org="o", namespace="ns", memory_key="k", value="v", **identity)
    await update_mod._live_rows_for_key(inp, _CH())
    return seen[0]


@pytest.mark.parametrize(
    "identity",
    [
        {"agent": "-", "user_id": "-", "session_id": "-"},
        {"agent": "*", "user_id": "-", "session_id": "-"},
        {"agent": "", "user_id": "", "session_id": ""},
    ],
)
async def test_the_identity_sentinels_resolve_the_key_namespace_wide(identity):
    params = await _resolution_params(**identity)
    assert not {"agent", "user_id", "session_id"} & params.keys()


async def test_a_user_or_session_star_is_a_literal_value_as_in_ask():
    params = await _resolution_params(user_id="*", session_id="*")
    assert (params["user_id"], params["session_id"]) == ("*", "*")


async def test_a_named_identity_still_narrows_the_key():
    params = await _resolution_params(agent="writer", user_id="u1", session_id="s1")
    assert (params["agent"], params["user_id"], params["session_id"]) == ("writer", "u1", "s1")
