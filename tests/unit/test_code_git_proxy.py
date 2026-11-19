"""Network git of the code index goes through an operator's forward proxy as an identity,
with the proxy's password in that one child's environment and nowhere else, or it does
not run."""

from __future__ import annotations

import ast
import base64
import logging
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from server.code.app.index import (
    bootstrap,
    compiler_tier,
    git_proxy,
    gitrepo,
    mirrors,
    pass_fetch,
    sandbox,
)
from server.code.app.index import repo_registry as reg
from server.code.app.index.git_proxy import GitProxyRefused, NetworkGit
from server.code.app.index.pass_budget import CodeIngestResult
from server.code.app.index.tier_health import ToolOutputError
from server.code.domain.index import state, tier_health
from server.core.paths import REPO_ROOT
from server.settings import settings
from tests.unit.code_fixtures import git_config, repo_config

PROXY = "http://proxy.example:3128"
REMOTE = "https://git.example/acme/widgets.git"
IDENTITY = "acme.widgets-indexer"
# Every character that would end a URL's userinfo or its password: each must be encoded.
TOKEN = "tok/en+with:odd chars%@"
ENCODED = quote(TOKEN, safe="")
BASIC = base64.b64encode(f"{IDENTITY}:{TOKEN}".encode()).decode()


def _holds(token_file: Path, token: str) -> None:
    token_file.write_text(token + "\n")


def _missing(token_file: Path) -> None:
    token_file.unlink()


@pytest.fixture
def proxy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The proxy set, with its password in a file; the file."""
    token_file = tmp_path / "proxy-token"
    _holds(token_file, TOKEN)
    monkeypatch.setattr(settings, "code_git_proxy_url", PROXY)
    monkeypatch.setattr(settings, "code_git_proxy_token_file", str(token_file))
    return token_file


class _Spawned:
    """What `run_job` started in place of a process."""

    def __init__(self) -> None:
        self.count = 0
        self.argv: list[str] = []
        self.env: dict[str, str] = {}


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> _Spawned:
    """No child runs: its argv and environment are recorded and it exits 0."""
    seen = _Spawned()

    async def _exec(*argv: str, env: dict[str, str], **_kw: Any) -> object:
        seen.count += 1
        seen.argv, seen.env = list(argv), dict(env)
        return object()

    async def _result(proc: object, job: Any) -> sandbox.JobResult:
        return sandbox.JobResult(0, b"", b"")

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(sandbox, "_result", _result)
    return seen


def _git_fails_saying(monkeypatch: pytest.MonkeyPatch, said: str) -> None:
    """No child runs: every job ends as git does when it prints `said` and exits 128."""

    async def _exec(*_argv: str, **_kw: Any) -> object:
        return object()

    async def _result(proc: object, job: Any) -> sandbox.JobResult:
        return sandbox.JobResult(128, b"", said.encode())

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(sandbox, "_result", _result)


def _entry(tmp_path: Path, name: str = "clone.git", **fields: Any) -> Any:
    fields = {"git_branch": "main", **fields}
    return repo_config(git_dir=str(tmp_path / name), git_remote=REMOTE, **fields)


async def _ls_remote(remote: str = REMOTE, identity: str = IDENTITY) -> sandbox.JobResult:
    return await sandbox.run_job(
        ["git", "ls-remote", remote],
        env=gitrepo.auth_env("repo-token", remote),
        timeout_s=5,
        **gitrepo.remote_access(remote, identity),
    )


# --- what the child gets --------------------------------------------------------------


async def test_a_network_git_job_gets_the_proxy_and_its_credentials_in_its_own_environment(
    proxy: Path, spawned: _Spawned, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "code_git_proxy_ca_file", str(tmp_path / "ca.pem"))
    given = gitrepo.auth_env("repo-token", REMOTE)
    before = dict(given)
    argv = ["git", "ls-remote", REMOTE]
    await sandbox.run_job(argv, env=given, timeout_s=5, **gitrepo.remote_access(REMOTE, IDENTITY))
    config = git_config(spawned.env)
    assert config["http.proxy"] == f"http://{IDENTITY}:{ENCODED}@proxy.example:3128"
    assert config["http.proxyAuthMethod"] == "basic"
    assert config["http.sslCAInfo"] == str(tmp_path / "ca.pem")
    # git takes the last value: ssh, which no HTTP proxy carries, is off for this job
    assert config["protocol.ssh.allow"] == "never"
    assert spawned.argv == argv
    # the caller's mapping, which a harness environment is built like, is left alone
    assert given == before


async def test_git_gets_a_bundle_for_the_proxy_s_own_certificate_only_when_one_is_set(
    proxy: Path, spawned: _Spawned, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An https:// proxy is verified with LOOM_CODE_GIT_PROXY_TLS_CA_FILE, which is not
    the remote's bundle: without it git keeps its default roots for the proxy."""
    monkeypatch.setattr(settings, "code_git_proxy_url", "https://proxy.example:8443")
    monkeypatch.setattr(settings, "code_git_proxy_ca_file", str(tmp_path / "ca.pem"))
    given = gitrepo.auth_env("repo-token", REMOTE)
    before = dict(given)

    async def _config() -> dict[str, str]:
        access = gitrepo.remote_access(REMOTE, IDENTITY)
        await sandbox.run_job(["git", "ls-remote", REMOTE], env=given, timeout_s=5, **access)
        return git_config(spawned.env)

    config = await _config()
    assert config["http.proxy"] == f"https://{IDENTITY}:{ENCODED}@proxy.example:8443"
    assert config["http.sslCAInfo"] == str(tmp_path / "ca.pem")
    assert "http.proxySSLCAInfo" not in config
    monkeypatch.setattr(settings, "code_git_proxy_tls_ca_file", str(tmp_path / "proxy-ca.pem"))
    config = await _config()
    assert config["http.proxySSLCAInfo"] == str(tmp_path / "proxy-ca.pem")
    assert config["http.sslCAInfo"] == str(tmp_path / "ca.pem")
    assert given == before


@pytest.mark.parametrize(
    ("entry", "default", "presented"),
    [
        ("entry-identity", "default-identity", "entry-identity"),
        ("", "default-identity", "default-identity"),
    ],
    ids=["the entry's own", "the deployment's default"],
)
async def test_git_presents_the_entry_identity_and_the_default_only_without_one(
    proxy: Path,
    spawned: _Spawned,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    default: str,
    presented: str,
) -> None:
    monkeypatch.setattr(settings, "code_git_proxy_identity", default)
    await _ls_remote(identity=entry)
    assert git_config(spawned.env)["http.proxy"].startswith(f"http://{presented}:")


async def test_the_password_file_is_read_again_for_every_job(
    proxy: Path, spawned: _Spawned
) -> None:
    await _ls_remote()
    assert ENCODED in git_config(spawned.env)["http.proxy"]
    _holds(proxy, "rotated")
    await _ls_remote()
    assert git_config(spawned.env)["http.proxy"] == f"http://{IDENTITY}:rotated@proxy.example:3128"


async def test_a_proxy_without_a_password_file_gets_the_identity_and_an_empty_password(
    proxy: Path, spawned: _Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "code_git_proxy_token_file", "")
    await _ls_remote()
    # The colon stays: for a user with no password git asks a credential helper.
    assert git_config(spawned.env)["http.proxy"] == f"http://{IDENTITY}:@proxy.example:3128"


@pytest.mark.parametrize(
    "job",
    [
        lambda: gitrepo.remote_access("/srv/mirrors/widgets.git", IDENTITY),
        lambda: {"network": True},
        lambda: {},
    ],
    ids=["git on a local mirror", "a harness with network access", "a local command"],
)
async def test_only_git_talking_to_a_network_remote_is_proxied(
    proxy: Path, spawned: _Spawned, job: Any
) -> None:
    env = gitrepo.git_config_env()
    await sandbox.run_job(["tool"], env=env, timeout_s=5, **job())
    assert spawned.env == env


async def test_without_the_setting_git_runs_with_the_environment_it_was_given(
    spawned: _Spawned,
) -> None:
    await _ls_remote()
    assert spawned.env == gitrepo.auth_env("repo-token", REMOTE)


# --- fail closed ----------------------------------------------------------------------


def _blank(token_file: Path) -> None:
    _holds(token_file, " ")


def _as_given(token_file: Path) -> None:
    pass


@pytest.mark.parametrize(
    ("change", "remote", "identity", "reason"),
    [
        (_missing, REMOTE, IDENTITY, state.GIT_PROXY_TOKEN_REFUSAL),
        (_blank, REMOTE, IDENTITY, state.GIT_PROXY_TOKEN_REFUSAL),
        (_as_given, REMOTE, "", state.GIT_PROXY_IDENTITY_REFUSAL),
        (_as_given, "git@git.example:acme/widgets.git", IDENTITY, state.GIT_PROXY_REMOTE_REFUSAL),
        (_as_given, "ssh://git@git.example/acme/w.git", IDENTITY, state.GIT_PROXY_REMOTE_REFUSAL),
    ],
    ids=["no file", "an empty file", "no identity", "scp remote", "ssh remote"],
)
async def test_a_job_that_cannot_go_through_the_proxy_is_refused_before_any_process_starts(
    proxy: Path, spawned: _Spawned, change: Any, remote: str, identity: str, reason: str
) -> None:
    change(proxy)
    with pytest.raises(GitProxyRefused) as refused:
        await _ls_remote(remote, identity)
    assert str(refused.value) == reason
    assert spawned.count == 0


async def test_the_sandbox_runs_no_network_git_while_the_proxy_is_set(
    proxy: Path, spawned: _Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "_available", True)
    with pytest.raises(GitProxyRefused) as refused:
        await _ls_remote()
    assert str(refused.value) == state.GIT_PROXY_SANDBOX_REFUSAL
    assert spawned.count == 0


async def test_each_caller_reports_a_refusal_as_its_own_failure_with_the_fixed_reason(
    proxy: Path, spawned: _Spawned, tmp_path: Path
) -> None:
    _missing(proxy)
    reason = state.GIT_PROXY_TOKEN_REFUSAL
    with pytest.raises(gitrepo.GitError) as fetch:
        await gitrepo.fetch(str(tmp_path / "m.git"), REMOTE, "main", proxy_identity=IDENTITY)
    with pytest.raises(ValueError) as probe:
        await reg.remote_has_branch(
            "acme/widgets", "main", "", git_remote=REMOTE, proxy_identity=IDENTITY
        )
    with pytest.raises(ToolOutputError) as submodules:
        await compiler_tier.git(
            tmp_path, "submodule", "update", network=True, proxy_identity=IDENTITY
        )
    cfg = repo_config(
        git_dir=str(tmp_path / "clone.git"), git_remote=REMOTE, git_proxy_identity=IDENTITY
    )
    assert await bootstrap.clone_mirror(cfg) == reason
    assert str(fetch.value) == str(probe.value) == str(submodules.value) == reason
    # the entry's card shows a clone's reason only in a shape the mirror job writes
    assert state.shown_clone_reason(reason) == reason
    assert spawned.count == 0


# --- a refusal by the proxy itself --------------------------------------------------------

# git's words for a CONNECT the proxy refused, in the two phrasings of its HTTP library.
REFUSED_BY_THE_PROXY = [
    (f"fatal: unable to access '{REMOTE}/': CONNECT tunnel failed, response 403\n", "403"),
    (
        f"fatal: unable to access '{REMOTE}/': Received HTTP code 407 from proxy after CONNECT\n",
        "407",
    ),
]


@pytest.mark.parametrize(("said", "status"), REFUSED_BY_THE_PROXY, ids=["403", "407"])
async def test_a_clone_the_proxy_refused_reads_as_a_refused_fetch_does(
    proxy: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, said: str, status: str
) -> None:
    """A clone is an entry's first network job and shows a fixed reason, never git's
    words: the reason names the proxy and its status, and the fetch tier records the
    kind and the fix it records for a refused fetch."""
    _git_fails_saying(monkeypatch, said)
    reason = await bootstrap.clone_mirror(_entry(tmp_path, git_proxy_identity=IDENTITY))
    assert reason == f"git clone failed: {state.GIT_PROXY_REFUSED} ({status})"
    assert state.shown_clone_reason(reason) == reason
    with pytest.raises(gitrepo.GitError) as refused:
        await gitrepo.fetch(str(tmp_path / "m.git"), REMOTE, "main", proxy_identity=IDENTITY)
    # as the ingest pass records the two failures
    at = datetime(2026, 1, 1, tzinfo=UTC)
    clone = tier_health.failed(None, "fetch", f"clone {REMOTE}: {reason}", at)
    fetch = tier_health.failed(None, "fetch", f"fetch {REMOTE}: {refused.value}", at)
    assert clone.kind == fetch.kind == "egress"
    assert clone.fix == fetch.fix and "operator's git proxy (LOOM_CODE_GIT_PROXY_URL)" in clone.fix
    # without the setting a failed clone reads as before
    monkeypatch.setattr(settings, "code_git_proxy_url", "")
    assert await bootstrap.clone_mirror(_entry(tmp_path, "b.git")) == "git clone failed (exit 128)"


@pytest.mark.parametrize(("said", "status"), REFUSED_BY_THE_PROXY, ids=["403", "407"])
async def test_the_ls_remote_before_an_add_names_the_proxy_that_refused_it(
    proxy: Path, monkeypatch: pytest.MonkeyPatch, said: str, status: str
) -> None:
    async def probe() -> str:
        with pytest.raises(ValueError) as refused:
            await reg.remote_has_branch(
                "acme/widgets", "main", "", git_remote=REMOTE, proxy_identity=IDENTITY
            )
        return str(refused.value)

    _git_fails_saying(monkeypatch, said)
    named = await probe()
    assert named == f"git ls-remote failed for acme/widgets: {state.GIT_PROXY_REFUSED} ({status})"
    assert tier_health.classify(named) == "egress"
    # without the setting the same words read as before
    monkeypatch.setattr(settings, "code_git_proxy_url", "")
    assert await probe() == "git ls-remote failed for acme/widgets"


# --- the password in no output --------------------------------------------------------


async def test_the_password_is_taken_out_of_what_a_job_prints_in_every_spelling(
    proxy: Path,
) -> None:
    """As its file holds it, percent-encoded as in `http.proxy` (the child's own
    environment, printed by `env`), and inside the Basic pair of the proxy header."""
    script = (
        f"printf '%s\\n' {shlex.quote(TOKEN)} >&2; echo 'Proxy-Authorization: Basic {BASIC}'; env"
    )
    done = await sandbox.run_job(
        ["sh", "-c", script],
        env={"PATH": "/usr/bin:/bin"},
        timeout_s=30,
        **gitrepo.remote_access(REMOTE, IDENTITY),
    )
    printed = done.stdout + done.stderr
    for spelling in (TOKEN, ENCODED, BASIC):
        assert spelling.encode() not in printed
    assert done.stderr == git_proxy.REDACTED + b"\n"
    assert b"Proxy-Authorization: Basic " + git_proxy.REDACTED in done.stdout
    assert f"http://{IDENTITY}:".encode() + git_proxy.REDACTED + b"@proxy.example:3128" in (
        done.stdout
    )


async def test_refs_a_caller_parses_are_left_as_the_remote_sent_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A weak password can spell part of a ref or a sha; git's own words (stderr) are
    masked, the remote's refs are data."""
    token_file = tmp_path / "proxy-token"
    _holds(token_file, "release")
    monkeypatch.setattr(settings, "code_git_proxy_url", PROXY)
    monkeypatch.setattr(settings, "code_git_proxy_token_file", str(token_file))
    listing = "abc123\trefs/heads/release/1.2\n"
    done = await sandbox.run_job(
        ["sh", "-c", f"printf {shlex.quote(listing)}; echo 'fatal: release' >&2"],
        env={"PATH": "/usr/bin:/bin"},
        timeout_s=30,
        keep_stdout=True,
        **gitrepo.remote_access(REMOTE, IDENTITY),
    )
    assert done.stdout.decode() == "abc123\trefs/heads/release/1.2\n"
    assert done.stderr == b"fatal: " + git_proxy.REDACTED + b"\n"


async def test_no_error_text_log_line_or_health_record_carries_the_password(
    proxy: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    _git_fails_saying(
        monkeypatch, f"fatal: Invalid proxy URL 'http://{IDENTITY}:{ENCODED}@proxy.example:3128'\n"
    )
    with pytest.raises(gitrepo.GitError) as failed:
        await gitrepo.fetch(str(tmp_path / "m.git"), REMOTE, "main", proxy_identity=IDENTITY)
    record = tier_health.failed(
        None, "fetch", f"fetch {REMOTE}: {failed.value}", datetime(2026, 1, 1, tzinfo=UTC)
    )
    cfg = repo_config(
        git_dir=str(tmp_path / "clone.git"), git_remote=REMOTE, git_proxy_identity=IDENTITY
    )
    with caplog.at_level(logging.WARNING, logger="loom.code_index.bootstrap"):
        assert await bootstrap.clone_mirror(cfg) == "git clone failed (exit 128)"
    assert "<proxy-token>" in str(failed.value) and "<proxy-token>" in caplog.text
    for text in (str(failed.value), record.error, record.fix, caplog.text):
        assert TOKEN not in text and ENCODED not in text


# --- every network git path carries the entry's identity -----------------------------


NETWORK_GIT: dict[str, Any] = {
    "fetch": lambda d: gitrepo.fetch(str(d), REMOTE, "main", proxy_identity=IDENTITY),
    "fetch_many": lambda d: gitrepo.fetch_many(str(d), REMOTE, ["main"], proxy_identity=IDENTITY),
    "remote_branches": lambda d: gitrepo.remote_branches(str(d), REMOTE, proxy_identity=IDENTITY),
    "fetch_branches": lambda d: gitrepo.fetch_branches(
        str(d), REMOTE, ["next"], proxy_identity=IDENTITY
    ),
    "fetch_shas": lambda d: gitrepo.fetch_shas(str(d), REMOTE, ["a" * 40], proxy_identity=IDENTITY),
    "remote_pull_heads": lambda d: gitrepo.remote_pull_heads(
        str(d), REMOTE, proxy_identity=IDENTITY
    ),
    "fetch_pull_heads": lambda d: gitrepo.fetch_pull_heads(
        str(d), REMOTE, [7], proxy_identity=IDENTITY
    ),
    "the add's branch probe": lambda d: reg.remote_has_branch(
        "acme/widgets", "main", "", git_remote=REMOTE, proxy_identity=IDENTITY
    ),
    "the add's branch listing": lambda d: reg.remote_branches(
        "acme/widgets", "", git_remote=REMOTE, proxy_identity=IDENTITY
    ),
    "a mirror's clone": lambda d: bootstrap.clone_mirror(_entry(d, git_proxy_identity=IDENTITY)),
    "a read's fetch": lambda d: bootstrap.fetch_mirror(_entry(d, git_proxy_identity=IDENTITY)),
    "a ledger merge's pin": lambda d: pass_fetch.pin_merges(
        _entry(d, git_proxy_identity=IDENTITY), str(d), ["a" * 40]
    ),
}


@pytest.mark.parametrize("name", list(NETWORK_GIT))
async def test_every_network_git_job_names_its_remote_and_the_entry_identity(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jobs: list[dict[str, Any]] = []

    async def _job(argv: list[str], **job: Any) -> sandbox.JobResult:
        jobs.append(job)
        return sandbox.JobResult(0, b"abc123\trefs/heads/main\n", b"")

    monkeypatch.setattr(sandbox, "run_job", _job)
    await NETWORK_GIT[name](tmp_path)
    assert [job["network_git"] for job in jobs] == [NetworkGit(REMOTE, IDENTITY)]


# The helpers through which git talks to a remote, as their callers name them.
_REMOTE_HELPERS = {
    "fetch",
    "fetch_many",
    "remote_branches",
    "fetch_branches",
    "fetch_shas",
    "remote_pull_heads",
    "fetch_pull_heads",
    "remote_has_branch",
    "_ls_remote",
}
_HELPER_MODULES = {"gitrepo", "reg", "repo_registry"}


def _talks_to_a_remote(call: ast.Call) -> bool:
    keywords = {k.arg for k in call.keywords}
    match call.func:
        case ast.Attribute(value=ast.Name(id=module), attr=name):
            if module in _HELPER_MODULES and name in _REMOTE_HELPERS:
                return True
            return (module, name) == ("compiler_tier", "git") and "network" in keywords
        case ast.Name(id="_ls_remote"):
            return True
        case ast.Name(id="_git"):
            return "remote" in keywords
        case ast.Name(id="git"):
            return "network" in keywords
    return False


def test_every_call_site_of_a_network_git_helper_passes_the_entry_identity() -> None:
    """A call that left it out would fetch its entry under the deployment's default
    identity, another repository's as far as the proxy can tell."""
    calls, missing = 0, []
    for path in sorted((REPO_ROOT / "server").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and _talks_to_a_remote(node):
                calls += 1
                if "proxy_identity" not in {k.arg for k in node.keywords}:
                    missing.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert calls >= 25, "the scan lost the call sites it checks"
    assert missing == []


async def test_a_submodule_update_carries_the_identity_and_a_local_git_command_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jobs: list[dict[str, Any]] = []

    async def _job(argv: list[str], **job: Any) -> sandbox.JobResult:
        jobs.append(job)
        return sandbox.JobResult(0, b"", b"")

    monkeypatch.setattr(compiler_tier, "run_job", _job)
    await compiler_tier.git(
        tmp_path, "submodule", "update", network=True, remote=REMOTE, proxy_identity=IDENTITY
    )
    await compiler_tier.git(tmp_path, "checkout", "--force", "a" * 40)
    # It follows .gitmodules, not the entry's remote: an entry on a local mirror still
    # fetches its submodules through the proxy.
    assert jobs[0]["network_git"] == NetworkGit(identity=IDENTITY)
    assert "network_git" not in jobs[1]


async def test_branches_of_one_mirror_share_a_fetch_only_under_one_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "widgets.git").mkdir()
    fetched: list[tuple[tuple[str, ...], str]] = []

    async def fetch_many(
        git_dir: str, remote: str, branches: list[str], *, token: str = "", proxy_identity: str = ""
    ) -> None:
        fetched.append((tuple(branches), proxy_identity))

    monkeypatch.setattr(gitrepo, "fetch_many", fetch_many)
    monkeypatch.setattr(pass_fetch, "_FETCHED_THIS_PASS", set())
    entries = [
        _entry(tmp_path, "widgets.git", namespace=ns, git_branch=branch, git_proxy_identity=who)
        for ns, branch, who in (
            ("code-widgets", "main", "indexer-1"),
            ("code-widgets-26-1", "26.1", "indexer-1"),
            ("code-widgets-26-2", "26.2", "indexer-2"),
        )
    ]
    assert await pass_fetch.prefetch_mirrors(entries, CodeIngestResult()) == 1
    # the third entry fetches in its own turn, as itself
    assert fetched == [(("main", "26.1"), "indexer-1")]


async def test_a_host_round_fetches_a_shared_mirror_as_its_first_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "widgets.git").mkdir()
    presented: list[str] = []

    class _Published:
        async def get_state(self, org: str, ns: str, key: str) -> str:
            return "tip" if key == "clone_head" else ""

        async def open_pr_heads(self, org: str, ns: str) -> dict[int, str]:
            return {7: "head"}

        async def merge_shas_page(self, *_a: Any, **_k: Any) -> list[Any]:
            return []

    async def _nothing_local(git_dir: str) -> dict[Any, str]:
        return {}

    async def _fetch(git_dir: str, remote: str, refs: list[Any], **access: str) -> int:
        presented.append(access["proxy_identity"])
        return len(refs)

    monkeypatch.setattr(mirrors.code_store, "CodeRepo", _Published)
    monkeypatch.setattr(gitrepo, "local_branch_shas", _nothing_local)
    monkeypatch.setattr(gitrepo, "local_pull_heads", _nothing_local)
    monkeypatch.setattr(gitrepo, "fetch_many", _fetch)
    monkeypatch.setattr(gitrepo, "fetch_pull_heads", _fetch)
    entries = [
        _entry(tmp_path, "widgets.git", namespace="code-widgets", git_proxy_identity="indexer-1"),
        _entry(
            tmp_path, "widgets.git", namespace="code-widgets-26", git_proxy_identity="indexer-2"
        ),
    ]
    await mirrors._fetch_round(entries[0].git_dir, entries)
    assert presented == ["indexer-1", "indexer-1"]  # the branches, then the open PR heads
