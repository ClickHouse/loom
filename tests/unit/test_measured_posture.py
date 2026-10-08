"""The code defaults are the measured posture.

Search and ranking flags that were turned on after a measurement must be the
code defaults, or a fresh deployment and every CI run get the unmeasured
values. This test pins the levers whose measurements are recorded. Changing a
value here means re-running the bench that justified it — not editing the
assertion.
"""

from __future__ import annotations

from server.settings import Settings


def _defaults() -> Settings:
    # Construct without the process environment or .env so this reads the
    # code defaults, not whatever the developer's shell exports.
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_prior_cap_is_the_measured_tiebreak() -> None:
    """recall@5 0.417 -> 0.583 on the ClickGap corpus at 0.08."""
    assert abs(_defaults().memory_prior_cap - 0.08) < 1e-9


def test_identifier_grounding_is_on() -> None:
    """Win-share neutral on a bench corpus (fired 5/150); removes confidently wrong answers."""
    assert _defaults().memory_search_identifier_grounding is True
