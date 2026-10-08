#!/usr/bin/env python3
"""Give scip-typescript a way to follow workspace imports without a build.

In a pnpm / yarn monorepo a package's `exports` table points at its BUILD
output (`@langfuse/shared` → `./dist/src/index.js`). On a checkout that was
only `install`ed, TypeScript resolves `import { f } from "@langfuse/shared"`
to nothing, scip-typescript records the reference as an untyped external,
and every cross-package call edge is lost (langfuse: 0 compiler edges into
`packages/shared` from `web/` and `worker/` on the first index).

This script derives `compilerOptions.paths` entries from every workspace
package's `exports` (and `main` / `types`), mapping each specifier to the
SOURCE file the build target came from (`dist/` → `src/`, `.js` → `.ts` /
`.tsx`, an existing file wins), and writes them into the consumer packages'
`tsconfig.json` — a backup `tsconfig.json.scip-bak` sits beside each, and
`--restore` puts the originals back. Run it before `scip-typescript index`
on a checkout that has no build output; skip it on a built checkout.

    python scip_ts_paths.py <checkout> [--restore] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

_BUILD_DIRS = ("dist", "build", "lib", "out", "esm", "cjs")
_SRC_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_CONDITIONS = ("types", "import", "module", "default", "require", "node")


def _pick(target):
    if isinstance(target, str):
        return target
    if isinstance(target, dict):
        for cond in _CONDITIONS:
            if cond in target:
                picked = _pick(target[cond])
                if picked:
                    return picked
        for value in target.values():
            picked = _pick(value)
            if picked:
                return picked
    return ""


def _source_for(pkg_dir: Path, target: str) -> Path | None:
    """The source file a build target was emitted from, if it exists."""
    rel = target[2:] if target.startswith("./") else target.lstrip("/")
    stem = re.sub(r"\.d\.ts$|\.(js|mjs|cjs|jsx|ts|tsx)$", "", rel)
    forms = [stem]
    segs = stem.split("/")
    if segs and segs[0] in _BUILD_DIRS:
        forms.append("/".join(["src", *segs[1:]]))
        forms.append("/".join(segs[1:]))
    for form in forms:
        for ext in _SRC_EXTS:
            p = pkg_dir / (form + ext)
            if p.is_file():
                return p
        for ext in _SRC_EXTS:
            p = pkg_dir / form / ("index" + ext)
            if p.is_file():
                return p
    return None


def workspace_packages(root: Path) -> list[tuple[str, Path, dict]]:
    """(name, directory, package.json) for every named package.json under
    `root` outside node_modules and dot-directories, sorted by path so every
    run sees the packages in the same order. The converter maps symbols to
    directories through the same list."""
    out = []
    for pj in sorted(root.rglob("package.json")):
        rel = pj.relative_to(root)
        if "node_modules" in rel.parts or any(p.startswith(".") for p in rel.parts[:-1]):
            continue
        try:
            data = json.loads(pj.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("name"), str) and data["name"]:
            out.append((data["name"], pj.parent, data))
    return out


def _expand_pattern(pkg_dir: Path, key: str, target: str) -> list[tuple[str, str]]:
    """Every concrete (subpath, target) a `*` export names: the star is the
    name of each entry that exists under the source form of the target's
    directory (`dist/src/features/*/index.js` → the dirs of `src/features/`)."""
    head, _, tail = target.partition("*")
    rel = head[2:] if head.startswith("./") else head.lstrip("/")
    segs = rel.rstrip("/").split("/") if rel.strip("/") else []
    roots = [rel]
    if segs and segs[0] in _BUILD_DIRS:
        roots.append("/".join(["src", *segs[1:]]) + ("/" if rel.endswith("/") else ""))
        roots.append("/".join(segs[1:]) + ("/" if rel.endswith("/") else ""))
    out: list[tuple[str, str]] = []
    for root in roots:
        base = pkg_dir / root if root else pkg_dir
        parent = base if root.endswith("/") or not root else base.parent
        prefix = "" if root.endswith("/") or not root else base.name
        if not parent.is_dir():
            continue
        for entry in sorted(parent.iterdir()):
            name = entry.name
            if prefix and not name.startswith(prefix):
                continue
            star = name[len(prefix) :]
            star = re.sub(r"\.(d\.ts|tsx?|jsx?|mjs|cjs)$", "", star) if entry.is_file() else star
            if not star or star.startswith("."):
                continue
            out.append((key.replace("*", star), target.replace("*", star)))
        if out:
            break
    return out


def paths_for(consumer_dir: Path, packages: list[tuple[str, Path, dict]]) -> dict[str, list[str]]:
    """{specifier: [relative source path]} for every exported subpath of
    every OTHER workspace package, relative to the consumer's tsconfig."""
    paths: dict[str, list[str]] = {}
    for name, pkg_dir, data in packages:
        if pkg_dir == consumer_dir:
            continue
        exports = data.get("exports")
        entries: list[tuple[str, str]] = []
        if isinstance(exports, dict) and any(k.startswith(".") for k in exports):
            for key, target in exports.items():
                picked = _pick(target)
                if not picked:
                    continue
                if "*" in key:
                    # A pattern export (`./features/*` →
                    # `./dist/src/features/*/index.js`) is expanded over the
                    # directories that exist under the target's source root
                    entries.extend(_expand_pattern(pkg_dir, key, picked))
                    continue
                entries.append((key, picked))
        else:
            picked = _pick(exports) if exports else ""
            fallback = data.get("types") or data.get("module") or data.get("main") or ""
            picked = picked or str(fallback)
            if picked:
                entries.append((".", picked))
        for key, target in entries:
            src = _source_for(pkg_dir, target)
            if src is None:
                continue
            spec = name if key == "." else name + key[1:]
            paths[spec] = [os.path.relpath(src, consumer_dir).replace(os.sep, "/")]
    return paths


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("checkout")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    root = Path(args.checkout).resolve()
    packages = workspace_packages(root)
    changed = 0
    for _name, pkg_dir, _data in packages:
        tsconfig = pkg_dir / "tsconfig.json"
        backup = pkg_dir / "tsconfig.json.scip-bak"
        if args.restore:
            if backup.exists():
                backup.replace(tsconfig)
                changed += 1
            continue
        if not tsconfig.exists():
            continue
        paths = paths_for(pkg_dir, packages)
        if not paths:
            continue
        text = tsconfig.read_text(encoding="utf-8")
        # tsconfig allows comments and trailing commas; strip them for json
        # (outside string literals: `"@/*"` is a path pattern, not a comment)
        stripped = re.sub(
            r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/',
            lambda m: m.group(1) or "",
            text,
            flags=re.S,
        )
        stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
        try:
            cfg = json.loads(stripped)
        except ValueError:
            print(f"skip {tsconfig}: not parseable", file=sys.stderr)
            continue
        co = cfg.setdefault("compilerOptions", {})
        existing = co.get("paths") or {}
        co["paths"] = {**paths, **existing}  # a package's own aliases win
        co.setdefault("baseUrl", ".")
        if args.dry_run:
            print(f"{tsconfig.relative_to(root)}: +{len(paths)} paths")
            continue
        if not backup.exists():
            backup.write_text(text, encoding="utf-8")
        tsconfig.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        changed += 1
        print(f"{tsconfig.relative_to(root)}: +{len(paths)} paths")
    print(f"{'restored' if args.restore else 'patched'} {changed} tsconfig(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
