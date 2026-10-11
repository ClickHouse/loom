"""LOOM_IDENTITY_PROVIDER dispatch + back-compat inference.

LOOM_IDENTITY_PROVIDER resolves the auth mode. Only 'single-user'
changes behaviour: it turns auth off and serves LOOM_DEFAULT_NAMESPACE
under a synthesized caller. Unset, the mode is inferred from the
legacy auth_required + self_signup pair, so older configs keep working.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


def test_explicit_setting_wins() -> None:
    from server.settings import settings

    with (
        patch.object(settings, "identity_provider", "gateway"),
        patch.object(settings, "auth_required", False),  # legacy says single
        patch.object(settings, "self_signup", "off"),
    ):
        assert settings.effective_identity_provider() == "gateway"


def test_back_compat_single_user_when_auth_off() -> None:
    """A plain `docker run loom` (auth_required=false) → single-user."""
    from server.settings import settings

    with (
        patch.object(settings, "identity_provider", ""),
        patch.object(settings, "auth_required", False),
        patch.object(settings, "self_signup", "off"),
    ):
        assert settings.effective_identity_provider() == "single-user"


def test_back_compat_trust_network_when_auth_on_and_signup_open() -> None:
    """A self-hosted intranet config → trust-network."""
    from server.settings import settings

    with (
        patch.object(settings, "identity_provider", ""),
        patch.object(settings, "auth_required", True),
        patch.object(settings, "self_signup", "open"),
    ):
        assert settings.effective_identity_provider() == "trust-network"


def test_back_compat_gateway_when_auth_on_and_signup_off() -> None:
    """Auth required but no self-signup → upstream gateway expected."""
    from server.settings import settings

    with (
        patch.object(settings, "identity_provider", ""),
        patch.object(settings, "auth_required", True),
        patch.object(settings, "self_signup", "off"),
    ):
        assert settings.effective_identity_provider() == "gateway"


def test_all_three_modes_are_settable() -> None:
    """No mode is implicitly forbidden by the resolver."""
    from server.settings import settings

    for mode in ("single-user", "trust-network", "gateway"):
        with patch.object(settings, "identity_provider", mode):
            assert settings.effective_identity_provider() == mode


def test_unknown_mode_rejected() -> None:
    """Explicit unknown values fail at load — operators get a clear error
    rather than silent fallthrough. The same holds for every closed-set auth
    setting: a typo in LOOM_OAUTH_LOGIN_PROVIDER read as "password" would
    leave password self-signup open beside SSO."""
    import pytest
    from pydantic import ValidationError

    from server.settings._auth import AuthSettings

    for field, bad in [
        ("identity_provider", "oidc"),
        ("oauth_login_provider", "OIDC"),
        ("self_signup", "Open"),
        ("jwt_algorithm", "hs256"),
    ]:
        with pytest.raises(ValidationError, match=field):
            AuthSettings(**{field: bad})


def test_signing_key_contradictions_fail_at_load() -> None:
    """A signing kid missing from the secret map mints nothing; under RS256 a
    signing kid without a public key mints tokens nothing here verifies; a
    malformed key map would fail every request. All three refuse at load."""
    import json

    import pytest
    from pydantic import ValidationError

    from server.settings._auth import AuthSettings

    AuthSettings()  # the defaults are consistent
    AuthSettings(jwt_secrets_json=json.dumps({"k2": "s" * 32}), jwt_active_kid="k2")
    with pytest.raises(ValidationError, match="LOOM_JWT_ACTIVE_KID"):
        AuthSettings(jwt_secrets_json=json.dumps({"k1": "s" * 32}), jwt_active_kid="k2")
    with pytest.raises(ValidationError, match="RS256 requires a public key"):
        AuthSettings(jwt_algorithm="RS256")
    with pytest.raises(ValidationError, match="RS256 requires a public key"):
        AuthSettings(jwt_algorithm="RS256", jwt_public_keys_json=json.dumps({"other": "pem"}))
    AuthSettings(jwt_algorithm="RS256", jwt_public_keys_json=json.dumps({"default": "pem"}))
    with pytest.raises(ValidationError, match="jwt_public_keys_json"):
        AuthSettings(jwt_public_keys_json="{not json")


def test_key_maps_decode_once_per_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mint and verify read the key maps per token; the JSON is decoded once
    per configured value, and a changed value (an override) takes effect."""
    import json

    from server.settings import _env
    from server.settings._auth import AuthSettings

    calls: list[str] = []
    real = _env._decode_json_or_b64

    def counting(raw: str, field: str) -> object:
        calls.append(field)
        return real(raw, field)

    monkeypatch.setattr(_env, "_decode_json_or_b64", counting)
    _env.json_map_items.cache_clear()
    s = AuthSettings(jwt_secrets_json=json.dumps({"k9": "a" * 32}), jwt_active_kid="k9")
    for _ in range(5):
        assert s.jwt_secret_map() == {"k9": "a" * 32}
    assert calls.count("jwt_secrets_json") == 1
    s.jwt_secret_map()["k9"] = "mutated"
    assert s.jwt_secret_map() == {"k9": "a" * 32}
    s.jwt_secrets_json = json.dumps({"k9": "b" * 32})
    assert s.jwt_secret_map() == {"k9": "b" * 32}
    _env.json_map_items.cache_clear()
