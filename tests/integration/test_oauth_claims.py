"""Sign-in state against the real store: of several requests spending one
credential or moving one device grant at the same moment, exactly one goes
ahead. Needs a ClickHouse with a Keeper (loom.oauth_signin_state, migration 032)."""

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
        pytest.skip("this ClickHouse has no Keeper: loom.oauth_signin_state is absent")
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
        "since": int(time.time()),
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
    # The others see it being spent: told to retry, then finding it spent.
    assert all(isinstance(r, claims.Busy | CodeReplayed) for r in results if r not in won), results


async def test_a_rotation_makes_only_the_new_token_current(store: Any) -> None:
    old, new = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    await store.save_refresh(old, _grant("loom-cli"))
    grant = await store.take_refresh(old, "loom-cli")
    await store.save_refresh(new, {**grant, "generation": grant["generation"] + 1})  # as issued
    assert await store.take_refresh(new, "loom-cli") is not None
    with pytest.raises(CodeReplayed):
        await store.take_refresh(old, "loom-cli")


async def test_a_refresh_token_put_back_after_a_failure_works_again(store: Any) -> None:
    token = secrets.token_urlsafe(32)
    await store.save_refresh(token, _grant("loom-cli"))
    grant = await store.take_refresh(token, "loom-cli")
    await store.save_refresh(token, grant)  # Loom failed after spending it
    assert await store.take_refresh(token, "loom-cli") is not None


async def test_a_code_presented_five_times_at_once_is_spent_once(store: Any) -> None:
    code = secrets.token_urlsafe(32)
    await store.save_code(code, _grant("loom-mcp"))
    results = await _all(*(store.take_code(code, "loom-mcp", lambda _g: True) for _ in range(5)))
    assert len([r for r in results if isinstance(r, dict)]) == 1, results


async def test_an_approved_device_code_is_redeemed_once(store: Any) -> None:
    d, _ = _device()
    code = secrets.token_urlsafe(32)
    d["id"] = token_id(code)
    await store.save_device(d)
    approved = await store.device(d["id"])
    await store.save_device(approved | {"status": "approved", "family": uuid.uuid4().hex})
    results = await _all(*(store.take_device(code) for _ in range(5)))
    assert len([r for r in results if isinstance(r, dict)]) == 1, results


async def test_a_swap_from_a_value_already_replaced_loses(store: Any) -> None:
    from server.core.ch import get_client

    ch, key, exp = get_client(), f"test:{uuid.uuid4().hex}", int(time.time()) + 60
    assert await claims.swap(ch, key, None, "v1", exp) == "v1"
    first, second = await _all(
        claims.swap(ch, key, "v1", "a", exp), claims.swap(ch, key, "v1", "b", exp)
    )
    assert sorted([first, second]) in (["a", "a"], ["b", "b"])  # both see the one winner
    assert await claims.swap(ch, key, "v1", "late", exp) in ("a", "b")


async def test_a_spend_whose_answer_keeper_lost_is_no_replay_on_retry(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = secrets.token_urlsafe(32)
    await store.save_refresh(token, _grant("loom-cli"))
    real = claims.get
    failed = False

    async def get(ch: Any, key: str) -> str | None:
        nonlocal failed
        if not failed:  # the spend landed, then Keeper failed on the read-back
            failed = True
            raise claims.Busy(key)
        return await real(ch, key)

    monkeypatch.setattr(claims, "get", get)
    with pytest.raises(claims.Busy):  # the client is told to retry
        await store.take_refresh(token, "loom-cli")
    assert await store.take_refresh(token, "loom-cli") is not None  # and the retry works


async def test_a_new_key_whose_answer_keeper_lost_is_undone(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    grant, token = _grant("loom-cli"), secrets.token_urlsafe(32)
    real = claims.get
    failed = False

    async def get(ch: Any, key: str) -> str | None:
        nonlocal failed
        if key.startswith("family:") and not failed:  # the insert landed, the read-back failed
            failed = True
            raise claims.Busy(key)
        return await real(ch, key)

    monkeypatch.setattr(claims, "get", get)
    with pytest.raises(claims.Busy):
        await store.save_refresh(token, grant)  # the code exchange answers 503
    retried = secrets.token_urlsafe(32)  # the retried exchange mints a new token
    await store.save_refresh(retried, grant)
    assert await store.take_refresh(retried, "loom-cli") is not None


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
    assert codes == [200, 409], codes
    found = await store._device(d["id"])
    assert found is not None and found[0]["status"] == "denied"


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


async def test_polling_a_pending_device_grant_writes_nothing(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    d, _ = _device()
    code = secrets.token_urlsafe(32)
    d["id"] = token_id(code)
    await store.save_device(d)
    swaps: list[str] = []
    real = store._swap

    async def swap(key: str, old: str | None, new: str) -> str | None:
        swaps.append(key)
        return await real(key, old, new)

    monkeypatch.setattr(store, "_swap", swap)
    polled = await store.take_device(code)
    assert polled is not None and polled["status"] == "pending"
    assert swaps == []


async def test_without_a_keeper_sign_in_answers_busy(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "_atomic", False)
    with pytest.raises(claims.Busy):
        await store.save_code(secrets.token_urlsafe(32), _grant("loom-mcp"))


async def test_an_owed_revocation_counts_in_every_process(store: Any) -> None:
    from server.auth import revocation

    family = uuid.uuid4().hex
    await store.owe_revocation(family, "logout")
    await revocation.refresh()  # what every process loads, at boot and on its cadence
    assert revocation.is_revoked(family)


async def test_a_session_another_process_ended_reads_as_ended(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.auth import revocation

    family = uuid.uuid4().hex
    assert not await revocation.revoked_outright(family)
    await revocation.revoke(family, reason="logout", revoked_by="claims-it")
    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset())  # this process lags
    assert await revocation.revoked_outright(family)


async def test_the_sweep_worker_drops_keys_once_their_credential_expired(store: Any) -> None:
    import importlib

    from server.core.ch import get_client
    from server.workers.registry import _WORKERS

    row = next(w for w in _WORKERS if w.name == "oauth_claims_sweep")
    sweep = getattr(importlib.import_module(row.module), row.pass_attr)

    ch, old, fresh = get_client(), f"test:{uuid.uuid4().hex}", f"test:{uuid.uuid4().hex}"
    await claims.swap(ch, old, None, "x", int(time.time()) - 60)
    await claims.swap(ch, fresh, None, "y", int(time.time()) + 60)
    await sweep()
    assert await claims.get(ch, old) is None
    assert await claims.get(ch, fresh) == "y"
