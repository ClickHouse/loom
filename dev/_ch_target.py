"""The ClickHouse target of the operator tools in dev/ (backup, restore, audit verify).

Every connection value resolves the way the server resolves it: a CLI flag,
else the `LOOM_CH_*` value `server.settings` reads from the environment or
`.env`. The host has no fallback: when neither a flag nor `LOOM_CH_HOST` names
it, the tool refuses rather than guessing localhost, so a tool run from the
wrong shell cannot silently act on whichever server answers there. The
password is read only from `LOOM_CH_PASSWORD` (never argv, where `ps` shows it)
and never printed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any


class TargetError(ValueError):
    """The target is not named explicitly; the message says how to name it."""


@dataclass(frozen=True)
class CHTarget:
    host: str
    port: int
    user: str
    database: str
    secure: bool
    password: str = field(default="", repr=False)

    def describe(self) -> str:
        """`user@host:port/database (tls)` — safe to print; no password."""
        tls = "tls" if self.secure else "plain"
        return f"{self.user}@{self.host}:{self.port}/{self.database} ({tls})"

    def client(self, *, use_database: bool = True, **kwargs: Any) -> Any:
        """A connected clickhouse_connect client; raises TargetError when the
        target cannot be reached or refuses the login or the database.
        `use_database=False` connects without selecting the database, for a
        caller that qualifies every name and may create the database."""
        import clickhouse_connect

        try:
            return clickhouse_connect.get_client(
                host=self.host,
                port=self.port,
                username=self.user,
                password=self.password,
                database=self.database if use_database else None,
                secure=self.secure,
                **kwargs,
            )
        except Exception as e:
            raise TargetError(f"cannot connect to {self.describe()}: {e}") from e


def add_target_args(parser: argparse.ArgumentParser) -> None:
    g = parser.add_argument_group(
        "ClickHouse target",
        "Each flag overrides the LOOM_CH_* value from the environment or .env. "
        "The host must be named by --host or LOOM_CH_HOST. The password is "
        "read from LOOM_CH_PASSWORD only.",
    )
    g.add_argument("--host", default=None, help="ClickHouse host (LOOM_CH_HOST)")
    g.add_argument("--port", type=int, default=None, help="HTTP(S) port (LOOM_CH_PORT)")
    g.add_argument("--user", default=None, help="User (LOOM_CH_USER)")
    g.add_argument("--database", default=None, help="Database (LOOM_CH_DATABASE)")
    g.add_argument(
        "--secure",
        action="store_true",
        default=None,
        help="Use HTTPS (LOOM_CH_SECURE; on by default for port 8443)",
    )


def resolve_target(args: argparse.Namespace, settings: Any = None) -> CHTarget:
    """The exact target from flags over `settings`; raises TargetError when
    the host is named nowhere."""
    if settings is None:
        from server.settings import settings as loaded

        settings = loaded
    if args.host is None and "ch_host" not in settings.model_fields_set:
        raise TargetError(
            "no ClickHouse target: pass --host or set LOOM_CH_HOST (environment or .env)"
        )
    port = args.port if args.port is not None else settings.ch_port
    secure = bool(args.secure) or bool(settings.ch_secure) or port == 8443
    return CHTarget(
        host=args.host if args.host is not None else settings.ch_host,
        port=port,
        user=args.user if args.user is not None else settings.ch_user,
        database=args.database if args.database is not None else settings.ch_database,
        secure=secure,
        password=settings.ch_password,
    )
