"""Provider proof is checked over raw bytes and the signed provider scope."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from server.knowledge.domain import source_push as push


def signed(body: dict[str, object], *, prefix: str = "") -> tuple[bytes, str]:
    raw = json.dumps(body, indent=2).encode()
    return raw, prefix + hmac.new(b"secret", raw, hashlib.sha256).hexdigest()


def test_linear_requires_signed_timestamp_and_organization() -> None:
    raw, signature = signed({"organizationId": "org", "webhookTimestamp": 1_000_000})
    push.verify_linear(raw, signature, "secret", "org", now=1000)
    for kwargs in (
        {"now": 1061, "organization_id": "org"},
        {"now": 1000, "organization_id": "other"},
    ):
        with pytest.raises(push.DeliveryRefused):
            push.verify_linear(raw, signature, "secret", **kwargs)
    with pytest.raises(push.DeliveryRefused):
        push.verify_linear(raw + b" ", signature, "secret", "org", now=1000)


@pytest.mark.parametrize("stamp", [True, "1000000", None, float("nan"), float("inf"), 10**1000])
def test_linear_refuses_invalid_signed_clock(stamp: object) -> None:
    raw, signature = signed({"organizationId": "org", "webhookTimestamp": stamp})
    with pytest.raises(push.DeliveryRefused):
        push.verify_linear(raw, signature, "secret", "org", now=1000)


def test_notion_checks_raw_body_workspace_and_prefix() -> None:
    raw, signature = signed({"workspace_id": "space"}, prefix="sha256=")
    push.verify_notion(raw, signature, "secret", "space")
    for body, proof, workspace in (
        (raw + b" ", signature, "space"),
        (raw, signature[7:], "space"),
        (raw, signature, "other"),
    ):
        with pytest.raises(push.DeliveryRefused):
            push.verify_notion(body, proof, "secret", workspace)


def test_drive_requires_whole_active_channel_binding() -> None:
    channel = {"id": "id", "token": "secret", "resource_id": "resource", "expiration": 1100}
    headers = {
        "x-goog-channel-id": "id",
        "x-goog-channel-token": "secret",
        "x-goog-resource-id": "resource",
        "x-goog-resource-state": "change",
    }
    assert push.verify_drive(headers, [channel], now=1000)
    assert not push.verify_drive({**headers, "x-goog-resource-state": "sync"}, [channel], now=1000)
    for key in headers:
        with pytest.raises(push.DeliveryRefused):
            push.verify_drive({**headers, key: "wrong"}, [channel], now=1000)
    with pytest.raises(push.DeliveryRefused):
        push.verify_drive(headers, [channel], now=1100)


def test_fingerprint_ignores_mapping_order_but_binds_credentials_and_items() -> None:
    kwargs = {"credential": "key", "items": [{"id": "one", "type": "file"}], "config": {}}
    first = push.fingerprint(**kwargs)
    assert first == push.fingerprint(**{**kwargs, "items": [{"type": "file", "id": "one"}]})
    assert first != push.fingerprint(**{**kwargs, "credential": "other"})
    assert first != push.fingerprint(**{**kwargs, "items": []})
