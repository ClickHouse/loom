"""`loom export-analytics`: one UTC day of usage and a namespace snapshot, as
Parquet files in an S3 bucket (docs/operations/analytics-export.md)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from typing import Any

from server.cli._ch import connect_declared, disconnect
from server.core.datetimes import utc_now as _now

# The first botocore whose put_object takes IfNoneMatch (the `aws` extra's floor).
BOTOCORE_FLOOR = (1, 35, 2)

# A refusal's exit code, argparse's for a bad flag too: the chart's
# podFailurePolicy fails the Job on it instead of retrying.
REFUSED = 2


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}") from None


def _refusal(day: date, today: date) -> str:
    """Why the export of `day` must not run, or ''."""
    from importlib.metadata import PackageNotFoundError, version

    from server.insights.app.analytics_export import MIN_NAME_KEY_CHARS
    from server.settings import settings

    if day >= today:
        return f"{day} has not ended yet (UTC): export a past day"
    key = settings.analytics_export_name_key
    if len(key) < MIN_NAME_KEY_CHARS:
        return (
            f"export-analytics needs LOOM_ANALYTICS_EXPORT_NAME_KEY: a random string of at "
            f"least {MIN_NAME_KEY_CHARS} characters"
        )
    # A Secret made from a file keeps its trailing newline, and every hash would
    # be keyed on it: a warehouse hashing with the bare key would join nothing.
    if key != key.strip():
        return "LOOM_ANALYTICS_EXPORT_NAME_KEY has leading or trailing whitespace: remove it"
    # A missing boto3, or a botocore without put_object's IfNoneMatch, fails at
    # the first upload, after the month's read: refuse before it instead.
    try:
        version("boto3")
        found = tuple(int(part) for part in version("botocore").split(".")[:3])
    except PackageNotFoundError:
        found = ()
    if found < BOTOCORE_FLOOR:
        floor = ".".join(map(str, BOTOCORE_FLOOR))
        return (
            f"export-analytics needs boto3 and botocore {floor} or later: the `aws` extra, "
            "which the Loom image ships (from a checkout: uv sync --extra aws)"
        )
    return ""


def _cmd_export(args: argparse.Namespace) -> int:
    from server.core.ch import get_client, statement_timeout
    from server.insights.app.analytics_export import (
        STATEMENT_TIMEOUT_S,
        Exported,
        check_credentials,
        export_day,
        name_hash_key,
        s3_client,
        s3_put,
    )
    from server.settings import settings

    today = _now().date()
    day = args.day or today - timedelta(days=1)
    if refusal := _refusal(day, today):
        print(f"  ✗ {refusal}", file=sys.stderr)
        return REFUSED
    name_key = name_hash_key(settings.analytics_export_name_key)

    def new_client() -> Any:
        return s3_client(role_arn=args.role_arn, region=args.region)

    # Missing or expired credentials, or a role that cannot be assumed, fail
    # here, not after the month's read; a missing write grant fails at the upload.
    check_credentials(role_arn=args.role_arn, region=args.region)
    put = s3_put(new_client, args.bucket, expected_owner=args.expected_bucket_owner)

    # No model calls and no writes to ClickHouse: a plain connection, outside
    # the provider barrier that `run_connected` enters. It connects inside the
    # deadline's scope, so the driver's read timeout is the deadline too.
    async def _go() -> list[Exported]:
        with statement_timeout(STATEMENT_TIMEOUT_S):
            ch = get_client()
            await connect_declared(ch, org_free="reads; writes only the export bucket")
            try:
                return await export_day(day, prefix=args.prefix, put=put, name_key=name_key, ch=ch)
            finally:
                await disconnect(ch)

    for e in asyncio.run(_go()):
        # A kept object's size is the earlier run's, which this run never read.
        state = f"wrote {e.size} bytes" if e.written else "already there, kept"
        print(f"  ✓ {e.entity}: s3://{args.bucket}/{e.key} ({state})")
    return 0


def register_analytics_export_cli(sub: Any) -> None:
    p = sub.add_parser(
        "export-analytics",
        help="Write one UTC day of usage and a namespace snapshot to S3 as Parquet, names "
        "hashed under LOOM_ANALYTICS_EXPORT_NAME_KEY. Never replaces an object, so a "
        "repeated run is safe.",
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
        help="An IAM role to assume with the default credentials, once before reading and "
        "again before each upload",
    )
    p.add_argument(
        "--region",
        default="",
        help="The bucket's AWS region (default: AWS_DEFAULT_REGION or the AWS config file)",
    )
    p.add_argument(
        "--expected-bucket-owner",
        default="",
        help="The AWS account id that must own the bucket; a write to any other is refused",
    )
    p.set_defaults(func=_cmd_export)
