"""Persistent refusal fences outlive rescans and root-secret rotation."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol

from server.auth import claims
from server.clients._vendor import VendorError
from server.core import leases
from server.core.ch import get_client, set_query_tag
from server.core.secrets_at_rest import seal, unseal
from server.knowledge.domain.source_crawl import Person, email
from server.knowledge.store import crawl_exclusions as store


class OrgAuthority(Protocol):
    async def validate(self, org: str) -> None: ...


KEY_PURPOSE = "knowledge_crawl.exclusion_key"
# KeeperMap expiry is UInt32 seconds; this is its fixed representable upper bound.
_EXPIRY = 2**32 - 1


def _fence(org: str) -> str:
    if not isinstance(org, str) or not org or "\0" in org:
        raise ValueError("crawl org refused")
    return "crawl-exclusions:" + hashlib.sha256(org.encode()).hexdigest()


@asynccontextmanager
async def org_lease(
    org: str, *, ttl_s: float, wait_s: float, ch: Any = None
) -> AsyncIterator[None]:
    if not all(type(v) in (int, float) and math.isfinite(v) for v in (ttl_s, wait_s)):
        raise ValueError("finite crawl lease bounds required")
    if ttl_s <= 0 or wait_s < 0:
        raise ValueError("crawl lease bounds refused")
    async with leases.cluster_lease(_fence(org), ttl_s=ttl_s, wait_s=wait_s, ch=ch or get_client()):
        yield


async def _move(ch: Any, org: str, old: str | None, new: str) -> None:
    if await claims.swap(ch, _fence(org), old, new, _EXPIRY) != new:
        raise VendorError("crawl exclusions changed")


async def initialize(
    org: str, *, authority: OrgAuthority, ttl_s: float, wait_s: float, ch: Any = None
) -> Index:
    """Registration initializes once under its checked org authority; reads never initialize."""
    set_query_tag("knowledge.crawl.exclusions.initialize")
    ch = ch or get_client()
    await authority.validate(org)
    async with org_lease(org, ttl_s=ttl_s, wait_s=wait_s, ch=ch):
        await authority.validate(org)
        row = await store.key(org, ch=ch)
        current = await claims.get(ch, _fence(org))
        if row is None:
            if current is not None or await store.any_refusal(org, ch=ch):
                raise VendorError("crawl exclusion key unavailable")
            key = secrets.token_hex(32)
            key_id = hashlib.sha256(bytes.fromhex(key)).hexdigest()
            blocked = f"initializing:{key_id}:{uuid.uuid4().hex}"
            await _move(ch, org, current, blocked)
            key_enc = seal(KEY_PURPOSE, key)
            await store.key_put(org, key_id, key_enc, generation=blocked, ch=ch)
        else:
            key_id = str(row["key_id"])
            if (
                isinstance(current, str)
                and current.startswith("initializing:" + key_id + ":")
                and not row["pending_id"]
                and not await store.any_refusal(org, ch=ch)
            ):
                _open_key(row)
                active = f"active:{key_id}:{uuid.uuid4().hex}"
                await _move(ch, org, current, active)
                await store.key_put(org, key_id, str(row["key_enc"]), generation=active, ch=ch)
                return await Index.open(org, ch=ch)
            if (
                isinstance(current, str)
                and current.startswith("active:" + key_id + ":")
                and str(row["generation"]).startswith("initializing:" + key_id + ":")
                and not row["pending_id"]
                and not await store.any_refusal(org, ch=ch)
            ):
                _open_key(row)
                await store.key_put(org, key_id, str(row["key_enc"]), generation=current, ch=ch)
                return await Index.open(org, ch=ch)
            # A blocked existing registry requires its actual failed cleanup to retry.
            if current is None or not current.startswith("active:" + key_id + ":"):
                raise VendorError("crawl exclusion repair required")
            return await Index.open(org, ch=ch)
        active = f"active:{key_id}:{uuid.uuid4().hex}"
        await _move(ch, org, blocked, active)
        await store.key_put(org, key_id, key_enc, generation=active, ch=ch)
    return await Index.open(org, ch=ch)


def _open_key(row: dict[str, Any]) -> bytes:
    plain = unseal(KEY_PURPOSE, str(row["key_enc"]), warn=False)
    if len(plain) != 64 or any(c not in "0123456789abcdef" for c in plain):
        raise VendorError("crawl exclusion key unavailable")
    key = bytes.fromhex(plain)
    if not secrets.compare_digest(hashlib.sha256(key).hexdigest(), str(row["key_id"])):
        raise VendorError("crawl exclusion key unavailable")
    return key


async def _finish(
    org: str,
    row: dict[str, Any],
    *,
    authority: OrgAuthority,
    cleanup: Callable[[], Awaitable[None]],
    ch: Any,
) -> None:
    key_id, pending = str(row["key_id"]), str(row["pending_id"])
    digests = row["pending_digests"]
    if (
        len(pending) != 32
        or any(c not in "0123456789abcdef" for c in pending)
        or not isinstance(digests, list)
        or not digests
        or any(
            not isinstance(d, str) or len(d) != 64 or any(c not in "0123456789abcdef" for c in d)
            for d in digests
        )
    ):
        raise VendorError("crawl exclusion repair unavailable")
    current = await claims.get(ch, _fence(org))
    blocked, active = f"blocked:{key_id}:{pending}", f"active:{key_id}:{pending}"
    # A crash before the initial CAS left the previous active fence in place.
    if current not in {row["generation"], blocked, active}:
        raise VendorError("crawl exclusion repair unavailable")
    if type(row["pending_exclude"]) not in (bool, int) or row["pending_exclude"] not in (0, 1):
        raise VendorError("crawl exclusion repair unavailable")
    if current != blocked:
        await _move(ch, org, current, blocked)
    await store.put(org, digests, exclude=bool(row["pending_exclude"]), ch=ch)
    await cleanup()
    await authority.validate(org)
    from server.erasure.app.crawl_person import require_refusals

    await require_refusals(org, digests, excluded=bool(row["pending_exclude"]), ch=ch)
    await _move(ch, org, blocked, active)
    # Until this full row lands, new readers still refuse the unfinished manifest.
    await store.key_put(org, key_id, str(row["key_enc"]), generation=active, ch=ch)


async def repair(
    org: str,
    *,
    ttl_s: float,
    wait_s: float,
    authority: OrgAuthority,
    cleanup: Callable[[], Awaitable[None]],
    expected_digests: tuple[str, ...] | None = None,
    ch: Any = None,
) -> Index:
    """Finish the persisted mutation after a crash; never guess a readmission."""
    set_query_tag("knowledge.crawl.exclusions.repair")
    ch = ch or get_client()
    await authority.validate(org)
    async with org_lease(org, ttl_s=ttl_s, wait_s=wait_s, ch=ch):
        await authority.validate(org)
        row = await store.key(org, ch=ch)
        if row is None:
            raise VendorError("crawl exclusion repair unavailable")
        _open_key(row)
        if expected_digests is not None and row["pending_digests"] != list(expected_digests):
            raise VendorError("another crawl exclusion cleanup is pending")
        await _finish(org, row, authority=authority, cleanup=cleanup, ch=ch)
    return await Index.open(org, ch=ch)


class Index:
    """One private immutable refusal observation, never a source authority or credential."""

    __slots__ = ("org", "_key", "_key_id", "_version", "_ch")

    def __init__(self, org: str, key: bytes, key_id: str, version: str, ch: Any) -> None:
        self.org, self._key, self._key_id, self._version, self._ch = org, key, key_id, version, ch

    def __repr__(self) -> str:
        return "Index(<redacted>)"

    @classmethod
    async def open(cls, org: str, *, ch: Any = None) -> Index:
        ch = ch or get_client()
        row = await store.key(org, ch=ch)
        observed = await claims.get(ch, _fence(org))
        if row is None or not isinstance(observed, str):
            raise VendorError("crawl exclusions unavailable")
        key_id = str(row["key_id"])
        if (
            not key_id
            or row["pending_id"]
            or row["generation"] != observed
            or not observed.startswith("active:" + key_id + ":")
        ):
            raise VendorError("crawl exclusion cleanup incomplete")
        return cls(org, _open_key(row), key_id, observed, ch)

    async def version(self) -> str:
        row = await store.key(self.org, ch=self._ch)
        if (
            await claims.get(self._ch, _fence(self.org)) != self._version
            or row is None
            or row["pending_id"]
            or row["key_id"] != self._key_id
            or row["generation"] != self._version
        ):
            raise VendorError("crawl exclusions changed")
        return self._version

    def _digest(self, purpose: str, values: list[str]) -> str:
        payload = json.dumps([purpose, self.org, *values], separators=(",", ":")).encode()
        return hmac.digest(self._key, payload, "sha256").hex()

    def person_digests(self, person: Person) -> list[str]:
        if not person.customer or not person.id:
            raise ValueError("provider identity required")
        return [
            self._digest("google-user", [person.customer, person.id]),
            *(
                self._digest("verified-email", [email(address)])
                for address in sorted(person.addresses)
            ),
        ]

    def contact_digests(self, contacts: tuple[str, ...]) -> list[str]:
        return sorted({self._digest("verified-email", [email(v)]) for v in contacts})

    async def contains(self, person: Person) -> bool:
        await self.version()
        found = await store.excluded(self.org, self.person_digests(person), ch=self._ch)
        await self.version()
        return bool(found)

    async def excluded_addresses(self, addresses: tuple[str, ...]) -> frozenset[str]:
        await self.version()
        digests = {
            email(address): self._digest("verified-email", [email(address)])
            for address in addresses
        }
        excluded = await store.excluded(self.org, list(digests.values()), ch=self._ch)
        await self.version()
        return frozenset(address for address, digest in digests.items() if digest in excluded)

    async def change(
        self,
        people: tuple[Person, ...],
        *,
        exclude: bool,
        ttl_s: float,
        wait_s: float,
        authority: OrgAuthority,
        cleanup: Callable[[], Awaitable[None]],
        contacts: tuple[str, ...] = (),
    ) -> Index:
        """A checked mutation stays fenced until every local rewrite and census succeeds."""
        set_query_tag("knowledge.crawl.exclusions.change")
        if type(exclude) is not bool or not people and not contacts:
            raise ValueError("explicit provider-person exclusion or readmission required")
        await authority.validate(self.org)
        async with org_lease(self.org, ttl_s=ttl_s, wait_s=wait_s, ch=self._ch):
            await authority.validate(self.org)
            await self.version()
            digests = sorted(
                {
                    *(digest for person in people for digest in self.person_digests(person)),
                    *self.contact_digests(contacts),
                }
            )
            row = await store.key(self.org, ch=self._ch)
            if row is None:
                raise VendorError("crawl exclusion key unavailable")
            pending = uuid.uuid4().hex
            await store.key_put(
                self.org,
                self._key_id,
                str(row["key_enc"]),
                generation=self._version,
                pending_id=pending,
                pending_digests=tuple(digests),
                pending_exclude=exclude,
                ch=self._ch,
            )
            row = await store.key(self.org, ch=self._ch)
            if row is None or row["pending_id"] != pending:
                raise VendorError("crawl exclusion repair unavailable")
            await _finish(self.org, row, authority=authority, cleanup=cleanup, ch=self._ch)
        return await Index.open(self.org, ch=self._ch)
