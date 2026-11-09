"""The kind of every namespace in an org, one statement per org, cached.

A search that fans out over "all my namespaces" spans the
memory-kind namespaces. Code and QA indexes (and bench corpora) are
namespaces too — `loom.namespaces.kind` says which — but they are addressed
by name or by their own tools (`code.*`), never swept for a personal fact.
Sweeping them would turn one `loom_search` over a few dozen namespaces into
hundreds of ClickHouse statements and seconds of reads.

Fail-open: an org whose directory cannot be read has no kinds but its configured
repositories', and a namespace without a kind row counts as memory — the sweep can
only narrow when the directory or the repository config says so. The cache mirrors
`namespace_classification.restricted_set`: `_TTL_S` seconds per org.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable

log = logging.getLogger(__name__)

_TTL_S = 30.0
_cache: dict[str, tuple[float, dict[str, str]]] = {}


def reset_cache() -> None:
    """Tests only."""
    _cache.clear()


def configured_kinds(org: str) -> dict[str, str]:
    """`code` for each repository the code config lists in `org` (`config_entries`):
    one with no directory row is a repository all the same."""
    from server.code.domain.index.repos import config_entries

    return {r.namespace: "code" for r in config_entries() if r.org == org}


async def kind_map(org: str) -> dict[str, str]:
    """`{namespace: kind}` for `org`, from the directory over `configured_kinds`; the
    configured kinds alone when the directory is unreadable."""
    now = time.monotonic()
    hit = _cache.get(org)
    if hit is not None and hit[0] > now:
        return hit[1]
    from server.core.ch import get_client

    client = get_client()
    if getattr(client, "_client", "absent") is None:
        return configured_kinds(org)
    try:
        rows = await client.query_all(
            """
            SELECT namespace, kind
            FROM loom.namespaces FINAL
            WHERE org = {org:String}
            """,
            params={"org": org},
        )
    except Exception as e:  # noqa: BLE001 — fail open: no kinds, no narrowing
        if hit is not None:
            log.warning(
                "namespace kind lookup failed for org %s; serving the last snapshot: %s", org, e
            )
            return hit[1]
        log.warning("namespace kind lookup failed for org %s; no narrowing: %s", org, e)
        return configured_kinds(org)
    out = configured_kinds(org)
    out.update((str(r.get("namespace") or ""), str(r.get("kind") or "")) for r in rows or [])
    out.pop("", None)
    _cache[org] = (now + _TTL_S, out)
    return out


def keep_kinds(namespaces: Iterable[str], kinds: dict[str, str], keep: frozenset[str]) -> list[str]:
    """`namespaces` in order, minus those whose kind is known and not in
    `keep`. An unknown or empty kind stays (fail-open)."""
    return [ns for ns in namespaces if (kinds.get(ns) or "") in keep or not (kinds.get(ns) or "")]


def fanout_kinds(raw: str | None = None) -> frozenset[str]:
    """The kinds a fan-out spans (`raw`, else `memory_search_fanout_kinds`;
    a comma list, empty = every kind)."""
    from server.settings import settings

    raw = str(settings.memory_search_fanout_kinds) if raw is None else raw
    return frozenset(k.strip() for k in raw.split(",") if k.strip())
