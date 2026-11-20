"""The SQL surface as data: the views a SQL user of the surface reads, the
scope rows `loom sql-access` writes, and the ClickHouse statements that make
the user. The views and `loom.sql_access` are made by migration 090
(docs/reference/sql-surface.md); each view admits only the rows
`loom.sql_access` grants to `currentUser()`, so a user with SELECT on the
views and no scope row reads nothing.

Pure: no I/O. `server.insights.store.sql_access` runs the statements.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from server.core.sql_literal import sql_string

VIEWS: tuple[str, ...] = (
    "sql_v1_memories",
    "sql_v1_memory_history",
    "sql_v1_conversations",
    "sql_v1_agent_events",
    "sql_v1_usage_daily",
)
# Every SQL user of the surface carries this profile, so `loom sql-access`
# tells its own users from any other ClickHouse user of the same name.
PROFILE = "loom_sql_surface"
ALL = "*"
# Names go into the SQL between backticks.
_USER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


@dataclass(frozen=True)
class Scope:
    """What a SQL user reads: one org's namespaces (ALL for every one of
    them), or with org ALL the whole deployment."""

    org: str
    namespaces: tuple[str, ...]

    def rows(self) -> set[tuple[str, str]]:
        if self.org == ALL:
            return {(ALL, ALL)}
        return {(self.org, ns) for ns in self.namespaces}


def user_name_problem(name: str, reserved: set[str]) -> str:
    """Why `name` cannot be a SQL user of the surface, or ''."""
    if not _USER_NAME.match(name):
        return "a user name is a letter followed by up to 63 letters, digits or underscores"
    if name in reserved:
        return f"{name} is one of Loom's own ClickHouse users"
    return ""


def scope_problem(scope: Scope) -> str:
    """Why `scope` names nothing a SQL user can read, or ''."""
    if scope.org == ALL:
        return "" if not scope.namespaces else "the whole deployment takes no namespaces"
    if not scope.org:
        return "name the org"
    if not scope.namespaces:
        return "name the namespaces, or * for every namespace of the org"
    if ALL in scope.namespaces and len(scope.namespaces) > 1:
        return "* already names every namespace of the org"
    if not all(scope.namespaces):
        return "a namespace name is empty"
    return ""


# Every column of loom.sql_access; a row is written whole.
ACCESS_COLUMNS = ("user_name", "org", "namespace", "granted", "updated_at")


def restricted_reach(
    scope: Scope, by_config: set[tuple[str, str]], by_directory: set[tuple[str, str]]
) -> list[tuple[str, str]]:
    """The restricted namespaces `scope` would reach that the views cannot
    leave out themselves: any it names literally, and, through a wildcard, the
    ones only the code repo config classifies (the views read the directory's
    classification at query time)."""
    literal = {(scope.org, ns) for ns in scope.namespaces if ns != ALL}
    reached = {(org, ns) for org, ns in by_config if scope.org in (ALL, org)}
    if scope.org != ALL and ALL not in scope.namespaces:
        reached &= literal
    return sorted(reached | (literal & by_directory))


def access_rows(
    user: str, scope: Scope, held: set[tuple[str, str]], now: datetime
) -> list[dict[str, object]]:
    """The `loom.sql_access` rows that give `user` exactly `scope`: each pair
    of it granted, each pair it held before and does not keep revoked."""
    keep = scope.rows()
    return [_row(user, org, ns, (org, ns) in keep, now) for org, ns in sorted(keep | held)]


def next_version(now: datetime, last: datetime | None) -> datetime:
    """The version a user's next rows carry: past every row it holds, so a
    revocation from a host whose clock runs behind the grant's still wins
    (`loom.sql_access` keeps the row with the highest `updated_at`)."""
    if last is None:
        return now
    last = last if last.tzinfo else last.replace(tzinfo=UTC)
    return max(now, last + timedelta(milliseconds=1))


def revoke_rows(user: str, held: set[tuple[str, str]], now: datetime) -> list[dict[str, object]]:
    return [_row(user, org, ns, False, now) for org, ns in sorted(held)]


def _row(user: str, org: str, ns: str, granted: bool, now: datetime) -> dict[str, object]:
    return dict(zip(ACCESS_COLUMNS, (user, org, ns, granted, now), strict=True))


def draw_password() -> str:
    """192 random bits behind a fixed prefix that holds a lower and an upper
    case letter, a digit and a symbol, as ClickHouse Cloud requires."""
    return "Aa1-" + secrets.token_hex(24)


# A SQL user's statements change no setting (`readonly = 1`) but these, which
# leave every view's rows as they are and which clients send on their own
# (pg_clickhouse's defaults among them). Any other setting could turn a view's
# filter into a partial one: an overflow mode that cuts the restricted set
# short, `additional_table_filters` under the history's window, PREWHERE under
# FINAL.
CHANGEABLE = ("final", "join_use_nulls", "group_by_use_nulls", "transform_null_in")
# Every overflow mode: a `break` cuts a read, a set or a group short and
# answers with what it has, which can drop a restricted namespace from a
# view's NOT IN set or rows from a total.
OVERFLOW_MODES = (
    "read_overflow_mode",
    "read_overflow_mode_leaf",
    "timeout_overflow_mode",
    "timeout_overflow_mode_leaf",
    "set_overflow_mode",
    "transfer_overflow_mode",
    "join_overflow_mode",
    "sort_overflow_mode",
    "distinct_overflow_mode",
    "group_by_overflow_mode",
    "result_overflow_mode",
)
# Values the profile holds whatever the server's defaults are; why each one
# matters is in docs/reference/sql-surface.md#access. Each is CONST: a server
# profile that pins one (the bundled one pins read_overflow_mode) then agrees
# with it at login.
PINNED = (
    "readonly = 1 CONST",
    "apply_deleted_mask = 1 CONST",
    "apply_patch_parts = 1 CONST",
    "use_query_cache = 0 CONST",
    "use_query_condition_cache = 0 CONST",
    "use_skip_indexes_if_final_exact_mode = 1 CONST",
    "do_not_merge_across_partitions_select_final = 0 CONST",
    "optimize_move_to_prewhere_if_final = 0 CONST",
    # A caller's filter runs on what a view returns, never below its scope:
    # ClickHouse plans a definer view sealed, and these keep it so.
    "query_plan_filter_push_down = 0 CONST",
    "enable_optimize_predicate_expression = 0 CONST",
    "allow_introspection_functions = 0 CONST",
    "send_logs_level = 'none' CONST",
    *(f"{name} = 'throw' CONST" for name in OVERFLOW_MODES),
)


@dataclass(frozen=True)
class Caps:
    """The ceilings a statement of a SQL user runs under: `insights.query`'s,
    except its result caps, since a SQL client reads whole result sets."""

    max_execution_s: float
    max_rows_to_read: int
    max_memory_bytes: int
    max_threads: int
    max_concurrent: int


def profile_sql(caps: Caps) -> list[str]:
    """The profile every SQL user of the surface carries."""
    settings = ", ".join(
        [
            *PINNED,
            f"max_execution_time = {caps.max_execution_s:g}",
            f"max_rows_to_read = {caps.max_rows_to_read}",
            f"max_memory_usage = {caps.max_memory_bytes}",
            f"max_threads = {caps.max_threads}",
            f"max_concurrent_queries_for_user = {caps.max_concurrent}",
            *(f"{name} CHANGEABLE_IN_READONLY" for name in CHANGEABLE),
        ]
    )
    return [
        f"CREATE SETTINGS PROFILE IF NOT EXISTS `{PROFILE}` SETTINGS {settings}",
        f"ALTER SETTINGS PROFILE `{PROFILE}` SETTINGS {settings}",
    ]


def create_user_sql(user: str, password: str) -> str:
    """A new SQL user with no grant yet."""
    return (
        f"CREATE USER `{user}` IDENTIFIED WITH sha256_password BY {sql_string(password)} "
        f"SETTINGS PROFILE '{PROFILE}'"
    )


def grant_sql(db: str, user: str) -> list[str]:
    """SELECT on the views and nothing else; `db` is already quoted."""
    return [f"GRANT SELECT ON {db}.`{view}` TO `{user}`" for view in VIEWS]


def password_sql(user: str, password: str) -> str:
    return f"ALTER USER `{user}` IDENTIFIED WITH sha256_password BY {sql_string(password)}"
