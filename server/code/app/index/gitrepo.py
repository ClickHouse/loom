"""Async git plumbing against a bare clone (the ingest worker's data source).

All operations shell out to `git` with asyncio subprocesses — no GitPython
dependency, no working tree. The bare clone is created by `loom code init`
and fetched by the code_ingest worker each pass. Every helper is read-only
except fetch().
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fnmatch
import logging
import os
import re
import signal
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from server.settings import settings

logger = logging.getLogger("loom.code_index.git")

_SHA40 = re.compile(r"[0-9a-f]{40}")


class GitError(RuntimeError):
    pass


# Git C-quotes a path holding non-ASCII bytes ("src/caf\303\251.py") unless told
# otherwise, and a quoted name reads back as a missing file. Every command names
# paths verbatim; the path listings below also read NUL-separated records, so a
# tab or newline in a name survives.
UNQUOTED_PATHS = ("-c", "core.quotePath=false")


def nul_fields(out: str) -> list[str]:
    """The records of a `-z` listing."""
    return [f for f in out.split("\0") if f]


# An object id, full or abbreviated (git accepts 4+ hex digits): what the range
# helpers take. Their inputs are stored watermarks and a caller's `commit`.
_OBJECT_ID = re.compile(r"[0-9a-fA-F]{4,64}")


def _object_id(value: str) -> str:
    """`value` if it is an object id, else GitError before git runs."""
    if not _OBJECT_ID.fullmatch(value or ""):
        raise GitError(f"not an object id: {str(value)[:80]!r}")
    return value


def _is_rev(value: str) -> bool:
    """Whether git can take `value` as a revision argument and not as an
    option: a value starting with `-` (`--output=<path>` writes a file) is not."""
    return bool(value) and not value.startswith("-")


def _rev(value: str) -> str:
    """`value` if `_is_rev`, else GitError before git runs."""
    if not _is_rev(value):
        raise GitError(f"not a revision: {str(value)[:80]!r}")
    return value


def add_git_config(env: dict[str, str], key: str, value: str) -> None:
    """Append one `-c key=value` to `env` through GIT_CONFIG_COUNT / _KEY_n / _VALUE_n."""
    n = int(env.get("GIT_CONFIG_COUNT", "0"))
    env[f"GIT_CONFIG_KEY_{n}"] = key
    env[f"GIT_CONFIG_VALUE_{n}"] = value
    env["GIT_CONFIG_COUNT"] = str(n + 1)


def is_local_remote(remote: str) -> bool:
    """A remote that is a path on this host (an operator's local mirror). As git
    reads it: no `scheme://` and no `host:` before the first slash (`mirror.git`
    and `dir/repo` are relative paths, `git@host:a/b` is ssh)."""
    if remote.startswith("file://"):
        return True
    return bool(remote) and "://" not in remote and ":" not in remote.split("/", 1)[0]


def auth_env(token: str = "", remote: str = "https://github.com/") -> dict[str, str]:
    """The environment every repository-job git runs with. It inherits nothing
    from the server but PATH, HOME, LANG and TMPDIR; no system or global git
    config is read; only the https and ssh transports are allowed (plus local
    paths when `remote` itself is one), so `ext::`, `file://` and the rest are
    refused for submodules and redirects too. GIT_TERMINAL_PROMPT=0 makes a
    missing credential a failure, not a hang.

    `token` rides an `http.<url>.extraheader` keyed to the remote's owner
    (`https://host/owner/`), so it appears in no argv, no remote URL, no stored
    config and no `code_index_state.repo_remote`, and git sends it to that
    owner's repositories only: a submodule URL elsewhere on the host gets none.
    Git matches the prefix byte for byte, so an owner spelled in another case
    gets none either."""
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    # The system config this replaces is where the image trusted mounted repos
    # owned by another uid; inside the sandbox every file has a foreign owner.
    add_git_config(env, "safe.directory", "*")
    add_git_config(env, "protocol.allow", "never")
    add_git_config(env, "protocol.https.allow", "always")
    add_git_config(env, "protocol.ssh.allow", "always")
    if is_local_remote(remote):
        add_git_config(env, "protocol.file.allow", "always")
    if not token or not remote.startswith(("https://", "http://")):
        return env
    parts = remote.split("/")
    host = parts[2]
    owner = parts[3] if len(parts) > 4 else ""
    username = {
        "github.com": "x-access-token",
        "gitlab.com": "oauth2",
        "bitbucket.org": "x-token-auth",
    }.get(host.split(":", 1)[0].lower(), "oauth2")
    basic = base64.b64encode(f"{username}:{token}".encode()).decode()
    scope = f"https://{host}/{owner}/" if owner else f"https://{host}/"
    add_git_config(env, f"http.{scope}.extraheader", f"AUTHORIZATION: basic {basic}")
    return env


def git_config_env() -> dict[str, str]:
    """`auth_env`'s git settings alone (no token), for a harness environment
    built from scratch: the git a build or `go mod download` runs gets them too."""
    return {k: v for k, v in auth_env().items() if k.startswith("GIT_")}


class RemoteAccess(TypedDict, total=False):
    """The `sandbox.JobOptions` a job talking to a remote needs."""

    readonly: list[Path]
    network: bool
    allow_remote_port: int | None


def remote_access(remote: str) -> RemoteAccess:
    """`sandbox.run_job` arguments for a job talking to `remote`: a local mirror is
    mounted read-only, any other remote is reached through the egress proxy."""
    if is_local_remote(remote):
        return {"readonly": [Path(remote.removeprefix("file://")).absolute()]}
    return {"network": True, "allow_remote_port": remote_port(remote)}


# A local fetch, ls-remote or clone starts upload-pack in the source repository with
# the environment's config cleared, so auth_env's safe.directory does not reach it
# and a repository another uid owns (one the host operator made) is refused as
# dubious. It gets the setting on its own command line instead.
_LOCAL_UPLOAD_PACK = "git -c 'safe.directory=*' upload-pack"


def upload_pack_args(remote: str) -> list[str]:
    """The `--upload-pack` a fetch, ls-remote or clone from `remote` needs."""
    return [f"--upload-pack={_LOCAL_UPLOAD_PACK}"] if is_local_remote(remote) else []


def remote_port(remote: str) -> int | None:
    """The port a network remote is reached on: an explicit `:port`, else 443
    for https and 22 for ssh. None for a local path."""
    if is_local_remote(remote):
        return None
    if "://" not in remote:
        return 22  # scp-style user@host:path
    scheme, rest = remote.split("://", 1)
    authority = rest.split("/", 1)[0].rsplit("@", 1)[-1]
    if authority.rsplit(":", 1)[-1].isdigit() and not authority.endswith("]"):
        return int(authority.rsplit(":", 1)[-1])
    return 22 if scheme == "ssh" else 443


async def communicate_or_kill(
    proc: asyncio.subprocess.Process,
    stdin: bytes | None = None,
    *,
    timeout_s: float,
    group: bool = False,
) -> tuple[bytes, bytes]:
    """`proc.communicate(stdin)` bounded by `timeout_s` (TimeoutError past it). On
    any exit that is not a normal finish — the timeout, the caller's cancellation (a
    `code.refresh` whose wait gave up, a dropped fan-out), any error — the child is
    killed and reaped, so no git process outlives the call that started it. With
    `group` (a child started in its own session) its whole process group is killed."""
    talk = proc.communicate(stdin) if stdin is not None else proc.communicate()
    try:
        return await asyncio.wait_for(talk, timeout=timeout_s)
    except BaseException:
        if group:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
        await _kill(proc)
        raise


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Kill a child that is still running and wait for it, so it leaves no zombie."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()


def _redact(text: str, token: str) -> str:
    if token and token in text:
        text = text.replace(token, "<token>")
    return text


async def _git(
    git_dir: str,
    *args: str,
    timeout_s: float | None = None,
    remote: str = "",
    token: str = "",
    stdin: bytes | None = None,
    index_file: str = "",
) -> str:
    """A git command on the bare clone. With `remote` it talks to that remote, so it
    runs as a repository job (sandbox.run_job): the network only through the egress
    proxy, the clone the one writable path, `token` for the remote's host. A local
    command reads and writes `index_file` as its index when given. `timeout_s`
    defaults to `code_git_timeout_s`."""
    if timeout_s is None:
        timeout_s = settings.code_git_timeout_s
    extra = upload_pack_args(remote)
    argv = ["git", *UNQUOTED_PATHS, "--git-dir", git_dir, args[0], *extra, *args[1:]]
    label = " ".join(args[:2])
    returncode: int | None
    if remote:
        returncode, out, err = await _network_git(
            argv, label, git_dir, remote, token, stdin, timeout_s
        )
    else:
        env = auth_env()
        if index_file:
            env["GIT_INDEX_FILE"] = index_file
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out, err = await communicate_or_kill(proc, stdin, timeout_s=timeout_s)
        except TimeoutError:
            raise GitError(f"git {label} timed out after {timeout_s}s") from None
        returncode = proc.returncode
    if returncode != 0:
        raise GitError(
            f"git {' '.join(args[:3])} rc={returncode}: "
            f"{_redact(err.decode('utf-8', 'replace'), token)[:500]}"
        )
    return out.decode("utf-8", "replace")


async def _network_git(
    argv: list[str],
    label: str,
    git_dir: str,
    remote: str,
    token: str,
    stdin: bytes | None,
    timeout_s: float,
) -> tuple[int, bytes, bytes]:
    from server.code.app.index.sandbox import run_job

    try:
        done = await run_job(
            argv,
            env=auth_env(token, remote),
            timeout_s=timeout_s,
            writable=[Path(git_dir)],
            stdin=stdin,
            keep_stdout=True,
            **remote_access(remote),
        )
    except TimeoutError:
        raise GitError(f"git {label} timed out after {timeout_s}s") from None
    return done.returncode, done.stdout, done.stderr


async def run(git_dir: str, *args: str, timeout_s: float | None = None) -> str:
    """A read-only git command's stdout (blame_range's blame, rev-list and ref reads).
    Same runner, timeout and error shape as the module's own readers; no token — the
    bare mirror is local."""
    return await _git(git_dir, *args, timeout_s=timeout_s)


async def run_bounded(
    git_dir: str,
    *args: str,
    max_bytes: int,
    timeout_s: float | None = None,
    check: bool = False,
) -> tuple[str, bool]:
    """A read-only git command whose stdout is read up to `max_bytes` and then cut:
    (text, truncated). A diff of a huge merge must not hold the request or the process's
    memory hostage. A non-zero exit with nothing read is a GitError; with `check`, so is
    one after output (what was read may be partial). A cut stream is not."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *UNQUOTED_PATHS,
        "--git-dir",
        git_dir,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=auth_env(),
    )
    assert proc.stdout is not None
    chunks: list[bytes] = []
    got = 0
    truncated = False
    try:
        async with asyncio.timeout(settings.code_git_timeout_s if timeout_s is None else timeout_s):
            while True:
                piece = await proc.stdout.read(65536)
                if not piece:
                    break
                chunks.append(piece)
                got += len(piece)
                if got > max_bytes:
                    truncated = True
                    break
    except TimeoutError:
        await _kill(proc)
        raise GitError(f"git {args[0] if args else ''} timed out") from None
    except BaseException:
        await _kill(proc)
        raise
    if truncated:
        proc.kill()
    err = b""
    try:
        _out, err = await communicate_or_kill(proc, timeout_s=10.0)
    except TimeoutError:
        pass  # killed and reaped; what was read stands
    if not truncated and proc.returncode not in (0, None) and (got == 0 or check):
        msg = err.decode("utf-8", "replace").strip()[:500]
        raise GitError(msg or f"git exited {proc.returncode}")
    text = b"".join(chunks)[:max_bytes].decode("utf-8", "replace")
    return text, truncated


async def grep_paths(
    git_dir: str,
    rev: str,
    literal: str,
    pathspecs: tuple[str, ...],
    *,
    limit: int = 50,
    timeout_s: float = 20.0,
) -> list[str]:
    """Paths at `rev` whose content contains `literal` (fixed string, case sensitive),
    under `pathspecs` — `git grep -l -F`. Exit 1 with nothing on stderr is git's "no
    match", not an error."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "--git-dir",
        git_dir,
        "grep",
        "-l",
        "-F",
        "-e",
        literal,
        _rev(rev),
        "--",
        *pathspecs,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=auth_env(),
    )
    try:
        out, err = await communicate_or_kill(proc, timeout_s=timeout_s)
    except TimeoutError:
        raise GitError(f"git grep {literal[:40]} timed out") from None
    if proc.returncode == 1 and not err.strip():
        return []
    if proc.returncode != 0:
        raise GitError(f"git grep rc={proc.returncode}: {err.decode('utf-8', 'replace')[:300]}")
    paths: list[str] = []
    for ln in out.decode("utf-8", "replace").splitlines():
        # `<rev>:<path>`
        paths.append(ln.split(":", 1)[1] if ":" in ln else ln)
        if len(paths) >= limit:
            break
    return paths


async def is_ancestor(
    git_dir: str, ancestor: str, descendant: str, *, timeout_s: float = 20.0
) -> bool | None:
    """Whether `ancestor` is reachable from `descendant` — `git merge-base
    --is-ancestor` (the descendant check behind code.first_build). Exit 0 is yes, 1 is
    no; anything else (a commit the mirror does not hold, a value that is not a
    revision) is None, never a guess."""
    if not (_is_rev(ancestor) and _is_rev(descendant)):
        return None
    proc = await asyncio.create_subprocess_exec(
        "git",
        "--git-dir",
        git_dir,
        "merge-base",
        "--is-ancestor",
        ancestor,
        descendant,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=auth_env(),
    )
    try:
        await communicate_or_kill(proc, timeout_s=timeout_s)
    except TimeoutError:
        raise GitError("git merge-base --is-ancestor timed out") from None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


async def has_commit(git_dir: str, sha: str) -> bool:
    """Whether the mirror holds `sha` (an old commit is gone after a history rewrite and
    a re-clone). A mirror git cannot read raises GitError: that is not a lost commit."""
    return bool(_SHA40.fullmatch(sha.strip().lower())) and not await missing_commits(git_dir, [sha])


async def release_containing(git_dir: str, sha: str, *, timeout_s: float = 3.0) -> str:
    """The first release line (`X.Y`) whose stable or LTS tags contain `sha`, or '' when
    no release tag does yet. ~21 ms on a mirror with 1,379 tags and a commit-graph."""
    out = await run(
        git_dir,
        "tag",
        "--contains",
        _rev(sha),
        "--list",
        "v*-stable",
        "--list",
        "v*-lts",
        timeout_s=timeout_s,
    )
    lines: list[tuple[int, int]] = []
    for tag in out.split():
        m = re.match(r"v(\d+)\.(\d+)\.", tag)
        if m:
            lines.append((int(m.group(1)), int(m.group(2))))
    if not lines:
        return ""
    major, minor = min(lines)
    return f"{major}.{minor}"


async def branches_containing(git_dir: str, sha: str, *, timeout_s: float = 20.0) -> list[str]:
    """The mirror's branch heads that contain `sha` (`git branch --contains`),
    short names; a sha the mirror does not hold is a GitError."""
    out = await _git(
        git_dir,
        "branch",
        "--contains",
        _rev(sha),
        "--format=%(refname:short)",
        timeout_s=timeout_s,
    )
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


async def fetch(git_dir: str, remote: str, branch: str, *, token: str = "") -> None:
    """Fetch one branch into the bare clone. `token` (the entry's github_token)
    authenticates a private remote through the environment, never the URL."""
    await _git(
        git_dir,
        "fetch",
        "--prune",
        remote,
        f"+refs/heads/{branch}:refs/heads/{branch}",
        timeout_s=settings.code_git_fetch_timeout_s,
        remote=remote,
        token=token,
    )


async def fetch_many(git_dir: str, remote: str, branches: list[str], *, token: str = "") -> None:
    """Fetch several configured branches of one mirror in ONE negotiation, rather than
    each configured entry running its own `fetch` of its own branch every pass. Same
    refspecs and `--prune` as `fetch`, one per branch."""
    wanted = sorted({b for b in branches if b})
    if not wanted:
        return
    await _git(
        git_dir,
        "fetch",
        "--prune",
        remote,
        *(f"+refs/heads/{b}:refs/heads/{b}" for b in wanted),
        timeout_s=settings.code_git_fetch_timeout_s,
        remote=remote,
        token=token,
    )


# ---- every branch the ledger's merges land on ------------------------------
# The configured branch is one of many a PR merges into: the release branches (`25.8`,
# `26.3`, …) and the backport bot's staging branches (`backport/26.3/118740`, the base
# of a `Cherry pick #N to 26.3` PR). A merge commit on a branch the mirror does not
# fetch is unknown to every reader that diffs it (most of them on `backport/*`). The
# globs below name which remote branches the mirror carries
# besides the configured ones; the refs are never pruned, so a branch upstream deletes
# keeps its merges reachable here.


def branch_globs(raw: str) -> tuple[str, ...]:
    """The comma-separated glob list of `code_git_extra_branches`."""
    return tuple(g.strip() for g in (raw or "").split(",") if g.strip())


def match_branches(names: list[str], globs: tuple[str, ...]) -> list[str]:
    """The branch names (short, no `refs/heads/`) any glob matches — fnmatch
    with `[..]` classes and `*` across `/`, case-sensitive; sorted, unique."""
    if not globs:
        return []
    out = {n for n in names if n and any(fnmatch.fnmatchcase(n, g) for g in globs)}
    return sorted(out)


def parse_ls_remote_heads(text: str) -> dict[str, str]:
    """Short branch name -> tip sha from `git ls-remote --heads` output."""
    tips: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            tips[parts[1][len("refs/heads/") :].strip()] = parts[0].strip()
    return tips


async def remote_branches(git_dir: str, remote: str, *, token: str = "") -> dict[str, str]:
    """Every branch the remote advertises with its tip (one `ls-remote
    --heads`; ~1 MB for a repository with 16k heads, 0.3 s)."""
    out = await _git(
        git_dir,
        "ls-remote",
        "--heads",
        remote,
        timeout_s=settings.code_git_timeout_s,
        remote=remote,
        token=token,
    )
    return parse_ls_remote_heads(out)


async def local_branch_shas(git_dir: str) -> dict[str, str]:
    """Short name -> tip sha of the clone's `refs/heads/*`."""
    out = await _git(
        git_dir, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/"
    )
    tips: dict[str, str] = {}
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) == 2:
            tips[parts[0]] = parts[1]
    return tips


def branches_to_fetch(
    remote: dict[str, str], local: dict[str, str], globs: tuple[str, ...], *, skip: str = ""
) -> list[str]:
    """The glob-matched remote branches whose tip the clone does not hold
    under the same name — on a warm mirror a handful, so the per-pass fetch
    negotiates a few refs, not 6.5k (22 s measured for the full list)."""
    names = match_branches(list(remote), globs)
    return [b for b in names if b != skip and local.get(b) != remote[b]]


async def fetch_branches(git_dir: str, remote: str, branches: list[str], *, token: str = "") -> int:
    """Fetch `branches` into the bare clone under their own names, in one
    `git fetch --stdin` (thousands of refspecs never touch argv), WITHOUT
    `--prune`: a staging branch the bot deletes keeps its merge commit
    reachable. Returns the number of refspecs sent; no-op on an empty list."""
    names = [b for b in dict.fromkeys(branches) if b and not b.startswith("-")]
    if not names:
        return 0
    refspecs = "".join(f"+refs/heads/{b}:refs/heads/{b}\n" for b in names).encode()
    await _git(
        git_dir,
        "fetch",
        "--stdin",
        remote,
        timeout_s=settings.code_git_fetch_timeout_s,
        remote=remote,
        token=token,
        stdin=refspecs,
    )
    return len(names)


MERGE_REF_PREFIX = "refs/loom/merges/"


async def fetch_shas(git_dir: str, remote: str, shas: list[str], *, token: str = "") -> int:
    """Fetch commits by sha and pin each under `refs/loom/merges/<sha>`. The backport
    bot's cherry-pick step merges into a staging branch it then rebases, so the merge
    commit the ledger recorded is reachable from no branch upstream; GitHub still serves
    it by sha (40 in 8.5 s measured), and the pin keeps `git gc` from dropping it.
    Returns the number of shas requested; 0 on an empty list."""
    wanted = [x for x in dict.fromkeys(s.strip().lower() for s in shas) if _SHA40.fullmatch(x)]
    if not wanted:
        return 0
    refspecs = "".join(f"{x}:{MERGE_REF_PREFIX}{x}\n" for x in wanted).encode()
    await _git(
        git_dir,
        "fetch",
        "--stdin",
        remote,
        timeout_s=settings.code_git_fetch_timeout_s,
        remote=remote,
        token=token,
        stdin=refspecs,
    )
    return len(wanted)


# ---- the heads of open pull requests ---------------------------------------
# GitHub publishes every pull request's head commit under `refs/pull/N/head`. Without
# it, a reader given an open PR (its head sha, its diff against the branch) falls back
# to the merge commit or answers `unknown`. Fetching ALL of them is not an option: a
# large repository has tens of thousands of such refs — many minutes and gigabytes of
# fetched packs, then every one of those refs to negotiate on every fetch. The readers
# want only the open PRs' heads. The mirror carries the heads of the PRs the ledger
# (`code_gh_items`) holds
# OPEN, refetched when upstream's tip moved, never pruned — a closed PR's head stays
# until `gc`.

PULL_HEAD_PREFIX = "refs/pull/"
_PULL_HEAD_RE = re.compile(r"^refs/pull/(\d+)/head$")


def pull_head_ref(pr_number: int) -> str:
    return f"{PULL_HEAD_PREFIX}{int(pr_number)}/head"


def parse_ls_remote_pull_heads(text: str) -> dict[int, str]:
    """`git ls-remote <remote> 'refs/pull/*/head'` → PR number → tip sha."""
    tips: dict[int, str] = {}
    for ln in text.splitlines():
        parts = ln.split("\t")
        if len(parts) != 2:
            continue
        m = _PULL_HEAD_RE.match(parts[1].strip())
        if m:
            tips[int(m.group(1))] = parts[0].strip()
    return tips


async def remote_pull_heads(git_dir: str, remote: str, *, token: str = "") -> dict[int, str]:
    """Every PR head upstream: PR number → tip sha. One `ls-remote` (5.5 MB / 87,904
    refs in ~1 s on ClickHouse/ClickHouse)."""
    out = await _git(
        git_dir,
        "ls-remote",
        remote,
        f"{PULL_HEAD_PREFIX}*/head",
        timeout_s=60.0,
        remote=remote,
        token=token,
    )
    return parse_ls_remote_pull_heads(out)


async def local_pull_heads(git_dir: str) -> dict[int, str]:
    """PR number → tip sha of the clone's `refs/pull/*/head`."""
    out = await _git(git_dir, "for-each-ref", "--format=%(objectname) %(refname)", PULL_HEAD_PREFIX)
    tips: dict[int, str] = {}
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) == 2:
            m = _PULL_HEAD_RE.match(parts[1])
            if m:
                tips[int(m.group(1))] = parts[0]
    return tips


def pull_heads_to_fetch(
    open_prs: list[int], remote: dict[int, str], local: dict[int, str], *, limit: int
) -> list[int]:
    """The open PRs whose upstream head the clone does not hold under
    `refs/pull/N/head`, in the caller's order (newest activity first), at
    most `limit`. A PR upstream no longer lists is skipped (closed since the
    ledger row); one whose tip the clone already has is skipped."""
    out: list[int] = []
    for n in dict.fromkeys(int(x) for x in open_prs if int(x) > 0):
        tip = remote.get(n)
        if tip and local.get(n) != tip:
            out.append(n)
            if len(out) >= max(1, limit):
                break
    return out


async def fetch_pull_heads(
    git_dir: str, remote: str, numbers: list[int], *, token: str = ""
) -> int:
    """Fetch `refs/pull/N/head` for each PR number into the clone under the
    same ref, one `git fetch --stdin`, no `--prune`. Returns the number of
    refspecs sent; 0 on an empty list."""
    wanted = [n for n in dict.fromkeys(int(x) for x in numbers) if n > 0]
    if not wanted:
        return 0
    refspecs = "".join(f"+{pull_head_ref(n)}:{pull_head_ref(n)}\n" for n in wanted).encode()
    await _git(
        git_dir,
        "fetch",
        "--stdin",
        remote,
        timeout_s=settings.code_git_fetch_timeout_s,
        remote=remote,
        token=token,
        stdin=refspecs,
    )
    return len(wanted)


async def pull_head_sha(git_dir: str, pr_number: int) -> str:
    """The clone's `refs/pull/N/head` tip, '' when the mirror lacks it."""
    try:
        out = await _git(git_dir, "rev-parse", "--verify", "-q", pull_head_ref(pr_number))
        return out.strip()
    except GitError:
        return ""


async def merge_base(git_dir: str, a: str, b: str) -> str:
    """`git merge-base a b`, '' when unrelated or unknown."""
    try:
        return (await _git(git_dir, "merge-base", _rev(a), _rev(b))).strip()
    except GitError:
        return ""


async def diff_name_only(
    git_dir: str, base_sha: str, head_sha: str, *, limit: int = 2000
) -> list[str]:
    """Paths the range changes (added, modified, deleted; renames as D+A)."""
    out = await _git(
        git_dir,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        "--end-of-options",
        _rev(base_sha),
        _rev(head_sha),
    )
    return nul_fields(out)[:limit]


async def missing_commits(git_dir: str, shas: list[str]) -> list[str]:
    """The shas among `shas` the clone does not hold — one `cat-file
    --batch-check`, read-only, in input order."""
    wanted = [x for x in dict.fromkeys(s.strip().lower() for s in shas) if _SHA40.fullmatch(x)]
    if not wanted:
        return []
    payload = ("\n".join(wanted) + "\n").encode()
    out = await _git(git_dir, "cat-file", "--batch-check", stdin=payload)
    lines = out.splitlines()
    return [x for i, x in enumerate(wanted) if i >= len(lines) or " missing" in lines[i]]


async def head_sha(git_dir: str, branch: str) -> str:
    return (await _git(git_dir, "rev-parse", f"refs/heads/{branch}")).strip()


async def resolve_commit(git_dir: str, rev: str) -> str:
    """Full sha of `rev` (a sha, abbreviation or ref), or ValueError when the clone does
    not hold it. Read-only: a pinned backfill resolves the commit it was told to index
    instead of the branch tip, so two arms indexed hours apart see one tree even though
    the shared mirror is fetched by the worker in between."""
    try:
        out = (await _git(git_dir, "rev-parse", "--verify", f"{_rev(rev)}^{{commit}}")).strip()
    except Exception as e:
        raise ValueError(f"{rev!r} is not a commit in {git_dir}: {e}") from e
    if not re.fullmatch(r"[0-9a-f]{40}", out):
        raise ValueError(f"{rev!r} did not resolve to a commit sha in {git_dir}: {out!r}")
    return out


async def commits_between(git_dir: str, since_sha: str, until_sha: str, limit: int) -> list[str]:
    """First-parent commit shas after since_sha up to until_sha, OLDEST
    first, capped at limit. Empty since_sha means 'just the tip'. Both are
    object ids (GitError otherwise): they come from stored watermarks."""
    if not since_sha:
        return [until_sha]
    _object_id(since_sha)
    _object_id(until_sha)
    out = await _git(
        git_dir,
        "rev-list",
        "--first-parent",
        "--reverse",
        f"{since_sha}..{until_sha}",
    )
    shas = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return shas[:limit]


async def commit_count_between(git_dir: str, since_sha: str, until_sha: str) -> int:
    """Number of first-parent commits after since_sha up to until_sha — the
    "how far behind is this watermark" gauge. Uncapped (rev-list --count),
    so it is safe for lag display where commits_between's limit would lie.
    Empty since_sha means everything is pending: returns -1 (unknown). Both
    are object ids (GitError otherwise): a stored watermark or a caller's
    commit never reaches git as anything else."""
    if not since_sha or not until_sha:
        return -1
    _object_id(since_sha)
    _object_id(until_sha)
    if since_sha == until_sha:
        return 0
    out = await _git(
        git_dir,
        "rev-list",
        "--count",
        "--first-parent",
        f"{since_sha}..{until_sha}",
    )
    return int(out.strip() or "0")


@dataclass(frozen=True)
class DiffEntry:
    status: str  # A | M | D | T (a rename or copy arrives as D+A or A)
    path: str


async def diff_files(git_dir: str, base_sha: str, head_sha: str) -> list[DiffEntry]:
    out = await _git(
        git_dir,
        "diff",
        "--name-status",
        "-z",
        "--no-renames",
        "--end-of-options",
        _rev(base_sha),
        _rev(head_sha),
    )
    return parse_name_status_z(out)


def name_status_z(out: str) -> list[tuple[str, str, str]]:
    """(status letter, old path, new path) of each `diff --name-status -z` record;
    the two paths are equal unless the record is a rename or copy."""
    fields = out.split("\0")
    records: list[tuple[str, str, str]] = []
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i][:1]
        if status in ("R", "C"):
            records.append((status, fields[i + 1], fields[i + 2]))
            i += 3
        else:
            records.append((status, fields[i + 1], fields[i + 1]))
            i += 2
    return records


def parse_name_status_z(out: str) -> list[DiffEntry]:
    """`diff --name-status -z` records as entries: a rename is its old path deleted
    and its new one added; a copy only adds the new one."""
    entries: list[DiffEntry] = []
    for status, old, new in name_status_z(out):
        if status == "R":
            entries.append(DiffEntry(status="D", path=old))
        entries.append(DiffEntry(status="A" if status in ("R", "C") else status, path=new))
    return entries


# files on either side of a range past which renames with edits are not looked for
_RENAME_LIMIT = 2000


async def diff_paths_renamed(git_dir: str, base_sha: str, head_sha: str) -> dict[str, str]:
    """old path -> path at `head_sha` for every path the range changed: itself when
    added or modified, the new path when renamed (`-M`), '' when deleted. Past
    `_RENAME_LIMIT` files on a side git looks for exact renames only, so a range that
    moves a whole tree stays bounded."""
    out = await _git(
        git_dir,
        "diff",
        "--name-status",
        "-z",
        "-M",
        f"-l{_RENAME_LIMIT}",
        "--end-of-options",
        _rev(base_sha),
        _rev(head_sha),
    )
    return {old: "" if status == "D" else new for status, old, new in name_status_z(out)}


async def edits_of(
    git_dir: str, base_sha: str, head_sha: str, path: str, pattern: str | None
) -> list[tuple[str, str]]:
    """(commit, its first parent) of each first-parent commit of `base_sha..head_sha`
    that changed a line of `path` matching `pattern` (`git log -G`; every commit that
    touched `path` without one), oldest first."""
    lines = ["-G", pattern] if pattern else []
    out = await _git(
        git_dir,
        "--literal-pathspecs",
        "log",
        "--first-parent",
        "--reverse",
        "--format=%H %P",
        *lines,
        "--end-of-options",
        f"{_rev(base_sha)}..{_rev(head_sha)}",
        "--",
        path,
    )
    return [(f[0], f[1]) for f in (line.split() for line in out.splitlines()) if len(f) > 1]


async def renamed_from(
    git_dir: str, base_sha: str, head_sha: str, paths: list[str]
) -> dict[str, str]:
    """path at `head_sha` -> its path at `base_sha` for those of `paths` the range
    renamed. Renames are sought only from the files the range deleted to `paths`, so
    a long range stays cheap and needs no rename limit."""
    raw = await _git(
        git_dir,
        "diff",
        "--raw",
        "--no-abbrev",
        "-z",
        "--no-renames",
        "--diff-filter=A",
        "--end-of-options",
        _rev(base_sha),
        _rev(head_sha),
    )
    fields = raw.split("\0")
    # `:000000 <mode> <null id> <id> A`, path: the null id removes the path from an index
    added = {path: meta.split()[2] for meta, path in zip(fields[::2], fields[1::2], strict=False)}
    wanted = added.keys() & set(paths)
    if not wanted:
        return {}
    # The index is `head_sha` less every other added file: a diff of it against
    # `base_sha` holds the deletions and `wanted` as the only additions.
    drop = "".join(f"0 {null}\t{path}\0" for path, null in added.items() if path not in wanted)
    with tempfile.TemporaryDirectory(prefix="loom-renames-") as tmp:
        index = os.path.join(tmp, "index")
        await _git(git_dir, "read-tree", _rev(head_sha), index_file=index)
        if drop:
            await _git(
                git_dir, "update-index", "-z", "--index-info", stdin=drop.encode(), index_file=index
            )
        out = await _git(
            git_dir,
            "diff",
            "--cached",
            "--name-status",
            "-z",
            "-M",
            "-l0",
            "--end-of-options",
            _rev(base_sha),
            index_file=index,
        )
    return {new: old for status, old, new in name_status_z(out) if status == "R" and new in wanted}


async def commit_subject(git_dir: str, sha: str) -> str:
    return (await _git(git_dir, "log", "-1", "--format=%s", _rev(sha))).strip()


async def commit_time(git_dir: str, sha: str) -> int:
    """Committer time as unix seconds."""
    return int((await _git(git_dir, "log", "-1", "--format=%ct", _rev(sha))).strip())


async def commit_meta_many(git_dir: str, shas: list[str]) -> dict[str, tuple[str, int]]:
    """sha → (subject, committer unix seconds) for every sha the clone holds, in one
    `git log --no-walk` rather than two subprocesses per row (870 ms for a page of
    `code.recent_commits` rows). A sha the clone does not hold is simply absent; nothing
    raises for it."""
    wanted = [s for s in shas if re.fullmatch(r"[0-9a-f]{7,40}", s or "")]
    if not wanted:
        return {}
    try:
        out = await _git(git_dir, "log", "--no-walk=unsorted", "--format=%H%x1f%s%x1f%ct", *wanted)
    except GitError:
        # one unknown sha fails the whole call; fall back to per-sha reads for
        # the ones that resolve
        out_map: dict[str, tuple[str, int]] = {}
        for s in wanted:
            try:
                one = await _git(git_dir, "log", "-1", "--format=%H%x1f%s%x1f%ct", s)
            except GitError:
                continue
            for line in one.splitlines():
                parts = line.split("\x1f")
                if len(parts) == 3 and parts[2].strip().isdigit():
                    out_map[parts[0]] = (parts[1], int(parts[2]))
        return out_map
    meta: dict[str, tuple[str, int]] = {}
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3 and parts[2].strip().isdigit():
            meta[parts[0]] = (parts[1], int(parts[2]))
    # callers hold short or full shas; answer under the spelling they asked with
    for s in wanted:
        if s not in meta:
            full = next((k for k in meta if k.startswith(s)), None)
            if full:
                meta[s] = meta[full]
    return meta


async def show_file(git_dir: str, sha: str, path: str) -> bytes | None:
    """File contents at a commit; None when the path is no file there (absent, a
    directory, a submodule) or `sha` is not a revision."""
    if not _is_rev(sha):
        return None
    proc = await asyncio.create_subprocess_exec(
        "git",
        "--git-dir",
        git_dir,
        "cat-file",
        "blob",
        f"{sha}:{path}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=auth_env(),
    )
    try:
        out, _err = await communicate_or_kill(proc, timeout_s=settings.code_git_timeout_s)
    except TimeoutError:
        raise GitError(f"git cat-file {path} timed out") from None
    if proc.returncode != 0:
        return None
    return out


async def file_at(git_dir: str, sha: str, path: str) -> bytes | None:
    """`path` at `sha`; None only when that commit has no such file (a directory or a
    submodule there is none). A mirror git cannot read, or a commit it does not have,
    raises GitError."""
    data = await show_file(git_dir, sha, path)
    if data is not None:
        return data
    listed = await _git(git_dir, "ls-tree", _rev(sha), "--", path)
    if any(line.split(" ", 2)[1:2] == ["blob"] for line in listed.splitlines()):
        raise GitError(f"git cat-file {path} failed at {sha}")
    return None


def blob_sha(data: bytes) -> str:
    """git's identity for file content: sha1("blob <len>\\0" + bytes). What `git
    ls-tree` prints per path and what `git hash-object` computes — so a row that records
    it can be compared with a tree listing without reading a single blob."""
    import hashlib

    h = hashlib.sha1(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


async def ls_tree_blobs(git_dir: str, sha: str) -> dict[str, str]:
    """path -> blob sha for every blob at a commit: one `git ls-tree -r`,
    metadata only (no blob is read). ~30k paths in well under a second on
    the ClickHouse mirror."""
    out = await _git(
        git_dir,
        "ls-tree",
        "-r",
        "-z",
        "--end-of-options",
        _rev(sha),
        timeout_s=settings.code_git_fetch_timeout_s,
    )
    # `<mode> <type> <object>\t<path>`: the default record, whose path -z leaves
    # verbatim (a --format %(path) is still quoted)
    blobs: dict[str, str] = {}
    for record in nul_fields(out):
        meta, _, path = record.partition("\t")
        fields = meta.split()
        if len(fields) == 3 and path:
            blobs[path] = fields[2]
    return blobs


async def ls_tree(git_dir: str, sha: str, prefixes: tuple[str, ...] = ()) -> list[str]:
    """All blob paths at a commit, optionally filtered to path prefixes."""
    out = await _git(
        git_dir,
        "ls-tree",
        "-r",
        "-z",
        "--name-only",
        "--end-of-options",
        _rev(sha),
        timeout_s=settings.code_git_fetch_timeout_s,
    )
    paths = nul_fields(out)
    if prefixes:
        paths = [p for p in paths if p.startswith(prefixes)]
    return paths
