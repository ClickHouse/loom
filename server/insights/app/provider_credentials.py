"""Encrypted operator configuration envelopes, never a public response shape."""

import json
from collections.abc import Mapping

from server.core.secrets_at_rest import seal, unseal
from server.insights.api.models_provider_setup import ProviderSetupInput
from server.settings import settings

_PURPOSE = "model-provider-configuration"


def seal_configuration(setup: ProviderSetupInput, existing_keys: Mapping[str, str]) -> str:
    """Keep omitted keys; an explicit replacement wins over the saved value."""
    if settings.jwt_sealing_secret_is_dev_default() or len(settings.jwt_secret) < 32:
        raise ValueError(
            "Set a unique LOOM_JWT_SECRET of at least 32 characters before saving keys"
        )
    keys = dict(existing_keys)
    keys.update({provider: secret.get_secret_value() for provider, secret in setup.keys.items()})
    payload = {"format": 1, "setup": {**setup.model_dump(), "keys": keys}}
    return seal(_PURPOSE, json.dumps(payload, separators=(",", ":")))


def open_configuration(envelope: str) -> ProviderSetupInput:
    """Fail closed when a secret was rotated or the envelope is corrupt.

    Never fall back to fake providers or silently apply the environment after a
    saved configuration becomes unreadable. The operator must re-enter the keys.
    """
    plain = unseal(_PURPOSE, envelope)
    if not plain:
        raise ValueError("provider_configuration_unreadable")
    try:
        payload = json.loads(plain)
        if payload["format"] != 1:
            raise ValueError("unknown envelope version")
        return ProviderSetupInput.model_validate(payload["setup"])
    except (ValueError, KeyError, TypeError):
        # Pydantic's error structure can contain the submitted secret input.
        raise ValueError("provider_configuration_invalid") from None
