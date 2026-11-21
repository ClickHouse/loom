"""Meter each real request while keeping provider credentials in the controller."""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from .spending import Spending

ATTRIBUTION_HEADERS = {
    "x-factory-org": "org",
    "x-factory-namespace": "namespace",
    "x-factory-feature": "feature",
    "x-factory-order": "order",
}


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Nonfinite JSON number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def text_request(payload):
    """The byte reservation covers text and custom tools, without paid side services."""
    if (
        not isinstance(payload, dict)
        or payload.get("cache_control") is not None
        or payload.get("service_tier") not in {None, "standard"}
        or payload.get("inference_geo") not in {None, "global"}
    ):
        raise ValueError("Request uses an unfrozen provider price category")
    blocks = []
    system = payload.get("system")
    if isinstance(system, list):
        blocks.extend(system)
    for message in payload.get("messages", []):
        content = message.get("content")
        if isinstance(content, list):
            blocks.extend(content)
    for tool in payload.get("tools", []):
        if tool.get("type") not in {None, "custom"} or tool.get("cache_control") is not None:
            raise ValueError("Hosted or cached tools have no frozen price")
    while blocks:
        block = blocks.pop()
        if (
            not isinstance(block, dict)
            or block.get("type")
            not in {"text", "tool_use", "tool_result", "thinking", "redacted_thinking"}
            or block.get("cache_control") is not None
        ):
            raise ValueError("Only uncached text blocks have a reserved input bound")
        if block["type"] == "tool_result" and isinstance(block.get("content"), list):
            blocks.extend(block["content"])


class Gateway:
    def __init__(self, spending: Spending, api_key: str, token: str, *, transport=None):
        if (
            not isinstance(api_key, str)
            or not api_key
            or not isinstance(token, str)
            or not token
            or api_key == token
        ):
            raise ValueError("Distinct upstream and private gateway credentials are required")
        self.spending, self.token = spending, token
        self.credentials = (api_key, token)
        self.client = httpx.Client(
            base_url="https://api.anthropic.com",
            headers={"x-api-key": api_key},
            timeout=httpx.Timeout(300, connect=5),
            transport=transport if transport is not None else httpx.HTTPTransport(retries=0),
            follow_redirects=False,
        )
        self.server = self.thread = None
        self.closed = False

    def contains_credential(self, value):
        if isinstance(value, str):
            return any(secret in value for secret in self.credentials)
        if isinstance(value, dict):
            return any(
                self.contains_credential(key) or self.contains_credential(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(self.contains_credential(item) for item in value)
        return False

    def authorize(self, headers):
        value = headers.get("x-api-key", "")
        if not isinstance(value, str) or not secrets.compare_digest(value, self.token):
            raise PermissionError("Gateway credential refused")
        if headers.get("anthropic-version") != "2023-06-01":
            raise ValueError("Freeze the Messages API version")
        if self.closed:
            raise PermissionError("Gateway is closed")

    def forward(self, payload, headers, *, raw_body=None):
        headers = {key.lower(): value for key, value in headers.items()}
        self.authorize(headers)
        if raw_body is None:
            raw_body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        else:
            parsed = decode(raw_body)
            if payload is not None and parsed != payload:
                raise ValueError("Request bytes differ from the parsed payload")
            payload = parsed
        role = headers.get("x-factory-role", "loom")
        text_request(payload)
        attribution = {
            field: headers[header]
            for header, field in ATTRIBUTION_HEADERS.items()
            if header in headers
        }
        if self.contains_credential(attribution) or self.contains_credential(
            headers.get("anthropic-beta")
        ):
            raise ValueError("Credentials cannot be used as attribution or feature flags")
        ticket = self.spending.reserve(
            payload, role=role, attribution=attribution, raw_input_bytes=len(raw_body)
        )
        try:
            forwarded = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
            if headers.get("anthropic-beta"):
                forwarded["anthropic-beta"] = headers["anthropic-beta"]
            response = self.client.post("/v1/messages", content=raw_body, headers=forwarded)
            response.raise_for_status()
            body = response.json()
            identifier = response.headers.get("request-id", "")
            if self.contains_credential(body) or self.contains_credential(identifier):
                raise ValueError("Provider response echoed a private credential")
            self.spending.settle(ticket, body, identifier)
            return response
        except BaseException as error:  # Cancellation also leaves a potentially billed dispatch.
            try:
                self.spending.unknown(ticket, type(error).__name__)
            except (OSError, ValueError):
                pass
            raise

    def start(self, *, host="0.0.0.0", port=0):
        if self.closed or self.server is not None:
            raise RuntimeError("Start a gateway exactly once")
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                try:
                    headers = {key.lower(): value for key, value in self.headers.items()}
                    if any(
                        len(self.headers.get_all(name, [])) > 1
                        for name in (
                            "Content-Length",
                            "X-Api-Key",
                            "X-Factory-Role",
                            "Anthropic-Version",
                        )
                    ):
                        raise ValueError("Ambiguous gateway headers")
                    gateway.authorize(headers)
                    size = int(self.headers.get("Content-Length", "0"))
                    if (
                        self.path != "/v1/messages"
                        or not 0 < size <= 8 * 1024 * 1024
                        or self.headers.get("Transfer-Encoding")
                    ):
                        raise ValueError("Unsupported gateway request")
                    self.connection.settimeout(5)
                    raw = self.rfile.read(size)
                    if len(raw) != size:
                        raise ValueError("Incomplete gateway request")
                    response = gateway.forward(None, headers, raw_body=raw)
                    body = response.content
                    self.send_response(response.status_code)
                    self.send_header("Content-Type", "application/json")
                    if response.headers.get("request-id"):
                        self.send_header("request-id", response.headers["request-id"])
                except Exception as error:  # noqa: BLE001 — errors must not expose prompts, provider payloads or credentials
                    body = json.dumps({"error": {"type": type(error).__name__}}).encode()
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # The provider receipt or unsettled reservation is already durable.

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server.server_address[1]

    def close(self):
        self.closed = True
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
            self.server = None
        self.client.close()
