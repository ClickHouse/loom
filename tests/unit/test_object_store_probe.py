"""Fixed bucket diagnostics preserve ordinary clients and bound SDK parsing."""

from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.core import object_store
from server.core import object_store_probe as probe

DATE = "Wed, 18 Nov 2026 12:00:00 GMT"
AT = datetime(2026, 11, 18, 12, tzinfo=UTC)


class Raw:
    def __init__(self, value: bytes, *, fail: BaseException | None = None) -> None:
        self.body = io.BytesIO(value)
        self.fail = fail
        self.reads: list[int] = []
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        self.reads.append(amount)
        if self.fail is not None:
            raise self.fail
        return self.body.read(amount)

    def close(self) -> None:
        self.closed = True
        self.body.close()


def ack(status: int = 200, **fields: Any) -> dict[str, Any]:
    return {"ResponseMetadata": {"HTTPStatusCode": status, "HTTPHeaders": {"date": DATE}}, **fields}


def test_raw_exact_limit_and_cap_plus_one_close_before_join() -> None:
    raw = Raw(b"x" * probe._LIST_BYTES)
    assert (
        len(probe._Raw(raw, probe._LIST_BYTES, str(probe._LIST_BYTES)).read()) == probe._LIST_BYTES
    )
    assert raw.closed and max(raw.reads) <= 64 * 1024
    overflow = Raw(b"x" * (probe._LIST_BYTES + 1))
    with pytest.raises(probe.ProbeError):
        b"".join(probe._Raw(overflow, probe._LIST_BYTES, None).stream())
    assert overflow.closed and sum(overflow.reads) == probe._LIST_BYTES + 1


@pytest.mark.parametrize("length", ["65537", "-1", "1x", True, 65536])
def test_declared_error_length_refuses_before_a_read(length: Any) -> None:
    raw = Raw(b"payload")
    with pytest.raises(probe.ProbeError):
        probe._Raw(raw, 65536, length)
    assert raw.closed and not raw.reads


@pytest.mark.parametrize("value,declared", [(b"abc", "2"), (b"a", "2")])
def test_actual_length_mismatch_closes(value: bytes, declared: str) -> None:
    raw = Raw(value)
    with pytest.raises(probe.ProbeError):
        probe._Raw(raw, 65536, declared).read()
    assert raw.closed


def test_raw_preserves_the_original_interruption() -> None:
    original = KeyboardInterrupt()
    raw = Raw(b"", fail=original)
    with pytest.raises(KeyboardInterrupt) as raised:
        probe._Raw(raw, 1025, None).read()
    assert raised.value is original and raw.closed


@pytest.mark.parametrize("value", [None, "", "not a date", "Wed, 18 Nov 2026 12:00:00", True])
def test_no_local_clock_date_fallback(value: Any) -> None:
    with pytest.raises(probe.ProbeError):
        probe._date(value)
    assert probe._date(DATE) == AT


@pytest.fixture
def configured(settings_override: Any) -> None:
    settings_override(cell="west", document_s3_bucket="bucket", document_s3_prefix="root")


def page(*, keys: tuple[str, ...] = ("root/a", "root/b"), more: bool = False) -> dict[str, Any]:
    return ack(
        Name="bucket",
        Prefix="root/",
        MaxKeys=2,
        KeyCount=len(keys),
        IsTruncated=more,
        Contents=[{"Key": key, "LastModified": AT} for key in keys],
    )


def test_strict_page_is_the_actual_object_store_page(configured: None) -> None:
    result = probe._page(page(more=True), "root/", "", 2)
    assert type(result) is object_store.Page and result.more and result.at == AT
    assert result.objects == [object_store.Listed("root/a", AT), object_store.Listed("root/b", AT)]


@pytest.mark.parametrize(
    "mutation",
    [
        {"IsTruncated": 1},
        {"KeyCount": True},
        {"KeyCount": 3},
        {"MaxKeys": True},
        {"Name": "other"},
        {"Prefix": "other/"},
        {"Contents": None},
        {"Contents": [{"Key": "root/a", "LastModified": datetime(2026, 1, 1)}], "KeyCount": 1},
        {"IsTruncated": True, "Contents": [], "KeyCount": 0},
    ],
)
def test_partial_or_malformed_page_refuses(configured: None, mutation: dict[str, Any]) -> None:
    response = page() | mutation
    with pytest.raises(probe.ProbeError):
        probe._page(response, "root/", "", 2)


@pytest.mark.parametrize(
    "keys",
    [
        ("root/b", "root/a"),
        ("root/a", "root/a"),
        ("foreign/a",),
        ("root/" + "x" * 1020,),
        ("root/a", "root/b", "root/c"),
    ],
)
def test_cursor_order_foreign_key_and_size_refuse(configured: None, keys: tuple[str, ...]) -> None:
    with pytest.raises(probe.ProbeError):
        probe._page(page(keys=keys), "root/", "", 2)


def test_page_requires_exact_resume_echo_and_strict_progress(configured: None) -> None:
    response = page()
    with pytest.raises(probe.ProbeError):
        probe._page(response, "root/", "root/a", 2)
    response["StartAfter"] = "root/a"
    with pytest.raises(probe.ProbeError):
        probe._page(response, "root/", "root/a", 2)
    response.update(KeyCount=1, Contents=[{"Key": "root/b", "LastModified": AT}])
    assert probe._page(response, "root/", "root/a", 2).objects == [
        object_store.Listed("root/b", AT)
    ]


def test_only_the_configured_own_marker_can_be_addressed(
    configured: None, settings_override: Any
) -> None:
    assert probe._location() == object_store.Location("bucket", "root/_cell_attach_probe/west")
    settings_override(cell="other/path")
    with pytest.raises(probe.ProbeError):
        probe._location()
    settings_override(cell="west", document_s3_prefix="x" * 510)
    with pytest.raises(probe.ProbeError):
        probe._location()
    settings_override(document_s3_bucket="")
    with pytest.raises(probe.ProbeError):
        probe._location()


@pytest.mark.parametrize("etag", ["*", '"*"', "", "bare", 'W/"weak"', '"bad\nvalue"', None])
def test_conditional_delete_requires_an_exact_strong_etag(etag: Any) -> None:
    with pytest.raises(probe.ProbeError):
        probe._etag(etag)


class MarkerClient:
    def __init__(self, value: bytes = b"own", etag: str = '"first"') -> None:
        self.body = Raw(value)
        self.etag = etag
        self.deletes: list[dict[str, Any]] = []
        self.ack = ack(204)

    def get_object(self, **kw: Any) -> dict[str, Any]:
        assert kw == {"Bucket": "bucket", "Key": "root/_cell_attach_probe/west"}
        # The actual transport bound is exercised separately through botocore.
        return ack(ETag=self.etag, Body=self.body)

    def delete_object(self, **kw: Any) -> dict[str, Any]:
        self.deletes.append(kw)
        return self.ack


def test_read_returns_bytes_and_etag_from_the_same_closed_get(configured: None) -> None:
    client = MarkerClient()
    assert probe._read(client, probe._location()) == probe.Marker(b"own", '"first"', AT)
    assert client.body.closed


@pytest.mark.asyncio
async def test_delete_uses_exact_owned_bytes_and_ifmatch(
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MarkerClient()
    monkeypatch.setattr(probe, "_client", lambda: client)
    await probe.delete_marker(b"own")
    assert client.body.closed
    assert client.deletes == [
        {"Bucket": "bucket", "Key": "root/_cell_attach_probe/west", "IfMatch": '"first"'}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value,etag", [(b"other", '"first"'), (b"own", "*"), (b"x" * 1025, '"first"')]
)
async def test_foreign_or_invalid_marker_never_deletes(
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
    value: bytes,
    etag: str,
) -> None:
    client = MarkerClient(value, etag)
    monkeypatch.setattr(probe, "_client", lambda: client)
    with pytest.raises(probe.ProbeError):
        await probe.delete_marker(b"own")
    assert client.body.closed and not client.deletes


@pytest.mark.asyncio
async def test_unsupported_delete_is_incomplete_without_an_unconditional_retry(
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MarkerClient()
    client.ack = ack(200)
    monkeypatch.setattr(probe, "_client", lambda: client)
    with pytest.raises(probe.ProbeError):
        await probe.delete_marker(b"own")
    assert len(client.deletes) == 1 and client.deletes[0]["IfMatch"] == '"first"'


@pytest.fixture
def sdk_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    # Synthetic unit credentials only; all transport is replaced on this client.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "unit-access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "unit-secret")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_CONFIG_FILE", raising=False)
    monkeypatch.delenv("AWS_DATA_PATH", raising=False)
    object_store._client_for.cache_clear()
    client = object_store._client_for("https://store.invalid", "us-east-1", 1, 2, diagnostic=True)
    try:
        yield client
    finally:
        client.close()
        object_store._client_for.cache_clear()


def response(status: int, value: bytes, *, date: str = DATE) -> tuple[Any, Raw]:
    from botocore.awsrequest import AWSResponse

    raw = Raw(value)
    return AWSResponse(
        "https://store.invalid", status, {"date": date, "content-length": str(len(value))}, raw
    ), raw


@pytest.mark.parametrize("status", [200, 307])
def test_actual_sdk_error_seam_sends_once_without_headbucket(
    sdk_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    value = b"<Error><Code>PermanentRedirect</Code><Endpoint>elsewhere.invalid</Endpoint></Error>"
    reply, raw = response(status, value)
    sent: list[Any] = []

    def send(request: Any) -> Any:
        sent.append(request)
        assert request.stream_output is True
        return reply

    monkeypatch.setattr(sdk_client._endpoint.http_session, "send", send)
    with pytest.raises(probe.ProbeError):
        sdk_client.list_objects_v2(Bucket="bucket", Prefix="root/", MaxKeys=2)
    assert len(sent) == 1 and sent[0].stream_output is False and raw.closed


def test_actual_sdk_list_uses_ordinary_parser_after_raw_cap(
    sdk_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    configured: None,
) -> None:
    value = b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>bucket</Name><Prefix>root/</Prefix><MaxKeys>2</MaxKeys><KeyCount>1</KeyCount><IsTruncated>false</IsTruncated><Contents><Key>root/a</Key><LastModified>2026-11-18T12:00:00.000Z</LastModified><ETag>"e"</ETag><Size>1</Size></Contents></ListBucketResult>'
    reply, raw = response(200, value)
    monkeypatch.setattr(sdk_client._endpoint.http_session, "send", lambda request: reply)
    parsed = sdk_client.list_objects_v2(Bucket="bucket", Prefix="root/", MaxKeys=2)
    result = probe._page(parsed, "root/", "", 2)
    assert result.objects == [object_store.Listed("root/a", AT)] and not result.more
    assert raw.closed and max(raw.reads) <= 64 * 1024


def test_actual_sdk_get_cannot_materialize_marker_overflow(
    sdk_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    configured: None,
) -> None:
    reply, raw = response(200, b"x" * 1026)
    reply.headers["etag"] = '"e"'
    monkeypatch.setattr(sdk_client._endpoint.http_session, "send", lambda request: reply)
    with pytest.raises(probe.ProbeError):
        probe._read(sdk_client, probe._location())
    assert raw.closed and not raw.reads


def test_send_restores_request_flag_on_transport_failure() -> None:
    original = KeyboardInterrupt()

    def send(request: Any) -> Any:
        assert request.stream_output is True
        raise original

    request = SimpleNamespace(context={}, stream_output=False)
    client = SimpleNamespace(_endpoint=SimpleNamespace(http_session=SimpleNamespace(send=send)))
    with pytest.raises(KeyboardInterrupt) as raised:
        probe._send(client, "PutObject", request)
    assert raised.value is original and request.stream_output is False
    with pytest.raises(probe.ProbeError):
        probe._send(client, "PutObject", request)


def test_shared_factory_preserves_ordinary_client_policy(sdk_client: Any) -> None:
    ordinary = object_store._client_config(2.5, 45)
    diagnostic = object_store._client_config(2.5, 45, diagnostic=True)
    assert ordinary.retries == {"max_attempts": 3, "mode": "standard"}
    assert diagnostic.retries == {"total_max_attempts": 1, "mode": "standard"}
    ordinary_options = dict(ordinary._user_provided_options)
    diagnostic_options = dict(diagnostic._user_provided_options)
    ordinary_options.pop("retries")
    diagnostic_options.pop("retries")
    assert ordinary_options == diagnostic_options
    assert diagnostic.max_pool_connections == object_store._THREADS == 16
    assert sdk_client is object_store._client_for(
        "https://store.invalid", "us-east-1", 1, 2, diagnostic=True
    )


def test_sdk_source_identity_refuses_a_changed_installed_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "_regular_digest", lambda path, size: "0" * 64)
    with pytest.raises(probe.ProbeError):
        probe.require_sdk()


def test_actual_sdk_get_closes_transport_before_parser_and_keeps_marker_bytes(
    sdk_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    configured: None,
) -> None:
    reply, raw = response(200, b"own")
    reply.headers["etag"] = '"first"'
    monkeypatch.setattr(sdk_client._endpoint.http_session, "send", lambda request: reply)
    assert probe._read(sdk_client, probe._location()) == probe.Marker(b"own", '"first"', AT)
    assert raw.closed and max(raw.reads) <= 1025 + 1


def test_actual_sdk_error_bytes_are_capped_before_parsing(
    sdk_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply, raw = response(403, b"x" * (65536 + 1))
    monkeypatch.setattr(sdk_client._endpoint.http_session, "send", lambda request: reply)
    with pytest.raises(probe.ProbeError):
        sdk_client.put_object(Bucket="bucket", Key="root/_cell_attach_probe/west", Body=b"own")
    assert raw.closed and not raw.reads


def test_actual_sdk_needs_retry_guard_precedes_wildcard_region_handler(
    sdk_client: Any,
) -> None:
    later: list[bool] = []
    sdk_client.meta.events.register_first("needs-retry.s3", lambda **kwargs: later.append(True))
    with pytest.raises(probe.ProbeError):
        sdk_client.meta.events.emit(
            "needs-retry.s3.ListObjectsV2",
            response=(SimpleNamespace(status_code=200), {"Error": {"Code": "PermanentRedirect"}}),
            attempts=1,
            caught_exception=None,
        )
    assert not later


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [412, 501])
async def test_actual_sdk_replacement_or_unsupported_delete_leaves_slot_incomplete(
    sdk_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    configured: None,
    status: int,
) -> None:
    requests: list[Any] = []
    raws: list[Raw] = []

    def send(request: Any) -> Any:
        requests.append(request)
        if request.method == "GET":
            reply, raw = response(200, b"own")
            reply.headers["etag"] = '"first"'
        else:
            assert request.method == "DELETE" and request.headers["If-Match"] == b'"first"'
            reply, raw = response(status, b"<Error><Code>PreconditionFailed</Code></Error>")
        raws.append(raw)
        return reply

    monkeypatch.setattr(sdk_client._endpoint.http_session, "send", send)
    monkeypatch.setattr(probe, "_client", lambda: sdk_client)
    with pytest.raises(probe.ProbeError):
        await probe.delete_marker(b"own")
    assert [r.method for r in requests] == ["GET", "DELETE"]
    assert all(raw.closed for raw in raws)


def test_actual_sdk_missing_date_closes_before_parser(
    sdk_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply, raw = response(200, b"", date="")
    monkeypatch.setattr(sdk_client._endpoint.http_session, "send", lambda request: reply)
    with pytest.raises(probe.ProbeError):
        sdk_client.put_object(Bucket="bucket", Key="root/_cell_attach_probe/west", Body=b"own")
    assert raw.closed and not raw.reads


def test_ordinary_client_does_not_receive_diagnostic_hooks(sdk_client: Any) -> None:
    from functools import partial

    ordinary = object_store._client_for("https://store.invalid", "us-east-1", 1, 2)
    try:
        listeners = ordinary.meta.events._emitter._handlers.prefix_search(
            "before-send.s3.ListObjectsV2"
        )
        assert ordinary is not sdk_client
        assert ordinary.meta.config.retries == {"total_max_attempts": 4, "mode": "standard"}
        assert not any(
            isinstance(handler, partial) and handler.func is probe._send for handler in listeners
        )
        assert ordinary._endpoint.http_session is not sdk_client._endpoint.http_session
    finally:
        ordinary.close()


def test_unsupported_sdk_version_refuses_before_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probe.metadata, "distribution", lambda name: SimpleNamespace(version="unsupported")
    )
    with pytest.raises(probe.ProbeError):
        probe.require_sdk()


def test_actual_sdk_express_signing_refuses_before_session_resolver(sdk_client: Any) -> None:
    later: list[bool] = []
    sdk_client.meta.events.register_first("before-sign.s3", lambda **kwargs: later.append(True))
    request = SimpleNamespace(context={"signing": {"signing_name": "s3express"}})
    with pytest.raises(probe.ProbeError):
        sdk_client.meta.events.emit(
            "before-sign.s3.GetObject",
            request=request,
            signing_name="s3",
            signature_version="v4-s3express",
        )
    assert not later


def test_custom_sdk_model_path_is_unsupported_without_importing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_DATA_PATH", "/not-opened/unit-model")
    with pytest.raises(probe.ProbeError):
        probe.require_sdk()


def test_a_changed_loaded_service_model_is_not_installed(
    sdk_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "_MODEL_SHA256", "0" * 64)
    with pytest.raises(probe.ProbeError):
        probe.install(sdk_client)


def prefix_reader(monkeypatch: pytest.MonkeyPatch, make: Any) -> list[str]:
    """Project synthetic wire pages through the real strict page validator."""
    calls: list[str] = []

    async def read(*, start_after: str = "", max_keys: int = 1000) -> object_store.Page:
        assert max_keys == 1000
        index = len(calls)
        calls.append(start_after)
        response = make(index, start_after)
        return probe._page(response, "root/", start_after, max_keys)

    monkeypatch.setattr(probe, "list_page", read)
    return calls


def prefix_page(keys: tuple[str, ...], after: str, *, more: bool = False) -> dict[str, Any]:
    return page(keys=keys, more=more) | {"MaxKeys": 1000, "StartAfter": after}


@pytest.mark.asyncio
async def test_prefix_exhausts_all_pages_and_exports_no_keys(
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = (("root/a", "root/b"), ("root/c",), ("root/z",))
    calls = prefix_reader(
        monkeypatch,
        lambda index, after: prefix_page(keys[index], after, more=index < 2),
    )
    assert await probe.check_prefix() is None
    assert calls == ["", "root/b", "root/c"]


@pytest.mark.asyncio
async def test_prefix_empty_final_page_finishes_without_an_extra_read(
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = prefix_reader(monkeypatch, lambda index, after: prefix_page((), after))
    assert await probe.check_prefix() is None
    assert calls == [""]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        "root/_cell_attach_probe/east",
        "root/nested/_cell_attach_probe/east",
        "root/nested/more/_cell_attach_probe/east",
    ],
)
async def test_prefix_foreign_fixed_marker_refuses_at_any_depth_without_export(
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    calls = prefix_reader(
        monkeypatch,
        lambda index, after: prefix_page((key,), after, more=True),
    )
    with pytest.raises(probe.ProbeError) as raised:
        await probe.check_prefix()
    assert calls == [""] and key not in str(raised.value)


@pytest.mark.asyncio
async def test_prefix_own_marker_and_nonmarker_suffixes_are_not_foreign_markers(
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = tuple(
        sorted(
            (
                "root/_cell_attach_probe/west",
                "root/deep/_cell_attach_probe/west",
                "root/deep/_cell_attach_probe/east/child",
                "root/deep/_cell_attach_probe/east_suffix",
                "root/deep/_cell_attach_probe/EAST",
                "root/deep/not_cell_attach_probe/east",
            )
        )
    )
    calls = prefix_reader(monkeypatch, lambda index, after: prefix_page(keys, after))
    assert await probe.check_prefix() is None
    assert calls == [""]


@pytest.mark.asyncio
async def test_prefix_finds_a_foreign_marker_after_an_earlier_clean_page(
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = (("root/a",), ("root/z/_cell_attach_probe/east",))
    calls = prefix_reader(
        monkeypatch,
        lambda index, after: prefix_page(keys[index], after, more=index == 0),
    )
    with pytest.raises(probe.ProbeError):
        await probe.check_prefix()
    assert calls == ["", "root/a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("more", [False, True])
async def test_prefix_exact_object_limit_never_fetches_page101(
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
    more: bool,
) -> None:
    def make(index: int, after: str) -> dict[str, Any]:
        assert index < 100
        keys = tuple(f"root/{number:06d}" for number in range(index * 1000, (index + 1) * 1000))
        return prefix_page(keys, after, more=index < 99 or more)

    calls = prefix_reader(monkeypatch, make)
    if more:
        with pytest.raises(probe.ProbeError):
            await probe.check_prefix()
    else:
        assert await probe.check_prefix() is None
    assert len(calls) == 100 and calls[-1] == "root/098999"


@pytest.mark.asyncio
async def test_prefix_page_limit_refuses_even_when_object_count_is_lower(
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = prefix_reader(
        monkeypatch,
        lambda index, after: prefix_page((f"root/{index:06d}",), after, more=True),
    )
    with pytest.raises(probe.ProbeError):
        await probe.check_prefix()
    assert len(calls) == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [0, 1])
async def test_prefix_utf8_bytes_exact_limit_and_limit_plus_one(
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
    extra: int,
) -> None:
    # 16383 * 1024 + 512 + 512 == 16 MiB; characters are mostly two-byte UTF-8.
    def make(index: int, after: str) -> dict[str, Any]:
        keys: list[str] = []
        for number in range(index * 1000, min((index + 1) * 1000, 16385)):
            length = 1024 if number < 16383 else 512 + (extra if number == 16384 else 0)
            head = f"root/{number:06d}/"
            remaining = length - len(head)
            keys.append(head + "é" * (remaining // 2) + "x" * (remaining % 2))
        return prefix_page(tuple(keys), after, more=index < 16)

    calls = prefix_reader(monkeypatch, make)
    if extra:
        with pytest.raises(probe.ProbeError):
            await probe.check_prefix()
    else:
        assert await probe.check_prefix() is None
    assert len(calls) == 17


@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["root/a", "root/0"])
async def test_prefix_duplicate_or_regressing_cross_page_key_refuses(
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
    second: str,
) -> None:
    calls = prefix_reader(
        monkeypatch,
        lambda index, after: prefix_page(
            ("root/a" if index == 0 else second,), after, more=index == 0
        ),
    )
    with pytest.raises(probe.ProbeError):
        await probe.check_prefix()
    assert calls == ["", "root/a"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"IsTruncated": None},
        {"IsTruncated": 1},
        {"KeyCount": 2},
        {"Contents": [], "KeyCount": 0, "IsTruncated": True},
        {"StartAfter": "root/wrong"},
        {"ResponseMetadata": {}},
    ],
)
async def test_prefix_partial_page_never_becomes_exhaustion(
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
    mutation: dict[str, Any],
) -> None:
    def make(index: int, after: str) -> dict[str, Any]:
        if index == 0:
            return prefix_page(("root/a",), after, more=True)
        return prefix_page(("root/b",), after) | mutation

    calls = prefix_reader(monkeypatch, make)
    with pytest.raises(probe.ProbeError):
        await probe.check_prefix()
    assert calls == ["", "root/a"]


@pytest.mark.asyncio
async def test_prefix_page1001st_object_is_rejected_by_the_existing_page_owner(
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = tuple(f"root/{number:06d}" for number in range(1001))
    calls = prefix_reader(monkeypatch, lambda index, after: prefix_page(keys, after))
    with pytest.raises(probe.ProbeError):
        await probe.check_prefix()
    assert calls == [""]


@pytest.mark.asyncio
@pytest.mark.parametrize("original", [RuntimeError("read failed"), asyncio.CancelledError()])
async def test_prefix_keeps_the_original_read_failure_or_cancellation(
    configured: None,
    monkeypatch: pytest.MonkeyPatch,
    original: BaseException,
) -> None:
    calls: list[str] = []

    async def read(*, start_after: str = "", max_keys: int = 1000) -> object_store.Page:
        calls.append(start_after)
        if not start_after:
            return probe._page(prefix_page(("root/a",), "", more=True), "root/", "", max_keys)
        raise original

    monkeypatch.setattr(probe, "list_page", read)
    with pytest.raises(type(original)) as raised:
        await probe.check_prefix()
    assert raised.value is original and calls == ["", "root/a"]
