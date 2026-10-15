"""`loom ch-setup`: prepare a ClickHouse that Loom did not start itself, a
self-managed server or a ClickHouse Cloud service, for Loom's first boot.

It creates the `loom` database and the read-only reader user
(`LOOM_CH_READER_USER`, default `loom_reader`): SELECT on `loom.*` and a
settings profile that pins Loom's caller-shaped caps as ceilings, with floors
no statement can go under. Run it again at any time: what is in place is left
alone, and a reader whose profile or grants differ from that is rebuilt to
exactly it, so changes an admin made to the reader do not survive a rerun.

A user without the privilege for a step gets the SQL to hand an admin instead,
with the password left as a placeholder. The bundled ClickHouse defines the
reader in its config, so there it only reports. An existing reader is
replaced only when it has the shape ch-setup gives it (a password user on the
settings profile of its own name) and logs in with LOOM_CH_PASSWORD; anything
else of that name is left alone.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any, Literal, NamedTuple

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


class Setting(NamedTuple):
    """One setting of the reader's profile: its value as SQL, its MIN and MAX
    (None for none) and whether it is CONST."""

    value: str
    low: float | None = None
    high: float | None = None
    const: bool = False


def _profile() -> dict[str, Setting]:
    """The reader's settings profile, with the caller-shaped caps Loom applies
    as ceilings. `reader_sql` writes it and `reader_status` checks the reader
    runs under exactly this."""
    from server.code.domain.deadline import STATEMENT_FLOOR_S
    from server.core.ch import caller_shaped_ceilings

    profile = {"readonly": Setting("2")}
    for name, cap in caller_shaped_ceilings().items():
        # The floor keeps a statement from sending 0, which ClickHouse reads as no limit.
        floor = STATEMENT_FLOOR_S if name == "max_execution_time" else 1.0
        profile[name] = Setting(_number(cap), floor, cap) if cap else Setting("0")
    profile["read_overflow_mode"] = Setting("'throw'", const=True)
    return profile


def reader_sql(reader: str, password: str) -> list[str]:
    """The reader's profile, user and grant; `password` is an SQL literal."""
    settings = []
    for name, s in _profile().items():
        bounds = (
            f" MIN {_number(s.low)} MAX {_number(s.high)}"
            if s.low is not None and s.high is not None
            else ""
        )
        settings.append(f"{name} = {s.value}{bounds}{' CONST' if s.const else ''}")
    return [
        f"CREATE SETTINGS PROFILE OR REPLACE `{reader}` SETTINGS {', '.join(settings)}",
        f"CREATE USER OR REPLACE `{reader}` IDENTIFIED BY {password} SETTINGS PROFILE '{reader}'",
        f"GRANT SELECT ON {preflight.DATABASE}.* TO `{reader}`",
    ]


def admin_sql(user: str, reader: str | None) -> list[str]:
    """Everything `ch-setup` does, for an admin to run on behalf of `user`;
    no reader when `reader` is None."""
    reader_part = (
        [f"-- replaces any existing user and settings profile named {reader}"]
        + reader_sql(reader, _PASSWORD_PLACEHOLDER)
        if reader
        else []
    )
    return [
        f"CREATE DATABASE IF NOT EXISTS {preflight.DATABASE}",
        f"GRANT CURRENT GRANTS ON {preflight.DATABASE}.* TO `{user}`",
        # system.query_log and system.processes among them show every user's past
        # and running queries; docs/operations/deployment.md says how to narrow them.
        f"GRANT {', '.join(f'SELECT ON system.{t}' for t in preflight.SYSTEM_TABLES)} TO `{user}`",
        # clusterAllReplicas, which asks every replica for its version. READ only:
        # on Cloud plain REMOTE also grants writes to any host through remote().
        f"GRANT READ ON REMOTE TO `{user}`",
        *reader_part,
    ]


def _setting(value: str, low: str | None, high: str | None, readonly: int) -> Setting:
    """A reader's `system.settings` row in `_profile`'s shape."""
    try:
        shown = _number(float(value))
    except ValueError:
        shown = f"'{value}'"
    lower = None if low is None else float(low)
    upper = None if high is None else float(high)
    return Setting(shown, lower, upper, readonly == 1)


def reader_status(reader: str) -> tuple[bool | None, str]:
    """Whether `reader` logs in with LOOM_CH_PASSWORD (None when the probe
    failed for another reason, which proves nothing), and why it is not ready
    ('' when it is): its only grant is SELECT on `loom.*`, and it runs under
    exactly `_profile`, the constraints included."""
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
            grants = [str(row[0]).replace("`", "") for row in ch.query("SHOW GRANTS").result_rows]
        finally:
            ch.close()
    except Exception as e:  # noqa: BLE001 — every failure is a reason the reader is not ready
        if error_code(e) in preflight.AUTH_FAILED:
            return False, "it does not exist or does not accept LOOM_CH_PASSWORD"
        return None, str(e).splitlines()[0][:160]
    actual = {row[0]: _setting(*row[1:]) for row in rows}
    # `readonly` cannot be changed under readonly anyway, so only its value counts.
    actual["readonly"] = Setting(actual["readonly"].value)
    if name != reader:
        return True, f"its login lands as {name!r}, not {reader!r}"
    if actual != profile:
        return True, "its profile does not match the current caps and constraints"
    if grants != [f"GRANT SELECT ON {preflight.DATABASE}.* TO {reader}"]:
        return True, f"its grants are not SELECT on {preflight.DATABASE}.* alone"
    return True, ""


Found = Literal["", "config", "user", "foreign", "profile", "unknown"]


def reader_objects(ch: Any, reader: str) -> Found:
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
        # Other accounts on the profile: inheriting it, or assigned it directly
        # (TO <user>, TO <role>, TO ALL), which ch-setup never does.
        shared = ch.command(
            "SELECT count() FROM system.settings_profile_elements "
            "WHERE inherit_profile = {r:String} AND ifNull(user_name, '') != {r:String}",
            parameters=params,
        ) or ch.command(
            "SELECT count() FROM system.settings_profiles "
            "WHERE name = {r:String} AND (apply_to_all OR notEmpty(apply_to_list))",
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
    for stmt in (f"DROP USER IF EXISTS `{reader}`", f"DROP SETTINGS PROFILE IF EXISTS `{reader}`"):
        try:
            ch.command(stmt)
        except Exception:  # noqa: BLE001 — the error that led here is the one to report
            pass


def _print_admin_sql(user: str, reader: str | None) -> None:
    """The SQL for an admin, on stderr after the reason it follows, so the two
    never interleave when a container relays the streams apart."""
    err = sys.stderr
    sys.stdout.flush()
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


def _refuse(message: str) -> bool:
    sys.stdout.flush()  # the progress so far comes first, even through a pipe
    print(f"✗ {message}", file=sys.stderr)
    return False


def _ensure_reader(ch: Any, user: str, reader: str) -> bool:
    """Create or rebuild the reader where it is provably Loom's; say why not
    otherwise. Who owns it is settled before anything is replaced; a login that
    may not list users only checks it."""
    found = reader_objects(ch, reader)
    drop_hint = (
        f"If it is Loom's reader from an earlier setup (the password changed, or a setup "
        f"stopped halfway), have an admin run DROP USER IF EXISTS `{reader}`; DROP SETTINGS "
        f"PROFILE `{reader}`; and run this again. Otherwise set LOOM_CH_READER_USER to another name"
    )
    if found == "foreign":
        return _refuse(
            f"the name {reader} is taken by a user or settings profile ch-setup did not make "
            "(a user without a password or not on the profile of its own name, or a profile "
            "other accounts use); set LOOM_CH_READER_USER to another name"
        )
    if found == "profile":
        return _refuse(f"{reader} is a settings profile with no user. {drop_hint}")
    if not found:
        print(f"· {reader}: creating it")
        return _create_reader(ch, user, reader, fresh=True)

    logged_in, problem = reader_status(reader)
    if logged_in is None:
        return _refuse(f"could not check {reader}: {problem}; run this again")
    if found == "config":
        if not logged_in:
            return _refuse(
                f"{reader} is defined by the server's config with a password other than "
                "LOOM_CH_PASSWORD: make the two equal"
            )
        if problem:
            return _refuse(
                f"{reader} is defined by the server's config and {problem}: change its profile "
                "in deploy/clickhouse-config/nlp_user_settings.xml to match "
                "LOOM_CH_CALLER_SHAPED_*, with SELECT on loom.* its only grant"
            )
        print(f"✓ {reader} is defined by the server's config")
        return True
    if not problem:
        print(f"✓ {reader} reads {preflight.DATABASE}.* under the current caps")
        return True
    if found == "unknown":
        # It may be someone else's account: replace nothing that cannot be seen.
        _refuse(
            f"{reader}: {problem}; {user!r} may not list users and settings profiles, so "
            "cannot tell who owns it"
        )
        _print_admin_sql(user, reader)
        return False
    if not logged_in:
        return _refuse(f"{reader} exists but does not accept LOOM_CH_PASSWORD. {drop_hint}")
    print(f"· {reader}: {problem}; rebuilding it")
    return _create_reader(ch, user, reader, fresh=False)


def _create_reader(ch: Any, user: str, reader: str, *, fresh: bool) -> bool:
    """Run the reader's SQL; a `fresh` one that fails halfway is dropped again."""
    from server.settings import settings

    try:
        for stmt in reader_sql(reader, _quote(settings.ch_password)):
            ch.command(stmt)
    except Exception as e:
        if fresh:
            _drop_reader(ch, reader)
        if error_code(e) != preflight.ACCESS_DENIED:
            raise
        _refuse(f"{user!r} may not create the reader or grant it SELECT on {preflight.DATABASE}.*")
        _print_admin_sql(user, reader)
        return False
    print(f"✓ {reader}: SELECT on {preflight.DATABASE}.* only, caps pinned")
    return True


def _set_up(ch: Any, user: str, reader: str | None) -> int:
    from server.settings import settings

    try:
        server = preflight.check(ch, need="server", strict=True)
    except preflight.PreflightError as e:
        # The admin SQL follows, so the pointer to it is left out.
        print(f"✗ {e.reason}", file=sys.stderr)
        if e.needs_admin:
            _print_admin_sql(user, reader)
        return 1
    print(f"✓ ClickHouse {server.version}; database {preflight.DATABASE} ready for {user!r}")
    # Strict, so only the telemetry tables can be left: name the grant itself.
    for table in server.unreadable:
        print(
            f"· may not read system.{table}, so its telemetry stays empty; "
            f"an admin can run GRANT SELECT ON system.{table} TO `{user}`"
        )
    if server.no_refresh:
        print(
            f"· may not {preflight.REFRESH_GRANT} on {preflight.DATABASE}.*, so purged code "
            "stays in the refreshable views until their next refresh; an admin can run "
            f"GRANT {preflight.REFRESH_GRANT} ON {preflight.DATABASE}.* TO `{user}`"
        )

    if reader is None:
        print(f"· LOOM_CH_READER_USER is empty: caller-shaped reads run as {user!r}")
    elif not _ensure_reader(ch, user, reader):
        return 1
    elif not settings.ch_reader_user:
        print(f"  set LOOM_CH_READER_USER={reader} so caller-shaped reads run as it")

    print(
        "· Keeper: "
        + (
            "present"
            if server.keeper
            else "absent; leases, run ids, OAuth exchanges and grant changes use the "
            "fallbacks (docs/operations/deployment.md)"
        )
    )
    print(
        "· lightweight UPDATE: "
        + ("on" if server.lightweight_updates else "off; Loom uses mutations")
    )
    return 0


def _cmd_ch_setup(args: argparse.Namespace) -> int:
    from server.settings import settings

    user = settings.ch_user
    # Set but empty is the opt-out: no reader, the reads run as the Loom user.
    # Unset (a checkout without it) means the default reader.
    opted_out = "LOOM_CH_READER_USER" in os.environ and not settings.ch_reader_user
    reader = None if opted_out else settings.ch_reader_user or DEFAULT_READER
    if not (_NAME.match(user) and (reader is None or _NAME.match(reader))):
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
