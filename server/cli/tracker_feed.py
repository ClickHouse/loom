"""Operator-scheduled import into a team's dedicated tracker database."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from server.core.github_values import FeedInputError


def _environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise FeedInputError(f"required credential environment variable is empty: {name}")
    return value


async def _registered(args: argparse.Namespace) -> tuple[Any, set[str], str]:
    from server.settings import settings
    from server.tracker.app import registry_sources
    from server.tracker.domain import sources

    if not settings.tracker_org or args.org != settings.tracker_org:
        raise FeedInputError("feed organization must match the configured tracker organization")
    # This is a new CLI process: an unsuccessful refresh cannot inherit a
    # previously cached registered source.
    await registry_sources.refresh_db_sources(force=True)
    endpoint = sources.endpoint(args.source)
    if endpoint is None or endpoint.mapping_json:
        raise FeedInputError("feed requires a registered source exposing the contract views")
    reader, _password = sources.credentials_for(args.source)
    if not reader or args.writer_user == reader:
        raise FeedInputError("feed writer and registered reader must be separate credentials")
    repos = {
        repo
        for entry in sources.namespace_entries().values()
        if entry.source == args.source
        for repo in entry.repos
    }
    if not repos:
        raise FeedInputError("registered source has no repository allowlist")
    return endpoint, repos, reader


async def _connection(args: argparse.Namespace, endpoint: Any, deployment: Any) -> Any:
    from server.cli._ch import connect_external
    from server.core.ch import ExternalCHClient
    from server.core.hosts import vet_host
    from server.settings import settings

    why, address = await vet_host(
        endpoint.host,
        allow_private=bool(settings.tracker_self_service_allow_private_hosts),
    )
    if why:
        raise FeedInputError("record database host refused")
    if not endpoint.database or endpoint.database in {
        "system",
        "information_schema",
        "INFORMATION_SCHEMA",
    }:
        raise FeedInputError("a dedicated record database is required")
    if not endpoint.secure and not settings.tracker_self_service_allow_private_hosts:
        raise FeedInputError("HTTP requires the development private-host setting")
    client = ExternalCHClient(
        host=endpoint.host,
        address=address,
        port=endpoint.port,
        secure=endpoint.secure,
        database=endpoint.database,
        username=args.writer_user,
        password=_environment(args.writer_password_env),
    )
    try:
        await connect_external(client, deployment=deployment, orgs=(args.org,))
        await _validate_writer(client, endpoint.database)
    except BaseException:
        await client.disconnect()
        raise
    return client


async def _validate_writer(client: Any, database: str) -> None:
    from server.tracker.domain.github_feed import writer_grants
    from server.tracker.store import github_feed as store

    for statement in ("SHOW GRANTS", "SHOW GRANTS FINAL"):
        rows = await client.query_all(statement)
        writer_grants([str(next(iter(row.values()))) for row in rows], database)
    if any(
        row["engine"] not in {"ReplacingMergeTree", "SharedReplacingMergeTree"}
        for row in await store.table_engines(client)
    ):
        raise FeedInputError("unsupported feed record table engine; use a dedicated feed record")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    from types import SimpleNamespace

    from server.cli._ch import connect_checked, disconnect
    from server.core.ch import get_client, set_query_tag
    from server.settings import settings
    from server.tracker.app import github_feed as feed
    from server.tracker.store import github_feed as store

    if not settings.tracker_org or args.org != settings.tracker_org:
        raise FeedInputError("feed organization must match the configured tracker organization")
    set_query_tag(f"cli.tracker-feed.{args.feed_command}")
    deployment = get_client()
    record = None
    try:
        await connect_checked(deployment, orgs=(args.org,))
        owner = await store.database_uuid(deployment)
        allowed: set[str] = set()
        if args.feed_command == "init":
            endpoint = SimpleNamespace(
                host=args.host,
                port=args.port,
                secure=not args.insecure,
                database=args.database,
            )
        else:
            endpoint, allowed, _reader = await _registered(args)
        record = await _connection(args, endpoint, deployment)
        if args.feed_command == "init":
            await feed.initialize(record, args.org, owner)
            return {"initialized": True}
        if args.feed_command == "configure":
            if not args.read_only_token:
                raise FeedInputError("token permission attestation requires --read-only-token")
            await feed.configure(
                record,
                org=args.org,
                owner_uuid=owner,
                repo=args.repo,
                token=_environment(args.token_env),
                allowed_repos=allowed,
                max_pages=args.max_pages,
            )
            return {"configured": True}
        return await feed.sync(
            record,
            org=args.org,
            owner_uuid=owner,
            repo=args.repo,
            allowed_repos=allowed,
        )
    finally:
        try:
            if record is not None:
                await record.disconnect()
        finally:
            await disconnect(deployment)


def _command(args: argparse.Namespace) -> int:
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001 — operator output must not quote provider or DB content
        from server.tracker.domain.github_feed import FeedCapacityError, FeedInputError

        detail = (
            str(exc) if isinstance(exc, (FeedInputError, FeedCapacityError)) else type(exc).__name__
        )
        print(f"tracker feed refused: {detail}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def register_tracker_feed(sub: Any) -> None:
    parser = sub.add_parser("tracker-feed", help="Maintain a team's GitHub tracker record")
    commands = parser.add_subparsers(dest="feed_command", required=True)
    for name in ("init", "configure", "sync"):
        command = commands.add_parser(name)
        command.add_argument("--org", required=True)
        command.add_argument("--writer-user", required=True)
        command.add_argument("--writer-password-env", default="LOOM_TRACKER_FEED_WRITER_PASSWORD")
        command.set_defaults(func=_command)
        if name == "init":
            command.add_argument("--host", required=True)
            command.add_argument("--port", type=int, default=8443)
            command.add_argument("--database", required=True)
            command.add_argument(
                "--insecure", action="store_true", help="Use HTTP for a private development record"
            )
        else:
            command.add_argument("--source", required=True)
            command.add_argument(
                "--repo", required=True, help="A repository in the source's allowlist"
            )
        if name == "configure":
            command.add_argument("--token-env", default="LOOM_TRACKER_FEED_GITHUB_TOKEN")
            command.add_argument(
                "--read-only-token",
                action="store_true",
                help="Attest the token has only read permissions",
            )
            command.add_argument(
                "--max-pages", type=int, default=5, help="Pages per stream, from 2 to 100"
            )
