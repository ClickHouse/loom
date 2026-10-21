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
    """The agent the request in flight files a bot's rule for and is served that
    bot's promoted rules as. A token pinned to one end user (`read_user_id`) acts
    as the agent its issuer signed, and as none ('': nothing is filed, and no rule
    promoted for a named bot is served; a rule for every agent still is) when it
    names none, never as the body's. A token Loom mints names no agent, so one
    with a `read_user_id` pin acts as none. Any other caller, an `own_user` token
    without a pin included, acts as the body's agent."""
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
    took a place among `hits` and more can be asked for, `widen(n)` searches again
    for `n` hits; if that search fails, the hits served from the first pass answer."""
    served = [h for h in hits if assistant_rules.serves_agent(tags_of(h), agent)]
    # A wider search past the cap would send the same query again.
    if len(served) < len(hits) and assistant_rules.hits_to_ask(top_k) > len(hits):
        try:
            wider = await widen(assistant_rules.hits_to_ask(top_k))
            served = [h for h in wider if assistant_rules.serves_agent(tags_of(h), agent)]
        except Exception as e:  # noqa: BLE001 — the first pass's served hits still answer
            log.warning("knowledge search widened past other bots' rules failed: %s", e)
    return served[:top_k]
