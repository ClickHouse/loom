"""Saved-message search over HTTP with a token pinned to one end user
(`read_scope=own_user`, `read_user_id`): the integration serving many users
through one credential reaches only the pinned user's saved messages.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from tests.integration._resources import unique_org

_CONTENT = "the renewal clause caps the price increase at four percent"


def _mint(org: str, ns: str, **claims: Any) -> str:
    from server.auth.jwt import mint_token

    return mint_token(
        subject=f"sa:saved-pin-{uuid.uuid4().hex[:6]}",
        org=org,
        namespaces=[ns],
        roles=["writer"],
        ttl_seconds=900,
        is_service_account=True,
        **claims,
    )


def _post(base: str, token: str, op: str, body: dict[str, Any]) -> httpx.Response:
    return httpx.post(
        f"{base}/v1/{op}", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=30
    )


def test_a_pinned_token_reads_only_its_users_saved_messages(control_plane_url: str) -> None:
    org = unique_org("saved-pin")
    ns = f"saved-pin-{uuid.uuid4().hex[:8]}"
    wide = _mint(org, ns)
    for user in ("u-1", "u-2"):
        r = _post(
            control_plane_url,
            wide,
            "conversation.save_message",
            {
                "org": org,
                "namespace": ns,
                "agent": "librechat",
                "conversation_id": f"c-{user}",
                "message_id": f"m-{user}",
                "role": "assistant",
                "content": _CONTENT,
                "user_id": user,
            },
        )
        assert r.status_code == 200 and r.json()["indexed"] is True, r.text

    pinned = _mint(org, ns, read_scope="own_user", read_user_id="u-2")
    search = {"org": org, "namespace": ns, "agent": "librechat", "query": _CONTENT}

    r = _post(
        control_plane_url,
        pinned,
        "conversation.search_saved_messages",
        {**search, "user_id": "u-2"},
    )
    assert r.status_code == 200, r.text
    assert [h["message_id"] for h in r.json()["hits"]] == ["m-u-2"]

    r = _post(
        control_plane_url,
        pinned,
        "conversation.search_saved_messages",
        {**search, "user_id": "u-1"},
    )
    assert r.status_code == 403, r.text
    assert "m-u-1" not in r.text

    # A pinned token saves as its own user only.
    r = _post(
        control_plane_url,
        pinned,
        "conversation.save_message",
        {
            "org": org,
            "namespace": ns,
            "agent": "librechat",
            "conversation_id": "c-u1",
            "message_id": "m-other",
            "role": "user",
            "content": _CONTENT,
            "user_id": "u-1",
        },
    )
    assert r.status_code == 403, r.text


def test_a_pinned_token_still_cannot_search_whole_transcripts(control_plane_url: str) -> None:
    """conversation.search spans threads that carry no user axis, so a pinned
    token stays refused there; the saved-message search is its surface."""
    org = unique_org("saved-pin")
    ns = f"saved-pin-{uuid.uuid4().hex[:8]}"
    pinned = _mint(org, ns, read_scope="own_user", read_user_id="u-2")
    r = _post(
        control_plane_url,
        pinned,
        "conversation.search",
        {"org": org, "namespace": ns, "agent": "librechat", "query": _CONTENT},
    )
    assert r.status_code == 403, r.text
