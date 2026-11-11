"""`loom export-analytics`: object keys, a conditional upload that never
replaces a file, and statements that carry no memory content or person."""

from __future__ import annotations

import argparse
import re
from datetime import UTC, date, datetime
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from server.cli import analytics_export as cli
from server.insights.app.analytics_export import export_day, name_hash_key, object_key, s3_put
from server.insights.store.analytics_export import (
    ENTITIES,
    STATEMENT_SETTINGS,
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
    assert len(ch.calls) == len(ENTITIES)
    for sql, params in ch.calls:
        assert sql.endswith(f"{STATEMENT_SETTINGS} FORMAT Parquet")
        assert params == {"day": day, "k0": name_key[0], "k1": name_key[1]}


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
    identified: list[dict[str, Any]] = []

    def __init__(self, **kw: Any) -> None:
        self.kw = kw

    def get_caller_identity(self) -> dict[str, Any]:
        _Session.identified.append(self.kw)
        return {"Arn": "arn:aws:sts::123456789012:assumed-role/export/pod"}

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


def test_credentials_are_checked_with_or_without_a_role(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.insights.app.analytics_export import check_credentials

    monkeypatch.setattr(boto3, "Session", _Session)
    monkeypatch.setattr(_Session, "assumed", [])
    monkeypatch.setattr(_Session, "identified", [])
    check_credentials(role_arn="arn:aws:iam::123456789012:role/w", region="us-east-2")
    assert len(_Session.assumed) == 1 and _Session.identified == []
    # The pod's own credentials (IRSA with no writer role): STS says whose they are.
    check_credentials(region="us-east-2")
    assert len(_Session.assumed) == 1 and _Session.identified == [{"region_name": "us-east-2"}]


def test_each_upload_gets_a_fresh_client() -> None:
    s3, stub = _stubbed()
    made: list[Any] = []

    def client() -> Any:
        made.append(s3)
        return s3

    # No expected owner given: the parameter is left out, not sent empty.
    stub.add_response(
        "put_object", {}, {"Bucket": "b", "Key": "a", "Body": b"1", "IfNoneMatch": "*"}
    )
    stub.add_response(
        "put_object", {}, {"Bucket": "b", "Key": "b", "Body": b"2", "IfNoneMatch": "*"}
    )
    put = s3_put(client, "b")
    with stub:
        put("a", b"1")
        put("b", b"2")
    stub.assert_no_pending_responses()
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
        with pytest.raises(ClientError, match="AccessDenied"):
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


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """The CLI's clock, pinned: 2026-10-10 00:30 UTC, when the CronJob runs."""
    at = datetime(2026, 10, 10, 0, 30, tzinfo=UTC)
    monkeypatch.setattr(cli, "_now", lambda: at)
    return at


@pytest.mark.parametrize("day", [date(2026, 10, 10), date(2026, 10, 11)])
def test_a_day_that_has_not_ended_is_refused(
    now: datetime, day: date, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli._cmd_export(_args(day=day)) == 2
    assert "has not ended" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("key", "refused"),
    [
        ("", "needs LOOM_ANALYTICS_EXPORT_NAME_KEY"),
        ("k" * 31, "at least 32 characters"),
        ("k" * 32, ""),
        ("k" * 32 + "\n", "leading or trailing whitespace"),
        (" " + "k" * 32, "leading or trailing whitespace"),
    ],
)
def test_a_missing_short_or_padded_name_key_is_refused(
    monkeypatch: pytest.MonkeyPatch, key: str, refused: str
) -> None:
    monkeypatch.setattr(settings, "analytics_export_name_key", key)
    got = cli._refusal(date(2000, 1, 1), date(2000, 1, 2))
    assert (refused in got) if refused else got == ""


class _ConnectingCh(_Ch):
    def __init__(self) -> None:
        super().__init__()
        self.read_timeout_at_connect = 0
        self.disconnected = False

    connected = False

    async def connect(self) -> None:
        from server.core.ch import _driver_read_timeout_s

        self.read_timeout_at_connect = _driver_read_timeout_s()

    async def disconnect(self) -> None:
        self.disconnected = True


def test_an_export_of_yesterday_connects_inside_its_deadline_and_hashes_under_the_key(
    now: datetime, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server.core import ch as ch_module
    from server.insights.app import analytics_export as app

    secret = "s" * 32
    monkeypatch.setattr(settings, "analytics_export_name_key", secret)
    fake = _ConnectingCh()
    monkeypatch.setattr(ch_module, "get_client", lambda: fake)
    monkeypatch.setattr(app, "s3_client", lambda **kw: None)
    monkeypatch.setattr(app, "check_credentials", lambda **kw: None)
    # usage is written, namespaces is already there from an earlier run.
    monkeypatch.setattr(
        app, "s3_put", lambda client, bucket, **kw: lambda key, body: "/usage/" in f"/{key}"
    )
    assert cli._cmd_export(_args()) == 0
    assert fake.read_timeout_at_connect >= app.STATEMENT_TIMEOUT_S and fake.disconnected
    assert all(
        p
        == {
            "day": date(2026, 10, 9),
            "k0": name_hash_key(secret)[0],
            "k1": name_hash_key(secret)[1],
        }
        for _, p in fake.calls
    )
    out = capsys.readouterr().out
    assert "s3://b/usage/2026/10/09/usage-2026-10-09.parquet (wrote 4 bytes)" in out
    kept = "s3://b/namespaces/2026/10/09/namespaces-2026-10-09.parquet (already there, kept)"
    assert kept in out


def test_day_must_be_iso() -> None:
    assert cli._day("2026-10-09") == date(2026, 10, 9)
    with pytest.raises(argparse.ArgumentTypeError):
        cli._day("10/09/2026")


def test_a_role_that_cannot_be_assumed_fails_before_any_read(
    now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core import ch as ch_module
    from server.insights.app import analytics_export as app

    monkeypatch.setattr(settings, "analytics_export_name_key", "s" * 32)
    fake = _ConnectingCh()
    monkeypatch.setattr(ch_module, "get_client", lambda: fake)

    def refused(**kw: Any) -> Any:
        raise PermissionError("AccessDenied: sts:AssumeRole")

    monkeypatch.setattr(app, "s3_client", refused)
    with pytest.raises(PermissionError):
        cli._cmd_export(_args(role_arn="arn:aws:iam::123456789012:role/w"))
    assert fake.calls == [] and fake.read_timeout_at_connect == 0


@pytest.mark.parametrize(
    ("boto3", "botocore", "refused"),
    [
        ("1.35.1", "1.35.1", True),
        ("1.35.2", "1.35.2", False),
        ("1.43.102", "1.43.102", False),
        (None, "1.43.102", True),  # botocore alone, e.g. from the AWS CLI v1
        ("1.43.102", None, True),
    ],
)
def test_a_missing_boto3_or_an_old_botocore_is_refused(
    monkeypatch: pytest.MonkeyPatch, boto3: str | None, botocore: str | None, refused: bool
) -> None:
    import importlib.metadata

    installed = {"boto3": boto3, "botocore": botocore}

    def version(name: str) -> str:
        if installed[name] is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return str(installed[name])

    monkeypatch.setattr(settings, "analytics_export_name_key", "k" * 32)
    monkeypatch.setattr(importlib.metadata, "version", version)
    got = cli._refusal(date(2000, 1, 1), date(2000, 1, 2))
    assert ("needs boto3 and botocore 1.35.2 or later" in got) is refused


def test_the_chart_fails_the_job_on_the_clis_refusal_code() -> None:
    from server.core.paths import REPO_ROOT

    template = (REPO_ROOT / "deploy/helm/loom/templates/analytics-export.yaml").read_text()
    rule = re.search(r"- action: FailJob\s+onExitCodes:.*?values: \[(\d+)\]", template, re.S)
    assert rule is not None and int(rule.group(1)) == cli.REFUSED


def test_the_reference_siphash_matches_the_published_vectors() -> None:
    from tests.siphash_reference import siphash_2_4_128

    key = bytes(range(16))
    assert siphash_2_4_128(key, b"").hex() == "a3817f04ba25a8e66df67214c7550293"
    assert siphash_2_4_128(key, bytes(range(15))).hex() == "5493e99933b0a8117e08ec0f97cfc3d9"


def test_the_key_words_are_the_documented_sha256_halves() -> None:
    import hashlib

    secret = "s" * 32
    digest = hashlib.sha256(secret.encode()).digest()
    k0, k1 = name_hash_key(secret)
    assert k0.to_bytes(8, "little") + k1.to_bytes(8, "little") == digest[:16]


async def test_a_body_that_is_not_a_whole_parquet_file_is_never_uploaded() -> None:
    class _CutOff(_Ch):
        async def raw_bytes(self, sql: str, params: dict[str, Any] | None = None) -> bytes:
            return b"PAR1 then the stream stopped"

    uploaded: list[str] = []

    def put(key: str, body: bytes) -> bool:
        uploaded.append(key)
        return True

    with pytest.raises(ValueError, match="not a whole Parquet file"):
        await export_day(date(2026, 10, 9), prefix="", put=put, name_key=(1, 2), ch=_CutOff())  # type: ignore[arg-type]
    assert uploaded == []
