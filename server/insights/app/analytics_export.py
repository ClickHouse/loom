"""`loom export-analytics`: write one UTC day of usage and a namespace snapshot
to an S3 bucket as Parquet, for a data warehouse to load.

Each entity of a day is one object at
`<prefix>/<entity>/<yyyy>/<mm>/<dd>/<entity>-<yyyy>-<mm>-<dd>.parquet`. The
upload is conditional (`If-None-Match: *`), so an object is never replaced: a
second run of the same day keeps the first run's files and reports them as
already there, and a run can be retried or repeated safely."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from server.core.ch import CHClient
from server.insights.store.analytics_export import ENTITIES, parquet

# put(key, body) -> True once written, False when the key already exists.
Put = Callable[[str, bytes], bool]

# Each statement reads a month partition of the ledger or every live memory
# row: far past the request read deadline on a large deployment, and inside
# the CronJob's one-hour deadline. The client must be built inside the
# `statement_timeout` scope for its driver read timeout to cover it.
STATEMENT_TIMEOUT_S = 1200

# A shorter key can be searched offline from one known name and its hash.
MIN_NAME_KEY_CHARS = 32


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


def name_hash_key(secret: str) -> tuple[int, int]:
    """The two 64-bit SipHash key words for `secret`: the first 16 bytes of
    SHA-256(secret as UTF-8), each half read little-endian. As bytes, that is
    the 16-byte key of a reference SipHash-2-4-128."""
    digest = hashlib.sha256(secret.encode()).digest()
    return int.from_bytes(digest[:8], "little"), int.from_bytes(digest[8:16], "little")


async def export_day(
    day: date, *, prefix: str, put: Put, key: tuple[int, int] | None, ch: CHClient
) -> list[Exported]:
    """Read and upload each entity of `day` in turn, names hashed under `key`
    (None: as stored)."""
    out: list[Exported] = []
    for entity in ENTITIES:
        body = await parquet(entity, day, key=key, ch=ch)
        path = object_key(prefix, entity, day)
        out.append(Exported(entity, path, len(body), put(path, body)))
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
