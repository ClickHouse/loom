"""Answer `server.core.hosts`'s name lookups in a test: the in-loop one (any host a
caller names) and the system one (an operator-allowlisted host)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from server.core import hosts


def resolves(monkeypatch: pytest.MonkeyPatch, answer: Callable[[str], list[str]]) -> None:
    """Both lookups answer `host` with `answer(host)`."""

    async def _in_loop(host: str) -> list[str]:
        return answer(host)

    monkeypatch.setattr(hosts, "_resolve", answer)
    monkeypatch.setattr(hosts, "_resolve_in_loop", _in_loop)
