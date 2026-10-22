"""Pluggable per-namespace classifier hook.

Mirror of `server/memory/app/rerank.py`. When a namespace sets
`classify_endpoint`, memory.set with `classify=True` POSTs the value
to that URL and uses the response as memory_type. Operator owns the
model + prompt; Loom owns the data plane.

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
memory_type wins.
"""

from __future__ import annotations

import logging

import httpx

from server.clients import _http
from server.llm import customer
from server.memory.app.log_once import LogOnce
from server.memory.domain.classify_endpoints import classify_refusal
from server.observability import degradations as _degradations
from server.observability import provider_health as _provider_health
from server.settings import settings

log = logging.getLogger(__name__)

# Namespaces whose stored classify_endpoint is refused, logged once per process.
_OFF_GATEWAY_LOGGED = LogOnce()


def _timeout() -> httpx.Timeout:
    # not a setting: the connect, write and pool waits; the read is the setting
    return httpx.Timeout(connect=2.0, read=settings.memory_classify_timeout_s, write=2.0, pool=2.0)


async def classify(
    *,
    endpoint: str,
    prompt_template: str,
    value: str,
    valid_types: list[str],
) -> str:
    """Call the operator's classifier. Returns "" on any error or
    when the response's memory_type isn't in valid_types (when
    valid_types is non-empty).
    """
    if not endpoint or classify_refusal(endpoint):
        # Off the gateway: no call; the caller's type stays.
        return ""
    body = {
        "value": value,
        "prompt_template": prompt_template,
        "valid_types": valid_types,
    }
    # Outside the fail-open: a refused call is an error, not a kept type.
    headers = customer.headers(await customer.require())
    try:
        # The model client, never redirected (the URL is a namespace's): its hook refuses
        # a host off the gateway, sends the identity headers only to the gateway's hosts,
        # and reads a gateway's refusal of the call's identity (server/clients/_http.py).
        resp = await _http.get_model_client().post(
            endpoint, json=body, headers=headers, timeout=_timeout(), follow_redirects=False
        )
        resp.raise_for_status()
        payload = resp.json()
    except customer.HOLDS:
        raise  # the gateway refused or held the call's identity: never "kept the caller's type"
    except Exception as e:  # noqa: BLE001 — a failed classifier keeps the caller's type
        log.warning("classify.call failed (%s); falling back to caller's type", e)
        _degradations.note("classify", "caller_type_kept", e)
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
        # Stored before the gateway was required: memory content never goes
        # there, and the namespace has no classifier until it is changed.
        if _OFF_GATEWAY_LOGGED.first((org, namespace)):
            log.warning(
                "%s/%s: its classify_endpoint is refused while the gateway is required "
                "(%s): not classified",
                org,
                namespace,
                refusal,
            )
        return "", ""
    return (endpoint, row.get("classify_prompt_template") or "")
