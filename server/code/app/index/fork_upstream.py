"""A fork resolves to its upstream's index.

`code.repos` with `repos` candidates (the checkout's remotes) that match no
readable entry asks GitHub, for the first few candidates, whether each is a
fork, and retries the match with the fork's parent, then its source. One
`GET /repos/{owner}/{name}` per new slug per process per day: answers (fork
or not) are cached for a day, a failed or timed-out read for ten minutes, and
nothing here ever raises — a failure is "not indexed". This module only names
repositories; which entries the caller may read is still decided by the
handler's per-entry gate, so a fork maps only to an index the caller can
already read.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

from server.code.app.ttl_cache import TtlCache
from server.settings import settings

logger = logging.getLogger("loom.code.fork_upstream")

MAX_CANDIDATES = 3

MAX_CACHE = 4096

# GitHub's own owner/name grammar: owner is 1-39 of [A-Za-z0-9-], name is
# [A-Za-z0-9._-] and never "." or "..". A candidate that is not exactly this
# never reaches the authenticated request, so a caller cannot steer the URL
# (`../..`, `a/b?x=`) with the server's token.
_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38})/(?!\.{1,2}$)[a-z0-9._-]{1,100}$")

# slug -> upstream slugs; a failed read is kept `code_fork_upstream_miss_ttl_s` as "not a
# known fork".
_cache: TtlCache[str, tuple[str, ...]] = TtlCache(
    lambda: settings.code_fork_upstream_cache_ttl_s, maxsize=MAX_CACHE
)


def valid_slug(slug: str) -> bool:
    return bool(_SLUG.match(slug or ""))


Fetch = Callable[[str], Awaitable[list[str]]]


def reset() -> None:
    _cache.clear()


async def _github_upstreams(slug: str) -> list[str]:
    from server.clients.github import repo_fork_upstreams
    from server.settings import settings

    return await repo_fork_upstreams(slug, str(settings.code_github_token))


async def _ask(slug: str, fetch: Fetch) -> None:
    """One GitHub read for `slug`, cached either way; never raises."""
    try:
        ups = tuple(u.lower() for u in await fetch(slug) if isinstance(u, str) and u)
        _cache.put(slug, ups)
    except Exception as e:  # noqa: BLE001 — a failed read is "not a known fork"
        logger.debug("fork_upstream: %s: %s", slug, e)
        _cache.put(slug, (), ttl_s=settings.code_fork_upstream_miss_ttl_s)


async def fork_upstreams(
    slugs: list[str], *, fetch: Fetch | None = None, budget_s: float | None = None
) -> list[tuple[str, str]]:
    """`(checkout_slug, upstream_slug)` pairs, in candidate order and, per
    candidate, parent before source. Reads are concurrent and share one
    `budget_s` bound; a read still running at the bound counts as a failure."""
    todo = [s for s in dict.fromkeys(slugs[:MAX_CANDIDATES]) if valid_slug(s)]
    missing = [s for s in todo if s not in _cache]
    if missing:
        f = fetch or _github_upstreams
        tasks = [asyncio.ensure_future(_ask(s, f)) for s in missing]
        bound = settings.code_fork_upstream_budget_s if budget_s is None else budget_s
        try:
            await asyncio.wait(tasks, timeout=bound)
        finally:
            # A read still running at the bound, or when the caller is cancelled,
            # is abandoned with it rather than left to finish unowned.
            for t in tasks:
                t.cancel()
        for s in missing:
            if s not in _cache:
                _cache.put(s, (), ttl_s=settings.code_fork_upstream_miss_ttl_s)
    return [(s, u) for s in todo for u in (_cache.get(s) or ())]
