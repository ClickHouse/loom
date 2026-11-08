"""Instruction-file drift: do the paths (and symbols) an agent-instruction
file names still exist at the indexed head?

CLAUDE.md, AGENTS.md, copilot-instructions and skill files are read into every agent
session and rot silently: a file is moved or deleted and the instruction keeps pointing
at the old path until an agent follows it and wastes a turn.

The index already knows every path (the bare clone's tree at head) and every
symbol (loom.code_symbols), so the check is deterministic — no LLM, no
heuristic re-reading of the repo. Pure functions here take text and a tree;
`lint_repo` wires them to a configured repo entry.
"""

from __future__ import annotations

import fnmatch
import logging
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from server.settings import settings

logger = logging.getLogger(__name__)

# Files agents read automatically or on demand. Matched against the tree
# at head, so a repo with none of them yields an empty report, not an error.
INSTRUCTION_FILE_PATTERNS: tuple[str, ...] = (
    r"^CLAUDE\.md$",
    r"^AGENTS\.md$",
    r"^GEMINI\.md$",
    r"^\.cursorrules$",
    r"^\.github/copilot-instructions\.md$",
    r"^\.github/instructions/.*\.md$",
    r"^\.github/(agents|skills|prompts)/.*\.md$",
    r"^\.claude/.*\.md$",
    r"^\.cursor/rules/.*\.mdc?$",
    r"^\.agents?/.*\.md$",
    r"^skills/[^/]+/SKILL\.md$",
)
_INSTRUCTION_RE = re.compile("|".join(f"(?:{p})" for p in INSTRUCTION_FILE_PATTERNS))

# A repo-relative path: at least one directory component, a file name with
# a short extension, no URL scheme in front. Leading `./` is tolerated.
_PATH_RE = re.compile(
    r"(?<![\w/@:.-])"  # not glued to a URL, an @handle or another path
    r"(?:\./)?"
    # each component starts with a word char or dot: "-DFLAG=cmake/x.txt"
    # must not yield "-cmake/x.txt"
    r"((?:[A-Za-z0-9_.][A-Za-z0-9_.-]*/)+[A-Za-z0-9_.][A-Za-z0-9_.-]*\.[A-Za-z0-9]{1,8})"
    r"(?![\w/])"
)
# C++/Rust-style qualified names in backticks: `DB::IColumn::insert`.
_SYMBOL_RE = re.compile(r"`((?:[A-Za-z_]\w*::)+[A-Za-z_]\w*)(?:\([^)`]*\))?`")

# Components that mark an illustrative path rather than a real one.
_PLACEHOLDER_PARTS = frozenset(
    {
        "path",
        "to",
        "some",
        "foo",
        "bar",
        "baz",
        "example",
        "examples",
        "your",
        "my",
        "xxx",
        "yyy",
        "nn",
        "n",
        "file",
        "name",
        "module",
    }
)
_PLACEHOLDER_CHARS = re.compile(r"[<>{}$*|]")
_VERSIONISH = re.compile(r"^\d+(\.\d+)+$")  # "1.2.3" caught by the path regex


@dataclass
class Ref:
    kind: str  # path | symbol
    value: str
    line: int
    in_code_block: bool = False


@dataclass
class Finding:
    ref: Ref
    suggestions: list[str] = field(default_factory=list)


@dataclass
class FileReport:
    path: str
    tokens: int
    refs: int
    stale: list[Finding] = field(default_factory=list)
    skipped: str = ""  # why the file was not linted (over the byte cap)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "tokens": self.tokens,
            "refs": self.refs,
            "skipped": self.skipped,
            "stale": [
                {
                    "kind": f.ref.kind,
                    "ref": f.ref.value,
                    "line": f.ref.line,
                    "in_code_block": f.ref.in_code_block,
                    "suggestions": f.suggestions,
                }
                for f in self.stale
            ],
        }


@dataclass
class LintReport:
    head: str
    files: list[FileReport]

    @property
    def stale_count(self) -> int:
        return sum(len(f.stale) for f in self.files)

    @property
    def tokens(self) -> int:
        return sum(f.tokens for f in self.files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "head": self.head,
            "files": [f.to_dict() for f in self.files],
            "instruction_files": len(self.files),
            "instruction_tokens": self.tokens,
            "stale_refs": self.stale_count,
        }


def is_instruction_file(path: str) -> bool:
    return bool(_INSTRUCTION_RE.match(path))


def find_instruction_files(tree: list[str] | set[str]) -> list[str]:
    return sorted(p for p in tree if is_instruction_file(p))


def select_targets(tree: list[str] | set[str], globs: list[str] | None = None) -> list[str]:
    """Instruction files plus every tree path matching one of `globs`
    (fnmatch semantics: `*` crosses `/`, so `docs/*.md` is the whole docs
    tree). Product documentation drifts the same way instruction files do,
    and the resolver does not care what the file is called."""
    out = set(find_instruction_files(tree))
    for g in globs or []:
        out.update(p for p in tree if fnmatch.fnmatchcase(p, g))
    return sorted(out)


def _is_placeholder(path: str) -> bool:
    if _PLACEHOLDER_CHARS.search(path):
        return True
    parts = path.lower().split("/")
    if any(p in _PLACEHOLDER_PARTS for p in parts[:-1]):
        return True
    stem = parts[-1].rsplit(".", 1)[0].lower()
    if stem in _PLACEHOLDER_PARTS or _VERSIONISH.match(parts[-1]):
        return True
    # "src/path/File.cpp" — CamelCase generic file names next to a
    # placeholder directory are examples, not references.
    return False


def extract_refs(text: str, *, symbols: bool = False) -> list[Ref]:
    """Path (and optionally qualified-symbol) references with line numbers.
    Fenced code blocks are tracked so a caller can weigh `in_code_block`
    refs differently — a stale path in a command block still misfires when
    an agent runs it, so they are reported, not dropped."""
    refs: list[Ref] = []
    seen: set[tuple[str, str]] = set()
    in_block = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_block = not in_block
            continue
        for m in _PATH_RE.finditer(line):
            value = posixpath.normpath(m.group(1))
            if value.startswith("..") or _is_placeholder(value):
                continue
            if ("path", value) in seen:
                continue
            seen.add(("path", value))
            refs.append(Ref("path", value, lineno, in_block))
        if symbols:
            for m in _SYMBOL_RE.finditer(line):
                value = m.group(1)
                if ("symbol", value) in seen:
                    continue
                seen.add(("symbol", value))
                refs.append(Ref("symbol", value, lineno, in_block))
    return refs


def suggest_paths(missing: str, tree: set[str], *, limit: int = 3) -> list[str]:
    """Same file name elsewhere in the tree — the usual shape of a move."""
    base = posixpath.basename(missing)
    cands = sorted(p for p in tree if posixpath.basename(p) == base)
    if len(cands) > limit:
        # Prefer the candidate sharing the longest directory prefix.
        want = missing.split("/")

        def shared(p: str) -> int:
            got = p.split("/")
            return sum(1 for a, b in zip(want, got, strict=False) if a == b)

        cands.sort(key=lambda p: (-shared(p), p))
    return cands[:limit]


def resolve_path(ref: str, tree: set[str], *, relative_to: str = "") -> str | None:
    """The tree path a reference denotes, or None. A reference resolves
    from the repo root or relative to the instruction file's own directory
    (skills say `scripts/run.sh` meaning `.claude/skills/x/scripts/run.sh`)."""
    if ref in tree:
        return ref
    if relative_to:
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(relative_to), ref))
        if joined in tree:
            return joined
    return None


def _top_level_dirs(tree: set[str]) -> set[str]:
    return {p.split("/", 1)[0] for p in tree if "/" in p}


def lint_text(
    path: str, text: str, tree: set[str], *, symbol_exists: dict[str, bool] | None = None
) -> FileReport:
    """Report for one instruction file against a tree (set of blob paths).
    `symbol_exists` maps qualified names already resolved by the caller;
    symbols absent from it are not judged.

    A path whose first component is not a directory of the repo (or of the
    instruction file's own directory) is not a reference INTO the repo —
    `tmp/perf.tsv`, `left/server.log` are runtime artefacts the instruction
    tells the agent to produce — so it is neither checked nor counted."""
    refs = extract_refs(text, symbols=symbol_exists is not None)
    top = _top_level_dirs(tree)
    here = posixpath.dirname(path)
    kept: list[Ref] = []
    stale: list[Finding] = []
    for ref in refs:
        if ref.kind == "path":
            first = ref.value.split("/", 1)[0]
            local_first = posixpath.normpath(posixpath.join(here, first)) if here else first
            if first not in top and not any(p.startswith(local_first + "/") for p in tree):
                continue  # runtime artefact, not a repo reference
            kept.append(ref)
            if resolve_path(ref.value, tree, relative_to=path) is not None:
                continue
            stale.append(Finding(ref, suggest_paths(ref.value, tree)))
            continue
        kept.append(ref)
        if symbol_exists is not None and ref.value in symbol_exists:
            if not symbol_exists[ref.value]:
                stale.append(Finding(ref))
    return FileReport(path=path, tokens=len(text) // 4, refs=len(kept), stale=stale)


async def lint_repo(
    cfg: Any,
    *,
    files: list[str] | None = None,
    symbols: bool = False,
    repo: Any | None = None,
    globs: list[str] | None = None,
) -> LintReport:
    """Lint every instruction file at the bare clone's head (or `files`),
    plus any tree path matching `globs` (e.g. `docs/*.md`). With `symbols`,
    qualified names in backticks are checked against loom.code_symbols
    through `repo` (a CodeRepo)."""
    from server.code.app.index import gitrepo

    head = await gitrepo.head_sha(cfg.git_dir, cfg.git_branch)
    tree = set(await gitrepo.ls_tree(cfg.git_dir, head))
    targets = files if files else select_targets(tree, globs)
    reports: list[FileReport] = []
    for path in targets:
        try:
            raw = await gitrepo.show_file(
                cfg.git_dir, head, path, max_bytes=settings.code_ingest_max_file_bytes
            )
        except gitrepo.BlobTooLarge as e:
            logger.warning("instructions lint: %s: %s not read: %s", cfg.label, path, e)
            reports.append(
                FileReport(path=path, tokens=0, refs=0, skipped="over code_ingest_max_file_bytes")
            )
            continue
        if raw is None:
            reports.append(
                FileReport(
                    path=path,
                    tokens=0,
                    refs=0,
                    stale=[
                        Finding(Ref("path", path, 0), suggest_paths(path, tree)),
                    ],
                )
            )
            continue
        text = raw.decode("utf-8", errors="replace")
        resolved: dict[str, bool] | None = None
        if symbols and repo is not None:
            resolved = {}
            for ref in extract_refs(text, symbols=True):
                if ref.kind != "symbol" or ref.value in resolved:
                    continue
                rows = await repo.symbols_by_name(cfg.org, cfg.namespace, ref.value, limit=1)
                resolved[ref.value] = bool(rows)
        reports.append(lint_text(path, text, tree, symbol_exists=resolved))
    return LintReport(head=head, files=reports)


def render_report(report: LintReport, *, stale_only: bool = False) -> str:
    lines = [
        f"instruction files: {len(report.files)} · tokens: {report.tokens:,} · "
        f"stale refs: {report.stale_count} · head {report.head[:12]}",
    ]
    for f in report.files:
        if stale_only and not f.stale and not f.skipped:
            continue
        if f.skipped:
            lines.append(f"  {f.path}  (not linted: {f.skipped})")
            continue
        lines.append(f"  {f.path}  ({f.tokens:,} tok, {f.refs} refs, {len(f.stale)} stale)")
        for finding in f.stale:
            where = f":{finding.ref.line}" if finding.ref.line else ""
            tag = " [code block]" if finding.ref.in_code_block else ""
            lines.append(f"    - {finding.ref.kind} {finding.ref.value}{where}{tag}")
            if finding.suggestions:
                lines.append(f"        did you mean: {', '.join(finding.suggestions)}")
    return "\n".join(lines)
