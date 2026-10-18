"""A deployment that requires auth refuses to start while it signs with the
checked-in dev secret; the local dev stack and non-deployment processes do not."""

from __future__ import annotations

import pytest

from server.auth.jwt import DEV_PROFILE_LABEL, DevSigningSecretRefused, require_real_signing_secret


def _check(**over: object) -> None:
    kw: dict[str, object] = {
        "deployment": True,
        "instance_label": "deploy:web",
        "auth_required": True,
        "dev_secret": True,
    }
    kw.update(over)
    require_real_signing_secret(**kw)  # type: ignore[arg-type]


def test_a_deployment_with_auth_on_and_the_dev_secret_is_refused() -> None:
    with pytest.raises(DevSigningSecretRefused, match="LOOM_JWT_SECRET"):
        _check()
    with pytest.raises(DevSigningSecretRefused):
        _check(instance_label="")  # the compose deployment without a label


@pytest.mark.parametrize(
    "over",
    [
        {"instance_label": DEV_PROFILE_LABEL},  # `make dev`
        {"deployment": False, "instance_label": "ci:integration"},
        {"deployment": False, "instance_label": "iso:w1:7880"},
        {"auth_required": False},  # single-user / auth off
        {"dev_secret": False},  # a real secret
    ],
)
def test_the_dev_stack_tests_and_real_secrets_start(over: dict[str, object]) -> None:
    _check(**over)


def test_a_keyring_key_does_not_hide_the_dev_root_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key in loom.signing_keys may sign, yet the root secret seals it and
    every stored credential, so the dev check reads LOOM_JWT_SECRET alone."""
    from datetime import datetime

    from server.auth import keyring
    from server.settings import DEV_JWT_SECRET, Settings

    monkeypatch.setattr(
        keyring,
        "_view",
        keyring._View(
            keys={"k1": keyring.SigningKey("k1", "HS256", "s" * 40, "", datetime.min)},
            loaded=True,
        ),
    )
    assert keyring.signing_key().kid == "k1"
    monkeypatch.setenv("LOOM_JWT_SECRET", DEV_JWT_SECRET)
    assert Settings().jwt_secret_is_dev_default()
    monkeypatch.setenv("LOOM_JWT_SECRET", "r" * 40)
    assert not Settings().jwt_secret_is_dev_default()


def test_the_dev_compose_stack_carries_the_exempt_label() -> None:
    from pathlib import Path

    compose = (Path(__file__).resolve().parents[2] / "dev" / "docker-compose.dev.yml").read_text()
    assert f'LOOM_INSTANCE_LABEL: "{DEV_PROFILE_LABEL}"' in compose
    assert 'LOOM_JWT_SECRET: "${LOOM_JWT_SECRET:?' in compose
    assert "dev-only-jwt-secret-do-not-use-in-prod" not in compose
