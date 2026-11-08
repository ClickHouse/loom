"""Ingest denylist: which paths the code plane refuses to chunk.

Three layers, checked in this order, first hit wins:

  * the registry override — `exclude` globs on the repo entry
    (LOOM_CODE_REPOS_JSON) or the scalar `LOOM_CODE_INGEST_EXCLUDE`; always
    honoured, this is the operator's lever for a vendored subtree the
    prefixes let in (`base/poco/` in ClickHouse);
  * the repo-local ignore file (`.loomignore` at the indexed commit, one
    glob per line, `#` comments) — the repo author's lever;
  * the built-in generated/minified/vendored rules — suffixes (`.pb.cc`,
    `_pb2.py`, `.min.js`, `.d.ts`, `.snap`, `.lock`, `.map`), the
    `.generated.` infix, and directory segments (node_modules, vendor,
    third_party, dist, build, __pycache__; `contrib/` unless the repo opts
    in). OFF by default: on a large C++ repository these rules match well
    under 1% of live rows — under the 3% floor at which a re-ingest is
    worth its RMT churn. `LOOM_CODE_INGEST_DENYLIST_BUILTIN`
    turns them on for a repo where they bite.

Two content rules, checked after the path layers:

  * binary content (a NUL in the first 8 KiB) is refused regardless of layer;
  * generated content — Go's own convention (`// Code generated … DO NOT
    EDIT.` before the package clause, golang.org/s/generatedcode) and the
    `@generated` marker other generators write — is refused when the repo
    entry says `exclude_generated` (a monorepo's protobuf stubs, mocks and
    ORM clients). The repository's `.gitattributes`
    `linguist-generated` / `linguist-vendored` patterns — its own statement
    of what is generated — join the ignore-file layer, always.

`ingest_file` is the single choke point: the worker, the backfill CLI, the
reconcile heal and the extractor re-chunk all pass through it, so an
excluded path's existing rows are tombstoned by whichever of them next
touches the file — no separate purge.
"""

from __future__ import annotations

import fnmatch
import logging
import posixpath
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# A repository's `.loomignore` and `.gitattributes` together name at most this many
# patterns (real ones: tens).
# not a setting: every indexed path is matched against every pattern; a bound on that
REPO_PATTERNS_MAX = 1000
# not a setting: a `.loomignore` or `.gitattributes` is read up to 1 MiB
PATTERN_FILE_MAX_BYTES = 1 << 20

DEFAULT_SUFFIXES: tuple[str, ...] = (
    ".pb.cc",
    ".pb.h",
    "_pb2.py",
    "_pb2_grpc.py",
    ".min.js",
    ".min.css",
    ".d.ts",
    ".snap",
    ".lock",
    ".map",
)
DEFAULT_INFIXES: tuple[str, ...] = (".generated.",)
DEFAULT_DIRS: tuple[str, ...] = (
    "node_modules",
    "vendor",
    "third_party",
    "third-party",
    "dist",
    "build",
    "__pycache__",
)
OPT_IN_DIRS: tuple[str, ...] = ("contrib",)

_BINARY_PROBE_BYTES = 8192


def is_binary(source: bytes) -> bool:
    """A NUL byte in the first 8 KiB: not text we can chunk."""
    return b"\x00" in source[:_BINARY_PROBE_BYTES]


# golang.org/s/generatedcode: a line matching this regex before the first
# non-comment, non-blank text (the package clause) marks a generated file
_GO_GENERATED_RE = re.compile(rb"^// Code generated .* DO NOT EDIT\.?[ \t]*$", re.M)
# the marker the other generators write (graphql-codegen, prisma, fbjs, buf):
# a comment on one of the first lines carrying `@generated`
_GENERATED_MARK_RE = re.compile(rb"^[ \t]*(?://|#|/\*+|\*|<!--|--|;)[ \t]*@generated\b", re.M)
_GENERATED_PROBE_BYTES = 4096
_GENERATED_MARK_LINES = 8


def is_generated(path: str, source: bytes) -> str:
    """Why the content says the file is generated, '' when it does not:
    `header:go` for the Go convention (the comment sits before the package
    clause), `marker:@generated` for the marker in the first lines."""
    head = source[:_GENERATED_PROBE_BYTES]
    if path.lower().endswith(".go"):
        pkg = re.search(rb"(?m)^package\s", head)
        pre = head[: pkg.start()] if pkg else head
        if _GO_GENERATED_RE.search(pre):
            return "header:go"
    first = b"\n".join(head.split(b"\n", _GENERATED_MARK_LINES)[:_GENERATED_MARK_LINES])
    if _GENERATED_MARK_RE.search(first):
        return "marker:@generated"
    return ""


_LINGUIST_ATTRS = ("linguist-generated", "linguist-vendored")


def parse_gitattributes(text: str) -> tuple[str, ...]:
    """The patterns a `.gitattributes` marks `linguist-generated` or
    `linguist-vendored` (bare or `=true`; `-attr` / `=false` unsets), as
    globs for the ignore layer — a leading `/` stripped, a pattern with no
    `/` matching the basename as git does."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        pattern, attrs = parts[0], parts[1:]
        if any(
            a in _LINGUIST_ATTRS
            or a.split("=", 1)[0] in _LINGUIST_ATTRS
            and a.split("=", 1)[1].lower() == "true"
            for a in attrs
            if not a.startswith("-")
        ):
            out.append(pattern.lstrip("/"))
    return tuple(out)


def parse_ignore_file(text: str) -> tuple[str, ...]:
    """`.loomignore` → glob tuple: one pattern per line, blank lines and
    `#` comments dropped, a leading `/` stripped (paths are repo-relative)."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line.lstrip("/"))
    return tuple(out)


def _pattern_hits(path: str, pattern: str) -> bool:
    """Glob semantics kept deliberately small: a pattern ending in `/` is a
    directory — it matches the path as a prefix or as any inner segment;
    anything else is matched against the whole path and the basename."""
    if pattern.endswith("/"):
        d = pattern.rstrip("/")
        return path.startswith(d + "/") or ("/" + d + "/") in ("/" + path)
    if fnmatch.fnmatchcase(path, pattern):
        return True
    return "/" not in pattern and fnmatch.fnmatchcase(posixpath.basename(path), pattern)


@dataclass(frozen=True)
class Denylist:
    builtin: bool = False
    include_contrib: bool = False
    patterns: tuple[str, ...] = ()  # registry exclude + ignore-file globs
    exclude_generated: bool = False  # The content rule, per repo entry

    def refusal(self, path: str, source: bytes) -> str:
        """Why the file is not indexed — its path (`reason`), then its
        content: binary always, generated when the entry says so."""
        why = self.reason(path)
        if why:
            return why
        if is_binary(source):
            return "excluded: binary"
        if self.exclude_generated:
            gen = is_generated(path, source)
            if gen:
                return f"excluded: generated ({gen})"
        return ""

    # Directory prefixes under `contrib/` that ARE indexed although `include_contrib` is
    # off — a vendored dependency the code calls into by name (`contrib/<lib>`), so
    # its definitions bind the caller's call sites. A prefix's own nested `contrib/`
    # stays out.
    contrib_paths: tuple[str, ...] = ()

    def _contrib_allowed(self, path: str) -> bool:
        for prefix in self.contrib_paths:
            pre = prefix.strip("/") + "/"
            if path.startswith(pre):
                rest_dirs = path[len(pre) :].split("/")[:-1]
                return not any(seg in OPT_IN_DIRS or seg in DEFAULT_DIRS for seg in rest_dirs)
        return False

    def reason(self, path: str) -> str:
        """Why `path` is not indexed; '' when it is."""
        for pattern in self.patterns:
            if _pattern_hits(path, pattern):
                return f"excluded: pattern {pattern}"
        if not self.builtin:
            return ""
        lower = path.lower()
        for suffix in DEFAULT_SUFFIXES:
            if lower.endswith(suffix):
                return f"excluded: suffix {suffix}"
        base = posixpath.basename(lower)
        for infix in DEFAULT_INFIXES:
            if infix in base:
                return f"excluded: generated ({infix})"
        segments = path.split("/")[:-1]
        for seg in segments:
            if seg in DEFAULT_DIRS:
                return f"excluded: dir {seg}/"
            if seg in OPT_IN_DIRS and not self.include_contrib:
                if self._contrib_allowed(path):
                    return ""
                return f"excluded: dir {seg}/ (per-repo opt-in)"
        return ""


def default_denylist() -> Denylist:
    """The denylist when no repo config is at hand (tests, ad-hoc ingest):
    the scalar settings only."""
    from server.settings import settings

    return Denylist(
        builtin=bool(settings.code_ingest_denylist_builtin),
        include_contrib=False,
        patterns=_split(settings.code_ingest_exclude),
    )


def _split(raw: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in str(raw or "").split(",") if p.strip())


def denylist_for(cfg: Any, ignore_text: str = "", attributes_text: str = "") -> Denylist:
    """Denylist for one repo entry: its `exclude` globs, the ignore file's globs, the
    `.gitattributes` linguist patterns, the built-in rules per the settings flag, and
    the entry's generated-content rule."""
    from server.settings import settings

    exclude = tuple(cfg.exclude)
    contrib_paths = tuple(str(p) for p in cfg.include_contrib_paths if str(p))
    from_repo = (*parse_ignore_file(ignore_text), *parse_gitattributes(attributes_text))
    if len(from_repo) > REPO_PATTERNS_MAX:
        logger.warning(
            "code ingest: %s: %d ignore and attribute patterns, the first %d kept",
            cfg.label,
            len(from_repo),
            REPO_PATTERNS_MAX,
        )
        from_repo = from_repo[:REPO_PATTERNS_MAX]
    return Denylist(
        builtin=bool(settings.code_ingest_denylist_builtin),
        include_contrib=cfg.include_contrib,
        patterns=(*exclude, *from_repo),
        exclude_generated=cfg.exclude_generated,
        contrib_paths=contrib_paths,
    )


async def load_denylist(cfg: Any, sha: str, show_file: Any) -> Denylist:
    """`denylist_for` with the repo-local ignore file and `.gitattributes` read at `sha`
    through `show_file(git_dir, sha, path, max_bytes=...)`, each up to 1 MiB and at most
    `REPO_PATTERNS_MAX` patterns together. A missing, unreadable or oversize file is no
    patterns — never a failed pass."""
    name = cfg.ignore_file
    ignore_text = await _show_text(cfg, sha, show_file, name) if name else ""
    attributes_text = await _show_text(cfg, sha, show_file, GITATTRIBUTES)
    return denylist_for(cfg, ignore_text, attributes_text)


GITATTRIBUTES = ".gitattributes"


async def _show_text(cfg: Any, sha: str, show_file: Any, name: str) -> str:
    """The file's text, read up to `PATTERN_FILE_MAX_BYTES`; '' past it."""
    from server.code.app.index.gitrepo import BlobTooLarge

    try:
        raw = await show_file(cfg.git_dir, sha, name, max_bytes=PATTERN_FILE_MAX_BYTES)
    except BlobTooLarge as e:
        logger.warning("code ingest: %s: %s not read, no patterns from it: %s", cfg.label, name, e)
        raw = None
    except Exception:  # noqa: BLE001 — a repo-local file is best-effort
        raw = None
    return raw.decode("utf-8", "replace") if raw else ""
