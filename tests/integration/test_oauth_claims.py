"""Sign-in transitions against the real store: of several requests spending one
credential or moving one device grant at the same moment, exactly one goes
ahead. Needs a ClickHouse with a Keeper (loom.oauth_claims, migration 029)."""

from __future__ import annotations

import asyncio
import re
import secrets
import time
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from server.auth import claims, device, oauth
from server.auth.oauth_store import ClickHouseStore, CodeReplayed, Device, Grant, token_id

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def store() -> Any:
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    claims._atomic = None  # ask this server
    if not await claims._available(ch):
        await ch.disconnect()
        pytest.skip("this ClickHouse has no Keeper: loom.oauth_claims is absent")
    yield ClickHouseStore()
    await ch.disconnect()


def _grant(client_id: str) -> Grant:
    return {
        "client_id": client_id,
        "redirect_uri": "",
        "code_challenge": "",
        "subject": "alice@example.com",
        "org": "claims-it",
        "namespaces": ["alice"],
        "roles": ["writer"],
        "exp": int(time.time()) + 600,
        "family": uuid.uuid4().hex,
        "upstream_refresh": "",
    }


def _device(status: str = "pending") -> tuple[Device, str]:
    user_code = "".join(secrets.choice("BCDFGHJKLMNPQRSTVWXZ") for _ in range(8))
    d: Device = {
        "id": token_id(secrets.token_urlsafe(32)),
        "client_id": device.DEVICE_CLIENT_ID,
        "subject": "",
        "org": "",
        "namespaces": [],
        "roles": [],
        "exp": int(time.time()) + 600,
        "family": "",
        "upstream_refresh": "",
        "user_code": device._user_code_id(user_code),
        "status": status,  # type: ignore[typeddict-item]
        "address": "127.0.0.1",
        "ticket": "",
    }
    return d, user_code


async def _all(*calls: Any) -> list[Any]:
    return await asyncio.gather(*calls, return_exceptions=True)


async def test_a_refresh_token_presented_five_times_at_once_is_spent_once(store: Any) -> None:
    token = secrets.token_urlsafe(32)
    await store.save_refresh(token, _grant("loom-cli"))
    results = await _all(*(store.take_refresh(token, "loom-cli") for _ in range(5)))
    won = [r for r in results if isinstance(r, dict)]
    assert len(won) == 1, results
    assert all(isinstance(r, CodeReplayed | claims.Busy) for r in results if r not in won)


async def test_a_code_presented_five_times_at_once_is_spent_once(store: Any) -> None:
    code = secrets.token_urlsafe(32)
    await store.save_code(code, _grant("loom-mcp"))
    results = await _all(*(store.take_code(code, "loom-mcp", lambda _g: True) for _ in range(5)))
    assert len([r for r in results if isinstance(r, dict)]) == 1, results


async def test_an_approved_device_code_is_redeemed_once(store: Any) -> None:
    d, _ = _device("approved")
    code = secrets.token_urlsafe(32)
    d["id"] = token_id(code)
    await store.save_device(d)
    results = await _all(*(store.take_device(code) for _ in range(5)))
    assert len([r for r in results if isinstance(r, dict)]) == 1, results


async def test_two_people_signing_in_on_one_code_at_once_end_it_for_both(store: Any) -> None:
    d, user_code = _device()
    await store.save_device(d)
    alice = ("alice@example.com", "claims-it", ["alice"], ["writer"])
    mallory = ("mallory@example.com", "claims-it", ["mallory"], ["writer"])
    pages = await _all(
        device.signed_in(user_code, alice, ""), device.signed_in(user_code, mallory, "")
    )
    codes = sorted(p.status_code for p in pages)
    # One signs in first; the other then finds it taken, and the grant ends.
    assert codes in ([200, 409], [409, 409]), codes
    final = await store.device(d["id"])
    assert final is not None and final["status"] == "denied"


async def test_one_approval_ticket_approves_once(store: Any) -> None:
    d, user_code = _device()
    await store.save_device(d)
    page = await device.signed_in(
        user_code, ("alice@example.com", "claims-it", ["alice"], ["writer"]), ""
    )
    ticket = re.search(r'name="ticket" value="([^"]+)"', page.body.decode())
    assert ticket
    app = FastAPI()
    app.add_exception_handler(oauth.OAuthError, oauth.oauth_error_response)
    app.include_router(device.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        answers = await _all(
            *(
                c.post(
                    "/oauth/device/approve",
                    data={"user_code": user_code, "ticket": ticket.group(1), "decision": "approve"},
                )
                for _ in range(5)
            )
        )
    approved = [a for a in answers if a.status_code == 200 and "signed in" in a.text]
    assert len(approved) == 1, [a.status_code for a in answers]


async def test_a_holder_taken_over_cannot_release_the_new_holders_claim(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.ch import get_client

    ch = get_client()
    key = f"test:{uuid.uuid4().hex}"
    held = "SELECT holder FROM loom.oauth_claims WHERE claim_key = {k:String}"
    monkeypatch.setattr(claims, "_STALE_S", 0)  # A stalls past it at once
    a_inside, b_inside, a_done = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def a() -> None:
        async with claims.holding(ch, key):
            a_inside.set()
            await b_inside.wait()  # still running while B has taken over
        a_done.set()

    async def b() -> str:
        await a_inside.wait()
        await asyncio.sleep(0.05)
        async with claims.holding(ch, key):
            b_inside.set()
            await a_done.wait()  # A has left and run its release
            row = await ch.query_one(held, params={"k": key})
            return str(row["holder"]) if row else ""

    _, b_holder = await asyncio.gather(a(), b())
    assert b_holder, "A's release dropped the claim B held"
    assert await ch.query_one(held, params={"k": key}) is None  # B dropped its own
