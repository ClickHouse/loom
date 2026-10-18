"""`loom okf-lint` — the compile gate for an OKF bundle.

A namespace exports as a directory of Markdown so memory changes can be reviewed
as a diff, and a diff nobody can gate is a log, not a review. This module is the
gate. It reads a bundle directory and nothing else — no
ClickHouse, no HTTP — so it runs in a mirror repository's CI, in the mirror
worker before a commit, and in front of `loom import-okf`.

Exit contract (the one a compile step in CI needs):

    2  a critical finding: the bundle is not a well-formed projection
    1  warnings only: reviewable, with the JSON retained
    0  clean

`--strict` promotes warnings to the failing exit. Orphans (concepts that link
to nothing and are linked from nothing) are reported as `info` — they are a
property of the data, not a defect.

Checks, by finding id:

    frontmatter                parses; `type` non-empty                    critical
    required_keys              title, description, loom.memory_key,
                               loom.memory_type on a concept               critical
    type_directory_mismatch    loom.memory_type disagrees with the folder  warning
    relates_to                 well-formed; a `.md` target resolves         critical
    pointer_unmounted          a `loom://org/ns/key` target: not checkable  warning
    unknown_relation           `kind` outside the canonical verbs           warning
    edited_since_export        loom.content_sha256 no longer hashes the
                               body (concepts and evidence files)          warning
    derived_needs_confidence   source_class derived at confidence 1.0      warning
    high_sensitivity_present   a `sensitivity: high` row travelled         warning
    index_missing              a directory or the root has no index.md     critical
    okf_version                the root index declares no okf_version      critical
    index_listing              a concept its indexes do not list           warning
    staging                    a `staging/` directory (never answers)      critical
    drift                      root loom.corpus_digest ≠ the tree           warning
    orphans                    unlinked concepts, one line                 info

The generation digest (`loom.corpus_digest` on the root index): take the concept files only
(not indexes, `log.md`, evidence or digests), sort them by bundle-relative path, append each
file's sha256 hex digest of its UTF-8 bytes without a separator, and sha256 that string.
`bundle_digest` here is the definition both sides use.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from server.core.hashing import sha256_value
from server.memory.domain import okf_bundle as okf
from server.memory.domain import okf_evidence as okf_ev
from server.memory.domain.edge_relations import CANONICAL_RELATIONS

CRITICAL = "critical"
WARNING = "warning"
INFO = "info"

POINTER_PREFIX = "loom://"
STAGING_DIR = "staging"
_MD_LINK = re.compile(r"\]\(([^)\s]+\.md)\)")
_SKIP_PARTS = frozenset({".git", "node_modules", ".atlas-index"})


@dataclass
class Finding:
    id: str
    path: str
    msg: str
    severity: str = CRITICAL


@dataclass
class LintReport:
    root: str
    findings: list[Finding] = field(default_factory=list)
    concepts: int = 0
    evidence_files: int = 0
    strict: bool = False

    def add(self, fid: str, path: str, msg: str, severity: str = CRITICAL) -> None:
        self.findings.append(Finding(fid, path, msg, severity))

    @property
    def criticals(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == CRITICAL]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARNING]

    @property
    def infos(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == INFO]

    @property
    def exit_code(self) -> int:
        if self.criticals:
            return 2
        if self.warnings:
            return 2 if self.strict else 1
        return 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.id] = out.get(f.id, 0) + 1
        return dict(sorted(out.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "strict": self.strict,
            "concepts": self.concepts,
            "evidence_files": self.evidence_files,
            "critical": len(self.criticals),
            "warnings": len(self.warnings),
            "infos": len(self.infos),
            "counts": self.counts(),
            "findings": [asdict(f) for f in self.findings],
        }

    def render(self) -> str:
        lines = [
            f"okf-lint — root={self.root} concepts={self.concepts} evidence={self.evidence_files}"
        ]
        for sev, label in ((CRITICAL, "CRITICAL"), (WARNING, "WARNINGS"), (INFO, "INFO")):
            rows = [f for f in self.findings if f.severity == sev]
            if rows:
                lines.append(f"{label} ({len(rows)}):")
                lines.extend(f"  [{f.id}] {f.path}: {f.msg}" for f in rows)
        verdict = {
            0: "ok — no issues",
            1: "ok — warnings only",
            2: "FAIL — critical issues present",
        }
        if self.exit_code == 2 and not self.criticals:
            lines.append("FAIL — warnings under --strict")
        else:
            lines.append(verdict[self.exit_code])
        return "\n".join(lines)


# ------------------------------------------------------------------
# reading the tree
# ------------------------------------------------------------------


def read_bundle(root: Path) -> dict[str, str]:
    """{bundle-relative posix path: text} for every Markdown file under `root`,
    skipping VCS and tooling directories. Symlinks that escape the root are
    left out (a bundle answers only from itself)."""
    root_r = root.resolve()
    files: dict[str, str] = {}
    for p in sorted(root.rglob("*.md")):
        rel_parts = p.relative_to(root).parts
        if any(part in _SKIP_PARTS or part.startswith(".") for part in rel_parts[:-1]):
            continue
        try:
            p.resolve().relative_to(root_r)
        except ValueError:
            continue
        if not p.is_file():
            continue
        files[p.relative_to(root).as_posix()] = p.read_text(encoding="utf-8", errors="replace")
    return files


def bundle_digest(files: dict[str, str]) -> str:
    """The generation digest: concept files sorted by
    bundle-relative path, each file's sha256 hex digest appended without a
    separator, sha256 of the result. Concept files only — indexes, `log.md`,
    evidence and digests are projections of the concepts or of other planes."""
    h = hashlib.sha256()
    for rel in sorted(files):
        if not okf.is_concept_path(rel):
            continue
        h.update(hashlib.sha256(files[rel].encode("utf-8")).hexdigest().encode("ascii"))
    return h.hexdigest()


def _frontmatter(text: str) -> tuple[dict[str, Any] | None, str, bool]:
    """(mapping or None when unparseable, body, had_frontmatter)."""
    fm_text, body, has_fm = okf.split_frontmatter(text)
    if not has_fm:
        return {}, body, False
    try:
        loaded = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return None, body, True
    if loaded is None:
        return {}, body, True
    if not isinstance(loaded, dict):
        return None, body, True
    return loaded, body, True


def _resolve_link(from_rel: str, href: str) -> str:
    """A `.md` link relative to `from_rel`'s directory → bundle-relative path."""
    base = from_rel.rsplit("/", 1)[0] if "/" in from_rel else ""
    parts = (base.split("/") if base else []) + href.split("/")
    stack: list[str] = []
    for p in parts:
        if p == "..":
            if stack:
                stack.pop()
        elif p and p != ".":
            stack.append(p)
    return "/".join(stack)


# ------------------------------------------------------------------
# checks
# ------------------------------------------------------------------


def _check_concept(
    rep: LintReport, rel: str, text: str, files: dict[str, str], links: dict[str, set[str]]
) -> None:
    fm, body, has_fm = _frontmatter(text)
    if fm is None:
        rep.add("frontmatter", rel, "frontmatter is not a YAML mapping")
        return
    if not has_fm or not str(fm.get("type") or "").strip():
        rep.add("frontmatter", rel, "missing frontmatter or empty `type`")
        return
    raw_loom = fm.get("loom")
    loom: dict[str, Any] = raw_loom if isinstance(raw_loom, dict) else {}
    directory = rel.rsplit("/", 1)[0] if "/" in rel else ""

    for key in ("title", "description"):
        if not str(fm.get(key) or "").strip():
            rep.add("required_keys", rel, f"missing `{key}`")
    for key in ("memory_key", "memory_type"):
        if not str(loom.get(key) or "").strip():
            rep.add("required_keys", rel, f"missing `loom.{key}`")
    mt = str(loom.get("memory_type") or "").strip()
    folder_type = okf.memory_type_for(str(fm.get("type") or ""), directory)
    if mt and directory and directory != mt and folder_type != mt:
        rep.add(
            "type_directory_mismatch",
            rel,
            f"loom.memory_type `{mt}` but the file sits under `{directory}/`",
            WARNING,
        )

    _check_relates_to(rep, rel, fm.get("relates_to"), files, links)
    for m in _MD_LINK.finditer(body):
        tgt = _resolve_link(rel, m.group(1))
        if tgt in files:
            links.setdefault(rel, set()).add(tgt)
            links.setdefault(tgt, set()).add(rel)

    _check_content_hash(rep, rel, fm, body, loom)
    sc = str(loom.get("source_class") or "").strip().lower()
    conf = loom.get("confidence")
    if sc == "derived" and (conf is None or float(conf) >= 1.0):
        rep.add(
            "derived_needs_confidence",
            rel,
            "source_class derived at confidence 1.0: an inference written as a fact",
            WARNING,
        )
    # A row is high by its column or by its sensitivity:high tag; a bundle can carry only the tag.
    if not okf.is_exportable({"sensitivity": loom.get("sensitivity"), "tags": fm.get("tags")}):
        rep.add(
            "high_sensitivity_present",
            rel,
            "a sensitivity=high row travelled in the bundle (export skips them by default)",
            WARNING,
        )


def _check_relates_to(
    rep: LintReport, rel: str, raw: Any, files: dict[str, str], links: dict[str, set[str]]
) -> None:
    if raw is None:
        return
    if not isinstance(raw, list):
        rep.add("relates_to", rel, "relates_to must be a list of {path, kind} mappings")
        return
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            rep.add("relates_to", rel, f"relates_to[{i}] is not a mapping")
            continue
        path = str(item.get("path") or "").strip()
        kind = str(item.get("kind") or "").strip()
        if not path:
            rep.add("relates_to", rel, f"relates_to[{i}] missing `path`")
            continue
        if kind and kind not in CANONICAL_RELATIONS:
            rep.add(
                "unknown_relation",
                rel,
                f"relates_to[{i}] kind `{kind}` is not a canonical verb",
                WARNING,
            )
        if path.startswith(POINTER_PREFIX):
            if len([p for p in path[len(POINTER_PREFIX) :].split("/") if p]) < 3:
                rep.add(
                    "relates_to",
                    rel,
                    f"relates_to[{i}] pointer `{path}` is not loom://org/namespace/key",
                )
            else:
                rep.add(
                    "pointer_unmounted",
                    rel,
                    f"relates_to[{i}] points outside the bundle: {path}",
                    WARNING,
                )
            continue
        if not path.endswith(".md"):
            rep.add("relates_to", rel, f"relates_to[{i}] path `{path}` is not a .md file")
            continue
        tgt = path.lstrip("/") if path in files else _resolve_link(rel, path)
        if tgt not in files:
            rep.add("relates_to", rel, f"relates_to[{i}] target does not resolve: {path}")
            continue
        links.setdefault(rel, set()).add(tgt)
        links.setdefault(tgt, set()).add(rel)


def _check_content_hash(
    rep: LintReport, rel: str, fm: dict[str, Any], body: str, loom: dict[str, Any]
) -> None:
    expected = str(loom.get("content_sha256") or "").strip()
    if not expected:
        return
    title = str(fm.get("title") or "")
    stripped = okf.strip_generated_sections(body.strip("\n"), title)
    candidates = [okf.body_to_value(stripped)]
    first = stripped.split("\n", 1)[0].strip()
    if first.startswith("# "):
        # the exporter's `# <title>` heading survives a frontmatter title edit
        candidates.append(okf.body_to_value(stripped.split("\n", 1)[1] if "\n" in stripped else ""))
    if all(sha256_value(v) != expected for v in candidates):
        rep.add(
            "edited_since_export",
            rel,
            "body no longer hashes to loom.content_sha256 (edited after export)",
            WARNING,
        )


def _check_evidence(rep: LintReport, rel: str, text: str) -> None:
    fm, _body, has_fm = _frontmatter(text)
    if fm is None or not has_fm or not str(fm.get("type") or "").strip():
        rep.add("frontmatter", rel, "missing frontmatter or empty `type`")
        return
    rec = okf_ev.parse_file(rel, text)
    if isinstance(rec, okf_ev.ChunkRecord) and okf_ev.edited_chunks([rec]):
        rep.add(
            "edited_since_export",
            rel,
            "chunk body no longer hashes to loom.content_sha256",
            WARNING,
        )


def _check_indexes(rep: LintReport, files: dict[str, str], concept_paths: list[str]) -> None:
    root_index = files.get("index.md")
    if root_index is None:
        rep.add("index_missing", "index.md", "root index.md missing")
    else:
        fm, _b, has_fm = _frontmatter(root_index)
        if not has_fm or fm is None or not str(fm.get("okf_version") or "").strip():
            rep.add("okf_version", "index.md", "root index declares no `okf_version`")
    by_dir: dict[str, list[str]] = {}
    for rel in concept_paths:
        by_dir.setdefault(rel.rsplit("/", 1)[0] if "/" in rel else "", []).append(rel)
    for d, members in sorted(by_dir.items()):
        idx_rel = f"{d}/index.md" if d else "index.md"
        idx = files.get(idx_rel)
        if idx is None:
            rep.add("index_missing", idx_rel, "directory has concept pages but no index.md")
            continue
        for rel in members:
            name = rel.rsplit("/", 1)[-1]
            if f"({name})" not in idx and f"({rel})" not in idx:
                rep.add("index_listing", rel, f"not listed in {idx_rel}", WARNING)
            if root_index is not None and d and f"({rel})" not in root_index:
                rep.add("index_listing", rel, "not listed in the root index.md", WARNING)


def generation_pointer(files: dict[str, str]) -> dict[str, Any]:
    """The root index.md's `loom` frontmatter (`corpus_digest`, `concepts`, `limit`, ...),
    or {} when there is none."""
    fm, _b, _h = _frontmatter(files.get("index.md", ""))
    loom = fm.get("loom") if isinstance(fm, dict) else None
    return loom if isinstance(loom, dict) else {}


def _check_generation(rep: LintReport, files: dict[str, str], concept_paths: list[str]) -> None:
    loom = generation_pointer(files)
    declared = str(loom.get("corpus_digest") or "").strip()
    if not declared:
        return
    actual = bundle_digest(files)
    if actual != declared:
        n = loom.get("concepts")
        detail = "" if n is None else f" ({len(concept_paths)} concept files, root index says {n})"
        rep.add(
            "drift",
            "index.md",
            f"loom.corpus_digest {declared[:12]}… ≠ tree {actual[:12]}…{detail}",
            WARNING,
        )


def lint_files(files: dict[str, str], *, root: str = ".", strict: bool = False) -> LintReport:
    """Lint an in-memory bundle ({relative path: text}); the pure core."""
    rep = LintReport(root=root, strict=strict)
    if any(rel.split("/", 1)[0] == STAGING_DIR for rel in files):
        rep.add("staging", STAGING_DIR, "a staging/ directory never answers; promote or remove it")
    links: dict[str, set[str]] = {}
    concept_paths = [rel for rel in sorted(files) if okf.is_concept_path(rel)]
    evidence_paths = [
        rel
        for rel in sorted(files)
        if okf_ev.is_evidence_path(rel) and rel.rsplit("/", 1)[-1] not in okf.RESERVED_FILES
    ]
    rep.concepts = len(concept_paths)
    rep.evidence_files = len(evidence_paths)
    for rel in concept_paths:
        _check_concept(rep, rel, files[rel], files, links)
    for rel in evidence_paths:
        _check_evidence(rep, rel, files[rel])
    _check_indexes(rep, files, concept_paths)
    _check_generation(rep, files, concept_paths)
    if len(concept_paths) > 1:
        orphans = [rel for rel in concept_paths if not links.get(rel)]
        if orphans:
            shown = ", ".join(orphans[:5]) + (" …" if len(orphans) > 5 else "")
            rep.add(
                "orphans",
                "",
                f"{len(orphans)} concept(s) link to nothing and are linked from nothing: {shown}",
                INFO,
            )
    return rep


def lint_bundle(root: Path, *, strict: bool = False) -> LintReport:
    """Lint the bundle directory at `root`."""
    root = Path(root)
    if not root.is_dir():
        rep = LintReport(root=str(root), strict=strict)
        rep.add("root", str(root), "not a directory")
        return rep
    files = read_bundle(root)
    rep = lint_files(files, root=str(root), strict=strict)
    if (root / STAGING_DIR).is_dir() and not any(f.id == "staging" for f in rep.findings):
        rep.add("staging", STAGING_DIR, "a staging/ directory never answers; promote or remove it")
    return rep


def report_json(rep: LintReport) -> str:
    return json.dumps(rep.to_dict(), indent=2, ensure_ascii=False)
