"""The model-call switch as a test needs it, set through the switch's one seam."""

from __future__ import annotations

from server.llm import customer
from server.settings import settings


def switch_off(*orgs: str) -> None:
    """The switch as this process just read it, with `orgs` switched off."""
    customer._switch._reset_for_tests(
        frozenset(orgs), read_age_s=0.0, due_in_s=settings.model_calls_policy_refresh_seconds
    )


def switch_unread() -> None:
    """A switch this process never read, with a read due now."""
    customer._switch._reset_for_tests(None, read_age_s=None, due_in_s=0.0)


def switch_stale() -> None:
    """A copy of the switch too old to trust, and not due to be read again."""
    customer._switch._reset_for_tests(
        frozenset(),
        read_age_s=settings.model_calls_policy_max_stale_seconds + 1,
        due_in_s=settings.model_calls_policy_refresh_seconds,
    )
