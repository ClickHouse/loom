"""A provider's HTTP error keeps the body's reason.

`httpx.Response.raise_for_status()` raises `Client error '400 Bad Request'
for url '…'` — the status line and the URL, nothing from the body. OpenAI
and Anthropic put the reason there (`error.code` / `error.type` /
`error.message`: `invalid_prompt`, `context_length_exceeded`,
`invalid_request_error` …), and by the time the exception reaches the ledger
(`bounded_error`) every quoted span is redacted, so the failure would read
`Client error '…' for url '…'` and say nothing about why.

`raise_for_status_with_detail` raises the same `httpx.HTTPStatusError` — same
`request`, same `response`, so every retry / outage predicate keeps working —
with a first line of the form

    400 Bad Request from openai:chat: code=invalid_prompt type=invalid_request_error msg=…

The fields are unquoted on purpose: the ledger's redaction keeps them.
"""

from __future__ import annotations

from typing import Any

import httpx

_MAX_DETAIL_CHARS = 160


def _error_fields(data: Any) -> dict[str, str]:
    """`code` / `type` / `msg` from an OpenAI- or Anthropic-shaped error body."""
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, str):
        return {"msg": err}
    if not isinstance(err, dict):
        return {}
    out: dict[str, str] = {}
    for key, name in (("code", "code"), ("type", "type"), ("message", "msg")):
        val = err.get(key)
        if isinstance(val, (str, int)) and str(val).strip():
            out[name] = " ".join(str(val).split())
    return out


def provider_error_detail(response: httpx.Response) -> str:
    """One bounded line naming the body's reason; '' when the body has none."""
    try:
        fields = _error_fields(response.json())
    except Exception:  # noqa: BLE001 — a non-JSON body has no fields
        fields = {}
    if not fields:
        return ""
    text = " ".join(f"{k}={v}" for k, v in fields.items())
    if len(text) > _MAX_DETAIL_CHARS:
        text = text[: _MAX_DETAIL_CHARS - 1] + "…"
    return text


def provider_error_code(exc: BaseException) -> str:
    """The body's `error.code` (else `error.type`) for a status error; ''
    otherwise. Low-cardinality: a metric label, not a message."""
    resp = getattr(exc, "response", None)
    if not isinstance(exc, httpx.HTTPStatusError) or resp is None:
        return ""
    try:
        fields = _error_fields(resp.json())
    except Exception:  # noqa: BLE001 — a non-JSON body has no code
        return ""
    code = fields.get("code") or fields.get("type") or ""
    return code[:64]


def raise_for_status_with_detail(response: httpx.Response, provider: str) -> None:
    """`response.raise_for_status()` whose error's first line carries the
    provider's own reason. A body without one raises the original."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = provider_error_detail(response)
        if not detail:
            raise
        first = f"{response.status_code} {response.reason_phrase} from {provider}: {detail}"
        raise httpx.HTTPStatusError(
            f"{first}\n{exc}", request=exc.request, response=exc.response
        ) from None
