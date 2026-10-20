"""OKF v0.2 bundle ⇄ Loom memory rows — the git-native projection of a namespace.

An OKF bundle (Open Knowledge Format, GoogleCloudPlatform/knowledge-catalog)
is a directory of Markdown files with YAML frontmatter: one concept per file,
`index.md` per directory listing each concept with a one-line description,
`log.md` with dated change entries. Its one hard rule is a non-empty `type`;
its optional families are `sources`, `generated {by, at}`,
`verified [{by, at}]`, `status draft|stable|deprecated` and `stale_after`.

Why Loom speaks it: a namespace rendered this way is something a person can
`git diff`, review in a pull request, and hand to any OKF-aware agent tool,
while Loom stays the store that retrieves, detects conflicts and reasons
about time. Nothing here touches ClickHouse; the CLI (`server/cli/okf.py`)
feeds these pure functions from the HTTP API and writes the files.

Mapping, memory → concept:

  memory_key                → file name and `loom.memory_key` (the key's slug, capped at
                              160 bytes; `-<hash>` when another key holds it or it is
                              a bundle file's or a Windows device's name)
  memory_type               → directory (its lowercased slug; `type-<slug>` for a
                              reserved name) and `type` (semantic→Fact,
                              procedural→Procedure, preference→Preference,
                              episodic→Event; anything else Title-cased)
  value                     → body (text as-is; JSON as a fenced block)
  tags                      → `tags`
  agent + created_at        → `generated: {by: "agent:<agent>", at}`
  verified_by/at (live)     → `verified: [{by, at}]`   (only human/process)
  status                    → active→stable, conflicted→draft, else deprecated
  expires_at                → `stale_after` (a review-by date)
  source_resource_id        → `sources: [{resource}]`
  everything Loom-specific  → `loom: {...}` (OKF consumers must preserve
                              unknown keys; the importer reads it back)

Links between concepts are first the edges someone
authored — `loom.operator_edges` rows with a canonical verb, and the
`supersedes` chain a superseded row's `superseded_by` implies — written as
authoritative frontmatter `relates_to: [{path, kind}]` and mirrored in the
body's "Related Concepts" section; then the shared-entity links (capped,
symmetric) so the bundle has a graph an OKF validator can walk. Concepts
that link to nothing remain orphans, which the CLI reports rather than
hides. The `loom:` block also carries the row's provenance tier
(`source_class`), `sensitivity`, `superseded_by` and `valid_to` so a round
trip cannot launder an inference into a first-hand fact.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import yaml  # stubs: types-PyYAML (dev extra)

from server.memory.domain.sensitivity import LEVELS, is_high

OKF_VERSION = "0.2"
RESERVED_FILES = frozenset({"index.md", "log.md", "AGENTS.md", "README.md"})
# Names a concept file or type directory never takes, compared case-insensitively as macOS
# and Windows filesystems compare them: the bundle's own files, and the device names Windows
# refuses as a file name with any extension.
_WINDOWS_DEVICES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(10)),
        *(f"lpt{i}" for i in range(10)),
    }
)
_RESERVED_NAMES = frozenset(f.casefold() for f in RESERVED_FILES)
# The longest file or directory name slug: a filesystem refuses a name over 255 bytes.
_MAX_SLUG_BYTES = 160  # not a setting: a bundle's file names must fit every filesystem
# Directory names a bundle reader skips or treats as something other than concepts: the
# evidence and digest planes (okf_evidence.EVIDENCE_DIRS), the lint's staging area, and
# tooling directories. A memory type that slugs to one of them is written under `type-<slug>`.
_RESERVED_DIRS = frozenset({"evidence", "digests", "staging", "node_modules"})

_TYPE_BY_MEMORY_TYPE: dict[str, str] = {
    "semantic": "Fact",
    "procedural": "Procedure",
    "preference": "Preference",
    "episodic": "Event",
}
_MEMORY_TYPE_BY_TYPE: dict[str, str] = {v.lower(): k for k, v in _TYPE_BY_MEMORY_TYPE.items()}
_MEMORY_TYPE_BY_TYPE.update(
    {"decision": "semantic", "process": "procedural", "runbook": "procedural"}
)

_STATUS_BY_LOOM: dict[str, str] = {
    "active": "stable",
    "conflicted": "draft",
    "duplicate": "deprecated",
    "superseded": "deprecated",
    "rejected": "deprecated",
    "tombstoned": "deprecated",
}

MAX_DESCRIPTION = 140
MAX_LINKS_PER_CONCEPT = 5
RELATED_HEADING = "# Related Concepts"
# Authored edge verbs that are not claims about the concepts: the digest
# chain (`precedes`) is a per-session sequence and the bulk of a
# namespace's edges; `recalled_for_review` and
# `bench_related` are telemetry. They stay in ClickHouse and out of the bundle.
EXCLUDED_EDGE_RELATIONS = frozenset({"precedes", "recalled_for_review", "bench_related"})
SUPERSEDES = "supersedes"
# The provenance classes a bundle may declare on import: the
# ones that say "not first-hand". A bundle claiming `asserted`, `extracted`
# or `observed` is ignored — the server decides those.
DECLARABLE_SOURCE_CLASSES = frozenset({"derived", "imported", "external"})

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_ACTOR_RE = re.compile(r"^(?:[a-zA-Z][\w.-]*:\S+|[^\s/]+/[^\s/]+)$")


@dataclass
class Concept:
    """One OKF concept: frontmatter fields plus the Markdown body."""

    id: str  # bundle-relative path without .md
    type: str
    title: str
    description: str
    body: str = ""
    tags: list[str] = field(default_factory=list)
    generated: dict[str, str] | None = None
    verified: list[dict[str, str]] = field(default_factory=list)
    status: str = ""
    stale_after: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    # Authoritative typed edges: `[{path, kind}]`, `path`
    # bundle-relative and ending in `.md`, `kind` a loom canonical verb.
    relates_to: list[dict[str, str]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def path(self) -> str:
        return f"{self.id}.md"

    @property
    def memory_key(self) -> str:
        raw = self.extra.get("loom")
        loom: dict[str, Any] = raw if isinstance(raw, dict) else {}
        return str(loom.get("memory_key") or self.id.rsplit("/", 1)[-1])

    @property
    def loom(self) -> dict[str, Any]:
        raw = self.extra.get("loom")
        return raw if isinstance(raw, dict) else {}


# ------------------------------------------------------------------
# memory → concept
# ------------------------------------------------------------------


def slugify(memory_key: str) -> str:
    """File-safe, case-preserving slug. The original key rides `loom.memory_key`."""
    slug = _SLUG_RE.sub("-", memory_key.strip()).strip("-.")
    return slug or "untitled"


def humanise_key(memory_key: str) -> str:
    words = re.sub(r"[_\-/.:]+", " ", memory_key).split()
    if not words:
        return memory_key
    text = " ".join(words)
    return text[0].upper() + text[1:]


def _one_line(text: str, limit: int) -> str:
    """First sentence-ish of `text`, single line, no frontmatter delimiter."""
    flat = " ".join(str(text).split())
    flat = flat.replace("---", "—")
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    for sep in (". ", "; ", ", ", " "):
        i = cut.rfind(sep)
        if i > limit // 2:
            cut = cut[:i]
            break
    return cut.rstrip(" .,;") + "…"


def value_to_body(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return "```json\n" + json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n```"


def body_to_value(body: str) -> Any:
    """Inverse of value_to_body: a lone fenced json block becomes JSON again."""
    text = body.strip()
    m = re.fullmatch(r"```json\n(.*)\n```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return text
    return text


def _iso(ts: Any) -> str:
    if ts is None or ts == "":
        return ""
    if isinstance(ts, datetime):
        dt: datetime = ts.astimezone(UTC).replace(tzinfo=None) if ts.tzinfo is not None else ts
        return dt.replace(microsecond=0).isoformat() + "Z"
    if isinstance(ts, date):
        return str(ts.isoformat())
    s: str = str(ts)
    return s if s.endswith("Z") or "+" in s[10:] else s + "Z"


def _date(ts: Any) -> str:
    s = _iso(ts)
    return s[:10] if s else ""


def okf_type_for(memory_type: str) -> str:
    mt = (memory_type or "").strip() or "semantic"
    return _TYPE_BY_MEMORY_TYPE.get(mt, mt[:1].upper() + mt[1:])


def memory_type_for(okf_type: str, directory: str = "") -> str:
    t = (okf_type or "").strip().lower()
    if t in _MEMORY_TYPE_BY_TYPE:
        return _MEMORY_TYPE_BY_TYPE[t]
    d = (directory or "").strip().lower()
    if d in _TYPE_BY_MEMORY_TYPE:
        return d
    return t or "semantic"


def _session_of(entry: dict[str, Any]) -> str:
    """The row's session id, or '' for loom's "no session" ('-')."""
    sid = str(entry.get("session_id") or "").strip()
    return "" if sid in ("-", "*") else sid


def _owner_of(entry: dict[str, Any]) -> str:
    """The row's owner for the bundle; loom's '-' sentinel is "no user"."""
    uid = entry.get("user_id")
    return "" if uid in (None, "", "-") else str(uid)


def memory_to_concept(
    entry: dict[str, Any], *, directory: str | None = None, slug: str | None = None
) -> Concept:
    """Build a Concept from a memory.get-shaped dict (+ memory_key, agent, namespace).

    `entry` keys read: memory_key, memory_type, value, tags, agent, created_at,
    updated_at, status, expires_at, valid_at, confidence, content_sha256,
    memory_id, namespace, entities, source_resource_id, source_trace,
    verified_by, verified_at, verification. Missing keys fall back.
    """
    memory_key = str(entry["memory_key"])
    memory_type = str(entry.get("memory_type") or "").strip() or "semantic"
    okf_type = okf_type_for(memory_type)
    directory = directory if directory is not None else type_directory(memory_type)
    slug = slug or file_slug(memory_key)
    value = entry.get("value")
    body_text = value_to_body(value if value is not None else "")
    title = humanise_key(memory_key)

    generated = None
    agent = str(entry.get("agent") or "").strip()
    if agent and agent not in ("-", "*"):
        generated = {"by": f"agent:{agent}", "at": _iso(entry.get("created_at"))}
    elif entry.get("created_at"):
        generated = {"by": "process:loom", "at": _iso(entry.get("created_at"))}

    verified: list[dict[str, str]] = []
    if entry.get("verification") in ("human", "process") and entry.get("verified_by"):
        verified = [{"by": str(entry["verified_by"]), "at": _iso(entry.get("verified_at"))}]

    status = _STATUS_BY_LOOM.get(str(entry.get("status") or "active"), "stable")
    if entry.get("tombstoned_at"):
        status = "deprecated"

    sources: list[dict[str, Any]] = []
    if entry.get("source_resource_id"):
        sources.append({"resource": str(entry["source_resource_id"]), "id": "src"})

    loom_extra: dict[str, Any] = {
        # Origin: the org, namespace and owner the row was
        # exported from, so a bundle imported elsewhere can say where each
        # concept came from. `user_id` is the owner; '-' is loom's "no user".
        "org": entry.get("org") or "",
        "namespace": entry.get("namespace") or "",
        "user_id": _owner_of(entry),
        "memory_key": memory_key,
        "memory_type": memory_type,
        "memory_id": str(entry.get("memory_id") or ""),
        "agent": agent,
        # The writer's session: the same key can live
        # under several sessions; an import that keeps the session keeps them
        # apart, and hit.session_id / cited_sessions keep meaning.
        "session_id": _session_of(entry),
        "confidence": float(conf) if (conf := entry.get("confidence")) is not None else 1.0,
        "content_sha256": entry.get("content_sha256") or "",
        "status": entry.get("status") or "active",
        "verification": entry.get("verification") or "none",
        "entities": [str(e) for e in (entry.get("entities") or [])],
    }
    if entry.get("valid_at"):
        loom_extra["valid_at"] = _iso(entry["valid_at"])
    if entry.get("updated_at"):
        loom_extra["updated_at"] = _iso(entry["updated_at"])
    if entry.get("source_trace"):
        loom_extra["source_trace"] = str(entry["source_trace"])
    # Provenance tier and lifecycle: how the content entered
    # memory, who may see it, which row replaced it, when it stopped holding.
    loom_extra["source_class"] = str(entry.get("source_class") or "")
    loom_extra["sensitivity"] = str(entry.get("sensitivity") or "")
    loom_extra["superseded_by"] = str(entry.get("superseded_by") or "")
    if entry.get("valid_to"):
        loom_extra["valid_to"] = _iso(entry["valid_to"])
    # Empty identity fields say nothing and churn diffs; drop them.
    loom_extra = {k: v for k, v in loom_extra.items() if v not in ("", None, [])}

    return Concept(
        id=f"{directory}/{slug}",
        type=okf_type,
        title=title,
        description=_one_line(
            body_text if isinstance(value, str) else f"{okf_type} {title}", MAX_DESCRIPTION
        )
        or f"{okf_type} {title}",
        body=body_text,
        tags=[str(t) for t in (entry.get("tags") or [])],
        generated=generated,
        verified=verified,
        status=status,
        stale_after=_date(entry.get("expires_at")),
        sources=sources,
        extra={"loom": loom_extra},
    )


# ------------------------------------------------------------------
# serialisation
# ------------------------------------------------------------------


def frontmatter_dict(c: Concept) -> dict[str, Any]:
    fm: dict[str, Any] = {"type": c.type, "title": c.title, "description": c.description}
    if c.tags:
        fm["tags"] = list(c.tags)
    if c.generated:
        fm["generated"] = dict(c.generated)
    if c.verified:
        fm["verified"] = [dict(v) for v in c.verified]
    if c.status:
        fm["status"] = c.status
    if c.stale_after:
        fm["stale_after"] = c.stale_after
    if c.sources:
        fm["sources"] = [dict(s) for s in c.sources]
    if c.relates_to:
        fm["relates_to"] = [dict(e) for e in c.relates_to]
    for k, v in c.extra.items():
        fm.setdefault(k, v)
    return fm


def serialize_concept(c: Concept) -> str:
    fm = yaml.safe_dump(
        frontmatter_dict(c),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=1000,
    ).rstrip("\n")
    body = c.body.rstrip("\n")
    heading = f"# {c.title}"
    if not body.startswith(heading):
        body = f"{heading}\n\n{body}" if body else heading
    return f"---\n{fm}\n---\n\n{body}\n"


def split_frontmatter(text: str) -> tuple[str, str, bool]:
    text = text.lstrip("﻿")
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        return "", text, False
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r") == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :]), True
    return "", text, False


def _scalar(v: Any) -> str:
    """YAML reads unquoted timestamps as datetime; keep the OKF string form."""
    if isinstance(v, datetime | date):
        return _iso(v)
    return str(v)


def parse_concept(rel_path: str, text: str) -> Concept:
    """Parse one concept file. Unknown frontmatter keys are kept in `extra`."""
    fm_text, body, has_fm = split_frontmatter(text)
    fm: dict[str, Any] = {}
    if has_fm:
        loaded = yaml.safe_load(fm_text) or {}
        if isinstance(loaded, dict):
            fm = loaded
    rel = rel_path.replace("\\", "/")
    cid = rel[:-3] if rel.endswith(".md") else rel
    known = {
        "type",
        "title",
        "description",
        "tags",
        "generated",
        "verified",
        "status",
        "stale_after",
        "sources",
        "relates_to",
    }
    verified_raw = fm.get("verified") or []
    if isinstance(verified_raw, dict):
        verified_raw = [verified_raw]
    # okf-lint reports a `tags` that is not a list; --force-import still reads the file.
    tags = fm.get("tags")
    return Concept(
        id=cid,
        type=str(fm.get("type") or "").strip(),
        title=str(fm.get("title") or humanise_key(cid.rsplit("/", 1)[-1])),
        description=str(fm.get("description") or ""),
        body=body.strip("\n"),
        tags=[str(t) for t in tags] if isinstance(tags, list) else [],
        generated=(
            {str(k): _scalar(v) for k, v in fm["generated"].items()}
            if isinstance(fm.get("generated"), dict)
            else None
        ),
        verified=[
            {str(k): _scalar(v) for k, v in v.items()} for v in verified_raw if isinstance(v, dict)
        ],
        status=str(fm.get("status") or ""),
        stale_after=str(fm.get("stale_after") or ""),
        sources=[s for s in (fm.get("sources") or []) if isinstance(s, dict)],
        relates_to=_parse_relates_to(fm.get("relates_to")),
        extra={k: v for k, v in fm.items() if k not in known},
    )


def _parse_relates_to(raw: Any) -> list[dict[str, str]]:
    """`relates_to` entries with a non-empty `path`; `kind` defaults to
    `related_to`, and an atlas-style `role` key is read as `kind`."""
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip().replace("\\", "/")
        if not path:
            continue
        kind = str(item.get("kind") or item.get("role") or "related_to").strip() or "related_to"
        edge = {"path": path, "kind": kind}
        for k, v in item.items():
            if k not in ("path", "kind", "role") and isinstance(v, str | int | float):
                edge[str(k)] = str(v)
        out.append(edge)
    return out


# ------------------------------------------------------------------
# concept → memory.set payload
# ------------------------------------------------------------------


def strip_generated_sections(body: str, title: str) -> str:
    """Remove the `# <title>` heading and the `# Related Concepts` section
    the exporter added, so a round trip returns the original value."""
    lines = body.split("\n")
    if lines and lines[0].strip() == f"# {title}":
        lines = lines[1:]
    out: list[str] = []
    skipping = False
    for ln in lines:
        s = ln.strip()
        if s in (RELATED_HEADING, "# Related"):
            skipping = True
            continue
        if skipping and s.startswith("# "):
            skipping = False
        if not skipping:
            out.append(ln)
    return "\n".join(out).strip("\n")


ORIGIN_TAG_PREFIX = "origin:"


def origin_tags(
    loom: dict[str, Any], *, target_org: str | None, target_namespace: str | None
) -> list[str]:
    """A copy remembers where it came from. When a concept's
    `loom.{org,namespace}` name a place other than the one it is imported
    into, the memory carries `origin:org:<org>`, `origin:namespace:<ns>`,
    `origin:user:<user_id>` and `origin:memory_key:<key>` tags, so an
    erasure of the origin can find the copy (`hasAny(tags, [...])`) and a
    reader can see it is not first-hand. A concept re-imported into its own
    org and namespace carries none: it is the same memory coming home."""
    o_org = str(loom.get("org") or "")
    o_ns = str(loom.get("namespace") or "")
    if not o_ns and not o_org:
        return []
    same_org = not o_org or target_org is None or o_org == target_org
    same_ns = not o_ns or target_namespace is None or o_ns == target_namespace
    if same_org and same_ns:
        return []
    tags: list[str] = []
    if o_org:
        tags.append(f"{ORIGIN_TAG_PREFIX}org:{o_org}")
    if o_ns:
        tags.append(f"{ORIGIN_TAG_PREFIX}namespace:{o_ns}")
    if loom.get("user_id"):
        tags.append(f"{ORIGIN_TAG_PREFIX}user:{loom['user_id']}")
    if loom.get("memory_key"):
        tags.append(f"{ORIGIN_TAG_PREFIX}memory_key:{loom['memory_key']}")
    return tags


def concept_to_memory(
    c: Concept, *, target_org: str | None = None, target_namespace: str | None = None
) -> dict[str, Any]:
    """Fields for memory.set (identity added by the caller). With the target
    org / namespace given, a concept exported from elsewhere carries its
    origin as tags."""
    raw_loom = c.extra.get("loom")
    loom: dict[str, Any] = dict(raw_loom) if isinstance(raw_loom, dict) else {}
    directory = c.id.rsplit("/", 1)[0] if "/" in c.id else ""
    memory_key = str(loom.get("memory_key") or c.id.rsplit("/", 1)[-1])
    memory_type = str(loom.get("memory_type") or memory_type_for(c.type, directory))
    value = body_to_value(strip_generated_sections(c.body, c.title))
    tags = list(dict.fromkeys([*c.tags, f"okf:type:{c.type}"] if c.type else c.tags))
    origin = origin_tags(loom, target_org=target_org, target_namespace=target_namespace)
    for t in origin:
        if t not in tags:
            tags.append(t)
    if c.status and c.status != "stable":
        tags.append(f"okf:status:{c.status}")
    if c.stale_after:
        tags.append(f"okf:stale_after:{c.stale_after}")
    payload: dict[str, Any] = {
        "memory_key": memory_key,
        "memory_type": memory_type,
        "value": value,
        "tags": tags,
        "confidence": float(loom.get("confidence", 1.0) or 1.0),
    }
    if origin:
        # The provenance class the ranking term already knows: trusted, and known not to be
        # first-hand.
        payload["source_class"] = "imported"
    declared = declared_source_class(loom)
    if declared:
        # A `derived` (loom's own inference), `imported` or
        # `external` row keeps saying so after the move; `memory.ask` marks
        # claims resting only on such rows `[inferred]`, and
        # relabelling them `imported` on the way in would un-mark them.
        payload["source_class"] = declared
    if loom.get("valid_to"):
        payload["valid_to"] = str(loom["valid_to"]).rstrip("Z")
    if c.sources and c.sources[0].get("resource"):
        payload["source_resource_id"] = str(c.sources[0]["resource"])
    if loom.get("entities"):
        payload["entities"] = [str(e) for e in loom["entities"]]
    if loom.get("valid_at"):
        payload["valid_at"] = str(loom["valid_at"]).rstrip("Z")
    return payload


def concept_session_id(c: Concept) -> str:
    """The session the concept's row was written under, from `loom.session_id`;
    '' when the bundle was written without `loom.session_id` or the row had
    none."""
    raw_loom = c.extra.get("loom")
    loom: dict[str, Any] = raw_loom if isinstance(raw_loom, dict) else {}
    sid = str(loom.get("session_id") or "").strip()
    return "" if sid in ("-", "*") else sid


def human_verified_actor(c: Concept) -> str:
    """The `human:` actor on a concept's live-looking verification, or ''."""
    for v in c.verified:
        by = str(v.get("by") or "")
        if by.startswith("human:") and _ACTOR_RE.match(by):
            return by
    return ""


def declared_source_class(loom: dict[str, Any]) -> str:
    """The `loom.source_class` a bundle may declare on import:
    one of `DECLARABLE_SOURCE_CLASSES`, else '' (the server decides)."""
    cls = str(loom.get("source_class") or "").strip().lower()
    return cls if cls in DECLARABLE_SOURCE_CLASSES else ""


def is_exportable(entry: dict[str, Any], *, include_high: bool = False) -> bool:
    """A `sensitivity = high` row has no embedding row
    and does not leave the database as clear text unless the
    exporter asks; a mirror repository has a wider audience than the
    namespace's grants. A row whose sensitivity the read did not answer, or
    answered with a level this build does not know, stays home too."""
    if include_high:
        return True
    level = entry.get("sensitivity")
    if level is None or str(level).strip().lower() not in LEVELS:
        return False
    return not is_high(level, entry.get("tags"))


# ------------------------------------------------------------------
# typed edges
# ------------------------------------------------------------------


def authored_relates_to(
    concepts: list[Concept],
    edges: list[dict[str, Any]] | None,
    *,
    excluded: frozenset[str] = EXCLUDED_EDGE_RELATIONS,
) -> dict[str, list[dict[str, str]]]:
    """concept id → `relates_to` entries from (a) authored `operator_edges`
    rows (`from_key`, `to_key`, `relation`) whose verb is not excluded and whose
    both ends are in the bundle, and (b) the `supersedes` a superseded row's
    `superseded_by` (a memory_id) implies, written on the NEWER row. A key that
    lives under several sessions is several concepts; an edge on the key
    lands on each of them. Deterministic order; no duplicates."""
    by_key: dict[str, list[Concept]] = {}
    by_memory_id: dict[str, Concept] = {}
    for c in concepts:
        by_key.setdefault(c.memory_key, []).append(c)
        mid = str(c.loom.get("memory_id") or "")
        if mid:
            by_memory_id[mid] = c
    out: dict[str, dict[tuple[str, str], dict[str, str]]] = {c.id: {} for c in concepts}

    def _add(src: Concept, tgt: Concept, kind: str, **more: str) -> None:
        if src.id == tgt.id:
            return
        key = (tgt.path, kind)
        if key not in out[src.id]:
            out[src.id][key] = {"path": tgt.path, "kind": kind, **more}

    for e in edges or []:
        kind = str(e.get("relation") or "").strip()
        if not kind or kind in excluded:
            continue
        for src in by_key.get(str(e.get("from_key") or ""), []):
            for tgt in by_key.get(str(e.get("to_key") or ""), []):
                more: dict[str, str] = {}
                conf = e.get("confidence")
                if conf is not None and float(conf) < 1.0:
                    more["confidence"] = f"{float(conf):.2f}"
                _add(src, tgt, kind, **more)
    for old in concepts:
        newer = by_memory_id.get(str(old.loom.get("superseded_by") or ""))
        if newer is not None and str(old.loom.get("status") or "") == "superseded":
            _add(newer, old, SUPERSEDES)
    return {cid: [v for _, v in sorted(m.items())] for cid, m in out.items()}


def resolve_relates_to(
    concepts: list[Concept],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """A parsed bundle's edges as memory.link payloads (`from_key`, `to_key`,
    `relation`, `path`) and the entries that name no concept in the bundle
    (a `loom://` pointer, a file that was not exported) — reported, not written."""
    by_path = {c.path: c for c in concepts}
    links: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []
    for c in concepts:
        for e in c.relates_to:
            tgt = by_path.get(e["path"])
            if tgt is None:
                unresolved.append({"path": c.path, "target": e["path"], "kind": e["kind"]})
                continue
            links.append(
                {
                    "from_key": c.memory_key,
                    "to_key": tgt.memory_key,
                    "relation": e["kind"],
                    "path": c.path,
                }
            )
    return links, unresolved


def concept_paths_by_key(concepts: list[Concept]) -> dict[str, str]:
    """memory_key → bundle path of its first concept (the digest lineage's target)."""
    out: dict[str, str] = {}
    for c in concepts:
        out.setdefault(c.memory_key, c.path)
    return out


# ------------------------------------------------------------------
# bundle planning
# ------------------------------------------------------------------


def type_directory(memory_type: str) -> str:
    """The bundle directory of a memory type: its slug, prefixed when a reader would skip it
    or read it as something other than concepts (a free-form type like `evidence`,
    `.notes` or `../x` would otherwise drop its rows from the bundle or escape it)."""
    # Lowercase: `Semantic` and `semantic` would share one directory on a case-insensitive
    # filesystem, and their index files would overwrite each other.
    slug = file_slug(memory_type).lower()
    if slug in _RESERVED_DIRS or slug in _RESERVED_NAMES or _is_device(slug):
        return f"type-{slug}"
    return slug


def file_slug(text: str) -> str:
    """A key's (or type's) file name: its slug, cut to `_MAX_SLUG_BYTES` with a hash of the
    whole text when longer (so two long keys that share a prefix still differ)."""
    slug = slugify(text)
    if len(slug.encode()) <= _MAX_SLUG_BYTES:
        return slug
    cut = slug.encode()[:_MAX_SLUG_BYTES].decode(errors="ignore").rstrip("-.")
    return f"{cut}-{_short_hash(text)}"


def _short_hash(text: str, attempt: int = 0) -> str:
    salted = f"{text}\0{attempt}" if attempt else text
    return hashlib.sha256(salted.encode()).hexdigest()[:8]


def _is_device(name: str) -> bool:
    return name.split(".", 1)[0].casefold() in _WINDOWS_DEVICES


def _suffixed(concept_id: str, suffix: str) -> str:
    """`concept_id` with `-<suffix>` before the first dot of its name: Windows reads a device
    name (`lpt9.txt`) by the part before the dot, so a suffix after it would not help."""
    directory, name = concept_id.rsplit("/", 1)  # every concept id is `<type dir>/<slug>`
    stem, dot, rest = name.partition(".")
    return f"{directory}/{stem}-{suffix}{dot}{rest}"


def _is_reserved(concept_id: str) -> bool:
    name = concept_id.rsplit("/", 1)[-1]
    return f"{name}.md".casefold() in _RESERVED_NAMES or _is_device(name)


def _dedupe_slugs(concepts: list[Concept]) -> None:
    """Give each concept an id no other concept has, compared case-insensitively. A taken or
    reserved name gets `-<hash of its key and row id>`, which no other key changes."""
    used: set[str] = set()
    for c in concepts:
        if c.id.casefold() in used or _is_reserved(c.id):
            identity = f"{c.memory_key}\0{c.loom.get('memory_id') or ''}"
            # a counter makes each attempt a new name, so the loop ends
            attempt = 0
            while (renamed := _suffixed(c.id, _short_hash(identity, attempt))).casefold() in used:
                attempt += 1
            c.id = renamed
        used.add(c.id.casefold())


def related_links(
    concepts: list[Concept], *, cap: int = MAX_LINKS_PER_CONCEPT
) -> dict[str, list[tuple[str, str]]]:
    """Symmetric links between concepts sharing an entity: id → [(target_id, reason)]."""
    by_entity: dict[str, list[int]] = {}
    for i, c in enumerate(concepts):
        for e in (c.extra.get("loom") or {}).get("entities", []) or []:
            by_entity.setdefault(str(e).lower(), []).append(i)
    links: dict[str, dict[str, str]] = {c.id: {} for c in concepts}
    for ent, idxs in sorted(by_entity.items()):
        if len(idxs) < 2 or len(idxs) > 50:  # a hub entity links nothing useful
            continue
        for a in idxs:
            for b in idxs:
                if a == b:
                    continue
                ca, cb = concepts[a], concepts[b]
                if cb.id not in links[ca.id] and len(links[ca.id]) < cap:
                    links[ca.id][cb.id] = f"shares entity {ent}"
    # symmetry: if a links b, b links a (so nobody is an orphan by direction)
    for src, tgts in list(links.items()):
        for tgt, why in list(tgts.items()):
            links[tgt].setdefault(src, why)
    return {k: sorted(v.items()) for k, v in links.items()}


def relative_link(from_id: str, to_id: str) -> str:
    from_dir = from_id.rsplit("/", 1)[0] if "/" in from_id else ""
    to_dir, to_name = to_id.rsplit("/", 1) if "/" in to_id else ("", to_id)
    if from_dir == to_dir:
        return f"{to_name}.md"
    ups = "../" * (from_dir.count("/") + 1) if from_dir else ""
    return f"{ups}{to_dir}/{to_name}.md" if to_dir else f"{ups}{to_name}.md"


def plan_concepts(
    entries: list[dict[str, Any]], edges: list[dict[str, Any]] | None = None
) -> list[Concept]:
    """Entries → sorted, slug-deduped concepts carrying their typed edges."""
    concepts = [memory_to_concept(e) for e in entries]
    # Ties on a slug break on the key and the row id, never the listing's order, so the same
    # rows keep the same files on every run. A file moves only when the key holding its bare
    # name changes (a new key sorts first, or the holder leaves and the next in order takes
    # it), and the copy of the key that moved is rewritten once.
    concepts.sort(key=lambda c: (c.id, c.memory_key, str(c.loom.get("memory_id") or "")))
    _dedupe_slugs(concepts)
    typed = authored_relates_to(concepts, edges)
    for c in concepts:
        c.relates_to = typed.get(c.id, [])
    return concepts


def _related_section(
    c: Concept, by_id: dict[str, Concept], entity_links: list[tuple[str, str]]
) -> str:
    """The body mirror of the graph: authored edges with their kind first,
    then the shared-entity links; frontmatter `relates_to` stays authoritative."""
    by_path = {x.path: x for x in by_id.values()}
    lines = [RELATED_HEADING]
    for e in c.relates_to:
        tgt = by_path.get(e["path"])
        if tgt is not None:
            lines.append(f"- [{tgt.title}]({relative_link(c.id, tgt.id)}): {e['kind']}")
    for tgt_id, why in entity_links:
        lines.append(f"- [{by_id[tgt_id].title}]({relative_link(c.id, tgt_id)}): {why}")
    return "\n".join(lines)


def is_concept_path(rel: str) -> bool:
    """A concept file: an `.md` that is not a reserved file and not under
    the evidence directories."""
    from server.memory.domain.okf_evidence import is_evidence_path  # imports this module

    return (
        rel.endswith(".md")
        and rel.rsplit("/", 1)[-1] not in RESERVED_FILES
        and not is_evidence_path(rel)
    )


def corpus_digest(files: dict[str, str]) -> str:
    """The generation digest the root index carries. One definition, owned by
    the lint gate (`okf_lint.bundle_digest`): concept files sorted by
    bundle-relative path, each file's sha256 hex appended without a separator,
    sha256 of the result. Indexes, the log and the evidence planes are outside
    it."""
    from server.memory.domain.okf_lint import bundle_digest

    return bundle_digest(files)


def plan_bundle(
    entries: list[dict[str, Any]],
    *,
    namespace: str,
    exported_at: datetime,
    title: str | None = None,
    edges: list[dict[str, Any]] | None = None,
    build_commit: str = "",
    complete: bool = False,
) -> dict[str, str]:
    """Return {relative_path: file_text} for a complete OKF v0.2 bundle.
    `edges` are the namespace's live `operator_edges` rows.
    The root `index.md` frontmatter carries the generation pointer:
    `corpus_digest`, `concepts`, `exported_at` (a date), when known the
    `build_commit` that rendered it, and `complete: true` when `entries` is
    every row of the namespace (prune trusts nothing else)."""
    concepts = plan_concepts(entries, edges)
    by_id = {c.id: c for c in concepts}
    links = related_links(concepts)
    n_edges = sum(len(c.relates_to) for c in concepts)

    files: dict[str, str] = {}
    for c in concepts:
        body = c.body
        if links.get(c.id) or c.relates_to:
            body = body.rstrip("\n") + "\n\n" + _related_section(c, by_id, links.get(c.id, []))
        files[c.path] = serialize_concept(Concept(**{**c.__dict__, "body": body}))

    # per-directory indexes (no frontmatter) and the root index (okf_version)
    dirs: dict[str, list[Concept]] = {}
    for c in concepts:
        dirs.setdefault(c.id.rsplit("/", 1)[0], []).append(c)
    pointer: dict[str, Any] = {
        "corpus_digest": corpus_digest(files),
        "concepts": len(concepts),
        "exported_at": exported_at.date().isoformat(),
    }
    if build_commit:
        pointer["build_commit"] = build_commit
    if complete:
        pointer["complete"] = True
    root_fm = (
        yaml.safe_dump(
            {"okf_version": OKF_VERSION, "loom": pointer},
            sort_keys=False,
            default_flow_style=False,
        )
        .rstrip("\n")
        .replace(f"okf_version: '{OKF_VERSION}'", f'okf_version: "{OKF_VERSION}"')
    )
    root_lines = [
        "---",
        root_fm,
        "---",
        "",
        f"# {title or namespace}",
        "",
        f"Loom namespace `{namespace}` rendered as an OKF v{OKF_VERSION} bundle "
        f"({len(concepts)} concepts, exported {exported_at.date().isoformat()}). "
        "Concepts are Loom memories; `loom:` frontmatter carries the row identity "
        "the importer reads back.",
        "",
    ]
    for d in sorted(dirs):
        heading = okf_type_for(d) + "s" if d in _TYPE_BY_MEMORY_TYPE else d
        root_lines += [f"# {heading}"]
        sub_lines = [f"# {heading}", ""]
        for c in sorted(dirs[d], key=lambda x: x.id):
            root_lines.append(f"* [{c.title}]({c.path}) - {c.description}")
            sub_lines.append(f"* [{c.title}]({c.path.rsplit('/', 1)[-1]}) - {c.description}")
        root_lines.append("")
        files[f"{d}/index.md"] = "\n".join(sub_lines) + "\n"
    files["index.md"] = "\n".join(root_lines).rstrip("\n") + "\n"

    orphans = (
        [c.id for c in concepts if not links.get(c.id) and not c.relates_to]
        if len(concepts) > 1
        else []
    )
    files["log.md"] = (
        f"## {exported_at.date().isoformat()}\n"
        f"* **Export**: Rendered {len(concepts)} concepts from Loom namespace `{namespace}` "
        f"({n_edges} typed edges in `relates_to`, "
        f"{len(concepts) - len(orphans)} linked by an edge or a shared entity, "
        f"{len(orphans)} without links).\n"
    )
    return files


def bundle_orphans(files: dict[str, str]) -> list[str]:
    """Concept ids in a planned bundle that link to nothing and are linked from nothing."""
    linked: set[str] = set()
    concept_ids = [
        p[:-3] for p in files if p.endswith(".md") and p.rsplit("/", 1)[-1] not in RESERVED_FILES
    ]
    for cid in concept_ids:
        text = files[f"{cid}.md"]
        body = split_frontmatter(text)[1]
        for m in re.finditer(r"\]\(([^)\s]+\.md)\)", body):
            href = m.group(1)
            base = cid.rsplit("/", 1)[0] if "/" in cid else ""
            parts = (base.split("/") if base else []) + href[:-3].split("/")
            stack: list[str] = []
            for p in parts:
                if p == "..":
                    if stack:
                        stack.pop()
                elif p and p != ".":
                    stack.append(p)
            tgt = "/".join(stack)
            if f"{tgt}.md" in files:
                linked.add(cid)
                linked.add(tgt)
    return sorted(c for c in concept_ids if c not in linked) if len(concept_ids) > 1 else []
