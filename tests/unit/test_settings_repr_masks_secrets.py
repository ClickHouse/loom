"""`repr(settings)` shows a secret field's length, never its value."""

from __future__ import annotations

import re

import pytest

from server.settings import Settings
from server.settings._env import is_secret_field

_SECRET = "s3cr3t-canary-value"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ch_password", _SECRET),
        ("jwt_secret", _SECRET),
        ("jwt_secret_previous", _SECRET),
        ("oidc_client_secret", _SECRET),
        ("scim_token", _SECRET),
        ("router_operator_token", "lco_" + "5" * 128),  # its form is fixed
        ("langfuse_admin_token", f"pk:{_SECRET}"),
        ("code_github_token", _SECRET),
        ("tracker_credentials_json", f'{{"src": {{"user": "u", "password": "{_SECRET}"}}}}'),
        ("code_repos_json", f'[{{"namespace": "n", "github_token": "{_SECRET}"}}]'),
        ("otel_exporter_otlp_headers", f"Authorization=Bearer {_SECRET}"),
        ("rerank_endpoints_json", f'{{"gw": "https://user:{_SECRET}@rerank.example/v1"}}'),
    ],
)
def test_secret_fields_are_masked(field: str, value: str) -> None:
    s = Settings(_env_file=None, **{field: value})  # type: ignore[call-arg]
    assert _SECRET not in repr(s)
    assert _SECRET not in str(s)
    assert f"{field}='<masked, {len(value)} chars>'" in repr(s)


def test_non_secret_fields_stay_readable() -> None:
    s = Settings(_env_file=None, tracker_sources_json="{}", clickstack_user_id_keys="uid")  # type: ignore[call-arg]
    assert "tracker_sources_json='{}'" in repr(s)
    assert "clickstack_user_id_keys='uid'" in repr(s)


@pytest.mark.parametrize(
    "bad",
    [
        {"default_namespace": "personal-x", "auth_required": False},  # model-level check
        {"jwt_revocation_max_stale_seconds": 5, "jwt_revocation_cache_ttl_seconds": 30},
        {"ch_port": "not-a-port"},  # field-level type error
    ],
)
def test_a_load_error_names_the_field_never_the_environment(bad: dict[str, object]) -> None:
    """A model-level check's error must not print every value it was handed,
    or one bad setting puts the ClickHouse password in the boot log."""
    with pytest.raises(ValueError) as ei:
        Settings(_env_file=None, ch_password=_SECRET, jwt_secret=_SECRET, **bad)  # type: ignore[arg-type]
    text = str(ei.value)
    assert _SECRET not in text
    assert "input_value" not in text


# Names with a secret's word in them that hold no secret: TTLs, the token-exchange
# endpoint's parameters.
_NOT_SECRETS = {
    "password_link_ttl_seconds",
    "oauth_access_token_ttl_seconds",
    "inference_token_url",
    "inference_workload_token_file",
    "inference_workload_token_mode",
    "inference_token_audience",
    "inference_token_scope",
    "inference_token_exchange_timeout_s",
}


def test_every_field_named_for_a_secret_is_a_secret() -> None:
    """A secret the classifier misses is printed by settings.list and accepted from the
    settings file; a new field with one of these words in its name must be one or be
    listed above. `_tokens` and `_token_budget` count tokens and name no secret."""
    named = {
        n
        for n in Settings.model_fields
        if re.search(r"secret|password|api_key|(?:^|_)key(?:_(?:file|previous|path))?$", n)
        or re.search(r"token(?!s(?:_|$)|_budget)", n)
    }
    assert {n for n in named if not is_secret_field(n)} == _NOT_SECRETS
