"""Validate successful selected-read evidence without guessing tool semantics."""

import hashlib

from server.clients.mcp_source import MCPEvidence, MCPSourceClient, MCPSourceError


def require_read_receipt(client: MCPSourceClient, selected: str, evidence: MCPEvidence) -> str:
    receipt = evidence.receipt
    digest = hashlib.sha256(evidence.text.encode()).hexdigest()
    mapping = client.config.read
    operation = "tools/call" if mapping is not None else "resources/read"
    if (
        receipt is None
        or not evidence.text.strip()
        or evidence.resource_id != selected
        or receipt.requested_id != selected
        or receipt.operation != operation
        or receipt.content_sha256 != digest
        or receipt.config_sha256 != client.config_sha256
        or not (
            receipt.identity_verified is True
            or (
                mapping is not None
                and mapping.result_format == "text"
                and receipt.identity_verified is False
            )
        )
        or len(evidence.text.encode()) > client.config.limits.max_content_bytes
    ):
        raise MCPSourceError("unsupported")
    return digest
