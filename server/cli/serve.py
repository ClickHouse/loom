"""`loom serve` (boot the control plane under uvicorn) and `loom migrate`
(apply schema migrations through `server.core.schema.migrate`).
"""

from __future__ import annotations

import argparse
import os
from typing import Any


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from server.settings import settings

    # Every process outside the compose deployment carries a
    # label; `loom serve` names itself when the operator did not.
    os.environ.setdefault("LOOM_INSTANCE_LABEL", f"cli:serve:{args.port}")
    # The startup checks read the address actually bound, --host included
    # (the env var reaches a --reload child).
    host = args.host or settings.bind_host
    os.environ["LOOM_BIND_HOST"] = host
    settings.bind_host = host
    uvicorn.run(
        "server.main:app",
        host=host,
        port=args.port,
        # the stdlib loop: uvloop forks the whole process for each git spawn
        loop=os.environ.get("LOOM_UVICORN_LOOP") or "asyncio",
        proxy_headers=True,
        forwarded_allow_ips=settings.trusted_proxies,
        log_level=args.log_level,
        reload=args.reload,
    )
    return 0


# `loom migrate` mirrors server/core/schema/migrate.py's flags explicitly:
# an argparse.REMAINDER positional after a subparser rejects a leading
# `--status`, so the help advertised flags the command could not take.
_MIGRATE_STORE_TRUE: tuple[tuple[str, str], ...] = (
    ("status", "Show applied/pending; do not change anything"),
    ("dry-run", "List what would apply; do not change anything"),
    ("strict", "With --dry-run/--status: exit 1 on a ledger-only file or checksum drift"),
)


def _add_migrate_flags(p_migrate: argparse.ArgumentParser) -> None:
    for flag, help_text in _MIGRATE_STORE_TRUE:
        p_migrate.add_argument(f"--{flag}", action="store_true", help=help_text)
    p_migrate.add_argument(
        "--target", type=int, default=None, help="Apply migrations through NNN only"
    )
    p_migrate.add_argument(
        "--migrations-dir", default=None, help="Directory containing NNN_*.sql files"
    )


def _migrate_argv(args: argparse.Namespace) -> list[str]:
    """The argv `loom migrate` hands `server.core.schema.migrate.main` — only what the
    user passed, so the runner's own defaults stay authoritative."""
    argv = [f"--{flag}" for flag, _ in _MIGRATE_STORE_TRUE if getattr(args, flag.replace("-", "_"))]
    if args.target is not None:
        argv += ["--target", str(args.target)]
    if args.migrations_dir:
        argv += ["--migrations-dir", args.migrations_dir]
    return argv


def _cmd_migrate(args: argparse.Namespace) -> int:
    from server.core.schema.migrate import main as migrate_main

    return migrate_main(_migrate_argv(args))


def _add_serve_and_migrate_parsers(sub: Any) -> None:
    """The `serve` and `migrate` subcommands."""
    p_serve = sub.add_parser("serve", help="Boot the control plane (uvicorn).")
    p_serve.add_argument(
        "--host", default=None, help="Address to bind (default: LOOM_BIND_HOST, 127.0.0.1)"
    )
    p_serve.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("LOOM_BIND_PORT", "7777")),
    )
    p_serve.add_argument("--log-level", default="info")
    p_serve.add_argument(
        "--reload",
        action="store_true",
        help="Auto-reload on code changes (dev only).",
    )
    p_serve.set_defaults(func=_cmd_serve)

    p_migrate = sub.add_parser(
        "migrate",
        help="Apply schema migrations against the configured CH.",
    )
    _add_migrate_flags(p_migrate)
    p_migrate.set_defaults(func=_cmd_migrate)
