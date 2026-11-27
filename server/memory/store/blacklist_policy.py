"""Pattern checks supplied by the composition root to derived-text stores."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from server.core.errors import BackendUnavailableError


@dataclass(frozen=True)
class BlacklistPolicy:
    capture: Callable[[str], Awaitable[dict[tuple[str, str], int]]]
    enforce: Callable[..., Awaitable[dict[tuple[str, str], int]]]
    matches: Callable[..., Awaitable[Any]]


_policy: BlacklistPolicy | None = None


def use_policy(policy: BlacklistPolicy) -> None:
    global _policy
    _policy = policy


def current_policy() -> BlacklistPolicy:
    if _policy is None:
        raise BackendUnavailableError("derived-text blacklist policy is not wired")
    return _policy
