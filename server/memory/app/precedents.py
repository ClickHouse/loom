"""Precedent answers for memory.ask.

A synthesized answer is a function of the question, the request shape and the evidence
the synthesizer read. Retrieval is deterministic, so when the same question with the
same shape retrieves the same window and every memory in it still carries the
content_sha256 it had when the answer was written, the synthesizer would read
byte-identical evidence and the earlier answer stands. That is the only condition under
which a precedent is served; every other case is a miss, and the fresh synthesized
answer overwrites the row.

This module is the orchestration memory.ask calls: reuse a precedent, or
schedule storing the fresh answer. The key, the evidence identity and the
validity checks are pure (`server.memory.domain.precedents`); ClickHouse access
lives in `server.memory.store.ask_precedents`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from server.memory.app import after_write
from server.memory.domain.precedents import (
    Evidence,
    Precedent,
    fast_eligibility,
    fast_namespaces,
    local_evidence,
    marks_block,
    merge,
    validity,
    window_identity,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Orchestration used by memory.ask. The repository is imported inside the
# functions.
# ---------------------------------------------------------------------------


async def reuse(
    key: str,
    *,
    org: str,
    namespace: str,
    window: list[Any],
    shape_sha256: str = "",
    max_age_hours: int = 0,
    retrieved_at: datetime | None = None,
) -> Precedent | None:
    """The stored precedent for `key` if the fresh `window` is byte-identical
    to the evidence it was written from; None on any miss or any error. A hit
    is recorded as a reuse (fire-and-forget) and, when `retrieved_at` is
    given, moves the row's retrieval time up to this retrieval: the window
    was just proven unchanged as of now, so the next ask can take the fast
    path against marks older than now."""
    import asyncio

    from server.memory.store import ask_precedents as repo

    try:
        prec = await repo.lookup(org, namespace, key)
        if prec is None:
            return None
        idents = window_identity(window, namespace)
        local = local_evidence(window)
        fresh = merge(await repo.evidence_for(org, idents), local)
    except Exception as e:  # noqa: BLE001 — a precedent read never blocks an answer
        log.warning("memory.ask precedent lookup failed (open): %s", e)
        return None
    why = validity(prec, fresh, window=idents + sorted(local.hashes), max_age_hours=max_age_hours)
    if why:
        log.debug("memory.ask precedent miss %s: %s", key[:12], why)
        return None
    after_write.schedule(
        repo.touch(
            org=org,
            namespace=namespace,
            prec=prec,
            shape_sha256=shape_sha256,
            retrieved_at=retrieved_at,
        ),
        name="memory.ask.precedent_touch",
    )
    await asyncio.sleep(0)
    return prec


async def fast_reuse(
    key: str,
    *,
    org: str,
    namespace: str,
    shape_sha256: str = "",
    max_age_hours: int = 1,
) -> Precedent | None:
    """The stored precedent for `key`, served BEFORE retrieval, when the row is
    eligible, no plane any of its namespaces reads has been written since its window was
    retrieved, the blacklist has not changed, and every memory row it cites is still
    live with the same hash. None on any miss or any error; the caller then retrieves
    and takes the post-retrieval precedent path, which may still serve the same row.
    Always None under the Postgres authority: the marks and the evidence are ClickHouse's,
    which can trail a delete or a label the authority holds."""
    import asyncio

    from server.core import namespace_write_marks as marks_repo
    from server.core import pg
    from server.memory.store import ask_precedents as repo

    if pg.enabled():
        return None
    try:
        prec = await repo.lookup(org, namespace, key)
        if prec is None:
            return None
        why = fast_eligibility(prec, max_age_hours=max_age_hours)
        if why:
            log.debug("memory.ask precedent fast miss %s: %s", key[:12], why)
            return None
        nss = fast_namespaces(prec, namespace)
        marks = await marks_repo.last_writes(org, nss)
        blacklist_at = await marks_repo.blacklist_updated_at()
        why = marks_block(prec, marks, namespaces=nss, blacklist_at=blacklist_at)
        if why:
            log.debug("memory.ask precedent fast miss %s: %s", key[:12], why)
            return None
        # The rows themselves, by key: a tombstone, a redaction or an edit
        # is a mutation the marks do not see. Local-evidence rows have no
        # key to re-read; their planes are covered by the marks above.
        idents = [(ns, k) for ns, k in prec.evidence.hashes if not ns.startswith("#")]
        local = Evidence({i: h for i, h in prec.evidence.hashes.items() if i[0].startswith("#")})
        fresh = merge(await repo.evidence_for(org, idents) if idents else Evidence({}), local)
    except Exception as e:  # noqa: BLE001 — a precedent read never blocks an answer
        log.warning("memory.ask precedent fast lookup failed (open): %s", e)
        return None
    why = validity(prec, fresh, window=idents + sorted(local.hashes), max_age_hours=0)
    if why:
        log.debug("memory.ask precedent fast miss %s: %s", key[:12], why)
        return None
    after_write.schedule(
        repo.touch(org=org, namespace=namespace, prec=prec, shape_sha256=shape_sha256),
        name="memory.ask.precedent_touch",
    )
    await asyncio.sleep(0)
    return prec


def schedule_store(
    key: str,
    *,
    org: str,
    namespace: str,
    question: str,
    shape_sha256: str,
    answer: str,
    cited_keys: list[str],
    window: list[Any],
    synth_model: str = "",
    retrieved_at: datetime | None = None,
) -> None:
    """Write the answer as a precedent, off the request path. The window is
    hashed NOW, after the answer; a memory edited between retrieval and this
    read leaves a hash the next lookup will not match, which is the safe
    direction. Nothing is stored when any window row is unhashed."""
    from server.memory.store import ask_precedents as repo

    idents = window_identity(window, namespace)
    local = local_evidence(window)
    if (not idents and not local.hashes) or not (answer or "").strip():
        return

    async def _bg() -> None:
        try:
            db = await repo.evidence_for(org, idents) if idents else Evidence({})
            evidence = merge(db, local)
            if not evidence.verifiable or set(db.hashes) != set(idents):
                log.debug(
                    "memory.ask precedent not stored: %s of %s memory rows hashed",
                    len(db.hashes),
                    len(idents),
                )
                return
            await repo.store(
                org=org,
                namespace=namespace,
                precedent_key=key,
                question=question,
                shape_sha256=shape_sha256,
                answer=answer,
                cited_keys=cited_keys,
                evidence=evidence,
                synth_model=synth_model,
                retrieved_at=retrieved_at,
            )
        except Exception as e:  # noqa: BLE001 — a precedent write never fails an answer
            log.warning("memory.ask precedent store failed (non-fatal): %s", e)

    after_write.schedule(_bg(), name="memory.ask.precedent_store")
