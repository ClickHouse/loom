"""Raw chunks and session digests as OKF bundle files.

On a conversation namespace 72% of the top-10 hits a question retrieves are
raw conversation chunks (`loom.memory_chunks`) and session digests
(`loom.session_summaries`), so a namespace exported as `loom.memory` alone and
re-imported answers from a third of its evidence (bench/okf_roundtrip.py:
Jaccard@10 0.106 against the source). `loom export-okf` writes these two
planes as files too:

    evidence/<session-slug>/<chunk_index>.md   type: Evidence, body = verbatim chunk
    digests/<session-slug>.md                  type: Digest,   body = digest text

The `loom:` frontmatter block carries the columns the importer needs
(session id, chunk index, hash, the memory keys the chunk supports, dates).
Embeddings do not travel: the importing server embeds with its own model so
the chunk leg (which filters on `embedding_model`) can see the rows. Bodies
round-trip byte-exact; `content_sha256` is recomputed on import and compared
with the exported hash so a hand-edited chunk is reported, not silently
re-hashed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from server.core.hashing import sha256_text
from server.memory.domain.okf_bundle import slugify, split_frontmatter
from server.memory.domain.sensitivity import HIGH, detect_sensitivity

EVIDENCE_DIR = "evidence"
DIGEST_DIR = "digests"
EVIDENCE_DIRS = frozenset({EVIDENCE_DIR, DIGEST_DIR})
EVIDENCE_TYPE = "Evidence"
DIGEST_TYPE = "Digest"


@dataclass
class ChunkRecord:
    session_id: str
    chunk_index: int
    content: str
    content_sha256: str = ""
    agent: str = ""
    parent_memory_key: str = ""
    memory_keys: list[str] = field(default_factory=list)
    line_start: int = 0
    line_end: int = 0
    created_at: str = ""
    source_url: str = ""

    @property
    def path(self) -> str:
        return f"{EVIDENCE_DIR}/{slugify(self.session_id)}/{self.chunk_index:05d}.md"


@dataclass
class DigestRecord:
    session_id: str
    summary_text: str
    agent: str = "-"
    user_id: str = "-"
    key_facts: list[str] = field(default_factory=list)
    derived_from: list[str] = field(default_factory=list)
    valid_at: str = ""
    created_at: str = ""

    @property
    def path(self) -> str:
        return f"{DIGEST_DIR}/{slugify(self.session_id)}.md"


def evidence_is_high(keys: list[str], text: str, held_keys: set[str]) -> bool:
    """A chunk or digest stays home when it is linked to a key held home, or when its text
    reads as a credential: raw text has no sensitivity column, so the detector memory.set
    runs decides."""
    return bool(set(keys) & held_keys) or detect_sensitivity(text) == HIGH


def chunk_is_high(c: ChunkRecord, held_keys: set[str]) -> bool:
    return evidence_is_high([c.parent_memory_key, *c.memory_keys], c.content, held_keys)


def digest_is_high(d: DigestRecord, held_keys: set[str]) -> bool:
    text = "\n".join([d.summary_text, *d.key_facts])
    return evidence_is_high(d.derived_from, text, held_keys)


def without_high(
    chunks: list[ChunkRecord], digests: list[DigestRecord], held_keys: set[str]
) -> tuple[list[ChunkRecord], list[DigestRecord], int]:
    """The evidence that may leave with the bundle, and how much stays home."""
    keep_c = [c for c in chunks if not chunk_is_high(c, held_keys)]
    keep_d = [d for d in digests if not digest_is_high(d, held_keys)]
    return keep_c, keep_d, len(chunks) + len(digests) - len(keep_c) - len(keep_d)


def _one_line(text: str, limit: int = 140) -> str:
    s = " ".join(text.split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _render(fm: dict[str, Any], body: str) -> str:
    head = yaml.safe_dump(
        fm, sort_keys=False, allow_unicode=True, default_flow_style=False, width=1000
    ).rstrip("\n")
    # Exactly one trailing newline; `parse_files` removes exactly one, so the
    # body round-trips byte for byte (a chunk ending in "\n" keeps it).
    return f"---\n{head}\n---\n{body}\n"


def chunk_to_file(c: ChunkRecord) -> str:
    loom: dict[str, Any] = {
        "kind": "chunk",
        "session_id": c.session_id,
        "chunk_index": int(c.chunk_index),
        "content_sha256": c.content_sha256 or sha256_text(c.content),
        "agent": c.agent or "",
        "parent_memory_key": c.parent_memory_key or "",
        "memory_keys": sorted({k for k in c.memory_keys if k}),
        "line_start": int(c.line_start or 0),
        "line_end": int(c.line_end or 0),
        "created_at": c.created_at or "",
        "source_url": c.source_url or "",
    }
    fm = {
        "type": EVIDENCE_TYPE,
        "title": f"Session {c.session_id} · chunk {c.chunk_index}",
        "description": _one_line(c.content),
        "loom": loom,
    }
    return _render(fm, c.content)


def digest_to_file(d: DigestRecord, concept_paths: dict[str, str] | None = None) -> str:
    """A digest file. With `concept_paths` (memory_key → bundle path) the digest
    carries `relates_to: [{path, kind: derived_from}]` for every key in its
    `derived_from` that is in the bundle: the one evidence
    file that is a claim about concepts — it compressed them — and so the one
    that links. Raw chunks still link to nothing."""
    loom: dict[str, Any] = {
        "kind": "digest",
        "session_id": d.session_id,
        "agent": d.agent or "-",
        "user_id": d.user_id or "-",
        "key_facts": list(d.key_facts or []),
        "derived_from": sorted({k for k in d.derived_from if k}),
        "valid_at": d.valid_at or "",
        "created_at": d.created_at or "",
    }
    fm: dict[str, Any] = {
        "type": DIGEST_TYPE,
        "title": f"Session {d.session_id} · digest",
        "description": _one_line(d.summary_text),
    }
    edges = [
        {"path": concept_paths[k], "kind": "derived_from"}
        for k in loom["derived_from"]
        if concept_paths and k in concept_paths
    ]
    if edges:
        fm["relates_to"] = edges
    fm["loom"] = loom
    return _render(fm, d.summary_text)


def plan_files(
    chunks: list[ChunkRecord],
    digests: list[DigestRecord],
    concept_paths: dict[str, str] | None = None,
) -> dict[str, str]:
    """{relative_path: text} for the evidence and digest planes."""
    files: dict[str, str] = {}
    for c in chunks:
        files[c.path] = chunk_to_file(c)
    for d in digests:
        files[d.path] = digest_to_file(d, concept_paths)
    return files


def is_evidence_path(rel_path: str) -> bool:
    top = rel_path.replace("\\", "/").split("/", 1)[0]
    return top in EVIDENCE_DIRS


def _body_of(text: str) -> tuple[dict[str, Any], str]:
    fm_text, body, has_fm = split_frontmatter(text)
    fm = yaml.safe_load(fm_text) if has_fm else None
    if body.endswith("\n"):
        body = body[:-1]
    return (fm if isinstance(fm, dict) else {}), body


def parse_file(rel_path: str, text: str) -> ChunkRecord | DigestRecord | None:
    """One evidence or digest file back to its record; None for anything else
    (an index, a stray note). A chunk whose body no longer hashes to the
    exported `content_sha256` keeps the exported hash in `content_sha256` so
    the importer can report the edit; the body is what is imported."""
    fm, body = _body_of(text)
    raw_loom = fm.get("loom")
    loom: dict[str, Any] = raw_loom if isinstance(raw_loom, dict) else {}
    kind = str(loom.get("kind") or "").lower() or (
        "chunk"
        if str(fm.get("type") or "") == EVIDENCE_TYPE
        else "digest"
        if str(fm.get("type") or "") == DIGEST_TYPE
        else ""
    )
    if kind == "chunk":
        return ChunkRecord(
            session_id=str(loom.get("session_id") or ""),
            chunk_index=int(loom.get("chunk_index") or 0),
            content=body,
            content_sha256=str(loom.get("content_sha256") or ""),
            agent=str(loom.get("agent") or ""),
            parent_memory_key=str(loom.get("parent_memory_key") or ""),
            memory_keys=[str(k) for k in (loom.get("memory_keys") or []) if str(k)],
            line_start=int(loom.get("line_start") or 0),
            line_end=int(loom.get("line_end") or 0),
            created_at=str(loom.get("created_at") or ""),
            source_url=str(loom.get("source_url") or ""),
        )
    if kind == "digest":
        return DigestRecord(
            session_id=str(loom.get("session_id") or ""),
            summary_text=body,
            agent=str(loom.get("agent") or "-"),
            user_id=str(loom.get("user_id") or "-"),
            key_facts=[str(k) for k in (loom.get("key_facts") or [])],
            derived_from=[str(k) for k in (loom.get("derived_from") or []) if str(k)],
            valid_at=str(loom.get("valid_at") or ""),
            created_at=str(loom.get("created_at") or ""),
        )
    return None


def parse_files(files: dict[str, str]) -> tuple[list[ChunkRecord], list[DigestRecord]]:
    chunks: list[ChunkRecord] = []
    digests: list[DigestRecord] = []
    for rel, text in sorted(files.items()):
        if not is_evidence_path(rel):
            continue
        rec = parse_file(rel, text)
        if isinstance(rec, ChunkRecord) and rec.session_id:
            chunks.append(rec)
        elif isinstance(rec, DigestRecord) and rec.session_id:
            digests.append(rec)
    return chunks, digests


def edited_chunks(chunks: list[ChunkRecord]) -> list[ChunkRecord]:
    """Chunks whose body hash differs from the exported hash (hand-edited)."""
    return [c for c in chunks if c.content_sha256 and sha256_text(c.content) != c.content_sha256]
