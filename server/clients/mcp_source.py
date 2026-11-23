"""Bounded reads from an explicitly configured remote MCP source.

The caller owns connection authorization and secret storage. No remote hint grants
permission to call a tool, import a document, or share its contents.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, ExitStack
from dataclasses import asdict, dataclass, field, replace
from datetime import timedelta
from typing import Any, Literal, TypeVar
from urllib.parse import urlsplit

import anyio
import httpx
from jsonschema import Draft202012Validator, SchemaError
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl
from referencing import Registry
from referencing.exceptions import Unresolvable

from server.core.ch import set_query_tag
from server.core.diagnostics import private_work
from server.core.webhooks import pinned_target

ErrorCode = Literal[
    "config", "unsupported", "denied", "protocol", "schema", "limit", "timeout", "closed"
]


class MCPSourceError(Exception):
    """Only a fixed code may cross the client boundary."""

    def __init__(self, code: ErrorCode) -> None:
        if code not in {
            "config",
            "unsupported",
            "denied",
            "protocol",
            "schema",
            "limit",
            "timeout",
            "closed",
        }:
            code = "protocol"
        self.code = code
        super().__init__("MCP source " + code)


def _error_code(error: BaseException) -> ErrorCode:
    if isinstance(error, MCPSourceError):
        return error.code
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(error, McpError) and error.error.code == httpx.codes.REQUEST_TIMEOUT:
        return "timeout"
    if isinstance(error, BaseExceptionGroup):
        codes = {_error_code(child) for child in error.exceptions}
        priorities: tuple[ErrorCode, ...] = (
            "denied",
            "limit",
            "timeout",
            "schema",
            "unsupported",
            "closed",
        )
        for code in priorities:
            if code in codes:
                return code
    return "protocol"


def _guard_state(deadline: float, failure: ErrorCode | None, *, active: bool = True) -> None:
    if failure is not None:
        raise MCPSourceError(failure)
    if not active:
        raise MCPSourceError("closed")
    if asyncio.get_running_loop().time() >= deadline:
        raise MCPSourceError("timeout")


def _check_response(method: str, response: httpx.Response) -> None:
    status = response.status_code
    if response.is_redirect or status in {401, 403}:
        raise MCPSourceError("denied")
    if status == 405 and method in {"GET", "DELETE"}:
        return
    if method == "DELETE":
        if status not in {200, 204}:
            raise MCPSourceError("protocol")
        return
    if method == "POST" and status == 202:
        return
    if status != 200:
        raise MCPSourceError("protocol")
    media = response.headers.get("content-type", "").partition(";")[0].strip().lower()
    allowed = (
        {"text/event-stream"} if method == "GET" else {"application/json", "text/event-stream"}
    )
    if media not in allowed:
        raise MCPSourceError("protocol")


@dataclass(frozen=True)
class MCPLimits:
    timeout_seconds: float = 30
    request_seconds: float = 10
    max_pages: int = 8
    max_results: int = 100
    max_content_bytes: int = 1_000_000
    max_wire_bytes: int = 4_000_000
    max_requests: int = 64

    def __post_init__(self) -> None:
        for value, ceiling in ((self.timeout_seconds, 120), (self.request_seconds, 30)):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 < value <= ceiling
            ):
                raise MCPSourceError("config")
        for value, ceiling in (
            (self.max_pages, 32),
            (self.max_results, 1000),
            (self.max_content_bytes, 4_000_000),
            (self.max_wire_bytes, 16_000_000),
            (self.max_requests, 256),
        ):
            if type(value) is not int or not 0 < value <= ceiling:
                raise MCPSourceError("config")


@dataclass(frozen=True, repr=False)
class MCPBearer:
    token: str
    audience: str


@dataclass(frozen=True, repr=False)
class MCPToolMapping:
    name: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    argument_name: str
    fixed_arguments: dict[str, Any] = field(default_factory=dict)
    items_path: tuple[str, ...] = ()
    id_path: tuple[str, ...] = ("id",)
    title_path: tuple[str, ...] | None = ("title",)
    text_path: tuple[str, ...] | None = ("text",)
    uri_path: tuple[str, ...] | None = ("uri",)
    revision_path: tuple[str, ...] | None = None
    cursor_argument: str | None = None
    next_cursor_path: tuple[str, ...] | None = None


@dataclass(frozen=True, repr=False)
class MCPSourceConfig:
    endpoint: str
    bearer_audience: str | None = None
    approved_tools: tuple[str, ...] = ()
    search: MCPToolMapping | None = None
    read: MCPToolMapping | None = None
    limits: MCPLimits = field(default_factory=MCPLimits)
    approved_resource_ids: tuple[str, ...] = ()


@dataclass(frozen=True, repr=False)
class MCPEvidence:
    resource_id: str
    uri: str
    title: str
    text: str = ""
    mime_type: str = "text/plain"
    revision: str | None = None
    receipt: MCPReadReceipt | None = None


@dataclass(frozen=True, repr=False)
class MCPReadReceipt:
    operation: Literal["resources/read", "tools/call"]
    requested_id: str
    content_sha256: str
    config_sha256: str


@dataclass(frozen=True, repr=False)
class MCPPage:
    items: tuple[MCPEvidence, ...]
    next_cursor: str | None
    complete: bool


@dataclass(frozen=True)
class MCPDiscovery:
    resources: bool
    tools: tuple[str, ...]


def _json(value: Any, maximum: int) -> bytes:
    try:
        encoded = json.dumps(
            value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode()
    except (ValueError, TypeError, RecursionError):
        raise MCPSourceError("schema") from None
    if len(encoded) > maximum:
        raise MCPSourceError("limit")
    return encoded


def _string(value: Any, maximum: int = 2048, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not empty):
        raise MCPSourceError("schema")
    if len(value.encode()) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise MCPSourceError("limit")
    return value


def _endpoint_url(url: str) -> httpx.URL:
    try:
        _string(url, 2048)
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or "\\" in url
            or "%" in url
            or any(c.isspace() for c in url)
            or parts.port == 0
            or any(p in (".", "..") for p in parts.path.split("/"))
        ):
            raise MCPSourceError("config")
        # HTTPX must authorize the same ASCII hostname that it sends on the wire.
        return httpx.URL(url)
    except (httpx.InvalidURL, ValueError, UnicodeError):
        raise MCPSourceError("config") from None


def _origin(url: str) -> tuple[str, str, int]:
    endpoint = _endpoint_url(url)
    return endpoint.scheme, endpoint.raw_host.decode("ascii"), endpoint.port or 443


# This subset has no references, regular expressions or combinatorial validators.
_SCHEMA_KEYS = frozenset({"type", "enum", "const", "title", "description"})
_SCHEMA_TYPE_KEYS = {
    "object": {"properties", "required", "additionalProperties"},
    "array": {"items", "maxItems", "minItems"},
    "string": {"maxLength", "minLength"},
    "integer": {"minimum", "maximum"},
    "number": {"minimum", "maximum"},
    "boolean": set(),
    "null": set(),
}


def _schema(schema: dict[str, Any]) -> None:
    _json(schema, 16_384)
    remaining = 256

    def visit(node: Any, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 8 or not isinstance(node, dict):
            raise MCPSourceError("schema")
        kind = node.get("type")
        if not isinstance(kind, str) or kind not in _SCHEMA_TYPE_KEYS:
            raise MCPSourceError("schema")
        if set(node) - (_SCHEMA_KEYS | _SCHEMA_TYPE_KEYS[kind]):
            raise MCPSourceError("schema")
        if node["type"] == "object":
            if node.get("additionalProperties") is not False:
                raise MCPSourceError("schema")
            properties = node.get("properties", {})
            if not isinstance(properties, dict) or len(properties) > 32:
                raise MCPSourceError("schema")
            for name, child in properties.items():
                _string(name, 128)
                visit(child, depth + 1)
        if node["type"] == "array":
            if type(node.get("maxItems")) is not int or not 0 <= node["maxItems"] <= 1000:
                raise MCPSourceError("schema")
            visit(node.get("items"), depth + 1)
        if node["type"] == "string":
            if type(node.get("maxLength")) is not int or not 0 <= node["maxLength"] <= 4_000_000:
                raise MCPSourceError("schema")
        if "enum" in node and (not isinstance(node["enum"], list) or len(node["enum"]) > 32):
            raise MCPSourceError("schema")

    visit(schema, 0)
    try:
        Draft202012Validator.check_schema(schema)
    except (SchemaError, ValueError, TypeError, RecursionError):
        raise MCPSourceError("schema") from None


def _validate(schema: dict[str, Any], value: Any, maximum: int) -> None:
    _json(value, maximum)
    try:
        # An explicit empty registry never retrieves external schema resources.
        valid = Draft202012Validator(schema, registry=Registry()).is_valid(value)
    except Unresolvable:
        raise MCPSourceError("schema") from None
    if not valid:
        raise MCPSourceError("schema")


_MISSING_CURSOR = object()


def _path(
    value: Any, path: tuple[str, ...] | None, default: Any = "", *, optional: bool = False
) -> Any:
    if path is None:
        return default
    for key in path:
        if not isinstance(value, dict):
            raise MCPSourceError("schema")
        if key not in value:
            if optional:
                return default
            raise MCPSourceError("schema")
        value = value[key]
    return value


class _LimitedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, owner: _PinnedTransport) -> None:
        self.stream, self.owner = stream, owner

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async with asyncio.timeout_at(self.owner.deadline):
                self.owner._check_state()
                async for chunk in self.stream:
                    self.owner.received += len(chunk)
                    if self.owner.received > self.owner.limits.max_wire_bytes:
                        raise MCPSourceError("limit")
                    await self.owner._authorize()
                    yield chunk
        except Exception as error:  # noqa: BLE001 — only fixed diagnostics cross the transport boundary
            self.owner.failure = self.owner.failure or _error_code(error)
            raise MCPSourceError(self.owner.failure) from None

    async def aclose(self) -> None:
        # The SDK cancels its task group before joining HTTP stream finalizers.
        with anyio.CancelScope(shield=True):
            await self.stream.aclose()


class _PinnedTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        config: MCPSourceConfig,
        credential: MCPBearer | None,
        deadline: float,
        validate_access: Callable[[], Awaitable[None]],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.endpoint = _endpoint_url(config.endpoint)
        self.credential, self.deadline, self.limits = credential, deadline, config.limits
        self.validate_access = validate_access
        self.inner = (
            transport
            if transport is not None
            else httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        )
        self.received, self.requests = 0, 0
        self.target: tuple[str, str, str] | None = None
        self.pin_lock = asyncio.Lock()
        self.failure: ErrorCode | None = None

    def _check_state(self) -> None:
        _guard_state(self.deadline, self.failure)

    async def _authorize(self) -> None:
        self._check_state()
        await self.validate_access()
        self._check_state()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return await self._handle(request)
        except Exception as error:  # noqa: BLE001 — HTTP exceptions may contain upstream credentials
            self.failure = self.failure or _error_code(error)
            raise MCPSourceError(self.failure) from None

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        response: httpx.Response | None = None
        handed_off = False
        try:
            async with asyncio.timeout_at(self.deadline):
                self._check_state()
                if request.url != self.endpoint or request.method not in {"POST", "GET", "DELETE"}:
                    raise MCPSourceError("denied")
                self.requests += 1
                if self.requests > self.limits.max_requests:
                    raise MCPSourceError("limit")
                await self._authorize()
                if self.target is None:
                    async with self.pin_lock:
                        if self.target is None:
                            refusal, pinned, host, sni = await pinned_target(
                                str(self.endpoint), schemes=("https",)
                            )
                            if (
                                refusal
                                or host != self.endpoint.netloc.decode("ascii")
                                or sni != self.endpoint.raw_host.decode("ascii")
                            ):
                                raise MCPSourceError("denied")
                            self.target = pinned, host, sni
                pinned, host, sni = self.target
                # Construct an allowlist of protocol headers, never propagate inbound headers.
                headers = {
                    key: value
                    for key, value in request.headers.items()
                    if key
                    in {
                        "accept",
                        "content-type",
                        "mcp-session-id",
                        "mcp-protocol-version",
                        "last-event-id",
                        "mcp-method",
                        "mcp-name",
                    }
                }
                if any(len(v) > 2048 for v in headers.values()):
                    raise MCPSourceError("limit")
                headers.update({"host": host, "accept-encoding": "identity"})
                if self.credential is not None:
                    headers["authorization"] = "Bearer " + self.credential.token
                body = await request.aread()
                if len(body) > 32_768:
                    raise MCPSourceError("limit")
                outgoing = httpx.Request(
                    request.method,
                    pinned,
                    headers=headers,
                    content=body,
                    extensions={
                        "timeout": request.extensions.get("timeout", {}),
                        "sni_hostname": sni,
                    },
                )
                await self._authorize()
                response = await self.inner.handle_async_request(outgoing)
                await self._authorize()
                _check_response(request.method, response)
                # Reject compression so the wire budget bounds decoded protocol bytes.
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise MCPSourceError("protocol")
                if not isinstance(response.stream, httpx.AsyncByteStream):
                    raise MCPSourceError("protocol")
                response.stream = _LimitedStream(response.stream, self)
                handed_off = True
                return response
        except Exception as error:  # noqa: BLE001 — poison the session before awaiting private failure cleanup
            self.failure = self.failure or _error_code(error)
            raise MCPSourceError(self.failure) from None
        finally:
            if response is not None and not handed_off:
                # Leave the deadline scope before joining an unclaimed response's finalizer.
                with anyio.CancelScope(shield=True):
                    await response.aclose()

    async def aclose(self) -> None:
        await self.inner.aclose()


_T = TypeVar("_T")


class MCPSourceClient:
    """One owner-authorized, bounded session; resources and tools yield evidence."""

    def __init__(
        self,
        config: MCPSourceConfig,
        credential: MCPBearer | None,
        *,
        validate_access: Callable[[], Awaitable[None]],
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = copy.deepcopy(config)
        self._test_transport = _transport
        self.credential = credential
        self.validate_access = validate_access
        origin = _origin(config.endpoint)
        if credential is None:
            if config.bearer_audience is not None:
                raise MCPSourceError("config")
        elif (
            config.bearer_audience is None
            or origin != _origin(config.bearer_audience)
            or credential.audience != config.bearer_audience
            or not isinstance(credential.token, str)
            or not 0 < len(credential.token) <= 8192
            or any(not 33 <= ord(c) <= 126 for c in credential.token)
        ):
            raise MCPSourceError("config")
        if len(config.approved_tools) > 2 or len(set(config.approved_tools)) != len(
            config.approved_tools
        ):
            raise MCPSourceError("config")
        mappings = [m for m in (self._config.search, self._config.read) if m is not None]
        if set(config.approved_tools) != {m.name for m in mappings}:
            raise MCPSourceError("config")
        for mapping in mappings:
            _string(mapping.name, 128)
            _string(mapping.argument_name, 128)
            _schema(mapping.input_schema)
            _schema(mapping.output_schema)
            if (
                mapping.input_schema["type"] != "object"
                or mapping.output_schema["type"] != "object"
            ):
                raise MCPSourceError("config")
            if not isinstance(mapping.fixed_arguments, dict):
                raise MCPSourceError("config")
            if mapping.cursor_argument is not None:
                _string(mapping.cursor_argument, 128)
            _json(mapping.fixed_arguments, 16_384)
            if mapping.argument_name in mapping.fixed_arguments or (
                mapping.cursor_argument is not None
                and (
                    mapping.cursor_argument == mapping.argument_name
                    or mapping.cursor_argument in mapping.fixed_arguments
                )
            ):
                raise MCPSourceError("config")
            for path in (
                mapping.items_path,
                mapping.id_path,
                mapping.title_path,
                mapping.text_path,
                mapping.uri_path,
                mapping.revision_path,
                mapping.next_cursor_path,
            ):
                if path is not None:
                    if not isinstance(path, tuple) or len(path) > 8:
                        raise MCPSourceError("config")
                    for part in path:
                        _string(part, 128)
        self._privacy: ExitStack | None = None
        self._owner_task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._closing = False
        self._owner_failure: ErrorCode | None = None
        self._session: ClientSession | None = None
        self._transport: _PinnedTransport | None = None
        self._operation_failure: ErrorCode | None = None
        self._deadline = 0.0
        self._discovery: MCPDiscovery | None = None
        self._schema_changed = False
        self._pages, self._results = 0, 0
        self._issued_cursors: set[tuple[tuple[str, str], str]] = set()
        self._used_cursors: set[tuple[tuple[str, str], str]] = set()
        if len(config.approved_resource_ids) > config.limits.max_results:
            raise MCPSourceError("config")
        self._selected_ids = frozenset(
            _string(identity) for identity in config.approved_resource_ids
        )
        self._resource_titles: dict[str, str] = {}

    def __repr__(self) -> str:
        return "MCPSourceClient(<redacted>)"

    @property
    def config(self) -> MCPSourceConfig:
        return copy.deepcopy(self._config)

    @property
    def config_sha256(self) -> str:
        return hashlib.sha256(_json(asdict(self._config), 131_072)).hexdigest()

    def _with_receipt(
        self, evidence: MCPEvidence, operation: Literal["resources/read", "tools/call"]
    ) -> MCPEvidence:
        receipt = MCPReadReceipt(
            operation,
            evidence.resource_id,
            hashlib.sha256(evidence.text.encode()).hexdigest(),
            self.config_sha256,
        )
        return replace(evidence, receipt=receipt)

    def _check_state(self, *, require_session: bool = False) -> None:
        owner = self._owner_task
        failure = self._operation_failure or self._owner_failure
        if failure is None and self._transport is not None:
            failure = self._transport.failure
        # The lifecycle owner may perform SDK session termination during cleanup.
        lifecycle = not require_session and asyncio.current_task() is owner
        active = (
            owner is not None
            and not owner.done()
            and (not self._closing or lifecycle)
            and (not require_session or self._session is not None)
        )
        _guard_state(self._deadline, failure, active=active)

    async def _authorize(self) -> None:
        self._check_state()
        await self.validate_access()
        self._check_state()

    async def __aenter__(self) -> MCPSourceClient:
        if self._owner_task is not None or self._deadline:
            raise MCPSourceError("closed")
        self._privacy = ExitStack()
        self._privacy.enter_context(private_work())
        loop = asyncio.get_running_loop()
        self._deadline = loop.time() + self._config.limits.timeout_seconds
        self._ready = loop.create_future()
        self._owner_task = asyncio.create_task(self._serve_session())
        try:
            await self._ready
            self._check_state(require_session=True)
            return self
        except asyncio.CancelledError:
            try:
                await self._close()
            except asyncio.CancelledError:
                pass
            finally:
                self._leave_private()
            raise
        except Exception as error:  # noqa: BLE001 — only fixed initialization failures reach callers
            try:
                await self._close()
            finally:
                self._leave_private()
            raise MCPSourceError(self._owner_failure or _error_code(error)) from None

    async def _serve_session(self) -> None:
        # SDK AnyIO scopes are entered and exited by this one lifecycle owner.
        with private_work():
            try:
                async with AsyncExitStack() as stack:
                    async with asyncio.timeout_at(self._deadline):
                        await self._initialize(stack)
                        assert self._ready is not None
                        if not self._ready.done():
                            self._ready.set_result(None)
                        await asyncio.Future[None]()
            except (
                Exception,  # noqa: BLE001 — SDK lifecycle failures must remain fixed codes
                asyncio.CancelledError,
            ) as error:
                failure = self._transport.failure if self._transport else None
                expected_close = self._closing and isinstance(error, asyncio.CancelledError)
                if failure is not None or not expected_close:
                    self._owner_failure = failure or _error_code(error)
                if self._ready is not None and not self._ready.done():
                    self._ready.set_exception(MCPSourceError(self._owner_failure or "closed"))
            finally:
                self._session = None

    async def _initialize(self, stack: AsyncExitStack) -> None:
        await self._authorize()
        transport = _PinnedTransport(
            self._config,
            self.credential,
            self._deadline,
            self._authorize,
            transport=self._test_transport,
        )
        self._transport = transport
        client = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(self._config.limits.request_seconds),
            )
        )
        read, write, _ = await stack.enter_async_context(
            streamable_http_client(self._config.endpoint, http_client=client)
        )
        self._session = await stack.enter_async_context(
            ClientSession(
                read,
                write,
                read_timeout_seconds=timedelta(seconds=self._config.limits.request_seconds),
                message_handler=self._message_handler,
            )
        )
        result = await self._session.initialize()
        self._discovery = MCPDiscovery(result.capabilities.resources is not None, ())
        await self._authorize()

    async def _close(self) -> None:
        set_query_tag("mcp-source.cleanup")
        owner = self._owner_task
        if owner is None:
            return
        if not self._closing:
            self._closing = True
            owner.cancel()
        interrupted: asyncio.CancelledError | None = None
        while not owner.done():
            try:
                await asyncio.shield(owner)
            except asyncio.CancelledError as error:
                interrupted = interrupted or error
        if not owner.cancelled():
            owner.result()
        if interrupted is not None:
            raise interrupted

    def _leave_private(self) -> None:
        privacy, self._privacy = self._privacy, None
        if privacy is not None:
            privacy.close()

    async def __aexit__(self, exc_type: Any, error: BaseException | None, traceback: Any) -> None:
        try:
            await self._close()
            failure = self._owner_failure
            if error is None and failure is not None and self._operation_failure is None:
                raise MCPSourceError(failure)
        finally:
            self._leave_private()

    async def _message_handler(self, message: Any) -> None:
        if isinstance(message, types.ServerNotification) and isinstance(
            message.root, types.ToolListChangedNotification
        ):
            self._schema_changed = True

    async def _run(self, action: Callable[[ClientSession], Awaitable[_T]]) -> _T:
        with private_work():
            return await self._execute(action)

    async def _execute(self, action: Callable[[ClientSession], Awaitable[_T]]) -> _T:
        set_query_tag("mcp-source")
        try:
            self._check_state(require_session=True)
            async with asyncio.timeout_at(self._deadline):
                await self._authorize()
                self._check_state(require_session=True)
                assert self._session is not None
                result = await action(self._session)
                await self._authorize()
                self._check_state(require_session=True)
                return result
        except (
            Exception,  # Terminal refusals must drain owned HTTP work before returning.
            asyncio.CancelledError,
        ) as error:
            if isinstance(error, asyncio.CancelledError):
                code: ErrorCode = "closed"
            elif isinstance(error, MCPSourceError):
                code = error.code
            else:
                code = (self._transport.failure if self._transport else None) or _error_code(error)
            code = self._operation_failure or code
            self._operation_failure = code
            try:
                await self._close()
            except asyncio.CancelledError:
                if not isinstance(error, asyncio.CancelledError):
                    raise
            if isinstance(error, asyncio.CancelledError):
                raise
            raise MCPSourceError(code) from None

    async def discover(self) -> MCPDiscovery:
        async def action(session: ClientSession) -> MCPDiscovery:
            approved = set(self._config.approved_tools)
            found: set[str] = set()
            cursor: str | None = None
            seen: set[str] = set()
            if approved:
                for _ in range(self._config.limits.max_pages):
                    result = await session.list_tools(
                        params=types.PaginatedRequestParams(cursor=cursor)
                    )
                    if len(result.tools) > self._config.limits.max_results:
                        raise MCPSourceError("limit")
                    for tool in result.tools:
                        if tool.name in seen:
                            raise MCPSourceError("schema")
                        seen.add(tool.name)
                        if tool.name not in approved:
                            continue
                        for mapping in (self._config.search, self._config.read):
                            if mapping is not None and mapping.name == tool.name:
                                if (
                                    tool.inputSchema != mapping.input_schema
                                    or tool.outputSchema != mapping.output_schema
                                ):
                                    raise MCPSourceError("schema")
                        found.add(tool.name)
                    if len(seen) > self._config.limits.max_results:
                        raise MCPSourceError("limit")
                    cursor = result.nextCursor
                    if cursor is None:
                        break
                    _string(cursor)
                else:
                    raise MCPSourceError("limit")
                if found != approved:
                    raise MCPSourceError("unsupported")
            assert self._discovery is not None
            self._discovery = MCPDiscovery(self._discovery.resources, tuple(sorted(found)))
            self._schema_changed = False
            return self._discovery

        return await self._run(action)

    def _page_start(self, cursor: str | None, scope: tuple[str, str]) -> None:
        if cursor is not None:
            _string(cursor)
            key = scope, cursor
            if key not in self._issued_cursors or key in self._used_cursors:
                raise MCPSourceError("denied")
            self._used_cursors.add(key)
        self._pages += 1
        if self._pages > self._config.limits.max_pages:
            raise MCPSourceError("limit")

    def _page(
        self, items: list[MCPEvidence], cursor: str | None, scope: tuple[str, str]
    ) -> MCPPage:
        self._results += len(items)
        if self._results > self._config.limits.max_results:
            raise MCPSourceError("limit")
        if cursor is not None:
            _string(cursor)
            key = scope, cursor
            if key in self._issued_cursors:
                raise MCPSourceError("protocol")
            self._issued_cursors.add(key)
        return MCPPage(tuple(items), cursor, cursor is None)

    async def list_resources(self, cursor: str | None = None) -> MCPPage:
        async def action(session: ClientSession) -> MCPPage:
            if self._discovery is None or not self._discovery.resources:
                raise MCPSourceError("unsupported")
            self._page_start(cursor, ("resources", ""))
            result = await session.list_resources(
                params=types.PaginatedRequestParams(cursor=cursor)
            )
            items = []
            for resource in result.resources:
                identity = _string(str(resource.uri))
                title = _string(resource.title or resource.name)
                items.append(MCPEvidence(identity, identity, title))
                self._resource_titles[identity] = title
            return self._page(items, result.nextCursor, ("resources", ""))

        return await self._run(action)

    async def _tool(
        self, session: ClientSession, mapping: MCPToolMapping, value: str, cursor: str | None = None
    ) -> Any:
        await self.discover()
        if (
            self._schema_changed
            or self._discovery is None
            or mapping.name not in self._discovery.tools
        ):
            raise MCPSourceError("schema")
        arguments = dict(mapping.fixed_arguments)
        arguments[mapping.argument_name] = _string(value, 4096)
        if cursor is not None:
            if mapping.cursor_argument is None:
                raise MCPSourceError("unsupported")
            arguments[mapping.cursor_argument] = cursor
        _validate(mapping.input_schema, arguments, 32_768)
        result = await session.call_tool(mapping.name, arguments)
        if self._schema_changed:
            raise MCPSourceError("schema")
        if result.isError or result.structuredContent is None:
            raise MCPSourceError("protocol")
        _validate(
            mapping.output_schema, result.structuredContent, self._config.limits.max_content_bytes
        )
        return result.structuredContent

    def _evidence(self, value: Any, mapping: MCPToolMapping) -> MCPEvidence:
        identity = _string(_path(value, mapping.id_path))
        title = _string(_path(value, mapping.title_path, identity))
        text = _path(value, mapping.text_path)
        if not isinstance(text, str) or len(text.encode()) > self._config.limits.max_content_bytes:
            raise MCPSourceError("limit")
        uri = _string(_path(value, mapping.uri_path, identity))
        try:
            citation = urlsplit(uri)
            if citation.username is not None or citation.password is not None:
                raise MCPSourceError("schema")
        except ValueError:
            raise MCPSourceError("schema") from None
        revision = _path(value, mapping.revision_path, None)
        if revision is not None:
            revision = _string(revision)
        return MCPEvidence(identity, uri, title, text, revision=revision)

    async def search(self, query: str, cursor: str | None = None) -> MCPPage:
        async def action(session: ClientSession) -> MCPPage:
            mapping = self._config.search
            if mapping is None:
                raise MCPSourceError("unsupported")
            self._page_start(cursor, ("search", query))
            result = await self._tool(session, mapping, query, cursor)
            rows = _path(result, mapping.items_path)
            if not isinstance(rows, list):
                raise MCPSourceError("schema")
            items = [self._evidence(row, mapping) for row in rows]
            next_cursor = _path(result, mapping.next_cursor_path, _MISSING_CURSOR, optional=True)
            return self._page(
                items,
                None if next_cursor is _MISSING_CURSOR else _string(next_cursor),
                ("search", query),
            )

        return await self._run(action)

    async def read(self, resource_id: str) -> MCPEvidence:
        async def action(session: ClientSession) -> MCPEvidence:
            identity = _string(resource_id)
            if identity not in self._selected_ids:
                raise MCPSourceError("denied")
            if self._config.read is not None:
                result = await self._tool(session, self._config.read, identity)
                evidence = self._evidence(
                    _path(result, self._config.read.items_path), self._config.read
                )
                if evidence.resource_id != identity:
                    raise MCPSourceError("protocol")
                return self._with_receipt(evidence, "tools/call")
            if self._discovery is None or not self._discovery.resources:
                raise MCPSourceError("unsupported")
            result = await session.read_resource(AnyUrl(identity))
            if not result.contents or len(result.contents) > self._config.limits.max_results:
                raise MCPSourceError("limit")
            texts: list[str] = []
            size = 0
            mime_type = result.contents[0].mimeType or "text/plain"
            for content in result.contents:
                if (
                    not isinstance(content, types.TextResourceContents)
                    or str(content.uri) != identity
                ):
                    raise MCPSourceError("unsupported")
                if (content.mimeType or "text/plain") != mime_type:
                    raise MCPSourceError("unsupported")
                size += len(content.text.encode()) + bool(texts)
                if size > self._config.limits.max_content_bytes:
                    raise MCPSourceError("limit")
                texts.append(content.text)
            return self._with_receipt(
                MCPEvidence(
                    identity,
                    identity,
                    self._resource_titles.get(identity, identity),
                    "\n".join(texts),
                    _string(mime_type, 128),
                ),
                "resources/read",
            )

        return await self._run(action)
