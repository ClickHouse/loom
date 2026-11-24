"""Bounded reads from an explicitly configured remote MCP source.

The caller supplies an approved connection and, if required, a preissued bearer.
It owns credential storage, authorization, and the use of the returned evidence.
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
from pydantic import AnyUrl, ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

from ._mcp_transport import private_work, resolve_public_address

ErrorCode = Literal[
    "config",
    "unsupported",
    "denied",
    "reconnect",
    "protocol",
    "schema",
    "limit",
    "timeout",
    "closed",
]


class MCPSourceError(Exception):
    """Only a fixed code may cross the client boundary."""

    def __init__(self, code: ErrorCode) -> None:
        if code not in {
            "config",
            "unsupported",
            "denied",
            "reconnect",
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
            "reconnect",
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
    if status == 401:
        raise MCPSourceError("reconnect")
    if response.is_redirect or status == 403:
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
    timeout_seconds: float = 30.0
    request_seconds: float = 10.0
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
        # Profile digests use one representation for equal effective deadlines.
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))
        object.__setattr__(self, "request_seconds", float(self.request_seconds))
        for value, ceiling in (
            (self.max_pages, 32),
            (self.max_results, 1000),
            (self.max_content_bytes, 4_000_000),
            (self.max_wire_bytes, 16_000_000),
            (self.max_requests, 256),
        ):
            if type(value) is not int or not 0 < value <= ceiling:
                raise MCPSourceError("config")


@dataclass(frozen=True)
class MCPDataPolicy:
    """Per-connection parser caps; callers may lower but not raise the ceilings."""

    max_nodes: int = 4096
    max_depth: int = 16
    max_items: int = 1000
    max_string_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        for value, ceiling in (
            (self.max_nodes, 4096),
            (self.max_depth, 16),
            (self.max_items, 1000),
            (self.max_string_bytes, 1_000_000),
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
    result_format: Literal["structured", "json_text", "text"] = "structured"
    require_output_schema: bool = True


@dataclass(frozen=True, repr=False)
class MCPSourceConfig:
    endpoint: str
    bearer_audience: str | None = None
    approved_tools: tuple[str, ...] = ()
    search: MCPToolMapping | None = None
    read: MCPToolMapping | None = None
    limits: MCPLimits = field(default_factory=MCPLimits)
    approved_resource_ids: tuple[str, ...] = ()
    data_policy: MCPDataPolicy = field(default_factory=MCPDataPolicy)


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
    identity_verified: bool = True


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


def _resource_url(identity: str) -> AnyUrl:
    try:
        uri = AnyUrl(identity)
    except ValidationError:
        raise MCPSourceError("unsupported") from None
    if str(uri) != identity:
        raise MCPSourceError("unsupported")
    return uri


def _endpoint_url(url: str) -> httpx.URL:
    try:
        _string(url, 2048)
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or "?" in url
            or "#" in url
            or "\\" in url
            or "%" in url
            or any(c.isspace() for c in url)
            or parts.port == 0
            or any(p in (".", "..") for p in parts.path.split("/"))
        ):
            raise MCPSourceError("config")
        return httpx.URL(url)
    except (httpx.InvalidURL, ValueError, UnicodeError):
        raise MCPSourceError("config") from None


def _origin(url: str) -> tuple[str, str, int]:
    endpoint = _endpoint_url(url)
    return endpoint.scheme, endpoint.raw_host.decode("ascii"), endpoint.port or 443


_SCHEMA_ANNOTATIONS = frozenset({"title", "description", "default", "examples"})
_SCHEMA_KEYS = _SCHEMA_ANNOTATIONS | {"enum", "const"}
_SCHEMA_TYPE_KEYS = {
    "object": {"properties", "required", "additionalProperties", "minProperties", "maxProperties"},
    "array": {"items", "maxItems", "minItems"},
    "string": {"maxLength", "minLength"},
    "integer": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"},
    "number": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"},
    "boolean": set(),
    "null": set(),
}


def _bounded_data(
    value: Any, maximum: int, policy: MCPDataPolicy, *, config_strings: bool = False
) -> None:
    remaining = policy.max_nodes
    string_maximum = min(maximum, policy.max_string_bytes)
    pending = [(value, 0)]
    while pending:
        node, depth = pending.pop()
        remaining -= 1
        if remaining < 0 or depth > policy.max_depth:
            raise MCPSourceError("limit")
        if isinstance(node, dict):
            if len(node) > 256 or any(not isinstance(key, str) for key in node):
                raise MCPSourceError("limit")
            pending.extend((item, depth + 1) for pair in node.items() for item in pair)
        elif isinstance(node, list):
            if len(node) > policy.max_items:
                raise MCPSourceError("limit")
            pending.extend((item, depth + 1) for item in node)
        elif isinstance(node, str):
            try:
                if config_strings:
                    _string(node, string_maximum, empty=True)
                if len(node.encode()) > string_maximum:
                    raise MCPSourceError("limit")
            except UnicodeError:
                raise MCPSourceError("schema") from None
        elif type(node) is int:
            if node.bit_length() > 4096:
                raise MCPSourceError("limit")
        elif node is not None and type(node) not in (bool, float):
            raise MCPSourceError("schema")
    _json(value, maximum)


def _schema_kind(node: dict[str, Any]) -> str | None:
    kind = node.get("type")
    if isinstance(kind, list):
        if len(kind) != 2 or "null" not in kind:
            raise MCPSourceError("schema")
        non_null = [item for item in kind if item != "null"]
        if len(non_null) != 1:
            raise MCPSourceError("schema")
        kind = non_null[0]
    if kind is not None and (not isinstance(kind, str) or kind not in _SCHEMA_TYPE_KEYS):
        raise MCPSourceError("schema")
    return kind


def _schema_children(node: dict[str, Any], *, root: bool = False) -> list[Any]:
    keys = set(node) - ({"$defs"} if root else set())
    if "enum" in node and (not isinstance(node["enum"], list) or len(node["enum"]) > 32):
        raise MCPSourceError("schema")
    if "$ref" in node:
        if keys - (_SCHEMA_ANNOTATIONS | {"$ref"}):
            raise MCPSourceError("schema")
        return []
    if "anyOf" in node:
        choices = node["anyOf"]
        if (
            keys - (_SCHEMA_KEYS | {"anyOf"})
            or not isinstance(choices, list)
            or len(choices) != 2
            or sum(choice == {"type": "null"} for choice in choices) != 1
        ):
            raise MCPSourceError("schema")
        return choices
    kind = _schema_kind(node)
    allowed = _SCHEMA_KEYS | ({"type"} | _SCHEMA_TYPE_KEYS[kind] if kind else set())
    if keys - allowed:
        raise MCPSourceError("schema")
    if kind == "object":
        properties = node.get("properties", {})
        additional = node.get("additionalProperties", True)
        if not isinstance(properties, dict) or len(properties) > 64:
            raise MCPSourceError("schema")
        for name in properties:
            _string(name, 128)
        if type(additional) is not bool and not isinstance(additional, dict):
            raise MCPSourceError("schema")
        return [*properties.values(), *([additional] if isinstance(additional, dict) else [])]
    return [node["items"]] if kind == "array" and "items" in node else []


def _schema(schema: dict[str, Any], policy: MCPDataPolicy) -> None:
    if not isinstance(schema, dict):
        raise MCPSourceError("schema")
    _bounded_data(schema, 16_384, policy)
    definitions = schema.get("$defs", {})
    if not isinstance(definitions, dict) or len(definitions) > 64:
        raise MCPSourceError("schema")
    for name in definitions:
        _string(name, 128)
        if any(char in name for char in "/~#%"):
            raise MCPSourceError("schema")
    remaining = 256

    def visit(node: Any, depth: int, active: tuple[str, ...] = (), *, root: bool = False) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 16 or not isinstance(node, dict):
            raise MCPSourceError("schema")
        children = _schema_children(node, root=root)
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
                raise MCPSourceError("schema")
            name = ref[len("#/$defs/") :]
            if name not in definitions or name in active:
                raise MCPSourceError("schema")
            visit(definitions[name], depth + 1, (*active, name))
        for child in children:
            visit(child, depth + 1, active)

    for name, definition in definitions.items():
        visit(definition, 1, (name,))
    visit(schema, 0, root=True)
    try:
        Draft202012Validator.check_schema(schema)
    except (SchemaError, ValueError, TypeError, RecursionError):
        raise MCPSourceError("schema") from None


def _validate(schema: dict[str, Any], value: Any, maximum: int, policy: MCPDataPolicy) -> None:
    _bounded_data(value, maximum, policy)
    try:
        # An explicit empty registry never retrieves external schema resources.
        valid = Draft202012Validator(schema, registry=Registry()).is_valid(value)
    except (Unresolvable, ValueError, TypeError, RecursionError):
        raise MCPSourceError("schema") from None
    if not valid:
        raise MCPSourceError("schema")


_DEFAULT_DATA_POLICY = MCPDataPolicy()


def validate_mcp_tool_mapping(
    mapping: MCPToolMapping, *, search: bool = False, policy: MCPDataPolicy = _DEFAULT_DATA_POLICY
) -> None:
    """Validate an operator contract without opening a session or granting authority."""
    if type(policy) is not MCPDataPolicy:
        raise MCPSourceError("config")
    _string(mapping.name, 128)
    _string(mapping.argument_name, 128)
    _schema(mapping.input_schema, policy)
    _schema(mapping.output_schema, policy)
    if (
        mapping.result_format not in ("structured", "json_text", "text")
        or type(mapping.require_output_schema) is not bool
        or mapping.input_schema.get("type") != "object"
        or mapping.output_schema.get("type")
        != ("string" if mapping.result_format == "text" else "object")
        or (search and mapping.result_format == "text")
        or (mapping.result_format == "text" and mapping.items_path)
    ):
        raise MCPSourceError("config")
    if mapping.result_format == "text" and (
        (mapping.id_path, mapping.title_path, mapping.text_path, mapping.uri_path)
        != (("id",), ("title",), ("text",), ("uri",))
        or mapping.revision_path is not None
        or mapping.next_cursor_path is not None
        or mapping.cursor_argument is not None
    ):
        raise MCPSourceError("config")
    if not isinstance(mapping.fixed_arguments, dict):
        raise MCPSourceError("config")
    if mapping.cursor_argument is not None:
        _string(mapping.cursor_argument, 128)
    _bounded_data(mapping.fixed_arguments, 16_384, policy, config_strings=True)
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


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len(pairs) > 256:
        raise MCPSourceError("limit")
    result = {}
    for key, value in pairs:
        if key in result:
            raise MCPSourceError("schema")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise MCPSourceError("schema")


def _text_json(text: str, maximum: int, policy: MCPDataPolicy) -> Any:
    _bounded_data(text, maximum, policy)
    try:
        return json.loads(text, object_pairs_hook=_json_pairs, parse_constant=_nonfinite)
    except (ValueError, TypeError, RecursionError):
        raise MCPSourceError("schema") from None


def _tool_value(
    result: types.CallToolResult, mapping: MCPToolMapping, limits: MCPLimits, policy: MCPDataPolicy
) -> Any:
    if result.isError:
        raise MCPSourceError("protocol")
    _bounded_data(result.model_dump(mode="json"), limits.max_wire_bytes, policy)
    if any(not isinstance(block, types.TextContent) for block in result.content):
        raise MCPSourceError("unsupported")
    if mapping.result_format == "structured":
        value: Any = result.structuredContent
        if value is None:
            raise MCPSourceError("protocol")
    else:
        if result.structuredContent is not None or len(result.content) != 1:
            raise MCPSourceError("protocol")
        block = result.content[0]
        if not isinstance(block, types.TextContent):
            raise MCPSourceError("unsupported")
        value = (
            _text_json(block.text, limits.max_content_bytes, policy)
            if mapping.result_format == "json_text"
            else block.text
        )
    _validate(mapping.output_schema, value, limits.max_content_bytes, policy)
    return value


def _resource_value(
    result: types.Result, identity: str, limits: MCPLimits, policy: MCPDataPolicy
) -> tuple[str, str]:
    data = result.model_dump(by_alias=True, exclude_unset=True)
    _bounded_data(data, limits.max_wire_bytes, policy)
    contents = data.get("contents")
    if not isinstance(contents, list) or not contents or len(contents) > limits.max_results:
        raise MCPSourceError("limit")
    texts: list[str] = []
    mime_type = "text/plain"
    size = 0
    for part in contents:
        if not isinstance(part, dict) or part.get("uri") != identity or "blob" in part:
            raise MCPSourceError("unsupported")
        text = part.get("text")
        mime = part.get("mimeType")
        if not isinstance(text, str) or (mime is not None and not isinstance(mime, str)):
            raise MCPSourceError("unsupported")
        mime = mime or "text/plain"
        if texts and mime != mime_type:
            raise MCPSourceError("unsupported")
        mime_type = mime
        size += len(text.encode()) + bool(texts)
        if size > limits.max_content_bytes:
            raise MCPSourceError("limit")
        texts.append(text)
    return "\n".join(texts), _string(mime_type, 128)


def _check_tool_schema(tool: types.Tool, mapping: MCPToolMapping, policy: MCPDataPolicy) -> None:
    _schema(tool.inputSchema, policy)
    if _json(tool.inputSchema, 16_384) != _json(mapping.input_schema, 16_384):
        raise MCPSourceError("schema")
    if tool.outputSchema is not None:
        _schema(tool.outputSchema, policy)
    expected = mapping.output_schema if mapping.require_output_schema else None
    if _json(tool.outputSchema, 16_384) != _json(expected, 16_384):
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
                    self.owner._check_state()
                    yield chunk
        except Exception as error:  # noqa: BLE001 — only fixed diagnostics cross the transport boundary
            self.owner.failure = self.owner.failure or _error_code(error)
            raise MCPSourceError(self.owner.failure) from None

    async def aclose(self) -> None:
        # The SDK cancels its task group before joining HTTP stream finalizers.
        with anyio.move_on_after(1, shield=True):
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
        self.target: httpx.URL | None = None
        self.pin_lock = asyncio.Lock()
        self.failure: ErrorCode | None = None
        self.terminate_only = False

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
                if self.terminate_only and request.method != "DELETE":
                    raise asyncio.CancelledError
                if request.url != self.endpoint or request.method not in {"POST", "GET", "DELETE"}:
                    raise MCPSourceError("denied")
                self.requests += 1
                if self.requests > self.limits.max_requests:
                    raise MCPSourceError("limit")
                await self._authorize()
                if self.target is None:
                    async with self.pin_lock:
                        if self.target is None:
                            address = await resolve_public_address(
                                self.endpoint.raw_host.decode("ascii")
                            )
                            if address is None:
                                raise MCPSourceError("denied")
                            self.target = self.endpoint.copy_with(host=str(address))
                assert self.target is not None
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
                headers.update(
                    {"host": self.endpoint.netloc.decode("ascii"), "accept-encoding": "identity"}
                )
                if self.credential is not None:
                    headers["authorization"] = "Bearer " + self.credential.token
                body = await request.aread()
                if len(body) > 32_768:
                    raise MCPSourceError("limit")
                timeouts = dict(request.extensions.get("timeout", {}))
                if request.method == "GET":
                    # Notifications may be idle until the session deadline.
                    timeouts["read"] = None
                outgoing = httpx.Request(
                    request.method,
                    self.target,
                    headers=headers,
                    content=body,
                    extensions={
                        "timeout": timeouts,
                        "sni_hostname": self.endpoint.raw_host.decode("ascii"),
                    },
                )
                await self._authorize()
                response = await self.inner.handle_async_request(outgoing)
                self._check_state()
                _check_response(request.method, response)
                if request.method == "DELETE":
                    return httpx.Response(response.status_code, content=b"")
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
                with anyio.move_on_after(1, shield=True):
                    await response.aclose()

    async def aclose(self) -> None:
        with anyio.move_on_after(1, shield=True):
            await self.inner.aclose()


_T = TypeVar("_T")


class MCPSourceClient:
    """One bounded, single-use Streamable HTTP session. Call methods sequentially.

    ``validate_access`` must return only while the caller still has authority.
    It may run concurrently for protocol I/O. It can raise ``MCPSourceError('denied')``
    to revoke access. Keep outbound SDK and HTTP tracing disabled in the worker.
    """

    def __init__(
        self,
        config: MCPSourceConfig,
        credential: MCPBearer | None,
        *,
        validate_access: Callable[[], Awaitable[None]],
        deadline: float | None = None,
        _transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if type(config.limits) is not MCPLimits or type(config.data_policy) is not MCPDataPolicy:
            raise MCPSourceError("config")
        self._config = copy.deepcopy(config)
        self._test_transport = _transport
        self._credential = credential
        self._validate_access = validate_access
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
            validate_mcp_tool_mapping(
                mapping, search=mapping is self._config.search, policy=self._config.data_policy
            )
        self._privacy: ExitStack | None = None
        self._owner_task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._closing = False
        self._owner_failure: ErrorCode | None = None
        self._session: ClientSession | None = None
        self._transport: _PinnedTransport | None = None
        self._operation_failure: ErrorCode | None = None
        self._deadline = 0.0
        self._outer_deadline = deadline
        self._discovery: MCPDiscovery | None = None
        self._tool_epoch = 0
        self._discovered_epoch = -1
        self._pages, self._results = 0, 0
        self._issued_cursors: set[tuple[tuple[str, str], str]] = set()
        self._used_cursors: set[tuple[tuple[str, str], str]] = set()
        if len(config.approved_resource_ids) > config.limits.max_results:
            raise MCPSourceError("config")
        self._selected_ids = frozenset(
            _string(identity) for identity in config.approved_resource_ids
        )
        if config.read is None:
            for identity in self._selected_ids:
                _resource_url(identity)
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
        self,
        evidence: MCPEvidence,
        operation: Literal["resources/read", "tools/call"],
        *,
        identity_verified: bool = True,
    ) -> MCPEvidence:
        receipt = MCPReadReceipt(
            operation,
            evidence.resource_id,
            hashlib.sha256(evidence.text.encode()).hexdigest(),
            self.config_sha256,
            identity_verified,
        )
        return replace(evidence, receipt=receipt)

    def _check_state(self, *, require_session: bool = False) -> None:
        owner = self._owner_task
        lifecycle = not require_session and asyncio.current_task() is owner
        failure = self._operation_failure
        # A cancelled read may still release its stateful session with current authority.
        if lifecycle and self._closing and failure == "closed":
            failure = None
        failure = failure or self._owner_failure
        if failure is None and self._transport is not None:
            failure = self._transport.failure
        active = (
            owner is not None
            and not owner.done()
            and (not self._closing or lifecycle)
            and (not require_session or self._session is not None)
        )
        _guard_state(self._deadline, failure, active=active)

    async def _authorize(self) -> None:
        self._check_state()
        await self._validate_access()
        self._check_state()

    async def _transport_authorize(self) -> None:
        try:
            await self._authorize()
        except MCPSourceError as error:
            if (
                error.code == "closed"
                and self._closing
                and asyncio.current_task() is not self._owner_task
            ):
                # Pending SDK notifications stop without poisoning expected shutdown.
                raise asyncio.CancelledError from None
            raise

    async def __aenter__(self) -> MCPSourceClient:
        if self._owner_task is not None or self._deadline:
            raise MCPSourceError("closed")
        self._privacy = ExitStack()
        self._privacy.enter_context(private_work())
        loop = asyncio.get_running_loop()
        self._deadline = loop.time() + self._config.limits.timeout_seconds
        if self._outer_deadline is not None:
            self._deadline = min(self._deadline, self._outer_deadline)
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
                code = await self._recheck_after_close(self._owner_failure or _error_code(error))
            finally:
                self._leave_private()
            raise MCPSourceError(code) from None

    async def _recheck_after_close(self, code: ErrorCode) -> ErrorCode:
        """Report current authority after the SDK owner drains, without another upstream call."""
        if self._transport is None or code in {"denied", "closed", "timeout"}:
            return code
        if asyncio.get_running_loop().time() >= self._deadline:
            return code
        try:
            async with asyncio.timeout_at(self._deadline):
                await self._validate_access()
        except Exception as current:  # noqa: BLE001 — expose only the current fixed refusal
            return _error_code(current)
        return code

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
            self._credential,
            self._deadline,
            self._transport_authorize,
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
        owner = self._owner_task
        if owner is None:
            return
        if not self._closing:
            self._closing = True
            if self._transport is not None:
                self._transport.terminate_only = True
                self._transport.deadline = min(
                    self._deadline, asyncio.get_running_loop().time() + 1
                )
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
            self._tool_epoch += 1

    async def _run(self, action: Callable[[ClientSession], Awaitable[_T]]) -> _T:
        with private_work():
            return await self._execute(action)

    async def _execute(self, action: Callable[[ClientSession], Awaitable[_T]]) -> _T:
        try:
            self._check_state(require_session=True)
            async with asyncio.timeout_at(self._deadline):
                await self._authorize()
                self._check_state(require_session=True)
                assert self._session is not None
                try:
                    result = await action(self._session)
                except Exception:  # Check authority before reporting an upstream error.
                    await self._validate_access()
                    raise
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
            code = await self._recheck_after_close(self._operation_failure or code)
            self._operation_failure = code
            raise MCPSourceError(code) from None

    async def discover(self) -> MCPDiscovery:
        async def action(session: ClientSession) -> MCPDiscovery:
            approved = set(self._config.approved_tools)
            epoch = self._tool_epoch
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
                                _check_tool_schema(tool, mapping, self._config.data_policy)
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
                if epoch != self._tool_epoch:
                    raise MCPSourceError("schema")
            assert self._discovery is not None
            self._discovery = MCPDiscovery(self._discovery.resources, tuple(sorted(found)))
            self._discovered_epoch = epoch
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
            scope = ("resources", "")
            self._page_start(cursor, scope)
            result = await session.send_request(
                types.ClientRequest(
                    types.ListResourcesRequest(params=types.PaginatedRequestParams(cursor=cursor))
                ),
                types.Result,
            )
            data = result.model_dump(by_alias=True, exclude_unset=True)
            limits, policy = self._config.limits, self._config.data_policy
            _bounded_data(data, limits.max_wire_bytes, policy)
            resources = data.get("resources")
            if not isinstance(resources, list) or len(resources) > limits.max_results:
                raise MCPSourceError("limit")
            items = []
            for resource in resources:
                if not isinstance(resource, dict):
                    raise MCPSourceError("schema")
                try:
                    provider_resource = types.Resource.model_validate(resource)
                except ValidationError:
                    raise MCPSourceError("schema") from None
                identity = _string(resource.get("uri"))
                # Keep the selected identity tied to the exact URI the server exposed.
                _resource_url(identity)
                title = _string(provider_resource.title or provider_resource.name)
                items.append(MCPEvidence(identity, identity, title))
                self._resource_titles[identity] = title
            next_cursor = data.get("nextCursor")
            if next_cursor is not None:
                next_cursor = _string(next_cursor)
            return self._page(items, next_cursor, scope)

        return await self._run(action)

    async def _tool(
        self, session: ClientSession, mapping: MCPToolMapping, value: str, cursor: str | None = None
    ) -> Any:
        await self.discover()
        if (
            self._tool_epoch != self._discovered_epoch
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
        _validate(mapping.input_schema, arguments, 32_768, self._config.data_policy)
        # Public SDK RPC path lets independent data bounds precede schema validation.
        result = await session.send_request(
            types.ClientRequest(
                types.CallToolRequest(
                    params=types.CallToolRequestParams(name=mapping.name, arguments=arguments)
                )
            ),
            types.CallToolResult,
        )
        if self._tool_epoch != self._discovered_epoch:
            raise MCPSourceError("schema")
        return _tool_value(result, mapping, self._config.limits, self._config.data_policy)

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

    async def _read_tool(self, session: ClientSession, identity: str) -> MCPEvidence:
        mapping = self._config.read
        assert mapping is not None
        result = await self._tool(session, mapping, identity)
        if mapping.result_format == "text":
            evidence = MCPEvidence(identity, identity, identity, result)
            return self._with_receipt(evidence, "tools/call", identity_verified=False)
        evidence = self._evidence(_path(result, mapping.items_path), mapping)
        if evidence.resource_id != identity:
            raise MCPSourceError("protocol")
        return self._with_receipt(evidence, "tools/call")

    async def read(self, resource_id: str) -> MCPEvidence:
        async def action(session: ClientSession) -> MCPEvidence:
            identity = _string(resource_id)
            if identity not in self._selected_ids:
                raise MCPSourceError("denied")
            if self._config.read is not None:
                return await self._read_tool(session, identity)
            if self._discovery is None or not self._discovery.resources:
                raise MCPSourceError("unsupported")
            # Read the wire URI before AnyUrl can normalize its spelling.
            result = await session.send_request(
                types.ClientRequest(
                    types.ReadResourceRequest(
                        params=types.ReadResourceRequestParams(uri=_resource_url(identity))
                    )
                ),
                types.Result,
            )
            text, mime_type = _resource_value(
                result, identity, self._config.limits, self._config.data_policy
            )
            return self._with_receipt(
                MCPEvidence(
                    identity,
                    identity,
                    self._resource_titles.get(identity, identity),
                    text,
                    mime_type,
                ),
                "resources/read",
            )

        return await self._run(action)
