"""Native Bedrock transport with per-request SDK signing and cancellable HTTP I/O."""

from __future__ import annotations

import asyncio
import json
import math
import re
import threading
from collections.abc import Callable
from functools import lru_cache
from typing import Any, NamedTuple
from urllib.parse import quote

import httpx

from server.clients import _retry
from server.clients._http import get_model_client
from server.core.errors import GatewayRequiredError
from server.llm import customer
from server.settings import settings
from server.settings.catalog import url_host

_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_SIGN_LOCK = threading.Lock()


class _Connection(NamedTuple):
    session: Any
    endpoint: str
    region: str


class _Reply(NamedTuple):
    data: dict[str, Any]
    headers: dict[str, str]


def _record_route(region: str, model: str) -> None:
    try:
        from opentelemetry.trace import get_current_span

        get_current_span().set_attributes(
            {
                "cloud.provider": "aws",
                "cloud.region": region,
                "aws.bedrock.request.model_id": model,
            }
        )
    except Exception:  # noqa: BLE001 — route telemetry must not fail a model call
        pass


def model_id(model: str) -> str:
    """A routing prefix is local metadata, never part of the AWS model id."""
    value = model.removeprefix("bedrock:").strip()
    if not value or len(value) > 2048:
        raise ValueError("Bedrock requires a model id or inference profile of 1..2048 characters")
    return value


@lru_cache(maxsize=8)
def _connection(region: str) -> _Connection:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise RuntimeError("Native Bedrock needs the server's aws extra") from exc
    session = boto3.session.Session()
    # The SDK resolves the partition and endpoint, but sends no model request.
    client = session.client(
        "bedrock-runtime",
        region_name=region or None,
        config=Config(ignore_configured_endpoint_urls=True),
    )
    try:
        return _Connection(session, client.meta.endpoint_url, client.meta.region_name)
    finally:
        client.close()


def _signed(
    operation: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    region: str,
) -> tuple[str, bytes, dict[str, str]]:
    request_body = {key: value for key, value in payload.items() if key != "modelId"}
    body = (
        request_body["body"] if operation == "invoke_model" else json.dumps(request_body).encode()
    )
    suffix = "invoke" if operation == "invoke_model" else "converse"
    # Session mutation and credential refresh are serialized; HTTP requests are not.
    # A credential snapshot lives for this signature, never for the client's lifetime.
    with _SIGN_LOCK:
        connection = _connection(region)
        _record_route(connection.region, str(payload["modelId"]))
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.exceptions import NoCredentialsError

        credentials = connection.session.get_credentials()
        if credentials is None:
            raise NoCredentialsError()
        url = f"{connection.endpoint}/model/{quote(str(payload['modelId']), safe='')}/{suffix}"
        if url_host(url) not in settings.gateway_hosts():
            # Match the HTTP sender's privacy filter before signing those headers.
            private = {name.lower() for name, _ in settings.identity_headers()}
            headers = {
                name: value for name, value in headers.items() if name.lower() not in private
            }
        request = AWSRequest(
            method="POST",
            url=url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                **headers,
            },
        )
        SigV4Auth(credentials.get_frozen_credentials(), "bedrock", connection.region).add_auth(
            request
        )
        return url, body, dict(request.headers)


def _provider_error(response: httpx.Response) -> httpx.HTTPStatusError:
    code = response.headers.get("x-amzn-errortype", "ProviderError").partition(":")[0]
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code):
        code = "ProviderError"
    safe_response = httpx.Response(
        response.status_code,
        request=response.request,
        json={"error": {"code": code}},
    )
    return httpx.HTTPStatusError(
        "Bedrock model request failed",
        request=response.request,
        response=safe_response,
    )


async def _post(url: str, body: bytes, headers: dict[str, str], request_timeout_s: float) -> _Reply:
    async with get_model_client().stream(
        "POST",
        url,
        content=body,
        headers=headers,
        timeout=request_timeout_s,
        follow_redirects=False,
    ) as response:
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            if len(raw) + len(chunk) > _MAX_RESPONSE_BYTES:
                raise ValueError("Bedrock response exceeds the model response limit")
            raw.extend(chunk)
        if response.status_code >= 300:
            raise _provider_error(response)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Bedrock response must be an object")
        return _Reply(data, dict(response.headers))


async def call(
    operation: str,
    payload: dict[str, Any],
    who: customer.Customer,
    *,
    request_timeout_s: float,
    attempts: int,
    max_wait_s: float = 10.0,
    on_attempt: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if settings.gateway_required():
        raise GatewayRequiredError(
            "Native Bedrock cannot bypass the required inference gateway; "
            "use the gateway's configured model endpoint"
        )
    if operation not in {"converse", "invoke_model"}:
        raise ValueError("Unsupported native Bedrock operation")
    if not math.isfinite(request_timeout_s) or request_timeout_s <= 0:
        raise ValueError("Native Bedrock requests require a positive finite timeout")
    async with asyncio.timeout(request_timeout_s):
        async for attempt in _retry.retrying(attempts=attempts, max_wait_s=max_wait_s):
            with attempt:
                url, body, headers = await asyncio.to_thread(
                    _signed,
                    operation,
                    payload,
                    customer.headers(who),
                    settings.bedrock_region,
                )
                if on_attempt is not None:
                    on_attempt()
                reply = await _post(url, body, headers, request_timeout_s)
                if operation == "invoke_model":
                    return {"body": reply.data, "ResponseMetadata": {"HTTPHeaders": reply.headers}}
                return reply.data
    raise RuntimeError("Bedrock retry loop exited without a response")
