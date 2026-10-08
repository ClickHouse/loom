"""The sign-in and sign-up models keep their credentials out of `repr`, so a
model that reaches a log line or a traceback never prints a password, a
token or a claim code; the wire shape is unchanged."""

from __future__ import annotations

from datetime import datetime

from server.auth.login import LoginInput, LoginOutput
from server.auth.self_signup import AgentSignupOutput, SelfSignupInput, SelfSignupOutput

_CANARY = "canary-credential-0123456789"


def test_credentials_stay_out_of_repr_and_in_the_payload() -> None:
    models = [
        LoginInput(identifier="avery@example.com", password=_CANARY),
        LoginOutput(
            token=_CANARY, subject="avery", org="o", namespaces=["n"], expires_at=datetime.now()
        ),
        SelfSignupInput(namespace="n", email="avery@example.com", password=_CANARY),
        SelfSignupOutput(token=_CANARY, namespace="n", org="o", subject="n"),
        AgentSignupOutput(
            token=_CANARY,
            namespace="agent-n",
            org="o",
            subject="sa:agent-n-abc123",
            claim_code=_CANARY + "-code",
            claim_url="https://loom.example/me",
        ),
    ]
    for m in models:
        assert _CANARY not in repr(m) and _CANARY not in str(m), type(m).__name__
        assert _CANARY in m.model_dump_json(), type(m).__name__
