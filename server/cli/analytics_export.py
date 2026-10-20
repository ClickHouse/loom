"""`loom export-analytics`: one UTC day of usage and a namespace snapshot, as
Parquet files in an S3 bucket (docs/operations/analytics-export.md)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, date, datetime, timedelta
from typing import Any


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}") from None


def _refusal(args: argparse.Namespace, today: date) -> str:
    """Why the export must not run, or ''."""
    from server.settings import settings

    if args.day >= today:
        return f"{args.day} has not ended yet (UTC): export a past day"
    if args.names == "hashed" and not settings.analytics_export_name_key:
        return "--names hashed needs LOOM_ANALYTICS_EXPORT_NAME_KEY"
    if args.names == "raw" and settings.self_signup == "open":
        return (
            "--names raw is refused while LOOM_SELF_SIGNUP=open: self-signup names carry usernames"
        )
    try:
        import boto3  # noqa: F401
    except ImportError:
        return "export-analytics needs boto3: pip install 'clickhouse-loom-server[aws]'"
    return ""


def _cmd_export(args: argparse.Namespace) -> int:
    from server.core.ch import get_client
    from server.insights.app.analytics_export import export_day, name_hash_key, s3_client, s3_put
    from server.settings import settings

    today = datetime.now(UTC).date()
    args.day = args.day or today - timedelta(days=1)
    if refusal := _refusal(args, today):
        print(f"  ✗ {refusal}", file=sys.stderr)
        return 2
    key = name_hash_key(settings.analytics_export_name_key) if args.names == "hashed" else None
    s3 = s3_client(role_arn=args.role_arn, region=args.region)
    put = s3_put(s3, args.bucket, expected_owner=args.expected_bucket_owner)

    # No model calls and no writes to ClickHouse: a plain connection, outside
    # the provider barrier that `run_connected` enters.
    async def _go() -> list[Any]:
        ch = get_client()
        await ch.connect()
        try:
            return await export_day(args.day, prefix=args.prefix, put=put, key=key, ch=ch)
        finally:
            await ch.disconnect()

    for e in asyncio.run(_go()):
        state = "wrote" if e.written else "already there, kept"
        print(f"  ✓ {e.entity}: s3://{args.bucket}/{e.key} ({e.size} bytes, {state})")
    return 0


def register_analytics_export_cli(sub: Any) -> None:
    p = sub.add_parser(
        "export-analytics",
        help="Write one UTC day of usage and a namespace snapshot to S3 as Parquet. "
        "Never replaces an object, so a repeated run is safe.",
    )
    p.add_argument("--bucket", required=True, help="The S3 bucket to write to")
    p.add_argument("--prefix", default="", help="A key prefix inside the bucket")
    p.add_argument(
        "--day",
        type=_day,
        default=None,
        help="The UTC day to export, YYYY-MM-DD (default: yesterday)",
    )
    p.add_argument(
        "--names",
        choices=("hashed", "raw"),
        default="hashed",
        help="hashed (default): org, namespace and agent as a keyed hash under "
        "LOOM_ANALYTICS_EXPORT_NAME_KEY; raw: as stored, for a deployment whose "
        "names are opaque ids (refused while self-signup is open)",
    )
    p.add_argument(
        "--role-arn",
        default="",
        help="An IAM role to assume with the default credentials before writing",
    )
    p.add_argument(
        "--region", default="", help="The bucket's AWS region (default: from AWS_REGION)"
    )
    p.add_argument(
        "--expected-bucket-owner",
        default="",
        help="The AWS account id that must own the bucket; a write to any other is refused",
    )
    p.set_defaults(func=_cmd_export)
