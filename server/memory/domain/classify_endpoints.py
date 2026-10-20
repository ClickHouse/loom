"""What a namespace's `classify_endpoint` may be.

memory.set with `classify=True` posts the memory's value there, so while the
gateway is required the endpoint must be at one of LOOM_INFERENCE_GATEWAY_HOSTS:
a namespace admin can neither send content elsewhere nor probe an internal host.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator

from server.settings import settings


def check_classify_endpoint(value: str | None) -> str | None:
    """The endpoint, or ValueError (a 422) when it is off the gateway."""
    host = settings.off_gateway(value) if value and value.strip() else ""
    if host:
        raise ValueError(
            f"classify_endpoint: {host} is not in LOOM_INFERENCE_GATEWAY_HOSTS, and "
            f"{settings.gateway_required_by()} sends memory content only there"
        )
    return value


ClassifyEndpoint = Annotated[str | None, AfterValidator(check_classify_endpoint)]
