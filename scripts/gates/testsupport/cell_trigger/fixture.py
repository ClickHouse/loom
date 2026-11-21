"""Own the isolated CI trigger acceptance fixture and its cleanup."""

from __future__ import annotations

import argparse
import ast
import datetime
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

GIB = 2**30

POLICY = dict(
    admit_mem_bytes=10 * GIB,
    admit_disk_bytes=16 * GIB,
    stop_mem_bytes=4 * GIB,
    stop_disk_bytes=16 * GIB,
    stop_process_rss_bytes=7 * GIB // 2,
    sample_seconds=1,
    wall_seconds=7200,
    drain_reserve_seconds=180,
)
HERE = Path(__file__).resolve().parent
FILES = ("fixture.py", "native.py", "tls_proxy.py", "capture.py", "owned_children.py")
PROTOCOL = "protocol.json"
PROTOCOL_SHA = "690f1a06d0a523600a3ef4dcfa5cb58cc48052812ecad4e6e9e591c9e4582924"
SPEC_FIELDS = {
    "schema",
    "fixture_id",
    "root",
    "head",
    "tree",
    "python",
    "python_sha256",
    "lockfile_sha256",
    "helper_path",
    "source_hashes",
    "workdir",
    "ports",
    "databases",
    "stores",
    "pg_admin_dsn",
    "pg_standby_dsn",
    "ch",
    "dependency_receipt",
    "ci_run",
}
CODEC_FILES = (
    "pyvenv.cfg",
    "lib/python3.12/site-packages/clickhouse_connect/_version.py",
    "lib/python3.12/site-packages/clickhouse_connect/datatypes/dynamic.py",
    "lib/python3.12/site-packages/clickhouse_connect/datatypes/format.py",
    "lib/python3.12/site-packages/asyncpg/_version.py",
    "lib/python3.12/site-packages/asyncpg/connection.py",
    "lib/python3.12/site-packages/asyncpg/protocol/protocol.cpython-312-aarch64-linux-gnu.so",
    "lib/python3.12/site-packages/asyncpg/protocol/record.cpython-312-aarch64-linux-gnu.so",
)
CODEC_VERSIONS = {"clickhouse-connect": "1.9.0", "asyncpg": "0.32.0"}


class Refused(RuntimeError):
    command: dict | None = None
    native: dict | None = None


def need(ok: object, field: str) -> None:
    if not ok:
        raise Refused(field)


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def pairs(items: list[tuple]) -> dict:
    out = {}
    for key, value in items:
        need(key not in out, "duplicate JSON field")
        out[key] = value
    return out


def invalid_constant(_value: str):
    raise Refused("nonfinite JSON number")


def finite_float(value: str) -> float:
    result = float(value)
    need(math.isfinite(result), "nonfinite JSON number")
    return result


def json_object(raw: bytes | str) -> dict:
    value = json.loads(
        raw, object_pairs_hook=pairs, parse_constant=invalid_constant, parse_float=finite_float
    )

    def depth(item, level=0):
        need(level <= 8, "JSON nesting")
        if isinstance(item, dict):
            for member in item.values():
                depth(member, level + 1)
        elif isinstance(item, list):
            for member in item:
                depth(member, level + 1)

    depth(value)
    need(isinstance(value, dict), "JSON object")
    return value


@contextmanager
def private_stream(path: Path, *, output: bool = False):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL if output else os.O_RDONLY | os.O_NONBLOCK
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    stream = None
    first = None
    try:
        info = os.fstat(fd)
        need(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_mode & 0o077 == 0,
            "owned private stream",
        )
        stream = os.fdopen(fd, "wb" if output else "rb", closefd=False)
        yield stream
    except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
        first = error
    finally:
        if stream is not None:
            if output:
                try:
                    stream.flush()
                    os.fsync(fd)
                except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                    first = error if first is None else first
            try:
                stream.close()
            except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                first = error if first is None else first
        try:
            os.close(fd)
        except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            first = error if first is None else first
    if first is not None:
        raise first


def read(path: Path, *, limit: int = 65536, expected_sha256: str | None = None) -> dict:
    with private_stream(path) as stream:
        raw = stream.read(limit + 1)
    need(len(raw) <= limit, "input size")
    if expected_sha256 is not None:
        need(hashlib.sha256(raw).hexdigest() == expected_sha256, "receipt bytes")
    return json_object(raw)


def write(path: Path, value: dict, *, limit: int = 65536) -> dict:
    raw = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()
    need(len(raw) <= limit, "receipt size")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    first = None
    stream = None
    try:
        stream = os.fdopen(fd, "wb", closefd=False)
        need(stream.write(raw) == len(raw), "complete receipt write")
        stream.flush()
        os.fsync(fd)
    except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
        first = error
    finally:
        if stream is not None:
            try:
                stream.close()
            except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                first = error if first is None else first
        try:
            os.close(fd)
        except BaseException:
            if first is None:
                raise
    if first is not None:
        raise first
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def ref(value: dict) -> dict:
    need(set(value) == {"path", "sha256"}, "receipt reference fields")
    path = Path(value["path"])
    need(path.is_absolute(), "absolute receipt")
    need(
        isinstance(value["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", value["sha256"]),
        "receipt digest",
    )
    return read(path, expected_sha256=value["sha256"])


def source(spec: dict) -> None:
    need(
        sys.version_info[:2] == (3, 12) and Path(sys.executable) == Path(spec["python"]),
        "actual admitted interpreter",
    )
    dependencies(spec, verify=True)
    root = Path(spec["root"])
    for command, expected in (
        (["rev-parse", "HEAD"], spec["head"]),
        (["rev-parse", "HEAD^{tree}"], spec["tree"]),
        (["status", "--porcelain"], ""),
    ):
        result = subprocess.run(
            ["git", *command], cwd=root, capture_output=True, text=True, check=False, timeout=5
        )
        need(result.returncode == 0 and result.stdout.strip() == expected, "frozen source")
    for name, expected in spec["source_hashes"].items():
        path = (
            root / "bench/cell_trigger_gate.py"
            if name == "driver"
            else root / "scripts/gates/cell_trigger_acceptance.py"
            if name == "entrypoint"
            else root / ".github/workflows/docker-smoke.yml"
            if name == "workflow"
            else HERE / name
        )
        need(sha(path) == expected, "private source bytes")
    need(
        Path(spec["helper_path"]) == HERE / "owned_children.py"
        and sha(Path(spec["helper_path"])) == spec["source_hashes"]["owned_children.py"],
        "owned child implementation",
    )
    need(sha(Path(spec["python"])) == spec["python_sha256"], "private interpreter")
    need(sha(root / "uv.lock") == spec["lockfile_sha256"], "locked dependency source")


def dependencies(spec: dict, *, verify: bool = False) -> None:
    value = ref(spec["dependency_receipt"])
    need(
        set(value)
        == {
            "schema",
            "root",
            "python",
            "python_sha256",
            "lockfile_sha256",
            "versions",
            "codec_sources",
        }
        and type(value["schema"]) is int
        and value["schema"] == 2,
        "closed dependency receipt",
    )
    for key in ("root", "python", "python_sha256", "lockfile_sha256"):
        need(value[key] == spec[key], "dependency source identity")
    environment = Path(spec["root"]) / ".venv"
    need(
        Path(spec["python"]) == environment / "bin/python"
        and value["versions"] == CODEC_VERSIONS
        and set(value["codec_sources"]) == set(CODEC_FILES),
        "fixed interpreter/codec versions and file set",
    )
    for name in CODEC_FILES:
        item = value["codec_sources"][name]
        path = environment / name
        need(
            set(item) == {"path", "sha256"}
            and item["path"] == str(path)
            and type(item["sha256"]) is str
            and re.fullmatch(r"[a-f0-9]{64}", item["sha256"]),
            "exact fixed codec path/hash",
        )
        if verify:
            info = path.lstat()
            need(
                stat.S_ISREG(info.st_mode)
                and info.st_size <= 32 * 1024 * 1024
                and sha(path) == item["sha256"],
                "regular frozen codec bytes",
            )
    if verify:
        cfg = dict(
            line.split(" = ", 1)
            for line in (environment / "pyvenv.cfg").read_text().splitlines()
            if " = " in line
        )
        need(
            cfg.get("implementation") == "CPython"
            and sys.version_info[:2] == (3, 12)
            and cfg.get("version_info") in {"3.12", ".".join(map(str, sys.version_info[:3]))}
            and cfg.get("include-system-site-packages") == "false",
            "fixed isolated CPython environment",
        )
        for package, field, name in (
            ("clickhouse-connect", "version", CODEC_FILES[1]),
            ("asyncpg", "__version__", CODEC_FILES[4]),
        ):
            module = ast.parse((environment / name).read_bytes())
            assignments = [
                node.value
                for node in module.body
                if (
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == field
                        for target in node.targets
                    )
                )
                or (
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == field
                )
            ]
            need(
                len(assignments) == 1
                and ast.literal_eval(assignments[0]) == CODEC_VERSIONS[package],
                "frozen codec source version",
            )


def database(name: str) -> str:
    need(
        isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", name),
        "database identifier",
    )
    need(
        name.lower()
        not in {
            "loom",
            "loom_router",
            "loom_telemetry",
            "default",
            "system",
            "postgres",
            "template0",
            "template1",
            "information_schema",
        },
        "scratch database",
    )
    return name


def pg_dsn(admin: str, db: str) -> str:
    value = urlsplit(admin)
    need(
        value.scheme in {"postgres", "postgresql"}
        and value.hostname == "127.0.0.1"
        and value.port
        and not value.query
        and not value.fragment,
        "numeric PG DSN",
    )
    return urlunsplit((value.scheme, value.netloc, "/" + database(db), "", ""))


def validate(spec: dict) -> None:
    fields = {
        "schema",
        "fixture_id",
        "root",
        "head",
        "tree",
        "python",
        "python_sha256",
        "lockfile_sha256",
        "helper_path",
        "source_hashes",
        "workdir",
        "ports",
        "databases",
        "stores",
        "pg_admin_dsn",
        "pg_standby_dsn",
        "ch",
        "dependency_receipt",
        "ci_run",
    }
    need(set(spec) == fields and type(spec["schema"]) is int and spec["schema"] == 2, "spec fields")
    need(str(uuid.UUID(spec["fixture_id"])) == spec["fixture_id"], "fixture UUID")
    for field in ("root", "python", "helper_path", "workdir"):
        need(Path(spec[field]).is_absolute(), "absolute fixture path")
    need(
        all(
            isinstance(spec[field], str) and re.fullmatch(r"[a-f0-9]{40}", spec[field])
            for field in ("head", "tree")
        ),
        "full source identity",
    )
    need(
        set(spec["source_hashes"]) == set(FILES) | {"driver", "entrypoint", "workflow", PROTOCOL},
        "source hash set",
    )
    need(spec["source_hashes"][PROTOCOL] == PROTOCOL_SHA, "fixed capture protocol")
    need(
        all(
            isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)
            for value in spec["source_hashes"].values()
        ),
        "source digest values",
    )
    need(set(spec["ports"]) == {"api", "router", "probe", "tls"}, "port roles")
    need(
        len(set(spec["ports"].values())) == 4
        and all(type(p) is int and 0 < p < 65536 for p in spec["ports"].values()),
        "distinct ports",
    )
    need(
        set(spec["databases"]) == {"pg_cell", "pg_directory", "ch_schema", "ch_telemetry"},
        "DB roles",
    )
    need(len(set(spec["databases"].values())) == 4, "distinct databases")
    for name in spec["databases"].values():
        database(name)
    pg_dsn(spec["pg_admin_dsn"], spec["databases"]["pg_cell"])
    need(
        set(spec["ch"]) == {"host", "port", "username", "password", "keeper_prefix"},
        "CH input fields",
    )
    need(
        spec["ch"]["host"] == "127.0.0.1"
        and type(spec["ch"]["port"]) is int
        and 0 < spec["ch"]["port"] < 65536,
        "numeric CH endpoint",
    )
    need(
        os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("CI") == "true",
        "GitHub CI only",
    )
    need(
        spec["ci_run"]
        == {
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "repository": os.environ.get("GITHUB_REPOSITORY"),
        }
        and str(spec["ci_run"]["run_id"]).isdigit(),
        "actual CI run binding",
    )
    dependencies(spec)
    need(set(spec["stores"]) == {"clickhouse", "pg_primary", "pg_standby"}, "three owned stores")
    for role, expected in spec["stores"].items():
        store_fields = {
            "role",
            "container_id",
            "container_name",
            "created_at",
            "owner_evidence",
            "host_pid",
            "start_ticks",
            "boot_id",
            "uid",
            "ports",
            "memory_limit_bytes",
            "server_limit_bytes",
            "shared_buffers_bytes",
            "retained",
            "disk_path",
        }
        need(set(expected) == store_fields, "store input fields")
        need(expected["role"] == role and expected["retained"] is False, "job-created store")
        need(
            type(expected["host_pid"]) is int
            and expected["host_pid"] > 1
            and str(expected["start_ticks"]).isdigit(),
            "store birth input",
        )
        for mapping in expected["ports"]:
            need(
                set(mapping) == {"host", "host_port", "internal_port", "protocol"}
                and mapping["host"] == "127.0.0.1"
                and mapping["protocol"] == "tcp",
                "store loopback mapping",
            )
        if role == "clickhouse":
            port = spec["ch"]["port"]
            need(
                expected["memory_limit_bytes"] == 5 * GIB
                and expected["server_limit_bytes"] == 9 * GIB // 2,
                "fixed CH caps",
            )
        else:
            target = urlsplit(
                spec["pg_admin_dsn"] if role == "pg_primary" else spec["pg_standby_dsn"]
            )
            need(
                target.scheme in {"postgres", "postgresql"}
                and target.hostname == "127.0.0.1"
                and target.port
                and not target.query
                and not target.fragment,
                "numeric owned PG endpoint",
            )
            port = target.port
            need(
                type(expected["memory_limit_bytes"]) is int
                and expected["memory_limit_bytes"] > 0
                and type(expected["shared_buffers_bytes"]) is int
                and expected["shared_buffers_bytes"] > 0,
                "observed PG caps",
            )
        need(any(m["host_port"] == port for m in expected["ports"]), "store endpoint binding")
        proof = ref(expected["owner_evidence"])
        need(
            proof.get("container_id") == expected["container_id"]
            and proof.get("created_at") == expected["created_at"]
            and proof.get("task") == "cell-trigger-acceptance"
            and proof.get("ci_run") == spec["ci_run"],
            "positive creator evidence",
        )


def child_env(spec: dict, settings: dict, *, ca: bool = False) -> dict:
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONPATH": spec["root"],
        "LOOM_ENV_FILE": "",
        "LOOM_SETTINGS_FILE": "",
        "HOME": str(Path(spec["workdir"]) / "home"),
        "TMPDIR": str(Path(spec["workdir"]) / "tmp"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(Path(spec["workdir"]) / "bytecode"),
        "GITHUB_ACTIONS": os.environ.get("GITHUB_ACTIONS", ""),
        "CI": os.environ.get("CI", ""),
        **{
            "GITHUB_" + k: os.environ.get("GITHUB_" + k, "")
            for k in ("RUN_ID", "RUN_ATTEMPT", "REPOSITORY")
        },
        "LOOM_INSTANCE_LABEL": "iso:trigger:" + spec["fixture_id"][:12],
    }
    for key, value in settings.items():
        need(re.fullmatch(r"[a-z][a-z0-9_]*", key), "setting name")
        env["LOOM_" + key.upper()] = value if isinstance(value, str) else json.dumps(value)
    if ca:
        env["SSL_CERT_FILE"] = str(Path(spec["workdir"]) / "ca.crt")
    return env


def load_owned(path: Path):
    from owned_children import OwnedChildren

    return OwnedChildren()


def error_metadata(
    error: BaseException, *, files: tuple[str, ...] = FILES, fallback: bool = True
) -> dict:
    site = {"file": "unknown", "line": 0, "function": "unknown"}
    frame = sys._getframe(1) if fallback else None
    line = frame.f_lineno if frame is not None else 0
    trace = error.__traceback__
    for _ in range(64):
        if trace is None:
            break
        candidate = trace.tb_frame
        if Path(candidate.f_code.co_filename).name in files and candidate.f_code.co_name not in {
            "need",
            "closed",
            "equal",
            "integer",
            "identifier",
            "hex64",
        }:
            frame = candidate
            line = trace.tb_lineno
        trace = trace.tb_next
    while frame is not None and frame.f_code.co_name in {"select", "block_teardown"}:
        frame = frame.f_back
        if frame is not None:
            line = frame.f_lineno
    if frame is not None and Path(frame.f_code.co_filename).name in files:
        site = {
            "file": Path(frame.f_code.co_filename).name,
            "line": line,
            "function": frame.f_code.co_name[:64],
        }
    return {
        "error_type": type(error).__name__[:64],
        "site": site,
        "outcome": "refused"
        if isinstance(error, Refused)
        else "error"
        if isinstance(error, Exception)
        else "cancelled",
    }


def native_failure_metadata(error: BaseException, *, files: tuple[str, ...] = FILES) -> dict:
    metadata = error_metadata(error, files=files)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", metadata["error_type"]):
        metadata["error_type"] = "unknown"
    code = getattr(error, "code", None)
    code = code if type(code) is int and 0 <= code < 2**31 else None
    causes = []
    seen = {id(error)}
    cause = error.__cause__ if error.__cause__ is not None else error.__context__
    for _ in range(4):
        if cause is None or id(cause) in seen:
            break
        seen.add(id(cause))
        name = type(cause).__name__
        number = getattr(cause, "errno", None)
        causes.append(
            {
                "error_type": name
                if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", name)
                else "unknown",
                "errno": number if type(number) is int and -(2**31) <= number < 2**31 else None,
            }
        )
        cause_site = error_metadata(cause, files=files, fallback=False)["site"]
        if cause_site["file"] != "unknown":
            causes[-1]["site"] = cause_site
        sqlstate = getattr(cause, "sqlstate", None)
        if type(sqlstate) is str and re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
            causes[-1]["sqlstate"] = sqlstate
        if name == "QueryCanceledError" and sqlstate == "57014":
            # Compare fixed server messages; exception text never enters the receipt.
            message = getattr(cause, "message", None)
            causes[-1]["cancel_reason"] = (
                {
                    "canceling statement due to statement timeout": "statement_timeout",
                    "canceling statement due to user request": "user_request",
                }.get(message, "unknown")
                if type(message) is str
                else "unknown"
            )
        cause = cause.__cause__ if cause.__cause__ is not None else cause.__context__
    driver = {"code": code, "causes": causes}
    sqlstate = getattr(error, "sqlstate", None)
    if type(sqlstate) is str and re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
        driver["sqlstate"] = sqlstate
    extra = getattr(error, "extra", None)
    if type(extra) is dict and type(extra.get("outcome_unknown")) is bool:
        driver["outcome_unknown"] = extra["outcome_unknown"]
    if code is not None or causes or len(driver) > 2:
        metadata["driver"] = driver
    return metadata


class Owner:
    def __init__(self, spec: dict, owned, note) -> None:
        self.spec, self.owned, self.note = spec, owned, note
        self.leaders: list[dict] = []
        self.failures: list[list] = []
        self.phase = "owner"
        self.failure_overflow = False
        self.failure_attempted = 0
        self.failure_written = 0
        self.diagnostic_failures = 0
        self.interruption = Refused("interrupted")
        self.stopping = False
        self.terminal_reporting = False
        self.blocked = False
        self.started = time.monotonic()
        self.serial = 0
        self.sampled = 0.0
        self.guard_stop = threading.Event()
        self.guard = None

    def select(self, error: BaseException, *, phase: str | None = None) -> None:
        # Select before formatting evidence. No IO in a signal handler.
        if len(self.failures) >= 64:
            self.failure_overflow = True
            return
        entry = [error, {"phase": phase or self.phase}]
        self.failures.append(entry)
        if len(self.failures) > 64:
            self.failure_overflow = True
            del self.failures[64:]
        selected_phase = entry[1]["phase"]
        unknown = {"file": "unknown", "line": 0, "function": "unknown"}
        try:
            entry[1] = {"phase": selected_phase, **error_metadata(error)}
            if isinstance(error, Refused):
                if error.command is not None:
                    entry[1]["command"] = error.command
                if error.native is not None:
                    entry[1]["native"] = error.native
        except BaseException as secondary:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            entry[1] = {
                "phase": selected_phase,
                "error_type": type(error).__name__[:64],
                "site": unknown,
                "outcome": "evidence-error",
            }
            if len(self.failures) < 64:
                self.failures.append(
                    [
                        secondary,
                        {
                            "phase": "diagnostic",
                            "error_type": type(secondary).__name__[:64],
                            "site": unknown,
                            "outcome": "error",
                        },
                    ]
                )
            else:
                self.failure_overflow = True
        if len(self.failures) > 64:
            self.failure_overflow = True
            del self.failures[64:]

    def failure_summary(self) -> dict:
        records = []
        snapshot = self.failures[:64]
        for index, (_, record) in enumerate(snapshot):
            if "outcome" not in record:
                break
            records.append(dict(index=index, primary=index == 0, **record))
        return {
            "records": records,
            "pending_records": len(snapshot) - len(records),
            "overflow": self.failure_overflow or len(self.failures) > 64,
            "event_attempts": self.failure_attempted,
            "event_records_written": self.failure_written,
            "diagnostic_write_failures": self.diagnostic_failures,
        }

    def flush_failures(self) -> None:
        # Snapshot once: failed evidence writes cannot recursively create more writes here.
        records = self.failure_summary()["records"]
        for index in range(self.failure_attempted, len(records)):
            self.failure_attempted = index + 1
            try:
                self.note("failure", **records[index])
                self.failure_written += 1
            except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                self.diagnostic_failures += 1
                self.select(error, phase="diagnostic")

    @property
    def first(self) -> BaseException | None:
        return self.failures[0][0] if self.failures else None

    def record(self, event: str, **fields) -> None:
        try:
            self.note(event, **fields)
        except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            self.select(error, phase="diagnostic")

    def interrupt(self, _signum, _frame) -> None:
        if not self.stopping:
            self.stopping = True
            self.select(self.interruption, phase="interrupt")
        if self.terminal_reporting:
            # Closed ownership permits cancellation to escape the final return boundary.
            first = self.first
            raise self.interruption if first is None else first

    def sample(self, *, admit: bool = False) -> None:
        try:
            now = time.monotonic()
            available = next(
                int(line.split()[1]) * 1024
                for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith("MemAvailable:")
            )
            need(available >= POLICY["admit_mem_bytes" if admit else "stop_mem_bytes"], "RAM floor")
            for path in [
                Path(self.spec["workdir"]),
                *[Path(s["disk_path"]) for s in self.spec["stores"].values()],
            ]:
                need(shutil.disk_usage(path).free >= 16 * GIB, "disk floor")
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            for store in self.spec["stores"].values():
                identity = self.owned.identity(store["host_pid"])
                need(identity is not None, "resource store birth changed")
                need(identity[1] == store["start_ticks"], "resource store birth changed")
                need(boot == store["boot_id"], "resource store birth changed")
                need(
                    os.stat(f"/proc/{store['host_pid']}").st_uid == store["uid"],
                    "resource store birth changed",
                )
            roots = [self.owned.pid] + [s["host_pid"] for s in self.spec["stores"].values()]
            births = {pid: self.owned.identity(pid) for pid in roots}
            need(all(births.values()), "resource root disappeared")
            observed = dict(births)
            disappeared = set()

            def complete_forest() -> bool:
                pending = list(roots)
                seen = set()
                while pending:
                    need(time.monotonic() - now < 3, "resource census stale")
                    pid = pending.pop()
                    if pid in seen:
                        continue
                    seen.add(pid)
                    before = self.owned.identity(pid)
                    if before is None:
                        need(pid not in births, "resource root disappeared")
                        disappeared.add(pid)
                        return False
                    need(
                        pid not in disappeared
                        and (pid not in observed or observed[pid][:2] == before[:2]),
                        "resource process birth changed",
                    )
                    observed[pid] = before
                    need(
                        self.owned.rss(pid) * 1024 <= POLICY["stop_process_rss_bytes"],
                        "process RSS",
                    )
                    try:
                        children = self.owned.children(pid)
                    except (FileNotFoundError, ProcessLookupError):
                        need(
                            pid not in births and self.owned.identity(pid) is None,
                            "resource child census unknown",
                        )
                        disappeared.add(pid)
                        return False
                    after = self.owned.identity(pid)
                    if after is None:
                        need(pid not in births, "resource root disappeared")
                        disappeared.add(pid)
                        return False
                    need(after[:2] == before[:2], "resource process birth changed")
                    pending.extend(children)
                return True

            # Discard an incomplete traversal; only a fresh whole forest can renew the sample.
            for _attempt in range(3):
                need(time.monotonic() - now < 3, "resource census stale")
                for pid, birth in births.items():
                    current = self.owned.identity(pid)
                    need(
                        current is not None and current[:2] == birth[:2],
                        "resource root birth changed",
                    )
                need(
                    Path("/proc/sys/kernel/random/boot_id").read_text().strip() == boot
                    and os.stat(f"/proc/{self.owned.pid}").st_uid == os.geteuid(),
                    "resource owner birth changed",
                )
                for store in self.spec["stores"].values():
                    need(
                        births[store["host_pid"]][1] == store["start_ticks"]
                        and Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                        == store["boot_id"]
                        and os.stat(f"/proc/{store['host_pid']}").st_uid == store["uid"],
                        "resource store birth changed",
                    )
                if complete_forest():
                    need(time.monotonic() - now < 3, "resource census stale")
                    break
            else:
                raise Refused("resource process census remained incomplete")
            for item in tuple(self.leaders):
                need(item["log"].stat().st_size <= 16 * 1024 * 1024, "child log bound")
            driver_artifact = Path(self.spec["workdir"]) / "driver.jsonl"
            if driver_artifact.exists():
                need(driver_artifact.stat().st_size <= 64 * 1024 * 1024, "driver artifact bound")
            summary = Path(self.spec["workdir"]) / "mint-summary.stdout"
            if summary.exists():
                need(summary.stat().st_size <= 4096, "mint stdout bound")
            total = 0
            for arm in range(10):
                path = Path(self.spec["workdir"]) / "delivery-intent" / f"arm-{arm}.jsonl"
                if path.exists():
                    size = path.stat().st_size
                    need(size <= 32 * 1024 * 1024, "intent file bound")
                    total += size
            need(total <= 320 * 1024 * 1024, "intent aggregate bound")
            self.sampled = now
        except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            self.select(error, phase="resource")

    def start_guard(self) -> None:
        def watch() -> None:
            try:
                while not self.guard_stop.wait(1):
                    self.sample()
            except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                self.select(error)

        self.sample(admit=True)
        if self.first is not None:
            raise self.first
        self.guard = threading.Thread(target=watch, name="trigger-resource-readonly", daemon=False)
        self.guard.start()

    def monitor(self, *, admit: bool = False, cleanup: bool = False) -> None:
        self.flush_failures()
        if admit:
            self.sample(admit=True)
        if time.monotonic() - self.started >= 7200:
            self.select(Refused("global wall overrun; cleanup retains exclusion"))
        if not cleanup:
            try:
                need(
                    not self.stopping and time.monotonic() - self.started < 7020,
                    "global work deadline",
                )
                need(time.monotonic() - self.sampled < 3, "resource sampler stale")
                for item in self.leaders:
                    if item["persistent"]:
                        need(item["process"].poll() is None, "fixture leader ended")
            except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                self.select(error)
        if self.first is not None and not cleanup:
            raise self.first

    def start(
        self,
        role: str,
        argv: list[str],
        env: dict,
        *,
        persistent: bool = False,
        cleanup: bool = False,
    ) -> dict:
        self.monitor(cleanup=cleanup)
        source(self.spec)
        argv = list(argv)
        executable = shutil.which(argv[0], path=env.get("PATH"))
        need(executable is not None and Path(executable).is_absolute(), "known child executable")
        argv[0] = executable
        self.serial += 1
        label = role + "-" + str(self.serial)
        log_path = Path(self.spec["workdir"]) / (label + ".log")
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        item = None
        stdout_fd = None
        try:
            if role == "mint-token":
                stdout_fd = os.open(
                    Path(self.spec["workdir"]) / "mint-summary.stdout",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
            process = subprocess.Popen(
                argv,
                cwd=self.spec["root"],
                env=env,
                stdout=stdout_fd if stdout_fd is not None else fd,
                stderr=fd if stdout_fd is not None else subprocess.STDOUT,
                start_new_session=True,
            )
            # Register Popen before any observation/callback can fail.
            item = {
                "role": label,
                "process": process,
                "pidfd": None,
                "identity": None,
                "persistent": persistent,
                "log": log_path,
                "terminal": None,
                "argv": list(argv),
                "configuration_sha256": digest(env),
                "launch_receipt": None,
                "terminal_receipt": None,
            }
            item["signals"] = []
            self.leaders.append(item)
            identity = self.owned.identity(process.pid)
            need(identity and identity[0] == self.owned.pid, "direct child birth")
            need(os.stat(f"/proc/{process.pid}").st_uid == os.geteuid(), "child UID")
            item["identity"] = identity
            item["pidfd"] = os.pidfd_open(process.pid)
            after = self.owned.identity(process.pid)
            need(after is not None and after[:2] == identity[:2], "stable pinned child")
            item["launch_receipt"] = write(
                Path(self.spec["workdir"]) / (label + "-birth.json"),
                {
                    "pid": process.pid,
                    "start_ticks": identity[1],
                    "ppid": identity[0],
                    "uid": os.geteuid(),
                    "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                    "cwd": self.spec["root"],
                    "configuration_sha256": digest(env),
                    "utc": utc(),
                },
            )
            self.record("birth", role=label, pid=process.pid, start_ticks=identity[1])
            if self.first is not None and not cleanup:
                raise self.first
            return item
        except BaseException as error:
            self.select(error)
            raise
        finally:
            if stdout_fd is not None:
                try:
                    os.close(stdout_fd)
                except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                    self.select(error)
            try:
                os.close(fd)
            except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                self.select(error)

    def poll(self, item: dict) -> int | None:
        result = item["process"].poll()
        if result is not None and item["terminal"] is None:
            item["terminal"] = {"returncode": result, "utc": utc()}
            item["persistent"] = False
            try:
                item["terminal_receipt"] = write(
                    Path(self.spec["workdir"]) / (item["role"] + "-terminal.json"), item["terminal"]
                )
            except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                self.select(error)
            self.record("terminal", role=item["role"], returncode=result)
        return result

    def command(
        self, role: str, argv: list[str], env: dict, *, seconds: float = 120, cleanup: bool = False
    ) -> dict:
        item = self.start(role, argv, env, cleanup=cleanup)
        end = (
            time.monotonic() + seconds
            if cleanup
            else min(self.started + 7020, time.monotonic() + seconds)
        )
        while self.poll(item) is None:
            self.monitor(cleanup=cleanup)
            need(time.monotonic() < end, "serial command deadline")
            time.sleep(0.05)
        if item["process"].returncode != 0:
            error = Refused("native command refused")
            error.command = {
                "role": role if role in {"inspect", "keeper-prefix", "create"} else "other",
                "serial": self.serial,
                "returncode": item["process"].returncode,
            }
            raise error
        if self.first is not None and not cleanup:
            raise self.first
        return item

    def send(self, item: dict, sig: int) -> None:
        if self.poll(item) is not None:
            return
        before = self.owned.identity(item["process"].pid)
        if (
            before is None
            or before[0] != self.owned.pid
            or (item["identity"] is not None and before[:2] != item["identity"][:2])
        ):
            self.blocked = True
            raise Refused("child identity mismatch")
        # No generic reaper runs while Popen owns this direct child, so an unreaped
        # Popen leader cannot have its PID reused during an interrupted birth capture.
        if item["identity"] is None:
            item["identity"] = before
        if item["pidfd"] is None:
            item["pidfd"] = os.pidfd_open(item["process"].pid)
        need(os.stat(f"/proc/{item['process'].pid}").st_uid == os.geteuid(), "signal UID")
        after = self.owned.identity(item["process"].pid)
        need(after is not None and after[:2] == before[:2], "signal birth recheck")
        try:
            signal.pidfd_send_signal(item["pidfd"], sig)
            item.setdefault("signals", []).append(sig)
        except ProcessLookupError:
            need(self.poll(item) is not None, "lost pinned child")

    def stop_one(self, item: dict, *, driver: bool = False, api: bool = False) -> None:
        if self.poll(item) is not None:
            self.terminal_outcome(item, driver=driver, api=api)
            return
        phases = (
            [(signal.SIGINT, 60), (signal.SIGTERM, 15)]
            if driver
            else [(signal.SIGTERM, 90 if api else 15)]
        )
        for sig, seconds in phases + [(signal.SIGKILL, 5)]:
            if self.poll(item) is not None:
                break
            if sig == signal.SIGKILL or (driver and sig == signal.SIGTERM):
                self.select(Refused("cleanup escalation"))
            self.send(item, sig)
            end = time.monotonic() + seconds
            while self.poll(item) is None and time.monotonic() < end:
                self.monitor(cleanup=True)
                time.sleep(0.05)
        need(self.poll(item) is not None, "child not reaped")
        item["process"].wait()
        self.terminal_outcome(item, driver=driver, api=api)

    def terminal_outcome(self, item: dict, *, driver: bool, api: bool) -> None:
        role = item["role"].split("-", 1)[0]
        result = item["process"].returncode
        sent = item.get("signals", [])
        if driver:
            ok = result in (0, 1) and not sent
        elif api or role in {"router", "proxy"}:
            # Uvicorn re-raises its captured TERM after graceful shutdown; the
            # private proxy consumes TERM. Only an owner-requested stop qualifies.
            ok = signal.SIGTERM in sent and result in (
                (0, -signal.SIGTERM) if role != "proxy" else (0,)
            )
        else:
            return
        if not ok:
            self.select(Refused("unexpected fixture terminal outcome"))

    def cleanup(self) -> None:
        # Each Popen owns its status until all leaders are reaped.
        self.flush_failures()
        if self.owned is None:
            need(not self.leaders, "uninitialized ownership with registered children")
            return
        for prefix in ("driver", "api", "router", "proxy", ""):
            for item in self.leaders:
                if item.get("cleaned") or (prefix and not item["role"].startswith(prefix)):
                    continue
                try:
                    self.stop_one(
                        item,
                        driver=item["role"].startswith("driver"),
                        api=item["role"].startswith("api"),
                    )
                    item["cleaned"] = True
                except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                    self.select(error)
        if self.blocked or any(self.poll(item) is None for item in self.leaders):
            raise Refused("owned leader cleanup unresolved; exclusion retained")
        try:
            self.owned.stop(None, lambda *args, **kwargs: self.record("adopted-cleanup", **kwargs))
        except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            self.select(error)
        need(not self.owned.children(self.owned.pid), "kernel children remain")
        for item in self.leaders:
            if item["pidfd"] is not None:
                try:
                    os.close(item["pidfd"])
                    item["pidfd"] = None
                except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                    self.select(error)


def runtime(spec: dict) -> int:
    """The separately admitted path; never called by inert controls."""
    from native import Fixture

    validate(spec)
    source(spec)
    for port in spec["ports"].values():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as check:
            check.bind(("127.0.0.1", port))
    workdir = Path(spec["workdir"])
    need(workdir.is_absolute() and not workdir.exists(), "exclusive work directory")
    os.mkdir(workdir, 0o700)
    for name in ("home", "tmp", "bytecode"):
        (workdir / name).mkdir(mode=0o700)
    events = os.open(
        workdir / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )

    def note(event: str, **fields) -> None:
        raw = (json.dumps(dict(utc=utc(), event=event, **fields), allow_nan=False) + "\n").encode()
        need(
            len(raw) <= 65536 and os.lseek(events, 0, os.SEEK_CUR) + len(raw) <= 16 * 1024 * 1024,
            "event bound",
        )
        need(os.write(events, raw) == len(raw), "complete event write")
        os.fsync(events)

    owner = None
    lock = None
    fixture = None
    release = False
    verdict = "INVALID"
    first = None
    events_closed = False
    lock_closed = False

    def selected() -> BaseException | None:
        retained = owner.first if owner is not None else None
        return retained if retained is not None else first

    try:
        owner = Owner(spec, None, note)
        owner.phase = "ownership"
        owner.owned = load_owned(Path(spec["helper_path"]))
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, owner.interrupt)
        owner.start_guard()
        owner.monitor(admit=True)
        lock = os.open(
            "/tmp/loom-full-unit-suite.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # This isolated job owns the canonical slot through final native cleanup.
        fixture = Fixture(spec, owner)
        owner.phase = "setup"
        fixture.setup()
        owner.monitor(admit=True)
        owner.phase = "measure"
        result = fixture.measure()
        owner.phase = "provenance"
        fixture.provenance()
        verdict = result
    except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
        first = error
        if owner is not None:
            owner.select(error)
    finally:
        if owner is not None:
            try:
                owner.phase = "cleanup"
                owner.cleanup()
                if fixture is not None:
                    owner.phase = "teardown"
                    fixture.teardown()
                owner.phase = "cleanup"
                owner.cleanup()
                release = True
            except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                owner.select(error)
        else:
            release = True
        if not release:
            # Never exit over unreaped children or uncertain task writes.
            while True:
                if owner is not None:
                    try:
                        owner.phase = "cleanup"
                        owner.cleanup()
                        if fixture is not None:
                            owner.phase = "teardown"
                            fixture.teardown()
                        owner.phase = "cleanup"
                        owner.cleanup()
                        release = True
                        break
                    except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                        owner.select(error)
                try:
                    time.sleep(1)
                except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                    if owner is not None:
                        owner.select(error)
        if owner is not None:
            try:
                owner.phase = "monitor-stop"
                owner.monitor(cleanup=True)
                owner.guard_stop.set()
                if owner.guard is not None:
                    owner.guard.join()
            except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                owner.select(error)
            owner.flush_failures()
            first = owner.first if owner.first is not None else first
        try:
            os.fsync(events)
        except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            if owner is not None:
                owner.select(error, phase="events-close")
            first = error if first is None else first
        finally:
            try:
                os.close(events)
                events_closed = True
            except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                if owner is not None:
                    owner.select(error, phase="events-close")
                first = error if first is None else first
    first = selected()
    if first is not None:
        verdict = "INVALID"
    try:
        write(
            workdir / "outer-result.json",
            {
                "schema": 2,
                "fixture_id": spec["fixture_id"],
                "candidate_verdict": verdict,
                "native_cleanup_complete": release
                and (fixture is None or not fixture.teardown_incomplete),
                "error_type": type(first).__name__ if first is not None else "",
                "failure_evidence": owner.failure_summary() if owner is not None else None,
                "utc": utc(),
            },
        )
    except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
        if owner is not None:
            owner.select(error, phase="result")
        first = error if first is None else first
        verdict = "INVALID"
    if lock is not None and release:
        try:
            os.close(lock)
            lock_closed = True
        except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            if owner is not None:
                owner.select(error, phase="unlock")
            first = error if first is None else first
            verdict = "INVALID"
    if owner is not None and release and events_closed and (lock is None or lock_closed):
        try:
            owner.terminal_reporting = True
        except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            owner.select(error, phase="reporting-mode")
    try:
        first = selected()
        if first is not None:
            verdict = "INVALID"
        # Candidate bytes require successful terminal status; they grant no PASS alone.
        print(
            json.dumps(
                {
                    "candidate_verdict": verdict,
                    "error_type": type(first).__name__ if first is not None else "",
                    "failure_evidence": owner.failure_summary() if owner is not None else None,
                }
            ),
            flush=True,
        )
        return 0 if verdict == "PASS" and selected() is None else 1
    except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
        if owner is not None:
            owner.select(error, phase="stdout")
        first = selected()
        first = error if first is None else first
        verdict = "INVALID"
        try:
            write(
                workdir / "reporting-failure.json",
                {
                    "schema": 2,
                    "verdict": "INVALID",
                    "error_type": type(first).__name__,
                    "failure_evidence": owner.failure_summary() if owner is not None else None,
                },
            )
        except BaseException as secondary:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            if owner is not None:
                owner.select(secondary, phase="reporting")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    args = parser.parse_args()
    try:
        return runtime(read(args.spec))
    except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
        print(json.dumps({"verdict": "INVALID", "error_type": type(error).__name__}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
