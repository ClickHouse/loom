"""The consent policy the composition root supplies to erasure stores."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from server.core.errors import BackendUnavailableError
from server.core.repo_base import _CHClient


@dataclass(frozen=True)
class ConsentPolicy:
    fence_names: Callable[[str, str, str], list[str]]
    lease_client: Callable[[_CHClient], _CHClient]
    lock_postgres: Callable[..., Awaitable[None]]
    record_clickhouse: Callable[..., Awaitable[None]]
    record_postgres: Callable[..., Awaitable[None]]


_policy: ConsentPolicy | None = None


def use_policy(policy: ConsentPolicy) -> None:
    global _policy
    _policy = policy


def current_policy() -> ConsentPolicy:
    if _policy is None:
        raise BackendUnavailableError("erasure consent policy is not wired")
    return _policy
