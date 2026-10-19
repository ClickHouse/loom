"""`repr(settings)` shows a secret field's length, never its value."""

from __future__ import annotations

import pytest

from server.settings import Settings

_SECRET = "s3cr3t-canary-value"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ch_password", _SECRET),
        ("jwt_secret", _SECRET),
        ("oidc_client_secret", _SECRET),
        ("scim_token", _SECRET),
        ("langfuse_admin_token", f"pk:{_SECRET}"),
        ("code_github_token", _SECRET),
        ("tracker_credentials_json", f'{{"src": {{"user": "u", "password": "{_SECRET}"}}}}'),
        ("code_repos_json", f'[{{"namespace": "n", "github_token": "{_SECRET}"}}]'),
        ("otel_exporter_otlp_headers", f"Authorization=Bearer {_SECRET}"),
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
