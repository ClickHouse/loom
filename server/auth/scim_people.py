"""Bounded active-person decisions for a cell's complete admission ancestry."""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any

from server.core import cell, ch
from server.core.datetimes import as_aware_utc
from server.core.grant_sql import VERIFIED_EMAIL_SQL
from server.core.scim_tokens import tokens
from server.settings import settings

MAX_ITEMS = 1024  # not a setting: security work per complete decision
MAX_BYTES = 65536  # not a setting: input, parameters and transferred identity data
SCIM_DECISION_S = 30.0  # not a setting: even an unbounded generic read must finish here


class _Refused(Exception):
    """The complete identity graph cannot qualify within its supported size."""


def _now() -> float:
    return time.monotonic()


def _encoded(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def _roots(subject: str, verified_email: str, also: tuple[str, ...], org: str) -> tuple[str, ...]:
    if not isinstance(also, (list, tuple)) or len(also) >= MAX_ITEMS:
        raise _Refused
    size = 0
    for value in (subject, verified_email, org, *also):
        if not isinstance(value, str) or len(value) > MAX_BYTES or "\x00" in value:
            raise _Refused
        size += len(value.encode())
        if size > MAX_BYTES:
            raise _Refused
    if any(not value.strip() for value in also):
        raise _Refused
    if _encoded((subject, verified_email, also, org)) > MAX_BYTES:
        raise _Refused
    return tuple(sorted(set(also) - {subject}))


def _decision_seconds() -> float:
    configured = float(settings.ch_timeout_read_s)
    return (
        min(SCIM_DECISION_S, configured)
        if math.isfinite(configured) and configured > 0
        else SCIM_DECISION_S
    )


def requires_provisioning() -> bool:
    return bool(settings.cell and tokens(settings.scim_token) and cell.mode() == "enforced")


async def refuses_subject(
    subject: str, *, org: str, verified_email: str = "", also: tuple[str, ...] = ()
) -> bool:
    from server.auth import authz, login, scim

    try:
        extra = _roots(subject, verified_email, also, org)
    except (UnicodeError, _Refused):
        return True
    email = scim.normalized(verified_email)
    required = requires_provisioning()
    if not required:
        for address in {scim.normalized(s) for s in (subject, email, *extra) if "@" in s}:
            if await authz._remembered(("scim", address), scim.deactivated):
                return True
        return False
    key = ("scim_people", cell.deployment_id(), settings.cell, org, email, extra, subject)

    async def read(primary: str) -> bool:
        seconds = _decision_seconds()
        decision = _Decision(org, primary, email, _now() + seconds)
        try:
            async with asyncio.timeout(seconds):
                with ch.own_write_reads(), ch.query_tag("auth.scim_people"):
                    return await decision.refuses((primary, *extra))
        except _Refused:
            return True
        except Exception as e:  # an unchecked identity never admits a person
            raise login.SignInUnavailable from e

    return await authz._remembered(key, read)


@dataclass
class _Decision:
    org: str
    primary: str
    verified_email: str
    deadline: float
    visiting: set[str] = field(default_factory=set)
    completed: set[str] = field(default_factory=set)
    emails: set[str] = field(default_factory=set)
    edges: int = 0
    accounts: int = 0
    transferred: int = 0

    def remaining(self) -> float:
        seconds = self.deadline - _now()
        if seconds <= 0:
            raise TimeoutError
        configured = ch.class_timeout_s("read")
        return min(seconds, configured) if math.isfinite(configured) and configured > 0 else seconds

    def transferred_bytes(self, count: int) -> None:
        self.transferred += count
        if self.transferred > MAX_BYTES:
            raise _Refused

    async def query(self, sql: str, params: dict[str, Any], *, many: bool = False) -> Any:
        if _encoded(params) > MAX_BYTES:
            raise _Refused
        with ch.statement_timeout(self.remaining()):
            client = ch.get_client()
            if many:
                return await client.query_all(sql, params=params)
            return await client.query_one(sql, params=params)

    async def query_one(self, sql: str, params: dict[str, Any]) -> Any:
        """Keep the shared ancestry reader within this decision's query bounds."""
        return await self.query(sql, params)

    async def refuses(self, roots: tuple[str, ...]) -> bool:
        for root in roots:
            if not root.strip():
                return True
            stack = [(root, False)]
            while stack:
                self.remaining()
                subject, leaving = stack.pop()
                if leaving:
                    self.visiting.remove(subject)
                    self.completed.add(subject)
                    continue
                if subject in self.visiting:
                    return True
                if subject in self.completed:
                    continue
                if len(self.visiting) + len(self.completed) >= MAX_ITEMS:
                    raise _Refused
                self.visiting.add(subject)
                stack.append((subject, True))
                if subject.lower().startswith("sa:"):
                    ancestors = await self.ancestors(subject)
                    self.edges += len(ancestors)
                    if self.edges > MAX_ITEMS:
                        raise _Refused
                    stack.extend((ancestor, False) for ancestor in reversed(ancestors))
                elif await self.person_refused(subject):
                    return True
        return False

    async def ancestors(self, subject: str) -> list[str]:
        from server.auth import service_accounts

        name = subject.lower().removeprefix("sa:").partition(":")[0]
        if not name:
            raise _Refused
        bot = await self.query(
            "SELECT active FROM loom.machine_identities FINAL WHERE name = {name:String} LIMIT 1",
            {"name": name},
        )
        if bot is not None:
            if not bot["active"]:
                raise _Refused
            return []
        row = await service_accounts._creator_row(
            self.org,
            subject,
            ch=self,
            max_items=MAX_ITEMS - self.edges,
            max_bytes=MAX_BYTES - self.transferred,
        )
        if row is None or row["revoked_at"] is not None:
            raise _Refused
        expires_at = row.get("expires_at")
        if expires_at is not None and as_aware_utc(expires_at).timestamp() <= time.time():
            raise _Refused
        if int(row["lineage_count"]) > MAX_ITEMS - self.edges:
            raise _Refused
        self.transferred_bytes(int(row["subject_bytes"]))
        creator = row["created_by"]
        lineage = row["lineage"]
        if any(not isinstance(s, str) or not s.strip() or "\x00" in s for s in (creator, *lineage)):
            raise _Refused
        return [creator, *lineage]

    async def account_emails(self, subject: str) -> tuple[set[str], set[str]]:
        from server.auth import scim

        folded = scim.normalized(subject)
        mandatory = {folded} if "@" in folded else set()
        witnesses = set(mandatory)
        if subject == self.primary and self.verified_email:
            mandatory.add(self.verified_email)
            witnesses.add(self.verified_email)
        if self.org not in ("", "*", "-"):
            owner = await self.owner(folded)
            if owner is not None:
                if "@" in owner:
                    mandatory.add(owner)
                    witnesses.add(owner)
                await self.contacts(owner, mandatory, witnesses)
        self.emails.update(mandatory | witnesses)
        if len(self.emails) > MAX_ITEMS:
            raise _Refused
        return mandatory, witnesses

    async def owner(self, subject: str) -> str | None:
        rows = await self.query(
            f"""SELECT DISTINCT
                if(length(lower(owner_email)) <= {{bytes:UInt32}}, lower(owner_email), '') AS owner,
                length(lower(owner_email)) > {{bytes:UInt32}} AS oversized
            FROM loom.namespaces FINAL
            WHERE org = {{org:String}} AND ifNull(active, true) = true
              AND (lower(owner_email) = {{sub:String}}
                OR (lower(contact_email) = {{sub:String}} AND {VERIFIED_EMAIL_SQL}))
            LIMIT 2""",
            {"org": self.org, "sub": subject, "bytes": MAX_BYTES - self.transferred},
            many=True,
        )
        if len(rows) > 1 or any(r["oversized"] for r in rows):
            raise _Refused
        if not rows:
            return None
        owner = str(rows[0]["owner"])
        if not owner:
            raise _Refused
        self.transferred_bytes(len(owner.encode()))
        return owner

    async def contacts(self, owner: str, mandatory: set[str], witnesses: set[str]) -> None:
        from server.auth import scim

        row = await self.query(
            f"""SELECT count() AS account_count, sum(length(contact_email)) AS account_bytes,
                if(account_count <= {{items:UInt32}} AND account_bytes <= {{bytes:UInt32}},
                   groupArray({MAX_ITEMS + 1})((
                     if(length(contact_email) <= {{bytes:UInt32}}, lower(contact_email), ''),
                     {VERIFIED_EMAIL_SQL})), []) AS accounts
            FROM loom.namespaces FINAL
            WHERE org = {{org:String}} AND lower(owner_email) = {{owner:String}}
              AND ifNull(active, true) = true
            LIMIT 1""",
            {
                "org": self.org,
                "owner": owner,
                "items": MAX_ITEMS - self.accounts,
                "bytes": MAX_BYTES - self.transferred,
            },
        )
        self.accounts += int(row["account_count"])
        if self.accounts > MAX_ITEMS:
            raise _Refused
        self.transferred_bytes(int(row["account_bytes"]))
        for email, verified in row["accounts"]:
            email = scim.normalized(email)
            if email:
                mandatory.add(email)
                if verified:
                    witnesses.add(email)

    async def person_refused(self, subject: str) -> bool:
        from server.auth import scim

        mandatory, witnesses = await self.account_emails(subject)
        if not witnesses:
            return True
        by_id = {scim.user_id(email): email for email in mandatory}
        rows = await self.query(
            """SELECT id, active, deleted FROM loom.scim_users FINAL
               WHERE id IN {ids:Array(String)} LIMIT 1024""",
            {"ids": list(by_id)},
            many=True,
        )
        found = set()
        for row in rows:
            if not row["active"] or row["deleted"]:
                return True
            found.add(by_id[row["id"]])
        missing = mandatory - found
        if not missing:
            return False
        row = await self.query(
            """SELECT groupUniqArray(1025)(address) AS qualified FROM (
                SELECT arrayJoin(arrayIntersect(work_addresses, {missing:Array(String)})) AS address
                FROM loom.scim_users FINAL WHERE id IN {ids:Array(String)}
                  AND active = 1 AND deleted = 0 AND email IN {witnesses:Array(String)}
            ) LIMIT 1""",
            {
                "ids": [scim.user_id(email) for email in witnesses],
                "witnesses": sorted(witnesses),
                "missing": sorted(missing),
            },
        )
        return not missing.issubset(row["qualified"])
