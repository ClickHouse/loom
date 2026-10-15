"""`loom ch-setup`: prepare a ClickHouse that Loom did not start itself, a
self-managed server or a ClickHouse Cloud service, for Loom's first boot.

It creates the `loom` database and the read-only reader user
(`LOOM_CH_READER_USER`, default `loom_reader`): SELECT on `loom.*` and a
settings profile that pins Loom's caller-shaped caps as ceilings, with floors
no statement can go under. Run it again at any time: what is in place is left
alone, and a reader whose profile no longer matches the caps is rebuilt.

A user without the privilege for a step gets the SQL to hand an admin instead,
with the password left as a placeholder. The bundled ClickHouse defines the
reader in its config, so there it only reports, and an existing user that
ch-setup did not make is never replaced.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

from server.core.ch_errors import error_code
from server.core.schema import preflight

DEFAULT_READER = "loom_reader"
_PASSWORD_PLACEHOLDER = "'<LOOM_CH_PASSWORD>'"
# Names go into the SQL between backticks; anything else is refused.
_NAME = re.compile(r"^[A-Za-z0-9_.@-]+$")


def _quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _number(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)


def _caps() -> dict[str, float]:
    """The reader profile's caps: Loom's caller-shaped ceilings (0 = none)."""
    from server.settings import settings

    return {
        "max_execution_time": float(settings.ch_caller_shaped_max_execution_s),
        "max_memory_usage": float(settings.ch_caller_shaped_max_memory_bytes),
        "max_rows_to_read": float(settings.ch_caller_shaped_max_rows_to_read),
    }


def reader_sql(reader: str, password: str) -> list[str]:
    """The reader's profile, user and grant; `password` is an SQL literal."""
    from server.code.domain.deadline import STATEMENT_FLOOR_S

    settings = ["readonly = 2"]
    for name, cap in _caps().items():
        if not cap:
            settings.append(f"{name} = 0")  # no cap asked for: none pinned either
            continue
        # The floor keeps a statement from sending 0, which ClickHouse reads as no limit.
        floor = STATEMENT_FLOOR_S if name == "max_execution_time" else 1
        settings.append(f"{name} = {_number(cap)} MIN {_number(floor)} MAX {_number(cap)}")
    settings.append("read_overflow_mode = 'throw' CONST")
    return [
        f"CREATE SETTINGS PROFILE OR REPLACE `{reader}` SETTINGS {', '.join(settings)}",
        f"CREATE USER OR REPLACE `{reader}` IDENTIFIED BY {password} SETTINGS PROFILE '{reader}'",
        f"GRANT SELECT ON {preflight.DATABASE}.* TO `{reader}`",
    ]


def admin_sql(user: str, reader: str) -> list[str]:
    """Everything `ch-setup` does, for an admin to run on behalf of `user`."""
    return [
        f"CREATE DATABASE IF NOT EXISTS {preflight.DATABASE}",
        f"GRANT CURRENT GRANTS ON {preflight.DATABASE}.* TO `{user}`",
        # system.query_log among them shows every user's queries on this server;
        # docs/operations/deployment.md says how to narrow it.
        f"GRANT {', '.join(f'SELECT ON system.{t}' for t in preflight.SYSTEM_TABLES)} TO `{user}`",
        # clusterAllReplicas, which asks every replica for its version. READ only:
        # on Cloud plain REMOTE also grants writes to any host through remote().
        f"GRANT READ ON REMOTE TO `{user}`",
        f"-- replaces any existing user and settings profile named {reader}",
        *reader_sql(reader, _PASSWORD_PLACEHOLDER),
    ]


def reader_problem(reader: str) -> str:
    """Why `reader` is not ready ('' when it is): it must log in with
    LOOM_CH_PASSWORD, read `loom.*` and run under the current caps."""
    import clickhouse_connect

    from server.core.ch_target import connection_kwargs

    caps = _caps()
    probes = ", ".join(
        ["currentUser()", "getSetting('readonly')"] + [f"getSetting('{n}')" for n in caps]
    )
    try:
        ch = clickhouse_connect.get_client(
            **{**connection_kwargs(database=""), "username": reader}, connect_timeout=10
        )
        try:
            name, readonly, *limits = ch.query(f"SELECT {probes}").result_rows[0]
            granted = str(ch.command(f"CHECK GRANT SELECT ON {preflight.DATABASE}.*")) == "1"
        finally:
            ch.close()
    except Exception as e:  # noqa: BLE001 — a reader that cannot log in is rebuilt
        if error_code(e) in preflight.AUTH_FAILED:
            return "missing, or its password is not LOOM_CH_PASSWORD"
        return str(e).splitlines()[0][:160]
    if name != reader or int(readonly) != 2 or [float(v) for v in limits] != list(caps.values()):
        return "its profile does not match the current caps"
    if not granted:
        return f"it lacks SELECT on {preflight.DATABASE}.*"
    return ""


def reader_owner(ch: Any, reader: str) -> str:
    """Who made `reader`: "config" (the server's users.xml, as on the bundled
    server), "ch-setup" (a user on the profile of its own name), "other", or
    "" when there is no such user or this login may not list users."""
    params = {"r": reader}
    try:
        rows = ch.query(
            "SELECT storage FROM system.users WHERE name = {r:String}", parameters=params
        )
        if not rows.result_rows:
            return ""
        if rows.result_rows[0][0] == "users_xml":
            return "config"
        ours = ch.command(
            "SELECT count() FROM system.settings_profile_elements "
            "WHERE user_name = {r:String} AND inherit_profile = {r:String}",
            parameters=params,
        )
    except Exception:  # noqa: BLE001 — a login that may not list users creates nothing either
        return ""
    return "ch-setup" if ours else "other"


def _print_admin_sql(user: str, reader: str) -> None:
    print(f"\nHave an admin run this, with the password of {user} in place of the placeholder:\n")
    for stmt in admin_sql(user, reader):
        print(f"  {stmt}" if stmt.startswith("--") else f"  {stmt};")
    print("\nThen run the installer again, or `docker compose run --rm loom loom ch-setup` in the")
    print("install directory (`loom ch-setup` from a checkout).")


def _set_up(ch: Any, user: str, reader: str) -> int:
    from server.settings import settings

    try:
        server = preflight.check(ch, need="migrate")
    except preflight.PreflightError as e:
        # The admin SQL follows, so the pointer to it is left out.
        print(f"✗ {e.reason}", file=sys.stderr)
        if e.needs_admin:
            _print_admin_sql(user, reader)
        return 1
    print(f"✓ ClickHouse {server.version}; database {preflight.DATABASE} ready for {user!r}")

    problem = reader_problem(reader)
    owner = reader_owner(ch, reader)
    if owner == "config" and problem:
        print(
            f"✗ {reader} is defined by the server's config and {problem}: raise its "
            "profile in deploy/clickhouse-config/nlp_user_settings.xml to match",
            file=sys.stderr,
        )
        return 1
    if owner == "config":
        print(f"✓ {reader} is defined by the server's config")
    elif not problem:
        print(f"✓ {reader} reads {preflight.DATABASE}.* under the current caps")
    elif owner == "other":
        print(
            f"✗ {reader} is an existing user ch-setup did not make ({problem}); "
            "set LOOM_CH_READER_USER to another name",
            file=sys.stderr,
        )
        return 1
    else:
        print(f"· {reader}: {problem}; creating it")
        try:
            for stmt in reader_sql(reader, _quote(settings.ch_password)):
                ch.command(stmt)
        except Exception as e:
            if error_code(e) != preflight.ACCESS_DENIED:
                raise
            print(f"✗ {user!r} may not create users or settings profiles", file=sys.stderr)
            _print_admin_sql(user, reader)
            return 1
        print(f"✓ {reader}: SELECT on {preflight.DATABASE}.* only, caps pinned")
    if not settings.ch_reader_user:
        print(f"  set LOOM_CH_READER_USER={reader} so caller-shaped reads run as it")

    print(
        "· Keeper: "
        + ("present" if server.keeper else "absent; leases and run ids use the fallbacks")
    )
    print(
        "· lightweight UPDATE: "
        + ("on" if server.lightweight_updates else "off; Loom uses mutations")
    )
    return 0


def _cmd_ch_setup(args: argparse.Namespace) -> int:
    from server.settings import settings

    user = settings.ch_user
    reader = settings.ch_reader_user or DEFAULT_READER
    if not (_NAME.match(user) and _NAME.match(reader)):
        print(f"✗ cannot use user names {user!r} / {reader!r} in SQL", file=sys.stderr)
        return 2
    if reader == user:
        print("✗ LOOM_CH_READER_USER must differ from LOOM_CH_USER", file=sys.stderr)
        return 2
    if args.print_sql:
        for stmt in admin_sql(user, reader):
            print(stmt if stmt.startswith("--") else f"{stmt};")
        return 0
    try:
        ch = preflight.connect()
    except preflight.PreflightError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    try:
        return _set_up(ch, user, reader)
    finally:
        ch.close()


_HELP = (
    "Prepare a self-managed ClickHouse or a ClickHouse Cloud service for Loom: create the "
    "loom database and the read-only reader user, or print the SQL for an admin."
)


def register_ch_setup_cli(sub: Any) -> None:
    p = sub.add_parser("ch-setup", help=_HELP, description=__doc__)
    p.add_argument(
        "--print-sql",
        action="store_true",
        help="Print the SQL for an admin (password as a placeholder); change nothing.",
    )
    p.set_defaults(func=_cmd_ch_setup)
