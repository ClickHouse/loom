"""Served reads: which keys an agent read returned to the agent.

`recall_count` holds only the recall credit (`recall_credit.py`), which a credited
`memory.search` writes and the rank reads. Every agent read that returns memory, credited
or not, also writes one row per key it returned to `loom.memory_served_daily`, in the
background (`store.served`). Nothing ranks on it; it answers "has an agent been shown
this?" for the inspector and `memory.health`.

The row is written by the op, over what its answer carries, so a hit the op trims (a
transcript row a context pack drops, an item past a section budget) is never counted. The
op's handler is wrapped where the op is declared; a handler another op calls directly
records nothing of its own. Not written: a `memory.search` sent with `auto_touch: false`
or a `memory.ask` with `reinforce: false` (evaluation reads), a read by the dashboard or
the assistant (a person inspecting), and a measurement read (a bench label, or the `*` /
`-` agent with no named consumer behind it, as `recall_credit.is_measurement_read` says).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from server.memory.app import after_write

log = logging.getLogger(__name__)

# (namespace, memory_key) pairs an answer returned.
Keys = list[tuple[str, str]]


def _counted_agent(agent: str) -> bool:
    """False for a reader whose reads are not an agent's: the dashboard, the assistant,
    a bench label, or a sentinel agent with no named consumer."""
    from server.memory.app import recall_credit
    from server.observability.consumer import INSPECTOR_CONSUMERS

    if recall_credit.current_named_consumer() in INSPECTOR_CONSUMERS:
        return False
    return not recall_credit.is_measurement_read(agent=agent)


def schedule_served(org: str, agent: str, keys: Iterable[tuple[str, str]]) -> None:
    """One served row per (namespace, key), in the background, on the newest live row of
    the key inside the request's read scope: a token narrowed to one user records on its
    own copy, where its reads are read."""
    from server.identity.app.request_scope import request_scope
    from server.memory.store import served

    ns_keys: dict[str, list[str]] = {}
    for ns, key in dict.fromkeys(keys):
        ns_keys.setdefault(ns, []).append(key)
    if not ns_keys or not _counted_agent(agent):
        return
    scope = request_scope()

    async def _write() -> None:
        try:
            await served.record_served(org, ns_keys, agent=agent, scope=scope)
        except Exception as e:  # noqa: BLE001 — a served row must not fail the read
            log.warning("served-read log failed (non-fatal): %s", e)

    after_write.schedule(_write(), name="memory.served")


def records_served[I, O](
    keys_of: Callable[[I, O], Keys],
) -> Callable[[Callable[[I], Awaitable[O]]], Callable[[I], Awaitable[O]]]:
    """Wrap an op's handler so its answer's keys (`keys_of`) are recorded as `served`."""

    def wrap(handler: Callable[[I], Awaitable[O]]) -> Callable[[I], Awaitable[O]]:
        @functools.wraps(handler)
        async def handle(input: I) -> O:
            out = await handler(input)
            try:
                keys = keys_of(input, out)
                if keys:
                    i: Any = input
                    schedule_served(str(i.org), str(i.agent or ""), keys)
            except Exception as e:  # noqa: BLE001 — a served row must not fail the read
                log.warning("served keys of %s failed (non-fatal): %s", handler.__name__, e)
            return out

        return handle

    return wrap


def _hit_keys(hits: Iterable[Any], namespace: str) -> Keys:
    return [(str(h.namespace or namespace), str(h.memory_key)) for h in hits]


def search_keys(input: Any, out: Any) -> Keys:
    """memory.search: the hits served, unless the caller asked for no usage."""
    if not input.auto_touch:
        return []
    return _hit_keys(out.results, input.namespace)


def ask_keys(input: Any, out: Any) -> Keys:
    """memory.ask: the retrieved memories its answer returns (`results`), unless the ask
    is an evaluation read (`reinforce: false`)."""
    if not input.reinforce:
        return []
    return _hit_keys(out.results, input.namespace)


def procedure_keys(input: Any, out: Any) -> Keys:
    """procedure.match: the procedures matched."""
    return [(str(m.namespace or input.namespace), str(m.memory_key)) for m in out.matches]


# The memory stack's layers that are stored rows; profile and knowledge are not.
_STACK_ROW_LAYERS = ("pinned", "recent_recall", "procedure")


def pack_keys(input: Any, out: Any) -> Keys:
    """context.pack: the stored rows its kept blocks cite (the pack notes each row's key
    where it makes the citation), which are the ones within its section budgets."""
    return list(out._served_rows)


def stack_keys(input: Any, out: Any) -> Keys:
    """memory.stack: the stored rows of its entries (not the profile or knowledge ones)."""
    return [
        (input.namespace, e.memory_key)
        for e in out.entries
        if e.section in _STACK_ROW_LAYERS and e.memory_key
    ]


def search_query_keys(input: Any, out: Any) -> Keys:
    """search.query: the memory section's hits it kept."""
    memory = out.memory or {}
    return [
        (str(h.get("namespace") or input.namespace), str(h["memory_key"]))
        for h in memory.get("results") or []
        if isinstance(h, dict) and h.get("memory_key")
    ]
