"""Which bot a request files rules for and is served promoted rules as, and the
filter that keeps another bot's rules out of the knowledge hits of
`context.pack`, `memory.stack`, `loom_search` and `memory.ask`. It reads the request's auth
context, so it lives in app; the tag rule is server.memory.domain.assistant_rules."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from server.memory.domain import assistant_rules

log = logging.getLogger(__name__)


def request_rule_agent(body_agent: str) -> str:
    """The agent this request files rules for and is served rules as: a
    `read_user_id`-pinned token's signed agent ('' when it names none), else the
    body's agent ('-' when empty)."""
    # Lazy: server.auth.context pulls in authz → operations → handlers, and
    # the handlers import this module at load time.
    from server.auth.context import current_auth

    auth = current_auth()
    if auth is not None and auth.read_user_id:
        return auth.agent_principal.removeprefix("agent:")
    return body_agent or "-"


async def served_hits[H](
    hits: list[H],
    widen: Callable[[int], Awaitable[list[H]]],
    tags_of: Callable[[H], list[str]],
    agent: str,
    top_k: int,
) -> list[H]:
    """The first `top_k` knowledge hits that serve `agent`. When another bot's rule
    took a place among a full first pass of `hits` and more can be asked for,
    `widen(n)` searches again for `n` hits; if that search fails, the hits served
    from the first pass answer."""
    served = [h for h in hits if assistant_rules.serves_agent(tags_of(h), agent)]
    # A first pass short of top_k found every hit there is, and at the cap a wider
    # search would send the same query again.
    full_pass = len(hits) >= top_k
    if len(served) < len(hits) and full_pass and assistant_rules.hits_to_ask(top_k) > top_k:
        try:
            wider = await widen(assistant_rules.hits_to_ask(top_k))
            served = [h for h in wider if assistant_rules.serves_agent(tags_of(h), agent)]
        except Exception as e:  # noqa: BLE001 — the first pass's served hits still answer
            log.warning("knowledge search widened past other bots' rules failed: %s", e)
    return served[:top_k]
