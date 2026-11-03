"""The reference extractor's HTTP service: the token on every `/v1` call, what
it declares, an extraction run in a child process, typed refusals, and the
limits on one extraction (time, memory, output size) with the child's empty
environment. The misbehaving children are small modules that replace the
child's `extract`.
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import httpx
import pytest

from loom_extractor import service

TOKEN = "t" * 24
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _client(**config: object) -> httpx.AsyncClient:
    app = service.create_app(service.Config(token=TOKEN, **config))  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x")


async def test_every_v1_call_needs_the_token_and_health_does_not() -> None:
    async with _client() as c:
        assert (await c.get("/healthz")).status_code == 200
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
            r = await c.get("/v1/capabilities", headers=headers)
            assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
            r = await c.post("/v1/extract?format=text", content=b"x", headers=headers)
            assert r.status_code == 401


def test_the_service_refuses_to_start_without_a_long_enough_token() -> None:
    for env in ({}, {"LOOM_EXTRACTOR_TOKEN": "short"}):
        with pytest.raises(SystemExit, match="LOOM_EXTRACTOR_TOKEN"):
            service.Config.from_env(env)
    config = service.Config.from_env(
        {"LOOM_EXTRACTOR_TOKEN": TOKEN, "LOOM_EXTRACTOR_MEMORY_MB": "512"}
    )
    assert (config.token, config.memory_mb, config.timeout_s, config.concurrency) == (
        TOKEN,
        512,
        120.0,
        1,
    )
    for name, value in (
        ("CONCURRENCY", "0"),
        ("TIMEOUT_S", "0"),
        ("TIMEOUT_S", "nan"),
        ("TIMEOUT_S", "inf"),
        ("MEMORY_MB", "0"),
        ("MAX_BYTES", "0"),
        ("MAX_ARTIFACT_BYTES", "0"),
    ):
        with pytest.raises(SystemExit, match=f"LOOM_EXTRACTOR_{name} must be a number above 0"):
            service.Config.from_env(
                {"LOOM_EXTRACTOR_TOKEN": TOKEN, f"LOOM_EXTRACTOR_{name}": value}
            )


def test_the_service_refuses_root_and_the_capabilities_that_lift_its_process_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from loom_extractor import __main__ as entry

    monkeypatch.setattr(entry.os, "getuid", lambda: 1000)
    monkeypatch.setattr(entry, "permitted_capabilities", lambda: 1 << 10)  # CAP_NET_BIND_SERVICE
    entry.refuse_root()
    for bit in (21, 24):  # CAP_SYS_ADMIN, CAP_SYS_RESOURCE
        monkeypatch.setattr(entry, "permitted_capabilities", lambda bit=bit: 1 << bit)
        with pytest.raises(SystemExit, match="CAP_SYS_ADMIN or CAP_SYS_RESOURCE"):
            entry.refuse_root()
    monkeypatch.setattr(entry.os, "getuid", lambda: 0)
    with pytest.raises(SystemExit, match="root"):
        entry.refuse_root()


def test_the_permitted_set_is_read_from_the_status_and_its_absence_refused() -> None:
    from loom_extractor import __main__ as entry

    status = "Name:\tpython\nCapInh:\t0000000000000000\nCapPrm:\t0000000001000000\n"
    assert entry.permitted_capabilities(status) == 1 << 24  # permitted, not effective
    with pytest.raises(SystemExit, match="capabilities"):
        entry.permitted_capabilities("Name:\tpython\n")
    own = next(
        int(line.split()[1], 16)
        for line in Path("/proc/self/status").read_text().splitlines()
        if line.startswith("CapPrm:")
    )
    assert entry.permitted_capabilities() == own


async def test_a_child_cannot_gain_privileges_on_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _child(
        tmp_path,
        monkeypatch,
        """
        import ctypes
        raise ExtractionError("corrupt", str(ctypes.CDLL(None).prctl(39, 0, 0, 0, 0)))
        """,
    )
    r = await _extract()
    assert r.json()["error"]["message"] == "1"  # PR_GET_NO_NEW_PRIVS


async def test_capabilities_declare_each_parsed_format_and_its_options() -> None:
    async with _client(max_bytes=1000) as c:
        d = (await c.get("/v1/capabilities", headers=AUTH)).json()
    assert d["contract_version"] == 1 and d["max_bytes"] == 1000
    assert d["extractor"] == {"name": "loom-reference", "version": "1"}
    formats = {f["format"]: f["options"] for f in d["formats"]}
    assert formats == {
        "markdown": ["encoding"],
        "html": ["encoding"],
        "text": ["encoding"],
        "sql": ["encoding", "sql_dialect"],
        "toml": ["encoding"],
    }


async def test_an_extraction_runs_in_a_child_and_returns_the_artifact() -> None:
    sql = b"CREATE TABLE t (a Int8) ENGINE = Memory;\nSELECT a FROM t;\n"
    async with _client() as c:
        r = await c.post(
            "/v1/extract",
            params={"format": "sql", "name": "q.sql", "sql_dialect": "clickhouse"},
            content=sql,
            headers=AUTH,
        )
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["schema_version"], d["format"], d["options"]) == (
        1,
        "sql",
        {"sql_dialect": "clickhouse"},
    )
    assert [b["text"] for b in d["blocks"]] == [
        "CREATE TABLE t (a Int8) ENGINE = Memory;",
        "SELECT a FROM t;",
    ]


@pytest.mark.parametrize(
    ("params", "body", "code"),
    [
        ({"format": "pdf", "name": "a.pdf"}, b"%PDF-1.7", "format_unavailable"),
        ({"format": "markdown", "name": "a.md", "sql_dialect": "x"}, b"# a", "invalid_option"),
        ({"format": "markdown", "name": "a.md"}, b"%PDF-1.7\n", "format_mismatch"),
        ({"format": "text", "name": "a.txt"}, b"caf\xe9", "encoding"),
        ({"format": "sql", "name": "q.sql", "sql_dialect": "nope"}, b"SELECT 1;", "invalid_option"),
        ({"format": "text", "name": "a.txt"}, b"x" * 2000, "too_large"),
    ],
)
async def test_a_source_it_cannot_extract_is_a_typed_422(
    params: dict[str, str], body: bytes, code: str
) -> None:
    async with _client(max_bytes=1000) as c:
        r = await c.post("/v1/extract", params=params, content=body, headers=AUTH)
    assert (r.status_code, r.json()["error"]["code"]) == (422, code), r.text


def _child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Run extractions through a child module whose `extract` is `body`."""
    (tmp_path / "test_child.py").write_text(
        textwrap.dedent(
            """
            import json, os, sys, time
            from loom_extractor import child
            from loom_extractor.source import ExtractionError

            def extract(data, name, options):
            """
        )
        + textwrap.indent(textwrap.dedent(body), "    ")
        + "\nchild.extract = extract\nsys.exit(child.main(sys.argv))\n"
    )
    monkeypatch.setattr(service, "_CHILD_MODULE", "test_child")
    monkeypatch.setattr(service, "_PACKAGE_ROOT", f"{tmp_path}{os.pathsep}{service._PACKAGE_ROOT}")


async def _extract(**config: object) -> httpx.Response:
    async with _client(**config) as c:
        return await c.post("/v1/extract?format=text&name=a.txt", content=b"x", headers=AUTH)


async def test_an_extraction_past_its_time_is_killed_and_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _child(tmp_path, monkeypatch, "time.sleep(60)")
    r = await _extract(timeout_s=1.0)
    assert (r.status_code, r.json()["error"]["code"]) == (422, "timeout")


async def test_an_extraction_past_its_memory_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _child(tmp_path, monkeypatch, "return bytearray(1 << 31)")
    r = await _extract(memory_mb=512)
    assert (r.status_code, r.json()["error"]["code"]) == (422, "resource_limit")


async def test_an_artifact_past_its_bound_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _child(
        tmp_path,
        monkeypatch,
        """
        class Big:
            def to_json(self):
                return b"x" * 300_000
        return Big()
        """,
    )
    r = await _extract(max_artifact_bytes=100_000)
    assert (r.status_code, r.json()["error"]["code"]) == (422, "resource_limit")


async def test_a_child_sees_no_environment_and_cannot_write_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _child(
        tmp_path,
        monkeypatch,
        f"""
        try:
            with open({str(tmp_path / "out")!r}, "w") as f:
                f.write("x")
                f.flush()
            wrote = "yes"
        except OSError:
            wrote = "no"
        raise ExtractionError("corrupt", json.dumps([sorted(os.environ), wrote]))
        """,
    )
    monkeypatch.setenv("LOOM_EXTRACTOR_TOKEN", TOKEN)
    r = await _extract()
    names, wrote = json.loads(r.json()["error"]["message"])
    assert TOKEN not in r.text and "LOOM_EXTRACTOR_TOKEN" not in names
    assert set(names) <= {"PYTHONPATH", "PATH", "LC_ALL", "LC_CTYPE"} and wrote == "no"


async def test_a_child_that_crashes_refuses_the_source_as_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry would crash the same way: the source is refused, not the service down."""
    _child(tmp_path, monkeypatch, "raise RuntimeError('bug')")
    r = await _extract()
    assert (r.status_code, r.json()["error"]["code"]) == (422, "corrupt")
    assert r.json()["error"]["message"] == "the parser failed on this file: RuntimeError"


async def test_a_parser_whose_dependency_is_missing_is_a_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken install, not a bad source: every source of that format would fail."""
    _child(tmp_path, monkeypatch, "import sqlglot_missing_from_this_install")
    r = await _extract()
    assert (r.status_code, r.json()["error"]["code"]) == (500, "extractor_error")


async def test_a_child_that_fails_before_its_parser_runs_is_a_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused prctl or a missing import is the service's fault, worth a retry."""
    (tmp_path / "test_child.py").write_text("raise OSError('prctl(PR_SET_DUMPABLE, 0) failed')\n")
    monkeypatch.setattr(service, "_CHILD_MODULE", "test_child")
    monkeypatch.setattr(service, "_PACKAGE_ROOT", f"{tmp_path}{os.pathsep}{service._PACKAGE_ROOT}")
    r = await _extract()
    assert (r.status_code, r.json()["error"]["code"]) == (500, "extractor_error")


def test_the_service_and_the_test_environment_pin_one_sqlglot() -> None:
    import tomllib

    root = Path(__file__).resolve().parents[2]
    server = tomllib.loads((root / "pyproject.toml").read_text())
    own = tomllib.loads((root / "extractor" / "pyproject.toml").read_text())
    sqlglot = [d for d in own["project"]["dependencies"] if d.startswith("sqlglot")]
    assert sqlglot == server["project"]["optional-dependencies"]["extractor"]


def test_the_image_locks_what_the_tests_ran() -> None:
    """The image installs extractor/uv.lock; these tests run on the root lock. A
    package both hold is at one version in each, so the image runs what passed."""
    import tomllib

    root = Path(__file__).resolve().parents[2]

    def locked(path: Path) -> dict[str, str]:
        return {p["name"]: p["version"] for p in tomllib.loads(path.read_text())["package"]}

    image, tests = locked(root / "extractor" / "uv.lock"), locked(root / "uv.lock")
    image.pop("loom-extractor")
    assert set(image) <= set(tests)
    assert {n: v for n, v in image.items() if tests[n] != v} == {}


async def test_a_child_is_non_dumpable_in_root_with_no_request_on_its_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _child(
        tmp_path,
        monkeypatch,
        """
        import ctypes
        dumpable = ctypes.CDLL(None).prctl(3, 0, 0, 0, 0)  # PR_GET_DUMPABLE
        raise ExtractionError("corrupt", json.dumps([dumpable, sys.argv, os.getcwd(), sys.path[0]]))
        """,
    )
    async with _client() as c:
        r = await c.post("/v1/extract?format=text&name=secret-name.txt", content=b"x", headers=AUTH)
    dumpable, argv, cwd, first = json.loads(r.json()["error"]["message"])
    # Without -P, `python -m` would put the working directory ("/") first.
    assert dumpable == 0 and cwd == "/" and first == str(tmp_path)
    assert "secret-name" not in json.dumps(argv) and json.loads(argv[1]).keys() == {
        "memory_bytes",
        "cpu_s",
    }


@pytest.mark.skipif(os.getuid() == 0, reason="root reads a non-dumpable process's environ")
def test_the_service_process_is_non_dumpable(tmp_path: Path) -> None:
    """What `python -m loom_extractor` does first: a process of the same user
    then cannot read its environment, which holds the token."""
    import subprocess
    import sys
    import time

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from loom_extractor.child import no_dump; no_dump(); import time; time.sleep(30)",
        ],
        env={"PYTHONPATH": service._PACKAGE_ROOT, "LOOM_EXTRACTOR_TOKEN": TOKEN},
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                (Path("/proc") / str(proc.pid) / "environ").read_bytes()
            except PermissionError:
                break
            assert time.monotonic() < deadline, "the process stayed readable"
            time.sleep(0.05)
    finally:
        proc.kill()
        proc.wait()


async def test_a_cancelled_extraction_kills_its_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    _child(
        tmp_path,
        monkeypatch,
        f"""
        open({str(tmp_path)!r} + "/pid-" + str(os.getpid()), "w").close()  # an empty file is allowed
        time.sleep(60)
        """,
    )
    config = service.Config(token=TOKEN)
    task = asyncio.create_task(
        service.run_child(config, b"x", {"format": "text", "name": "a", "options": {}})
    )
    for _ in range(200):
        pids = [int(n[4:]) for n in os.listdir(tmp_path) if n.startswith("pid-")]
        if pids:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)


@pytest.mark.skipif(os.getuid() == 0, reason="the kernel does not hold root to RLIMIT_NPROC")
async def test_a_child_can_start_no_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _child(
        tmp_path,
        monkeypatch,
        """
        try:
            pid = os.fork()
        except OSError as e:
            raise ExtractionError("corrupt", type(e).__name__)
        if pid == 0:
            os._exit(0)
        raise ExtractionError("corrupt", "forked")
        """,
    )
    r = await _extract()
    assert r.json()["error"]["message"] == "BlockingIOError"


async def test_a_child_can_open_no_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The service's network may hold the Loom server (an unauthenticated one, on
    a laptop): a taken-over extraction reaches no address on it or beyond."""
    _child(
        tmp_path,
        monkeypatch,
        """
        import socket
        refused = []
        for family in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX):
            try:
                socket.socket(family)
            except PermissionError:
                refused.append(family.name)
        try:
            socket.socketpair()
        except PermissionError:
            refused.append("pair")
        raise ExtractionError("corrupt", " ".join(refused))
        """,
    )
    r = await _extract()
    assert r.json()["error"]["message"] == "AF_INET AF_INET6 AF_UNIX pair"


def test_a_machine_with_no_socket_filter_refuses_to_extract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from loom_extractor import child

    monkeypatch.setattr(child.platform, "machine", lambda: "riscv64")
    with pytest.raises(OSError, match="no socket filter for riscv64"):
        child.no_sockets()


_SCOPE = {
    "type": "http",
    "asgi": {"version": "3.0"},
    "http_version": "1.1",
    "method": "POST",
    "path": "/v1/extract",
    "raw_path": b"/v1/extract",
    "query_string": b"format=text&name=a.txt",
    "headers": [(b"authorization", AUTH["Authorization"].encode())],
    "client": ("127.0.0.1", 1),
    "server": ("x", 80),
    "scheme": "http",
    "root_path": "",
}


async def test_a_caller_that_disconnects_has_its_child_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    _child(
        tmp_path,
        monkeypatch,
        f"""
        open({str(tmp_path)!r} + "/pid-" + str(os.getpid()), "w").close()
        time.sleep(60)
        """,
    )
    app = service.create_app(service.Config(token=TOKEN))
    started = asyncio.Event()
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        if not started.is_set():
            started.set()
            return {"type": "http.request", "body": b"x", "more_body": False}
        for _ in range(600):  # the child has started once it has left its pid
            if any(n.startswith("pid-") for n in os.listdir(tmp_path)):
                break
            await asyncio.sleep(0.05)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await asyncio.wait_for(app(_SCOPE, receive, send), 30)
    (pid,) = [int(n[4:]) for n in os.listdir(tmp_path) if n.startswith("pid-")]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert sent[0]["status"] == 499


async def test_a_cancelled_handler_stops_its_child_before_it_frees_the_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    _child(
        tmp_path,
        monkeypatch,
        f"""
        open({str(tmp_path)!r} + "/pid-" + str(os.getpid()), "w").close()
        time.sleep(60)
        """,
    )
    app = service.create_app(service.Config(token=TOKEN))
    body = [{"type": "http.request", "body": b"x", "more_body": False}]

    async def receive() -> dict[str, object]:
        if body:
            return body.pop()
        await asyncio.Event().wait()  # the caller stays
        return {}

    async def send(message: dict[str, object]) -> None:
        pass

    handler = asyncio.create_task(app(_SCOPE, receive, send))
    pids: list[int] = []
    for _ in range(600):
        pids = [int(n[4:]) for n in os.listdir(tmp_path) if n.startswith("pid-")]
        if pids:
            break
        await asyncio.sleep(0.05)
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)


async def test_a_handler_cancelled_while_it_reaps_a_gone_callers_child_stays_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    reaping = asyncio.Event()

    async def slow_reap(*args: object) -> bytes:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            reaping.set()
            await asyncio.sleep(0.5)  # the reap the handler's own cancel then interrupts
            raise
        return b""

    monkeypatch.setattr(service, "run_child", slow_reap)
    app = service.create_app(service.Config(token=TOKEN))
    messages = [
        {"type": "http.disconnect"},
        {"type": "http.request", "body": b"x", "more_body": False},
    ]
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return messages.pop()

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    handler = asyncio.create_task(app(_SCOPE, receive, send))
    await asyncio.wait_for(reaping.wait(), 10)
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler
    assert not sent, "no 499 for a handler that was itself cancelled"
