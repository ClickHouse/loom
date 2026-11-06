"""Document originals in an S3-compatible bucket (`document_s3_bucket`).

The bucket client the knowledge store writes through and erasure deletes and
counts through.

Each write of an original is its own immutable object,
`<prefix>/<org>/<namespace>/<sha256>/<write id>`, and the catalog row in
`loom.document_blobs` names it in `location` (`s3://<bucket>/<key>`); a sweep
deleting one write's object never deletes another's. The org and namespace
segments are percent-encoded, so a `/` in a name cannot place an object under
another namespace's prefix.

The bucket is written with the store's own SHA-256 check, so bytes that do not
hash to the sha in their key are refused by the store, not kept.

boto3 is synchronous: every call runs in a worker thread. Credentials come from
boto3's chain (environment, profile, instance role); none is a Loom setting.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import importlib.util
import logging
import re
import threading
import uuid
import weakref
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Collection
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, unquote

from server.core.ch import acquire_slot
from server.core.errors import BackendUnavailableError, ValidationError
from server.settings import settings

log = logging.getLogger(__name__)

SCHEME = "s3://"
# The error codes a store answers for an object that is not there.
_MISSING = ("404", "NoSuchKey", "NotFound")
_SHA = re.compile(r"[0-9a-f]{64}")
# Bytes per read of a streamed download.
_STREAM_CHUNK = 1024 * 1024
# The longest key written: the bucket sweep keeps the last key it read as its
# cursor, and a worker's cursor holds at most this many characters.
MAX_KEY = 512  # not a setting: the worker cursor's limit
# Keys per DeleteObjects request.
_DELETE_BATCH = 1000  # not a setting: the S3 API's limit


class ObjectStoreError(BackendUnavailableError):
    """The bucket refused or failed a request; the caller fails closed. The
    message names the operation and the store's error code, never the bucket
    or a key (the log line has them)."""


def _failed(what: str, where: str, e: Exception) -> ObjectStoreError:
    log.warning("object store: %s %s failed: %s", what, where, e)
    return ObjectStoreError(
        f"object store {what} failed ({_code(e) or type(e).__name__})", operation=what
    )


@dataclass(frozen=True)
class Location:
    bucket: str
    key: str

    def uri(self) -> str:
        return f"{SCHEME}{self.bucket}/{self.key}"

    @classmethod
    def parse(cls, uri: str) -> Location:
        if not uri.startswith(SCHEME) or "/" not in uri[len(SCHEME) :]:
            raise ValueError(f"not an object location: {uri!r}")
        bucket, key = uri[len(SCHEME) :].split("/", 1)
        return cls(bucket, key)


def root_prefix() -> str:
    return f"{settings.document_s3_prefix}/"


def namespace_prefix(org: str, namespace: str) -> str:
    """The key prefix every object of the namespace sits under, ending in '/'."""
    return f"{root_prefix()}{quote(org, safe='')}/{quote(namespace, safe='')}/"


def scope_of(key: str) -> tuple[str, str, str] | None:
    """The (org, namespace, sha256) of a key under `root_prefix()`, or None for
    a key that is not an original's."""
    root = root_prefix()
    if not key.startswith(root):
        return None
    parts = key[len(root) :].split("/")
    if len(parts) != 4 or not all(parts) or not _SHA.fullmatch(parts[2]):
        return None
    return unquote(parts[0]), unquote(parts[1]), parts[2]


def in_namespaces(key: str, org: str, namespaces: Collection[str]) -> bool:
    """Whether `key` is an original of `org` in one of `namespaces`. A key under a
    namespace's prefix may be another scope's after a prefix change: with the
    prefix moved up from `loom/originals` to `loom`, the old key
    `loom/originals/acme/docs/<sha>/<id>` sits under the prefix of org `originals`,
    namespace `acme`."""
    scope = scope_of(key)
    return scope is not None and scope[0] == org and scope[1] in namespaces


def original_prefix(org: str, namespace: str, sha: str) -> str:
    """The prefix holding every write of one original: `<namespace prefix><sha>/`."""
    return f"{namespace_prefix(org, namespace)}{sha}/"


def check_key_fits(org: str, namespace: str) -> None:
    """Refuse an org and namespace whose originals' keys would pass MAX_KEY: a key's
    sha and write id have fixed lengths, so the names alone decide."""
    length = len(original_prefix(org, namespace, "0" * 64)) + len(uuid.uuid4().hex)
    if length > MAX_KEY:
        raise ValidationError(
            f"the org and namespace names are too long for an object key ({length} > {MAX_KEY})"
        )


def new_location(org: str, namespace: str, sha: str) -> Location:
    check_key_fits(org, namespace)
    return Location(
        settings.document_s3_bucket, f"{original_prefix(org, namespace, sha)}{uuid.uuid4().hex}"
    )


_CLIENT_LOCK = threading.Lock()
# Bucket calls run on threads of their own, so a stalled bucket holds these and
# not the threads every other `asyncio.to_thread` caller shares.
_THREADS = 16  # not a setting: a bound on blocked threads, not a throughput knob
_EXECUTOR = ThreadPoolExecutor(max_workers=_THREADS, thread_name_prefix="loom-object-store")
# The bucket threads are split into slots per event loop, so no class of call takes
# a thread another was promised; every slot is held until the threads it started are
# done (`Lease`).
# - Streams: a fixed share, so a burst of large downloads holds a bounded number of
#   connections and threads.
STREAM_SLOTS = 4  # not a setting: a share of the thread bound
_STREAMS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)
# - Listings and deletes (erasure and its census, the sweep): a fixed share. Background
#   work waits for one rather than failing.
LISTING_SLOTS = 4  # not a setting: a share of the thread bound
_LISTINGS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)
# - Whole-object transfers (an upload, a download up to its 4 MB buffer): the rest, so
#   a stalled bucket answers a burst of them 503 while erasure and the sweep still run.
_TRANSFER_SLOTS = _THREADS - STREAM_SLOTS - LISTING_SLOTS  # not a setting: the rest
_TRANSFERS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


@functools.lru_cache(maxsize=4)
def _client_for(endpoint: str, region: str, connect_s: float, read_s: float) -> Any:
    """A client for the endpoint, region and timeouts, cached. Each build makes its own
    session; the lock serialises building the session and the client, as botocore
    loads its data files and credential chain then and is not reliably safe to do
    so in two threads at once. The cache does not hold the lock, so two first
    calls may each build a client and one is kept."""
    import boto3

    config = _client_config(connect_s, read_s)
    with _CLIENT_LOCK:
        session = boto3.session.Session()
        return session.client(
            "s3", endpoint_url=endpoint or None, region_name=region or None, config=config
        )


def _client_config(connect_s: float, read_s: float) -> Any:
    from botocore.config import Config

    return Config(
        connect_timeout=connect_s,
        read_timeout=read_s,
        retries={"max_attempts": 3, "mode": "standard"},
        max_pool_connections=_THREADS,  # one connection per bucket thread
        # Checksums only where a request needs one (`put` sends its SHA-256 itself):
        # boto3's default of one on every request is refused by some S3-compatible stores.
        # botocore takes these from 1.36.
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )


def _client() -> Any:
    return _client_for(
        settings.document_s3_endpoint_url,
        settings.document_s3_region,
        settings.document_s3_connect_timeout_s,
        settings.document_s3_read_timeout_s,
    )


class Lease:
    """A slot held for a block and for every bucket thread the block started: a
    cancelled caller cannot stop a thread, so its slot comes back only when the
    block has ended and each of its threads is done."""

    def __init__(self, slots: asyncio.Semaphore) -> None:
        self._slots = slots
        self._jobs = 0  # threads started under it and not yet done
        self._open = True  # its block has not ended
        self._released = False

    def hold(self, job: Future[Any]) -> None:
        loop = asyncio.get_running_loop()
        self._jobs += 1

        def done(_: Future[Any]) -> None:
            try:
                loop.call_soon_threadsafe(self._job_done)
            except RuntimeError:  # a thread that outlived a shut-down server's loop
                pass

        job.add_done_callback(done)

    def _job_done(self) -> None:
        self._jobs -= 1
        self._release_when_idle()

    def close(self) -> None:
        self._open = False
        self._release_when_idle()

    def _release_when_idle(self) -> None:
        if not self._open and self._jobs == 0 and not self._released:
            self._released = True
            self._slots.release()


def _submit[T](fn: Callable[[], T], lease: Lease) -> Future[T]:
    job = _EXECUTOR.submit(fn)
    lease.hold(job)
    return job


async def _thread[T](fn: Callable[[], T], lease: Lease) -> T:
    return await asyncio.wrap_future(_submit(fn, lease))


async def _s3(method: str, *, lease: Lease, **kwargs: Any) -> Any:
    """One S3 call on a bucket thread; the client is built there too, since its
    first build imports boto3 and walks the credential chain."""
    return await _thread(lambda: getattr(_client(), method)(**kwargs), lease)


def _store_errors() -> tuple[type[Exception], ...]:
    """What a boto3 call raises for a refused or failed request (botocore comes
    with boto3, which `_client` has imported by then)."""
    from botocore.exceptions import BotoCoreError, ClientError

    return (BotoCoreError, ClientError)


def _code(e: Exception) -> str:
    response = getattr(e, "response", None) or {}
    return str(response.get("Error", {}).get("Code", ""))


async def put(loc: Location, data: bytes, sha: str, lease: Lease) -> None:
    """Write `data` at `loc`, checked by the store against `sha`, under `lease`:
    the transfer slot (`transfer`) the caller holds around this object's write."""
    checksum = base64.b64encode(bytes.fromhex(sha)).decode()
    try:
        await _s3(
            "put_object",
            lease=lease,
            Bucket=loc.bucket,
            Key=loc.key,
            Body=data,
            ChecksumSHA256=checksum,
        )
    except _store_errors() as e:
        raise _failed("write", loc.uri(), e) from e


def require_client() -> None:
    """Refuse to serve with a bucket configured and no boto3 installed, or one too
    old for the client's options: a write would fail only when it reached the
    bucket. Checked at server startup, not on every settings load, so the CLI alone
    runs without the `aws` extra."""
    if not settings.document_s3_bucket:
        return
    if importlib.util.find_spec("boto3") is None:
        raise RuntimeError("LOOM_DOCUMENT_S3_BUCKET needs boto3 (the `aws` extra)")
    try:
        _client_config(settings.document_s3_connect_timeout_s, settings.document_s3_read_timeout_s)
    except TypeError as e:  # an option this botocore does not know
        raise RuntimeError("LOOM_DOCUMENT_S3_BUCKET needs boto3 1.36 or later") from e


@asynccontextmanager
async def transfer() -> AsyncIterator[Lease]:
    """One of the loop's transfer slots, held for the block and for the bucket
    threads started under it, or 503 OVERLOADED after `ch_pool_wait_ms`."""
    lease = Lease(
        await _acquire_or_overload(_TRANSFERS, _TRANSFER_SLOTS, "document bucket transfer")
    )
    try:
        yield lease
    finally:
        lease.close()


def _semaphore(
    registry: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore], size: int
) -> asyncio.Semaphore:
    """The running loop's semaphore in `registry`."""
    loop = asyncio.get_running_loop()
    slots = registry.get(loop)
    if slots is None:
        slots = registry[loop] = asyncio.Semaphore(size)
    return slots


async def _acquire_or_overload(
    registry: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore],
    size: int,
    what: str,
) -> asyncio.Semaphore:
    """One of the loop's slots in `registry`, held until the caller releases it."""
    slots = _semaphore(registry, size)
    await acquire_slot(slots, what)
    return slots


async def _listing_or_delete(method: str, **kwargs: Any) -> Any:
    """One S3 call under one of the loop's listing and delete slots, waited for."""
    slots = _semaphore(_LISTINGS, LISTING_SLOTS)
    await slots.acquire()
    lease = Lease(slots)
    try:
        return await _s3(method, lease=lease, **kwargs)
    finally:
        lease.close()


async def read(loc: Location) -> bytes | None:
    """The object's bytes, or None when it is gone, under one of the loop's
    transfer slots: a stalled bucket answers a burst of downloads 503 OVERLOADED after
    `ch_pool_wait_ms`, as ClickHouse's own reads do, rather than queueing them
    on the bucket threads."""

    def fetch() -> bytes:
        # One thread from request to close: a cancelled caller leaves no second
        # thread closing a body the first is still reading.
        body = _client().get_object(Bucket=loc.bucket, Key=loc.key)["Body"]
        try:
            return bytes(body.read())
        finally:
            body.close()  # a failed read still gives its connection back

    async with transfer() as lease:
        try:
            return await _thread(fetch, lease)
        except _store_errors() as e:
            if _code(e) in _MISSING:
                return None
            raise _failed("read", loc.uri(), e) from e


async def stream(loc: Location) -> AsyncGenerator[bytes, None]:
    """The object's bytes as chunks, under one of the stream slots; none when it
    is gone. A store that fails partway raises, as a failed read does."""
    lease = Lease(await _acquire_or_overload(_STREAMS, STREAM_SLOTS, "document bucket stream"))
    job: Future[Any] = _submit(
        lambda: _client().get_object(Bucket=loc.bucket, Key=loc.key)["Body"], lease
    )
    body: Any = None
    try:
        try:
            body = await asyncio.wrap_future(job)
        except _store_errors() as e:
            if _code(e) in _MISSING:
                return
            raise _failed("read", loc.uri(), e) from e
        try:
            while True:
                job = _submit(lambda: body.read(_STREAM_CHUNK), lease)
                chunk = await asyncio.wrap_future(job)
                if not chunk:
                    break
                yield bytes(chunk)
        except _store_errors() as e:
            raise _failed("read", loc.uri(), e) from e
    finally:
        # Shielded: a server cancels a gone client's response more than once, and a
        # second cancel must stop only this wait, never the close or the release.
        cleanup = asyncio.ensure_future(_close_after(job, body, lease))
        cleanup.add_done_callback(_retrieve)
        await asyncio.shield(cleanup)


def _retrieve(task: asyncio.Future[Any]) -> None:
    if not task.cancelled() and (e := task.exception()) is not None:
        log.warning("object store: closing a stream failed: %s", e)


async def _close_after(job: Future[Any], body: Any, lease: Lease) -> None:
    """Close the stream's body (None while `job` is its request) once `job`,
    the stream's last call, is done, then give the slot back: a consumer that
    stopped mid-call waits for it rather than closing the body under it, and a
    request it left running still gives its connection back."""
    try:
        if not job.done():
            waited = asyncio.wrap_future(job)
            await asyncio.wait({waited})
            if not waited.cancelled():
                waited.exception()  # read, so a failure no consumer awaits is not logged as lost
        if body is None:
            if job.cancelled() or job.exception() is not None:
                return
            body = job.result()
        await _thread(body.close, lease)
    finally:
        lease.close()


@dataclass(frozen=True)
class Listed:
    key: str
    modified: datetime  # UTC, as the store reports it


@dataclass(frozen=True)
class Page:
    objects: list[Listed]
    more: bool  # more keys follow
    at: datetime  # the store's clock when it answered (its `Date` header): see `_store_time`


async def list_page(
    bucket: str, prefix: str, *, start_after: str = "", max_keys: int = 1000
) -> Page:
    """Up to `max_keys` objects under `prefix` after the key `start_after`, in
    key order, whether more follow, and the store's time: object ages are read
    against the clock that stamped them, not this host's."""
    args: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": max_keys}
    if start_after:
        args["StartAfter"] = start_after
    try:
        page = await _listing_or_delete("list_objects_v2", **args)
    except _store_errors() as e:
        raise _failed("list", f"s3://{bucket}/{prefix}", e) from e
    listed = [Listed(str(o["Key"]), o["LastModified"]) for o in page.get("Contents", [])]
    return Page(listed, bool(page.get("IsTruncated")), _store_time(page))


# A store that sends no `Date`: this host's clock, less the most two clocks can
# differ while S3 signatures still pass (SigV4 refuses past 15 minutes), so an
# object can only look younger than it is.
_SIGNATURE_SKEW_S = 900  # not a setting: the signature protocol's own bound


def _store_time(response: dict[str, Any]) -> datetime:
    date = response.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("date", "")
    try:
        return parsedate_to_datetime(date).astimezone(UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC) - timedelta(seconds=_SIGNATURE_SKEW_S)


async def _pages(bucket: str, prefix: str) -> AsyncGenerator[list[str], None]:
    """The keys under `prefix`, a listing page at a time. A page's keys may be
    deleted before the next is read: the listing resumes after the last key."""
    after = ""
    while True:
        listing = await list_page(bucket, prefix, start_after=after)
        if listing.objects:
            yield [o.key for o in listing.objects]
        if not listing.more or not listing.objects:
            return
        after = listing.objects[-1].key


async def keys_under(bucket: str, prefix: str) -> list[str]:
    """Every key under `prefix`: for a prefix known to hold few."""
    return [k async for keys in _pages(bucket, prefix) for k in keys]


async def count_under(bucket: str, prefix: str) -> int:
    """How many objects are under `prefix`."""
    return sum([len(keys) async for keys in _pages(bucket, prefix)])


async def delete(bucket: str, keys: list[str]) -> None:
    """Delete `keys`; a key already gone is not an error, any other refusal is."""
    for i in range(0, len(keys), _DELETE_BATCH):
        batch = keys[i : i + _DELETE_BATCH]
        try:
            response = await _listing_or_delete(
                "delete_objects",
                Bucket=bucket,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
            )
        except _store_errors() as e:
            raise _failed("delete", f"s3://{bucket}", e) from e
        errors = [x for x in response.get("Errors", []) if x.get("Code") != "NoSuchKey"]
        if errors:
            log.warning(
                "object store: s3://%s kept %d object(s): %s", bucket, len(errors), errors[0]
            )
            raise ObjectStoreError(
                f"object store delete kept {len(errors)} object(s) ({errors[0].get('Code', '')})",
                operation="delete",
            )


async def delete_under(bucket: str, prefixes: list[str], owned: Callable[[str], bool]) -> None:
    """Delete every object under each prefix that `owned` accepts, a batch of keys
    at a time."""
    held: list[str] = []
    for prefix in prefixes:
        async for keys in _pages(bucket, prefix):
            held.extend(k for k in keys if owned(k))
            while len(held) >= _DELETE_BATCH:
                await delete(bucket, held[:_DELETE_BATCH])
                held = held[_DELETE_BATCH:]
    await delete(bucket, held)
