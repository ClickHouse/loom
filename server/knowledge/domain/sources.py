"""Recording admission and opaque document-source lifecycle metadata. Pure: no I/O."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from server.core.errors import BadConfigError
from server.tracker.domain.sources import SOURCE_NAME_RE

# The kinds a source may be. Each has a config parser here and a probe in
# `server.knowledge.app.source_probes`.
KINDS = ("recording",)

# Zoom recording registrations use numeric meeting ids.
RECORDING_ID_RE = {
    "zoom": re.compile(r"[0-9]{9,12}"),
}
MAX_RECORDING_IDS = 20  # not a setting: the add op's wire contract (1 to 20 ids)

# The actor the sync worker writes a source's documents as. Its kind is neither
# `human` nor `process`, the only kinds a token maps to, so no request writes as
# it. A source's documents are the live documents tagged `source:<name>` that this
# actor wrote. Their `source:*` tags belong to the worker: a write by anyone else
# that keeps the worker as the author (a re-save of the same text, a tag edit) may
# not change them (SOURCE_TAGS_LOCKED). A caller tags its own documents freely, and
# such a document is never a source's.
SYNC_WORKER_ACTOR = "worker:knowledge-source-sync"

# The sync state of a source lives in loom.code_index_state under this key.
STATE_KEY_PREFIX = "knowledge_source:"


# `KnowledgeSource.access`: who a source's documents are shown to.
NAMESPACE_ACCESS = "namespace"  # every reader of the namespace
MIRRORED_ACCESS = "mirrored"  # a reader the store admits, from the namespace's mirror


@dataclass(frozen=True)
class KnowledgeSource:
    """One registry row of `loom.knowledge_sources`; the credential stays sealed."""

    org: str
    namespace: str
    name: str
    kind: str
    # One registration's id: written once by add, carried by every later write.
    source_id: str
    # A shared recording's provider and selected IDs; opaque rows keep raw JSON below.
    config: dict[str, Any] = field(default_factory=dict, repr=False)
    # Retained lifecycle metadata, never interpreted as native source selections.
    items: list[dict[str, Any]] = field(default_factory=list, repr=False)
    # Existing sealed columns retain their distinct encryption purposes.
    credential_enc: str = field(default="", repr=False)
    secret_enc: str = field(default="", repr=False)
    # Who the documents are shown to: NAMESPACE_ACCESS or MIRRORED_ACCESS; a later build
    # may write another value, which `served` refuses.
    access: str = NAMESPACE_ACCESS
    credential_mode: str = "shared"
    # Connected authority is explicit; created_by remains audit attribution only.
    owner_subject: str = ""
    connection_id: str = ""
    created_by: str = ""
    active: bool = True
    # On an inactive row: an admin asked for the source's documents to be retired.
    pending_delete: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # The namespace's mirror, which the sync sets before a mirrored source's turn; never
    # stored.
    mirror: str = ""
    source_contract_version: int = 0
    connection_slot: str = ""
    # Lifecycle writes preserve unknown payloads without interpreting or exposing them.
    generic_config_json: str = field(default="", repr=False)
    generic_items_json: str = field(default="", repr=False)


def opaque_payload(*, kind: str, mode: str, version: int) -> bool:
    """Only shared recording rows have an interpreted configuration."""
    return kind != "recording" or mode != "shared" or version != 0


def shown(value: Any) -> str:
    """A refused value as a message quotes it: at most 40 characters of it, quoted, a cut
    one marked, so a refusal never grows with what a caller sent."""
    if isinstance(value, str):
        return repr(value) if len(value) <= 40 else f"{value[:40]!r}…"
    text = repr(value)
    return text if len(text) <= 40 else f"{text[:40]}…"


def unknown_keys(keys: list[str]) -> str:
    """Unknown keys as a refusal names them: the first, quoted, and how many more."""
    more = f" and {len(keys) - 1} more" if len(keys) > 1 else ""
    return f"key {shown(keys[0])}{more}"


def normalize_name(name: str) -> str:
    return name.strip().lower()


def valid_name(name: str) -> bool:
    return bool(SOURCE_NAME_RE.fullmatch(name))


def state_key(name: str) -> str:
    return STATE_KEY_PREFIX + name


def source_key(org: str, namespace: str, name: str) -> str:
    """A source's key in the sync's cursor, its states and its turn lease."""
    return f"{org}/{namespace}/{name}"


def turn_lease_name(org: str, namespace: str, name: str) -> str:
    """The lease a source's sync turn holds; build N-1 takes the same name."""
    return f"knowledge-source-sync:{source_key(org, namespace, name)}"


def source_tag(name: str) -> str:
    """The tag every document a source wrote carries."""
    return f"source:{name}"


def source_id_tag(source_id: str) -> str:
    """The tag that makes a mirrored document its source's: in a mirror, ownership goes
    by the source's id, so a later source of the same name owns none of an earlier
    one's documents."""
    return f"source-id:{source_id}"


def mirror_uri(address: str, source_id: str) -> str:
    """A mirrored document's `source_uri`: the file's address and its source's id, so
    each source that reaches a file keeps its own copy and list."""
    return address + mirror_uri_suffix(source_id)


def mirror_uri_suffix(source_id: str) -> str:
    """The end of every `source_uri` a mirrored source writes."""
    return f"#{source_id}"


def home(src: KnowledgeSource) -> tuple[str, str]:
    """Where the source's documents live and the tag that marks them its own: the
    namespace and `source:<name>`, or for a mirrored source its mirror and
    `source-id:<id>`. ValueError for an `access` this build does not know, or a
    mirrored source whose mirror is not known."""
    if src.access == NAMESPACE_ACCESS:
        return src.namespace, source_tag(src.name)
    if src.access != MIRRORED_ACCESS:
        raise ValueError("a source's access is not one this build knows")
    if not src.mirror:
        raise ValueError("a mirrored source's mirror is not known")
    return src.mirror, source_id_tag(src.source_id)


def source_tags(tags: list[str]) -> set[str]:
    """The `source:*` tags among `tags`."""
    return {t for t in tags if t.startswith("source:")}


def parse_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    if kind != "recording":
        raise BadConfigError("this source kind is unavailable")
    return _recording_config(config)


def _recording_config(config: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(config) - {"provider", "ids"})
    if unknown:
        raise BadConfigError(f"unknown config {unknown_keys(unknown)}")
    provider = config.get("provider")
    if not isinstance(provider, str) or provider not in RECORDING_ID_RE:
        raise BadConfigError("config.provider: one of " + ", ".join(RECORDING_ID_RE))
    id_re = RECORDING_ID_RE[provider]
    raw_ids = config.get("ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise BadConfigError(f"config.ids: a list of 1 to {MAX_RECORDING_IDS} ids")
    ids: list[str] = []
    for raw in raw_ids:
        # A meeting id may come as a JSON number; a bool is no id.
        value = str(raw) if isinstance(raw, int) and not isinstance(raw, bool) else raw
        if not isinstance(value, str) or not id_re.fullmatch(value):
            raise BadConfigError(f"config.ids: {shown(raw)} is not a meeting id (digits)")
        if value not in ids:
            ids.append(value)
    if len(ids) > MAX_RECORDING_IDS:
        raise BadConfigError(f"config.ids: at most {MAX_RECORDING_IDS} ids")
    return {"provider": provider, "ids": ids}


def served(src: KnowledgeSource) -> bool:
    """Only supported shared recordings are ordinary namespace documents."""
    return (
        src.kind == "recording"
        and src.credential_mode == "shared"
        and src.source_contract_version == 0
        and src.access == NAMESPACE_ACCESS
    )


def mirror_synced(src: KnowledgeSource) -> bool:
    """Only the supported connected MCP contract can publish protected copies."""
    return (
        src.kind == "mcp"
        and src.credential_mode == "connected"
        and type(src.source_contract_version) is int
        and src.source_contract_version == 1
        and src.access == MIRRORED_ACCESS
    )


def id_list_config(src: KnowledgeSource) -> dict[str, Any] | None:
    """Recording config for a served row; opaque registrations expose no config."""
    return src.config if served(src) else None


def stored_config(src: KnowledgeSource) -> dict[str, Any]:
    """Recording config; opaque lifecycle writes use their original JSON bytes."""
    return src.config


def json_object(raw: str) -> dict[str, Any]:
    """A stored JSON object (a source's config, a sync state), or {} when it is not one."""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# The parts of a stored sync state a reader sees, with their types. The rest
# (`full`, `full_listed_at`, `file_waits`, `waits_reset_for`, `retry_granted_for`, and
# legacy checkpoints) is the worker's own bookkeeping.
SYNC_FIELDS: dict[str, type] = {
    "requested_at": str,
    "last_synced_at": str,
    "status": str,
    "documents_seen": int,
    "changed": int,
    "removed": int,
    "unconverted": int,
    "unconverted_reasons": dict,
    "skipped": int,
    "error": str,
    "pending_extract": int,
    "last_extracted_at": str,
    "extracted": int,
    "flagged_pinned": int,
    "unextracted": int,
    "extract_error": str,
}


def sync_of(detail: str) -> dict[str, Any]:
    """The parts of a stored sync state a reader sees, each of its type; {} before
    any request or pass."""
    state = json_object(detail)
    return {k: v for k, t in SYNC_FIELDS.items() if isinstance(v := state.get(k), t)}


# What a namespace's readers see of a mirrored source's sync: never its counts, its
# reasons or its error text, which describe what the mirror holds.
_MIRRORED_SYNC_SHOWN = frozenset({"requested_at", "last_synced_at", "status"})


def sync_shown(src: KnowledgeSource, detail: str) -> dict[str, Any]:
    """The sync state the namespace's readers are shown for `src` (`sync_of`)."""
    shown = sync_of(detail)
    if served(src):
        return shown
    return {k: v for k, v in shown.items() if k in _MIRRORED_SYNC_SHOWN}


__all__ = [
    "KINDS",
    "KnowledgeSource",
    "MAX_RECORDING_IDS",
    "MIRRORED_ACCESS",
    "NAMESPACE_ACCESS",
    "RECORDING_ID_RE",
    "STATE_KEY_PREFIX",
    "SYNC_FIELDS",
    "SYNC_WORKER_ACTOR",
    "home",
    "id_list_config",
    "json_object",
    "mirror_synced",
    "mirror_uri",
    "mirror_uri_suffix",
    "normalize_name",
    "opaque_payload",
    "parse_config",
    "served",
    "shown",
    "source_id_tag",
    "source_tag",
    "source_tags",
    "state_key",
    "stored_config",
    "sync_of",
    "sync_shown",
    "unknown_keys",
    "valid_name",
]
