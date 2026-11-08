"""A hostile repository job inside the real sandbox: it tries to read the
server's secrets, reach the metadata address, loopback and private ranges (directly,
through the egress proxy, through a redirect and through a .gitmodules URL), escape
with `unshare`, fork-bomb, and leave a process behind. Every attempt fails and
nothing outlives the job.

Needs a host where bubblewrap can create user namespaces; skipped elsewhere, and
a failure where LOOM_TEST_REQUIRE_SANDBOX=1 (the CI sandbox job)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from server.code.app.index import compiler_tier, egress, sandbox
from server.code.app.index.gitrepo import auth_env
from tests.unit._hosts import resolves

_HAS_SANDBOX = sandbox.sandbox_available()


@pytest.fixture(autouse=True)
def _real_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    if not _HAS_SANDBOX:
        if os.environ.get("LOOM_TEST_REQUIRE_SANDBOX") == "1":
            pytest.fail("bubblewrap cannot create namespaces here")
        pytest.skip("bubblewrap cannot create namespaces here")
    monkeypatch.setattr(sandbox, "_available", True)


CANARY = "canary-7f3a9c"
ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin"}


async def _python(code: str, **kw) -> sandbox.JobResult:
    return await sandbox.run_sandboxed(
        [sys.executable, "-I", "-c", textwrap.dedent(code)],
        env=ENV,
        timeout_s=kw.pop("timeout_s", 30),
        merge_stderr=True,
        **kw,
    )


def _running(marker: str) -> list[int]:
    """Host pids whose command line carries `marker`."""
    pids = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
            except OSError:
                continue
            if marker.encode() in cmdline:
                pids.append(int(entry.name))
    return pids


async def test_host_secrets_are_out_of_reach(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "server-home"
    home.mkdir()
    (home / "secret.txt").write_text(CANARY)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOOM_CANARY_SECRET", CANARY)
    done = await _python(
        f"""
        import os
        for path in ({str(home / "secret.txt")!r}, "/proc/{os.getpid()}/environ",
                     "/proc/1/environ", "/etc/shadow", "/var/run/docker.sock"):
            try:
                print(path, open(path, "rb").read()[:200])
            except OSError as e:
                print(path, "refused", type(e).__name__)
        print("env", sorted(os.environ))
        """
    )
    assert done.returncode == 0, done.stdout
    assert CANARY.encode() not in done.stdout
    assert b"LOOM_CANARY_SECRET" not in done.stdout


async def test_no_direct_network(tmp_path: Path) -> None:
    listener = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        done = await _python(
            f"""
            import socket
            for host, port in (("169.254.169.254", 80), ("127.0.0.1", {port}),
                               ("10.0.0.1", 443), ("1.1.1.1", 443)):
                try:
                    socket.create_connection((host, port), timeout=2)
                    print("REACHED", host, port)
                except OSError as e:
                    print("refused", host, type(e).__name__)
            try:
                socket.getaddrinfo("example.com", 443)
                print("REACHED dns")
            except OSError:
                print("no dns")
            """
        )
    finally:
        listener.close()
    assert b"REACHED" not in done.stdout, done.stdout


async def test_the_proxy_refuses_private_targets_and_redirects_to_them(
    tmp_path: Path, monkeypatch
) -> None:
    """public.example is a test server that answers every request with a redirect
    to private.example; the job follows it the way git or npm would, with a new
    CONNECT, and the proxy refuses that hop."""

    async def redirect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 302 Found\r\nLocation: https://private.example/x\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(redirect, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    real_vet, real_open = egress.vet_host, asyncio.open_connection

    async def vet(host: str) -> tuple[str, str]:
        if host == "public.example":
            return "", "127.0.0.1"  # stands in for a public address
        return await real_vet(host)

    async def open_connection(host: str, p: int, **kw):
        return await real_open(host, port if host == "127.0.0.1" else p, **kw)

    monkeypatch.setattr(egress, "vet_host", vet)
    monkeypatch.setattr(egress.asyncio, "open_connection", open_connection)
    resolves(monkeypatch, lambda h: ["10.0.0.5"] if "private" in h else [h])
    try:
        done = await _python(
            f"""
            import socket
            def connect(target):
                s = socket.create_connection(("127.0.0.1", {sandbox.RELAY_PORT}), timeout=5)
                s.sendall(f"CONNECT {{target}} HTTP/1.1\\r\\n\\r\\n".encode())
                return s, s.recv(4096)
            for target in ("169.254.169.254:443", "169.254.169.254:80", "127.0.0.1:443",
                           "10.0.0.1:443", "private.example:443"):
                print(target, connect(target)[1][:12])
            s, head = connect("public.example:443")
            print("public", head[:12])
            s.sendall(b"GET /repo.git/info/refs HTTP/1.1\\r\\nHost: public.example\\r\\n\\r\\n")
            location = s.recv(4096).split(b"Location: ")[1].split(b"\\r\\n")[0].decode()
            host = location.split("/")[2]
            print("redirect", host, connect(host + ":443")[1][:12])
            """,
            network=True,
        )
    finally:
        server.close()
    out = done.stdout.decode()
    assert done.returncode == 0, out
    assert "public b'HTTP/1.1 200'" in out
    lines = [ln for ln in out.splitlines() if not ln.startswith("public")]
    assert lines and all("HTTP/1.1 40" in ln for ln in lines), out


async def test_submodule_urls_to_private_hosts_and_local_transports_fail(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    resolves(monkeypatch, lambda h: [h])  # address literals only
    ident = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t"}
    env = {**auth_env(), **ident, "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*args: str) -> str:
        done = subprocess.run(["git", *args], check=True, env=env, capture_output=True, text=True)
        return done.stdout.strip()

    secret_repo, repo, pwned = tmp_path / "secret-repo", tmp_path / "checkout", tmp_path / "pwned"
    git("init", "-q", str(secret_repo))
    git("-C", str(secret_repo), "commit", "-q", "--allow-empty", "-m", "secret")
    pin = git("-C", str(secret_repo), "rev-parse", "HEAD")
    git("init", "-q", str(repo))
    urls = {
        "meta": "https://169.254.169.254/x.git",
        "private": "https://10.0.0.1/x.git",
        "local": f"file://{secret_repo}",
        "path": str(secret_repo),
        "ext": f"ext::sh -c touch% {pwned}",
    }
    (repo / ".gitmodules").write_text(
        "".join(f'[submodule "{n}"]\n\tpath = {n}\n\turl = {u}\n' for n, u in urls.items())
    )
    for name in urls:
        git("-C", str(repo), "update-index", "--add", "--cacheinfo", f"160000,{pin},{name}")
    git("-C", str(repo), "add", ".gitmodules")
    git("-C", str(repo), "commit", "-q", "-m", "submodules")

    caplog.set_level("WARNING", logger="loom.code_index.egress")
    with pytest.raises(RuntimeError) as failed:
        await compiler_tier.git(repo, "submodule", "update", "--init", "--recursive", network=True)
    assert not pwned.exists()
    assert not any((repo / name / ".git").exists() for name in urls)
    text = str(failed.value) + caplog.text
    assert "a private or local address" in caplog.text, text


async def test_unshare_is_refused_inside(tmp_path: Path) -> None:
    done = await _python(
        """
        import os
        try:
            os.unshare(os.CLONE_NEWUSER)
            print("ESCAPED")
        except OSError as e:
            print("refused", e.errno)
        """
    )
    assert b"ESCAPED" not in done.stdout and b"refused" in done.stdout


async def test_a_raw_clone_into_a_new_user_namespace_is_refused(monkeypatch) -> None:
    """clone(CLONE_NEWUSER) is unshare by another name; refused by seccomp even
    on a bwrap too old for --disable-userns."""
    monkeypatch.setattr(sandbox, "_disable_userns", False)
    done = await _python(
        """
        import ctypes, os, signal
        libc = ctypes.CDLL(None, use_errno=True)
        nr = 220 if os.uname().machine == "aarch64" else 56
        pid = libc.syscall(nr, 0x10000000 | signal.SIGCHLD, 0, 0, 0, 0)
        if pid == 0:
            os._exit(0)
        print("ESCAPED" if pid > 0 else f"refused {ctypes.get_errno()}")
        """
    )
    assert b"ESCAPED" not in done.stdout and b"refused" in done.stdout, done.stdout


async def test_a_fork_bomb_is_bounded_per_job_and_nothing_survives(monkeypatch) -> None:
    """Two jobs at once, each forking until the kernel says no: each stops at
    its own limit, whatever else the uid runs on the host."""
    monkeypatch.setattr(sandbox.settings, "code_sandbox_max_pids", 64)
    marker = "bomb-31337"
    bomb = f"""
        # {marker}
        import errno, os, time
        forks = 0
        while True:
            try:
                if os.fork() == 0:
                    time.sleep(60)
                    os._exit(0)
                forks += 1
            except OSError as e:
                print(forks, e.errno == errno.EAGAIN, flush=True)
                break
        """
    results = await asyncio.gather(_python(bomb, timeout_s=20), _python(bomb, timeout_s=20))
    for done in results:
        forks, eagain = done.stdout.split()
        assert eagain == b"True", done.stdout
        # Each job gets the whole limit, less its python and the sandbox's init:
        # a budget the two shared would leave one of them near half.
        assert 60 <= int(forks) <= 62, done.stdout
    await asyncio.sleep(0.5)
    assert _running(marker) == []


async def test_an_orphan_does_not_outlive_its_job() -> None:
    done = await sandbox.run_sandboxed(
        ["sh", "-c", "sleep 4242 & echo started"], env=ENV, timeout_s=10
    )
    assert done.stdout == b"started\n"
    await asyncio.sleep(0.5)
    assert _running("sleep 4242") == []


async def test_the_ssh_relay_is_refused_a_private_host(monkeypatch) -> None:
    """ssh remotes go through the relay's connect mode (GIT_SSH_COMMAND's
    ProxyCommand), and the proxy refuses a host that resolves privately."""
    resolves(monkeypatch, lambda h: ["10.0.0.5"])
    relay = [sys.executable, "-I", sandbox._RELAY_INSIDE, "connect"]
    done = await sandbox.run_sandboxed(
        [*relay, "git.private.example", "22", f"{sandbox._EGRESS_INSIDE}/proxy.sock"],
        env=ENV,
        timeout_s=20,
        network=True,
        merge_stderr=True,
    )
    assert done.returncode == 1 and b"refused" in done.stdout


async def test_a_toolchain_mount_shows_no_loom_data(tmp_path: Path, monkeypatch) -> None:
    """The C++ tier with the default paths: scip-clang at var/bin/scip-clang, the
    other entries' mirrors and checkouts beside it in var/."""
    from server.settings import settings

    monkeypatch.chdir(tmp_path)
    for field in ("code_repos_root", "code_scip_checkout_dir"):
        monkeypatch.setattr(settings, field, type(settings).model_fields[field].default)
    canaries = [
        tmp_path / "var/other-data/file",
        tmp_path / "var/code_repos/self/checkout-other/secret",
        tmp_path / "var/env-backup-1",
    ]
    for path in canaries:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CANARY)
    scip_clang = tmp_path / "var/bin/scip-clang"
    scip_clang.parent.mkdir(parents=True)
    scip_clang.write_text("#!/bin/sh\necho scip-clang-ran\n")
    scip_clang.chmod(0o755)
    reads = " ".join(f"cat {p} 2>/dev/null;" for p in canaries)
    for given in (scip_clang, scip_clang.parent):
        done = await sandbox.run_sandboxed(
            ["sh", "-c", f"{scip_clang} 2>&1; {reads} ls -R {tmp_path}/var 2>&1"],
            env=ENV,
            timeout_s=30,
            readonly=sandbox.tool_mounts(given),
        )
        assert CANARY.encode() not in done.stdout, done.stdout
        assert b"other-data" not in done.stdout and b"checkout-other" not in done.stdout
    assert (
        b"scip-clang-ran"
        in (
            await sandbox.run_sandboxed(
                [str(scip_clang)], env=ENV, timeout_s=30, readonly=sandbox.tool_mounts(scip_clang)
            )
        ).stdout
    )


async def test_the_checkout_stays_writable_under_a_read_only_parent(tmp_path: Path) -> None:
    checkout = tmp_path / "tools" / "repos" / "checkout"
    (checkout / ".git").mkdir(parents=True)
    done = await sandbox.run_sandboxed(
        ["sh", "-c", f"echo built > {checkout}/out && touch {checkout}/.git/hook; echo $?"],
        env=ENV,
        timeout_s=30,
        writable=[checkout],
        readonly=[tmp_path / "tools", checkout / ".git"],
        merge_stderr=True,
    )
    assert (checkout / "out").read_text() == "built\n"
    assert not (checkout / ".git" / "hook").exists(), done.stdout


async def test_output_past_its_bounds_ends_bounded(monkeypatch) -> None:
    """A parsed stdout past its limit kills the job; diagnostic output keeps a tail."""
    monkeypatch.setattr(sandbox, "_STDOUT_LIMIT", 4096)
    monkeypatch.setattr(sandbox, "_TAIL_BYTES", 100)
    flood = "head -c 1000000 /dev/zero"
    killed = await sandbox.run_sandboxed(
        ["sh", "-c", f"{flood}; sleep 30"], env=ENV, timeout_s=20, keep_stdout=True
    )
    assert killed.returncode != 0 and killed.stdout == b""
    assert b"stdout passed 4096 bytes" in killed.stderr
    tail = await sandbox.run_sandboxed(
        ["sh", "-c", f"{flood}; echo end"], env=ENV, timeout_s=20, merge_stderr=True
    )
    assert tail.returncode == 0 and len(tail.stdout) == 100 and tail.stdout.endswith(b"end\n")


async def test_loom_data_inside_the_python_prefix_is_unreadable(
    tmp_path: Path, monkeypatch
) -> None:
    from server.settings import settings

    prefix = tmp_path / "py"
    (prefix / "checkouts").mkdir(parents=True)
    (prefix / "checkouts" / "secret").write_text(CANARY)
    (prefix / "lib.txt").write_text("runtime")
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(settings, "code_scip_checkout_dir", str(prefix / "checkouts"))
    done = await sandbox.run_sandboxed(
        [
            "sh",
            "-c",
            f"cat {prefix}/lib.txt; cat {prefix}/checkouts/secret; ls -a {prefix}/checkouts",
        ],
        env=ENV,
        timeout_s=30,
        merge_stderr=True,
    )
    assert b"runtime" in done.stdout
    assert CANARY.encode() not in done.stdout and b"secret\n" not in done.stdout, done.stdout


async def test_a_job_writes_with_the_process_umask(tmp_path: Path, monkeypatch) -> None:
    """The code repos root is shared through its group: what a job creates there keeps
    the server's umask 007, so the other uid can write it too."""
    from server.code.app.index.repo_modes import repo_umask

    monkeypatch.setattr("server.settings.settings.code_repos_shared_gid", os.getegid())

    out = tmp_path / "out"
    out.mkdir()
    with repo_umask():
        done = await _python(
            f"""
            import os
            os.mkdir({str(out / "d")!r})
            open({str(out / "f")!r}, "w").close()
            """,
            writable=[out],
        )
    assert done.returncode == 0, done.stdout
    assert (out / "d").stat().st_mode & 0o777 == 0o770
    assert (out / "f").stat().st_mode & 0o777 == 0o660
