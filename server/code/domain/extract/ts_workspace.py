"""The workspace registry: which directories are workspace
packages and what each is named.

`package_dirs_for` guesses a package's directory from its name under the
conventional roots (`packages/`, `apps/`, `libs/`, the root); a workspace
whose package is named otherwise (`shadcn-svelte` in `packages/cli`) or
rooted elsewhere (`sv-addons/*`) binds nothing. The manifests say exactly:
`pnpm-workspace.yaml` `packages:` globs, or the root `package.json`
`workspaces` (a list, or `{"packages": [...]}`), expanded against the
namespace's live paths to the member directories that hold a
`package.json`, each read for its `name`. Pure: the caller hands in texts
and the path set.
"""
from __future__ import annotations

import json
import posixpath
import re

from server.code.domain.extract.ts_config import strip_comments

# `[^\S\n]`: whitespace within the line; `^\s*` would scan on through every blank line
# below each line start, quadratic in a file of them.
# The item ends on a character that is not whitespace and the runs around the closing
# quote are possessive: a lazy item before `\s*` splits a run of spaces every way.
_YAML_ITEM_RE = re.compile(
    r"^[^\S\n]*-\s*+['\"]?([^'\"#\n]*[^'\"#\s])[^\S\n]*+['\"]?[^\S\n]*+(?:#.*)?$", re.M
)
# not a setting: each member glob is matched against every package directory
MEMBER_GLOBS_MAX = 1000
_YAML_PACKAGES_RE = re.compile(r"^packages:\s*$", re.M)
_YAML_TOP_KEY_RE = re.compile(r"^[A-Za-z_][\w-]*:", re.M)


def workspace_globs(pnpm_yaml: str, root_package_json: str) -> list[str]:
    """The member globs, `pnpm-workspace.yaml` first, then npm / yarn
    `workspaces`; '' for both when the repository declares no workspace."""
    out: list[str] = []
    m = _YAML_PACKAGES_RE.search(pnpm_yaml or "")
    if m:
        rest = pnpm_yaml[m.end():]
        nxt = _YAML_TOP_KEY_RE.search(rest)
        block = rest[: nxt.start()] if nxt else rest
        out.extend(g.strip() for g in _YAML_ITEM_RE.findall(block) if not g.strip().startswith("!"))
    try:
        data = json.loads(root_package_json or "{}")
    except (ValueError, RecursionError):  # nesting deeper than the parser's stack
        data = {}
    ws = data.get("workspaces") if isinstance(data, dict) else None
    if isinstance(ws, dict):
        ws = ws.get("packages")
    if isinstance(ws, list):
        out.extend(str(g).strip() for g in ws if isinstance(g, str) and not g.startswith("!"))
    return [g for g in dict.fromkeys(g.lstrip("./").rstrip("/") for g in out) if g]


def extra_manifest_globs(lerna_json: str, rush_json: str, nx_json: str) -> list[str]:
    """The other monorepo tools' member lists — Lerna's
    `packages` globs (`lerna.json`), Rush's `projects[].projectFolder`
    (`rush.json`, JSON with comments), Nx's `workspaceLayout` roots
    (`nx.json`: `appsDir` / `libsDir`, as `<dir>/*`). Each is optional."""
    out: list[str] = []
    for text in (lerna_json, rush_json, nx_json):
        if not text:
            continue
        stripped = strip_comments(text)
        stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
        try:
            data = json.loads(stripped)
        except (ValueError, RecursionError):  # nesting deeper than the parser's stack
            continue
        if not isinstance(data, dict):
            continue
        pk = data.get("packages")
        if isinstance(pk, list):
            out.extend(str(g) for g in pk if isinstance(g, str))
        for proj in data.get("projects") or []:
            if isinstance(proj, dict) and isinstance(proj.get("projectFolder"), str):
                out.append(proj["projectFolder"])
        layout = data.get("workspaceLayout")
        if isinstance(layout, dict):
            for key in ("appsDir", "libsDir"):
                if isinstance(layout.get(key), str) and layout[key]:
                    out.append(layout[key].rstrip("/") + "/*")
    return [g for g in dict.fromkeys(g.lstrip("./").rstrip("/") for g in out) if g]


def member_dirs(globs: list[str], present: set[str]) -> list[str]:
    """The directories the globs name that hold a `package.json` in the
    namespace: `docs` → `docs`; `packages/*` → each `packages/<x>`;
    `packages/**` → every depth below. The first `MEMBER_GLOBS_MAX` globs count."""
    manifests = [p for p in present if posixpath.basename(p) == "package.json" and "/" in p]
    dirs = sorted(posixpath.dirname(p) for p in manifests)
    known = set(dirs)
    out: dict[str, None] = {}  # ordered, and a membership test that does not scan
    for g in globs[:MEMBER_GLOBS_MAX]:
        if "*" not in g:
            if g in known:
                out[g] = None
            continue
        head = g.split("*", 1)[0].rstrip("/")
        deep = "**" in g
        for d in dirs:
            if not d.startswith(head + "/" if head else ""):
                continue
            rest = d[len(head) + 1:] if head else d
            if "node_modules" in rest.split("/"):
                continue
            if deep or "/" not in rest:
                out[d] = None
    return list(out)


def registry(member_texts: dict[str, str]) -> dict[str, str]:
    """{package name: directory} from each member's `package.json`."""
    out: dict[str, str] = {}
    for d, text in member_texts.items():
        try:
            data = json.loads(text or "{}")
        except (ValueError, RecursionError):  # nesting deeper than the parser's stack
            m = re.search(r'"name"\s*:\s*"([^"]+)"', text or "")  # a manifest the reader mangled
            data = {"name": m.group(1)} if m else None
        name = data.get("name") if isinstance(data, dict) else None
        if isinstance(name, str) and name:
            out[name] = d
    return out


def package_name_of(spec: str) -> str:
    """`@scope/name/sub` → `@scope/name`; `name/sub` → `name`; '' for a
    relative or alias specifier."""
    if not spec or spec.startswith((".", "/", "@/", "~/", "#/", "$")):
        return ""
    parts = spec.split("/")
    if spec.startswith("@"):
        return "/".join(parts[:2]) if len(parts) >= 2 else ""
    return parts[0]


__all__ = ["extra_manifest_globs", "member_dirs", "package_name_of", "registry", "workspace_globs"]
