"""Calls to the extractor services that turn an original into an artifact. The
server parses no original itself: each format goes to the service
LOOM_DOCUMENT_EXTRACTORS_JSON routes it to, else LOOM_DOCUMENT_EXTRACTOR_URL,
and is extracted only if that service's capabilities declare it. There is no
in-process fallback: a format with no service that declares it raises
ExtractorUnavailable.

What a service returns is untrusted: a call is bounded in time as a whole,
its response in size while it is read, and `parse_artifact` checks the
artifact before anything uses it. The wire contract is
docs/reference/extractor-contract.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from server.core.document_formats import format_of_media_type, media_type_of
from server.knowledge.domain.extract import (
    KNOWN_FORMATS,
    SCHEMA_VERSION,
    Artifact,
    InvalidArtifact,
    format_of,
    parse_artifact,
)
from server.settings import settings

logger = logging.getLogger("loom.knowledge.extractors")

# How long a service's declared formats are trusted before they are read again.
_CAPABILITIES_TTL_S = 60.0  # not a setting: a redeployed extractor is seen within it
_CAPABILITIES_MAX_BYTES = 64 * 1024  # not a setting: a declaration is a short list
_ERROR_MAX_BYTES = 64 * 1024  # not a setting: a refusal is a code and a short message
_CODE = re.compile(r"[a-z_]{1,40}")
_MESSAGE_CHARS = 500  # not a setting: the most of a refusal's message kept
# A service's own refusals that say it cannot take the format now, not that the
# source is bad: the source can be tried again.
_UNAVAILABLE_CODES = frozenset({"format_unavailable"})
# The 4xx answers that say nothing about the source: a refused credential (also a
# proxy's), a request timed out on the way, too many requests.
_UNAVAILABLE_4XX = frozenset({401, 403, 407, 408, 429})


class ExtractorUnavailable(Exception):
    """No extractor can take this source now: none is configured for its format,
    none declares it, or the service is unreachable, slow past the timeout,
    refuses the server's token, answers out of contract or fails. Retrying
    later may succeed."""


class FormatUnavailable(ExtractorUnavailable):
    """No configured extractor takes this format: none is routed for it, the one
    it is routed to does not declare it, or that one answers `format_unavailable`.
    A deployment's configuration, not an outage: a caller that retries only
    outages (an ingestion job) fails the source with `format_unavailable` instead.
    Still an ExtractorUnavailable, as adding an extractor makes it extractable."""


class ExtractionFailed(Exception):
    """This source cannot be extracted. `code` is the contract's (e.g.
    `format_mismatch`, `encoding`, `corrupt`, `too_large`, `timeout`,
    `resource_limit`, `unsupported_format`, `invalid_option`), or
    `invalid_artifact` for a response that breaks the contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Capabilities:
    name: str
    formats: frozenset[str]
    max_bytes: int


_declared: dict[str, tuple[float, Capabilities]] = {}


def endpoint_for(fmt: str, media_type: str = "") -> str:
    """The base URL of the service `fmt` goes to, or '' when none is set. A
    media type's route applies only to a source of that media type's format."""
    routes = settings.document_extractor_routes()
    media = media_type_of(media_type)
    by_media = routes.get(media) if format_of_media_type(media) == fmt else None
    return (routes.get(fmt) or by_media or settings.document_extractor_url).rstrip("/")


def _token(url: str) -> str:
    return settings.document_extractor_token_map().get(url) or settings.document_extractor_token


# The transport calls go through; None is the network. Tests set an in-process one.
transport: httpx.AsyncBaseTransport | None = None


def _client(url: str) -> httpx.AsyncClient:
    token = _token(url)
    return httpx.AsyncClient(
        timeout=settings.document_extractor_timeout_s,
        # Uncompressed: the size bounds count the bytes the server holds.
        headers={"Accept-Encoding": "identity"}
        | ({"Authorization": f"Bearer {token}"} if token else {}),
        transport=transport,
    )


def _uncompressed(response: httpx.Response) -> None:
    """A compressed answer (a compressing proxy in front of the service, say) is
    out of contract whatever its status: the service is unavailable, and no
    source is refused for it."""
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("", "identity"):
        raise ExtractorUnavailable(f"the extractor answered with {encoding[:20]!r} encoding")


class _TooLarge(Exception):
    pass


async def _read(response: httpx.Response, cap: int) -> bytes:
    out = bytearray()
    # Raw bytes: the bound counts what was received.
    async for chunk in response.aiter_raw():
        out += chunk
        if len(out) > cap:
            raise _TooLarge(f"the extractor's response is over {cap} bytes")
    return bytes(out)


def _capabilities(data: bytes) -> Capabilities:
    d = json.loads(data)
    version = d.get("contract_version") if isinstance(d, dict) else None
    if type(version) is not int or version != SCHEMA_VERSION:  # not `true`, not `1.0`
        raise ValueError(f"contract_version is not {SCHEMA_VERSION}")
    formats = d.get("formats")
    if not isinstance(formats, list):
        raise ValueError("formats is not a list")
    max_bytes = d.get("max_bytes")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise ValueError("max_bytes is not a positive integer")
    extractor = d.get("extractor")
    name = extractor.get("name") if isinstance(extractor, dict) else None
    return Capabilities(
        name=name[:100] if isinstance(name, str) else "",
        formats=frozenset(
            f["format"] for f in formats if isinstance(f, dict) and f.get("format") in KNOWN_FORMATS
        ),
        max_bytes=max_bytes,
    )


async def capabilities(url: str) -> Capabilities:
    """What the service at `url` declares, read at most once a minute."""
    hit = _declared.get(url)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]
    try:
        async with (
            asyncio.timeout(settings.document_extractor_timeout_s),
            _client(url) as client,
            client.stream("GET", f"{url}/v1/capabilities") as r,
        ):
            _uncompressed(r)
            if r.status_code != 200:
                raise ExtractorUnavailable(f"the extractor's capabilities answered {r.status_code}")
            caps = _capabilities(await _read(r, _CAPABILITIES_MAX_BYTES))
    except TimeoutError as e:
        raise ExtractorUnavailable("the extractor's capabilities took past the timeout") from e
    except httpx.HTTPError as e:
        raise ExtractorUnavailable(f"the extractor is unreachable: {type(e).__name__}") from e
    # ValueError: also a body that is not JSON; RecursionError: one nested too deep.
    except (ValueError, RecursionError, _TooLarge) as e:
        raise ExtractorUnavailable(
            f"the extractor's capabilities are not the contract's: {type(e).__name__}"
        ) from e
    _declared[url] = (time.monotonic() + _CAPABILITIES_TTL_S, caps)
    return caps


def _endpoints(fmt: str) -> list[str]:
    """Every service a source of `fmt` can go to: its format's own route alone
    when it has one, else the default URL and the route of each media type
    that names `fmt`."""
    routes = settings.document_extractor_routes()
    if fmt in routes:  # a format's own route hides its media types', as in endpoint_for
        return [routes[fmt].rstrip("/")]
    by_media = [url for key, url in routes.items() if format_of_media_type(key) == fmt]
    return list(dict.fromkeys(u.rstrip("/") for u in [endpoint_for(fmt), *by_media] if u))


def _origin(url: str) -> str:
    """`scheme://host:port` of a URL: a URL setting is a secret, as its user
    info, path or query can carry a credential."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"


async def available_formats() -> dict[str, str]:
    """Each format some configured service declares, with the first such
    service's URL. A service that cannot be read declares nothing, and is read
    once per listing, so it costs one timeout, not one per format."""
    out: dict[str, str] = {}
    read: dict[str, Capabilities | None] = {}
    for fmt in KNOWN_FORMATS:
        for url in _endpoints(fmt):
            if url not in read:
                try:
                    read[url] = await capabilities(url)
                except ExtractorUnavailable as e:
                    logger.warning("document extractor %s: %s", _origin(url), e)
                    read[url] = None
            caps = read[url]
            if caps is not None and fmt in caps.formats:
                out[fmt] = url
                break
    return out


def _refusal(status: int, data: bytes) -> ExtractionFailed | ExtractorUnavailable:
    try:
        err = json.loads(data)["error"]
        code, message = err["code"], err["message"]
    except (ValueError, RecursionError, KeyError, TypeError):
        code, message = "", ""
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        code = "refused"
    if not isinstance(message, str):
        message = ""
    message = message[:_MESSAGE_CHARS] or f"the extractor answered {status}"
    if code in _UNAVAILABLE_CODES:
        return FormatUnavailable(message)
    return ExtractionFailed(code, message)


async def extract(
    data: bytes,
    *,
    name: str,
    media_type: str = "",
    declared_format: str = "",
    options: Mapping[str, str] | None = None,
) -> Artifact:
    """The artifact of one original, extracted by the service its format goes
    to and checked. ExtractionFailed: the source cannot be extracted (refused by
    name here or by the service, or answered with an artifact out of contract).
    ExtractorUnavailable: no service could take it now."""
    try:
        fmt = format_of(name, media_type, declared_format)
    except ValueError as e:
        raise ExtractionFailed("unsupported_format", str(e)) from e
    url = endpoint_for(fmt, media_type)
    if not url:
        raise FormatUnavailable(f"no extractor is configured for {fmt}")
    caps = await capabilities(url)
    if fmt not in caps.formats:
        raise FormatUnavailable(f"the extractor for {fmt} does not declare it")
    if len(data) > caps.max_bytes:
        raise ExtractionFailed("too_large", f"the source is over the extractor's {caps.max_bytes}")
    params = {**(options or {}), "format": fmt, "name": name}
    cap = settings.document_extractor_max_artifact_bytes
    try:
        async with (
            asyncio.timeout(settings.document_extractor_timeout_s),
            _client(url) as client,
            client.stream(
                "POST",
                f"{url}/v1/extract",
                params=params,
                content=data,
                headers={"Content-Type": "application/octet-stream"},
            ) as r,
        ):
            _uncompressed(r)
            # The status first: only an artifact is read up to the artifact bound, and a
            # proxy's long error page is not mistaken for one.
            if 400 <= r.status_code < 500 and r.status_code not in _UNAVAILABLE_4XX:
                try:
                    refusal = await _read(r, _ERROR_MAX_BYTES)
                except _TooLarge:
                    refusal = b""
                raise _refusal(r.status_code, refusal)
            if r.status_code != 200:  # a redirect, another 2xx, a refused token, a 5xx
                raise ExtractorUnavailable(f"the extractor answered {r.status_code}")
            body = await _read(r, cap)
    except TimeoutError as e:
        raise ExtractorUnavailable("the extraction took past the timeout") from e
    except httpx.HTTPError as e:
        raise ExtractorUnavailable(f"the extractor is unreachable: {type(e).__name__}") from e
    except _TooLarge as e:  # the artifact, not the original: the service's own code for it
        raise ExtractionFailed("resource_limit", str(e)) from e
    try:
        # Off the event loop: decoding and checking up to the artifact bound takes a while.
        return await asyncio.to_thread(parse_artifact, body, fmt=fmt, max_bytes=cap)
    except InvalidArtifact as e:
        raise ExtractionFailed("invalid_artifact", str(e)) from e
