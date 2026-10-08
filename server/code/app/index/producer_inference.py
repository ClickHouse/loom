"""Which compiler tier a repository's tree calls for.

A repository added from the UI names no producer; the workers that keep the compiler
tiers current (`code_scip_delta` for scip-clang, the TypeScript worker, the Go worker)
ask the tree instead. The rule is the languages' own build manifests, read from the
namespace's live paths — one membership read of a handful of basenames — never a guess
from file counts:

  go.mod                                    → go
  tsconfig.json / jsconfig.json / package.json → typescript (scip-typescript
                                              reads JavaScript through a tsconfig)
  CMakeLists.txt at the root                → clang
  pyproject.toml / setup.py / requirements.txt → python

A tree with several manifests calls for several tiers, and each worker
answers only for its own; an explicit `scip_producer` on the entry wins
over every inference, and `scip_extra_producers` adds tiers beside it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.code.domain.index.repos import RepoConfig

MARKERS: dict[str, tuple[str, ...]] = {
    "go": ("go.mod",),
    "typescript": ("tsconfig.json", "jsconfig.json", "package.json"),
    "clang": ("CMakeLists.txt",),
    "python": ("pyproject.toml", "setup.py", "requirements.txt"),
}
PRODUCERS: tuple[str, ...] = tuple(MARKERS)
ALL_MARKERS: tuple[str, ...] = tuple(m for ms in MARKERS.values() for m in ms)


def infer_producers(paths: Iterable[str], *, root_only_clang: bool = True) -> list[str]:
    """The producers whose manifests the paths carry, in MARKERS order. A
    `CMakeLists.txt` counts only at the repository root (a vendored library's
    own CMake file is not a C++ project) unless `root_only_clang` is off."""
    names = set()
    for p in paths:
        base = p.rsplit("/", 1)[-1]
        if base == "CMakeLists.txt" and root_only_clang and "/" in p:
            continue
        names.add(base)
    return [prod for prod, marks in MARKERS.items() if any(m in names for m in marks)]


def applies(entry_producer: str, worker_producer: str, tree_producers: Iterable[str]) -> bool:
    """Does a compiler-tier worker own this entry? An explicit producer
    decides alone ('clang' is what an `.env` entry says by default); an
    empty one defers to the tree."""
    if entry_producer:
        return entry_producer == worker_producer
    return worker_producer in set(tree_producers)


async def tree_producers(repo: object, org: str, ns: str) -> list[str]:
    """The producers the namespace's live tree calls for: the manifests at the
    repository root (one membership read) plus any `go.mod` below it (a Go monorepo has
    none at the root)."""
    present = await repo.live_paths_present(org, ns, list(ALL_MARKERS))  # type: ignore[attr-defined]
    paths = list(present)
    if "go.mod" not in present:
        below = await repo.live_paths_named(org, ns, "go.mod", limit=1)  # type: ignore[attr-defined]
        paths.extend(below)
    return infer_producers(paths)


async def worker_owns(
    repo: object,
    cfg: RepoConfig,
    worker_producer: str,
) -> bool:
    """The compiler-tier worker's ownership test — the entry's explicit producer, else
    (an entry naming none, `''`) the tree's manifests."""
    if worker_producer in cfg.scip_extra_producers:
        return True  # a further tier the entry names beside its primary one
    if cfg.scip_producer:
        return cfg.scip_producer == worker_producer
    tree = await tree_producers(repo, cfg.org, cfg.namespace)
    return applies("", worker_producer, tree)


__all__ = [
    "ALL_MARKERS",
    "MARKERS",
    "PRODUCERS",
    "applies",
    "infer_producers",
    "tree_producers",
    "worker_owns",
]
