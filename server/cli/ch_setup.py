"""`loom ch-setup`: prepare a ClickHouse that Loom did not start itself, a
self-managed server or a ClickHouse Cloud service, for Loom's first boot.

It creates the `loom` database and the read-only reader user
(`LOOM_CH_READER_USER`, default `loom_reader`): SELECT on `loom.*` and a
settings profile that pins Loom's caller-shaped caps as ceilings, with floors
no statement can go under. Run it again at any time: what is in place is left
alone, and a reader whose profile no longer matches the caps is rebuilt.

A user without the privilege for a step gets the SQL to hand an admin instead,
with the password left as a placeholder. The bundled ClickHouse defines the
reader in its config, so there it only reports. An existing reader is
replaced only when it has the shape ch-setup gives it (a password user on the
settings profile of its own name) and logs in with LOOM_CH_PASSWORD; anything
else of that name is left alone.
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
    """The reader profile's caps: the caller-shaped ceilings Loom applies."""
    from server.core.ch import caller_shaped_ceilings

    return caller_shaped_ceilings()


def _profile() -> dict[str, tuple[str, float | None, float | None, bool]]:
    """The reader's settings profile: each setting's value (as SQL), its MIN
    and MAX (None for none) and whether it is CONST. `reader_sql` writes it and
    `reader_status` checks the reader runs under exactly this."""
    from server.code.domain.deadline import STATEMENT_FLOOR_S

    profile: dict[str, tuple[str, float | None, float | None, bool]] = {
        "readonly": ("2", None, None, False)
    }
    for name, cap in _caps().items():
        # The floor keeps a statement from sending 0, which ClickHouse reads as no limit.
        floor = STATEMENT_FLOOR_S if name == "max_execution_time" else 1.0
        profile[name] = (_number(cap), floor, cap, False) if cap else ("0", None, None, False)
    profile["read_overflow_mode"] = ("'throw'", None, None, True)
    return profile


def reader_sql(reader: str, password: str) -> list[str]:
    """The reader's profile, user and grant; `password` is an SQL literal."""
    settings = []
    for name, (value, low, high, const) in _profile().items():
        bounds = (
            f" MIN {_number(low)} MAX {_number(high)}"
            if low is not None and high is not None
            else ""
        )
        settings.append(f"{name} = {value}{bounds}{' CONST' if const else ''}")
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


def _setting(
    value: str, low: str | None, high: str | None, readonly: int
) -> tuple[str, float | None, float | None, bool]:
    """A reader's `system.settings` row in `_profile`'s shape."""
    try:
        shown = _number(float(value))
    except ValueError:
        shown = f"'{value}'"
    lower = None if low is None else float(low)
    upper = None if high is None else float(high)
    return (shown, lower, upper, readonly == 1)


def reader_status(reader: str) -> tuple[bool, str]:
    """Whether `reader` logs in with LOOM_CH_PASSWORD, and why it is not ready
    ('' when it is): it must read `loom.*` and run under exactly `_profile`, the
    constraints included."""
    import clickhouse_connect

    from server.core.ch_target import connection_kwargs

    profile = _profile()
    try:
        ch = clickhouse_connect.get_client(
            **{**connection_kwargs(database=""), "username": reader}, connect_timeout=10
        )
        try:
            name = ch.command("SELECT currentUser()")
            rows = ch.query(
                "SELECT name, value, min, max, readonly FROM system.settings "
                "WHERE name IN {names:Array(String)}",
                parameters={"names": list(profile)},
            ).result_rows
            granted = str(ch.command(f"CHECK GRANT SELECT ON {preflight.DATABASE}.*")) == "1"
        finally:
            ch.close()
    except Exception as e:  # noqa: BLE001 — every failure is a reason the reader is not ready
        if error_code(e) in preflight.AUTH_FAILED:
            return False, "it does not accept LOOM_CH_PASSWORD"
        return False, str(e).splitlines()[0][:160]
    actual = {row[0]: _setting(*row[1:]) for row in rows}
    # `readonly` cannot be changed under readonly anyway, so only its value counts.
    actual["readonly"] = (actual["readonly"][0], None, None, False)
    if name != reader or actual != profile:
        return True, "its profile does not match the current caps and constraints"
    if not granted:
        return True, f"it lacks SELECT on {preflight.DATABASE}.*"
    return True, ""


def reader_objects(ch: Any, reader: str) -> str:
    """What already carries the reader's name: "" (nothing), "config" (the
    server's users.xml, as on the bundled server), "user" (an SQL user in the
    shape ch-setup gives it: a password and the settings profile of its own
    name), "foreign" (any other user, or a profile other accounts use),
    "profile" (a profile with no user: left over, or someone else's), or
    "unknown" when this login may not list them."""
    params = {"r": reader}
    try:
        users = ch.query(
            "SELECT storage, hasAny(auth_type, ['no_password', 'no_authentication']) "
            "FROM system.users WHERE name = {r:String}",
            parameters=params,
        ).result_rows
        own_profile = ch.command(
            "SELECT count() FROM system.settings_profile_elements "
            "WHERE user_name = {r:String} AND inherit_profile = {r:String}",
            parameters=params,
        )
        profiles = ch.command(
            "SELECT count() FROM system.settings_profiles WHERE name = {r:String}",
            parameters=params,
        )
        shared = ch.command(
            "SELECT count() FROM system.settings_profile_elements "
            "WHERE inherit_profile = {r:String} AND ifNull(user_name, '') != {r:String}",
            parameters=params,
        )
    except Exception:  # noqa: BLE001 — a login that may not list them cannot tell
        return "unknown"
    if users and users[0][0] == "users_xml":
        return "config"
    if users:
        _, open_login = users[0]
        return "user" if own_profile and not open_login and not shared else "foreign"
    if shared:
        return "foreign"
    return "profile" if profiles else ""


def _drop_reader(ch: Any, reader: str) -> None:
    """Undo a reader made halfway, so a rerun starts from nothing."""
    try:
        ch.command(f"DROP USER IF EXISTS `{reader}`")
        ch.command(f"DROP SETTINGS PROFILE IF EXISTS `{reader}`")
    except Exception:  # noqa: BLE001 — the error that led here is the one to report
        pass


def _print_admin_sql(user: str, reader: str) -> None:
    """The SQL for an admin, on stderr after the reason it follows, so the two
    never interleave when a container relays the streams apart."""
    err = sys.stderr
    print(
        f"\nHave an admin run this, with the password of {user} in place of the placeholder:\n",
        file=err,
    )
    for stmt in admin_sql(user, reader):
        print(f"  {stmt}" if stmt.startswith("--") else f"  {stmt};", file=err)
    print(
        "\nThen run the installer again, or `docker compose run --rm loom loom ch-setup` in the",
        file=err,
    )
    print("install directory (`loom ch-setup` from a checkout).", file=err)


def _set_up(ch: Any, user: str, reader: str) -> int:
    from server.settings import settings

    try:
        server = preflight.check(ch, need="migrate", strict=True)
    except preflight.PreflightError as e:
        # The admin SQL follows, so the pointer to it is left out.
        print(f"✗ {e.reason}", file=sys.stderr)
        if e.needs_admin:
            _print_admin_sql(user, reader)
        return 1
    print(f"✓ ClickHouse {server.version}; database {preflight.DATABASE} ready for {user!r}")

    logged_in, problem = reader_status(reader)
    found = reader_objects(ch, reader)
    if found == "config" and problem:
        print(
            f"✗ {reader} is defined by the server's config and {problem}: raise its "
            "profile in deploy/clickhouse-config/nlp_user_settings.xml to match",
            file=sys.stderr,
        )
        return 1
    if found == "config":
        print(f"✓ {reader} is defined by the server's config")
    elif not problem:
        print(f"✓ {reader} reads {preflight.DATABASE}.* under the current caps")
    elif found == "unknown":
        # It may be someone else's account: replace nothing that cannot be seen.
        print(
            f"✗ {user!r} may not list users and settings profiles, so cannot tell who "
            f"owns {reader}",
            file=sys.stderr,
        )
        _print_admin_sql(user, reader)
        return 1
    elif found == "foreign":
        print(
            f"✗ {reader} exists and is not one ch-setup made (no password, not on the profile "
            "of its own name, or its profile is shared); set LOOM_CH_READER_USER to another name",
            file=sys.stderr,
        )
        return 1
    elif found == "profile" or (found == "user" and not logged_in):
        why = "is a settings profile with no user" if found == "profile" else f"exists: {problem}"
        print(
            f"✗ {reader} {why}. If it is Loom's reader from an earlier setup (the password "
            f"changed, or a setup stopped halfway), have an admin run DROP USER IF EXISTS "
            f"{reader}; DROP SETTINGS PROFILE {reader}; and run this again. Otherwise set "
            "LOOM_CH_READER_USER to another name",
            file=sys.stderr,
        )
        return 1
    else:
        print(f"· {reader}: {problem}; rebuilding it" if found else f"· {reader}: creating it")
        try:
            for stmt in reader_sql(reader, _quote(settings.ch_password)):
                ch.command(stmt)
        except Exception as e:
            if not found:
                _drop_reader(ch, reader)
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
