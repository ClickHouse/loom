"""What the state gauges read from the domains.

The census statement, the agent-label gauge and the code-plane lag and
config gauges need a few facts only a domain holds: the knowledge plane's
chunk-liveness predicate, the usage kinds an agent writes, the configured
repositories and the compiler tiers. The composition root
(`server/wiring.py`) hands them in with `use_state_inputs`; the repository
list and the config-error count are callables, read at every probe.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


class CodeRepo(Protocol):
    @property
    def org(self) -> str: ...

    @property
    def namespace(self) -> str: ...

    @property
    def git_dir(self) -> str: ...

    @property
    def git_branch(self) -> str: ...


class CompilerTier(Protocol):
    @property
    def producer(self) -> str: ...

    @property
    def watermark_key(self) -> str: ...


@dataclass(frozen=True, slots=True)
class StateInputs:
    # `loom.chunks` rows that are live (the knowledge plane's predicate)
    live_chunk_sql: str
    # the usage event kinds an agent, not the server, writes
    agent_label_kinds: tuple[str, ...]
    # the configured code repositories, as of the call
    code_repos: Callable[[], Sequence[CodeRepo]]
    # conditions in the repo config this process could not use; None before
    # the config was first read
    repos_config_errors: Callable[[], int | None]
    compiler_tiers: tuple[CompilerTier, ...]
    # the code-index tiers the `code_tier_failing` health problem reports: one mapping
    # per entry and tier (org, namespace, tier, kind, failed_passes, lag_commits, severity)
    failing_code_tiers: Callable[[], Sequence[Mapping[str, Any]]]


_inputs: StateInputs | None = None


def use_state_inputs(inputs: StateInputs | None) -> None:
    global _inputs
    _inputs = inputs


def state_inputs() -> StateInputs:
    if _inputs is None:
        raise RuntimeError("no state-gauge inputs are wired into this process")
    return _inputs
