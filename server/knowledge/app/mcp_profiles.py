"""Operator read approvals and unopened clients; caller grants remain separate."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, fields, replace
from functools import lru_cache
from typing import Any, Literal

from server.clients.mcp_source import (
    MCPBearer,
    MCPLimits,
    MCPSourceClient,
    MCPSourceConfig,
    MCPSourceError,
    MCPToolMapping,
    validate_mcp_tool_mapping,
)
from server.knowledge.domain.mcp_sources import (
    ConnectionEnvelope,
    MCPSourceContractError,
    SourceContract,
    bind_envelope,
)
from server.settings import settings

MAX_CATALOG_BYTES = 8 * 1024 * 1024
MAX_PROFILES = 10_000
MAX_PROFILE_BYTES = 65_536
MAX_SCHEMA_BYTES = 16_384
MAX_PROFILE_NODES = 4096
MAX_PROFILE_DEPTH = 16


class MCPProfileError(ValueError):
    """Configuration and binding failures reveal no supplied values."""

    def __init__(self) -> None:
        super().__init__("MCP_PROFILE_INVALID")


def _text(value: Any, maximum: int, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not empty and not value.strip())
        or len(value.encode("utf-8")) > maximum
        or any(unicodedata.category(char) == "Cc" for char in value)
    ):
        raise MCPProfileError()
    return value


def _object(value: Any, allowed: set[str], required: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - allowed or required - set(value):
        raise MCPProfileError()
    return value


def _canonical(value: Any, maximum: int) -> str:
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    if len(encoded.encode("utf-8")) > maximum:
        raise MCPProfileError()
    return encoded


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MCPProfileError()
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise MCPProfileError()


def _bounded_tree(value: Any) -> None:
    remaining = MAX_PROFILE_NODES

    def visit(node: Any, depth: int, *, schema_data: bool = False) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > MAX_PROFILE_DEPTH:
            raise MCPProfileError()
        if isinstance(node, dict):
            for key, child in node.items():
                _text(key, MAX_SCHEMA_BYTES, empty=True)
                visit(
                    child,
                    depth + 1,
                    schema_data=schema_data
                    or (depth == 2 and key in {"input_schema", "output_schema"}),
                )
        elif isinstance(node, list):
            for child in node:
                visit(child, depth + 1, schema_data=schema_data)
        elif isinstance(node, str):
            if schema_data:
                if len(node.encode()) > MAX_SCHEMA_BYTES:
                    raise MCPProfileError()
            else:
                _text(node, MAX_SCHEMA_BYTES, empty=True)
        elif isinstance(node, float) and not math.isfinite(node):
            raise MCPProfileError()

    visit(value, 0)


def _strings(value: Any, count: int, size: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > count:
        raise MCPProfileError()
    result = tuple(_text(item, size) for item in value)
    if len(set(result)) != len(result):
        raise MCPProfileError()
    return result


def _mapping(value: Any, *, search: bool = False) -> MCPToolMapping | None:
    if value is None:
        return None
    data = dict(
        _object(
            value,
            {item.name for item in fields(MCPToolMapping)},
            {"name", "input_schema", "output_schema", "argument_name"},
        )
    )
    for name in ("name", "argument_name", "cursor_argument"):
        if name in data and not (name == "cursor_argument" and data[name] is None):
            _text(data[name], 128)
    for name in ("input_schema", "output_schema", "fixed_arguments"):
        if name in data:
            if not isinstance(data[name], dict):
                raise MCPProfileError()
            _canonical(data[name], MAX_SCHEMA_BYTES)
    for name in (
        "items_path",
        "id_path",
        "title_path",
        "text_path",
        "uri_path",
        "revision_path",
        "next_cursor_path",
    ):
        if name in data:
            if data[name] is None and name not in {"items_path", "id_path"}:
                continue
            path = data[name]
            if not isinstance(path, list) or len(path) > 8:
                raise MCPProfileError()
            data[name] = tuple(_text(part, 128) for part in path)
    mapping = MCPToolMapping(**data)
    validate_mcp_tool_mapping(mapping, search=search)
    return mapping


def _config(value: Any) -> MCPSourceConfig:
    data = _object(
        value,
        {"endpoint", "bearer_audience", "approved_tools", "search", "read", "limits"},
        {"endpoint"},
    )
    audience = data.get("bearer_audience")
    if audience is not None:
        audience = _text(audience, 2048)
    limits = _object(data.get("limits", {}), {item.name for item in fields(MCPLimits)}, set())
    return MCPSourceConfig(
        endpoint=_text(data["endpoint"], 2048),
        bearer_audience=audience,
        approved_tools=_strings(data.get("approved_tools", []), 2, 128),
        search=_mapping(data.get("search"), search=True),
        read=_mapping(data.get("read")),
        limits=MCPLimits(**limits),
    )


def _snapshot(config: MCPSourceConfig) -> str:
    data = asdict(config)
    # Selection has one authority: the source contract, never the operator prototype.
    del data["approved_resource_ids"]
    return _canonical(data, MAX_PROFILE_BYTES)


@dataclass(frozen=True)
class MCPProfileMetadata:
    id: str
    label: str
    auth: Literal["none", "bearer"]
    profile_sha256: str
    allow_collection_copy: bool
    approved_tools: tuple[str, ...]


@dataclass(frozen=True, repr=False)
class FrozenProfileDefinition:
    """Canonical operator profile JSON; its digest covers policy and config together."""

    metadata: MCPProfileMetadata
    allowed_orgs: frozenset[str]
    config_snapshot: str = field(repr=False)

    def allows(self, org: str) -> bool:
        return org in self.allowed_orgs or "*" in self.allowed_orgs


def _profile(value: Any) -> FrozenProfileDefinition:
    _bounded_tree(value)
    _canonical(value, MAX_PROFILE_BYTES)
    data = _object(
        value,
        {"id", "label", "auth", "allowed_orgs", "allow_collection_copy", "config"},
        {"id", "label", "auth", "allowed_orgs", "config"},
    )
    identity = _text(data["id"], 128)
    label = _text(data["label"], 256)
    auth = data["auth"]
    if auth not in ("none", "bearer"):
        raise MCPProfileError()
    orgs = _strings(data["allowed_orgs"], 256, 256)
    copy_allowed = data.get("allow_collection_copy", False)
    if type(copy_allowed) is not bool:
        raise MCPProfileError()
    config = _config(data["config"])
    if (auth == "bearer") != (config.bearer_audience is not None):
        raise MCPProfileError()
    snapshot = _snapshot(config)
    approval = {
        "id": identity,
        "label": label,
        "auth": auth,
        "allowed_orgs": sorted(orgs),
        "allow_collection_copy": copy_allowed,
        "config": json.loads(snapshot),
    }
    snapshot = _canonical(approval, MAX_PROFILE_BYTES)
    digest = hashlib.sha256(snapshot.encode()).hexdigest()
    metadata = MCPProfileMetadata(
        identity, label, auth, digest, copy_allowed, config.approved_tools
    )
    return FrozenProfileDefinition(metadata, frozenset(orgs), snapshot)


@dataclass(frozen=True)
class MCPProfilePage:
    items: tuple[MCPProfileMetadata, ...]
    next_after: str | None = None


@dataclass(frozen=True, repr=False)
class MCPProfileCatalog:
    """Parse only trusted operator configuration; request arguments select an ID later."""

    _profiles: tuple[FrozenProfileDefinition, ...] = ()
    sha256: str = hashlib.sha256(b"[]").hexdigest()

    @classmethod
    def parse(cls, payload: str | bytes | None = None) -> MCPProfileCatalog:
        try:
            if payload is None:
                return cls()
            if isinstance(payload, bytes):
                if len(payload) > MAX_CATALOG_BYTES:
                    raise MCPProfileError()
                payload = payload.decode("utf-8")
            if not isinstance(payload, str) or len(payload.encode()) > MAX_CATALOG_BYTES:
                raise MCPProfileError()
            data = _object(
                json.loads(payload, object_pairs_hook=_pairs, parse_constant=_nonfinite),
                {"version", "profiles"},
                {"version", "profiles"},
            )
            if type(data["version"]) is not int or data["version"] != 1:
                raise MCPProfileError()
            rows = data["profiles"]
            if not isinstance(rows, list) or len(rows) > MAX_PROFILES:
                raise MCPProfileError()
            profiles = tuple(sorted((_profile(row) for row in rows), key=lambda p: p.metadata.id))
            if sum(len(p.config_snapshot.encode()) for p in profiles) > MAX_CATALOG_BYTES:
                raise MCPProfileError()
            if len({p.metadata.id for p in profiles}) != len(profiles):
                raise MCPProfileError()
            digest = hashlib.sha256(
                _canonical(
                    [p.metadata.profile_sha256 for p in profiles], MAX_CATALOG_BYTES
                ).encode()
            ).hexdigest()
            return cls(profiles, digest)
        except (ValueError, TypeError, RecursionError, OverflowError, MCPSourceError):
            raise MCPProfileError() from None

    def get(self, org: str, profile_id: str) -> FrozenProfileDefinition:
        try:
            _text(org, 256)
            _text(profile_id, 128)
            for profile in self._profiles:
                if profile.metadata.id == profile_id and profile.allows(org):
                    return profile
        except (ValueError, TypeError):
            pass
        raise MCPProfileError() from None

    def page(
        self, org: str, query: str = "", after: str | None = None, limit: int = 100
    ) -> MCPProfilePage:
        try:
            _text(org, 256)
            needle = _text(query, 256, empty=True).casefold()
            if after is not None:
                _text(after, 128)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise MCPProfileError()
            items: list[MCPProfileMetadata] = []
            for profile in self._profiles:
                if not profile.allows(org):
                    continue
                metadata = profile.metadata
                if needle not in metadata.id.casefold() and needle not in metadata.label.casefold():
                    continue
                if after is not None and metadata.id <= after:
                    continue
                items.append(metadata)
                if len(items) > limit:
                    return MCPProfilePage(tuple(items[:limit]), items[limit - 1].id)
            return MCPProfilePage(tuple(items))
        except (ValueError, TypeError):
            raise MCPProfileError() from None


def build_mcp_source_client(
    profile: FrozenProfileDefinition,
    envelope: ConnectionEnvelope,
    *,
    org: str,
    owner: str,
    slot: str,
    generation: str,
    validate_access: Callable[[], Awaitable[None]],
    contract: SourceContract | None = None,
) -> MCPSourceClient:
    """Bind sealed credentials to current approval; fresh access checks run on use.

    The caller supplies verified context and rejects local credentials. Its callback
    rechecks grants, fleet readiness, source, profile and owner standing. Collection
    copy approval is a read policy constraint; publication still needs its own grant.
    """
    try:
        if not isinstance(profile, FrozenProfileDefinition) or not callable(validate_access):
            raise MCPProfileError()
        if not isinstance(envelope, ConnectionEnvelope):
            raise MCPProfileError()
        envelope = ConnectionEnvelope.model_validate(envelope)
        if not profile.allows(org) or profile.metadata.auth != envelope.auth:
            raise MCPProfileError()
        config = _config(json.loads(profile.config_snapshot)["config"])
        bind_envelope(
            envelope,
            org=org,
            owner=owner,
            slot=slot,
            generation=generation,
            profile_id=profile.metadata.id,
            profile_sha256=profile.metadata.profile_sha256,
            endpoint=config.endpoint,
            audience=config.bearer_audience,
        )
        selected: tuple[str, ...] = ()
        if contract is not None:
            if not isinstance(contract, SourceContract):
                raise MCPProfileError()
            contract = SourceContract.model_validate(contract)
            if (
                contract.slot,
                contract.generation,
                contract.profile_id,
                contract.profile_sha256,
            ) != (
                slot,
                generation,
                profile.metadata.id,
                profile.metadata.profile_sha256,
            ):
                raise MCPProfileError()
            if (
                contract.reader_policy == "collection_copy"
                and not profile.metadata.allow_collection_copy
            ):
                raise MCPProfileError()
            selected = tuple(resource.id for resource in contract.resources)
        credential = None
        if envelope.bearer is not None and envelope.audience is not None:
            credential = MCPBearer(envelope.bearer.get_secret_value(), envelope.audience)
        return MCPSourceClient(
            replace(config, approved_resource_ids=selected),
            credential,
            validate_access=validate_access,
        )
    except (ValueError, TypeError, RecursionError, MCPSourceError, MCPSourceContractError):
        raise MCPProfileError() from None


@lru_cache(maxsize=1)
def _catalog_from_setting(payload: str) -> MCPProfileCatalog:
    if not isinstance(payload, str):
        raise MCPProfileError()
    return MCPProfileCatalog.parse(None if payload == "" else payload)


def load_mcp_profile_catalog() -> MCPProfileCatalog:
    """Read trusted operator configuration; retain at most one immutable snapshot."""
    return _catalog_from_setting(settings.knowledge_source_mcp_profiles_json)
