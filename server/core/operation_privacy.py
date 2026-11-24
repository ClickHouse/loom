"""Payloads kept out of diagnostics even when the caller opts into capture."""

KNOWLEDGE_SOURCE_LIVE = "knowledge.source.live"


def retains_no_payload(operation: str) -> bool:
    return operation == KNOWLEDGE_SOURCE_LIVE
