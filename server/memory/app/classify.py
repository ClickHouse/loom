"""Pluggable per-namespace classifier hook.

When a namespace sets `classify_endpoint`, memory.set with `classify=True`
POSTs the value to that URL and uses the response as memory_type. A namespace
admin owns the URL, the model and the prompt; Loom owns the data plane, and
checks the URL at registration and at every call (`classify_refusal`).

A URL through the gateway (`classify_route`: the gateway is required, or the host
is one of its listed hosts) is a model call, on the model client with the
call's identity headers. Any other is posted on its own client to the public
address its host was vetted to, with no redirect, no identity headers and a
capped, uncompressed answer, all under the classifier's deadline.

Contract — request body:
{
  "value": "<the memory value as JSON-encoded string>",
  "prompt_template": "<the namespace's prompt_template>",
  "valid_types": ["observation", "decision", ...]   // the namespace's
                                                       tag_registry, or
                                                       empty
}

Contract — response body:
{
  "memory_type": "observation",   // one of valid_types, or empty
  "confidence": 0.92               // optional; Loom ignores it
}

Fail-open: any error returns empty string; caller's original
memory_type wins. A `customer.HOLDS` exception (the gateway refused or held the
call's identity) passes through instead: the write fails, not "kept the type".
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any

import httpx

from server.clients import _http
from server.core import webhooks
from server.llm import customer
from server.memory.app.log_once import LogOnce
from server.memory.domain.classify_endpoints import classify_refusal, classify_route
from server.observability import degradations as _degradations
from server.observability import provider_health as _provider_health
from server.settings import settings

log = logging.getLogger(__name__)

# Namespaces whose classify_endpoint, stored or its host at the call, is refused:
# logged once per process, as it is read again at every memory.set(classify=true).
_REFUSED_LOGGED = LogOnce()


def _timeout() -> httpx.Timeout:
    # not a setting: the connect, write and pool waits; the read is the setting
    return httpx.Timeout(connect=2.0, read=settings.memory_classify_timeout_s, write=2.0, pool=2.0)


async def classify(
    *,
    org: str,
    namespace: str,
    endpoint: str,
    prompt_template: str,
    value: str,
    valid_types: list[str],
) -> str:
    """Call the namespace's classifier. Returns "" on any error or
    when the response's memory_type isn't in valid_types (when
    valid_types is non-empty).
    """
    endpoint = endpoint.strip()
    refusal, via_gateway = classify_route(endpoint)
    if not endpoint or refusal:
        # Refused (see classify_route): no call; the caller's type stays.
        return ""
    body = {
        "value": value,
        "prompt_template": prompt_template,
        "valid_types": valid_types,
    }
    # Outside the fail-open: a refused call is an error, not a kept type.
    who = await customer.require()
    try:
        if via_gateway:
            # The model client, never redirected (the URL is a namespace's): its hook refuses
            # a host off the gateway, sends the identity headers only to the gateway's hosts,
            # and reads a gateway's refusal of the call's identity (server/clients/_http.py).
            resp = await _http.get_model_client().post(
                endpoint,
                json=body,
                headers=customer.headers(who),
                timeout=_timeout(),
                follow_redirects=False,
            )
            resp.raise_for_status()
            payload = resp.json()
        else:
            payload = await _post_vetted(endpoint, body, scope=(org, namespace))
    except _NotCalled:
        return ""  # noted and logged where it was refused
    except customer.HOLDS:
        raise  # the gateway refused or held the call's identity: never "kept the caller's type"
    except Exception as e:  # noqa: BLE001 — a failed classifier keeps the caller's type
        log.warning("classify.call failed (%s); falling back to caller's type", e)
        # Only the kind of failure: the namespace admin chose the URL, and the text of a
        # failure (a refused port, a status line) would map what answers there.
        _degradations.note("classify", "caller_type_kept", type(e).__name__)
        return ""
    if not isinstance(payload, dict):
        _degradations.note("classify", "caller_type_kept", "malformed payload")
        return ""
    candidate = str(payload.get("memory_type") or "").strip()
    if not candidate:
        _degradations.note("classify", "caller_type_kept", "empty memory_type")
        return ""
    if valid_types and candidate not in valid_types:
        log.warning(
            "classify.call returned %r not in registered types %r; ignoring",
            candidate,
            valid_types,
        )
        _degradations.note("classify", "caller_type_kept", f"{candidate!r} not a registered type")
        return ""
    _provider_health.record_success("classify")
    return candidate


class _NotCalled(Exception):
    """The call is refused before it is sent (its host): kept apart from any answer,
    a JSON null included."""


# The largest classifier answer read: {"memory_type": ...} is a few dozen bytes.
_MAX_ANSWER_BYTES = 64 * 1024  # not a setting: bounds what an admin's endpoint can send


async def _post_vetted(endpoint: str, body: Mapping[str, object], *, scope: tuple[str, str]) -> Any:
    """POST to a classifier off the gateway and return its decoded answer: to the public
    address its host resolves to (vetted here and connected to, so a rebinding name
    cannot move it), with no redirect and no identity headers. The lookup, the call
    and the answer, at most `_MAX_ANSWER_BYTES`, are under one deadline. _NotCalled,
    noted, when the host is refused (logged once per `scope`, the (org, namespace)).
    The name is looked up on the event loop, so a name that never answers holds
    nothing past the deadline: no thread is left for a tenant's names to fill."""
    async with asyncio.timeout(settings.memory_classify_timeout_s):
        refusal, url, host, server_name = await webhooks.pinned_target(
            # http only for a listed host: classify_refusal refused it for any other
            endpoint,
            schemes=("https", "http"),
            private_hosts=settings.memory_classify_private_host_set(),
            in_loop=True,
        )
        if refusal:
            if _REFUSED_LOGGED.first(("host", *scope)):
                log.warning(
                    "%s/%s: its classify_endpoint's host is refused (%s): not classified",
                    *scope,
                    refusal,
                )
            _degradations.note("classify", "caller_type_kept", "endpoint host refused")
            raise _NotCalled
        async with (
            httpx.AsyncClient(follow_redirects=False, verify=webhooks.tls()) as client,
            client.stream(
                "POST",
                url,
                json=body,
                # The cap counts bytes on the wire: a compressed answer would inflate in
                # one step before any cap is seen.
                headers={"Accept-Encoding": "identity", **({"Host": host} if host else {})},
                timeout=_timeout(),
                extensions={"sni_hostname": server_name} if server_name else {},
            ) as resp,
        ):
            resp.raise_for_status()
            if resp.headers.get("content-encoding", "identity").strip().lower() != "identity":
                raise ValueError("the classifier's answer is compressed")
            answer = bytearray()
            async for chunk in resp.aiter_raw():
                answer += chunk
                if len(answer) > _MAX_ANSWER_BYTES:
                    raise ValueError("the classifier's answer is over 64 KiB")
    return json.loads(answer)


async def get_namespace_classify_config(
    org: str,
    namespace: str,
) -> tuple[str, str]:
    """Look up (classify_endpoint, classify_prompt_template). Empty
    when not configured."""
    from server.core.ch import get_client

    try:
        row = await get_client().query_one(
            """
            SELECT
                ifNull(classify_endpoint, '') AS classify_endpoint,
                ifNull(classify_prompt_template, '') AS classify_prompt_template
            FROM loom.namespaces FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
            LIMIT 1
            """,
            params={"org": org, "ns": namespace},
        )
    except Exception as e:  # noqa: BLE001 — a failed config read means no classifier
        log.warning("classify config lookup failed: %s", e)
        return "", ""
    if not row:
        return "", ""
    endpoint = row.get("classify_endpoint") or ""
    refusal = classify_refusal(endpoint)
    if refusal:
        # Stored before a rule that refuses it (the gateway, https only): memory content
        # never goes there, and the namespace has no classifier until it is changed.
        if _REFUSED_LOGGED.first((org, namespace)):
            log.warning(
                "%s/%s: its classify_endpoint is refused (%s): not classified",
                org,
                namespace,
                refusal,
            )
        return "", ""
    return (endpoint, row.get("classify_prompt_template") or "")
