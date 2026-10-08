"""Fence stripping for JSON-shaped LLM completions."""

from __future__ import annotations

import re

# A leading ``` / ```json fence and a trailing ``` fence, with the whitespace
# beside them, if the model decorates its JSON despite being told not to.
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")


def strip_code_fence(text: str) -> str:
    """The completion with surrounding whitespace and markdown code fences removed."""
    return _FENCE_RE.sub("", (text or "").strip()).strip()
