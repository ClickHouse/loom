"""`loom export-analytics`: object keys, a conditional upload that never
replaces a file, and statements that carry no memory content or person."""

from __future__ import annotations

import argparse
import re
from datetime import date
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from server.cli import analytics_export as cli
from server.insights.app.analytics_export import export_day, object_key, s3_put
from server.insights.store.analytics_export import ENTITIES


def test_object_keys_are_one_per_entity_and_day() -> None:
    day = date(2026, 10, 9)
    assert object_key("", "usage", day) == "usage/2026/10/09/usage-2026-10-09.parquet"
    assert object_key("/loom/", "namespaces", day) == (
        "loom/namespaces/2026/10/09/namespaces-2026-10-09.parquet"
    )


class _Ch:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def raw_bytes(self, sql: str, params: dict[str, Any] | None = None) -> bytes:
        self.calls.append((sql, params or {}))
        return b"PAR1"


async def test_export_day_reads_and_uploads_each_entity() -> None:
    ch, seen = _Ch(), []

    def put(key: str, body: bytes) -> bool:
        seen.append((key, body))
        return key.startswith("p/usage")

    out = await export_day(date(2026, 10, 9), prefix="p", put=put, ch=ch)  # type: ignore[arg-type]
    assert [(e.entity, e.written) for e in out] == [("usage", True), ("namespaces", False)]
    assert all(sql.endswith("FORMAT Parquet") for sql, _ in ch.calls)
    assert all(params == {"day": date(2026, 10, 9)} for _, params in ch.calls)
    assert [k for k, _ in seen] == [e.key for e in out]


def _stubbed() -> tuple[Any, Stubber]:
    s3 = boto3.client(
        "s3", region_name="us-east-2", aws_access_key_id="x", aws_secret_access_key="y"
    )
    return s3, Stubber(s3)


def test_upload_is_conditional_and_names_the_bucket_owner() -> None:
    s3, stub = _stubbed()
    expected = {
        "Bucket": "b",
        "Key": "k",
        "Body": b"data",
        "IfNoneMatch": "*",
        "ExpectedBucketOwner": "123456789012",
    }
    stub.add_response("put_object", {}, expected)
    with stub:
        assert s3_put(s3, "b", expected_owner="123456789012")("k", b"data") is True
    stub.assert_no_pending_responses()


def test_an_existing_object_is_kept_and_other_errors_raise() -> None:
    s3, stub = _stubbed()
    stub.add_client_error(
        "put_object", service_error_code="PreconditionFailed", http_status_code=412
    )
    stub.add_client_error("put_object", service_error_code="AccessDenied", http_status_code=403)
    put = s3_put(s3, "b")
    with stub:
        assert put("k", b"data") is False
        with pytest.raises(Exception, match="AccessDenied"):
            put("k", b"data")


# Columns that hold memory content or name a person. `user_id` and `session_id`
# may appear only inside the distinct counts.
_NEVER = ("memory_key", "value", "args", "subject", "owner_email", "impersonated_by", "request_id")


@pytest.mark.parametrize("entity", sorted(ENTITIES))
def test_statements_export_no_content_and_no_person(entity: str) -> None:
    sql = ENTITIES[entity]
    for column in _NEVER:
        assert not re.search(rf"\b{column}\b", sql), column
    rest = re.sub(r"uniqExactIf\((user_id|session_id), \1 != '-'\)", "", sql)
    assert not re.search(r"\b(user_id|session_id)\b", rest)


def _args(**kw: Any) -> argparse.Namespace:
    base = {
        "day": None,
        "bucket": "b",
        "prefix": "",
        "role_arn": "",
        "region": "",
        "expected_bucket_owner": "",
    }
    return argparse.Namespace(**{**base, **kw})


def test_a_day_that_has_not_ended_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli._cmd_export(_args(day=date(2999, 1, 1))) == 2
    assert "has not ended" in capsys.readouterr().err


def test_day_must_be_iso() -> None:
    assert cli._day("2026-10-09") == date(2026, 10, 9)
    with pytest.raises(argparse.ArgumentTypeError):
        cli._day("10/09/2026")
