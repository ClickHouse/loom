"""Admin setup for provider notifications; source reads never reveal this state."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PushSourceInput(BaseModel):
    org: str = Field(..., description="The org.")
    namespace: str = Field(..., description="The source's namespace.")
    name: str = Field(..., max_length=100, description="The registered source name.")


class PushConfigureInput(PushSourceInput):
    """Enable provider sync hints for an existing source, or disable its setup.
    Polling remains enabled. Requires a namespace admin and a configured HTTPS
    public base URL. Drive creates bounded change channels; Notion requires
    manual verification before confirmation; Linear takes its webhook secret
    at confirmation after the provider has its notification address."""

    enabled: bool = Field(default=True, description="Enable a fresh setup; false disables push.")
    agent: str = Field(default="-", description="The configuring agent, default '-'.")
    user_id: str = Field(default="-", description="The configuring user, default '-'.")
    session_id: str = Field(default="-", description="The configuring session, default '-'.")


class PushStatusInput(PushSourceInput):
    """Read push setup as a namespace admin.

    Explicit reveal returns only a pending Notion verification token, for copying
    to the provider's verification form.
    """

    reveal_challenge: bool = Field(
        default=False, description="Reveal a pending Notion challenge, default false."
    )


class PushConfirmInput(PushSourceInput):
    """Activate the exact pending setup.

    For Notion, first verify in the provider UI and name the inspected challenge
    fingerprint. For Linear, supply the signing secret of the webhook created
    with this generation's notification address.
    """

    generation: str = Field(..., max_length=36, description="The pending setup generation.")
    challenge_fingerprint: str = Field(
        default="",
        max_length=64,
        description="The inspected Notion challenge's SHA-256 fingerprint; empty for Linear.",
    )
    signing_secret: str = Field(
        default="",
        max_length=512,
        repr=False,
        description="The Linear webhook signing secret; empty for Notion.",
    )


class PushStatusOutput(BaseModel):
    enabled: bool = False
    status: str = "disabled"
    provider: str = ""
    generation: str = ""
    callback_url: str = ""
    provider_id: str = ""
    challenge_fingerprint: str = ""
    verification_token: str = Field(default="", repr=False)
    channels: int = 0
    expires_at: float = 0
