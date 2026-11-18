"""Provider authentication for source sync hints; no payload becomes content."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import dataclass, field
from typing import Any

MAX_BODY_BYTES = 65_536  # not a setting: the callback wire limit
MAX_CHANNELS = 20  # not a setting: bounded provider requests per source
SECRET_PURPOSE = "knowledge_source_push.secret"


def writer_lease_name(org: str, namespace: str) -> str:
    return "knowledge-push.writer/" + json.dumps([org, namespace], separators=(",", ":"))


@dataclass(frozen=True)
class PushRegistration:
    org: str
    namespace: str
    name: str
    source_id: str
    provider: str
    generation: str
    fingerprint: str
    provider_id: str = ""
    status: str = "pending"
    secret_enc: str = field(default="", repr=False)
    agent: str = "-"
    user_id: str = "-"
    session_id: str = "-"
    actor: str = ""


class DeliveryRefused(ValueError):
    """Missing or unverifiable provider proof; never include the proof in errors."""


def payload(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_BODY_BYTES:
        raise DeliveryRefused("delivery too large")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise DeliveryRefused("invalid delivery") from None
    if not isinstance(data, dict):
        raise DeliveryRefused("invalid delivery")
    return data


def fingerprint(*, credential: str, items: list[dict[str, Any]], config: dict[str, Any]) -> str:
    """Bind setup to credential bytes, unaffected by at-rest key rotation."""
    raw = json.dumps([credential, items, config], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def challenge_fingerprint(challenge: str) -> str:
    return hashlib.sha256(challenge.encode()).hexdigest()


def _signature(raw: bytes, signature: str, secret: str, *, prefix: str = "") -> None:
    if not secret or len(raw) > MAX_BODY_BYTES:
        raise DeliveryRefused("unverifiable delivery")
    expected = prefix + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.encode(), expected.encode()):
        raise DeliveryRefused("unverifiable delivery")


def verify_linear(
    raw: bytes, signature: str, secret: str, organization_id: str, *, now: float
) -> None:
    _signature(raw, signature, secret)
    body = payload(raw)
    stamp = body.get("webhookTimestamp")
    try:
        seconds = float(stamp) / 1000 if isinstance(stamp, (int, float)) else math.nan
    except OverflowError:
        seconds = math.nan
    if (
        isinstance(stamp, bool)
        or not isinstance(stamp, (int, float))
        or not math.isfinite(seconds)
        or abs(now - seconds) > 60
        or not organization_id
        or body.get("organizationId") != organization_id
    ):
        raise DeliveryRefused("unverifiable delivery")


def verify_notion(raw: bytes, signature: str, secret: str, workspace_id: str) -> None:
    _signature(raw, signature, secret, prefix="sha256=")
    body = payload(raw)
    if not workspace_id or body.get("workspace_id") != workspace_id:
        raise DeliveryRefused("unverifiable delivery")


def verify_drive(headers: dict[str, str], channels: list[dict[str, Any]], *, now: float) -> bool:
    """Whether a verified channel requests work; early sync needs no active channel."""
    for channel in channels:
        token = channel.get("token")
        if (
            isinstance(token, str)
            and token
            and headers.get("x-goog-channel-id") == channel.get("id")
            and headers.get("x-goog-resource-id") == channel.get("resource_id")
            and hmac.compare_digest(
                headers.get("x-goog-channel-token", "").encode(), token.encode()
            )
            and float(channel.get("expiration", 0)) > now
        ):
            state = headers.get("x-goog-resource-state")
            if state not in {"sync", "change"}:
                raise DeliveryRefused("unverifiable delivery")
            return state == "change"
    raise DeliveryRefused("unverifiable delivery")
