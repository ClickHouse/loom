"""Which repositories' memory links trail their indexed head, as `/v1/health` reads it.

The link pass follows a repository's links to its indexed head (`code_link_recheck`).
While a recheck keeps failing or is cut, the links stay at an older commit and reads go
on serving their states. Each process keeps when it first saw a repository's links
behind (`refresh`, on the code_repos refresh timer); the `memory_links_behind` health
problem reads that snapshot (`behind`) and never ClickHouse.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from server.code.app import memory_links as code
from server.code.domain.index.repos import registry_unread
from server.memory.store import code_links as store
from server.settings import settings

log = logging.getLogger(__name__)

# (org, code namespace) -> (the head its links were last checked at, when this process
# first saw the indexed head past it, on time.monotonic()). None until the first read,
# and while nothing follows the links.
_TRAILING: dict[tuple[str, str], tuple[str, float]] | None = None


async def refresh(ch: Any | None = None) -> None:
    """Re-read which repositories holding a live link have an indexed head their links
    were not checked at: one small read of the two watermarks, then the link table's
    rows of the repositories where they differ. A repository keeps the time it was first
    seen behind while its links stay at the same commit; one whose links moved on, even
    to a head the index has passed again, starts over. With linking off nothing follows
    the links, by configuration, and nothing is reported; with no repository configured
    nothing is read. Until the registry is first read the snapshot stays as it was.
    Never raises: a failed read keeps the last snapshot."""
    global _TRAILING
    if not settings.memory_code_links_enabled:
        _TRAILING = None
        return
    if registry_unread():
        return
    if not code.code_namespaces():
        _TRAILING = {}  # no repository is indexed: nothing to read
        return
    try:
        unchecked = await code.unchecked_heads(ch)
        linked = set(await store.code_namespaces(sorted(unchecked), ch=ch)) if unchecked else set()
    except Exception as e:  # noqa: BLE001 — the health signal keeps its last reading
        log.warning("memory links: lag not re-read: %s", e)
        return
    seen, now = _TRAILING or {}, time.monotonic()
    _TRAILING = {
        entry: (at, seen[entry][1] if entry in seen and seen[entry][0] == at else now)
        for entry, at in unchecked.items()
        if entry in linked
    }


def behind() -> int | None:
    """How many repositories' memory links have trailed their indexed head for longer
    than a link pass may take (its interval plus its deadline), as this process saw
    them; None until it first read them, and with linking off."""
    if _TRAILING is None:
        return None
    grace = (
        settings.memory_code_links_interval_seconds + settings.memory_code_links_max_pass_seconds
    )
    now = time.monotonic()
    return sum(1 for _, since in _TRAILING.values() if now - since > grace)


def reset_for_tests() -> None:
    global _TRAILING
    _TRAILING = None
