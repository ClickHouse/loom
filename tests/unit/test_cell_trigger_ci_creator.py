"""A failed fixture may remove only the containers its creator recorded."""

import signal
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.gates.cell_trigger_acceptance import Stores, native_cleanup_complete, write


class FakeStores(Stores):
    def __init__(self, folder: Path, rows: dict):
        super().__init__(folder, folder, "fixture", {"run_id": "123"})
        self.rows = deepcopy(rows)
        self.removed = []

    def inspect(self, identity, *, cleanup=False):
        return self.rows[identity]

    def command(self, *argv, timeout=120, cleanup=False):
        assert cleanup
        if argv[:3] == ("docker", "rm", "--force"):
            self.removed.append(argv[-1])
            return ""
        assert argv[:3] == ("docker", "ps", "-a")
        identity = next(part.removeprefix("id=") for part in argv if part.startswith("id="))
        return identity if identity in self.rows and identity not in self.removed else ""


def container(identity="own", created="birth", label="fixture"):
    return {
        "Id": identity,
        "Created": created,
        "Name": "/intent",
        "Config": {"Labels": {"loom.ci.trigger": label}},
    }


@pytest.mark.parametrize(
    "change",
    [
        {"Id": "reused"},
        {"Created": "new-birth"},
        {"Config": {"Labels": {"loom.ci.trigger": "foreign"}}},
    ],
)
def test_cleanup_refuses_changed_container_identity(tmp_path, change):
    actual = {**container(), **change}
    stores = FakeStores(tmp_path, {"own": actual})
    stores.items = [{"role": "pg-primary", "name": "intent", "id": "own", "created": "birth"}]
    assert stores.cleanup()
    assert stores.removed == []


def test_cleanup_continues_exact_owned_containers_after_a_refusal(tmp_path):
    stores = FakeStores(tmp_path, {"own": container(), "foreign": container(label="other")})
    stores.items = [
        {"role": "pg-primary", "name": "intent", "id": "own", "created": "birth"},
        {"role": "pg-standby", "name": "other", "id": "foreign", "created": "birth"},
    ]
    assert stores.cleanup() == ["RuntimeError:pg-standby"]
    assert stores.removed == ["own"]


def test_partial_create_requires_its_exact_name_and_random_label(tmp_path):
    stores = FakeStores(tmp_path, {"intent": container()})
    stores.items = [{"role": "clickhouse", "name": "intent", "id": None, "created": None}]
    assert stores.cleanup() == []
    assert stores.removed == ["own"]


def test_partial_create_never_adopts_a_foreign_matching_name(tmp_path):
    stores = FakeStores(tmp_path, {"intent": container(label="other")})
    stores.items = [{"role": "clickhouse", "name": "intent", "id": None, "created": None}]
    assert stores.cleanup() == ["RuntimeError:clickhouse"]
    assert stores.removed == []


def test_cancellation_is_deferred_until_owned_cleanup_finishes(tmp_path):
    stores = FakeStores(tmp_path, {})
    stores.interrupted(signal.SIGTERM, None)
    assert stores.cancelled == signal.SIGTERM
    stores.terminal = True
    with pytest.raises(SystemExit) as caught:
        stores.interrupted(signal.SIGINT, None)
    assert caught.value.code == 128 + signal.SIGTERM


def test_store_removal_requires_acknowledged_native_cleanup(tmp_path):
    (tmp_path / "fixture").mkdir()
    with pytest.raises(FileNotFoundError):
        native_cleanup_complete(tmp_path, "fixture")
    write(
        tmp_path / "fixture/outer-result.json",
        {"schema": 2, "fixture_id": "fixture", "native_cleanup_complete": False},
    )
    with pytest.raises(RuntimeError, match="Native retirement"):
        native_cleanup_complete(tmp_path, "fixture")


def test_failed_measurement_can_close_only_after_real_native_cleanup(tmp_path):
    (tmp_path / "fixture").mkdir()
    write(
        tmp_path / "fixture/outer-result.json",
        {
            "schema": 2,
            "fixture_id": "fixture",
            "candidate_verdict": "FAIL",
            "native_cleanup_complete": True,
        },
    )
    native_cleanup_complete(tmp_path, "fixture")


def test_dependency_metadata_accepts_only_the_actual_python_version(tmp_path):
    import hashlib
    import json
    import sys

    from scripts.gates.testsupport.cell_trigger import fixture

    environment = tmp_path / ".venv"
    for name in fixture.CODEC_FILES:
        path = environment / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture codec bytes")
    (environment / fixture.CODEC_FILES[1]).write_text('version = "1.9.0"\n')
    (environment / fixture.CODEC_FILES[4]).write_text('__version__ = "0.32.0"\n')
    spec = {
        "root": str(tmp_path),
        "python": str(environment / "bin/python"),
        "python_sha256": "0" * 64,
        "lockfile_sha256": "1" * 64,
    }

    def check(version):
        (environment / "pyvenv.cfg").write_text(
            f"implementation = CPython\nversion_info = {version}\n"
            "include-system-site-packages = false\n"
        )
        receipt = {
            "schema": 2,
            **spec,
            "versions": fixture.CODEC_VERSIONS,
            "codec_sources": {
                name: {"path": str(environment / name), "sha256": fixture.sha(environment / name)}
                for name in fixture.CODEC_FILES
            },
        }
        receipt.pop("dependency_receipt", None)
        path = tmp_path / "receipt.json"
        path.write_text(json.dumps(receipt))
        path.chmod(0o600)
        spec["dependency_receipt"] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        fixture.dependencies(spec, verify=True)

    check(".".join(map(str, sys.version_info[:3])))
    check("3.12")
    for changed in ("3.13.0", "3.12.invalid", "3.12.999"):
        with pytest.raises(fixture.Refused, match="fixed isolated CPython"):
            check(changed)


@pytest.fixture
def protocol_reader(tmp_path, monkeypatch):
    import sys

    from scripts.gates.testsupport.cell_trigger import fixture

    monkeypatch.setitem(sys.modules, "fixture", fixture)
    from scripts.gates.testsupport.cell_trigger import capture

    data = (Path(fixture.__file__).parent / fixture.PROTOCOL).read_bytes()
    path = tmp_path / fixture.PROTOCOL
    path.write_bytes(data)
    path.chmod(0o644)
    monkeypatch.setattr(capture, "HERE", tmp_path)
    return capture, fixture, path, data


def test_committed_public_protocol_is_readable_without_private_receipt_mode(protocol_reader):
    capture, _, _, _ = protocol_reader
    assert capture.protocol()["schema"] == 1


def test_public_protocol_keeps_hash_and_symlink_guards(protocol_reader):
    capture, fixture, path, data = protocol_reader
    path.write_bytes(data + b"\n")
    with pytest.raises(fixture.Refused):
        capture.protocol()
    path.unlink()
    target = path.with_name("target.json")
    target.write_bytes(data)
    target.chmod(0o644)
    path.symlink_to(target)
    with pytest.raises((OSError, fixture.Refused)):
        capture.protocol()


def test_private_receipt_still_refuses_public_read_permissions(protocol_reader):
    _, fixture, path, _ = protocol_reader
    with pytest.raises(fixture.Refused, match="owned private stream"):
        fixture.read(path)


def test_replication_waits_for_primary_tcp_after_temporary_socket_ready(tmp_path, monkeypatch):
    from scripts.gates import cell_trigger_acceptance as creator

    monkeypatch.setattr(creator, "ports", lambda count: list(range(18000, 18000 + count)))
    monkeypatch.setattr(creator.time, "sleep", lambda _: None)

    class InitializingStores(Stores):
        def __init__(self):
            super().__init__(tmp_path, tmp_path, "fixture", {"run_id": "123"})
            self.tcp_attempts = 0
            self.tcp_ready = False

        def _clickhouse_ready(self, port):
            return True

        def create(self, role, args):
            return {"id": role}

        def command(self, *argv, timeout=120, cleanup=False):
            if "pg_isready" in argv:
                assert argv[argv.index("-h") + 1] == "127.0.0.1"
                self.tcp_attempts += 1
                if self.tcp_attempts == 1:
                    raise RuntimeError("simulated TCP startup refusal")
                self.tcp_ready = True
                return "127.0.0.1:5432 - accepting connections"
            if "psql" in argv:
                query = argv[-1]
                if query == "SELECT 1":
                    return "1"  # The temporary init server accepts Unix sockets only.
                assert self.tcp_ready, "Replication configuration raced the temporary init server"
                return "sync" if "SELECT sync_state" in query else ""
            return "1" if "clickhouse-client" in argv else "network-id"

    stores = InitializingStores()
    stores.start("test-image")
    assert stores.tcp_attempts == 2 and stores.tcp_ready


@pytest.mark.parametrize("setting", ["config_mount", "database", "data_owner"])
def test_clickhouse_creation_uses_ci_server_configuration(tmp_path, monkeypatch, setting):
    from scripts.gates import cell_trigger_acceptance as creator

    monkeypatch.setattr(creator, "ports", lambda count: list(range(18000, 18000 + count)))

    class CapturedCreation(Exception):
        pass

    class CapturingStores(Stores):
        def command(self, *argv, timeout=120, cleanup=False):
            return "network-id"

        def create(self, role, args):
            assert role == "clickhouse"
            self.creation_args = args
            raise CapturedCreation

    stores = CapturingStores(tmp_path, tmp_path, "fixture", {"run_id": "123"})
    with pytest.raises(CapturedCreation):
        stores.start("test-image")
    args = stores.creation_args
    if setting == "config_mount":
        mounts = [args[index + 1] for index, value in enumerate(args) if value == "-v"]
        for filename, section in (
            ("custom_settings_prefixes.xml", "config.d"),
            ("log_queries_readonly.xml", "users.d"),
        ):
            assert (
                str(tmp_path / "dev/clickhouse-ci" / filename)
                + ":/etc/clickhouse-server/"
                + section
                + "/"
                + filename
                + ":ro"
            ) in mounts
    elif setting == "database":
        environment = [args[index + 1] for index, value in enumerate(args) if value == "-e"]
        assert "CLICKHOUSE_DB=loom" in environment
    else:
        environment = [args[index + 1] for index, value in enumerate(args) if value == "-e"]
        owner = (tmp_path / "clickhouse-data").stat()
        assert f"CLICKHOUSE_UID={owner.st_uid}" in environment
        assert f"CLICKHOUSE_GID={owner.st_gid}" in environment
        assert not any(
            value.startswith(("CLICKHOUSE_RUN_AS_ROOT=", "CLICKHOUSE_DO_NOT_CHOWN="))
            for value in environment
        )


def test_failure_diagnostics_expose_only_allowlisted_owned_state(tmp_path):
    import json

    class DiagnosticStores(Stores):
        def command(self, *argv, timeout=120, cleanup=False):
            assert cleanup and timeout == 5
            if argv[:2] == ("docker", "inspect"):
                value = container()
                value["Config"]["Env"] = ["TOKEN=private-value"]
                value["State"] = {
                    "Status": "running",
                    "Running": True,
                    "ExitCode": 0,
                    "OOMKilled": False,
                    "Error": "private-log",
                }
                return json.dumps([value])
            return "primary:sync"

    stores = DiagnosticStores(tmp_path, tmp_path, "fixture", {"run_id": "123"})
    stores.phase = "standby_sync"
    stores.items = [{"role": "pg-primary", "name": "intent", "id": "own", "created": "birth"}]
    result = stores.failure_diagnostics()
    assert result == {
        "phase": "standby_sync",
        "containers": [
            {
                "role": "pg-primary",
                "available": True,
                "status": "running",
                "exit_code": 0,
                "oom_killed": False,
                "pg_state": {"recovery": False, "standby_sync": "sync"},
            }
        ],
    }
    assert "private" not in json.dumps(result)


def test_diagnostic_failure_preserves_primary_result_and_runs_cleanup(
    tmp_path, monkeypatch, capsys
):
    import json
    import sys

    from scripts.gates import cell_trigger_acceptance as creator

    cleaned = []

    class FailingStores(Stores):
        def start(self, image):
            raise ValueError("private primary failure")

        def failure_diagnostics(self):
            raise OSError("private diagnostic failure")

        def cleanup(self):
            cleaned.append(True)
            return []

    root = Path(sys.executable).parent.parent.parent
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(root))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_REPOSITORY"):
        monkeypatch.setenv(name, "123")
    monkeypatch.setattr(sys, "argv", ["creator", "--clickhouse-image", "test-image"])
    monkeypatch.setattr(creator, "Stores", FailingStores)
    monkeypatch.setattr(creator, "source", lambda _: {"head": "a" * 40, "tree": "b" * 40})
    monkeypatch.setattr(creator, "source_hashes", lambda _: {})
    monkeypatch.setattr(creator, "resources", lambda _: None)
    monkeypatch.setattr(creator.os, "umask", lambda _: 0)
    monkeypatch.setattr(creator.signal, "signal", lambda *_: None)
    assert creator.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert cleaned == [True]
    assert result["error_type"] == "ValueError" and result["diagnostic_error_type"] == "OSError"
    assert "private" not in json.dumps(result)
    published = json.loads((tmp_path / "cell-trigger-artifacts/result.json").read_text())
    assert (
        published["error_type"] == "ValueError" and published["diagnostic_error_type"] == "OSError"
    )


@pytest.fixture
def keeper_config_case(tmp_path, monkeypatch):
    import json
    import sys
    from types import SimpleNamespace

    from scripts.gates.testsupport.cell_trigger import fixture

    monkeypatch.setitem(sys.modules, "fixture", fixture)
    from scripts.gates.testsupport.cell_trigger import capture

    monkeypatch.setitem(sys.modules, "capture", capture)
    from scripts.gates.testsupport.cell_trigger import native

    expected = {
        "container_id": "owned-ch",
        "created_at": "birth",
        "host_pid": 123,
        "memory_limit_bytes": 5 * fixture.GIB,
        "server_limit_bytes": 9 * fixture.GIB // 2,
        "shared_buffers_bytes": 128 * 1024**2,
        "ports": [],
        "disk_path": str(tmp_path),
        "start_ticks": "start",
        "uid": 1000,
        "boot_id": "boot",
    }
    actual = {
        "Id": "owned-ch",
        "Created": "birth",
        "State": {"Running": True, "Pid": 123},
        "HostConfig": {"Memory": 5 * fixture.GIB},
        "NetworkSettings": {"Ports": {}},
        "Mounts": [{"Source": str(tmp_path)}],
    }
    calls = []
    owner = SimpleNamespace(owned=SimpleNamespace(identity=lambda _: (1, "start")))
    owner.prefix = "/keeper_map_tables\n"

    def command(role, argv, env, *, seconds=120, cleanup=False):
        calls.append((argv, cleanup))
        log = tmp_path / (role + ".log")
        if argv == ["docker", "inspect", "owned-ch"]:
            log.write_text(json.dumps([actual]))
        else:
            assert argv == [
                "docker",
                "exec",
                "owned-ch",
                "clickhouse",
                "extract-from-config",
                "--config-file",
                "/etc/clickhouse-server/config.xml",
                "--key",
                "keeper_map_path_prefix",
            ]
            log.write_text(owner.prefix)
        return {"log": log}

    owner.command = command
    original_stat, original_read = native.os.stat, Path.read_text

    def stat(path, *args, **kwargs):
        if str(path) == "/proc/123":
            return SimpleNamespace(st_uid=1000)
        return original_stat(path, *args, **kwargs)

    def read_text(path, *args, **kwargs):
        if str(path) == "/proc/sys/kernel/random/boot_id":
            return "boot\n"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(native.os, "stat", stat)
    monkeypatch.setattr(Path, "read_text", read_text)
    spec = {
        "workdir": str(tmp_path),
        "fixture_id": "fixture",
        "stores": {"clickhouse": expected},
        "ch": {"keeper_prefix": "/keeper_map_tables"},
        "databases": {"ch_schema": "bench_schema"},
        "pg_standby_dsn": "fake",
    }
    instance = native.Fixture(spec, owner)
    monkeypatch.setattr(instance, "env", lambda _: {})
    return native, instance, owner, actual, calls


@pytest.mark.asyncio
async def test_keeper_config_allows_creation_without_sql_server_setting(
    keeper_config_case, monkeypatch
):
    from contextlib import asynccontextmanager

    import asyncpg

    native, instance, _, _, calls = keeper_config_case
    instance.inspect_stores()
    commands = []
    queries = []

    class Postgres:
        async def fetchval(self, query, *args):
            return {
                "SELECT NOT pg_is_in_recovery()": True,
                "SELECT pg_is_in_recovery()": True,
                "SHOW server_version_num": "160000",
                "SHOW max_prepared_transactions": "0",
                "SHOW synchronous_standby_names": "FIRST 1 (loom_standby)",
                "SELECT pg_size_bytes(current_setting('shared_buffers'))": 128 * 1024**2,
                "SELECT EXISTS(SELECT FROM pg_stat_replication WHERE state='streaming' "
                "AND sync_state='sync' AND flush_lsn IS NOT NULL)": True,
            }[query]

        async def close(self, **kwargs):
            assert kwargs["timeout"] > 0

    class ClickHouse:
        async def command(self, query):
            commands.append(query)

    async def connect(*args, **kwargs):
        return Postgres()

    @asynccontextmanager
    async def connections(*args, **kwargs):
        yield Postgres(), ClickHouse()

    async def rows(ch, query, parameters=None):
        queries.append(query)
        if "keeper_map_path_prefix" in query:
            return []  # The real config key is absent from declared ServerSettings.
        if "max_server_memory_usage" in query:
            return [{"value": str(9 * native.GIB // 2)}]
        if "count()" in query:
            return [{"n": 0}]
        if "toString(uuid)" in query:
            return [{"id": "database-uuid"}]
        assert "system.zookeeper" in query
        return []

    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(native, "connections", connections)
    monkeypatch.setattr(native, "rows", rows)
    instance.s["stores"]["pg_primary"] = {"shared_buffers_bytes": 128 * 1024**2}
    instance.s["stores"]["pg_standby"] = {"shared_buffers_bytes": 128 * 1024**2}
    await native.stores(instance.s, "ch_schema", instance.w / "created.json")
    assert commands == ["CREATE DATABASE `bench_schema` ENGINE=Atomic"]
    assert len(calls) == 2 and calls[-1][0][-1] == "keeper_map_path_prefix"
    assert not any("keeper_map_path_prefix" in query for query in queries)


@pytest.mark.parametrize("cleanup", [False, True])
def test_keeper_config_refuses_wrong_actual_prefix(keeper_config_case, cleanup):
    native, instance, owner, _, calls = keeper_config_case
    owner.prefix = "/different-prefix\n"
    with pytest.raises(native.Refused, match="actual Keeper prefix"):
        instance.inspect_stores(cleanup=cleanup)
    assert calls[-1][1] is cleanup


def test_keeper_config_probe_follows_store_ownership_validation(keeper_config_case):
    native, instance, _, actual, calls = keeper_config_case
    actual["Created"] = "foreign-birth"
    with pytest.raises(native.Refused, match="current owned container"):
        instance.inspect_stores()
    assert [argv for argv, _ in calls] == [["docker", "inspect", "owned-ch"]]


def test_native_setup_registers_independent_append_only_writes(keeper_config_case, monkeypatch):
    native, instance, owner, _, _ = keeper_config_case
    instance.s.update(
        ports={"probe": 7790, "router": 7791, "tls": 7792, "api": 7793},
        pg_admin_dsn="postgresql://u:p@127.0.0.1:15432/postgres",
        databases={
            "pg_cell": "bench_cell",
            "pg_directory": "bench_directory",
            "ch_schema": "bench_schema",
            "ch_telemetry": "bench_telemetry",
        },
        ch={"port": 18123, "username": "fixture", "password": "test-only"},
        head="a" * 40,
        python="unused",
    )
    log = instance.w / "credentials.log"
    log.write_text("credential: lcr_" + "1" * 128 + "\ncredential: lco_" + "2" * 128 + "\n")

    def action(task, *, role="", **kwargs):
        if task == "create":
            native.write(instance.w / (role + "-created.json"), {"created": True})
        return {}

    class Registered(Exception):
        pass

    def register(path, *, token, body):
        assert path == "/v1/namespace.register"
        assert token == "fixture-token"
        assert body == {
            "org": instance.org,
            "namespace": instance.namespace,
            "owner_email": "benchmark",
            "default_conflict_policy": "append_only",
        }
        raise Registered

    monkeypatch.setattr(instance, "inspect_stores", lambda: None)
    monkeypatch.setattr(instance, "action", action)
    monkeypatch.setattr(instance, "cli", lambda *args, **kwargs: {"log": log})
    monkeypatch.setattr(instance, "command_receipt", lambda *args, **kwargs: None)
    monkeypatch.setattr(instance, "certificate", lambda: None)
    monkeypatch.setattr(instance, "start_api", lambda *args: None)
    monkeypatch.setattr(instance, "process_receipt", lambda *args: {})
    monkeypatch.setattr(owner, "start", lambda *args, **kwargs: {}, raising=False)
    monkeypatch.setattr(native, "mint_metadata", lambda *args: ({}, "fixture-token"))
    monkeypatch.setattr(instance, "http", register)
    with pytest.raises(Registered):
        instance.setup()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["auto_reinforce", "append_only"])
async def test_native_bootstrap_checks_persisted_independent_write_policy(
    keeper_config_case, monkeypatch, policy
):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    native, instance, _, _, _ = keeper_config_case
    authority = {"authority": "postgres", "deployment_id": "deployment", "epoch": 1}
    conn = SimpleNamespace(
        fetchrow=AsyncMock(return_value=authority), fetchval=AsyncMock(return_value=0)
    )

    @asynccontextmanager
    async def connected(*args, **kwargs):
        yield conn, object()

    async def rows(ch, sql, params=None):
        if ".storage_authority" in sql:
            return [authority]
        if ".namespaces " in sql:
            assert params == {"org": instance.org, "ns": instance.namespace}
            row = {"owner_email": "benchmark"}
            if "default_conflict_policy" in sql:
                row["default_conflict_policy"] = policy
            return [row]
        if ".namespace_grants " in sql:
            return [{"subject": "benchmark", "role": "admin"}]
        raise AssertionError(sql)

    monkeypatch.setattr(native, "connections", connected)
    monkeypatch.setattr(native, "rows", rows)
    output = instance.w / "bootstrap.json"
    if policy != "append_only":
        with pytest.raises(native.Refused, match="native registered namespace"):
            await native.proof(instance.s, output, bootstrap=True)
        assert not output.exists()
    else:
        await native.proof(instance.s, output, bootstrap=True)
        assert native.read(output)["namespace_registered"] is True


@pytest.mark.parametrize("role", ["inspect", "keeper-prefix", "create", "private-role"])
def test_nonzero_native_command_retains_only_structural_metadata(monkeypatch, role):
    import json
    from types import SimpleNamespace

    from scripts.gates.testsupport.cell_trigger import fixture

    owner = fixture.Owner({}, None, lambda *args, **kwargs: None)
    owner.phase, owner.serial = "setup", 5
    item = {"process": SimpleNamespace(returncode=7)}
    monkeypatch.setattr(owner, "start", lambda *args, **kwargs: item)
    monkeypatch.setattr(owner, "poll", lambda _: 7)
    with pytest.raises(fixture.Refused) as caught:
        owner.command(role, ["private-argv"], {"TOKEN": "private-token"})
    owner.select(caught.value)
    record = owner.failure_summary()["records"][0]
    assert owner.first is caught.value
    assert record["command"] == {
        "role": role if role != "private-role" else "other",
        "serial": 5,
        "returncode": 7,
    }
    assert record["site"]["file"] == "fixture.py"
    assert record["site"]["function"] == "command"
    assert "private-" not in json.dumps(record)


def test_native_failure_site_reaches_published_evidence_without_private_logs(
    keeper_config_case, monkeypatch, tmp_path, capsys
):
    import json
    import sys

    from scripts.gates.cell_trigger_acceptance import publish
    from scripts.gates.testsupport.cell_trigger import fixture

    native, instance, owner, _, _ = keeper_config_case
    context, output = tmp_path / "failure-context.json", tmp_path / "pg_cell-created.json"
    instance.context = context
    instance.s["databases"]["pg_cell"] = "bench_cell"
    instance.s["python"] = "unused"
    native.write(context, instance.s | {"root": str(tmp_path)})

    async def failed(*args, **kwargs):
        raise ValueError("private-native-secret")

    monkeypatch.setattr(native, "stores", failed)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "native.py",
            "--task",
            "create",
            "--role",
            "pg_cell",
            "--context",
            str(context),
            "--output",
            str(output),
        ],
    )
    assert native.main() == 1
    assert not output.exists()
    metadata = native.read(output.with_name(output.name + ".failure.json"), limit=4096)
    assert metadata["error_type"] == "ValueError"
    assert metadata["site"]["file"] == "native.py"
    assert metadata["site"]["function"] == "main"
    assert metadata["site"]["line"] > 0
    assert "private-native-secret" not in json.dumps(metadata) + capsys.readouterr().err
    original = fixture.Refused("private-command-secret")
    original.command = {"role": "create", "serial": 5, "returncode": 1}

    def refused(*args, **kwargs):
        raise original

    owner.command = refused
    with pytest.raises(fixture.Refused) as caught:
        instance.action("create", role="pg_cell")
    assert caught.value is original
    observer = fixture.Owner({}, None, lambda *args, **kwargs: None)
    observer.select(caught.value, phase="setup")
    evidence = observer.failure_summary()
    assert evidence["records"][0]["native"] == metadata
    assert evidence["records"][0]["command"] == original.command
    folder = tmp_path / "publication"
    (folder / "fixture").mkdir(parents=True)
    native.write(folder / "fixture/outer-result.json", {"failure_evidence": evidence})
    (folder / "fixture/private.log").write_text("private-log-secret")
    (folder / "fixture/private-settings.json").write_text('{"token":"private-settings-secret"}')
    destination = tmp_path / "published"
    publish(folder, destination, original, phase="native", diagnostic_error_type=None)
    assert {path.name for path in destination.iterdir()} == {"outer-result.json", "result.json"}
    published = json.loads((destination / "outer-result.json").read_text())
    assert published["failure_evidence"] == evidence
    assert "private-" not in json.dumps(published)


@pytest.mark.parametrize(
    "contents",
    [
        "not-json",
        '{"error_type":"private-extra"}',
        "x" * 4097,
        pytest.param("[" * 1500 + "0" + "]" * 1500, id="deep-nesting"),
    ],
)
def test_invalid_native_failure_sidecar_preserves_command_refusal(keeper_config_case, contents):
    native, instance, owner, _, _ = keeper_config_case
    instance.s["python"] = "unused"
    path = instance.w / "pg_cell-created.json.failure.json"
    path.write_text(contents)
    path.chmod(0o600)
    original = native.Refused("original")

    def refused(*args, **kwargs):
        raise original

    owner.command = refused
    with pytest.raises(native.Refused) as caught:
        instance.action("create", role="pg_cell")
    assert caught.value is original and original.native is None


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("site", ["read", "closed"])
def test_fatal_native_diagnostic_preserves_original_command_refusal(
    keeper_config_case, monkeypatch, kind, site
):
    native, instance, owner, _, _ = keeper_config_case
    instance.s["python"] = "unused"
    original = native.Refused("original")

    def refused(*args, **kwargs):
        raise original

    def diagnostic(*args, **kwargs):
        raise kind("private-diagnostic-secret")

    owner.command = refused
    monkeypatch.setattr(native, "read", lambda *args, **kwargs: {})
    monkeypatch.setattr(native, site, diagnostic)
    try:
        instance.action("create", role="pg_cell")
    except BaseException as error:  # noqa: BLE001 — assert the actual retained failure, including fatal diagnostics.
        assert error is original and original.native is None
    else:
        pytest.fail("command refusal was swallowed")


def test_clickhouse_waits_for_published_http_after_temporary_native_ready(tmp_path, monkeypatch):
    from scripts.gates import cell_trigger_acceptance as creator

    monkeypatch.setattr(creator, "ports", lambda count: list(range(18000, 18000 + count)))
    monkeypatch.setattr(creator.time, "sleep", lambda _: None)

    class InitializingClickHouse(Stores):
        def __init__(self):
            super().__init__(tmp_path, tmp_path, "fixture", {"run_id": "123"})
            self.published_attempts = 0
            self.native_attempts = 0

        def create(self, role, args):
            return {"id": role}

        def command(self, *argv, timeout=120, cleanup=False):
            if "clickhouse-client" in argv:
                self.native_attempts += 1
                return "1"  # The temporary loopback-only server already accepts native queries.
            if "pg_isready" in argv:
                return "127.0.0.1:5432 - accepting connections"
            return "sync" if "SELECT sync_state" in argv[-1] else "network-id"

        def _clickhouse_ready(self, port):
            assert port == 18000
            self.published_attempts += 1
            return self.published_attempts == 2

    stores = InitializingClickHouse()
    stores.start("test-image")
    assert stores.published_attempts == 2
    assert stores.native_attempts == 0
    assert stores.phase == "clickhouse_query"
