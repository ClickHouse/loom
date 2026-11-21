"""Persisted permissions are renewed after an in-flight ASK generation."""

from __future__ import annotations

from tests.integration._mirror_ask_asgi import run_scenario
from tests.integration._resources import ch_target, unique_org


def test_signed_ask_discards_generation_after_permission_loss(mint_test_token) -> None:
    ch_target()
    org = unique_org("checked-ask")
    writer = mint_test_token(org=org, namespaces=["team"], roles=["writer"])
    run_scenario(org, writer)
