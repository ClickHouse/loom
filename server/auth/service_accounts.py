"""A service account's creator, and whether the account reads through the
creator's grants.

A service-account token (`sa:` subject) carries no grants of its own. Its
`loom.service_accounts` row names who created it and whether it inherits
that creator's live grants (`inherit_creator_grants`: true for a token
minted from the dashboard's Settings, false for an agent-signup token). The gate
and every namespace listing read this one row through `sa_creator`, so
they agree on what the token may read. A revoked row is written through
`revoked_row`, here so the SCIM deactivation can write one too.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from server.auth.fail_closed import note_unreadable
from server.core.datetimes import as_aware_utc
from server.settings import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SACreator:
    """The unrevoked service account's `created_by`, and whether the account
    reads through that creator's grants."""

    subject: str
    inherits: bool
    # "agent" for an agent sign-up's account, whose creator is a namespace, not a person.
    kind: str = ""


# (org, sa_subject) -> (time.monotonic() of the read, its creator). Kept for
# the revocation refresh interval, as `delegation._creator_account` keeps the
# creator's account; a revoke also revokes the token by subject at once. The
# cap bounds it.
_creators: dict[tuple[str, str], tuple[float, SACreator]] = {}
_CREATOR_CACHE_MAX = 4096


async def agent_signup_tokens(org: str, namespace: str, *, ch: Any | None = None) -> set[str]:
    """The `sa_subject`s agent sign-up stored for `namespace`: made by the
    namespace itself, under the agent name prefix no person's token can take."""
    from server.auth.self_signup import AGENT_CLIENT, AGENT_NAME_PREFIX

    if ch is None:
        from server.core.ch import get_client

        ch = get_client()
    rows = await ch.query_all(
        """
        SELECT sa_subject FROM loom.service_accounts FINAL
        WHERE org = {org:String} AND created_by = {ns:String} AND client = {client:String}
          AND startsWith(name, {prefix:String})
        """,
        params={"org": org, "ns": namespace, "client": AGENT_CLIENT, "prefix": AGENT_NAME_PREFIX},
    )
    return {str(r["sa_subject"]) for r in rows}


async def sa_creator(org: str, sa_subject: str, *, ch: Any | None = None) -> SACreator | None:
    """The creator of the unrevoked service account `sa_subject` in `org`;
    None for a subject that is not one, a row with no creator, or a failed
    read (fail closed: no inheritance). A found row is cached for the
    revocation refresh interval; a miss or a failed read is not."""
    if not sa_subject.startswith("sa:"):
        return None
    key = (org, sa_subject)
    hit = _creators.get(key)
    if hit is not None and time.monotonic() - hit[0] < settings.jwt_revocation_cache_ttl_seconds:
        return hit[1]
    try:
        row = await _creator_row(org, sa_subject, ch=ch)
    except Exception as e:  # noqa: BLE001 — a failed read inherits nothing (fail closed)
        log.warning("service_accounts lookup failed for %s/%s: %s", org, sa_subject, e)
        note_unreadable()
        return None
    if row is not None and row["revoked_at"] is not None:
        return None
    creator = str((row or {}).get("created_by") or "")
    if not creator:
        return None
    found = SACreator(
        subject=creator,
        inherits=bool((row or {}).get("inherit_creator_grants")),
        kind=str((row or {}).get("kind") or ""),
    )
    if len(_creators) >= _CREATOR_CACHE_MAX:
        _creators.clear()
    _creators[key] = (time.monotonic(), found)
    return found


async def _creator_row(
    org: str,
    sa_subject: str,
    *,
    ch: Any | None = None,
    max_items: int = 1024,
    max_bytes: int = 65536,
) -> dict[str, Any] | None:
    """One current row, with ancestry projected only within the caller's budget."""
    if ch is None:
        from server.core.ch import get_client

        ch = get_client()
    row = await ch.query_one(
        """SELECT inherit_creator_grants, kind, revoked_at, expires_at,
                  length(lineage) AS lineage_count,
                  arraySum(arrayMap(s -> length(s), lineage)) + length(created_by) AS subject_bytes,
                  if(length(created_by) <= {bytes:UInt32}, created_by, '') AS bounded_creator,
                  if(lineage_count <= {items:UInt32} AND subject_bytes <= {bytes:UInt32},
                     lineage, []) AS bounded_lineage
           FROM loom.service_accounts FINAL
           WHERE org = {org:String} AND sa_subject = {sub:String} LIMIT 1""",
        params={"org": org, "sub": sa_subject, "items": max_items, "bytes": max_bytes},
    )
    if row is not None:
        row["created_by"] = row.pop("bounded_creator")
        row["lineage"] = row.pop("bounded_lineage")
    return row


async def inheriting_creator(org: str, sa_subject: str, *, ch: Any | None = None) -> str | None:
    """The creator whose live grants `sa_subject` reads through, or None: the
    account does not inherit, has no creator, or is its own creator."""
    sa = await sa_creator(org, sa_subject, ch=ch)
    if sa is None or not sa.inherits or sa.subject == sa_subject:
        return None
    return sa.subject


def revoked_row(row: dict[str, Any], *, revoked_at: datetime, revoked_by: str) -> dict[str, Any]:
    """A new version of the `loom.service_accounts` row `row`, revoked. Every
    column is carried forward: one left out reads as its default under FINAL.
    Its version outranks the row's, whichever host's clock wrote that one."""
    return {
        "org": row["org"],
        "name": row["name"],
        "sa_subject": row["sa_subject"],
        "namespaces": row["namespaces"],
        "roles": row["roles"],
        "ttl_days": row["ttl_days"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "revoked_at": revoked_at,
        "revoked_by": revoked_by,
        # The host's clock: the row was read under the name's claim, so passing
        # its version is enough to replace it.
        "version": max(time.time_ns(), int(row["version"]) + 1),
        "kind": row["kind"],
        "client": row["client"],
        "inherit_creator_grants": row["inherit_creator_grants"],
        "ns_roles": row["ns_roles"],
        "lineage": row["lineage"],
        "saves_to": row["saves_to"],
    }


async def hand_down_lineage(org: str, name: str) -> None:
    """Before a new token takes `name`, add to the lineage of the tokens its
    former holder made, directly or below a token gone already, every subject
    above them. Once the new row replaces the former one under FINAL, no row
    names the subject they were made by, and a token listed before `lineage`
    would be found by neither column. Called under the name's claim; each token
    rewritten under its own (parent before child, so claims never wait in a
    cycle)."""
    from server.auth.grant_claims import NotClaimed
    from server.core.ch import get_client, own_write_reads
    from server.core.errors import GrantBusyError

    ch = get_client()
    key = {"org": org, "name": name}
    with own_write_reads():
        former = await ch.query_one(
            "SELECT sa_subject, created_by, lineage FROM loom.service_accounts FINAL "
            "WHERE org = {org:String} AND name = {name:String}",
            params=key,
        )
    if former is None:
        return
    made_by = str(former["sa_subject"])
    above = sorted({made_by, str(former["created_by"]), *former["lineage"]} - {""})
    # As the revoke pass walks them; an expired token it never reaches.
    find = (
        "FROM loom.service_accounts FINAL WHERE org = {org:String} "
        "AND (created_by = {by:String} OR has(lineage, {by:String})) "
        f"AND NOT hasAll(lineage, {{above:Array(String)}}) AND {_UNEXPIRED}"
    )
    params = {"org": org, "by": made_by, "above": above}
    with own_write_reads():
        made = await ch.query_all(f"SELECT name {find}", params=params)
    gate = asyncio.Semaphore(_CLAIMS_AT_ONCE)  # as `_revoke_all`: each its own claim

    async def add(child: str) -> None:
        async with gate:
            await _add_lineage(org, child, find, params, above)

    try:
        async with asyncio.TaskGroup() as tasks:
            for r in made:
                tasks.create_task(add(str(r["name"])))
    except ExceptionGroup as e:  # the failure itself, as `_revoke_all` raises it
        first = e.exceptions[0]
        # A claim's errors name its token, and it may be another person's.
        if isinstance(first, GrantBusyError):
            raise GrantBusyError(
                "a change to a token made under this name is running; retry shortly"
            ) from None
        if isinstance(first, NotClaimed):
            raise NotClaimed(
                "changes to the tokens made under this name cannot be claimed; retry shortly",
                operation="grant_change",
            ) from None
        raise first from e


async def _add_lineage(
    org: str, name: str, find: str, params: dict[str, Any], above: list[str]
) -> None:
    from server.auth.authz import identity_write
    from server.auth.grant_claims import service_account_change
    from server.core.ch import get_client, own_write_reads

    ch = get_client()
    async with service_account_change(org, name):
        with own_write_reads():  # again, under its claim
            row = await ch.query_one(
                f"SELECT * {find} AND name = {{name:String}}", params={**params, "name": name}
            )
        if row is None:
            return
        row = dict(row)
        lineage = sorted({*row["lineage"], *above})
        # Every column carried, past the version read, as `revoked_row` does.
        version = max(time.time_ns(), int(row["version"]) + 1)
        with identity_write():
            await ch.insert(
                "loom.service_accounts", [{**row, "lineage": lineage, "version": version}]
            )


# Names a pass revokes at once: each is a lease round trip and an insert.
_CLAIMS_AT_ONCE = 8
# Creators read at once: their keys are one array parameter, which ClickHouse
# refuses past its HTTP field size (128 KiB by default). 100 subjects of the
# longest names, three keys each, stay under a quarter of it.
_CREATORS_AT_ONCE = 100
_UNEXPIRED = "(expires_at IS NULL OR expires_at > now())"
# Made by one of the subjects, or below one: a token whose parent's name was
# reused is found by its lineage, as the row its parent held is gone.
_MADE_BY = "(created_by IN {subs:Array(String)} OR hasAny(lineage, {subs:Array(String)}))"


async def revoke_created_by(
    subjects: list[str],
    *,
    revoked_by: str,
    work: _RevocationWork | None = None,
) -> set[str]:
    """Mark revoked each token any of `subjects` made, directly or through
    their tokens, whose token the revocations kill, so its name comes free;
    one made after a cutoff stays live. A retry finds only what an earlier
    try left. Returns the `sa:` subjects of every unexpired token found dead,
    those an earlier try marked included."""
    from server.auth import revocation
    from server.auth.delegation import MAX_CHAIN_DEPTH
    from server.core.ch import get_client, own_write_reads

    ch = get_client() if work is None else work.ch
    seen = set(subjects)
    creators = sorted(seen)
    dead: set[str] = set()  # the `sa:` subjects of the dead tokens found so far
    # One pass per generation: the person's tokens, then the tokens those made.
    for _ in range(MAX_CHAIN_DEPTH + 1):
        if not creators:
            return dead
        made: set[str] = set()
        for i in range(0, len(creators), _CREATORS_AT_ONCE):
            chunk = creators[i : i + _CREATORS_AT_ONCE]
            if work is None:
                with own_write_reads():
                    rows = await ch.query_all(
                        "SELECT org, name, sa_subject, created_by, created_at, lineage, "
                        "revoked_at IS NULL AS live FROM loom.service_accounts FINAL "
                        f"WHERE {_MADE_BY} AND {_UNEXPIRED}",
                        params={"subs": chunk},
                    )
            else:
                rows = await work.read(f"{_MADE_BY} AND {_UNEXPIRED}", {"subs": chunk})
            # Read after the list, so it tells whether each token listed is dead.
            revoked = (
                await revocation.cutoffs(chunk, permanent=True)
                if work is None
                else await work.cutoffs(chunk)
            )
            # Only those still dead: a row listed revoked is judged again, as the gate would.
            dead |= {
                str(r["sa_subject"]) for r in rows if not r["live"] and _is_dead(r, revoked, dead)
            }
            live = [r for r in rows if r["live"]]
            if work is None:
                dead |= await _revoke_all(live, chunk, revoked, dead, revoked_by)
            else:
                dead |= await _revoke_all(live, chunk, revoked, dead, revoked_by, work=work)
            # Revoked ones too: a retry still reaches what their tokens made.
            made |= {str(r["sa_subject"]) for r in rows}
        made -= seen
        seen |= made
        creators = sorted(made)
    return dead


def _is_dead(row: dict[str, Any], revoked: dict[str, int | None], dead: set[str]) -> bool:
    """Whether the token `row` lists is dead: a token above it is, or a subject
    above it is revoked outright, or narrowed to a cutoff later than the row's
    `created_at`. Judged by the row's `created_at`, which is never earlier
    than the chain's oldest `iat`: a token this calls live may still be dead
    at the gate (a revocation landed between its mint's check and its save),
    never the reverse."""
    from server.auth import revocation

    above = _above(row)
    if above & dead:
        return True
    cutoffs = [revoked[s] for s in above if s in revoked]
    if not cutoffs:
        return False
    made_at = int(as_aware_utc(row["created_at"]).timestamp())
    return any(revocation.kills(cutoff, made_at) for cutoff in cutoffs)


def _above(row: dict[str, Any]) -> set[str]:
    """The subjects above the token `row` lists: its creator and its lineage."""
    return {str(row["created_by"]), *row["lineage"]}


async def _revoke_all(
    found: list[dict[str, Any]],
    creators: list[str],
    revoked: dict[str, int | None],
    dead: set[str],
    revoked_by: str,
    *,
    work: _RevocationWork | None = None,
) -> set[str]:
    """Revoke those of the rows `found` whose token is dead, each name claimed
    as a create claims it and read again under it: a revoked copy of a row
    read before must not replace a newer one with the name. A task holds one
    claim at a time, so two passes over the same names never wait on each
    other in a cycle, and each row that lands stays revoked for a retry.
    Returns the `sa:` subjects revoked."""
    from server.auth.grant_claims import service_account_change
    from server.core.ch import get_client

    ch = get_client() if work is None else work.ch
    gate = asyncio.Semaphore(_CLAIMS_AT_ONCE if work is None else 4)
    listed = {str(r["sa_subject"]) for r in found}

    async def revoke(org: str, name: str) -> str | None:
        async with gate:
            bounded: dict[str, Any] = {} if work is None else {"deadline": work.deadline, "ch": ch}
            if work is not None:
                work.statements(6)  # The existing finite claim's worst path, including release.
            async with service_account_change(org, name, **bounded):
                return await _revoke_named(
                    org,
                    name,
                    creators,
                    revoked,
                    dead,
                    revoked_by,
                    listed,
                    ch=ch,
                    work=work,
                )

    try:
        async with asyncio.TaskGroup() as tasks:
            done = [tasks.create_task(revoke(r["org"], r["name"])) for r in found]
    except ExceptionGroup as e:  # the failure itself, which the caller maps to a 503
        raise e.exceptions[0] from e
    return {subject for task in done if (subject := task.result()) is not None}


async def _revoke_named(
    org: str,
    name: str,
    creators: list[str],
    revoked: dict[str, int | None],
    dead: set[str],
    revoked_by: str,
    listed: set[str],
    *,
    ch: Any,
    work: _RevocationWork | None,
) -> str | None:
    from server.auth import revocation
    from server.auth.authz import identity_write
    from server.core.ch import own_write_reads
    from server.core.datetimes import naive_utc_now_seconds

    if work is None:
        with own_write_reads():
            row = await ch.query_one(
                "SELECT * FROM loom.service_accounts FINAL "
                "WHERE org = {org:String} AND name = {name:String} "
                f"AND {_MADE_BY} AND revoked_at IS NULL AND {_UNEXPIRED}",
                params={"org": org, "name": name, "subs": creators},
            )
    else:
        rows = await work.read(
            "org = {org:String} AND name = {name:String} "
            f"AND {_MADE_BY} AND revoked_at IS NULL AND {_UNEXPIRED}",
            {"org": org, "name": name, "subs": creators},
            point=True,
        )
        row = rows[0] if rows else None
    if row is None:
        return None
    row = dict(row)
    # A replacement under the name may postdate the discovery's revocation view.
    now_revoked = revoked
    if row["sa_subject"] not in listed:
        now_revoked = (
            await revocation.cutoffs(sorted(_above(row)), permanent=True)
            if work is None
            else await work.cutoffs(sorted(_above(row)))
        )
    if not _is_dead(row, now_revoked, dead):
        return None
    revoked_copy = revoked_row(row, revoked_at=naive_utc_now_seconds(), revoked_by=revoked_by)
    with identity_write():
        if work is None:
            await ch.insert("loom.service_accounts", [revoked_copy])
        else:
            await work.insert(revoked_copy)
            work.confirmed_dead.add(str(row["sa_subject"]))
    return str(row["sa_subject"])


_SA_COLUMNS = (
    "org",
    "name",
    "sa_subject",
    "namespaces",
    "roles",
    "ttl_days",
    "created_by",
    "created_at",
    "expires_at",
    "revoked_at",
    "revoked_by",
    "version",
    "kind",
    "client",
    "inherit_creator_grants",
    "ns_roles",
    "lineage",
    "saves_to",
)


@dataclass
class _RevocationWork:
    """One finite descendant pass; counters are neither a cache nor a replay record."""

    ch: Any
    deadline: float
    roots: set[str]
    fetched: int = 0
    used_statements: int = 0
    mutations: int = 0
    subjects: set[str] = field(default_factory=set)
    confirmed_dead: set[str] = field(default_factory=set)

    def statements(self, count: int) -> None:
        from server.core.ch_bounded import BoundedReadError

        if self.used_statements + count > 1024:
            raise BoundedReadError("Descendant statements exceed their allowance")
        self.used_statements += count

    async def read(
        self,
        predicate: str,
        params: dict[str, Any],
        *,
        point: bool = False,
    ) -> list[dict[str, Any]]:
        from server.core.ch_bounded import BoundedReadError, read_rows

        maximum = min(1 if point else 256, 512 - self.fetched)
        if maximum <= 0:
            raise BoundedReadError("Descendant rows exceed their allowance")
        self.statements(1)
        dates = {"created_at", "expires_at", "revoked_at"}
        projection = ", ".join(
            f"toUnixTimestamp64Milli(toDateTime64({key}, 3)) AS {key}" if key in dates else key
            for key in _SA_COLUMNS
        )
        # Reserve before awaiting: four point readers share this same finite pass.
        self.fetched += maximum
        rows = await read_rows(
            "SELECT "
            + projection
            + " FROM (SELECT "
            + ", ".join(_SA_COLUMNS)
            + " FROM loom.service_accounts FINAL WHERE "
            + predicate
            + " LIMIT {limit:UInt32}) FORMAT JSONEachRow",
            {**params, "limit": maximum + 1},
            ch=self.ch,
            deadline=self.deadline,
            row_bytes=8191,
            total_bytes=maximum * 8192,
            max_rows=maximum,
            tag="cell.each.subject_descendants",
        )
        # Charging the full row allowance covers whitespace and newlines across every read.
        self.fetched -= maximum - len(rows)
        for row in rows:
            self._row(row)
            subject = row["sa_subject"]
            if subject not in self.roots and subject not in self.subjects:
                if len(self.subjects) == 256:
                    raise BoundedReadError("Descendant subjects exceed their allowance")
                self.subjects.add(subject)
        return rows

    def _row(self, row: dict[str, Any]) -> None:
        from server.core.ch_bounded import BoundedReadError

        if set(row) != set(_SA_COLUMNS):
            raise BoundedReadError("Malformed descendant row")
        for key in (
            "org",
            "name",
            "sa_subject",
            "created_by",
            "revoked_by",
            "kind",
            "client",
            "ns_roles",
            "saves_to",
        ):
            if type(row[key]) is not str:
                raise BoundedReadError("Malformed descendant identity")
        for key in ("namespaces", "roles", "lineage"):
            if type(row[key]) is not list or any(type(value) is not str for value in row[key]):
                raise BoundedReadError("Malformed descendant collection")
        if (
            type(row["version"]) is not int
            or not 0 <= row["version"] < 2**64 - 1
            or type(row["ttl_days"]) is not int
            or type(row["inherit_creator_grants"]) not in (int, bool)
            or row["inherit_creator_grants"] not in (0, 1)
        ):
            raise BoundedReadError("Malformed descendant row")
        for key in ("created_at", "expires_at", "revoked_at"):
            if row[key] is None and key != "created_at":
                continue
            if type(row[key]) is not int:
                raise BoundedReadError("Malformed descendant timestamp")
            try:
                row[key] = datetime(1970, 1, 1) + timedelta(milliseconds=row[key])
            except (ValueError, OverflowError) as error:
                raise BoundedReadError("Malformed descendant timestamp") from error
        row["live"] = row["revoked_at"] is None

    async def cutoffs(self, subjects: list[str]) -> dict[str, int | None]:
        from server.auth import revocation

        self.statements((len(subjects) + 4) // 5)
        return await revocation.cutoffs_bounded(
            subjects,
            permanent=True,
            ch=self.ch,
            deadline=self.deadline,
        )

    async def insert(self, row: dict[str, Any]) -> None:
        from server.core.ch import set_query_tag, statement_timeout
        from server.core.ch_bounded import BoundedReadError

        remaining = min(2.0, self.deadline - time.monotonic())
        if self.mutations >= 256 or remaining <= 0:
            raise BoundedReadError("Descendant mutation allowance exhausted")
        self.statements(1)
        self.mutations += 1
        set_query_tag("cell.each.subject_descendant_write")
        async with asyncio.timeout(remaining):
            with statement_timeout(remaining):
                await self.ch.insert(
                    "loom.service_accounts",
                    [row],
                    settings={
                        "async_insert": 0,
                        "wait_for_async_insert": 1,
                        "max_memory_usage": 128 * 1024**2,
                    },
                )


async def revoke_created_by_bounded(
    subject: str,
    *,
    revoked_by: str,
    ch: Any,
    deadline: float,
    progress: dict[str, int],
) -> set[str]:
    work = _RevocationWork(ch=ch, deadline=deadline, roots={subject})
    try:
        return await revoke_created_by([subject], revoked_by=revoked_by, work=work)
    finally:
        progress["tokens"] = len(work.confirmed_dead)


__all__ = [
    "SACreator",
    "hand_down_lineage",
    "inheriting_creator",
    "revoke_created_by",
    "revoked_row",
    "sa_creator",
]
