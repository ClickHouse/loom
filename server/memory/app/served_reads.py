"""Served reads: which keys an agent read returned to the agent.

`recall_count` holds only the recall credit (`recall_credit.py`), which a credited
`memory.search` writes and the rank reads. Every agent read that recalls memory, credited
or not, also writes one row per key it returned to `loom.memory_served_daily`, in the
background (`store.served`). Nothing ranks on it; it answers "has an agent been shown
this?" for the inspector and `memory.health`.

The row is written by the op, over what its answer carries, so a hit the op trims (a
transcript row a context pack drops, an item past a section budget) is never counted. The
op's handler is wrapped where the op is declared; a handler another op calls directly
records nothing of its own. Not written: a `memory.search` sent with `auto_touch: false`
or a `memory.ask` with `reinforce: false` (evaluation reads), a key lookup (`memory.get`,
`memory.bulk_get`, `memory.list`: the agent names the key it reads), a read by the
dashboard or the assistant (a person inspecting), and a measurement read (a bench label,
or the `*` / `-` agent with no named consumer behind it, as
`recall_credit.is_measurement_read` says). The code plane's reads that annotate code with
the memories naming it (code.review_brief, lesson notes) are not memory recalls.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Protocol

from server.identity.app.request_scope import request_scope
from server.memory.app import after_write, recall_credit
from server.memory.domain.context_layout import STACK_ROW_LAYERS
from server.memory.domain.search_types import hit_row
from server.memory.store import served
from server.observability.consumer import INSPECTOR_CONSUMERS

log = logging.getLogger(__name__)

# (namespace, memory_key) pairs an answer returned.
Keys = list[tuple[str, str]]


def _counted_agent(agent: str) -> bool:
    """False for a reader whose reads are not an agent's: the dashboard, the assistant,
    a bench label, or a sentinel agent with no named consumer."""
    if recall_credit.current_named_consumer() in INSPECTOR_CONSUMERS:
        return False
    return not recall_credit.is_measurement_read(agent=agent)


def schedule_served(org: str, agent: str, keys: Iterable[tuple[str, str]]) -> None:
    """One served row per (namespace, key), in the background, on the newest live row of
    the key inside the request's read scope: a token narrowed to one user records on its
    own copy, where its reads are read."""
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


class _AgentRead(Protocol):
    """The input of an agent read op: the org it reads, and the agent from its `agent`
    field when it has one (`_reader`)."""

    @property
    def org(self) -> str: ...


def _reader(input: Any) -> str:
    """Who read: the input's agent when it names one; for a sentinel agent (`*`, `-`: the
    SDK's and MCP's cross-agent reads, a context pack) or an input with none
    (memory.for_file), the named consumer behind it. With neither, the sentinel stays and
    `_counted_agent` refuses it as a measurement read, and an input with no agent records
    nothing."""
    # memory.for_file's input has no `agent` field
    agent = str(getattr(input, "agent", "") or "")
    if agent and not recall_credit.is_agent_sentinel(agent):
        return agent
    return recall_credit.current_named_consumer() or agent


def records_served[I: _AgentRead, O](
    keys_of: Callable[[I, O], Keys],
) -> Callable[[Callable[[I], Awaitable[O]]], Callable[[I], Awaitable[O]]]:
    """Wrap an op's handler so its answer's keys (`keys_of`) are recorded as served."""

    def wrap(handler: Callable[[I], Awaitable[O]]) -> Callable[[I], Awaitable[O]]:
        @functools.wraps(handler)
        async def handle(input: I) -> O:
            out = await handler(input)
            try:
                keys = keys_of(input, out)
                if keys:
                    reader = _reader(input)
                    if reader:
                        schedule_served(input.org, reader, keys)
            except Exception as e:  # noqa: BLE001 — a served row must not fail the read
                log.warning("served keys of %s failed (non-fatal): %s", handler.__name__, e)
            return out

        # which answer keys this op records, for the test that pins each op's extractor
        handle.served_keys = keys_of  # type: ignore[attr-defined]
        return handle

    return wrap


def _hit_keys(hits: Iterable[Any], namespace: str) -> Keys:
    """The stored row each hit came from (a chunk's is its parent fact's)."""
    return [hit_row(h, namespace) for h in hits]


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


def rendered_rows(input: Any, out: Any) -> Keys:
    """context.pack, memory.for_file: the stored rows the op noted its `rendered` answer
    prints (`_served_rows`), after every budget: a pack's sections and its own
    (`context_layout.served_rows`), a file block's row budget (`for_file.render_block`)."""
    return list(out._served_rows)


def stack_keys(input: Any, out: Any) -> Keys:
    """memory.stack: the stored rows of its entries (not the profile or knowledge ones)."""
    return [
        (e.namespace or input.namespace, e.memory_key)
        for e in out.entries
        if e.section in STACK_ROW_LAYERS and e.memory_key
    ]


def search_query_keys(input: Any, out: Any) -> Keys:
    """search.query: the memory section's hits it kept."""
    memory = out.memory or {}
    return [
        hit_row(h, input.namespace)
        for h in memory.get("results") or []
        if isinstance(h, dict) and h.get("memory_key")
    ]
