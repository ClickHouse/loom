"""Document sources a namespace's admins register (`knowledge.source.*`): the
kinds, the name rule, each kind's items and the configuration form a build
without items reads. Pure: no I/O.

A source's items and configuration hold ids and filters only, never a host or a
URL: each kind's API host is a constant of its client module, so no
caller-supplied host is ever connected to.

A `gdrive`, `notion` or `linear` source reads a list of items (`parse_items`);
`config` in the id-list form (`folder_ids`, `page_ids`, `project_ids`) is turned into
items (`items_from_config`) and written alongside them while every item fits in
it (`config_from_items`). A `recording` source keeps its config and has no items.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from server.core.errors import BadConfigError
from server.tracker.domain.sources import SOURCE_NAME_RE

# The kinds a source may be. Each has a config parser here and a probe in
# `server.knowledge.app.source_probes`.
KINDS = ("gdrive", "linear", "notion", "recording")

# The shared-folder store's type of a folder: a folder, never a file the sync reads.
_FOLDER_MIME = "application/vnd.google-apps.folder"
# A shared-folder store's file kinds, by the name a config's `include` uses.
GDRIVE_MIME_TYPES = {
    "docs": "application/vnd.google-apps.document",
    "slides": "application/vnd.google-apps.presentation",
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
# The kinds a config without `include` takes. `docx` is read when a config names it and
# is left out of the default, so a build that does not know it still syncs every source
# a default config made (docs/architecture/rolling-upgrades.md).
GDRIVE_DEFAULT_INCLUDE = ("docs", "slides", "pdf", "pptx")
# A folder id goes into the store's query string, so only its own alphabet passes.
_FOLDER_ID_RE = re.compile(r"[A-Za-z0-9_-]{10,128}")
MAX_FOLDERS = 20  # not a setting: the add op's wire contract (1 to 20 folders)
# A tracker project id is a UUID; it travels as a GraphQL variable.
_PROJECT_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MAX_PROJECTS = 20  # not a setting: the add op's wire contract (1 to 20 projects)
# A page store's page and database ids, together, at most.
MAX_PAGE_STORE_IDS = 20  # not a setting: the add op's wire contract, few enough to probe each

# A meeting store's recordings, by `provider`: a meeting space's id (it goes into a
# request path and a listing filter) or a numeric meeting id.
RECORDING_ID_RE = {
    "meet": re.compile(r"[A-Za-z0-9_-]{1,128}"),
    "zoom": re.compile(r"[0-9]{9,12}"),
}
MAX_RECORDING_IDS = 20  # not a setting: the add op's wire contract (1 to 20 ids)

# A source's items, at most.
MAX_ITEMS = 300  # not a setting: the add and update ops' wire contract
# Items a probe checks, at most: every item while there are this many, else the
# ones added last.
SAMPLE_ITEMS = 20  # not a setting: the add and update ops' wire contract
# A source's walk of its recursive folders: levels of subfolders, and subfolders in all,
# at most. The roots are walked together, so a turn spends a bounded number of queries
# however many recursive items the source holds.
MAX_DEPTH = 8  # not a setting: the items' wire contract
MAX_WALK_FOLDERS = 500  # not a setting: the items' wire contract
# Each kind's item types and the fields each takes besides `type`.
ITEM_FIELDS: dict[str, dict[str, frozenset[str]]] = {
    "gdrive": {
        "folder": frozenset({"id", "recursive"}),
        "file": frozenset({"id"}),
        "shared_drive": frozenset({"id"}),
        "filter": frozenset({"name_contains", "kinds", "modified_after", "in"}),
    },
    "notion": {"page": frozenset({"id"}), "database": frozenset({"id"})},
    "linear": {"project": frozenset({"id", "slug_id"})},
}
# A filter's fields go into a store query (escaped by the client), so each has a
# narrow pattern: a name fragment of printable characters and an ISO date.
_NAME_FRAGMENT_RE = re.compile(r"[^\x00-\x1f\x7f]{1,100}")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A tracker project's short id, as its link carries it; the probe resolves it to the
# project's UUID.
_PROJECT_SLUG_ID_RE = re.compile(r"[0-9a-f]{12}")

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
    # The options the source reads (`options_in`): a gdrive source's `include`, a
    # recording source's provider and ids. The store writes `stored_config(src)`.
    config: dict[str, Any] = field(default_factory=dict)
    # The things the source reads (`parse_items`); a row an older build wrote has them
    # filled from `config_json` by the store.
    items: list[dict[str, Any]] = field(default_factory=list)
    # The sealed credential: in `credential_enc` while the source fits the id-list config
    # (a build without items can use it), else in `secret_enc`.
    credential_enc: str = ""
    secret_enc: str = ""
    # Who the documents are shown to: NAMESPACE_ACCESS or MIRRORED_ACCESS; a later build
    # may write another value, which `served` refuses.
    access: str = NAMESPACE_ACCESS
    created_by: str = ""
    active: bool = True
    # On an inactive row: an admin asked for the source's documents to be retired.
    pending_delete: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


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


def source_tags(tags: list[str]) -> set[str]:
    """The `source:*` tags among `tags`."""
    return {t for t in tags if t.startswith("source:")}


def parse_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    """The stored form of `config` for `kind`; BadConfigError when unusable."""
    if kind == "gdrive":
        return _gdrive_config(config)
    if kind == "linear":
        return _linear_config(config)
    if kind == "notion":
        return _notion_config(config)
    if kind == "recording":
        return _recording_config(config)
    raise BadConfigError(f"no config rule for kind {shown(kind)}")


def _gdrive_config(config: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(config) - {"folder_ids", "include"})
    if unknown:
        raise BadConfigError(f"unknown config {unknown_keys(unknown)}")
    folder_ids = config.get("folder_ids")
    if not isinstance(folder_ids, list) or not folder_ids:
        raise BadConfigError("config.folder_ids: a list of 1 to 20 folder ids")
    folders: list[str] = []
    for folder in folder_ids:
        if not isinstance(folder, str) or not _FOLDER_ID_RE.fullmatch(folder):
            raise BadConfigError(
                f"config.folder_ids: {shown(folder)} is not a folder id (letters, digits, - and _)"
            )
        if folder not in folders:
            folders.append(folder)
    if len(folders) > MAX_FOLDERS:
        raise BadConfigError(f"config.folder_ids: at most {MAX_FOLDERS} folders")
    include = parse_include(config.get("include", list(GDRIVE_DEFAULT_INCLUDE)), "config.include")
    return {"folder_ids": folders, "include": include}


def parse_include(include: Any, where: str = "include") -> list[str]:
    """A `gdrive` source's file kinds, in the table's own order, so two equal
    configs store the same text; BadConfigError when unusable."""
    if not isinstance(include, list) or not include:
        raise BadConfigError(f"{where}: a list of " + ", ".join(GDRIVE_MIME_TYPES))
    # Type first: a list or an object element is unhashable for the membership test.
    bad = [k for k in include if not isinstance(k, str) or k not in GDRIVE_MIME_TYPES]
    if bad:
        raise BadConfigError(
            f"{where}: {shown(bad[0])} is not one of " + ", ".join(GDRIVE_MIME_TYPES)
        )
    return [k for k in GDRIVE_MIME_TYPES if k in include]


def _linear_config(config: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(config) - {"project_ids"})
    if unknown:
        raise BadConfigError(f"unknown config {unknown_keys(unknown)}")
    project_ids = config.get("project_ids")
    if not isinstance(project_ids, list) or not project_ids:
        raise BadConfigError("config.project_ids: a list of 1 to 20 project ids")
    projects: list[str] = []
    for project in project_ids:
        # Lower-cased: a document's project is compared with these ids as the API
        # reports them.
        value = project.strip().lower() if isinstance(project, str) else project
        if not isinstance(value, str) or not _PROJECT_ID_RE.fullmatch(value):
            raise BadConfigError(
                f"config.project_ids: {shown(project)} is not a project id (a UUID)"
            )
        if value not in projects:
            projects.append(value)
    if len(projects) > MAX_PROJECTS:
        raise BadConfigError(f"config.project_ids: at most {MAX_PROJECTS} projects")
    return {"project_ids": projects}


def _page_store_ids(config: dict[str, Any], key: str) -> list[str]:
    raw = config.get(key, [])
    if not isinstance(raw, list):
        raise BadConfigError(f"config.{key}: a list of ids")
    ids: list[str] = []
    for value in raw:
        normal = _page_store_id(value) if isinstance(value, str) else ""
        if not normal:
            raise BadConfigError(f"config.{key}: {shown(value)} is not an id (32 hex digits)")
        if normal not in ids:
            ids.append(normal)
    return ids


def _page_store_id(value: str) -> str:
    """The dashed lower-case form of an id given with or without dashes, or ""."""
    hexes = value.strip().lower().replace("-", "")
    if not re.fullmatch(r"[0-9a-f]{32}", hexes):
        return ""
    return f"{hexes[:8]}-{hexes[8:12]}-{hexes[12:16]}-{hexes[16:20]}-{hexes[20:]}"


def _notion_config(config: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(config) - {"page_ids", "database_ids"})
    if unknown:
        raise BadConfigError(f"unknown config {unknown_keys(unknown)}")
    pages = _page_store_ids(config, "page_ids")
    databases = _page_store_ids(config, "database_ids")
    if not 1 <= len(pages) + len(databases) <= MAX_PAGE_STORE_IDS:
        raise BadConfigError(
            f"config.page_ids and config.database_ids: 1 to {MAX_PAGE_STORE_IDS} ids in all"
        )
    return {"page_ids": pages, "database_ids": databases}


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
        if isinstance(value, str) and provider == "meet":
            value = value.removeprefix("spaces/")
        if not isinstance(value, str) or not id_re.fullmatch(value):
            what = "a space id" if provider == "meet" else "a meeting id (digits)"
            raise BadConfigError(f"config.ids: {shown(raw)} is not {what}")
        if value not in ids:
            ids.append(value)
    if len(ids) > MAX_RECORDING_IDS:
        raise BadConfigError(f"config.ids: at most {MAX_RECORDING_IDS} ids")
    return {"provider": provider, "ids": ids}


def parse_items(kind: str, items: list[Any], where: str = "items") -> list[dict[str, Any]]:
    """The stored form of `items` for `kind`, duplicates dropped (a later item of the
    same type and id replaces the earlier one); BadConfigError when unusable, naming
    the field the caller sent them in (`where`). It does not count them: the op does, once
    short ids are resolved."""
    return unique_items([_item(kind, raw, f"{where}[{n}]") for n, raw in enumerate(items)])


def unique_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`items` with a later one of the same type and id replacing an earlier one."""
    found: dict[str, dict[str, Any]] = {}
    for item in items:
        key = item_key(item)
        found.pop(key, None)  # re-inserted, so a later item also takes the later place
        found[key] = item
    return list(found.values())


def check_filters(items: list[dict[str, Any]], include: list[str]) -> None:
    """Refuse a filter whose `kinds` share none with `include`: it would list nothing on
    every turn."""
    for item in items:
        if item["type"] == "filter" and "kinds" in item and not set(item["kinds"]) & set(include):
            kinds = ", ".join(item["kinds"])
            raise BadConfigError(f"a filter's kinds ({kinds}): none of them is in include")


def max_items(kind: str) -> int:
    """Items a source of `kind` holds, at most. A tracker or page-store turn reads every
    item on every pass and its listing has no pass-time bound, so those keep their 20; a
    `gdrive` turn stops at the pass deadline between walk levels and between items, though
    it walks its recursive folders and reads each `file` item's metadata on every pass."""
    return {"linear": MAX_PROJECTS, "notion": MAX_PAGE_STORE_IDS}.get(kind, MAX_ITEMS)


def item_key(item: dict[str, Any]) -> str:
    """What makes two items the same: type and id, or a filter's every field."""
    if "id" in item:
        return f"{item['type']}:{item['id']}"
    if "slug_id" in item:
        return f"{item['type']}:slug:{item['slug_id']}"
    return json.dumps(item, sort_keys=True)


def _item(kind: str, raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BadConfigError(f"{where}: an object with a `type`")
    types = ITEM_FIELDS[kind]
    item_type = raw.get("type")
    if not isinstance(item_type, str) or item_type not in types:
        raise BadConfigError(f"{where}.type: one of " + ", ".join(types))
    unknown = sorted(set(raw) - types[item_type] - {"type"})
    if unknown:
        raise BadConfigError(f"{where}: unknown {unknown_keys(unknown)}")
    if item_type == "filter":
        return _filter_item(raw, where)
    if item_type == "project":
        return _project_item(raw, where)
    item_id = raw.get("id")
    if item_type in ("page", "database"):
        normal = _page_store_id(item_id) if isinstance(item_id, str) else ""
        if not normal:
            raise BadConfigError(f"{where}.id: {shown(item_id)} is not an id (32 hex digits)")
        return {"type": item_type, "id": normal}
    if not isinstance(item_id, str) or not _FOLDER_ID_RE.fullmatch(item_id):
        raise BadConfigError(
            f"{where}.id: {shown(item_id)} is not an id (letters, digits, - and _)"
        )
    if item_type != "folder":
        return {"type": item_type, "id": item_id}
    recursive = raw.get("recursive", False)
    if not isinstance(recursive, bool):
        raise BadConfigError(f"{where}.recursive: true or false")
    return {"type": "folder", "id": item_id, "recursive": recursive}


def _project_item(raw: dict[str, Any], where: str) -> dict[str, Any]:
    if ("id" in raw) == ("slug_id" in raw):
        raise BadConfigError(f"{where}: an `id` (a UUID) or a `slug_id`, not both")
    if "slug_id" in raw:
        slug_id = raw["slug_id"]
        value = slug_id.strip().lower() if isinstance(slug_id, str) else ""
        if not _PROJECT_SLUG_ID_RE.fullmatch(value):
            raise BadConfigError(f"{where}.slug_id: {shown(slug_id)} is not a project's short id")
        return {"type": "project", "slug_id": value}
    project = raw["id"]
    value = project.strip().lower() if isinstance(project, str) else ""
    if not _PROJECT_ID_RE.fullmatch(value):
        raise BadConfigError(f"{where}.id: {shown(project)} is not a project id (a UUID)")
    return {"type": "project", "id": value}


def _filter_item(raw: dict[str, Any], where: str) -> dict[str, Any]:
    item: dict[str, Any] = {"type": "filter"}
    if "name_contains" in raw:
        name = raw["name_contains"]
        if not isinstance(name, str) or not _NAME_FRAGMENT_RE.fullmatch(name.strip()):
            raise BadConfigError(f"{where}.name_contains: 1 to 100 printable characters")
        item["name_contains"] = name.strip()
    if "kinds" in raw:
        kinds = raw["kinds"]
        if (
            not isinstance(kinds, list)
            or not kinds
            or not all(isinstance(k, str) and k in GDRIVE_MIME_TYPES for k in kinds)
        ):
            raise BadConfigError(f"{where}.kinds: a list of " + ", ".join(GDRIVE_MIME_TYPES))
        item["kinds"] = [k for k in GDRIVE_MIME_TYPES if k in kinds]
    if "modified_after" in raw:
        when = raw["modified_after"]
        if not isinstance(when, str) or not _valid_date(when):
            raise BadConfigError(f"{where}.modified_after: a date, YYYY-MM-DD")
        item["modified_after"] = when
    if "in" in raw:
        folder = raw["in"]
        if not isinstance(folder, str) or not _FOLDER_ID_RE.fullmatch(folder):
            raise BadConfigError(f"{where}.in: a folder id (letters, digits, - and _)")
        item["in"] = folder
    if len(item) == 1:
        raise BadConfigError(
            f"{where}: a filter names at least one of name_contains, kinds, modified_after, in"
        )
    return item


def _valid_date(value: str) -> bool:
    if not _DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def items_from_config(kind: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    """The items the id-list `config` of `kind` names, in its order; [] for a kind
    without items. The config was checked when it was stored."""
    if kind == "gdrive":
        return [
            {"type": "folder", "id": f, "recursive": False} for f in config.get("folder_ids") or []
        ]
    if kind == "notion":
        return [{"type": "page", "id": p} for p in config.get("page_ids") or []] + [
            {"type": "database", "id": d} for d in config.get("database_ids") or []
        ]
    if kind == "linear":
        return [{"type": "project", "id": p} for p in config.get("project_ids") or []]
    return []


def config_from_items(
    kind: str, items: list[dict[str, Any]], include: list[str]
) -> dict[str, Any] | None:
    """The id-list config form of `items` (at most `max_items(kind)`, as the ops count
    them), or None when it cannot hold them (`fits_id_list`)."""
    if not fits_id_list(kind, items):
        return None
    ids = [str(item["id"]) for item in items]
    if kind == "gdrive":
        return {"folder_ids": ids, "include": list(include)}
    if kind == "notion":
        return {
            "page_ids": item_ids(items, "page"),
            "database_ids": item_ids(items, "database"),
        }
    return {"project_ids": ids}


def fits_id_list(kind: str, items: list[dict[str, Any]]) -> bool:
    """Whether the id-list config form holds `items`: not when an item is a gdrive item
    other than a plain folder, there are more folders than the form holds, or a project
    is named only by its short id."""
    if not items or not all(item.get("id") for item in items):
        return False
    if kind == "gdrive":
        plain = all(i["type"] == "folder" and not i.get("recursive") for i in items)
        return plain and len(items) <= MAX_FOLDERS
    return kind in ("notion", "linear")


def item_ids(items: list[dict[str, Any]], item_type: str) -> list[str]:
    """The ids of the items of one type, in order."""
    return [str(i["id"]) for i in items if i["type"] == item_type and i.get("id")]


def unread_file_note(mime: str) -> str:
    """Why a `file` item of the store's type `mime` lists nothing."""
    if mime == _FOLDER_MIME:
        return "a folder; add it as a folder item"
    if mime in GDRIVE_MIME_TYPES.values():
        return "not a kind the source's include names; name its kind in include"
    return f"{shown(mime)} is a type the sync does not read"


def include_mime_types(include: list[str], kinds: list[str] | None = None) -> list[str]:
    """The file types `include` names, narrowed to a filter's `kinds` when given."""
    return [GDRIVE_MIME_TYPES[k] for k in include if kinds is None or k in kinds]


def include_in(config: dict[str, Any]) -> list[str]:
    """The file kinds a `gdrive` config reads: its `include`, or the default."""
    return list(config.get("include") or GDRIVE_DEFAULT_INCLUDE)


def options_in(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    """The options alone of a config, without the ids its items came from."""
    if kind == "recording":
        return config
    return {"include": include_in(config)} if kind == "gdrive" else {}


def served(src: KnowledgeSource) -> bool:
    """Whether this build serves `src`: one shown to every reader of its namespace. A
    mirrored one, or any `access` a later build writes, is that build's."""
    return src.access == NAMESPACE_ACCESS


def id_list_config(src: KnowledgeSource) -> dict[str, Any] | None:
    """The config a build without items reads for `src`, or None when it cannot
    express the source: such a source is stored with `kind` empty. A build without
    items shows every document to every reader of the namespace."""
    if not served(src):
        return None
    if src.kind == "recording":
        return src.config
    return config_from_items(src.kind, src.items, include_in(src.config))


def stored_config(src: KnowledgeSource) -> dict[str, Any]:
    """What `config_json` holds: the id-list config, or the options alone."""
    config = id_list_config(src)
    return options_in(src.kind, src.config) if config is None else config


def json_object(raw: str) -> dict[str, Any]:
    """A stored JSON object (a source's config, a sync state), or {} when it is not one."""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# The parts of a stored sync state a reader sees, with their types. The rest
# (`full`, `full_listed_at`, `file_waits`, `waits_reset_for`, `retry_granted_for`, and
# a page store's `child_pages`) is the worker's own bookkeeping.
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


__all__ = [
    "GDRIVE_DEFAULT_INCLUDE",
    "GDRIVE_MIME_TYPES",
    "ITEM_FIELDS",
    "KINDS",
    "KnowledgeSource",
    "MAX_DEPTH",
    "MAX_FOLDERS",
    "MAX_ITEMS",
    "MAX_PAGE_STORE_IDS",
    "MAX_PROJECTS",
    "MAX_RECORDING_IDS",
    "MAX_WALK_FOLDERS",
    "RECORDING_ID_RE",
    "SAMPLE_ITEMS",
    "STATE_KEY_PREFIX",
    "SYNC_FIELDS",
    "SYNC_WORKER_ACTOR",
    "check_filters",
    "config_from_items",
    "fits_id_list",
    "id_list_config",
    "include_in",
    "include_mime_types",
    "item_ids",
    "item_key",
    "items_from_config",
    "json_object",
    "max_items",
    "normalize_name",
    "options_in",
    "parse_config",
    "parse_include",
    "parse_items",
    "shown",
    "source_tag",
    "source_tags",
    "state_key",
    "stored_config",
    "sync_of",
    "unique_items",
    "unknown_keys",
    "unread_file_note",
    "valid_name",
]
