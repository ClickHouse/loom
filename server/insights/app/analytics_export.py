"""`loom export-analytics`: write one UTC day of usage and a namespace snapshot
to an S3 bucket as Parquet, for a data warehouse to load.

Each entity of a day is one object at
`<prefix>/<entity>/<yyyy>/<mm>/<dd>/<entity>-<yyyy>-<mm>-<dd>.parquet`. The
upload is conditional (`If-None-Match: *`), so an object is never replaced: a
second run of the same day keeps the first run's files and reports them as
already there, and a run can be retried or repeated safely."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from server.core.ch import CHClient, query_tag
from server.insights.store.analytics_export import ENTITIES, parquet

# put(key, body) -> True once written, False when the key already exists.
Put = Callable[[str, bytes], bool]


@dataclass(frozen=True)
class Exported:
    entity: str
    key: str
    size: int
    written: bool


def object_key(prefix: str, entity: str, day: date) -> str:
    stamp = day.isoformat()
    path = f"{entity}/{day:%Y}/{day:%m}/{day:%d}/{entity}-{stamp}.parquet"
    return f"{prefix.strip('/')}/{path}" if prefix.strip("/") else path


async def export_day(
    day: date, *, prefix: str, put: Put, entities: tuple[str, ...] = tuple(ENTITIES), ch: CHClient
) -> list[Exported]:
    """Read and upload each entity of `day` in turn."""
    out: list[Exported] = []
    with query_tag("cli.export-analytics"):
        for entity in entities:
            body = await parquet(entity, day, ch=ch)
            key = object_key(prefix, entity, day)
            out.append(Exported(entity, key, len(body), put(key, body)))
    return out


def s3_client(*, role_arn: str = "", region: str = "") -> Any:
    """An S3 client on the default AWS credentials, or on `role_arn` assumed
    with them (a writer role in another account)."""
    import boto3

    session = boto3.Session(region_name=region or None)
    if role_arn:
        creds = session.client("sts").assume_role(
            RoleArn=role_arn, RoleSessionName="loom-analytics-export"
        )["Credentials"]
        session = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=region or None,
        )
    return session.client("s3")


def s3_put(s3: Any, bucket: str, *, expected_owner: str = "") -> Put:
    """A conditional upload to `bucket`: an existing key is kept, not replaced."""
    from botocore.exceptions import ClientError

    extra: dict[str, Any] = {"ExpectedBucketOwner": expected_owner} if expected_owner else {}

    def put(key: str, body: bytes) -> bool:
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*", **extra)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "PreconditionFailed":
                return False
            raise
        return True

    return put
