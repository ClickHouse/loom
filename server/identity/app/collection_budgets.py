"""Spend policy for grant-authorized collections, stored in their directory row."""

from __future__ import annotations

from typing import Any

from server.auth import sharing
from server.auth.authz import identity_write
from server.auth.grant_claims import account_change
from server.core.ch import get_client, own_write_reads
from server.core.datetimes import naive_utc_now_seconds
from server.core.errors import ValidationError
from server.identity.api.models_collections import (
    CollectionBudgetSetInput,
    CollectionBudgetSetOutput,
)
from server.identity.store import collections as collections_repo
from server.identity.store import namespaces as namespaces_repo
from server.tasks.app.budget_notifications import inbox_owned_by

_FIELDS = ("daily_cost_cap_usd", "daily_user_cost_cap_usd", "budget_alert_inbox")


def _patch(input: CollectionBudgetSetInput) -> dict[str, Any]:
    patch = {key: getattr(input, key) for key in _FIELDS if key in input.model_fields_set}
    if not patch or any(value is None for value in patch.values()):
        raise ValidationError("supply at least one budget field; null does not change a policy")
    return patch


async def handle_collection_budget_set(
    input: CollectionBudgetSetInput,
) -> CollectionBudgetSetOutput:
    """The transport checks ADMIN on this collection before its policy is read or changed."""
    org, namespace = input.org, input.namespace
    patch = _patch(input)
    if not sharing.is_collection(namespace) or sharing.is_personal(namespace):
        raise ValidationError("budget settings require a non-personal collection")
    ch = get_client()
    async with account_change(org, namespace):
        with own_write_reads():
            row = await namespaces_repo.fetch_row(org, namespace, ch=ch)
            live = await collections_repo.fetch(org, namespace, ch=ch)
        if not row or row.get("kind") != "collection" or not row.get("active") or not live:
            raise ValidationError("budget settings require an active collection")
        inbox = patch.get("budget_alert_inbox")
        if inbox and not await inbox_owned_by(org, namespace, inbox):
            raise ValidationError(
                "budget_alert_inbox must name an existing task inbox owned by this collection"
            )
        changed = any(row.get(key) != value for key, value in patch.items())
        if changed:
            row = {**row, **patch, "updated_at": max(naive_utc_now_seconds(), row["updated_at"])}
            with identity_write():
                await ch.insert("loom.namespaces", [row])
    return CollectionBudgetSetOutput(
        namespace=namespace, changed=changed, **{key: row[key] for key in _FIELDS}
    )
