"""Bounded reads and exact-owned marker writes for cell separation checks.

These operations use the deployment's ordinary bucket configuration. A refused
or unknown response is never evidence of absence or completed cleanup.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from importlib import metadata
from pathlib import Path
from typing import Any, cast

from server.core import object_store
from server.core.cell import NAME
from server.core.ch import set_query_tag
from server.settings import settings

_LIST_BYTES = 8 * 1024 * 1024
_ERROR_BYTES = 64 * 1024
_MARKER_BYTES = 1024
_CHUNK = 64 * 1024
_ETAG = re.compile(r'"[\x21\x23-\x7e]{1,1022}"')
_SENT = "loom_cell_probe_sent"
_MODEL_SHA256 = "fef1a4b5a29d993ec1659fd203d7e0d954bcfbea528707ef0ea285382fbeb982"
# Installed source and model identity reviewed against the locked packages.
_SDK_FILES: dict[str, tuple[str, dict[str, tuple[int, str]]]] = {
    "boto3": (
        "1.43.102",
        {
            "boto3/session.py": (
                22106,
                "c113623eabf46a1438af30940513cdd54b94ede6d7b6894743d5f7e1d18b9a78",
            ),
            "boto3-1.43.102.dist-info/METADATA": (
                6571,
                "ecfda0e6c61eba7a5813b0be14364808ad56ad66e9af31b9a6a07e9a5d27fccb",
            ),
        },
    ),
    "botocore": (
        "1.43.102",
        {
            "botocore/endpoint.py": (
                17469,
                "0ce924a118e3ad98a2c71293541508efc1b3e0f84fb1b80272394829ba656262",
            ),
            "botocore/httpsession.py": (
                20785,
                "14b2961314c711499da785ec30f00d9b04ead55a4403b2aba5c5e822ffd93b7b",
            ),
            "botocore/awsrequest.py": (
                23375,
                "85d0ae028b154a991db1078c70987440c290c55b9c34f66e8ae4053de5d22c2f",
            ),
            "botocore/response.py": (
                7865,
                "e2ee6f81ec35f5dae9879f5f5c600dca22b78a2c43cefb0dac3b8b894ce9d11d",
            ),
            "botocore/parsers.py": (
                60629,
                "2367dc4b9f07e495185c65ea6e9d37014133968de19f860822f48b1a7e1f26ea",
            ),
            "botocore/client.py": (
                54779,
                "0e7b790ccb6aa2ca34803c6878aaead7db9108eee4e3fba0aa66008b647967f9",
            ),
            "botocore/hooks.py": (
                25053,
                "a484c295983cf1e8905f73ec435ff7db0c9d8b9d8b24321a9c5d18bdfd81e312",
            ),
            "botocore/config.py": (
                20566,
                "3ff88b9f5af4b19a41792e5696918135e0bfc64011dcb4843fb515922239014f",
            ),
            "botocore/utils.py": (
                138508,
                "e9d9182eacf6881499284a357fa1759796d7f57bc32930b691a92ad9915f2530",
            ),
            "botocore/data/s3/2006-03-01/service-2.json.gz": (
                184637,
                "e338f63ec7e05cc17ba0bb77ab9a2608ddff1bcba490f672ecc2c9bc32761c33",
            ),
            "botocore-1.43.102.dist-info/METADATA": (
                5570,
                "fe68d4befe049f7c6cd63df10c32f6d1a629a5561d1877d655323c5bcb9886c7",
            ),
            "botocore/handlers.py": (
                68835,
                "504f9fd31a56a197dd63ee46796f86de471c02e550b2813a5fbcfacf3bcd6819",
            ),
            "botocore/retries/standard.py": (
                27817,
                "ba5ede051c74c789ce59b03e9dff32c4da252190032ff7f512479b8ce1cc598b",
            ),
            "botocore/model.py": (
                32334,
                "1f502c7ddf1ca329b695e30d36eff773fd2c483434ef1f1d9bc27582d1c38b8a",
            ),
            "botocore/signers.py": (
                34984,
                "3340dfd41a217eea8a8418eabeab1a00c68a21057b75c3b094c0fc33d2e1f17e",
            ),
            "botocore/loaders.py": (
                18826,
                "aa2be08c3dd386f52681b27e325d28664cd9c6b9cdbd6cf8eff7d2eda66aea42",
            ),
            "botocore/data/s3/2006-03-01/service-2.sdk-extras.json": (
                98,
                "1617a38a6d78af2b696fb4e16bfd18eb72ad73aa9d247bc078643cf9dfd66408",
            ),
            "botocore/data/s3/2006-03-01/endpoint-rule-set-1.json.gz": (
                23219,
                "f0127007d896917ddc62361ac8db64f47be2c7470f6ae290afdca587cc2758cb",
            ),
        },
    ),
    "urllib3": (
        "2.8.0",
        {
            "urllib3/response.py": (
                54628,
                "0e4559c737914d4f8f941dca9ecfaf6caec6792258302bffec7b8b41dae3c71a",
            ),
            "urllib3-2.8.0.dist-info/METADATA": (
                7389,
                "10898c620e8007c030e07fa5622b68358a43010025dfbd78a1cb797699de2bb4",
            ),
        },
    ),
}


class ProbeError(object_store.ObjectStoreError):
    """The bounded bucket proof is unavailable or incomplete."""


def _refuse() -> ProbeError:
    # Do not copy request URLs, keys, headers or response bodies into diagnostics.
    return ProbeError("object store cell probe refused", operation="cell probe")


def _regular_digest(path: Path, size: int) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise _refuse()
        return hashlib.sha256(stream.read(size + 1)).hexdigest()


def require_sdk() -> None:
    """Refuse an SDK whose transport/parser/model contract was not reviewed."""
    if os.environ.get("AWS_DATA_PATH"):
        raise _refuse()
    set_query_tag("cell-attach-bucket-probe")
    try:
        for name, (version, files) in _SDK_FILES.items():
            dist = metadata.distribution(name)
            if dist.version != version:
                raise _refuse()
            for relative, (size, digest) in files.items():
                if _regular_digest(Path(cast(Path, dist.locate_file(relative))), size) != digest:
                    raise _refuse()
    except (OSError, metadata.PackageNotFoundError, ValueError) as exc:
        raise _refuse() from exc


def _date(value: Any) -> datetime:
    if not isinstance(value, str):
        raise _refuse()
    try:
        at = parsedate_to_datetime(value)
        if at.tzinfo is None or format_datetime(at, usegmt=True) != value:
            raise _refuse()
        return at.astimezone(UTC)
    except (ValueError, TypeError, OverflowError) as exc:
        raise _refuse() from exc


class _Raw:
    """Only bounded reads reach the signed client's original raw response."""

    def __init__(self, raw: Any, cap: int, declared: Any) -> None:
        self.raw, self.cap, self.seen = raw, cap, 0
        self.closed = False
        self.declared: int | None = None
        try:
            if declared is not None:
                if not isinstance(declared, str) or not re.fullmatch(r"[0-9]{1,10}", declared):
                    raise _refuse()
                self.declared = int(declared)
                if self.declared > cap:
                    raise _refuse()
        except BaseException:
            self._close_failed()
            raise

    def _close_failed(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 — retain the original read or validation failure
            pass

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.raw.close()

    def _next(self, amount: int) -> bytes:
        try:
            asked = min(amount, _CHUNK, self.cap - self.seen + 1)
            chunk = self.raw.read(asked)
            if not isinstance(chunk, bytes) or len(chunk) > asked:
                raise _refuse()
            self.seen += len(chunk)
            if self.seen > self.cap or (self.declared is not None and self.seen > self.declared):
                raise _refuse()
            if not chunk:
                if self.declared is not None and self.seen != self.declared:
                    raise _refuse()
                self.close()
            return chunk
        except BaseException:
            self._close_failed()
            raise

    def read(self, amt: int | None = None) -> bytes:
        if amt == 0 or self.closed:
            return b""
        if amt is not None and (type(amt) is not int or amt < 0):
            self._close_failed()
            raise _refuse()
        set_query_tag("cell-attach-bucket-probe")
        chunks: list[bytes] = []
        size = 0
        while amt is None or size < amt:
            chunk = self._next(self.cap + 1 if amt is None else amt - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks)

    def stream(self, amt: int = _CHUNK, decode_content: bool = False) -> Iterator[bytes]:
        if type(amt) is not int or amt <= 0 or decode_content:
            self._close_failed()
            raise _refuse()
        set_query_tag("cell-attach-bucket-probe")
        try:
            while chunk := self.read(min(amt, _CHUNK)):
                yield chunk
        finally:
            self.close()


def _send(client: Any, operation: str, request: Any, **_: Any) -> Any:
    if request.context.get(_SENT):
        raise _refuse()
    request.context[_SENT] = True
    previous = request.stream_output
    try:
        request.stream_output = True
        response = client._endpoint.http_session.send(request)
    finally:
        request.stream_output = previous
    raw = response.raw
    try:
        from botocore.awsrequest import AWSResponse

        if (
            type(response) is not AWSResponse
            or type(response.status_code) is not int
            or response._content is not None
        ):
            raise _refuse()
        cap = _LIST_BYTES if operation == "ListObjectsV2" else _ERROR_BYTES
        if operation == "GetObject" and response.status_code < 300:
            cap = _MARKER_BYTES + 1
        response.raw = _Raw(raw, cap, response.headers.get("content-length"))
        _date(response.headers.get("date"))
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise _refuse()
        if response.status_code >= 300:
            # Refuse before S3's region redirect handler can issue another call.
            _ = response.content
            raise _refuse()
        if operation == "GetObject":
            # Close the connection before parsing even malformed GET headers.
            value = response.raw.read()
            response.raw = _Raw(io.BytesIO(value), cap, str(len(value)))
        return response
    except BaseException:
        try:
            response.raw.close()
        except Exception:  # noqa: BLE001 — retain the original bounded transport failure
            pass
        raise


def _no_retry(
    *, response: Any, attempts: int, caught_exception: BaseException | None, **_: Any
) -> None:
    # Exact-operation events precede S3's wildcard region/HeadBucket handler.
    if caught_exception is not None:
        raise caught_exception
    if (
        attempts != 1
        or response is None
        or not isinstance(response[1], dict)
        or "Error" in response[1]
    ):
        raise _refuse()


def _ordinary_signing(*, request: Any, signing_name: str, **_: Any) -> None:
    # Express auth would create a session through another S3 operation.
    effective = request.context.get("signing", {}).get("signing_name", signing_name)
    if effective != "s3":
        raise _refuse()


def install(client: Any) -> None:
    """Install only these four operations on the fixed diagnostic client."""
    from functools import partial

    model = client.meta.service_model
    description = json.dumps(model._service_description, sort_keys=True, separators=(",", ":"))
    if (
        model.service_name != "s3"
        or hashlib.sha256(description.encode()).hexdigest() != _MODEL_SHA256
    ):
        raise _refuse()
    set_query_tag("cell-attach-bucket-probe")
    for operation in ("ListObjectsV2", "GetObject", "PutObject", "DeleteObject"):
        client.meta.events.register_first(
            f"before-sign.s3.{operation}",
            _ordinary_signing,
            unique_id=f"loom-cell-probe-signing-{operation}",
        )
        client.meta.events.register_first(
            f"before-send.s3.{operation}",
            partial(_send, client, operation),
            unique_id=f"loom-cell-probe-{operation}",
        )
        client.meta.events.register_first(
            f"needs-retry.s3.{operation}",
            _no_retry,
            unique_id=f"loom-cell-probe-no-retry-{operation}",
        )


def _client() -> Any:
    return object_store._client_for(
        settings.document_s3_endpoint_url,
        settings.document_s3_region,
        settings.document_s3_connect_timeout_s,
        settings.document_s3_read_timeout_s,
        diagnostic=True,
    )


def _ack(response: Any, status: int) -> datetime:
    if not isinstance(response, dict):
        raise _refuse()
    meta = response.get("ResponseMetadata")
    if not isinstance(meta, dict) or type(meta.get("HTTPStatusCode")) is not int:
        raise _refuse()
    headers = meta.get("HTTPHeaders")
    if meta["HTTPStatusCode"] != status or not isinstance(headers, dict):
        raise _refuse()
    return _date(headers.get("date"))


def _etag(value: Any) -> str:
    if not isinstance(value, str) or value in ("*", '"*"') or not _ETAG.fullmatch(value):
        raise _refuse()
    return value


def _location() -> object_store.Location:
    name = settings.cell
    if not settings.document_s3_bucket or not isinstance(name, str) or not NAME.fullmatch(name):
        raise _refuse()
    key = f"{object_store.root_prefix()}_cell_attach_probe/{name}"
    if len(key.encode("utf-8")) > object_store.MAX_KEY:
        raise _refuse()
    return object_store.Location(settings.document_s3_bucket, key)


def _value(value: bytes) -> None:
    if not isinstance(value, bytes) or not 1 <= len(value) <= _MARKER_BYTES:
        raise _refuse()


@dataclass(frozen=True)
class Marker:
    value: bytes
    etag: str
    at: datetime


def _read(client: Any, loc: object_store.Location) -> Marker:
    response = client.get_object(Bucket=loc.bucket, Key=loc.key)
    body = response.get("Body")
    if body is None:
        raise _refuse()
    try:
        at = _ack(response, 200)
        etag = _etag(response.get("ETag"))
        value = body.read()
        _value(value)
        marker = Marker(value, etag, at)
    except BaseException:
        try:
            body.close()
        except Exception:  # noqa: BLE001 — retain the original marker read failure
            pass
        raise
    else:
        body.close()
        return marker


async def read_marker() -> Marker:
    loc = _location()
    async with object_store.transfer() as lease:
        return await object_store._thread(lambda: _read(_client(), loc), lease)


async def put_marker(value: bytes) -> datetime:
    _value(value)
    loc = _location()

    def put() -> datetime:
        response = _client().put_object(Bucket=loc.bucket, Key=loc.key, Body=value)
        at = _ack(response, 200)
        _etag(response.get("ETag"))
        return at

    async with object_store.transfer() as lease:
        return await object_store._thread(put, lease)


async def delete_marker(expected: bytes) -> None:
    """Delete only the exact bytes read with the ETag sent in IfMatch."""
    _value(expected)
    loc = _location()

    def delete() -> None:
        client = _client()
        marker = _read(client, loc)
        if marker.value != expected:
            raise _refuse()
        response = client.delete_object(Bucket=loc.bucket, Key=loc.key, IfMatch=marker.etag)
        _ack(response, 204)
        if response.get("DeleteMarker", False) is not False:
            raise _refuse()

    async with object_store.transfer() as lease:
        await object_store._thread(delete, lease)


def _page(response: Any, prefix: str, after: str, maximum: int) -> object_store.Page:
    at = _ack(response, 200)
    if (
        response.get("Name") != settings.document_s3_bucket
        or response.get("Prefix") != prefix
        or type(response.get("MaxKeys")) is not int
        or response["MaxKeys"] != maximum
        or type(response.get("IsTruncated")) is not bool
        or type(response.get("KeyCount")) is not int
        or (after and response.get("StartAfter") != after)
    ):
        raise _refuse()
    entries = response.get("Contents", [])
    if (
        not isinstance(entries, list)
        or len(entries) > maximum
        or response["KeyCount"] != len(entries)
    ):
        raise _refuse()
    listed: list[object_store.Listed] = []
    previous = after
    set_query_tag("cell-attach-bucket-probe")
    for entry in entries:
        if not isinstance(entry, dict):
            raise _refuse()
        key, modified = entry.get("Key"), entry.get("LastModified")
        if (
            not isinstance(key, str)
            or not key.startswith(prefix)
            or key <= previous
            or not 1 <= len(key.encode("utf-8")) <= 1024
            or not isinstance(modified, datetime)
            or modified.tzinfo is None
        ):
            raise _refuse()
        listed.append(object_store.Listed(key, modified.astimezone(UTC)))
        previous = key
    if response["IsTruncated"] and not listed:
        raise _refuse()
    return object_store.Page(listed, response["IsTruncated"], at)


async def list_page(*, start_after: str = "", max_keys: int = 1000) -> object_store.Page:
    """One strictly ordered page of this deployment's configured root only."""
    _location()
    prefix = object_store.root_prefix()
    if (
        type(max_keys) is not int
        or not 1 <= max_keys <= 1000
        or not isinstance(start_after, str)
        or len(start_after.encode("utf-8")) > 1024
        or (start_after and not start_after.startswith(prefix))
    ):
        raise _refuse()
    args = {"Bucket": settings.document_s3_bucket, "Prefix": prefix, "MaxKeys": max_keys}
    if start_after:
        args["StartAfter"] = start_after
    slots = object_store._semaphore(object_store._LISTINGS, object_store.LISTING_SLOTS)
    await slots.acquire()
    lease = object_store.Lease(slots)
    try:
        return await object_store._thread(
            lambda: _page(_client().list_objects_v2(**args), prefix, start_after, max_keys), lease
        )
    finally:
        lease.close()


async def check_prefix() -> None:
    """Exhaust the configured root and refuse another cell's fixed marker."""
    own = _location()
    name = own.key.rsplit("/", 1)[1]
    after = ""
    objects = key_bytes = 0
    set_query_tag("cell-attach-bucket-probe")
    for _ in range(100):
        page = await list_page(start_after=after, max_keys=1000)
        previous = after
        for item in page.objects:
            size = len(item.key.encode("utf-8"))
            if item.key <= after or objects >= 100000 or key_bytes + size > 16 * 1024 * 1024:
                raise _refuse()
            objects += 1
            key_bytes += size
            after = item.key
            _, reserved, holder = item.key.rpartition("/_cell_attach_probe/")
            if reserved and NAME.fullmatch(holder) and holder != name:
                raise _refuse()
        if not page.more:
            return
        if after == previous or objects == 100000:
            raise _refuse()
    raise _refuse()
