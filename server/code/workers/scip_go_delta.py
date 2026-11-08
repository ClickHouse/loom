"""Per-merge compiler-tier freshness for Go.

`code_scip_delta` keeps the C++ compiler tier current, `code_scip_ts_delta`
 the TypeScript one. This worker is the Go row: for every
repository entry whose `scip_producer` is "go" it compares `git_head` with
`scip_commit_go`, moves the configured checkout to `git_head` (fetching from
the LOCAL bare clone code_ingest already advanced — never the network),
works out which Go modules the window touched (the nearest `go.mod` above
every changed `.go` / `go.mod` / `go.sum`; the first run with no watermark
is every module), runs `run_go_monorepo_delta.sh` in DELTA_OUT_DIR mode over
those modules only (scip-go per module, repository-relative paths, generated
documents dropped) and applies each module's
JSON delta in-process through the same validated apply path the upload ops
use.

scip-go indexes a module whole (a type-checked build), so a window that
touched three of hundreds of modules costs three module builds, not the tree; the
module cache under `code_scip_go_modcache` makes the second build of a
module cheap. A module whose `go mod download` fails (a `replace` onto a
path the checkout lacks) is skipped by the runner and named in the pass
outcome, never fatal to the pass; it fails the tier's health record (kind `module`)
until a run builds again.

House rule: the SERVER never parses protobuf — scip-go and the
conversion run in subprocesses; the worker consumes the JSON deltas.
The worker is enabled by default; entries on a host without the Go toolchain
or scip-go skip cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from server.code.app.index.compiler_tier import (
    BaseUnknown,
    TierDegraded,
    checkout_fetch_args,
    compiler_target,
    git,
    harness_failure,
    resolve_checkout,
    run_harness,
    run_tier,
)
from server.code.app.index.gitrepo import (
    UNQUOTED_PATHS,
    auth_env,
    communicate_or_kill,
    git_config_env,
    nul_fields,
)
from server.code.app.index.sandbox import tool_mounts
from server.code.app.index.untrusted_files import read_delta
from server.code.domain.index.tier_health import module_failures, without_module_failures
from server.core.paths import SCIP_HARNESS_DIR
from server.settings import settings

if TYPE_CHECKING:
    from server.code.domain.index.repos import RepoConfig
    from server.code.store.index import CodeRepo

logger = logging.getLogger("loom.workers.code_scip_go_delta")

_HARNESS = SCIP_HARNESS_DIR / "run_go_monorepo_delta.sh"
PRODUCER = "go"
WATERMARK = "scip_commit_go"
_MODULE_FILES = ("go.mod", "go.sum")


@dataclass
class CodeScipGoDeltaResult:
    repos_checked: int = 0
    deltas_applied: int = 0
    modules_indexed: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # modules the runner could not build (`go mod download` / scip-go failed)
    modules_skipped: list[str] = field(default_factory=list)


async def run_pass() -> CodeScipGoDeltaResult:
    return await run_tier(
        "code_scip_go_delta",
        PRODUCER,
        WATERMARK,
        CodeScipGoDeltaResult(),
        _run_repo,
        gc_checkout=True,
        extras=lambda r: {
            "modules_indexed": r.modules_indexed,
            "modules_skipped": r.modules_skipped[:5],
        },
    )


def _resolve_paths(cfg: RepoConfig) -> tuple[Path, Path, str | None]:
    """(checkout, bare clone, missing), in a thread. The toolchain is checked here
    too: the pass skips, never errors, on a host without `go` or scip-go."""
    checkout, bare, missing = resolve_checkout(cfg, PRODUCER)
    if missing:
        return checkout, bare, missing
    if not _which("go", settings.code_scip_go_bin_dir):
        return checkout, bare, "go toolchain not found (code_scip_go_bin_dir)"
    if not _which(settings.code_scip_go_path, settings.code_scip_go_bin_dir):
        return checkout, bare, f"scip-go not found at {settings.code_scip_go_path}"
    return checkout, bare, None


def _which(name: str, extra_dir: str) -> bool:
    p = Path(name)
    if p.is_absolute():
        return p.exists()
    dirs = ([extra_dir] if extra_dir else []) + os.environ.get("PATH", "").split(os.pathsep)
    return any((Path(d) / name).exists() for d in dirs if d)


def modules_for_window(changed: list[str], module_dirs: list[str]) -> list[str]:
    """The module directories a window's changed paths fall under: the
    nearest `go.mod` directory above each path (the module index rule), '.'
    for the root module; pure, sorted, deduplicated."""
    dirs = sorted({d.strip("/") for d in module_dirs}, key=len, reverse=True)
    out: set[str] = set()
    for path in changed:
        base = path.rsplit(".", 1)[-1]
        if not (path.endswith(".go") or posixpath.basename(path) in _MODULE_FILES) and base != "go":
            continue
        for d in dirs:
            if d == "" or path.startswith(d + "/"):
                out.add(d or ".")
                break
    return sorted(out)


async def _module_dirs(checkout: Path) -> list[str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *UNQUOTED_PATHS,
        "-C",
        str(checkout),
        "ls-files",
        "-z",
        "--",
        "go.mod",
        "**/go.mod",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=auth_env(),
    )
    out, _err = await communicate_or_kill(proc, timeout_s=settings.code_git_timeout_s)
    return sorted({posixpath.dirname(p) for p in nul_fields(out.decode("utf-8", "replace"))})


async def _changed_paths(checkout: Path, base: str, head: str) -> list[str]:
    """Paths changed in `base..head`. A base the checkout cannot resolve is
    `BaseUnknown` (the next run indexes in full); any other git failure
    raises, so an empty answer never advances the watermark past a window."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *UNQUOTED_PATHS,
        "-C",
        str(checkout),
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        "--end-of-options",
        base,
        head,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=auth_env(),
    )
    out, _err = await communicate_or_kill(proc, timeout_s=settings.code_git_fetch_timeout_s)
    if proc.returncode != 0:
        if not await _has_commit(checkout, base):
            raise BaseUnknown(f"base commit {base[:12]} not in {checkout}")
        raise RuntimeError(f"git diff {base[:12]}..{head[:12]} exit {proc.returncode}")
    return nul_fields(out.decode("utf-8", "replace"))


async def _has_commit(checkout: Path, sha: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(checkout),
        "cat-file",
        "-e",
        f"{sha}^{{commit}}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=auth_env(),
    )
    await communicate_or_kill(proc, timeout_s=settings.code_git_timeout_s)
    return proc.returncode == 0


async def _modules_failing(repo: CodeRepo, cfg: RepoConfig) -> bool:
    """The tier's record holds a module that would not build (kind `module`, classified
    from the whole error): a pass that builds nothing leaves it as it is."""
    from server.code.domain.index import tier_health

    raw = await repo.get_state(cfg.org, cfg.namespace, tier_health.state_key(PRODUCER))
    record = tier_health.parse(PRODUCER, raw or "")
    return record is not None and record.failing and record.kind == "module"


async def _run_repo(repo: CodeRepo, cfg: RepoConfig, result: CodeScipGoDeltaResult) -> bool:
    """False when it built nothing while a module failure is recorded (`run_tier` then
    keeps the record); True otherwise."""
    target = await compiler_target(repo, cfg)
    if not target:
        result.skipped.append(f"{cfg.namespace}: git_head not seeded")
        return False
    scip_commit = await repo.get_state(cfg.org, cfg.namespace, WATERMARK) or ""
    if target == scip_commit:
        return not await _modules_failing(repo, cfg)  # compiler tier is current
    checkout, bare, missing = await asyncio.to_thread(_resolve_paths, cfg)
    if missing:
        result.skipped.append(f"{cfg.namespace}: {missing}")
        return False
    await git(checkout, *checkout_fetch_args(bare, target), local_remote=bare)
    await git(checkout, "checkout", "--force", target)

    if scip_commit:
        changed = await _changed_paths(checkout, scip_commit, target)
        modules = modules_for_window(changed, await _module_dirs(checkout))
        if not modules:
            logger.info("code_scip_go_delta: %s no Go change in window", cfg.namespace)
            await repo.set_state(cfg.org, cfg.namespace, WATERMARK, target)
            return not await _modules_failing(repo, cfg)
    else:
        modules = []  # first run: the runner walks every go.mod

    with tempfile.TemporaryDirectory(prefix="scip_go_delta_") as tmp:
        out_dir = Path(tmp)
        log = await _run_harness(checkout, scip_commit or "--full", target, out_dir, modules, cfg)
        failed = module_failures(log)
        result.modules_skipped.extend(f"{cfg.namespace}: {line}" for line in failed)
        from server.code.app.index.scip_delta import apply_delta, parse_delta

        # One delta per module the harness ran over (a full run: every go.mod it lists)
        expected = modules or await _module_dirs(checkout)
        paths = await asyncio.to_thread(_deltas, out_dir, len(expected))
        for path in paths:
            raw = await asyncio.to_thread(read_delta, path)
            delta = parse_delta(raw)
            del raw  # the bytes are not needed past the parse
            stats = await apply_delta(repo, cfg.org, cfg.namespace, delta)
            del delta  # free this module's model before the next one is read
            result.modules_indexed += 1
            logger.info(
                "code_scip_go_delta: %s module %s applied %s",
                cfg.namespace,
                path.stem.replace("__", "/"),
                stats.model_dump(),
            )
    await repo.set_state(cfg.org, cfg.namespace, WATERMARK, target)
    result.deltas_applied += 1
    logger.info(
        "code_scip_go_delta: %s %s..%s: %d module deltas",
        cfg.namespace,
        (scip_commit or "full")[:8],
        target[:8],
        len(paths),
    )
    if failed:  # the rest is applied; the modules that would not build fail the tier
        raise TierDegraded("; ".join(failed))
    return True


def _bin_dirs() -> list[str]:
    scip_go = Path(settings.code_scip_go_path)
    return [
        d
        for d in (
            settings.code_scip_go_bin_dir,
            str(scip_go.parent) if scip_go.is_absolute() else "",
        )
        if d
    ]


def _cache_dirs(cfg: RepoConfig) -> tuple[Path, Path]:
    """(GOMODCACHE, GOPATH) of this entry alone. A shared cache would let one
    repository's job plant module sources another's reuses: go does not re-check
    the sums of what is already in its cache."""
    entry = Path(settings.code_scip_go_modcache).absolute() / cfg.org / cfg.namespace
    return entry / "mod", entry / "gopath"


def _make_cache_dirs(cfg: RepoConfig) -> list[Path]:
    """The cache directories, created so the sandbox can mount them writable."""
    dirs = list(_cache_dirs(cfg))
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def _harness_env(out_dir: Path, modules: list[str], cfg: RepoConfig) -> dict[str, str]:
    """The runner's environment (sync: resolves the binaries' paths). It carries
    no credential: `go mod download` runs the repository's module graph, so a
    private dependency the entry's token could read is not fetched."""
    modcache, gopath = _cache_dirs(cfg)
    env = {
        **git_config_env(),
        "PATH": ":".join([*_bin_dirs(), os.environ.get("PATH", os.defpath)]),
        "PYTHON": sys.executable,  # converter needs the server's venv (protobuf runtime)
        "DELTA_OUT_DIR": str(out_dir),
        "SCIP_GO": str(Path(settings.code_scip_go_path)),
        "JOBS": str(settings.code_scip_go_jobs),
        "MIN_GO_FILES": "1",
        "EXCLUDE_GENERATED": "1" if cfg.exclude_generated else "0",
        "GOFLAGS": "-mod=mod",
        "GOMODCACHE": str(modcache),
        "GOPATH": str(gopath),
        "HOME": str(Path.home()),
    }
    if settings.code_scip_go_private:
        env["GOPRIVATE"] = settings.code_scip_go_private
        env["GONOSUMDB"] = settings.code_scip_go_private
    if modules:
        env["MODULES"] = "\n".join(modules)
    return env


def _deltas(out_dir: Path, most: int) -> list[Path]:
    """The `*.json` deltas the harness wrote, refused past `most`: the listing stops at
    the first delta too many."""
    found: list[Path] = []
    with os.scandir(out_dir) as entries:
        for entry in entries:
            if entry.name.endswith(".json"):  # the root module's is `.json`
                found.append(Path(entry.path))
                if len(found) > most:
                    raise RuntimeError(f"Go harness wrote more deltas than its {most} module(s)")
    return sorted(found)


async def _run_harness(
    checkout: Path,
    base: str,
    head: str,
    out_dir: Path,
    modules: list[str],
    cfg: RepoConfig,
) -> str:
    """`run_go_monorepo_delta.sh` in DELTA_OUT_DIR mode over `modules` (every
    module when empty): index, convert, write one JSON delta per module — no
    upload, no watermark read (the worker owns both). Returns the log. Module
    downloads go through the sandbox's egress proxy."""
    env = await asyncio.to_thread(_harness_env, out_dir, modules, cfg)
    caches = await asyncio.to_thread(_make_cache_dirs, cfg)
    returncode, text = await run_harness(
        _HARNESS,
        checkout,
        [base, head],
        env,
        out_dir=out_dir,
        timeout_s=settings.code_scip_go_delta_timeout_seconds,
        writable=caches,
        readonly=[mount for d in _bin_dirs() for mount in tool_mounts(Path(d))],
        network=True,
    )
    if returncode != 0:
        # failed outright: a module it skipped earlier is not the cause
        raise harness_failure(returncode, without_module_failures(text)[-2000:])
    return text


__all__ = ["PRODUCER", "WATERMARK", "CodeScipGoDeltaResult", "modules_for_window", "run_pass"]
