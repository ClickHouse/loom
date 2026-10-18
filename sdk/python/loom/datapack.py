"""Datapack — the encapsulating unit for an agentic application.

A Datapack is just a bound `(org, namespace)` tuple. It exists as a named
construct so callers think of "this app's data" as one thing rather than
chasing three separate parameters through every method signature.

You spawn `Agent` instances out of it — one per agent_name your app
runs. All agents inside a datapack share the namespace's memory and
knowledge; per-agent isolation comes from the `agent` axis on every row.

Usage::

    pack = Datapack(org="clickhouse", namespace="my-bot")
    detector  = pack.agent("detector",  api_key=TOK)
    validator = pack.agent("validator", api_key=TOK)

    async with detector, validator:
        await detector.knowledge.add(text=..., source_uri="github://pr/100")
        await validator.working_memory.set(block_key="review/pr-100", value=...)

This is naming sugar over `Agent(org=..., namespace=..., agent=...)`; the
wire shape is the same.
"""

from __future__ import annotations

from typing import Any

from loom.agent import Agent
from loom.retry import RetryPolicy


class Datapack:
    """An (org, namespace) tuple you spawn agents out of.

    Takes every `Agent` option as a default for the agents it spawns; each
    `.agent(name)` is its own `Agent` with its own connection pool.
    """

    def __init__(
        self,
        org: str,
        namespace: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 60.0,
        pacing: bool = True,
        coalesce_sets: bool = True,
        ratelimit_max_wait_s: float | None = None,
        auto_run: bool | None = None,
        retry: RetryPolicy | dict[str, Any] | None = None,
        consumer: str = "",
        mentor_brief: bool | None = None,
        profile: str | None = None,
    ) -> None:
        self.org = org
        self.namespace = namespace
        self._defaults: dict[str, Any] = {
            "base_url": base_url,
            "api_key": api_key,
            "timeout_s": timeout_s,
            "pacing": pacing,
            "coalesce_sets": coalesce_sets,
            "ratelimit_max_wait_s": ratelimit_max_wait_s,
            "auto_run": auto_run,
            "retry": retry,
            "consumer": consumer,
            "mentor_brief": mentor_brief,
            "profile": profile,
        }

    def agent(self, name: str, **overrides: Any) -> Agent:
        """A new `Agent` for `name` on this datapack's (org, namespace).
        `overrides` (any `Agent` option, e.g. a service account's
        `api_key`) win over the datapack's defaults; an unknown one raises
        `TypeError`."""
        return Agent(self.org, self.namespace, name, **{**self._defaults, **overrides})

    def __repr__(self) -> str:
        return f"Datapack(org={self.org!r}, namespace={self.namespace!r})"
