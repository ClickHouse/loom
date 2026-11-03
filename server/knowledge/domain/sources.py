"""Document sources a repository's maintainers register (`knowledge.source.*`):
the kinds, the name rule and each kind's configuration. Pure: no I/O.

A source's configuration holds ids and filters only, never a host or a URL: each
kind's API host is a constant of its client module, so no caller-supplied host is
ever connected to.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from server.tracker.domain.sources import SOURCE_NAME_RE

# The kinds a source may be. Each has a config parser here and a probe in
# `server.knowledge.app.source_probes`.
KINDS = ("gdrive",)

# A shared-folder store's file kinds, by the name a config's `include` uses.
GDRIVE_MIME_TYPES = {
    "docs": "application/vnd.google-apps.document",
    "slides": "application/vnd.google-apps.presentation",
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
# A folder id goes into the store's query string, so only its own alphabet passes.
_FOLDER_ID_RE = re.compile(r"[A-Za-z0-9_-]{10,128}")
MAX_FOLDERS = 20  # not a setting: the add op's wire contract (1 to 20 folders)

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


@dataclass(frozen=True)
class KnowledgeSource:
    """One registry row of `loom.knowledge_sources`; the credential stays sealed."""

    org: str
    namespace: str
    name: str
    kind: str
    # One registration's id: written once by add, carried by every later write.
    source_id: str
    config: dict[str, Any] = field(default_factory=dict)
    credential_enc: str = ""
    created_by: str = ""
    active: bool = True
    # On an inactive row: the maintainer asked for the source's documents to be retired.
    pending_delete: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SourceConfigError(ValueError):
    """A config a kind cannot use; the message names what to fix."""


def normalize_name(name: str) -> str:
    return name.strip().lower()


def valid_name(name: str) -> bool:
    return bool(SOURCE_NAME_RE.fullmatch(name))


def state_key(name: str) -> str:
    return STATE_KEY_PREFIX + name


def source_tag(name: str) -> str:
    """The tag every document a source wrote carries."""
    return f"source:{name}"


def source_tags(tags: list[str]) -> set[str]:
    """The `source:*` tags among `tags`."""
    return {t for t in tags if t.startswith("source:")}


def parse_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    """The stored form of `config` for `kind`; SourceConfigError when unusable."""
    if kind == "gdrive":
        return _gdrive_config(config)
    raise SourceConfigError(f"no config rule for kind {kind!r}")


def _gdrive_config(config: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(config) - {"folder_ids", "include"})
    if unknown:
        raise SourceConfigError(f"unknown config keys: {', '.join(unknown)}")
    folder_ids = config.get("folder_ids")
    if not isinstance(folder_ids, list) or not folder_ids:
        raise SourceConfigError("config.folder_ids: a list of 1 to 20 folder ids")
    folders: list[str] = []
    for folder in folder_ids:
        if not isinstance(folder, str) or not _FOLDER_ID_RE.fullmatch(folder):
            raise SourceConfigError(
                f"config.folder_ids: {folder!r} is not a folder id (letters, digits, - and _)"
            )
        if folder not in folders:
            folders.append(folder)
    if len(folders) > MAX_FOLDERS:
        raise SourceConfigError(f"config.folder_ids: at most {MAX_FOLDERS} folders")
    include = config.get("include", list(GDRIVE_MIME_TYPES))
    if not isinstance(include, list) or not include:
        raise SourceConfigError("config.include: a list of " + ", ".join(GDRIVE_MIME_TYPES))
    # Type first: a list or an object element is unhashable for the membership test.
    bad = [k for k in include if not isinstance(k, str) or k not in GDRIVE_MIME_TYPES]
    if bad:
        raise SourceConfigError(
            f"config.include: {bad[0]!r} is not one of " + ", ".join(GDRIVE_MIME_TYPES)
        )
    # Stored in the table's own order, so two equal configs store the same text.
    return {"folder_ids": folders, "include": [k for k in GDRIVE_MIME_TYPES if k in include]}


def json_object(raw: str) -> dict[str, Any]:
    """A stored JSON object (a source's config, a sync state), or {} when it is not one."""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# The parts of a stored sync state a reader sees, with their types. The rest
# (`full`, `full_listed_at`, `extractor_waits`) is the worker's own bookkeeping.
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
}


def sync_of(detail: str) -> dict[str, Any]:
    """The parts of a stored sync state a reader sees, each of its type; {} before
    any request or pass."""
    state = json_object(detail)
    return {k: v for k, t in SYNC_FIELDS.items() if isinstance(v := state.get(k), t)}


__all__ = [
    "GDRIVE_MIME_TYPES",
    "KINDS",
    "KnowledgeSource",
    "MAX_FOLDERS",
    "STATE_KEY_PREFIX",
    "SYNC_FIELDS",
    "SYNC_WORKER_ACTOR",
    "SourceConfigError",
    "json_object",
    "normalize_name",
    "parse_config",
    "source_tag",
    "source_tags",
    "state_key",
    "sync_of",
    "valid_name",
]
