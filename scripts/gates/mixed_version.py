"""Builds N-1 and N side by side on one ClickHouse (a rolling upgrade).

Boots the base build (default: the merge base with origin/main) on an empty
ClickHouse, then this checkout, which applies its newer migrations, then the
base again: the N-1 replica restarting on N's schema. A third process runs
this checkout with a changed dashboard bundle, as N+1's frontend would be.
Then it writes from each build and reads from the other, and checks:

- memories, runs and conflict verdicts one build wrote read back whole on the
  other, and survive the other build's version writes;
- a column only a newer schema has (`ALTER ADD` here, as N+1's migration
  would) survives a version write by a build that does not know it
  (server/core/rmt_carry.py);
- a build that boots on a ledger with migrations it does not carry logs the
  WARNING naming them;
- every asset a build's dashboard page names loads from a replica of the other
  build (needs npm);
- after the cross-boot each build answers ready;
- each build answers `insights.query` on the analyst entities at N's version,
  and no logged statement text or error holds the scope key or its pads
  (both skipped where N-1 has no analysis plane);
- with every process on `LOOM_BACKGROUND_WORKERS=auto`, exactly one process
  runs the workers, and it is N+1: its tree carries one no-op migration more,
  so N hands the lease over to it.

The rules these check are docs/architecture/rolling-upgrades.md. ClickHouse
runs in a throwaway container on its default config, so custom settings take
`SQL_`. `LOOM_CH_CONTAINER` gives the throwaway container an isolated name,
as in `dev/ci_start_clickhouse.sh`. The gate needs docker; without it, or `git`,
it skips with exit 0, or fails with `--require` (CI).

    python scripts/gates/mixed_version.py [--base REV] [--keep] [--require] [--ranking-neutral]
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # run as a file: this checkout's server, not an installed one
    sys.path.insert(0, str(ROOT))

from bench.retrieval_determinism import (  # noqa: E402
    RANKING_OPS,
    assert_rankings_equal,
    capture_rankings,
    ranking_timings,
)
from bench.scale_load import (  # noqa: E402
    _error_code,
    _summary,
    assert_latency_current,
    assert_latency_visible,
    assert_latency_worker,
    committed_latency_rows,
    latency_observations,
)
from scripts.gates._latency_observer import HEADER, parse_metadata, source_hashes  # noqa: E402
from server.core.sql_literal import sql_string  # noqa: E402
from server.insights.domain import analysis as boundary  # noqa: E402

CH_IMAGE = os.environ.get("LOOM_CLICKHOUSE_IMAGE", "clickhouse/clickhouse-server:26.10.4.207")
CH_PASSWORD = "loomdev"
JWT_SECRET = "mixed-version-gate-secret-" + "0" * 32
# How long the carrying processes may take to acknowledge a new column
# (server/core/rmt_carry.py `ACK_WAIT_S`).
CARRY_ACK_WAIT_S = 120.0
# The N+1 bundle: one line more in the entry module changes its hashed name.
NEXT_BUNDLE_LINE = '\nconsole.debug("mixed-version gate: the N+1 bundle");\n'
# How long a published asset or a settled lease may take to show.
SETTLE_S = 90.0


_PORTS: list[int] = []


def _free_port() -> int:
    if _PORTS:
        return _PORTS.pop(0)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _skip(why: str, *, required: bool) -> int:
    if required:
        print(f"mixed-version gate cannot run: {why}")
        return 1
    print(f"mixed-version gate skipped: {why}")
    return 0


@contextlib.contextmanager
def clickhouse(keep: bool) -> Iterator[int]:
    port = _free_port()
    name = os.environ.get("LOOM_CH_CONTAINER") or f"loom-mixed-{os.getpid()}"
    cfg = ROOT / "deploy" / "clickhouse-config"
    mounts = [
        (ROOT / "deploy" / "lemmagen3", "/var/lib/clickhouse/lemmagen3"),
        (cfg / "lemmatizers.xml", "/etc/clickhouse-server/config.d/lemmatizers.xml"),
        (cfg / "nlp_user_settings.xml", "/etc/clickhouse-server/users.d/nlp_user_settings.xml"),
        (cfg / "keeper.xml", "/etc/clickhouse-server/config.d/keeper.xml"),
    ]
    cmd = ["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8123"]
    cmd += ["-e", "CLICKHOUSE_DB=loom", "-e", "CLICKHOUSE_USER=loom"]
    cmd += [
        "-e",
        f"CLICKHOUSE_PASSWORD={CH_PASSWORD}",
        "-e",
        "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1",
    ]
    cmd += ["--ulimit", "nofile=262144:262144"]
    for src, dst in mounts:
        cmd += ["-v", f"{src}:{dst}:ro"]
    subprocess.run([*cmd, CH_IMAGE], check=True, capture_output=True)
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"http://127.0.0.1:{port}/ping", timeout=2).status_code == 200:
                    break
            time.sleep(1)
        else:
            raise RuntimeError("ClickHouse did not start")
        yield port
    finally:
        if not keep:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def sql(
    port: int,
    query: str,
    params: dict[str, str] | None = None,
    settings: dict[str, Any] | None = None,
) -> str:
    """`query`'s answer; `params` bind its `{name:Type}` placeholders. A
    setting that changes how the statement is logged goes with the request,
    since in a SETTINGS clause it does not cut the statement's own text."""
    bound = {f"param_{k}": v for k, v in (params or {}).items()}
    r = httpx.post(
        f"http://127.0.0.1:{port}/",
        params={"user": "loom", "password": CH_PASSWORD, **bound, **(settings or {})},
        content=query.encode(),
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"ClickHouse: {r.text[:400]}")
    return r.text.strip()


def worktree_copy(into: Path) -> Path:
    """This checkout's files, tracked and untracked (not ignored), uncommitted edits included."""
    names = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    for name in filter(None, names):
        src = ROOT / name.decode()
        if src.is_file():
            dst = into / name.decode()
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    return into


def add_noop_migration(tree: Path) -> str:
    """One migration more than `tree` carries, so it boots with a higher
    schema_version, as N+1 would."""
    migrations = tree / "schemas" / "migrations"
    last = max(int(f.name[:3]) for f in migrations.glob("[0-9][0-9][0-9]_*.sql"))
    name = f"{last + 1:03d}_mixed_version_gate.sql"
    (migrations / name).write_text(
        "-- The mixed-version gate's N+1: no schema change.\nSELECT 1;\n"
    )
    return name


def build_frontend(tree: Path, *, node_modules: Path | None = None) -> bool:
    """`frontend/dist` for a tree that has none; False without npm."""
    front = tree / "frontend"
    if (front / "dist" / "index.html").exists():
        return True
    if not shutil.which("npm") or not (front / "package.json").exists():
        return False
    if not (front / "node_modules").exists():
        if node_modules is not None and node_modules.exists():
            (front / "node_modules").symlink_to(node_modules)
        else:
            subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=front, check=True)
    subprocess.run(["npm", "run", "build"], cwd=front, check=True, capture_output=True)
    return True


def base_tree(rev: str, into: Path) -> Path:
    into.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "-C", str(ROOT), "archive", rev], check=True, capture_output=True
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(into)], input=archive, check=True)
    return into


class Server:
    """One Loom process of one build; `restart` keeps its port."""

    def __init__(
        self,
        name: str,
        tree: Path,
        ch_port: int,
        logs: Path,
        *,
        ranking_neutral: bool = False,
        web_workers: int = 1,
        stage_observer: bool = False,
    ) -> None:
        assert web_workers in (1, 2)
        self.web_workers = web_workers
        assert not stage_observer or web_workers == 2
        self.stage_observer = stage_observer
        self.stage_sources = source_hashes(tree) if stage_observer else {}
        self.name, self.tree, self.ch_port = name, tree, ch_port
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.log = logs / f"{name}.log"
        self.proc: subprocess.Popen[bytes] | None = None
        self.workers = "off"
        self.ranking_neutral = ranking_neutral

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("LOOM_")}
        env.update(
            PYTHONPATH=str(self.tree),
            LOOM_CH_HOST="127.0.0.1",
            LOOM_CH_PORT=str(self.ch_port),
            LOOM_CH_USER="loom",
            LOOM_CH_PASSWORD=CH_PASSWORD,
            LOOM_CH_SECURE="false",
            LOOM_AUTH_REQUIRED="true",
            LOOM_OAUTH_ENABLED="true",  # a sign-in on every build, so they answer each other's
            LOOM_JWT_SECRET=JWT_SECRET,
            LOOM_EMBEDDING_MODEL="fake-ngram-1024",
            LOOM_LLM_PROVIDER="fake",
            LOOM_BACKGROUND_WORKERS=self.workers,
            # The analysis probe's refused reads, which the gate searches the logs for.
            LOOM_CH_ANALYSIS_PROBE_INTERVAL_S="5",
            LOOM_LATENCY_BUDGET_MULTIPLIER="10",
            # Low enough that the fake embedder judges the two region facts close.
            LOOM_CONFLICT_THRESHOLD="0.5",
            LOOM_OTEL_ENABLED="false",
            LOOM_INSTANCE_LABEL=f"ci:mixed-version-{self.name}",
        )
        if self.web_workers != 1:
            env["LOOM_WEB_WORKERS"] = str(self.web_workers)
            env["LOOM_RUNTIME_DIR"] = str(self.log.parent / (self.name + ".runtime"))
        if self.ranking_neutral:
            env.update(
                LOOM_MEMORY_ASK_RECENCY_WEIGHT="0", LOOM_MEMORY_SEARCH_RECENCY_POOL_WEIGHT="0"
            )
        if self.workers != "off":
            env["LOOM_DEPLOYMENT"] = "1"  # one shared lease, not one per instance label
        return env

    def start(self) -> None:
        if self.web_workers != 1:
            (self.log.parent / (self.name + ".runtime")).mkdir(mode=0o700, exist_ok=True)
        target = ["server.main:app"]
        if self.stage_observer:
            target = [
                "_latency_observer:create_app",
                "--factory",
                "--app-dir",
                str(ROOT / "scripts" / "gates"),
            ]
        out = self.log.open("ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", *target]
            + ["--host", "127.0.0.1", "--port", str(self.port)]
            + (["--workers", str(self.web_workers)] if self.web_workers != 1 else []),
            cwd=self.tree,
            env=self.env(),
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # its workers child stops with it
        )
        deadline = time.monotonic() + 600  # an empty database replays the journal
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.name} exited at boot:\n{self.tail()}")
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"{self.url}/v1/health", timeout=2).status_code == 200:
                    return
            time.sleep(1)
        raise RuntimeError(f"{self.name} never became healthy:\n{self.tail()}")

    def ready(self) -> tuple[bool, list[str]]:
        """Whether `/v1/health/ready` answers 200 within SETTLE_S, and the
        checks that failed last."""
        deadline = time.monotonic() + SETTLE_S
        failing: list[str] = []
        while True:
            with contextlib.suppress(httpx.HTTPError, ValueError):
                r = httpx.get(f"{self.url}/v1/health/ready", timeout=10)
                if r.status_code == 200:
                    return True, []
                failing = list(r.json().get("failing") or [r.status_code])
            if time.monotonic() > deadline:
                return False, failing
            time.sleep(2)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(30)
        if self.proc:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()
        self.proc = None

    def restart(self) -> None:
        self.stop()
        self.start()

    def tail(self, n: int = 6000) -> str:
        return self.log.read_text(errors="replace")[-n:] if self.log.exists() else ""

    def call(
        self,
        token: str,
        op: str,
        body: dict[str, Any],
        *,
        observation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter() if observation is not None else 0.0
        if observation is not None:
            observation["started_unix_ns"] = time.time_ns()
            observation["started_monotonic_s"] = started
        try:
            if observation is None:
                r = httpx.post(
                    f"{self.url}/v1/{op}",
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=60,
                )
            else:
                connection: dict[str, float] = {}
                observation["connection_events_ms"] = connection

                def trace(name: str, _info: dict[str, Any]) -> None:
                    if name in (
                        "connection.connect_tcp.started",
                        "connection.connect_tcp.complete",
                        "connection.connect_tcp.failed",
                        "http11.send_request_body.complete",
                        "http11.receive_response_headers.complete",
                    ):
                        connection[name] = (time.perf_counter() - started) * 1000

                setup = time.perf_counter()
                # Match httpx.post: a fresh default client, closed inside elapsed_ms.
                with httpx.Client(timeout=60) as client:
                    observation["client_setup_ms"] = (time.perf_counter() - setup) * 1000
                    r = client.post(
                        f"{self.url}/v1/{op}",
                        json=body,
                        headers={"Authorization": f"Bearer {token}"},
                        extensions={"trace": trace},
                    )
        except httpx.HTTPError as exc:
            if observation is not None:
                observation.update(
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    error=type(exc).__name__,
                    finished_unix_ns=time.time_ns(),
                )
            raise
        if observation is not None:
            observation.update(
                elapsed_ms=(time.perf_counter() - started) * 1000,
                status=r.status_code,
                error="" if r.status_code == 200 else f"{r.status_code}:{_error_code(r)}",
                finished_unix_ns=time.time_ns(),
            )
            try:
                observation["httpx_elapsed_ms"] = r.elapsed.total_seconds() * 1000
                request_id = r.headers.get("X-Request-Id", "")
                if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", request_id):
                    observation["request_id"] = request_id
                for name, duration in re.findall(
                    r"(?:^|,\s*)(app|ch);dur=([0-9]+(?:\.[0-9]+)?)",
                    r.headers.get("Server-Timing", ""),
                ):
                    observation[f"server_{name}_ms"] = float(duration)
                if self.stage_observer:
                    observation["server_stages"] = parse_metadata(
                        r.headers.get(HEADER, ""), self.stage_sources
                    )
            except Exception as error:  # noqa: BLE001 — timing metadata cannot change the response
                observation["timing_error"] = type(error).__name__
        if r.status_code != 200:
            raise AssertionError(f"{self.name} {op}: {r.status_code} {r.text[:400]}")
        return dict(r.json())

    def page_assets(self) -> list[str]:
        """The `/assets/...` files this replica's dashboard page names."""
        page = httpx.get(f"{self.url}/", timeout=30).text
        return sorted(set(re.findall(r'(?:src|href)="(/assets/[^"]+)"', page)))

    def leader(self) -> tuple[bool, str]:
        r = httpx.get(f"{self.url}/v1/health", params={"include": "workers"}, timeout=10)
        lease = dict(r.json().get("workers_leader") or {})
        reason = str(lease.get("reason") or "")
        return bool(lease.get("held")) and reason.startswith("holds "), reason


def mint(org: str, ns: str) -> str:
    os.environ.setdefault("LOOM_JWT_SECRET", JWT_SECRET)
    import jwt

    from server.auth.jwt import mint_token

    raw = mint_token(
        subject="sa:mixed-version",
        org=org,
        namespaces=[ns],
        roles=["admin"],
        ttl_seconds=3600,
        secret=JWT_SECRET,
    )
    # Signed as kid `default` (the root secret), which every build verifies.
    claims = jwt.decode(raw, JWT_SECRET, algorithms=["HS256"], options={"verify_aud": False})
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256", headers={"kid": "default"})


class Scenario:
    """The checks, each run in both directions between the two builds."""

    def __init__(
        self,
        old: Server,
        new: Server,
        nxt: Server,
        ch_port: int,
        *,
        owned_servers: list[Server] | None = None,
        owned_logs: Path | None = None,
    ) -> None:
        self.owned_servers, self.owned_logs = owned_servers, owned_logs
        self.builds = {"N-1": old, "N": new}
        self.new = new
        self.next = nxt
        self.ch_port = ch_port
        self.org = f"mixed-{uuid.uuid4().hex[:8]}"
        self.ident = {"org": self.org, "namespace": "mixed", "agent": "gate"}
        self.token = mint(self.org, "mixed")
        self.failures: list[str] = []

    def check(self, cond: bool, what: str) -> None:
        print(("  ok    " if cond else "  FAIL  ") + what)
        if not cond:
            self.failures.append(what)

    def call(self, build: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.builds[build].call(self.token, op, body)

    def get(self, build: str, key: str) -> dict[str, Any]:
        out = self.call(build, "memory.get", {**self.ident, "memory_key": key})
        return dict(out.get("memory") or out)

    def run_all(self, *, ranking_sources: tuple[str, str] | None = None) -> list[str]:
        self.all_ready()
        if ranking_sources is not None:
            self.ranking_neutrality(*ranking_sources)
            self.latency_acceptance(*ranking_sources)
        ids = self.memories_read_across()
        self.absence_precondition_across()
        for a, b in (("N", "N-1"), ("N-1", "N")):
            self.run_completes_across(a, b)
            self.verdict_survives(a, b)
        self.unknown_column_survives(ids)
        self.newer_ledger_warns()
        self.analysis_across()
        self.sign_in_across()
        self.dashboard_loads_across()
        self.one_leader()
        return self.failures

    def latency_acceptance(self, base: str, head: str) -> None:
        """Small CI timings, two two-worker groups per arm; no production latency claim."""
        from bench.scale_load import _outcomes

        assert self.owned_servers is not None and self.owned_logs is not None
        policies = {}

        def value(key: str) -> str:
            # Alphabetic codewords avoid token/card/contact shapes without policy overrides.
            digest = hashlib.sha256(("latency-fixture/" + key).encode()).hexdigest()
            letters = digest.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
            return " ".join(letters[i : i + 16] for i in range(0, 64, 16))

        corpus = {f"seed/{i:04}": value(f"seed/{i:04}") for i in range(2048)}
        light = {f"light/{i:02}": value(f"light/{i:02}") for i in range(16)}
        # Both cohorts exist before timing; head does not add its seed corpus after base.
        # Measured writes still add heads between arms, so physical snapshots can differ.
        prepared: dict[str, list[Any]] = {}
        for label in ("base", "head"):
            fixtures = []
            for role, contents in (("main", corpus), ("light", light)):
                org = f"{self.org}-{label}-{role}"
                ident = {
                    "org": org,
                    "namespace": "latency",
                    "agent": "gate",
                    "user_id": "-",
                    "session_id": "-",
                }
                token = mint(org, "latency")
                expected = {}
                entries = [
                    {"memory_key": k, "value": v, "memory_type": "semantic"}
                    for k, v in contents.items()
                ]
                for offset in range(0, len(entries), 8):
                    seed_started = time.monotonic()
                    batch = entries[offset : offset + 8]
                    result = self.builds["N-1"].call(
                        token, "memory.bulk_set", {**ident, "entries": batch}
                    )
                    expected.update(committed_latency_rows("memory.bulk_set", result, batch))
                    # Four requests/sec is below the ordinary namespace write rate.
                    time.sleep(max(0, 0.25 - (time.monotonic() - seed_started)))
                fixture = (role, ident, token, expected)
                fixtures.append(fixture)
            prepared[label] = fixtures
        for label, source in (("base", base), ("head", head)):
            tree = self.builds["N-1" if label == "base" else "N"].tree
            arm = [
                Server(
                    f"latency-{label}-{i}",
                    tree,
                    self.ch_port,
                    self.owned_logs,
                    ranking_neutral=True,
                    web_workers=2,
                    stage_observer=True,
                )
                for i in range(2)
            ]
            # Registration precedes every start, including a partially failed start.
            self.owned_servers.extend(arm)
            samples: list[dict[str, Any]] = []
            visibility: dict[str, list[float]] = {
                f"{role}/{op}": []
                for role in ("main", "light")
                for op in ("memory.set", "memory.bulk_set")
            }
            proofs = []
            proofs_after = []
            fixtures = prepared[label]
            complete = False
            try:
                for srv in arm:
                    srv.start()
                    assert srv.ready()[0]
                    proofs.append(self.latency_workers(srv))
                assert len({w["pid"] for p in proofs for w in p["workers"]}) == 4
                for _role, ident, token, expected in fixtures:
                    for srv in arm:
                        self.latency_census(srv, ident, token, expected)

                def exercise(
                    fixture: tuple[Any, ...],
                    *,
                    owned_arm: list[Server] = arm,
                    arm_samples: list[dict[str, Any]] = samples,
                    arm_visibility: dict[str, list[float]] = visibility,
                ) -> None:
                    role, ident, token, expected = fixture
                    writer, reader = owned_arm

                    def call(
                        target: Server, op: str, body: dict[str, Any], phase: str, check: Any = None
                    ) -> dict[str, Any]:
                        sample: dict[str, Any] = {
                            "role": role,
                            "op": op,
                            "phase": phase,
                            "server": target.name,
                        }
                        sample["request_sha256"] = hashlib.sha256(
                            json.dumps(body, sort_keys=True).encode()
                        ).hexdigest()
                        arm_samples.append(sample)
                        try:
                            result = target.call(token, op, {**ident, **body}, observation=sample)
                            if op in ("memory.set", "memory.bulk_set"):
                                sample["outcomes"] = _outcomes(op, result)
                                committed_latency_rows(
                                    op, result, [body] if op == "memory.set" else body["entries"]
                                )
                            if check is not None:
                                check(result)
                            return result
                        except (AssertionError, httpx.HTTPError, ValueError, TypeError) as exc:
                            sample["error"] = sample.get("error") or type(exc).__name__
                            raise

                    def search(key: str, row: dict[str, Any], phase: str) -> None:
                        call(
                            reader,
                            "memory.search",
                            {
                                "query": row["value"],
                                "top_k": 5,
                                "namespace_scope": "this",
                                "search_mode": "semantic",
                                "auto_touch": False,
                                "personalization": "none",
                                "reinforcement_weight": 0,
                                "recency_pool_weight": 0,
                            },
                            phase,
                            lambda found: assert_latency_visible(found, key, row),
                        )

                    for i in range(8 if role == "main" else 4):
                        key = next(iter(expected))
                        call(
                            reader,
                            "memory.get",
                            {"memory_key": key},
                            "measured",
                            lambda current, row=expected[key]: assert_latency_current(current, row),
                        )
                        for op, count in (("memory.set", 1), ("memory.bulk_set", 2)):
                            entries = [
                                {
                                    "memory_key": f"{op}/{i}/{j}",
                                    "value": value(f"{role}/{op}/{i}/{j}"),
                                    "memory_type": "semantic",
                                }
                                for j in range(count)
                            ]
                            body = entries[0] if count == 1 else {"entries": entries}
                            result = call(writer, op, body, "measured")
                            ack = time.perf_counter()
                            committed = committed_latency_rows(op, result, entries)
                            expected.update(committed)
                            # No settle loop: the FIRST post-ack search must be current.
                            for new_key, row in committed.items():
                                search(new_key, row, "first_after_ack")
                            arm_visibility[f"{role}/{op}"].append(
                                (time.perf_counter() - ack) * 1000
                            )
                            for new_key, row in committed.items():
                                call(
                                    reader,
                                    "memory.get",
                                    {"memory_key": new_key},
                                    "first_point_after_ack",
                                    lambda current, row=row: assert_latency_current(current, row),
                                )
                        search(key, expected[key], "measured")

                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(exercise, fixture) for fixture in fixtures]
                    for future in futures:
                        future.result()
                for _role, ident, token, expected in fixtures:
                    for srv in arm:
                        self.latency_census(srv, ident, token, expected)
                proofs_after = [self.latency_workers(srv) for srv in arm]
                assert {(w["pid"], w["start_ticks"]) for p in proofs for w in p["workers"]} == {
                    (w["pid"], w["start_ticks"]) for p in proofs_after for w in p["workers"]
                }, "web worker restarted during measurement"
                policies[label] = [w["ratelimit"] for p in proofs_after for w in p["workers"]]
                assert all(p == policies[label][0] for p in policies[label]), (
                    "unequal worker policy"
                )
                if label == "head":
                    assert policies["base"] == policies["head"], "different arm rate/process shares"
                complete = True
            finally:
                # Cleanup precedes formatting; even a malformed receipt cannot strand an arm.
                try:
                    arm[0].stop()
                finally:
                    arm[1].stop()
                timing_evidence = self.latency_timing_evidence(samples, fixtures)
                print(
                    "latency-acceptance "
                    + json.dumps(
                        {
                            "source": source,
                            "arm": label,
                            "authority": "clickhouse",
                            "complete": complete,
                            "providers": "fake-ngram-1024/fake",
                            "process_groups": proofs,
                            "process_groups_after": proofs_after,
                            "main_seed_rows": len(corpus),
                            "light_seed_rows": len(light),
                            "fixtures": [
                                {"role": role, "identity": ident, "committed_head_count": len(rows)}
                                for role, ident, _token, rows in fixtures
                            ],
                            "corpus_sha256": hashlib.sha256(
                                json.dumps(
                                    {"main": corpus, "light": light}, sort_keys=True
                                ).encode()
                            ).hexdigest(),
                            "summary": {
                                role: {
                                    op: latency_observations(selected)
                                    for op in (
                                        "memory.get",
                                        "memory.set",
                                        "memory.bulk_set",
                                        "memory.search",
                                    )
                                    if (
                                        selected := [
                                            s
                                            for s in samples
                                            if s["role"] == role
                                            and s["op"] == op
                                            and s["phase"] == "measured"
                                        ]
                                    )
                                }
                                for role in ("main", "light")
                            },
                            "attempts": samples,
                            "timing_evidence": timing_evidence,
                            "ack_to_first_search_samples_ms": visibility,
                            "normal_load_errors": {
                                "attempted": len(samples),
                                "errors": {
                                    error: sum(s.get("error") == error for s in samples)
                                    for error in sorted(
                                        {s["error"] for s in samples if s.get("error")}
                                    )
                                },
                                "lag_errors": {
                                    code: sum(s.get("error") == "503:" + code for s in samples)
                                    for code in ("PROJECTION_BEHIND", "PUBLICATION_PENDING")
                                },
                                "lag_error_rate": (
                                    sum(
                                        s.get("error")
                                        in ("503:PROJECTION_BEHIND", "503:PUBLICATION_PENDING")
                                        for s in samples
                                    )
                                    / len(samples)
                                    if samples
                                    else None
                                ),
                            },
                            "ack_to_first_search_ms": {
                                op: _summary(ms, {}) for op, ms in visibility.items()
                            },
                            "visibility_definition": (
                                "ack to all first serial searches; bulk=two rows; no retry"
                            ),
                            "small_n_only": True,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )

    def latency_timing_evidence(
        self, samples: list[dict[str, Any]], fixtures: list[Any]
    ) -> dict[str, Any]:
        """Read bounded timing witnesses after the measured server groups stop."""
        timing_evidence: dict[str, Any] = {}
        try:
            ids = list(dict.fromkeys(s["request_id"] for s in samples if s.get("request_id")))
            timing_evidence["request_count"] = len(ids)
            if len(ids) > 120:
                timing_evidence["error"] = "request_limit"
            elif ids:
                params = {
                    "ids": "[" + ", ".join(map(sql_string, ids)) + "]",
                    "orgs": "[" + ", ".join(sql_string(f[1]["org"]) for f in fixtures) + "]",
                }
                bounds = {
                    "max_execution_time": 5,
                    "max_rows_to_read": 1_000_000,
                    "max_memory_usage": 64_000_000,
                    "max_result_rows": 121,
                    "result_overflow_mode": "throw",
                }
                sql(self.ch_port, "SYSTEM FLUSH LOGS", settings=bounds)
                statements = {
                    "requests": (
                        "SELECT request_id, operation, toString(ts) AS emitted_at, "
                        "latency_ms, instance, instance_label, build_commit, "
                        "args['out_ch_elapsed_ms'] AS ch_elapsed_ms, "
                        "args['out_ch_statements'] AS ch_statements "
                        "FROM loom.agent_events WHERE kind = 'request' "
                        "AND org IN {orgs:Array(String)} AND namespace = 'latency' "
                        "AND request_id IN {ids:Array(String)} "
                        "ORDER BY request_id LIMIT 121 FORMAT JSONEachRow"
                    ),
                    "native": (
                        "SELECT splitByChar(' ', log_comment)[2] AS request_id, "
                        "count() AS statements, "
                        "toString(min(query_start_time_microseconds)) AS first_start, "
                        "toString(max(event_time_microseconds)) AS last_end, "
                        "sum(query_duration_ms) AS summed_ms, max(query_duration_ms) AS max_ms, "
                        "argMax(query_id, query_duration_ms) AS slowest_query_id, "
                        "sum(read_rows) AS read_rows, sum(read_bytes) AS read_bytes, "
                        "max(memory_usage) AS max_memory_bytes "
                        "FROM system.query_log WHERE is_initial_query "
                        "AND type IN ('QueryFinish', 'ExceptionBeforeStart', "
                        "'ExceptionWhileProcessing') "
                        "AND request_id IN {ids:Array(String)} "
                        "GROUP BY request_id ORDER BY request_id LIMIT 121 FORMAT JSONEachRow"
                    ),
                    "statement_costs": (
                        "SELECT splitByChar(' ', log_comment)[2] AS request_id, "
                        "count() AS total_statements, toUInt8(total_statements > 64) AS overflow, "
                        "arraySort(s -> (s.1, s.2, s.3), groupArray(64)(("
                        "toUnixTimestamp64Micro(query_start_time_microseconds), "
                        "toUnixTimestamp64Micro(event_time_microseconds), "
                        "if(startsWith(query_id, concat(request_id, ':')), "
                        "toUInt64OrZero(splitByChar(':', query_id)[2]), 0), "
                        "normalized_query_hash, query_duration_ms, read_rows, read_bytes, "
                        "memory_usage, toUInt8(type), exception_code, "
                        "toFloat64OrNull(Settings['max_execution_time']), "
                        "toUInt64OrNull(Settings['max_memory_usage']), "
                        "toUInt64OrNull(Settings['max_rows_to_read']), "
                        "if(mapContains(Settings, 'read_overflow_mode'), "
                        "toUInt8(Settings['read_overflow_mode'] = 'throw'), NULL)"
                        "))) AS statements "
                        "FROM system.query_log WHERE is_initial_query "
                        "AND type IN ('QueryFinish', 'ExceptionBeforeStart', "
                        "'ExceptionWhileProcessing') "
                        "AND request_id IN {ids:Array(String)} "
                        "GROUP BY request_id ORDER BY request_id LIMIT 121 FORMAT JSONEachRow"
                    ),
                }
                for name, statement in statements.items():
                    limits = bounds
                    if name == "statement_costs":
                        limits = {
                            **bounds,
                            "max_result_bytes": 2_097_152,
                            "output_format_json_quote_64bit_integers": 0,
                        }
                    raw = sql(self.ch_port, statement, params, limits)
                    if name == "statement_costs" and len(raw.encode()) > 2_097_152:
                        timing_evidence["statement_cost_error"] = "output_limit"
                        continue
                    timing_evidence[name] = [json.loads(line) for line in raw.splitlines()]
                    if name == "statement_costs" and any(
                        row["overflow"] for row in timing_evidence[name]
                    ):
                        timing_evidence["statement_cost_error"] = "statement_limit"
        except Exception as error:  # noqa: BLE001 — evidence cannot replace a workload failure
            timing_evidence["error"] = type(error).__name__
        return timing_evidence

    def latency_workers(self, srv: Server) -> dict[str, Any]:
        assert srv.proc is not None and srv.web_workers == 2 and srv.workers == "off"
        seen = {}
        deadline = time.monotonic() + SETTLE_S
        while len(seen) < 2 and time.monotonic() < deadline:
            response = httpx.get(
                f"{srv.url}/v1/health", headers={"Connection": "close"}, timeout=10
            )
            assert response.status_code == 200
            health = response.json()
            assert health.get("fake_embedding") is True and health.get("fake_llm") is True
            assert health.get("storage_authority") == "clickhouse"
            assert health.get("instance_label") == f"ci:mixed-version-{srv.name}"
            pid = int(health["instance"].rsplit(":", 1)[1])
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            assert_latency_worker(pid, srv.proc.pid, int(stat[1]), os.getpgid(pid))
            assert health.get("participant_id")
            policy = health["ratelimit"]
            assert policy.get("web_workers") == 2 and not policy.get("error")
            seen[pid] = {
                "pid": pid,
                "start_ticks": stat[19],
                "participant_id": health["participant_id"],
                "instance": health["instance"],
                "ratelimit": {
                    k: policy[k]
                    for k in (
                        "backend",
                        "divisor",
                        "replica_count",
                        "replica_count_floor",
                        "web_workers",
                        "class_fractions",
                    )
                },
            }
            if len(seen) < 2:
                time.sleep(0.01)
        assert len(seen) == 2, "did not observe two actual owned web workers"
        return {
            "parent_pid": srv.proc.pid,
            "server": srv.name,
            "workers": list(seen.values()),
            "tree": str(srv.tree),
            "command": srv.proc.args,
        }

    def latency_census(
        self, srv: Server, ident: dict[str, Any], token: str, expected: dict[str, Any]
    ) -> None:
        count = srv.call(token, "memory.list", {**ident, "count_only": True})
        assert count.get("total") == len(expected), "fixture has extra/missing heads"
        keys = list(expected)
        for offset in range(0, len(keys), 64):
            batch = keys[offset : offset + 64]
            got = srv.call(token, "memory.bulk_get", {**ident, "memory_keys": batch})
            assert not got.get("missing") and set(got.get("results", {})) == set(batch)
            for key, row in got["results"].items():
                assert_latency_current(row, expected[key])

    def ranking_neutrality(self, base: str, head: str) -> None:
        """Compare the real base and head readers over one immutable fake-vector corpus."""
        corpus = {
            "orchard": "Amber orchard fruit harvest",
            "garden": "Green garden seed watering",
            "harbor": "Quiet harbor fishing boats",
            "river": "Swift river stone crossing",
            "summit": "Snowy summit mountain trail",
            "valley": "Sunny valley meadow flowers",
        }
        positives = {corpus[key]: key for key in ("orchard", "harbor", "summit")}
        identity = {**self.ident, "namespace": "ranking"}
        token = mint(self.org, "ranking")
        for srv in self.builds.values():
            health = httpx.get(f"{srv.url}/v1/health", timeout=10)
            assert health.status_code == 200
            assert health.json().get("fake_embedding") is True
            assert health.json().get("fake_llm") is True
            assert srv.ranking_neutral and srv.workers == "off"
        for key, value in corpus.items():
            written = self.builds["N-1"].call(
                token,
                "memory.set",
                {
                    **identity,
                    "memory_key": key,
                    "memory_type": "semantic",
                    "value": value,
                },
            )
            assert written.get("memory_key") == key and written.get("status") == "active"
            assert written.get("version", 0) > 0 and written.get("noop") is False
        anchor = datetime.now(UTC)
        common = {
            **identity,
            "top_k": len(corpus),
            "namespace_scope": "this",
            "search_mode": "semantic",
            "personalization": "none",
            "reinforcement_weight": 0,
            "as_of": anchor.isoformat(),
            "observation_date": anchor.date().isoformat(),
        }
        census = {**common, "query": next(iter(positives)), "auto_touch": False}
        pending = dict(self.builds)
        deadline = time.monotonic() + SETTLE_S
        while pending:
            for label, srv in list(pending.items()):
                rows = srv.call(token, "memory.search_trace", census).get("results")
                assert isinstance(rows, list), "missing fixture census"
                keys = [row["memory_key"] for row in rows]
                assert len(keys) == len(set(keys)) and set(keys) <= set(corpus)
                if set(keys) == set(corpus):
                    pending.pop(label)
            assert not pending or time.monotonic() < deadline, "incomplete ranking fixture census"
            if pending:
                time.sleep(1)
        requests = {
            op: {
                query: {
                    **common,
                    "query": query,
                    **(
                        {
                            "allow_synthesis": False,
                            "reinforce": False,
                            "precedents": False,
                            "decompose": "off",
                            "world_knowledge": "forbid",
                            "personalize": False,
                            "knowledge": False,
                            "llm_cache": False,
                            "depth": "excerpt",
                        }
                        if op == "memory.ask"
                        else {"auto_touch": False, "recency_pool_weight": 0}
                    ),
                }
                for query in positives
            }
            for op in RANKING_OPS
        }
        snapshots = {}
        elapsed: dict[str, dict[str, dict[str, float]]] = {}
        native = {}
        for label, srv in self.builds.items():
            since = sql(self.ch_port, "SELECT toString(now64(6))")
            elapsed[label] = {}
            snapshots[label] = capture_rankings(
                lambda op, body, srv=srv: srv.call(token, op, body),
                requests,
                positives,
                elapsed=elapsed[label],
            )
            until = sql(self.ch_port, "SELECT toString(now64(6))")
            native[label] = self.ranking_native_settings(since, until, required=label == "N")
        assert_rankings_equal(snapshots["N-1"], snapshots["N"], queries=list(positives))
        for snapshot in snapshots.values():
            for queries in snapshot.values():
                for rows in queries.values():
                    keys = [row["identity"]["memory_key"] for row in rows]
                    assert len(keys) == len(set(keys)) and set(keys) <= set(corpus)

        def digest(value: Any) -> str:
            return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

        print(
            "ranking-neutral "
            + json.dumps(
                {
                    "sources": {"base": base, "head": head},
                    "admission_sources": {
                        label: {
                            "present": (srv.tree / "server/core/vectors/admission.py").is_file(),
                            "sha256": hashlib.sha256(
                                (srv.tree / "server/core/vectors/admission.py").read_bytes()
                            ).hexdigest()
                            if (srv.tree / "server/core/vectors/admission.py").is_file()
                            else None,
                            "catalog_sha256": hashlib.sha256(
                                (srv.tree / "server/settings/catalog.py").read_bytes()
                            ).hexdigest(),
                        }
                        for label, srv in self.builds.items()
                    },
                    "corpus_sha256": digest(corpus),
                    "requests_sha256": digest(requests),
                    "identity": identity,
                    "profile": {
                        "embedding_model": "fake-ngram-1024",
                        "llm_provider": "fake",
                        "workers": "off",
                        "ask_recency_weight": 0,
                        "search_recency_pool_weight": 0,
                    },
                    "native_settings": native,
                    "admission_credits": (
                        "not measured; native settings witness attributed execution only"
                    ),
                    "timing_method": (
                        "three queries per op/build; p99 nearest rank; no statistical guarantee"
                    ),
                    "timings": {label: ranking_timings(calls) for label, calls in elapsed.items()},
                    "snapshots": snapshots,
                },
                sort_keys=True,
            )
        )
        self.check(True, "base and head preserve all three ordered retrieval results and scores")

    def ranking_native_settings(
        self, since: str, until: str, *, required: bool
    ) -> list[dict[str, Any]]:
        """Audit native settings on the compared reads, not source presence alone."""
        names = ("max_rows_to_read", "max_memory_usage", "max_execution_time", "read_overflow_mode")
        defaults = {
            row["name"]: row["default"]
            for row in map(
                json.loads,
                sql(
                    self.ch_port,
                    "SELECT name, default FROM system.settings WHERE name IN "
                    "('max_rows_to_read','max_memory_usage',"
                    "'max_execution_time','read_overflow_mode') "
                    "FORMAT JSONEachRow",
                ).splitlines(),
            )
        }
        assert set(defaults) == set(names), "missing native setting defaults"
        sql(self.ch_port, "SYSTEM FLUSH LOGS")
        raw = sql(
            self.ch_port,
            "SELECT splitByChar(' ', log_comment)[1] AS operation, "
            "Settings['max_rows_to_read'] AS max_rows_to_read, "
            "Settings['max_memory_usage'] AS max_memory_usage, "
            "Settings['max_execution_time'] AS max_execution_time, "
            "Settings['read_overflow_mode'] AS read_overflow_mode, count() AS queries "
            "FROM system.query_log WHERE type = 'QueryFinish' AND is_initial_query "
            "AND event_time_microseconds BETWEEN {since:DateTime64(6)} AND {until:DateTime64(6)} "
            "AND positionCaseInsensitive(query, 'cosineDistance(') > 0 "
            "AND splitByChar(' ', log_comment)[1] IN "
            "('memory.search','memory.search_trace','memory.ask') "
            "GROUP BY operation, max_rows_to_read, max_memory_usage, max_execution_time, "
            "read_overflow_mode ORDER BY operation, max_rows_to_read FORMAT JSONEachRow",
            {"since": since, "until": until},
        )
        rows = [json.loads(line) for line in raw.splitlines()]
        assert {row["operation"] for row in rows} == set(RANKING_OPS), (
            "missing native read witnesses"
        )
        for row in rows:
            row["limits"] = {name: row.pop(name) or defaults[name] for name in names}
            row["queries"] = int(row["queries"])
            assert row["queries"] > 0
            if required:
                limits = row["limits"]
                assert float(limits["max_rows_to_read"]) == 1_000_000
                assert limits["read_overflow_mode"] == "throw"
                assert 0 < float(limits["max_memory_usage"]) <= 4_000_000_000
                assert 0 < float(limits["max_execution_time"]) <= 30
        return [{"window": {"since": since, "until": until}, **row} for row in rows]

    def all_ready(self) -> None:
        for label, srv in {**self.builds, "N+1": self.next}.items():
            ok, failing = srv.ready()
            self.check(
                ok,
                f"{label} answers ready after the cross-boot"
                + (f" ({failing})" if failing else ""),
            )

    def memories_read_across(self) -> dict[str, str]:
        ids = {}
        for label in self.builds:
            body = {**self.ident, "memory_key": f"facts/{label}", "value": f"by {label}"}
            ids[label] = str(self.call(label, "memory.set", body)["memory_id"])
        for label, other in (("N", "N-1"), ("N-1", "N")):
            got = self.get(label, f"facts/{other}")
            self.check(got.get("value") == f"by {other}", f"{label} reads what {other} wrote")
        return ids

    def absence_precondition_across(self) -> None:
        for label in ("N", "N-1"):
            body = {
                **self.ident,
                "memory_key": f"conditional/{label}",
                "memory_type": "episodic",
                "value": {"first": True},
                "if_match_sha256": "absent",
                "clear_expiry": True,
            }
            srv = self.builds[label]
            reply = httpx.post(
                f"{srv.url}/v1/memory.set",
                json=body,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=60,
            )
            self.check(
                reply.status_code == 200 if label == "N" else reply.status_code in (200, 409),
                f"{label} accepts absence or refuses it without an unconditional write",
            )
            if reply.status_code == 409:
                self.check(
                    reply.json().get("detail", {}).get("code") == "PRECONDITION_FAILED",
                    f"{label} refuses unsupported absence as a precondition failure",
                )
                self.check(
                    not self.get("N", body["memory_key"])["found"],
                    f"{label} leaves the absent key untouched on refusal",
                )
                self.call("N", "memory.set", body)
            changed = httpx.post(
                f"{srv.url}/v1/memory.set",
                json={**body, "value": {"clobbered": True}},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=60,
            )
            self.check(changed.status_code == 409, f"{label} refuses absence when a row exists")
            other = "N-1" if label == "N" else "N"
            row = self.get(other, body["memory_key"])
            self.check(
                row.get("value") == {"first": True},
                f"{other} reads the conditional creation unchanged",
            )
            self.call(
                other,
                "memory.set",
                {
                    **body,
                    "value": {"revised": True},
                    "if_match_sha256": row["content_sha256"],
                },
            )
            self.check(
                self.get(label, body["memory_key"]).get("value") == {"revised": True},
                f"{label} reads the other build's hash-conditional revision",
            )

    def analysis_across(self) -> None:
        if not (self.builds["N-1"].tree / "server/insights/domain/analysis.py").exists():
            print("  skip  insights.query: N-1 has no analysis plane")
            return
        # N first: it answers only once its own entities are written and probed.
        for label in ("N", "N-1"):
            self.check(self.answers_insights(label), f"{label} answers insights.query")
        self.ordinary_events_across()
        names = boundary.SettingNames("SQL_")  # the gate runs the default config
        version = sql(
            self.ch_port,
            "SELECT trim(BOTH '\\'' FROM value) FROM system.settings_profile_elements "
            "WHERE profile_name = {user:String} AND setting_name = {name:String}",
            {"user": boundary.ANALYST_USER, "name": names.version},
        )
        self.check(
            version == str(boundary.ENTITY_VERSION),
            f"the analyst entities are at N's version ({version})",
        )
        policy = sql(
            self.ch_port,
            "SELECT select_filter FROM system.row_policies WHERE short_name = {name:String} "
            "AND database = 'loom' AND table = 'memory'",
            {"name": boundary.POLICY_NAME},
        )
        try:
            secrets = boundary.policy_spellings(policy)
        except ValueError as e:
            self.check(False, f"the memory policy names the scope key ({e})")
            return
        # An older build's probe errors can hold the pads in clear in the
        # server log: its search starts now.
        since = sql(self.ch_port, "SELECT now()")
        self.logs_hold_no_key(since, {"k": "[" + ", ".join(map(sql_string, secrets)) + "]"})

    def ordinary_events_across(self) -> None:
        """Actual requests from each build retain every ordinary catalog value."""
        table = next(t for t in boundary.TABLES if t.name == "agent_events")
        columns = ", ".join(f"toString({c.name})" for c in table.columns)
        for writer in self.builds:
            agent = "event_" + uuid.uuid4().hex
            key = "events/" + writer
            self.call(
                writer,
                "memory.set",
                {
                    **self.ident,
                    "agent": agent,
                    "memory_key": key,
                    "value": {"writer": writer},
                },
            )
            where = (
                f"org = {sql_string(self.org)} AND namespace = 'mixed' "
                f"AND agent = {sql_string(agent)} AND operation = 'memory.set' "
                f"AND memory_key = {sql_string(key)}"
            )
            statement = f"SELECT {columns} FROM loom.agent_events WHERE {where}"
            deadline = time.monotonic() + SETTLE_S
            while True:
                raw = sql(self.ch_port, statement + " FORMAT JSONCompactEachRow")
                if raw or time.monotonic() > deadline:
                    break
                time.sleep(0.1)
            expected = [json.loads(line) for line in raw.splitlines()]
            self.check(len(expected) == 1, f"{writer} emits one real ordinary event")
            for reader in self.builds:
                result = self.call(
                    reader,
                    "insights.query",
                    {
                        "org": self.org,
                        "namespace": "mixed",
                        "sql": statement,
                    },
                )
                self.check(
                    result["rows"] == expected,
                    f"{reader} reads all ordinary fields written by {writer}",
                )
                aggregate = (
                    "SELECT toString(count()), toString(sum(input_tokens)), "
                    "toString(sum(upstream_cost_usd)) FROM loom.agent_events "
                    f"WHERE {where}"
                )
                wanted = json.loads(sql(self.ch_port, aggregate + " FORMAT JSONCompactEachRow"))
                result = self.call(
                    reader,
                    "insights.query",
                    {
                        "org": self.org,
                        "namespace": "mixed",
                        "sql": aggregate,
                    },
                )
                self.check(
                    result["rows"] == [wanted],
                    f"{reader} aggregates {writer}'s ordinary event exactly",
                )
        self.private_event_grants_across()

    def private_event_grants_across(self) -> None:
        marker = "event_private_" + uuid.uuid4().hex
        sql(
            self.ch_port,
            "INSERT INTO loom.agent_events_private "
            "(org, namespace, agent, request_id, mirror_marked, model, input_tokens) VALUES ("
            f"{sql_string(self.org)}, 'mixed', 'gate', {sql_string(marker)}, 1, 'private', 987)",
        )
        for label in self.builds:
            response = httpx.post(
                f"{self.builds[label].url}/v1/insights.query",
                json={
                    "org": self.org,
                    "namespace": "mixed",
                    "sql": "SELECT count() FROM loom.agent_events_private",
                },
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=60,
            )
            self.check(
                response.status_code != 200, f"{label} cannot read the private physical event store"
            )

    def answers_insights(self, label: str) -> bool:
        """Whether `label` answers insights.query within SETTLE_S: a process
        answers once its own probe has passed."""
        body = {"org": self.org, "namespace": "mixed", "sql": "SELECT count() FROM loom.memory"}
        deadline = time.monotonic() + SETTLE_S
        while True:
            r = httpx.post(
                f"{self.builds[label].url}/v1/insights.query",
                json=body,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=60,
            )
            if r.status_code == 200 or time.monotonic() > deadline:
                return r.status_code == 200
            time.sleep(2)

    def logs_hold_no_key(self, since: str, needles: dict[str, str]) -> None:
        """No statement text or error in query_log, and no server log error
        since `since`, holds a spelling of the key; each search first shows a
        refused read of the probe reached that log."""
        sql(self.ch_port, "SYSTEM FLUSH LOGS")
        refused = sql(
            self.ch_port,
            "SELECT count() FROM system.query_log WHERE user = {user:String} "
            "AND exception LIKE '%UNKNOWN_SETTING%'",
            {"user": boundary.ANALYST_USER},
        )
        self.check(refused != "0", f"the probe's refused reads are logged ({refused})")
        logged = sql(
            self.ch_port,
            "SELECT count() FROM system.query_log WHERE "
            "multiSearchAnyCaseInsensitive(query, {k:Array(String)}) "
            "OR multiSearchAnyCaseInsensitive(formatted_query, {k:Array(String)}) "
            "OR multiSearchAnyCaseInsensitive(log_comment, {k:Array(String)}) "
            "OR multiSearchAnyCaseInsensitive(exception, {k:Array(String)})",
            needles,
            boundary.LOG_TEXT_CUT,
        )
        self.check(logged == "0", "no logged statement or error holds the scope key")
        # The server log records each refused read's error, whatever log_queries says.
        deadline = time.monotonic() + SETTLE_S
        while True:
            sql(self.ch_port, "SYSTEM FLUSH LOGS")
            quoted = sql(
                self.ch_port,
                "SELECT count() FROM system.text_log WHERE event_time >= {since:DateTime} "
                "AND level = 'Error' AND position(message, 'SQL_loom_') > 0 "
                "AND message LIKE '%UNKNOWN_SETTING%'",
                {"since": since},
            )
            if quoted != "0" or time.monotonic() > deadline:
                break
            time.sleep(2)
        self.check(quoted != "0", f"a probe's refused read reached the server log ({quoted})")
        errors = sql(
            self.ch_port,
            "SELECT count() FROM system.text_log WHERE event_time >= {since:DateTime} "
            "AND level = 'Error' AND multiSearchAnyCaseInsensitive(message, {k:Array(String)})",
            {"since": since, **needles},
            boundary.LOG_TEXT_CUT,
        )
        self.check(errors == "0", "no server log error holds the scope key")

    def run_completes_across(self, a: str, b: str) -> None:
        run_id = self.call(a, "run.begin", {**self.ident, "goal": f"begun on {a}"})["run_id"]
        done = {**self.ident, "run_id": run_id, "status": "completed"}
        self.call(b, "run.complete", done)
        run = self.call(a, "run.get", {**self.ident, "run_id": run_id})
        run = dict(run.get("run") or run)
        self.check(
            run.get("status") == "completed" and run.get("goal") == f"begun on {a}",
            f"a run begun on {a} and completed on {b} keeps its goal",
        )

    def verdict_survives(self, a: str, b: str) -> None:
        # Each direction has its own subject, so its pair is not judged against the other's.
        subject = "Logs" if a == "N" else "Backups"
        key = f"retention/{a}"
        for days in (30, 90):
            body = {**self.ident, "memory_key": f"{key}/{days}"}
            out = self.call(a, "memory.set", {**body, "value": f"{subject} are kept {days} days."})
        verdict = self.get(a, f"{key}/90")
        self.check(verdict.get("status") == "conflicted", f"{a} judges the second fact conflicted")
        tags = {"org": self.org, "memory_id": out["memory_id"], "tags": ["touched"]}
        self.call(b, "memory.update_by_id", tags)
        after = self.get(a, f"{key}/90")
        self.check(
            after.get("status") == "conflicted"
            and after.get("conflict_with") == verdict.get("conflict_with"),
            f"{a}'s conflict verdict survives {b}'s version write",
        )

    def unknown_column_survives(self, ids: dict[str, str]) -> None:
        """A column only a newer schema has (added here as N+1's migration
        would) survives a version write by a build that does not know it."""
        sql(self.ch_port, "ALTER TABLE loom.memory ADD COLUMN gate_probe String DEFAULT ''")
        sql(
            self.ch_port,
            "INSERT INTO loom._migrations (filename, checksum, applied_by) "
            "VALUES ('998_gate_probe.sql', 'gate', 'mixed-version-gate')",
        )
        # What N+1's boot does before it writes the column: wait for every carrying
        # process to acknowledge it. The version writes below then come at once.
        self.await_carry_acks()
        for label in self.builds:
            where = f"org = '{self.org}' AND memory_key = 'facts/{label}'"
            sql(
                self.ch_port,
                "INSERT INTO loom.memory SELECT * REPLACE "
                "('kept' AS gate_probe, version + 1 AS version) "
                f"FROM loom.memory FINAL WHERE {where}",
            )
            tags = {"org": self.org, "memory_id": ids[label], "tags": ["v2"]}
            self.call(label, "memory.update_by_id", tags)
            probe = sql(self.ch_port, f"SELECT gate_probe FROM loom.memory FINAL WHERE {where}")
            self.check(probe == "kept", f"{label}'s version write keeps a column it does not know")
        sql(self.ch_port, "ALTER TABLE loom.memory DROP COLUMN gate_probe")

    def await_carry_acks(self) -> None:
        """Every live `loom.carry_acks` row has loom.memory's current columns
        (the fingerprint of server/core/rmt_carry.py)."""
        names = sql(
            self.ch_port,
            "SELECT name FROM system.columns WHERE database = 'loom' AND table = 'memory' "
            "AND default_kind NOT IN ('MATERIALIZED', 'ALIAS', 'EPHEMERAL')",
        ).split("\n")
        want = hashlib.sha256("\n".join(sorted(names)).encode()).hexdigest()[:16]
        deadline = time.monotonic() + CARRY_ACK_WAIT_S
        while True:
            acks = sql(
                self.ch_port,
                "SELECT JSONExtractString(fingerprints, 'loom.memory') FROM loom.carry_acks FINAL "
                "WHERE at > now64(3) - toIntervalSecond(30) "
                "AND JSONHas(fingerprints, 'loom.memory') FORMAT TSV",
            )
            behind = [a for a in acks.split("\n") if a and a != want]
            if not behind:
                return
            if time.monotonic() > deadline:
                raise RuntimeError(f"carrying processes never acknowledged the column: {behind}")
            time.sleep(0.5)

    def newer_ledger_warns(self) -> None:
        sql(
            self.ch_port,
            "INSERT INTO loom._migrations (filename, checksum, applied_by) "
            "VALUES ('999_gate_future.sql', 'gate', 'mixed-version-gate')",
        )
        self.new.restart()
        log = self.new.tail()
        self.check(
            "999_gate_future.sql" in log and "schema is newer than this build" in log,
            "N booting on a newer ledger logs the WARNING naming the file",
        )

    def sign_in_across(self) -> None:
        """A sign-in whose steps land on both builds fails cleanly, as a client
        can retry, and leaves the grant to the build that started it."""
        new, old = self.builds["N"], self.builds["N-1"]
        r = httpx.post(
            f"{new.url}/oauth/device_authorization", data={"client_id": "loom-cli"}, timeout=30
        )
        self.check(r.status_code == 200, f"N starts a device grant ({r.status_code})")
        if r.status_code != 200:
            return
        poll = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": r.json()["device_code"],
            "client_id": "loom-cli",
        }
        answer = httpx.post(f"{old.url}/oauth/token", data=poll, timeout=30)
        self.check(
            answer.status_code < 500, f"N-1 answers N's device poll cleanly ({answer.status_code})"
        )
        again = httpx.post(f"{new.url}/oauth/token", data=poll, timeout=30)
        pending = again.status_code == 400 and "authorization_pending" in again.text
        self.check(pending, "N's device grant outlives a poll N-1 answered")
        refresh = {
            "grant_type": "refresh_token",
            "refresh_token": "not-a-token",
            "client_id": "loom-cli",
        }
        for label, srv in self.builds.items():
            got = httpx.post(f"{srv.url}/oauth/token", data=refresh, timeout=30)
            refused = got.status_code == 400 and "invalid_grant" in got.text
            # A build that serves no sign-in has no token endpoint at all.
            self.check(
                refused or (label == "N-1" and got.status_code == 404),
                f"{label} refuses an unknown refresh as invalid_grant ({got.status_code})",
            )
            if label == "N-1" and "Loom-Sessions" in got.headers:
                print("  note  N-1 keeps sessions; a refresh it answers is not checked")
                return
        self.check("Loom-Sessions" not in answer.headers, "N-1's answers say they are N-1's")
        self.refresh_across(old.url, kept=True)
        self.refresh_across(new.url, kept=False)

    def refresh_across(self, url: str, *, kept: bool) -> None:
        """The SDK refreshing a session against `url`: N-1's refusal leaves it
        to ask again, N's ends it."""
        sys.path.insert(0, str(ROOT / "sdk" / "python"))
        from loom import credentials
        from loom.errors import AuthInvalid, BackendUnavailable, LoomError

        real = credentials.CREDENTIALS_FILE
        with tempfile.TemporaryDirectory() as tmp:
            credentials.CREDENTIALS_FILE = Path(tmp) / "credentials"
            spent = {"access_token": "x", "refresh_token": "not-a-token", "expires_in": 0}
            credentials._save("gate", url, spent, session="s", marked=True)
            failed: Exception | None = None
            try:
                credentials.access_token("gate", refused="x")
            except LoomError as e:
                failed = e
            finally:
                stored = credentials.stored_login(url, "gate") is not None
                credentials.CREDENTIALS_FILE = real
        want = BackendUnavailable if kept else AuthInvalid
        label = "N-1" if kept else "N"
        self.check(
            isinstance(failed, want) and stored == kept,
            f"the SDK {'keeps' if kept else 'ends'} a session {label} refuses ({failed!r})",
        )

    def dashboard_loads_across(self) -> None:
        """A page served by one build asks any replica for its assets."""
        pages = {"N-1": self.builds["N-1"], "N": self.new, "N+1": self.next}
        built = {label: (srv.tree / "frontend" / "dist").exists() for label, srv in pages.items()}
        if not built["N"]:
            print("  note  no npm: the dashboard bundles are not built, nor checked")
            return
        for a, b in (("N", "N+1"), ("N+1", "N"), ("N", "N-1"), ("N-1", "N")):
            page, other = pages[a], pages[b]
            if not built[a]:
                print(f"  note  {a}'s bundle is not built; its page is not checked")
                continue
            assets = page.page_assets()
            missing = [p for p in assets if not self.resolves(other, p)]
            self.check(
                bool(assets) and not missing,
                f"every asset of {a}'s page loads from {b} ({len(assets)} files)"
                + (f"; missing {missing}" if missing else ""),
            )

    @staticmethod
    def resolves(srv: Server, path: str) -> bool:
        """200 for `path` on `srv` within SETTLE_S: a replica publishes after boot."""
        deadline = time.monotonic() + SETTLE_S
        while True:
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"{srv.url}{path}", timeout=10).status_code == 200:
                    return True
            if time.monotonic() > deadline:
                return False
            time.sleep(2)

    def one_leader(self) -> None:
        """Every process on `auto`: one holds the workers lease, and it is N+1,
        the build with the higher schema_version."""
        servers = {"N-1": self.builds["N-1"], "N": self.new, "N+1": self.next}
        for srv in servers.values():
            srv.workers = "auto"
            srv.restart()
        # N takes the lease first and hands it over once N+1's beats name it. A
        # first beat goes out before its lease exists, so N+1 may hold it before
        # the older builds read a beat of N+1's naming it: wait for both.
        deadline = time.monotonic() + SETTLE_S
        steady = 0
        holders: list[str] = []
        reasons: dict[str, str] = {}
        while time.monotonic() < deadline and steady < 3:
            state = {k: s.leader() for k, s in servers.items()}
            holders = [k for k, (held, _) in state.items() if held]
            reasons = {k: why for k, (_, why) in state.items()}
            aside = all("to a newer build" in reasons[k] for k in ("N-1", "N"))
            steady = steady + 1 if holders == ["N+1"] and aside else 0
            time.sleep(2)
        self.check(
            holders == ["N+1"],
            f"one process of the newest build runs the workers ({holders}; {reasons})",
        )
        for label in ("N-1", "N"):
            self.check(
                "to a newer build" in reasons[label],
                f"{label} leaves the lease to N+1 ({reasons[label]})",
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--base", default="", help="the N-1 revision (default: merge base with origin/main)"
    )
    ap.add_argument("--keep", action="store_true", help="keep ClickHouse and the logs")
    ap.add_argument(
        "--port-base", type=int, default=0, help="use PORT..PORT+3 (default: free ports)"
    )
    ap.add_argument(
        "--require", action="store_true", help="fail instead of skipping when it cannot run"
    )
    ap.add_argument(
        "--ranking-neutral",
        action="store_true",
        help="compare retrieval on the current base and head",
    )
    args = ap.parse_args(argv)
    if args.port_base:
        _PORTS.extend(range(args.port_base, args.port_base + 4))
    if not shutil.which("docker") or not shutil.which("git"):
        return _skip("needs docker and git", required=args.require)
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return _skip("the docker daemon is not reachable", required=args.require)
    base = args.base
    if not base:
        mb = subprocess.run(
            ["git", "-C", str(ROOT), "merge-base", "HEAD", "origin/main"],
            capture_output=True,
            text=True,
        )
        if mb.returncode != 0:
            return _skip(
                "no origin/main to take the base build from (pass --base)",
                required=args.require,
            )
        base = mb.stdout.strip()
    ranking_sources = None
    if args.ranking_neutral:
        base_sha, head_sha = (
            subprocess.check_output(
                ["git", "-C", str(ROOT), "rev-parse", ref + "^{commit}"], text=True
            ).strip()
            for ref in (base, "HEAD")
        )
        ranking_sources = (base_sha, head_sha)
    work = Path(tempfile.mkdtemp(prefix="loom-mixed-"))
    failures: list[str] = []
    servers: list[Server] = []
    try:
        with clickhouse(args.keep) as ch_port:
            old = Server(
                "base",
                base_tree(base, work / "base"),
                ch_port,
                work,
                ranking_neutral=args.ranking_neutral,
            )
            new = Server("head", ROOT, ch_port, work, ranking_neutral=args.ranking_neutral)
            nxt = Server("next", worktree_copy(work / "next"), ch_port, work)
            servers += [old, new, nxt]
            print(f"next carries {add_noop_migration(nxt.tree)}")
            if build_frontend(ROOT):
                print("dashboard bundles: base, head, and head with one line more")
                build_frontend(old.tree)
                with (nxt.tree / "frontend" / "src" / "main.tsx").open("a") as f:
                    f.write(NEXT_BUNDLE_LINE)
                build_frontend(nxt.tree, node_modules=ROOT / "frontend" / "node_modules")
            print(f"base {base[:12]} boots on an empty ClickHouse")
            old.start()
            print("head boots and applies its migrations")
            new.start()
            print("base restarts on head's schema")
            old.restart()
            nxt.start()
            scenario = Scenario(old, new, nxt, ch_port, owned_servers=servers, owned_logs=work)
            failures = scenario.run_all(ranking_sources=ranking_sources)
    except Exception:
        for s in servers:
            print(f"--- {s.name} log tail ---\n{s.tail()}")
        raise
    finally:
        for s in servers:
            s.stop()
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"logs kept in {work}")
    if failures:
        print(f"mixed-version gate: {len(failures)} failure(s)")
        return 1
    print("mixed-version gate: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
