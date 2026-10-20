"""`loom export-analytics`: one UTC day of usage and a namespace snapshot, as
Parquet files in an S3 bucket (docs/operations/analytics-export.md)."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta
from typing import Any

from server.cli._ch import run_connected


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}") from None


def _cmd_export(args: argparse.Namespace) -> int:
    from server.insights.app.analytics_export import export_day, s3_client, s3_put

    day = args.day or datetime.now(UTC).date() - timedelta(days=1)
    if day >= datetime.now(UTC).date():
        print(f"  ✗ {day} has not ended yet (UTC): export a past day", file=sys.stderr)
        return 2
    try:
        import boto3  # noqa: F401
    except ImportError:
        print(
            "  ✗ export-analytics needs boto3: pip install 'clickhouse-loom-server[aws]'",
            file=sys.stderr,
        )
        return 2
    s3 = s3_client(role_arn=args.role_arn, region=args.region)
    put = s3_put(s3, args.bucket, expected_owner=args.expected_bucket_owner)

    async def _go(ch: Any) -> list[Any]:
        return await export_day(day, prefix=args.prefix, put=put, ch=ch)

    for e in run_connected(_go):
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
