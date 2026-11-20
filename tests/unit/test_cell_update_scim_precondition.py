"""A valid SCIM target reaches the update owner's provisioning precondition."""

from __future__ import annotations

from copy import deepcopy

import pytest

from server.cli.cell_update_config import target_fingerprint
from server.router import fingerprints
from tests.unit.test_cell_update_pair import value
from tests.unit.test_cell_update_target import REGISTERED, _config


@pytest.mark.parametrize("scim_on", [True, False], ids=["scim-on", "scim-off"])
def test_target_checks_the_real_cell_token_policy_without_a_blanket_refusal(scim_on):
    config = _config(
        scim_token="lsc_first_" + "a" * 64 if scim_on else "",
        oauth_login_provider="oidc",
        auth_required=True,
    )
    registered = {**REGISTERED, "is_first": True}
    before = deepcopy(registered)
    try:
        result = target_fingerprint(config, "deployment", registered)
    except ValueError as error:
        result = error
    assert not isinstance(result, ValueError), str(result)
    assert result["fields"]["scim"] is scim_on
    assert registered == before


@pytest.mark.parametrize("scim_on", [True, False], ids=["scim-on", "scim-off"])
def test_pair_selects_the_reserved_target_for_the_real_precondition_owner(scim_on):
    current, target = value("old"), value("new")
    target["fields"]["scim"] = scim_on
    before = deepcopy((current, target))
    try:
        result = fingerprints.configuration_pair(current, None, target, "config")
    except ValueError as error:
        result = error
    assert not isinstance(result, ValueError), str(result)
    assert result == (current, target)
    assert (current, target) == before
