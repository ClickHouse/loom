"""An update validates its fingerprint before reserving or accepting it."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

from server.core import cell
from server.settings import Settings, settings

REGISTERED = {
    "cell": "first",
    "host": "loom.example",
    "deployment_id": "deployment",
    "bucket": "docs",
    "bucket_prefix": "originals",
}


def _config(**overrides: Any) -> Settings:
    values = settings.model_dump() | {
        "public_base_url": "https://loom.example",
        "oauth_issuer": "https://loom.example",
        "document_s3_bucket": "docs",
        "document_s3_prefix": "originals",
        "code_repos_json": "[]",
        "code_org": "",
        "tracker_org": "",
        "oidc_jit_org": "",
        "self_signup": "off",
        "scim_token": "",
    }
    return Settings(_env_file=None, **(values | overrides))


def test_target_uses_explicit_settings_and_the_existing_code_reader() -> None:
    from server.cli.cell_update_config import target_fingerprint

    config = _config(code_org="configured", tracker_org="tracker")
    before_config, before_registration = config.model_dump(), deepcopy(REGISTERED)
    value = target_fingerprint(config, "deployment", REGISTERED)
    assert value == cell.as_value(cell.fingerprint_for(config, "deployment", {"configured"}))
    assert value["fields"]["configured_orgs"] == ["configured", "tracker"]
    assert config.model_dump() == before_config
    assert REGISTERED == before_registration


@pytest.mark.parametrize(
    "change,field",
    [
        ({"document_s3_bucket": "elsewhere"}, "bucket"),
        ({"document_s3_prefix": "elsewhere"}, "bucket_prefix"),
        ({"public_base_url": "https://elsewhere.example"}, "public_host"),
        ({"oauth_issuer": "https://elsewhere.example"}, "oauth_issuer_host"),
    ],
)
def test_target_refuses_changed_registered_identity_without_echoing_values(
    change: dict[str, str], field: str
) -> None:
    from server.cli.cell_update_config import target_fingerprint

    with pytest.raises(ValueError, match=field) as error:
        target_fingerprint(_config(**change), "deployment", REGISTERED)
    assert "elsewhere" not in str(error.value)


def test_target_refuses_different_deployment_and_bucketless_adoption() -> None:
    from server.cli.cell_update_config import target_fingerprint

    with pytest.raises(ValueError, match="deployment_id"):
        target_fingerprint(_config(), "different", REGISTERED)
    with pytest.raises(ValueError, match="bucket"):
        target_fingerprint(
            _config(), "deployment", REGISTERED | {"bucket": "", "bucket_prefix": ""}
        )


def test_target_refuses_invalid_scim_credentials_before_any_state_work() -> None:
    from server.cli.cell_update_config import target_fingerprint

    with pytest.raises(ValueError, match="SCIM") as error:
        target_fingerprint(
            _config(scim_token="private-scim-token"),
            "deployment",
            REGISTERED | {"is_first": True},
        )
    assert "private-scim-token" not in str(error.value)


@pytest.mark.parametrize("org", ["-", "*", "x" * (cell.ORG_MAX_BYTES + 1)])
def test_target_refuses_reserved_or_oversized_configured_orgs(org: str) -> None:
    from server.cli.cell_update_config import target_fingerprint

    # A fully validated Settings model may still name a non-tenant configured org.
    with pytest.raises(ValueError, match="fingerprint"):
        target_fingerprint(_config(tracker_org=org), "deployment", REGISTERED)


def test_target_must_fit_a_live_placement() -> None:
    from server.cli.cell_update_config import target_fingerprint

    config = _config(
        code_repos_json=json.dumps(
            [
                {"namespace": f"repo{n}", "org": str(n) + "x" * 1800, "repo": "owner/project"}
                for n in range(35)
            ]
        )
    )
    with pytest.raises(ValueError, match="placement body"):
        target_fingerprint(config, "deployment", REGISTERED)


def test_registration_check_applies_defaults_and_preserves_newer_fields() -> None:
    from server.router.fingerprints import registration_refusal

    value = cell.as_value({"future": {"nested": [False, 0]}})
    before = deepcopy(value)
    empty = REGISTERED | {"host": "", "deployment_id": "", "bucket": "", "bucket_prefix": ""}
    assert registration_refusal(value, empty) == ""
    assert value == before
    assert "deployment_id" in registration_refusal(value, REGISTERED)
    assert "unreadable" in registration_refusal({"v": True, "fields": {}}, empty)
