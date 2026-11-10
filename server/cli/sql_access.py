"""`loom sql-access`: the SQL users that read a tenant's data through the SQL
surface's views (docs/reference/sql-surface.md)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

REFUSED = 2
# not a setting: a few seconds for a replica to see a user just created, not a knob
LOGIN_ATTEMPTS = 5


def _scope(args: argparse.Namespace) -> Any:
    from server.insights.domain.sql_surface import ALL, Scope

    namespaces = tuple(ns.strip() for ns in args.namespaces.split(",")) if args.namespaces else ()
    # With --all, any namespace named is refused by the scope check; `*` is
    # not an org name (Scope reads it as --all).
    org = ALL if args.all else ("" if args.org == ALL else args.org)
    return Scope(org, namespaces)


def _login_check(user: str, password: str) -> str:
    """Why a login of `user` is unsafe, or '': it must not read the system
    tables that show every user's statements, which ClickHouse refuses only with
    `access_control_improvements.select_from_system_db_requires_grant` on.
    `system.processes` always exists, unlike `system.query_log`. A replica that
    has not seen the new user yet refuses the login, so it is tried a few times."""
    import time

    import clickhouse_connect

    from server.core.ch_errors import error_code, error_line
    from server.core.ch_target import connection_kwargs
    from server.core.schema.preflight import ACCESS_DENIED, AUTH_FAILED
    from server.insights.app.sql_access import LoginUnchecked

    for attempt in range(LOGIN_ATTEMPTS):
        try:
            ch = clickhouse_connect.get_client(
                **{**connection_kwargs(), "username": user, "password": password},
                connect_timeout=10,
            )
            break
        except Exception as e:  # re-raised: a login that cannot be checked is refused
            if error_code(e) not in AUTH_FAILED or attempt == LOGIN_ATTEMPTS - 1:
                raise LoginUnchecked(f"could not log in as it: {error_line(e, 160)}") from e
            time.sleep(1)
    try:
        ch.query("SELECT count() FROM system.processes")
    except Exception as e:  # re-raised unless it is the refusal wanted
        if error_code(e) == ACCESS_DENIED:
            return ""
        raise LoginUnchecked(f"could not read as it: {error_line(e, 160)}") from e
    finally:
        ch.close()
    return (
        "it can read system.processes, which shows every user's statements: turn on "
        "access_control_improvements.select_from_system_db_requires_grant in the server's config"
    )


def _connected(work: Any) -> Any:
    """Run `work(ch)` on one plain connection: the command calls no model."""
    from server.core.ch import get_client

    async def _go() -> Any:
        ch = get_client()
        await ch.connect()
        try:
            return await work(ch)
        finally:
            await ch.disconnect()

    return asyncio.run(_go())


def _cmd_grant(args: argparse.Namespace) -> int:
    from server.insights.app.sql_access import AccessRefused, grant

    try:
        done = _connected(
            lambda ch: grant(
                args.user, _scope(args), login_check=_login_check, rotate=args.rotate, ch=ch
            )
        )
    except AccessRefused as e:
        print(f"  ✗ {e}", file=sys.stderr)
        if e.__cause__ is not None:
            # The operator ran the command: the cause is theirs to read.
            print(f"    {e.__cause__}", file=sys.stderr)
        return REFUSED
    verb = "created" if done.created else "updated"
    reads = ", ".join(f"{org}/{ns}" for org, ns in sorted(done.rows))
    print(f"  ✓ {done.user} {verb}; it reads {reads}")
    if done.password:
        # Shown once: Loom keeps no copy.
        print(f"  password: {done.password}")
    return 0


def _cmd_revoke(args: argparse.Namespace) -> int:
    from server.insights.app.sql_access import AccessRefused, revoke

    try:
        done = _connected(lambda ch: revoke(args.user, ch=ch))
    except AccessRefused as e:
        print(f"  ✗ {e}", file=sys.stderr)
        return REFUSED
    if not done.dropped and not done.held:
        print(f"  · no SQL user of the surface named {args.user}, and no scope rows")
        return 0
    state = "dropped" if done.dropped else "had no user to drop"
    print(f"  ✓ {args.user} {state}; {len(done.held)} scope row(s) revoked")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    from server.insights.app.sql_access import config_restricted
    from server.insights.domain.sql_surface import ALL, Scope, restricted_reach
    from server.insights.store.sql_access import grants

    by_config = config_restricted()
    status = 0
    for row in _connected(lambda ch: grants(ch=ch)):
        org, ns = str(row["org"]), str(row["namespace"])
        print(f"  {row['user_name']}\t{org}/{ns}\t{row['updated_at']}")
        # The views cannot read the code repo configuration: a grant made before a
        # namespace was restricted there now reaches it.
        if reached := restricted_reach(Scope(org, (ns,) if org != ALL else ()), by_config, set()):
            names = ", ".join(f"{o}/{n}" for o, n in reached)
            print(
                f"    ✗ reaches {names}, restricted by the code repo configuration: grant "
                "this user the other namespaces by name",
                file=sys.stderr,
            )
            status = REFUSED
    return status


def register_sql_access_cli(sub: Any) -> None:
    p = sub.add_parser(
        "sql-access",
        help="Make, rescope or drop a ClickHouse user that reads a tenant's memory, "
        "conversations and request ledger through the sql_v1_* views.",
    )
    cmds = p.add_subparsers(dest="sql_access_cmd", required=True)
    g = cmds.add_parser(
        "grant",
        help="Make the user read exactly this scope, creating it (and printing its password "
        "once) when there is none.",
    )
    g.add_argument("--user", required=True, help="The ClickHouse user name")
    which = g.add_mutually_exclusive_group(required=True)
    which.add_argument("--org", default="", help="The org the user reads")
    g.add_argument(
        "--namespaces",
        default="",
        help="Comma-separated namespaces of the org, or * for every one of them",
    )
    which.add_argument(
        "--all",
        action="store_true",
        help="Every org and namespace: for a deployment whose data belongs to one org",
    )
    g.add_argument(
        "--rotate", action="store_true", help="Give an existing user a new password, printed once"
    )
    g.set_defaults(func=_cmd_grant)
    r = cmds.add_parser("revoke", help="Take the user's scope away, then drop the user.")
    r.add_argument("--user", required=True, help="The ClickHouse user name")
    r.set_defaults(func=_cmd_revoke)
    ls = cmds.add_parser("list", help="Every granted scope row, by SQL user.")
    ls.set_defaults(func=_cmd_list)
