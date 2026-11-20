"""Fresh provisioning census before a cell accepts SCIM enforcement."""

from __future__ import annotations

import asyncio
import getpass
import json
import math
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from server.auth import claims, revocation, scim, scim_people, subject_revocation
from server.auth.scim_routes import scim_settings_problem
from server.cli import cell_control
from server.core import cell, ch
from server.core.cell_ch import WHOLE_SQL, Refusal, Refused
from server.core.datetimes import as_aware_utc
from server.settings import settings

_PAGE = 128  # not a setting: bounded identity metadata transferred per census read

# The cursor is the actual FINAL row key, never a subject spelling or OFFSET.
_SOURCES = {
    "accounts": (
        "loom.namespaces",
        "org",
        "namespace",
        "owner_email",
        "''",
        "''",
        "toUInt32(0)",
        "ifNull(active, true) = true",
    ),
    "sessions": (
        "loom.oauth_state",
        "''",
        "id",
        "subject",
        "verified_email",
        "family",
        "generation",
        "record_type = 'session_refresh' AND expires_at > toUInt32(now())",
    ),
    "services": (
        "loom.service_accounts",
        "org",
        "name",
        "sa_subject",
        "''",
        "''",
        "toUInt32(0)",
        "revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())",
    ),
}


def required(*values: Mapping[str, Any] | None) -> bool:
    enabled = False
    for value in values:
        if value is None:
            continue
        if not cell.matches({}, value):
            raise _refuse("an accepted configuration fingerprint is unreadable")
        scim_on = value["fields"].get("scim", cell.DEFAULTS["scim"])
        if type(scim_on) is not bool:
            raise _refuse("an accepted configuration has an unreadable SCIM policy")
        enabled = enabled or scim_on
    return enabled


def _refuse(reason: str) -> Refused:
    return Refused([Refusal("SCIM provisioning", reason)])


def _same(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, default=str) == json.dumps(
        right, sort_keys=True, default=str
    )


async def _identity(conn: Any, row: dict[str, Any], own: tuple[Any, Any]) -> dict[str, Any]:
    await cell_control.paired(conn, row)
    found = await conn.fetchrow(
        "SELECT a.deployment_id, f.is_first, f.cell, m.mode, m.since, "
        "s.current_fp, s.next_fp, s.updated_at FROM loom.authority a "
        "LEFT JOIN loom.cell_first f ON true LEFT JOIN loom.tenancy_mode m ON true "
        "LEFT JOIN loom.cell_self s ON true"
    )
    if (
        found is None
        or found["deployment_id"] != row["deployment_id"]
        or found["cell"] != row["cell"]
        or type(found["is_first"]) is not bool
        or found["is_first"] is not row["is_first"]
        or found["mode"] not in ("recording", "enforced")
        or (not found["is_first"] and found["mode"] != "enforced")
    ):
        raise _refuse("the local deployment, first identity or tenancy mode differs")
    actual = (cell.json_object(found["current_fp"]), cell.json_object(found["next_fp"]))
    if actual[0] is None or not _same(actual, own):
        raise _refuse("the accepted local fingerprint pair changed")
    if row.get("current_fp") is not None:
        reserved = (row["current_fp"], row.get("next_fp"))
        if any(
            value is not None and not any(_same(value, item) for item in reserved)
            for value in actual
        ):
            raise _refuse("the local fingerprint pair differs from the directory reservation")
    elif found["is_first"] is not True:
        raise _refuse("a later cell has no acknowledged directory fingerprint pair")
    for value in actual:
        if (
            value is not None
            and value.get("fields", {}).get("deployment_id") != row["deployment_id"]
        ):
            raise _refuse("an accepted fingerprint names another deployment")
    configured = scim.tokens(settings.scim_token)
    problem = scim_settings_problem(
        settings.scim_token,
        settings.oauth_login_provider,
        settings.auth_actually_required(),
        cell_name=row["cell"],
        first=(found["is_first"], found["cell"]),
    )
    if not configured or problem:
        raise _refuse(problem or "the accepted SCIM policy has no configured token")
    return dict(found)


def _page_sql(kind: str) -> str:
    table, key0, key1, subject, email, family, generation, predicate = _SOURCES[kind]
    fields = (key0, key1, "org", subject, email, family)
    size = " + ".join(f"length({field})" for field in fields)
    bounded = ", ".join(f"if(length({field}) <= {{bytes:UInt32}}, {field}, '')" for field in fields)
    return (
        f"SELECT count() AS n, sum(identity_bytes) AS bytes, "
        f"if(bytes <= {{bytes:UInt32}}, groupArray({_PAGE})(identity), []) AS identities FROM ("
        f"SELECT tuple({bounded}, {generation}, toUnixTimestamp(created_at)) AS identity, "
        f"{size} AS identity_bytes FROM {table} FINAL WHERE ({predicate}) AND "
        f"({{started:UInt8}} = 0 OR ({key0}, {key1}) > ({{after0:String}}, {{after1:String}})) "
        f"ORDER BY {key0}, {key1} LIMIT {_PAGE})"
    )


async def _query(client: Any, sql: str, params: dict[str, Any], deadline: float) -> Any:
    if scim_people._encoded(params) > scim_people.MAX_BYTES:
        raise _refuse("a census cursor exceeds its supported parameter size")
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("SCIM provisioning census exhausted its scan budget")
    seconds = min(left, scim_people._decision_seconds())
    async with asyncio.timeout(seconds):
        with ch.statement_timeout(seconds):
            return await client.query_one(sql + " " + WHOLE_SQL, params=params)


async def _refuses_person(org: str, subject: str, email: str, deadline: float) -> bool:
    try:
        extra = scim_people._roots(subject, email, (), org)
        seconds = min(deadline - time.monotonic(), scim_people._decision_seconds())
        if seconds <= 0:
            raise TimeoutError
        decision = scim_people._Decision(
            org, subject, scim.normalized(email), time.monotonic() + seconds
        )
        async with asyncio.timeout(seconds):
            return await decision.refuses((subject, *extra))
    except (scim_people._Refused, UnicodeError):
        return True


async def _current_session(client: Any, identity: tuple[Any, ...], deadline: float) -> bool:
    _key0, rid, _org, _subject, _email, family, generation, _created = identity
    if not family:
        raise _refuse("a refresh grant has no session family")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    async with asyncio.timeout(min(remaining, scim_people._decision_seconds())):
        with ch.statement_timeout(min(remaining, scim_people._decision_seconds())):
            current = await claims.get(client, "family:" + family)
    if current is None:
        return False
    if current == f"{generation}:{rid}":
        return True
    parts = current.split(":")
    if len(parts) < 2 or not parts[0].isdigit():
        raise _refuse("a refresh session has unreadable current state")
    if parts[0] == str(generation) and len(parts) == 4 and parts[1] == "spending":
        return True  # A put-back or takeover can still restore this grant.
    if int(parts[0]) > int(generation) or (len(parts) == 2 and parts[1] == "ended"):
        return False
    raise _refuse("a refresh grant is ahead of its session state")


async def _cutoff(
    conn: Any,
    org: str,
    subject: str,
    floor: datetime,
    *,
    contract: bool,
    log: Callable[[str], None],
    report: dict[str, Any],
) -> bool:
    # The same subject lease serializes deactivation, reactivation and --scim.
    async with scim.subject_leases([subject]):
        existing = (await revocation.scim_revocations([subject])).get(subject)
        if existing is not None and (
            existing["issued_before"] is None or as_aware_utc(existing["issued_before"]) >= floor
        ):
            return True
        if not contract:
            return False
        if subject_revocation.refusal(subject):
            raise _refuse("an unprovisioned account has no revocable subject")
        if subject.lower().startswith("sa:"):
            name = subject.lower().removeprefix("sa:").partition(":")[0]
            registered = await ch.get_client().query_one(
                "SELECT active FROM loom.machine_identities FINAL "
                "WHERE name = {name:String} LIMIT 1",
                params={"name": name},
            )
            if registered is not None:
                raise _refuse("a registered machine identity cannot be revoked by SCIM attach")
        await cell_control.check_lock(conn)
        cutoff = revocation.from_now_on()
        if floor > cutoff:
            raise _refuse("a stored identity timestamp is ahead of the supported clock-skew bound")
        by = "cli:" + getpass.getuser()
        written = await revocation.scim_revoke(
            subject,
            reason="scim: unprovisioned at cell attach",
            revoked_by=by,
            issued_before=cutoff,
            reread=False,
        )
        # A later audit/read failure cannot undo this accepted contraction.
        report["revoked"] += 1
        log(f"SCIM cutoff accepted for {subject!r}; it remains if a later check fails")
        confirmed = (await revocation.scim_revocations([subject])).get(subject)
        if confirmed is None or (
            confirmed["issued_before"] is not None
            and as_aware_utc(confirmed["issued_before"]) < cutoff
        ):
            raise _refuse("an accepted SCIM cutoff could not be confirmed; it is not rolled back")
        await subject_revocation.audit_revocation(
            written, token_id=subject, kind="subject", by=by, org=org, scim=True
        )
        await cell_control.check_lock(conn)
        return True


async def check(
    client: Any,
    conn: Any,
    row: dict[str, Any],
    own: tuple[Any, Any],
    *,
    seconds: float,
    revoke: bool = False,
    dry_run: bool = False,
    target: dict[str, Any] | None = None,
    report: dict[str, Any] | None = None,
    log: Callable[[str], None] = lambda _message: None,
) -> None:
    """Read the intended policy freshly; contract only under the native attach owner."""
    if not (required(target) if target is not None else required(*own)):
        return
    if not math.isfinite(seconds) or seconds <= 0:
        raise _refuse("the provisioning census needs a finite positive scan budget")
    if client is not ch.get_client():
        raise _refuse("the census client differs from this process's configured store")
    if target is not None and target.get("fields", {}).get("deployment_id") != row.get(
        "deployment_id"
    ):
        raise _refuse("the intended configuration names another deployment")
    status = report if report is not None else {}
    status.update(checked=0, revoked=0, unprovisioned=0, complete=False)
    deadline = time.monotonic() + seconds
    started = datetime.now(UTC)
    try:
        async with asyncio.timeout(seconds):
            with ch.own_write_reads(), ch.query_tag("cli.cell.scim_precondition"):
                binding = await _identity(conn, row, own)
                floor = revocation.past_every_token_before(
                    max(as_aware_utc(binding["since"]), as_aware_utc(binding["updated_at"]))
                )
                # Recording processes can still mint without positive provisioning.
                if binding["mode"] != "enforced" or not required(own[0]):
                    floor = max(floor, revocation.past_every_token_before(started))
                for kind in _SOURCES:
                    await _source(
                        client,
                        conn,
                        kind,
                        floor,
                        deadline,
                        contract=revoke and not dry_run,
                        status=status,
                        log=log,
                    )
                after = await _identity(conn, row, own)
                if not _same(binding, after):
                    raise _refuse("local acceptance changed during the provisioning census")
                status["complete"] = True
    except Refused:
        raise
    except Exception as error:  # Incomplete identity reads never establish provisioning.
        raise _refuse(
            f"the provisioning census is unavailable ({type(error).__name__}); "
            f"{status['revoked']} accepted cutoffs remain; rerun the same command"
        ) from error


async def _source(
    client: Any,
    conn: Any,
    kind: str,
    floor: datetime,
    deadline: float,
    *,
    contract: bool,
    status: dict[str, Any],
    log: Callable[[str], None],
) -> None:
    cursor = ("", "")
    started = False
    while True:
        page = await _query(
            client,
            _page_sql(kind),
            {
                "started": int(started),
                "after0": cursor[0],
                "after1": cursor[1],
                "bytes": scim_people.MAX_BYTES,
            },
            deadline,
        )
        if page is None or int(page["n"]) > _PAGE or int(page["bytes"]) > scim_people.MAX_BYTES:
            raise _refuse(f"the {kind} census page exceeds its supported metadata size")
        values = sorted(tuple(value) for value in page["identities"])
        if len(values) != int(page["n"]):
            raise _refuse(f"the {kind} census page is incomplete")
        if not values:
            break
        for identity in values:
            if len(identity) != 8:
                raise _refuse(f"the {kind} census row is unreadable")
            key0, key1, org, subject, email, _family, _generation, created = identity
            key = (key0, key1)
            if (started and key <= cursor) or any(
                not isinstance(value, str) or "\x00" in value for value in identity[:6]
            ):
                raise _refuse(f"the {kind} census cursor or identity is unreadable")
            cursor, started = key, True
            # An ownerless namespace is a resource, not a person's account.
            if kind == "accounts" and subject == "":
                continue
            if kind == "sessions" and not await _current_session(client, identity, deadline):
                continue
            status["checked"] += 1
            if not await _refuses_person(org, subject, email, deadline):
                continue
            status["unprovisioned"] += 1
            if not subject.strip():
                raise _refuse(f"an active {kind} row has no revocable owner")
            row_floor = max(
                floor, revocation.past_every_token_before(datetime.fromtimestamp(created, UTC))
            )
            if not await _cutoff(
                conn,
                org,
                subject,
                row_floor,
                contract=contract,
                log=log,
                report=status,
            ):
                raise _refuse(
                    f"unprovisioned {kind} subject {subject!r} in org {org!r}; "
                    "provision its complete identity or use --revoke-unprovisioned; "
                    "a dry run never revokes"
                )
        if len(values) < _PAGE:
            break
