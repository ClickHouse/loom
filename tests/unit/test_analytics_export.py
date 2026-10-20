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
from server.insights.app.analytics_export import export_day, name_hash_key, object_key, s3_put
from server.insights.store.analytics_export import (
    ENTITIES,
    NAME_COLUMNS,
    ROW_ORDER,
    _hashed,
    name_hash_sql,
)
from server.settings import settings


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


async def test_export_day_hashes_every_name_and_uploads_each_entity() -> None:
    ch, seen = _Ch(), []

    def put(key: str, body: bytes) -> bool:
        seen.append((key, body))
        return key.startswith("p/usage")

    day, name_key = date(2026, 10, 9), name_hash_key("secret")
    out = await export_day(day, prefix="p", put=put, name_key=name_key, ch=ch)  # type: ignore[arg-type]
    assert [(e.entity, e.written) for e in out] == [("usage", True), ("namespaces", False)]
    assert [k for k, _ in seen] == [e.key for e in out]
    for (sql, params), entity in zip(ch.calls, ENTITIES, strict=True):
        assert sql == f"{_hashed(entity)} FORMAT Parquet"
        assert params == {"day": day, "k0": name_key[0], "k1": name_key[1]}
        for column in NAME_COLUMNS[entity]:
            assert f"{name_hash_sql(column)} AS {column}" in sql


@pytest.mark.parametrize("entity", sorted(ENTITIES))
def test_only_the_hashed_statement_orders_rows(entity: str) -> None:
    # Rows sorted by the stored names would give the names back without the key.
    assert "ORDER BY" not in ENTITIES[entity]
    assert _hashed(entity).endswith(f") ORDER BY {ROW_ORDER[entity]}")


def test_the_hash_key_is_stable_and_depends_on_the_secret() -> None:
    assert name_hash_key("a") == name_hash_key("a") != name_hash_key("b")
    assert all(0 <= w < 2**64 for w in name_hash_key("a"))


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
        assert s3_put(lambda: s3, "b", expected_owner="123456789012")("k", b"data") is True
    stub.assert_no_pending_responses()


class _Session:
    """boto3.Session: records each session's arguments and the STS call."""

    assumed: list[dict[str, Any]] = []

    def __init__(self, **kw: Any) -> None:
        self.kw = kw

    def client(self, service: str) -> Any:
        if service == "sts":
            return self
        return (service, self.kw)

    def assume_role(self, **kw: Any) -> dict[str, Any]:
        _Session.assumed.append(kw)
        creds = {"AccessKeyId": "ak", "SecretAccessKey": "sk", "SessionToken": "st"}
        return {"Credentials": creds}


def test_the_s3_client_writes_under_the_assumed_role(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.insights.app.analytics_export import s3_client

    monkeypatch.setattr(boto3, "Session", _Session)
    monkeypatch.setattr(_Session, "assumed", [])
    service, kw = s3_client(role_arn="arn:aws:iam::123456789012:role/w", region="us-east-2")
    assert _Session.assumed == [
        {"RoleArn": "arn:aws:iam::123456789012:role/w", "RoleSessionName": "loom-analytics-export"}
    ]
    assert (service, kw) == (
        "s3",
        {
            "aws_access_key_id": "ak",
            "aws_secret_access_key": "sk",
            "aws_session_token": "st",
            "region_name": "us-east-2",
        },
    )
    assert s3_client() == ("s3", {"region_name": None}) and len(_Session.assumed) == 1


def test_each_upload_gets_a_fresh_client() -> None:
    s3, stub = _stubbed()
    made: list[Any] = []

    def client() -> Any:
        made.append(s3)
        return s3

    stub.add_response("put_object", {})
    stub.add_response("put_object", {})
    put = s3_put(client, "b")
    with stub:
        put("a", b"1")
        put("b", b"2")
    assert len(made) == 2


def test_an_existing_object_is_kept_and_other_errors_raise() -> None:
    s3, stub = _stubbed()
    stub.add_client_error(
        "put_object", service_error_code="PreconditionFailed", http_status_code=412
    )
    stub.add_client_error("put_object", service_error_code="AccessDenied", http_status_code=403)
    put = s3_put(lambda: s3, "b")
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


@pytest.mark.parametrize(
    ("key", "refused"),
    [
        ("", "needs LOOM_ANALYTICS_EXPORT_NAME_KEY"),
        ("k" * 31, "at least 32 characters"),
        ("k" * 32, ""),
    ],
)
def test_a_missing_or_short_name_key_is_refused(
    monkeypatch: pytest.MonkeyPatch, key: str, refused: str
) -> None:
    monkeypatch.setattr(settings, "analytics_export_name_key", key)
    got = cli._refusal(_args(day=date(2000, 1, 1)), date(2000, 1, 2))
    assert (refused in got) if refused else got == ""


class _ConnectingCh(_Ch):
    def __init__(self) -> None:
        super().__init__()
        self.read_timeout_at_connect = 0
        self.disconnected = False

    async def connect(self) -> None:
        from server.core.ch import _driver_read_timeout_s

        self.read_timeout_at_connect = _driver_read_timeout_s()

    async def disconnect(self) -> None:
        self.disconnected = True


def test_an_export_connects_inside_its_deadline_and_hashes_under_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server.core import ch as ch_module
    from server.insights.app import analytics_export as app

    secret = "s" * 32
    monkeypatch.setattr(settings, "analytics_export_name_key", secret)
    fake = _ConnectingCh()
    monkeypatch.setattr(ch_module, "get_client", lambda: fake)
    monkeypatch.setattr(app, "s3_put", lambda client, bucket, **kw: lambda key, body: True)
    assert cli._cmd_export(_args(day=date(2000, 1, 1))) == 0
    assert fake.read_timeout_at_connect >= app.STATEMENT_TIMEOUT_S and fake.disconnected
    assert all((p["k0"], p["k1"]) == name_hash_key(secret) for _, p in fake.calls)
    out = capsys.readouterr().out
    assert "s3://b/usage/2000/01/01/usage-2000-01-01.parquet (4 bytes, wrote)" in out


def test_day_must_be_iso() -> None:
    assert cli._day("2026-10-09") == date(2026, 10, 9)
    with pytest.raises(argparse.ArgumentTypeError):
        cli._day("10/09/2026")
