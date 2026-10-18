"""SCIM requests racing each other and a first sign-in, on ClickHouse.

The unit tests interleave them over an in-memory table; here every read and
write is the store's and the leases are real ones, taken on ClickHouse.
"""

from __future__ import annotations

import asyncio
import functools
import random
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from server.auth import revocation, scim
from server.core.ch import get_client
from server.settings import settings
from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


class _Seeded:
    """The accounts a test makes, and what to remove once it is done."""

    def __init__(self) -> None:
        self.org = unique_org("scim-race")
        self.emails: list[str] = []
        self.subjects: list[str] = []

    def email(self) -> str:
        self.emails.append(f"race-{uuid.uuid4().hex[:8]}@example.com")
        return self.emails[-1]

    def subject(self) -> str:
        self.subjects.append(f"race-{uuid.uuid4().hex[:8]}")
        return self.subjects[-1]

    async def account(self, email: str, subject: str) -> None:
        """An SSO account (no password) of `subject` naming `email`, as JIT makes one."""
        now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        row = {
            "org": self.org,
            "namespace": f"ns-{uuid.uuid4().hex[:8]}",
            "owner_email": subject,
            "contact_email": email,
            "password_hash": "",
            "description": "",
            "tags": [],
            "active": True,
            "visibility": "private",
            "created_at": now,
            "updated_at": now,
        }
        await get_client().insert("loom.namespaces", [row])


@pytest.fixture
async def seeded(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Seeded]:
    async def no_wait(cutoff: datetime) -> None:
        return None

    monkeypatch.setattr(settings, "scim_token", secrets.token_hex(32))
    monkeypatch.setattr(revocation, "wait_out", no_wait)  # the wait is not what races
    ch = get_client()
    await ch.connect()
    made = _Seeded()
    try:
        yield made
    finally:
        # The namespaces rows go with the org; these name none.
        for table, column, values in (
            ("scim_users", "email", made.emails),
            ("revoked_tokens", "jti", [revocation.SCIM_PREFIX + s for s in made.subjects]),
        ):
            await ch.execute(
                f"ALTER TABLE loom.{table} DELETE WHERE {column} IN {{v:Array(String)}} "
                "SETTINGS mutations_sync = 2",
                params={"v": values},
            )
        await ch.disconnect()


async def _sent(rng: random.Random, *requests: Callable[[], Awaitable[Any]]) -> None:
    """The requests at once, each a moment apart; one answered busy is sent
    again, as a provider does."""

    async def after_a_moment(request: Callable[[], Awaitable[Any]]) -> Any:
        await asyncio.sleep(rng.random() * 0.05)
        return await request()

    pending = list(requests)
    for _ in range(4):
        answers = await asyncio.gather(
            *(after_a_moment(r) for r in pending), return_exceptions=True
        )
        for answer in answers:
            if isinstance(answer, BaseException) and not isinstance(answer, scim.Busy):
                raise answer
        pending = [r for r, a in zip(pending, answers, strict=True) if isinstance(a, scim.Busy)]
        if not pending:
            return
    raise AssertionError("still busy after three retries")


async def _keeps_out(subject: str, *, issued_at: int | None = None) -> bool:
    """Whether a token of `subject` issued at `issued_at` (a fresh one: past any cutoff) is dead."""
    when = int(time.time()) + 2 if issued_at is None else issued_at
    return await revocation.subjects_revoked([subject], issued_at=when)


async def test_a_reactivation_racing_a_deactivation_of_a_shared_subject(seeded) -> None:
    # Two addresses reach one account; one comes back as the other leaves.
    rng = random.Random(27)
    for _ in range(8):
        shared, own = seeded.subject(), seeded.subject()
        a, b = seeded.email(), seeded.email()
        await seeded.account(a, shared)
        await seeded.account(a, own)
        await seeded.account(b, shared)
        ua = await scim.create(a, a, active=False)
        ub = await scim.create(b, b, active=True)
        await _sent(
            rng,
            functools.partial(scim.set_active, ua["id"], True),
            functools.partial(scim.set_active, ub["id"], False),
        )
        ra, rb = await scim.get(ua["id"]), await scim.get(ub["id"])
        assert ra is not None and ra["active"] and rb is not None and not rb["active"]
        assert await _keeps_out(shared), "the one still deactivated reaches it"
        assert not await _keeps_out(own), "only the one reactivated reaches it"


class _SignIn:
    """A first sign-in as SSO makes it: checked, the JIT account made, checked
    again. The JIT lands just after a deactivation read the accounts, which
    then goes on to write the row as the sign-in checks again."""

    def __init__(self, seeded: _Seeded, email: str, subject: str, rng: random.Random) -> None:
        self.seeded, self.email, self.subject, self.rng = seeded, email, subject, rng
        self.accounts_read, self.made = asyncio.Event(), asyncio.Event()

    async def accounts(self, email: str) -> dict[str, str]:
        """`scim._subjects`, for the deactivation."""
        found = await _ACCOUNTS(email)
        self.accounts_read.set()
        await self.made.wait()
        return found

    async def __call__(self) -> int | None:
        """The session's `iat`; None when refused."""
        if await scim.deactivated(self.email):
            return None
        await self.accounts_read.wait()
        await self.seeded.account(self.email, self.subject)
        self.made.set()
        await asyncio.sleep(self.rng.random() * 0.02)
        if await scim.deactivated(self.email):
            return None
        return int(time.time())


_ACCOUNTS = scim._subjects


async def test_a_first_sign_in_racing_a_deactivation_keeps_no_session(seeded, monkeypatch) -> None:
    # A session the sign-in gets past both checks is dead once the deactivation answers.
    rng = random.Random(27)
    signed_in = 0
    for _ in range(16):
        email, subject = seeded.email(), seeded.subject()
        user = await scim.create(email, email, active=True)
        sign_in = _SignIn(seeded, email, subject, rng)
        monkeypatch.setattr(scim, "_subjects", sign_in.accounts)
        iat, _ = await asyncio.gather(sign_in(), scim.set_active(user["id"], False))
        row = await scim.get(user["id"])
        assert row is not None and not row["active"]
        if iat is not None:
            signed_in += 1
            assert await _keeps_out(subject, issued_at=iat)
    assert signed_in, "no sign-in got through to race the deactivation"
