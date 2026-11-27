"""Verify dotted Python package re-exports against retained source."""

from __future__ import annotations

import ast
import asyncio
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from server.code.api.models_common import _safe_path
from server.code.app.index import gitrepo
from server.code.app.index.denylist import under_prefixes
from server.code.app.index.mirrors import present_dir
from server.code.app.read import _verified_blob
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.index.repos import repo_for_namespace
from server.code.store.index.sql import _hex

if TYPE_CHECKING:
    from server.code.store.index import CodeRepo

_MAX_INITIALIZER_BYTES = 16_384
_MAX_DESTINATION_PATHS = 8


def _literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_literal(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(key is not None and _literal(key) for key in node.keys) and all(
            _literal(value) for value in node.values
        )
    return False


def _facade_module(source: bytes, export: str) -> str:
    """The module in an unconditional `from .module import Name [as Name]`."""
    try:
        statements = ast.parse(source.decode("utf-8")).body
    except (SyntaxError, UnicodeDecodeError):
        return ""
    module = ""
    for statement in statements:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            for alias in statement.names:
                if alias.name == "*":
                    return ""
                if (alias.asname or alias.name.split(".", 1)[0]) == export:
                    if (
                        module
                        or not isinstance(statement, ast.ImportFrom)
                        or statement.level != 1
                        or not statement.module
                        or not statement.module.isidentifier()
                        or alias.name != export
                    ):
                        return ""
                    module = statement.module
        elif isinstance(statement, ast.Assign) and _literal(statement.value):
            if any(
                not isinstance(target, ast.Name) or target.id == export
                for target in statement.targets
            ):
                return ""
        elif not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)):
            # Control flow, calls and attribute writes can change the public binding.
            return ""
    return module


def _export_site(written: list[str], row: dict[str, Any]) -> tuple[str, str, str] | None:
    path = str(row.get("path") or "")
    parent, _, filename = path.rpartition("/")
    if (
        not parent
        or not filename.endswith(".py")
        or filename == "__init__.py"
        or not _safe_path(path)
        or any(char in path for char in "\0\r\n")
    ):
        return None
    module, *members = str(row.get("qualified_name") or "").split("::")
    if module != module_scope(path) or len(members) not in (1, 2):
        return None
    kinds = {"class", "function"} if len(members) == 1 else {"function", "method", "property"}
    if row.get("kind") not in kinds or written[-len(members) :] != members:
        return None
    package = written[: -len(members)]
    if not package or parent.split("/")[-len(package) :] != package:
        return None
    suffix = "/".join([*package, "__init__.py"])
    return f"{parent}/__init__.py", suffix, members[0]


async def resolve(
    repo: CodeRepo, org: str, ns: str, name: str, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Read at most one verified initializer, and keep the destination's stored identity."""
    written = name.split(".")
    if len(written) < 2 or not all(part.isidentifier() for part in written):
        return []
    sites: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if site := _export_site(written, row):
            sites[site].append(row)
    if len(sites) != 1:
        return []
    site, possible = next(iter(sites.items()))
    initializer, suffix, export = site
    destinations = {str(row["path"]) for row in possible}
    if len(destinations) > _MAX_DESTINATION_PATHS:
        return []
    cfg = repo_for_namespace(org, ns)
    if cfg is None or not under_prefixes(cfg.path_prefixes, initializer):
        return []
    git_dir = await present_dir(cfg)
    if not git_dir:
        return []
    files, by_suffix = await asyncio.gather(
        repo.file_rows_for_paths(org, ns, sorted(destinations | {initializer, suffix})),
        repo.paths_by_suffix(org, ns, [suffix], limit=2),
    )
    roots = set(by_suffix.get(suffix, []))
    if suffix in files:
        roots.add(suffix)
    if roots != {initializer} or (source := files.get(initializer)) is None:
        return []
    commit = source["indexed_commit"]
    if (
        source["lang"] != "python"
        or not source["chunk_count"]
        or not commit
        or not source["blob_sha"]
    ):
        return []
    raw, _ = await _verified_blob(
        git_dir, initializer, commit, source["blob_sha"], max_bytes=_MAX_INITIALIZER_BYTES
    )
    if raw is None or not (module := _facade_module(raw, export)):
        return []
    path = initializer.removesuffix("__init__.py") + module + ".py"
    matched = [row for row in possible if row["path"] == path]
    target = files.get(path)
    if len({(row["qualified_name"], row["symbol_id"]) for row in matched}) != 1 or target is None:
        return []
    if target["lang"] != "python" or target["indexed_commit"] != commit or not target["blob_sha"]:
        return []
    if not under_prefixes(cfg.path_prefixes, path) or any(
        _hex(row.get("indexed_commit")) != commit for row in matched
    ):
        return []
    parent = initializer.removesuffix("__init__.py")
    competing_paths = {
        path.removesuffix(".py") + "/__init__.py",
        f"{parent}{export}.py",
        f"{parent}{export}/__init__.py",
    }
    if await gitrepo.blob_paths_at(git_dir, commit, competing_paths | {path}) != {path}:
        return []
    qualified_name = matched[0]["qualified_name"]
    if qualified_name.count("::") == 2 and not await _has_class_owner(
        repo, org, ns, path, qualified_name.rsplit("::", 1)[0], commit
    ):
        return []
    return matched


async def _has_class_owner(
    repo: CodeRepo, org: str, ns: str, path: str, name: str, commit: str
) -> bool:
    """The index also scopes nested functions, which cannot qualify as class members."""
    owners = await repo.symbols_by_name(org, ns, name, path=path, limit=2)
    return len(owners) == 1 and (
        owners[0]["kind"] == "class"
        and owners[0]["path"] == path
        and owners[0]["qualified_name"] == name
        and _hex(owners[0].get("indexed_commit")) == commit
    )
