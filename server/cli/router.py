"""`loom router serve` and `loom router migrate`: the router in front of a deployment's
cells, and its directory's schema (docs/architecture/cells.md, "The router").

The router reads and writes only its directory (`LOOM_ROUTER_PG_DSN`), never a cell's
stores, so neither command connects to ClickHouse or to a memory authority.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any


def _refusal() -> str:
    """Why the router may not run against this configuration ('' when it may)."""
    from server.settings import settings

    if not settings.router_pg_dsn:
        return "set LOOM_ROUTER_PG_DSN to the directory's Postgres"
    if settings.router_pg_dsn == settings.pg_dsn:
        # `loom router migrate` also refuses a database holding a memory authority.
        return "LOOM_ROUTER_PG_DSN is LOOM_PG_DSN: give the directory a database of its own"
    from server.router.routing import usable_upstream

    upstream = settings.router_upstream_url
    if upstream and not usable_upstream(upstream):
        return "LOOM_ROUTER_UPSTREAM_URL is no http(s)://host[:port] with no path"
    return ""


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from server.router.app import build_app
    from server.settings import settings

    if refusal := _refusal():
        print(f"loom router serve: {refusal}", file=sys.stderr)
        return 2
    os.environ.setdefault("LOOM_INSTANCE_LABEL", f"cli:router:{args.port}")
    uvicorn.run(
        build_app(),
        host=args.host,
        port=args.port,
        loop="asyncio",
        # A lookup is limited per client address: the one the trusted proxies report.
        proxy_headers=True,
        forwarded_allow_ips=settings.trusted_proxies,
        log_level=args.log_level,
    )
    return 0


def _cmd_migrate(_args: argparse.Namespace) -> int:
    from server.core.schema import pg_migrate
    from server.router import directory

    if refusal := _refusal():
        print(f"loom router migrate: {refusal}", file=sys.stderr)
        return 2
    try:
        applied = asyncio.run(directory.migrate())
    except (directory.DirectoryUnavailable, pg_migrate.MigrationDrift) as e:
        print(f"loom router migrate: {e}", file=sys.stderr)
        return 2
    print("applied: " + (", ".join(applied) if applied else "none (the directory is current)"))
    return 0


def _add_router_parser(sub: Any) -> None:
    p = sub.add_parser("router", help="The router in front of a deployment's cells.")
    rsub = p.add_subparsers(dest="router_cmd", required=True)
    serve = rsub.add_parser(
        "serve",
        help="Serve the placement API and org lookups, and route every other request to a cell.",
    )
    serve.add_argument("--host", default="127.0.0.1", help="Address to listen on")
    serve.add_argument("--port", type=int, default=7780, help="Port to listen on")
    serve.add_argument("--log-level", default="info", help="uvicorn's log level")
    serve.set_defaults(func=_cmd_serve)
    migrate = rsub.add_parser("migrate", help="Apply the directory's pending migrations.")
    migrate.set_defaults(func=_cmd_migrate)
