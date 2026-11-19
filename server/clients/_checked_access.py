"""A source-bound credential that native clients must revalidate at HTTP boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal, SupportsIndex
from urllib.parse import urlsplit


class CheckedAccess:
    """An internal checkout; converting it to an ordinary credential is forbidden."""

    __slots__ = ("_token", "kind", "deadline", "_validate")

    def __init__(
        self,
        token: str,
        *,
        kind: Literal["gdrive", "notion", "linear"],
        deadline: float,
        validate: Callable[[], Awaitable[None]],
    ) -> None:
        if (
            not math.isfinite(deadline)
            or deadline <= time.monotonic()
            or not token
            or len(token) > 8192
            or any(not 33 <= ord(c) <= 126 for c in token)
        ):
            raise ValueError("unusable connected checkout")
        self._token, self.kind, self.deadline = token, kind, deadline
        self._validate = validate

    def __repr__(self) -> str:
        return "CheckedAccess(<redacted>)"

    def __str__(self) -> str:
        raise TypeError("a connected checkout is not a shared credential")

    def __reduce_ex__(self, protocol: SupportsIndex) -> Any:
        raise TypeError("a connected checkout cannot be copied or serialized")

    def pacing_key(self) -> str:
        return hashlib.sha256(self._token.encode()).hexdigest()[:16]

    async def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError("connected checkout expired")
        async with asyncio.timeout_at(self.deadline):
            await self._validate()
        if time.monotonic() >= self.deadline:
            raise TimeoutError("connected checkout expired")

    async def authorization(self, url: str) -> str:
        target = urlsplit(url)
        allowed = (
            self.kind == "gdrive"
            and (
                target.netloc == "www.googleapis.com"
                and target.path.startswith("/drive/v3/")
                or target.netloc == "slides.googleapis.com"
                and target.path.startswith("/v1/presentations/")
            )
            or self.kind == "notion"
            and target.netloc == "api.notion.com"
            and target.path.startswith("/v1/")
            or self.kind == "linear"
            and target.netloc == "api.linear.app"
            and target.path == "/graphql"
        )
        if (
            target.scheme != "https"
            or target.fragment
            or target.query
            or "%" in target.path
            or "\\" in target.path
            or any(part in (".", "..") for part in target.path.split("/"))
            or not allowed
        ):
            raise ValueError("connected checkout destination refused")
        await self.check()
        return "Bearer " + self._token


type Credential = str | CheckedAccess
