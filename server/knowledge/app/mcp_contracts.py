"""Apply deployment admission limits around the pure versioned wire codecs."""

from server.knowledge.domain import mcp_sources as wire
from server.knowledge.domain.mcp_sources import (
    ConnectionEnvelope,
    MCPSourceContractError,
    ResourceIdentity,
    SourceContract,
)
from server.settings import settings


def _envelope_size(value: str) -> None:
    try:
        exceeds = len(value.encode("utf-8")) > settings.knowledge_mcp_envelope_max_bytes
    except (ValueError, TypeError, AttributeError):
        raise MCPSourceContractError() from None
    if exceeds:
        raise MCPSourceContractError()


def encode_envelope(envelope: ConnectionEnvelope) -> str:
    encoded = wire.encode_envelope(envelope)
    _envelope_size(encoded)
    return encoded


def decode_envelope(plaintext: str) -> ConnectionEnvelope:
    _envelope_size(plaintext)
    return wire.decode_envelope(plaintext)


def validate_source_contract(contract: SourceContract) -> None:
    try:
        contract = SourceContract.model_validate(contract)
    except (ValueError, TypeError, RecursionError):
        raise MCPSourceContractError() from None
    validate_resource_selection(contract.resources)


def validate_resource_selection(resources: tuple[ResourceIdentity, ...]) -> None:
    if (
        not resources
        or len(resources) > settings.knowledge_mcp_selected_resources_max
        or len({resource.id for resource in resources}) != len(resources)
        or any(
            len(resource.id.encode("utf-8")) > settings.knowledge_mcp_resource_id_max_bytes
            for resource in resources
        )
    ):
        raise MCPSourceContractError()


def encode_source(contract: SourceContract) -> tuple[str, str]:
    validate_source_contract(contract)
    return wire.encode_source(contract)


def decode_source(config_json: str, items_json: str) -> SourceContract:
    contract = wire.decode_source(config_json, items_json)
    validate_source_contract(contract)
    return contract
