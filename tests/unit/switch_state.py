"""The model-call switch and the extraction policies as a test needs them, each
set through its copy's one seam."""

from __future__ import annotations

from server.llm import customer
from server.memory.app import extraction_policy
from server.settings import settings


def switch_read_now(*off: str) -> None:
    """The switch as this process just read it, with `off` switched off."""
    customer._switch._reset_for_tests(
        frozenset(off), read_age_s=0.0, due_in_s=settings.model_calls_policy_refresh_seconds
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


def policies_read_now(policies: dict[tuple[str, str], str]) -> None:
    """The extraction policies as this process just read them."""
    extraction_policy._policies._reset_for_tests(
        policies, read_age_s=0.0, due_in_s=settings.model_calls_policy_refresh_seconds
    )


def policies_unread() -> None:
    """Extraction policies this process never read, with a read due now."""
    extraction_policy._policies._reset_for_tests(None, read_age_s=None, due_in_s=0.0)
