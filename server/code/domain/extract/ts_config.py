"""tsconfig / jsconfig module resolution for the TypeScript lane.

The compiler resolves a bare specifier through `compilerOptions.paths`
(one `*` per pattern, longest matching prefix wins, the mapped targets tried
in order, each relative to `baseUrl` or, without one, to the config that
defines the pattern) and, with `baseUrl` set, resolves any bare specifier
under it before `node_modules`. `extends` composes a chain of configs; the
child's options win and each `paths` table stays relative to the file that
wrote it. This module is pure: the caller hands it the texts of the configs
the namespace holds; it never touches the file system.
"""
from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

CONFIG_NAMES = ("tsconfig.json", "jsconfig.json")
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_MAX_EXTENDS = 8
# A config's `paths` keeps at most this many patterns, and each at most this many
# targets (real ones: tens and one to three).
# not a setting: every import specifier is matched against every pattern; a bound on that
PATHS_PATTERNS_MAX = 1000
# not a setting: every target of the matching pattern is a candidate path to look up
PATHS_TARGETS_MAX = 50


def strip_comments(text: str) -> str:
    r"""`text` without its `//` and `/* */` comments, as `re.sub(r"//[^\n]*|/\*.*?\*/",
    "", text, flags=re.S)` gives, in one pass: that pattern searches to the end of the
    text from every unclosed `/*`, quadratic in a file of them."""
    out: list[str] = []
    i = 0
    closed = True  # False once a `/*` has no `*/` after it: no later one has either
    while (j := text.find("/", i)) >= 0 and j + 1 < len(text):
        nxt = text[j + 1]
        if nxt == "/":
            end = text.find("\n", j)
            out.append(text[i:j])
            i = len(text) if end < 0 else end
            continue
        if nxt == "*" and closed:
            end = text.find("*/", j + 2)
            if end >= 0:
                out.append(text[i:j])
                i = end + 2
                continue
            closed = False
        out.append(text[i : j + 1])
        i = j + 1
    out.append(text[i:])
    return "".join(out)


def parse_jsonc(text: str) -> dict[str, Any]:
    """tsconfig is JSON with comments and trailing commas; a file that still
    does not parse is an empty config, never an ingest failure."""
    stripped = _TRAILING_COMMA_RE.sub(r"\1", strip_comments(text or ""))
    try:
        data = json.loads(stripped)
    except (ValueError, RecursionError):  # nesting deeper than the parser's stack
        return {}
    return data if isinstance(data, dict) else {}


@dataclass
class PathsTable:
    """One config's `paths` with the directory its targets are relative to."""
    base: str  # baseUrl resolved against the config dir, or the config dir
    patterns: dict[str, list[str]] = field(default_factory=dict)
    # the wildcard patterns by head, then tail (to the pattern's place in `patterns`),
    # with the tail lengths each head holds, the head lengths longest first and the
    # patterns in order; built at the first lookup, once `patterns` is filled
    _heads: dict[str, tuple[dict[str, int], list[int]]] | None = field(
        default=None, repr=False, compare=False
    )
    _head_lengths: list[int] = field(default_factory=list, repr=False, compare=False)
    _names: list[str] = field(default_factory=list, repr=False, compare=False)

    def best(self, spec: str) -> tuple[str, str] | None:
        """(pattern, `*` binding) of the pattern the compiler picks for `spec`: an exact
        one, else the matching wildcard with the longest head, the first written on a
        tie; None when none matches. One lookup per head and tail length the table
        holds, instead of a match against every pattern."""
        if spec in self.patterns and "*" not in spec:
            return spec, ""
        if self._heads is None:
            by_head: dict[str, dict[str, int]] = {}
            for order, pat in enumerate(self.patterns):
                if "*" in pat:
                    head, _, tail = pat.partition("*")
                    by_head.setdefault(head, {}).setdefault(tail, order)
            self._heads = {
                h: (tails, sorted({len(t) for t in tails})) for h, tails in by_head.items()
            }
            self._head_lengths = sorted({len(h) for h in by_head}, reverse=True)
            self._names = list(self.patterns)
        for n in self._head_lengths:
            if n > len(spec) or spec[:n] not in self._heads:
                continue
            tails, lengths = self._heads[spec[:n]]
            rest = spec[n:]
            orders = [
                tails[rest[len(rest) - k :]]
                for k in lengths
                if k <= len(rest) and rest[len(rest) - k :] in tails
            ]
            if orders:
                pat = self._names[min(orders)]
                return pat, rest[: len(rest) - len(pat.partition("*")[2])]
        return None


@dataclass
class ResolvedConfig:
    dir: str
    base_url: str | None = None  # resolved, repository-relative
    tables: list[PathsTable] = field(default_factory=list)  # child first


def config_dir_for(from_path: str, config_paths: set[str]) -> str | None:
    """The config governing `from_path`: the nearest ancestor directory that
    holds a tsconfig.json (or, failing that, a jsconfig.json). '' is the
    repository root; None when no config governs the file."""
    parts = posixpath.dirname(from_path).split("/") if posixpath.dirname(from_path) else []
    for depth in range(len(parts), -1, -1):
        d = "/".join(parts[:depth])
        if config_path_in(d, config_paths) is not None:
            return d
    return None


def config_path_in(d: str, config_paths: set[str]) -> str | None:
    for name in CONFIG_NAMES:
        p = posixpath.join(d, name) if d else name
        if p in config_paths:
            return p
    return None


def extends_candidates(config_path: str, spec: str, package_dirs: list[str]) -> list[str]:
    """Where an `extends` value may live: relative to the extending config
    (with `.json` appended when absent), or inside a workspace package
    (`@repo/typescript-config/base.json` -> `<pkg dir>/base.json`, the
    package's `tsconfig.json` when only the name is given)."""
    here = posixpath.dirname(config_path)
    if spec.startswith("."):
        stem = posixpath.normpath(posixpath.join(here, spec)) if here else posixpath.normpath(spec)
        if stem.startswith(".."):
            return []
        if stem.endswith(".json"):
            return [stem]
        return [stem + ".json", posixpath.join(stem, "tsconfig.json")]
    parts = spec.split("/")
    sub = "/".join(parts[2:] if spec.startswith("@") else parts[1:])
    out: list[str] = []
    for d in package_dirs:
        out.append(posixpath.join(d, sub) if sub else posixpath.join(d, "tsconfig.json"))
        if sub and not sub.endswith(".json"):
            out.append(posixpath.join(d, sub + ".json"))
    return out


def next_extends(
    config_path: str, text: str, package_dirs_for: Callable[[str], list[str]], present: set[str],
) -> str | None:
    """The config `config_path` extends, when the namespace holds it."""
    ext = parse_jsonc(text).get("extends")
    specs = ext if isinstance(ext, list) else [ext] if isinstance(ext, str) else []
    for spec in specs:
        if not isinstance(spec, str):
            continue
        cands = extends_candidates(config_path, spec, package_dirs_for(spec))
        hit = next((c for c in cands if c in present), None)
        if hit is not None:
            return hit
    return None


def resolve_config(
    config_path: str, texts: dict[str, str], package_dirs_for: Callable[[str], list[str]],
    present: set[str],
) -> ResolvedConfig:
    """Compose the `extends` chain from `config_path` down to its roots.
    `texts` maps a config path to its text (missing = unreadable, chain
    ends); `package_dirs_for(spec)` names the workspace directories a
    package specifier may live in. The child's `baseUrl` wins; every `paths`
    table keeps its own base (the compiler resolves each relative to the
    file that wrote it)."""
    out = ResolvedConfig(dir=posixpath.dirname(config_path))
    seen: set[str] = set()
    cur: str | None = config_path
    while cur is not None and cur not in seen and len(seen) < _MAX_EXTENDS:
        seen.add(cur)
        text = texts.get(cur, "")
        cfg = parse_jsonc(text)
        raw_opts = cfg.get("compilerOptions")
        opts: dict[str, Any] = raw_opts if isinstance(raw_opts, dict) else {}
        here = posixpath.dirname(cur)
        raw_base = opts.get("baseUrl")
        base_url = raw_base if isinstance(raw_base, str) else None
        resolved_base = _join(here, base_url) if base_url is not None else None
        if out.base_url is None and resolved_base is not None:
            out.base_url = resolved_base
        raw_paths = opts.get("paths")
        paths: dict[str, Any] = raw_paths if isinstance(raw_paths, dict) else {}
        if paths:
            table = PathsTable(base=resolved_base if resolved_base is not None else here)
            for pat, targets in paths.items():
                if len(table.patterns) >= PATHS_PATTERNS_MAX:
                    break
                if isinstance(pat, str) and isinstance(targets, list):
                    kept = [t for t in targets if isinstance(t, str)]
                    table.patterns[pat] = kept[:PATHS_TARGETS_MAX]
            out.tables.append(table)
        cur = next_extends(cur, text, package_dirs_for, present)
    return out


def _join(here: str, rel: str) -> str:
    joined = posixpath.normpath(posixpath.join(here, rel)) if here else posixpath.normpath(rel)
    return "" if joined == "." else joined


def paths_candidates(spec: str, cfg: ResolvedConfig) -> list[str]:
    """The repository stems a bare specifier maps to, in the compiler's
    order: the longest-prefix `paths` pattern of the nearest table that
    matches, its targets in order (the `*` substituted); then `baseUrl` +
    the specifier. Relative specifiers map to nothing here."""
    if spec.startswith((".", "/")) or not spec:
        return []
    for table in cfg.tables:
        best = table.best(spec)
        if best is None:
            continue
        stems = (_join(table.base, t.replace("*", best[1], 1)) for t in table.patterns[best[0]])
        return [s for s in dict.fromkeys(stems) if s]  # the nearest matching table decides
    if cfg.base_url is not None:
        stem = _join(cfg.base_url, spec)
        if stem:
            return [stem]
    return []


__all__ = [
    "CONFIG_NAMES", "PathsTable", "ResolvedConfig", "config_dir_for", "config_path_in",
    "extends_candidates", "next_extends", "parse_jsonc", "paths_candidates", "resolve_config",
    "strip_comments",
]
