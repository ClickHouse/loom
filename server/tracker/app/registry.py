"""Directory rows for the tracker namespaces.

A tracker namespace is a directory row and nothing else — its data is read live
from the tracker source. The code registry's shape
(code/app/index/registry.py): insert a row once per process when none
exists — kind='qa', tags ['qa'], private, and `classification` as
configured (a restricted tracker namespace carries private-repository issues and
PRs; the restricted-namespace gate covers it). An existing row is narrowed to restricted
when the config says so and never widened. The leader runs
`ensure_configured_tracker_namespaces` at boot so the row exists before the
first read (or, when its claim stays busy past the wait, by the background retry).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("loom.tracker.registry")

_DONE: set[tuple[str, str]] = set()


async def ensure_tracker_registered(
    org: str,
    namespace: str,
    *,
    classification: str = "standard",
    ch: Any | None = None,
    join_policy: str = "",
) -> bool:
    """The caller holds the namespace's account claim."""
    key = (org, namespace)
    if key in _DONE:
        return False
    from server.core.ch import get_client

    client = ch or get_client()
    restricted = classification == "restricted"
    from server.identity.app.registration import namespace_row

    row = await namespace_row(org, namespace, ch=client)
    now = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    if row is not None:
        if restricted and str(row.get("classification") or "standard") != "restricted":
            narrowed = dict(row)
            narrowed.update(classification="restricted", visibility="private", updated_at=now)
            await client.insert("loom.namespaces", [narrowed])
            from server.identity.app.namespace_classification import note_restricted

            note_restricted(org, namespace)
            logger.warning("qa namespace %s/%s narrowed to restricted", org, namespace)
        _DONE.add(key)
        return False
    await client.insert(
        "loom.namespaces",
        [
            {
                "org": org,
                "namespace": namespace,
                "owner_email": "",
                "description": (
                    "tracker record, read live (issues, PRs, triage, risk; "
                    "read-only reference data)"
                ),
                "tags": ["tracker"],
                "active": True,
                "kind": "tracker",
                "visibility": "private",
                "classification": "restricted" if restricted else "standard",
                # A public tracker namespace is joined, not granted — any
                # principal in the org joins it directly; a restricted one asks
                # (a registrant may choose `approval` for a public one)
                "join_policy": "approval" if restricted else (join_policy or "open"),
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    if restricted:
        from server.identity.app.namespace_classification import note_restricted

        note_restricted(org, namespace)
    _DONE.add(key)
    logger.info("registered qa namespace %s/%s (classification=%s)", org, namespace, classification)
    return True


async def ensure_configured_tracker_namespaces(*, ch: Any | None = None) -> list[str]:
    """Directory rows for every namespace in the QA configuration.
    Best effort per namespace; returns the ones registered this call."""
    from server.auth.grant_claims import account_change
    from server.tracker.domain.namespaces import configured_namespaces
    from server.tracker.store.source import qa_intel

    if not qa_intel.configured():
        return []
    done: list[str] = []
    for ns, cfg in sorted(configured_namespaces().items()):
        if (cfg.org, ns) in _DONE:
            continue
        classification = "restricted" if cfg.restricted else "standard"
        try:
            # The claim every writer of the directory row holds, as `tracker.source.add` does.
            async with account_change(cfg.org, ns):
                if await ensure_tracker_registered(
                    cfg.org, ns, classification=classification, ch=ch
                ):
                    done.append(ns)
        except Exception as e:  # noqa: BLE001 — the directory is a convenience at boot
            logger.warning("qa namespace registration skipped for %s: %s", ns, e)
    return done


def configured_pending() -> bool:
    """Whether a configured namespace is not yet registered by this process."""
    from server.tracker.domain.namespaces import configured_namespaces
    from server.tracker.store.source import qa_intel

    return qa_intel.configured() and any(
        (cfg.org, ns) not in _DONE for ns, cfg in configured_namespaces().items()
    )


def reset_for_tests() -> None:
    _DONE.clear()
