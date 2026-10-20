"""The model-call switch as a test needs it, set through the switch's one seam."""

from __future__ import annotations

from server.llm import customer
from server.settings import settings


def switch_read_now(*off: str) -> None:
    """The switch as this process just read it, with `off` switched off."""
    customer._switch._reset_for_tests(
        frozenset(off), read_age_s=0.0, due_in_s=settings.model_calls_policy_refresh_seconds
    )
