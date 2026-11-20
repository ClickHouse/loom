"""Wire dedicated existing clients to bounded physical transport accounting."""

from __future__ import annotations

import uuid

from .adapters import BedrockProvider, SDKContext


def bind_bedrock(client, model_id: str, *, approved_model_id: str, audit):
    """Attach public botocore hooks before exposing the provider to the agent."""
    provider = BedrockProvider(
        client, model_id, approved_model_id=approved_model_id, physical_counter=audit.count
    )
    if client.meta.service_model.service_name != "bedrock-runtime":
        raise ValueError("A dedicated Bedrock runtime client is required")
    identity = "factory-" + uuid.uuid4().hex
    client.meta.events.register(
        "before-send.bedrock-runtime.Converse", audit.before_send, unique_id=identity + "-send"
    )
    client.meta.events.register(
        "after-call.bedrock-runtime.Converse", audit.after_call, unique_id=identity + "-response"
    )
    return provider


def bind_context(agent, verify_scope, *, expected_scope: dict, audit):
    """Use the pinned SDK transport without replacing its auth or request hooks.

    The SDK's HTTP client is an internal attribute, so the live protocol must
    lock the SDK source and qualify this binding. Static existing credentials
    avoid unobserved login/refresh traffic in a separate authentication client.
    """
    transport = agent.transport
    if transport._auth is not None or transport.session_protection is not None:
        raise PermissionError("Authentication/session side traffic is not qualified")
    context = SDKContext(agent, verify_scope, audit.count, expected_scope=expected_scope)
    transport._client.event_hooks["request"].append(audit.request)
    transport._client.event_hooks["response"].insert(0, audit.response)
    return context
