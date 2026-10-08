"""Hydrate a bounded tracker rebuild page through the namespace's source gate."""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from server.insights.app.provider_rebuild_batch import RebuildBatchError
from server.tracker.app.embed_index import embed_text, text_sha256
from server.tracker.app.source import tracker_source


def _groups(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str, str], list[int]]:
    groups = defaultdict(list)
    for index, row in enumerate(rows):
        if row.get("tombstoned_at") is not None:
            continue
        if (
            any(
                not isinstance(row.get(key), str) or not row[key]
                for key in ("org", "namespace", "source_id")
            )
            or row.get("kind") not in ("issue", "pr")
            or type(row.get("number")) is not int
            or row["number"] <= 0
        ):
            raise RebuildBatchError("tracker_source_identity_invalid")
        groups[(row["org"], row["namespace"], row["kind"])].append(index)
    return groups


async def hydrate_tracker_page(
    rows: Sequence[Mapping[str, Any]], *, body_chars: int, reconcile: bool = False
) -> dict[int, str]:
    """Return text by page position, matched by tenant, kind, source ID and digest.

    Source reads retain the tracker namespace's privacy and credential gates.
    A missing or changed source cannot silently replace the stored text digest;
    those rows need reconciliation before a metadata-preserving rebuild. Deleted
    rows need no source read and never expose their former text to a provider.
    """
    if len(rows) > 64 or type(body_chars) is not int or body_chars < 0:
        raise RebuildBatchError("tracker_hydration_bounds_invalid")
    groups = _groups(rows)
    result = {}
    for (org, namespace, kind), indexes in groups.items():
        try:
            items = await tracker_source(org, namespace).embed_items_by_number(
                org,
                namespace,
                kind=kind,
                numbers=sorted({rows[index]["number"] for index in indexes}),
                body_chars=body_chars,
            )
        except Exception:  # noqa: BLE001 — never expose upstream source text or credentials in status
            raise RebuildBatchError("tracker_source_unavailable") from None
        by_identity = {}
        for item in items:
            identity = (item.get("kind"), item.get("number"), item.get("source_id"))
            if identity in by_identity:
                raise RebuildBatchError("tracker_source_ambiguous")
            by_identity[identity] = item
        for index in indexes:
            row = rows[index]
            matched = by_identity.get((kind, row["number"], row["source_id"]))
            if matched is None:
                raise RebuildBatchError("tracker_source_missing")
            if not isinstance(matched.get("title"), str) or not isinstance(
                matched.get("body"), str
            ):
                raise RebuildBatchError("tracker_source_text_invalid")
            text = embed_text(matched["title"], matched["body"], body_chars=body_chars)
            if not reconcile and text_sha256(text) != row.get("text_sha256"):
                raise RebuildBatchError("tracker_source_changed")
            result[index] = text
    return result


async def reconcile_tracker_page(
    rows: Sequence[Mapping[str, Any]], *, body_chars: int
) -> tuple[list[dict[str, Any]], dict[int, str]]:
    """Refresh the digest with the exact authorized text sent to the target provider.

    Source ownership and deletion markers stay frozen. Only a present, uniquely
    matched source can refresh text; missing/private/deleted upstream records do
    not authorize embedding a guessed body or reviving a snapshot tombstone.
    """
    texts = await hydrate_tracker_page(rows, body_chars=body_chars, reconcile=True)
    refreshed = [dict(row) for row in rows]
    for index, text in texts.items():
        refreshed[index]["text_sha256"] = text_sha256(text)
    return refreshed, texts
