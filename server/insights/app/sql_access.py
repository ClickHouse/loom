"""`loom sql-access`: make, rescope and remove the SQL users of the SQL
surface (docs/reference/sql-surface.md). Each user reads the `sql_v1_*`
views and nothing else, and the views admit only the (org, namespace)
pairs `loom.sql_access` grants it.

Order keeps every step fail-closed: a new user exists, with no grant, before
its scope rows are written, and gets its grants last; a rescope writes the
rows that stay and go in one insert; a revocation takes the rows away before
the user goes. A step that fails leaves no granted row without its user.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from server.code.domain.index.repos import config_entries
from server.core.ch_names import DEFAULT_READER
from server.core.datetimes import utc_now
from server.core.repo_base import _CHClient, _default_client
from server.insights.domain import analysis as boundary
from server.insights.domain import sql_surface as surface
from server.insights.store import sql_access as store
from server.settings import settings


class AccessRefused(Exception):
    """A request `loom sql-access` will not carry out; the message says why."""


class LoginUnchecked(Exception):
    """A login check that could not run; the message says why."""


@dataclass(frozen=True)
class Granted:
    user: str
    created: bool
    # Set when the user was made or its password rotated: shown once, never stored.
    password: str | None
    rows: set[tuple[str, str]]


# Why a login of a SQL user is unsafe ('' when it is not), raising
# LoginUnchecked when it cannot tell: it runs one statement as the user and so
# needs its password.
LoginCheck = Callable[[str, str], str]


def config_restricted() -> set[tuple[str, str]]:
    """The namespaces this process's code repo configuration classifies
    restricted. The views cannot read it, so a grant checks it here: run the
    command with the server's configuration. A maintainer's access row is in
    loom.code_repos, which the views read themselves, so the configuration
    alone (before the registry's first read) is what this check needs."""
    return {
        (cfg.org, cfg.namespace) for cfg in config_entries() if cfg.classification == "restricted"
    }


def _reserved() -> set[str]:
    reader = settings.ch_reader_user or DEFAULT_READER
    return {"default", settings.ch_user, reader, boundary.ANALYST_USER}


async def grant(
    user: str,
    scope: surface.Scope,
    *,
    login_check: LoginCheck,
    rotate: bool = False,
    ch: _CHClient | None = None,
) -> Granted:
    """Make `user` read exactly `scope`, creating it when there is none. A
    user whose password this call sets is checked with `login_check` before it
    gets any scope or grant, and dropped when the check fails."""
    ch = ch or _default_client()
    if problem := surface.user_name_problem(user, _reserved()) or surface.scope_problem(scope):
        raise AccessRefused(problem)
    by_config = config_restricted()
    by_directory = await store.restricted(scope.org, ch=ch)
    if reached := surface.restricted_reach(scope, by_config, by_directory):
        names = ", ".join(f"{org}/{ns}" for org, ns in reached)
        raise AccessRefused(
            f"the scope reaches restricted namespaces ({names}): Loom audits each read of a "
            "restricted namespace, which a SQL view cannot, so grant the other namespaces by name"
        )
    kind = await store.user_kind(user, ch=ch)
    if kind == "other":
        raise AccessRefused(
            f"a ClickHouse user named {user} exists and is not a SQL user of the surface "
            f"(it does not carry the {surface.PROFILE} profile): pick another name"
        )
    caps = surface.Caps(
        settings.ch_analysis_max_execution_s,
        settings.ch_analysis_max_rows_to_read,
        settings.ch_analysis_max_memory_bytes,
        settings.ch_analysis_max_threads,
        settings.ch_analysis_max_concurrent,
    )
    await store.run(surface.profile_sql(caps), ch=ch)
    password: str | None = None
    if not kind or rotate:
        password = surface.draw_password()
        made = surface.create_user_sql if not kind else surface.password_sql
        await store.run([made(user, password)], ch=ch)
        await _check_login(user, password, login_check, created=not kind, ch=ch)
    try:
        held, last = await store.held(user, ch=ch)
        now = surface.next_version(utc_now(), last)
        await store.write_rows(surface.access_rows(user, scope, held, now), ch=ch)
        await store.run(surface.grant_sql(store.target_database(), user), ch=ch)
    except Exception as e:  # re-raised: the new password is never shown, so say to rotate
        if password is None:
            raise
        raise AccessRefused(
            f"{user} has a new password but its scope or grants were not written; "
            "run grant again with --rotate"
        ) from e
    return Granted(user, not kind, password, scope.rows())


async def _check_login(
    user: str, password: str, login_check: LoginCheck, *, created: bool, ch: _CHClient
) -> None:
    """Refuse a login `login_check` finds unsafe, dropping the user. One it
    could not check drops a user this call created; an existing user keeps its
    scope and grants, with a password no one holds until a rerun rotates it."""
    try:
        problem = await asyncio.to_thread(login_check, user, password)
    except LoginUnchecked as e:
        if created:
            await revoke(user, ch=ch)
            raise AccessRefused(f"{user} was dropped: its login could not be checked") from e
        raise AccessRefused(
            f"{user}'s new password could not be checked; its scope is unchanged: "
            "run grant again with --rotate"
        ) from e
    if problem:
        await revoke(user, ch=ch)
        raise AccessRefused(f"{user} was dropped: {problem}")


@dataclass(frozen=True)
class Revoked:
    held: set[tuple[str, str]]
    # Whether a SQL user of the surface by that name was there to drop.
    dropped: bool


async def revoke(user: str, *, ch: _CHClient | None = None) -> Revoked:
    """Take every scope row from `user`, then drop it. The rows go even when
    another ClickHouse user holds the name, since taking a scope away only
    narrows what anyone reads; that user stays."""
    ch = ch or _default_client()
    if problem := surface.user_name_problem(user, _reserved()):
        raise AccessRefused(problem)
    held, last = await store.held(user, ch=ch)
    if held:
        now = surface.next_version(utc_now(), last)
        await store.write_rows(surface.revoke_rows(user, held, now), ch=ch)
    kind = await store.user_kind(user, ch=ch)
    if kind == "other":
        raise AccessRefused(
            f"{user} is not a SQL user of the surface (it does not carry the "
            f"{surface.PROFILE} profile): its scope rows are revoked, drop the user yourself"
        )
    await store.run([f"DROP USER IF EXISTS `{user}`"], ch=ch)
    return Revoked(held, kind == "surface")
